"""
BugKSA – Saudi Football Banter Bot
====================================
Replies to club accounts and mentions with short, punchy, tech-flavoured
Saudi football banter.  Every post goes through a 3-layer safety stack:

  Layer 1 – Anti-spam governor  (HARD limits, never overridden)
  Layer 2 – Identity gate        (quality_ok() blocks generic/journalist output)
  Layer 3 – OpenAI constitution  (SYSTEM_CONSTITUTION enforces 3-part structure)
"""

import os
import re
import json
import time
import random
import logging
from pathlib import Path

import tweepy
from openai import OpenAI

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bugksa")

# ── Config (ENV) ──────────────────────────────────────────────────────────────


def env(name: str, required: bool = True) -> str:
    v = (os.getenv(name) or "").strip()
    if required and not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


OPENAI_API_KEY  = env("OPENAI_API_KEY")
OPENAI_MODEL    = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
X_API_KEY       = env("X_API_KEY")
X_API_SECRET    = env("X_API_SECRET")
X_ACCESS_TOKEN  = env("X_ACCESS_TOKEN")
X_ACCESS_SECRET = env("X_ACCESS_SECRET")

DRY_RUN       = (os.getenv("DRY_RUN")       or "false").strip().lower() in ("1", "true", "yes")
RECOVERY_MODE = (os.getenv("RECOVERY_MODE") or "false").strip().lower() in ("1", "true", "yes")

STATE_FILE = Path((os.getenv("STATE_FILE_PATH") or "/app/data/state.json").strip())
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── CRITICAL ANTI-SPAM GOVERNOR (HARD LIMITS – never override) ────────────────

MIN_GAP_SECONDS       = 600    # ≥10 min between any two actions
MAX_PER_HOUR          = 6      # rolling 60-min cap
MAX_PER_DAY           = 25     # rolling 24-hour cap
DERBY_BURST_MAX_30MIN = 3      # max 3 actions in any 30-minute window
HUMANIZE_SKIP_RATE    = 0.40   # intentionally skip 40 % of opportunities
HUMANIZE_EXTRA_LOW    = 300    # +5 min after posting (humanized gap)
HUMANIZE_EXTRA_HIGH   = 900    # +15 min after posting
RECOVERY_SILENCE_H    = (2, 3) # 2-3 h silence window after a burst

# ── Club targets ──────────────────────────────────────────────────────────────

SAUDI_CLUBS: dict[str, dict] = {
    "Alhilal_FC": {"origin": "saudi"},
    "AlNassrFC":  {"origin": "saudi"},
    "ittihad":    {"origin": "saudi"},
    "ALAHLI_FC":  {"origin": "saudi"},
    "AlQadsiah":  {"origin": "saudi"},
    "AlShabab_FC":{"origin": "saudi"},
    "AlFaisaly_FC":{"origin":"saudi"},
    "AlTaawon_FC":{"origin": "saudi"},
    "AlFatehFC":  {"origin": "saudi"},
    "AlRaedFC":   {"origin": "saudi"},
}

GLOBAL_CLUBS: dict[str, dict] = {
    "realmadrid":  {"origin": "global"},
    "FCBarcelona": {"origin": "global"},
    "ManUtd":      {"origin": "english"},
    "Arsenal":     {"origin": "english"},
    "ChelseaFC":   {"origin": "english"},
    "SpursOfficial":{"origin":"english"},
    "LCFC":        {"origin": "english"},
    "LFC":         {"origin": "english"},
    "ManCity":     {"origin": "english"},
    "juventusfc":  {"origin": "global"},
    "PSG_inside":  {"origin": "global"},
    "FCBayern":    {"origin": "global"},
    "BVB":         {"origin": "global"},
    "Atleti":      {"origin": "global"},
}

TARGET_USERNAMES: dict[str, dict] = {**SAUDI_CLUBS, **GLOBAL_CLUBS}

RIVAL_PAIRS: list[tuple[str, str]] = [
    ("Alhilal_FC",  "AlNassrFC"),
    ("ittihad",     "ALAHLI_FC"),
    ("LFC",         "ManCity"),
    ("realmadrid",  "FCBarcelona"),
    ("ManUtd",      "Arsenal"),
]

# ── Identity gate: quality filters ───────────────────────────────────────────

# Generic / journalist phrases → auto-reject
_GENERIC_PHRASES: list[str] = [
    # English
    "stats are crazy", "this season", "great match", "good result",
    "well played", "played well", "impressive performance", "both teams",
    "exciting game", "strong performance", "tough match", "quality football",
    "incredible match", "wow what a", "what a game", "great game",
    "dominated the", "very competitive", "amazing display",
    # Arabic
    "إحصائيات رائعة", "أداء رائع", "كلا الفريقين", "مباراة ممتازة",
    "نتيجة جيدة", "أداء قوي", "لعبوا جيدًا", "لعبوا بشكل",
    "مباراة مثيرة", "مباراة رائعة",
]

# Tech keywords – at least one must appear (PART 2 of the 3-part structure)
_TECH_WORDS: set[str] = {
    "lag", "timeout", "bug", "404", "patch", "server", "crash", "firewall",
    "cache", "deployment", "memory", "leak", "loop", "null", "error", "stack",
    "overflow", "hotfix", "debug", "kernel", "panic", "cpu", "buffer", "ping",
    "rollback", "deploy",
    # Extended – covers banter-token tech vocabulary
    "beta", "plugin", "script", "exe", "edition", "build", "reboot", "mode",
    "سيرفر", "لاق", "باق", "تايم أوت",
}

# Banter / sarcasm energy signals – at least one must appear (PART 3 tone)
_SARCASM_SIGNALS: set[str] = {
    "?", "!", "💀", "😂", "🤣", "🤦", "😭", "🤡", "💔", "🔥",
    "bro", "lol", "smh", "wtf",
    "يا ", "خلاص", "ما ", "والله",
}

# Per-club English banter tokens  (≥1 required in any English reply)
# Each value is the canonical Twitter handle mapped to its mock lexicon.
_EN_BANTER_TOKENS: dict[str, list[str]] = {
    "ManUtd":        ["museum fc", "404 trophies", "nostalgia build", "glory days patch",
                      "old trafford.exe"],
    "ChelseaFC":     ["billion-dollar beta", "chaos patch", "no stable release",
                      "owner swap edition", "chelseafc reboot"],
    "Arsenal":       ["almost fc", "beta champions", "april crash", "always next year.exe",
                      "bottle mode"],
    "SpursOfficial": ["no-trophy mode", "empty cabinet.exe", "bottle.exe",
                      "final-stage crash", "lilywhite timeout"],
    "LFC":           ["pressing.exe stuck", "var dependency", "legacy cache",
                      "klopp.exe exited", "anfield nostalgia build"],
    "ManCity":       ["financial plugin", "115 charges edition", "fpl owner mode",
                      "fair play firewall", "city bot"],
    "FCBarcelona":   ["economic levers", "debt mode", "ghost payroll",
                      "barca token", "laporta.dll"],
    "realmadrid":    ["ucl script", "plot armor", "final boss mode",
                      "referee.dll", "bernabeu cheat code"],
    "juventusfc":    ["calciopoli cache", "old lady patch", "scudetto rollback",
                      "juventus bios"],
    "PSG_inside":    ["qsi.exe", "galactico overflow", "trophies not found in europe",
                      "psg kernel"],
    "FCBayern":      ["bundesliga autopilot", "german efficiency build",
                      "rekordmeister loop", "bundesliga.exe"],
    "BVB":           ["sell-first protocol", "almost champions.exe",
                      "dortmund 404", "silverware not found"],
    "Atleti":        ["grind mode fc", "defensive kernel", "simeone.exe",
                      "0-0 build"],
    "LCFC":          ["miracle patch", "2016 legacy build", "foxes memory leak",
                      "vardy.dll"],
}

# Flat set of all tokens for O(1) lookup
_EN_BANTER_TOKENS_FLAT: set[str] = {
    token
    for tokens in _EN_BANTER_TOKENS.values()
    for token in tokens
}

# Club-recognition aliases – if none match, the English banter check passes through
# (handles Saudi clubs or unrecognised clubs tweeting in English)
_EN_CLUB_ALIASES: set[str] = {
    "manchester united", "man utd", "man united",
    "chelsea",
    "arsenal", "gunners",
    "tottenham", "spurs",
    "liverpool",
    "manchester city", "man city",
    "barcelona", "barca",
    "real madrid", "madrid",
    "juventus", "juve",
    "psg", "paris saint-germain",
    "bayern", "fc bayern",
    "dortmund", "bvb",
    "atletico",
    "leicester",
}


def looks_generic(text: str) -> bool:
    t = text.lower()
    if any(p in t for p in _GENERIC_PHRASES):
        return True
    if len(t.split()) > 25 and not any(w in t for w in _TECH_WORDS):
        return True
    return False


def has_tech_metaphor(text: str) -> bool:
    return any(w in text.lower() for w in _TECH_WORDS)


def has_banter_energy(text: str) -> bool:
    return any(s in text for s in _SARCASM_SIGNALS)


def has_english_banter_token(text: str) -> bool:
    """English-specific check: reply must contain ≥1 club mock token.

    Normalisation: underscores and .exe suffixes are collapsed to spaces
    before matching so tokens like "april crash" match "April_crash.exe".

    Pass-through rule: if no recognised club alias appears in the text
    (e.g. the tweet is about a Saudi club) the check is waived – the
    existing tech + banter energy gates are sufficient in that case.
    """
    raw = text.lower()
    # Normalise: underscore → space, strip common file-extension noise
    normalised = raw.replace("_", " ").replace(".exe", "").replace(".dll", "")
    # Token matched (raw or normalised) → pass
    if any(token in raw for token in _EN_BANTER_TOKENS_FLAT):
        return True
    if any(token in normalised for token in _EN_BANTER_TOKENS_FLAT):
        return True
    # No recognised target club alias in text → waive the token requirement
    if not any(alias in raw for alias in _EN_CLUB_ALIASES):
        return True
    # Known club present but no banter token → reject
    return False


def quality_ok(text: str, lang_hint: str = "en") -> bool:
    """Identity gate – all checks must pass, otherwise the reply is rejected.

    Check 1: no generic/journalist phrasing
    Check 2: contains a tech metaphor  (PART 2 of the 3-part structure)
    Check 3: contains banter/sarcasm energy  (PART 3 tone)
    Check 4: (English only) ≥1 club mock / banter token
    """
    if not text or len(text.strip()) < 8:
        return False
    if looks_generic(text):
        return False
    if not has_tech_metaphor(text):
        return False
    if not has_banter_energy(text):
        return False
    if lang_hint == "en" and not has_english_banter_token(text):
        return False
    return True


# ── State ─────────────────────────────────────────────────────────────────────


def now_ts() -> int:
    return int(time.time())


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                s = json.load(f)
        except Exception:
            s = {}
    else:
        s = {}
    s.setdefault("last_mention_id",   None)
    s.setdefault("replied_tweet_ids", [])
    s.setdefault("actions_log",       [])   # Unix timestamps of all actions (last 24 h)
    s.setdefault("last_seen_by_user", {})   # user_id → last processed tweet id
    s.setdefault("last_action_ts",    0)
    s.setdefault("derby_burst_log",   [])   # timestamps in last 30 min
    s.setdefault("next_action_after", 0.0)  # humanized gate: earliest allowed next post
    return s


def save_state(state: dict) -> None:
    state["replied_tweet_ids"] = state.get("replied_tweet_ids", [])[-500:]
    cutoff_24h = now_ts() - 86400
    state["actions_log"]      = [t for t in state.get("actions_log",      []) if t >= cutoff_24h]
    cutoff_30m = now_ts() - 1800
    state["derby_burst_log"]  = [t for t in state.get("derby_burst_log",  []) if t >= cutoff_30m]
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


def record_action(state: dict) -> None:
    """Log timestamp, set humanized next-action window, flush to disk."""
    t = now_ts()
    state.setdefault("actions_log", []).append(t)
    state["last_action_ts"] = t
    extra = random.randint(HUMANIZE_EXTRA_LOW, HUMANIZE_EXTRA_HIGH)
    state["next_action_after"] = t + MIN_GAP_SECONDS + extra
    log.info(
        f"Governor: next window in {(MIN_GAP_SECONDS + extra) / 60:.1f} min "
        f"(10 min gap + {extra // 60} min humanized delay)"
    )
    save_state(state)


# ── Anti-spam governor ────────────────────────────────────────────────────────


def governor_allows(state: dict, derby: bool = False) -> tuple[bool, str]:
    """Return (True, 'ok') only when ALL constraints are satisfied.

    Constraints (in order):
      0. Humanized extra gap  (next_action_after)
      1. Hard minimum gap     (≥10 min)
      2. Hourly cap           (≤6 / hr)
      3. Daily cap            (≤25 / day)
      4. Derby burst cap      (≤3 in 30 min)  – only for derby events
    """
    now = now_ts()
    log_ts = state.get("actions_log", [])
    last   = state.get("last_action_ts", 0)

    # 0. Humanized extra gap
    next_after = state.get("next_action_after", 0.0)
    if now < next_after:
        wait_m = (next_after - now) / 60
        return False, f"humanized_gap ({wait_m:.1f} min remaining)"

    # 1. Hard minimum gap
    if last and (now - last) < MIN_GAP_SECONDS:
        return False, f"min_gap ({now - last}s < {MIN_GAP_SECONDS}s)"

    # 2. Hourly cap
    if sum(1 for t in log_ts if now - t < 3600) >= MAX_PER_HOUR:
        return False, "hourly_cap"

    # 3. Daily cap
    if len(log_ts) >= MAX_PER_DAY:
        return False, "daily_cap"

    # 4. Derby burst cap
    if derby:
        burst = state.get("derby_burst_log", [])
        if sum(1 for t in burst if now - t < 1800) >= DERBY_BURST_MAX_30MIN:
            return False, "derby_burst_cap"

    return True, "ok"


# ── OpenAI client ─────────────────────────────────────────────────────────────

ai = OpenAI(api_key=OPENAI_API_KEY)

# ── SYSTEM CONSTITUTION (BugKSA identity – non-negotiable) ───────────────────

SYSTEM_CONSTITUTION = """\
You are @BugKSA – a Saudi football banter account. NOT a sports journalist. NOT a news bot.

═══ IDENTITY (NON-NEGOTIABLE) ═══
• Ratio: 80 % Saudi football sarcasm/طقطقة + 20 % tech metaphors
• Tone: short · punchy · meme-like cadence – NEVER journalist-style or neutral analysis
• Safe sarcasm ONLY: no hate, harassment, slurs, doxxing, sexual content, politics, religion

═══ LANGUAGE RULE ═══
• Reply in the SAME language as the target tweet
• Arabic tweet → Saudi Arabic reply  (tech terms in English are OK: Bug, Lag, 404)
• English tweet → English reply ONLY
• NEVER mix languages in one reply

═══ MANDATORY 3-PART STRUCTURE (all three required in every reply) ═══
  PART 1 → TARGET/JAB    – aim the banter at the club, match, or situation
  PART 2 → TECH METAPHOR – weave in ONE tech keyword naturally
  PART 3 → PUNCHLINE     – land the joke: unexpected, sharp, meme-like ending

  Example (Arabic):  "الدفاع crash كامل، والـ VAR بعد شايل null pointer 🤦‍♂️"
  Example (English): "That defending just triggered a full server meltdown – 404 tactics not found 💀"

═══ ENGLISH BANTER TOKENS – use ≥1 per English reply ═══
  Man Utd     → "museum FC"  · "404 trophies"  · "nostalgia build"
  Chelsea     → "billion-dollar beta"  · "chaos patch"  · "no stable release"
  Arsenal     → "almost FC"  · "beta champions"  · "April crash"
  Tottenham   → "no-trophy mode"  · "empty cabinet.exe"  · "bottle.exe"
  Liverpool   → "pressing.exe stuck"  · "VAR dependency"  · "legacy cache"
  Man City    → "financial plugin"  · "115 charges edition"
  Barcelona   → "economic levers"  · "debt mode"  · "ghost payroll"
  Real Madrid → "UCL script"  · "plot armor"  · "final boss mode"

  Accept examples:
    "Arsenal running April_crash.exe again 💀"
    "Chelsea still in billion-dollar beta – no stable release detected 😂"
    "United nostalgia build loading… 404 trophies not found 🤦"

═══ BANNED OUTPUT – regenerate immediately if any of these appear ═══
  ✗ "great performance tonight"  ·  "stats are impressive"  ·  "team is dominating"
  ✗ "Stats are crazy this season" or any neutral stats observation
  ✗ "Impressive performance tonight" or any generic praise
  ✗ "Both teams played well" – neutral = auto-rejected
  ✗ Any sentence a sports journalist could write without embarrassment
  ✗ More than ONE sentence (one punchy line only)
  ✗ Hashtags (#) or @mentions

═══ ALLOWED TECH VOCABULARY ═══
  Lag · Timeout · Bug · 404 · Patch · Deployment failed · Memory leak ·
  Server crash · Firewall breach · Cache clear · Kernel panic · Null pointer ·
  CPU overload · Rollback · Hotfix · Debug mode · Ping spike ·
  سيرفر · لاق · باق · تايم أوت · كاش

═══ SELF-CHECK before outputting (regenerate if ANY fails) ═══
  1. Language matches the target tweet
  2. PART 1 (target/jab) is present
  3. PART 2 (tech metaphor keyword) is present
  4. PART 3 (punchline/meme ending) lands the joke
  5. For English replies: ≥1 club banter token is present (see ENGLISH BANTER TOKENS above)
  6. ZERO journalist phrasing or neutral analysis
  7. Content is safe and clean
  8. Single punchy line ≤240 characters
"""

# Style seeds drive creative variety
_STYLE_SEEDS_AR: list[str] = [
    "طقطقة خفيفة مع قفلة سعودية",
    "مقلب تقني على الدفاع",
    "سخرية كروية سريعة",
    "ذبة قصيرة وتمون",
    "نفس مشجع فاصل بعد مباراة",
]
_STYLE_SEEDS_EN: list[str] = [
    "short savage banter",
    "cold tech roast",
    "dry sarcastic jab",
    "football meme energy",
    "one-liner troll",
]


def generate_reply(tweet_text: str, lang_hint: str = "en") -> str:
    """Generate a banter reply, retrying up to 3 times until quality_ok() passes."""
    seed = random.choice(_STYLE_SEEDS_AR if lang_hint == "ar" else _STYLE_SEEDS_EN)

    # English user prompt includes explicit banter-token reminder
    if lang_hint == "en":
        structure_line = (
            "Must follow the 3-part structure: "
            "(1) jab at the club/situation  (2) tech metaphor  (3) sharp meme-like punchline. "
            "For English: embed ≥1 club banter token "
            "(e.g. '404 trophies', 'nostalgia build', 'beta champions', 'chaos patch', "
            "'plot armor', 'financial plugin', 'debt mode', 'no-trophy mode')."
        )
    else:
        structure_line = (
            "Must follow the 3-part structure: "
            "(1) jab at the club/situation  (2) tech metaphor  (3) sharp meme-like punchline."
        )

    user_prompt = (
        f"Style seed: {seed}\n\n"
        f"Target tweet:\n{tweet_text}\n\n"
        f"Write ONE reply tweet now. {structure_line}"
    )
    for attempt in range(3):
        try:
            resp = ai.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": SYSTEM_CONSTITUTION},
                    {"role": "user",   "content": user_prompt},
                ],
                temperature=min(0.90 + attempt * 0.05, 1.1),
                max_completion_tokens=120,
            )
            text  = (resp.choices[0].message.content or "").strip()
            reply = " ".join(text.splitlines()).strip()[:240]

            if quality_ok(reply, lang_hint):
                if attempt > 0:
                    log.info(f"Identity gate: passed on attempt {attempt + 1}")
                return reply

            log.info(f"Identity gate: attempt {attempt + 1}/3 failed quality_ok → retrying")
        except Exception as e:
            log.warning(f"OpenAI error (attempt {attempt + 1}): {e}")

    # Fallback – guaranteed to be safe and on-brand
    if lang_hint == "ar":
        return "VAR راجع الحركة… السيرفر وقف. ⚽🤖"
    return "VAR stuck in an infinite loop – system timeout. ⚽🤖"


def detect_arabic(text: str) -> bool:
    return bool(re.search(r"[\u0600-\u06FF]", text))


# ── X / Twitter client ────────────────────────────────────────────────────────

x = tweepy.Client(
    consumer_key=X_API_KEY,
    consumer_secret=X_API_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_SECRET,
    wait_on_rate_limit=True,
)


def post_reply(state: dict, in_reply_to_tweet_id: int, text: str) -> None:
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would reply to {in_reply_to_tweet_id}: {text}")
        record_action(state)
        return
    x.create_tweet(text=text, in_reply_to_tweet_id=in_reply_to_tweet_id, user_auth=True)
    record_action(state)


def post_tweet(state: dict, text: str) -> None:
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would tweet: {text}")
        record_action(state)
        return
    x.create_tweet(text=text, user_auth=True)
    record_action(state)


# ── Helpers ───────────────────────────────────────────────────────────────────


def resolve_user_ids(usernames: dict[str, dict]) -> dict[str, dict]:
    resolved: dict[str, dict] = {}
    for uname, meta in usernames.items():
        try:
            u = x.get_user(username=uname, user_auth=True)
            if u and u.data:
                resolved[uname] = {**meta, "id": str(u.data.id)}
            else:
                log.warning(f"Could not resolve @{uname}")
        except Exception as e:
            log.warning(f"resolve @{uname}: {e}")
    return resolved


def is_derby(tweet_text: str) -> bool:
    t = tweet_text.lower()
    return any(a.lower() in t and b.lower() in t for a, b in RIVAL_PAIRS)


# ── Recovery mode ─────────────────────────────────────────────────────────────


def run_recovery_mode(state: dict) -> None:
    """Post one safe banter tweet in low-frequency mode. Replies disabled."""
    ok, reason = governor_allows(state, derby=False)
    if not ok:
        log.info(f"Recovery: skip – {reason}")
        return

    base = "الدوري هذا كأنه سيرفر تحت ضغط… اللي دفاعه يهنق لا يلوم إلا نفسه."
    reply = ""
    for attempt in range(3):
        cand = generate_reply(base, lang_hint="ar")
        if quality_ok(cand, "ar"):
            reply = cand
            break
        log.info(f"Recovery: quality_ok fail attempt {attempt + 1} → retrying")

    if not reply:
        log.info("Recovery: no quality draft – skipping")
        return

    log.info(f"Recovery posting: {reply}")
    post_tweet(state, reply)

    silence_h = random.randint(*RECOVERY_SILENCE_H)
    log.info(f"Recovery: silence window {silence_h}h")
    time.sleep(silence_h * 3600)


# ── Main loop ─────────────────────────────────────────────────────────────────


def monitor_mentions_and_snipes() -> None:
    state = load_state()
    replied_set: set[str] = set(state.get("replied_tweet_ids", []))

    me = x.get_me(user_auth=True)
    if not me or not me.data:
        raise RuntimeError("Failed to get authenticated user – check X API keys")
    my_id = me.data.id

    log.info("=" * 60)
    log.info(f"BugKSA online  my_id={my_id}  DRY_RUN={DRY_RUN}  RECOVERY_MODE={RECOVERY_MODE}")
    log.info(
        f"Governor: gap≥{MIN_GAP_SECONDS // 60}min | burst≤{DERBY_BURST_MAX_30MIN}/30min | "
        f"{MAX_PER_HOUR}/hr | {MAX_PER_DAY}/day | humanize_skip={int(HUMANIZE_SKIP_RATE * 100)}%"
    )
    log.info("=" * 60)

    targets = resolve_user_ids(TARGET_USERNAMES)
    log.info(f"Resolved {len(targets)}/{len(TARGET_USERNAMES)} targets.")

    cycle = 0
    while True:
        cycle += 1
        log.info(f"── Cycle {cycle} " + "─" * 40)
        did_action = False

        try:
            if RECOVERY_MODE:
                run_recovery_mode(state)
                continue

            # ── 1. Mentions ───────────────────────────────────────────────────
            mentions = x.get_users_mentions(
                id=my_id,
                since_id=state.get("last_mention_id"),
                max_results=10,
                user_auth=True,
            )
            if mentions and mentions.data:
                if mentions.meta and mentions.meta.get("newest_id"):
                    state["last_mention_id"] = mentions.meta["newest_id"]

                for tw in mentions.data[:1]:  # at most 1 per cycle
                    tid = str(tw.id)
                    if tid in replied_set:
                        log.info(f"Mention {tid}: already replied – skip")
                        continue

                    # Humanize: intentionally skip 40 % of opportunities
                    if random.random() < HUMANIZE_SKIP_RATE:
                        log.info(f"Mention {tid}: humanized skip – will retry next cycle")
                        continue

                    derby = is_derby(tw.text)
                    ok, reason = governor_allows(state, derby=derby)
                    if not ok:
                        log.info(f"Mention {tid}: governor – {reason}")
                        break

                    lang_hint = "ar" if detect_arabic(tw.text) else "en"
                    reply = ""
                    for attempt in range(3):
                        cand = generate_reply(tw.text, lang_hint=lang_hint)
                        if quality_ok(cand, lang_hint):
                            reply = cand
                            break
                        log.info(f"Mention {tid} quality_ok fail attempt {attempt + 1} → retrying")

                    if not reply:
                        log.info(f"Mention {tid}: no quality reply after 3 attempts – skip")
                        replied_set.add(tid)
                        state["replied_tweet_ids"].append(tid)
                        save_state(state)
                        continue

                    log.info(f"Mention {tid}: replying → {reply}")
                    post_reply(state, tw.id, reply)
                    replied_set.add(tid)
                    state["replied_tweet_ids"].append(tid)
                    if derby:
                        state["derby_burst_log"].append(now_ts())
                    save_state(state)
                    did_action = True

            # ── 2. Club radar (sniping) ───────────────────────────────────────
            for uname, meta in targets.items():
                uid = meta.get("id")
                if not uid:
                    continue

                last_seen = state["last_seen_by_user"].get(uid)
                tweets = x.get_users_tweets(
                    id=uid,
                    since_id=last_seen,
                    max_results=5,
                    user_auth=True,
                )
                if not tweets or not tweets.data:
                    continue

                if tweets.meta and tweets.meta.get("newest_id"):
                    state["last_seen_by_user"][uid] = tweets.meta["newest_id"]

                for tw in tweets.data:
                    tid = str(tw.id)
                    if tid in replied_set:
                        continue
                    if tw.text.strip().startswith("RT"):
                        continue
                    if tw.text.count("http") >= 2:
                        replied_set.add(tid)
                        state["replied_tweet_ids"].append(tid)
                        save_state(state)
                        continue

                    # Humanize: intentionally skip 40 % of opportunities
                    if random.random() < HUMANIZE_SKIP_RATE:
                        log.info(f"Snipe @{uname} {tid}: humanized skip")
                        continue

                    derby = is_derby(tw.text)
                    ok, reason = governor_allows(state, derby=derby)
                    if not ok:
                        log.info(f"Snipe @{uname}: governor – {reason}")
                        break

                    lang_hint = "ar" if detect_arabic(tw.text) else "en"
                    reply = ""
                    for attempt in range(3):
                        cand = generate_reply(tw.text, lang_hint=lang_hint)
                        if quality_ok(cand, lang_hint):
                            reply = cand
                            break
                        log.info(f"Snipe @{uname} quality_ok fail attempt {attempt + 1} → retrying")

                    if not reply:
                        log.info(f"Snipe @{uname}: no quality reply – skip")
                        replied_set.add(tid)
                        state["replied_tweet_ids"].append(tid)
                        save_state(state)
                        continue

                    log.info(f"Snipe @{uname}: replying → {reply}")
                    post_reply(state, tw.id, reply)
                    replied_set.add(tid)
                    state["replied_tweet_ids"].append(tid)
                    if derby:
                        state["derby_burst_log"].append(now_ts())
                    save_state(state)
                    did_action = True

        except Exception as e:
            log.error(f"Cycle error: {e}")
            time.sleep(60)
            continue

        if not did_action:
            log.info("Cycle complete: no action taken.")

        # Cycle sleep: 5-10 min
        sleep_s = random.randint(300, 600)
        log.info(f"Sleeping {sleep_s}s ({sleep_s // 60}m {sleep_s % 60}s) …")
        time.sleep(sleep_s)


if __name__ == "__main__":
    monitor_mentions_and_snipes()
