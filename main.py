"""
BugKSA – Saudi Football Sarcasm Bot
====================================

Railway Variables to set
─────────────────────────
Required:
  OPENAI_API_KEY    – OpenAI secret key
  X_API_KEY         – Twitter/X consumer key
  X_API_SECRET      – Twitter/X consumer secret
  X_ACCESS_TOKEN    – Twitter/X access token
  X_ACCESS_SECRET   – Twitter/X access token secret

Optional (defaults shown):
  OPENAI_MODEL      – OpenAI model name            (default: gpt-4o-mini)
  STATE_FILE_PATH   – Path to JSON state file      (default: /app/data/state.json)
  PENDING_FILE_PATH – Path to pending drafts file  (default: /app/data/pending.json)
  DRY_RUN           – "true" → never post to X     (default: false)
  RECOVERY_MODE     – "true" → only status tweets  (default: true)

Safe defaults for a previously-flagged account:
  RECOVERY_MODE=true  → replies and sniping disabled; only harmless status tweets
  DRY_RUN=true        → nothing posted to X; generated drafts saved to PENDING_FILE_PATH
Set both to "false" only when the account is cleared and ready to go live.
"""

import json
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

import tweepy
from openai import OpenAI

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bugksa")

# ── Env helpers ───────────────────────────────────────────────────────────────

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _flag(key: str, default: bool = True) -> bool:
    """Read a boolean env var. Absent → default."""
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


# ── Config ────────────────────────────────────────────────────────────────────
OPENAI_API_KEY  = _env("OPENAI_API_KEY")
X_API_KEY       = _env("X_API_KEY")
X_API_SECRET    = _env("X_API_SECRET")
X_ACCESS_TOKEN  = _env("X_ACCESS_TOKEN")
X_ACCESS_SECRET = _env("X_ACCESS_SECRET")

OPENAI_MODEL      = _env("OPENAI_MODEL",      "gpt-4o-mini")
STATE_FILE_PATH   = Path(_env("STATE_FILE_PATH",   "/app/data/state.json"))
PENDING_FILE_PATH = Path(_env("PENDING_FILE_PATH", "/app/data/pending.json"))
DRY_RUN           = _flag("DRY_RUN",         default=False)
RECOVERY_MODE     = _flag("RECOVERY_MODE",   default=True)

# ── Rate-limit constants ──────────────────────────────────────────────────────
RATE_MIN_GAP  = 45    # ≥45 seconds between any two actions
RATE_MAX_HOUR = 10    # rolling 60-minute cap (≤10–12/hr per spec)
RATE_MAX_DAY  = 100   # rolling 24-hour cap

# Recovery-mode constants (posted-flagged-account rehabilitation)
RECOVERY_MAX_DAY = 3     # original status tweets per day
RECOVERY_MIN_GAP = 7200  # ≥2 hours between recovery tweets

# ── Target club accounts ──────────────────────────────────────────────────────
TARGET_USERNAMES: list[str] = [
    # Saudi Pro League
    "Alhilal_FC", "AlNassrFC", "ittihad", "ALAHLI_FC",
    "AlQadsiah", "AlShabab_FC", "AlFaisaly_FC", "AlTaawon_FC",
    "AlFatehFC", "AlRaedFC",
    # Saudi sports media / figures
    "Hadaf_SA", "kooora",
    # English Premier League
    "ManUtd", "Arsenal", "ChelseaFC", "SpursOfficial",
    "LCFC", "Everton", "WestHam", "Wolves",
    # Champions League heavy-weights
    "realmadrid", "FCBarcelona", "ManCity", "LFC",
    "juventusfc", "PSG_inside", "FCBayern", "BVB",
    "Atleti",
]

# ── Club personality profiles ─────────────────────────────────────────────────
# lang: "ar-sa" → Saudi Arabic reply  |  "en" → English reply
CLUB_PROFILES: dict[str, dict] = {
    # Saudi Pro League ─────────────────────────────────────────────────────────
    "Alhilal_FC":    {"name": "الهلال",         "lang": "ar-sa",
                      "personality": "يتحدث من مركز التفوق المطلق كأنه يملك الدوري بالوراثة"},
    "AlNassrFC":     {"name": "النصر",           "lang": "ar-sa",
                      "personality": "مبني على الضجة والاحتفال المسبق، يعيش على المبالغة"},
    "ittihad":       {"name": "الاتحاد",         "lang": "ar-sa",
                      "personality": "فوضى منظمة، ديناميكية عاطفية، مسرح درامي من الدرجة الأولى"},
    "ALAHLI_FC":     {"name": "الأهلي",          "lang": "ar-sa",
                      "personality": "يختفي ثم يعود بقوة، بطل الكوميباك الأبدي"},
    "AlQadsiah":     {"name": "القادسية",        "lang": "ar-sa",
                      "personality": "مفاجأة الدوري، يظهر فجأة في القمة ثم يختفي كـ cache مؤقت"},
    "AlShabab_FC":   {"name": "الشباب",          "lang": "ar-sa",
                      "personality": "الفريق الذي يُعطي وعوداً أكثر من مسؤول تقني"},
    "AlFaisaly_FC":  {"name": "الفيصلي",        "lang": "ar-sa",
                      "personality": "تراث عريق لكن حظه يُشبه سيرفراً قديماً"},
    "AlTaawon_FC":   {"name": "التعاون",         "lang": "ar-sa",
                      "personality": "دائماً في المنتصف، لا صعود ولا هبوط، safe mode دائم"},
    "AlFatehFC":     {"name": "الفتح",           "lang": "ar-sa",
                      "personality": "ينام في الدوري ويصحى فجأة على الكأس"},
    "AlRaedFC":      {"name": "الرائد",          "lang": "ar-sa",
                      "personality": "يكافح البقاء كل موسم كأنه loop لا ينتهي"},
    # Saudi sports media
    "Hadaf_SA":      {"name": "هدف",             "lang": "ar-sa",
                      "personality": "يتابع الأخبار قبل حدوثها، سيرفر الأخبار الكروية"},
    "kooora":        {"name": "كووورة",           "lang": "ar-sa",
                      "personality": "الحكم الأول والأخير في الإنترنت الكروي العربي"},
    # English Premier League ───────────────────────────────────────────────────
    "ManUtd":        {"name": "Man United",       "lang": "en",
                      "personality": "living off Sir Alex's legacy like deprecated code nobody dares delete"},
    "Arsenal":       {"name": "Arsenal",          "lang": "en",
                      "personality": "always close to the title, always buffer overflow at the end"},
    "ChelseaFC":     {"name": "Chelsea",          "lang": "en",
                      "personality": "fires managers faster than an auto-deployment pipeline"},
    "SpursOfficial": {"name": "Spurs",            "lang": "en",
                      "personality": "brilliant in the first leg, crashes in the second like a beta server"},
    "LCFC":          {"name": "Leicester",        "lang": "en",
                      "personality": "one legendary patch release and then legacy mode forever"},
    "Everton":       {"name": "Everton",          "lang": "en",
                      "personality": "fighting relegation bravely every season, eternal survival mode"},
    "WestHam":       {"name": "West Ham",         "lang": "en",
                      "personality": "a whole city runs on football dreams and late goals"},
    "Wolves":        {"name": "Wolves",           "lang": "en",
                      "personality": "surprise compiler that never fully commits"},
    # European heavy-weights ───────────────────────────────────────────────────
    "realmadrid":    {"name": "Real Madrid",      "lang": "en",
                      "personality": "scripted destiny – the universe is literally running their matchday cron job"},
    "FCBarcelona":   {"name": "Barcelona",        "lang": "en",
                      "personality": "obsessed with tiki-taka like a dev who caches everything and scores nothing"},
    "ManCity":       {"name": "Man City",         "lang": "en",
                      "personality": "petrodollar-powered machine: technically perfect, emotionally zero"},
    "LFC":           {"name": "Liverpool",        "lang": "en",
                      "personality": "lifts a trophy then emotionally collapses for two seasons straight"},
    "juventusfc":    {"name": "Juventus",         "lang": "en",
                      "personality": "Serie A's grandfather – still runs on Windows XP"},
    "PSG_inside":    {"name": "PSG",              "lang": "en",
                      "personality": "buys every star but can't find a working team.exe"},
    "FCBayern":      {"name": "Bayern",           "lang": "en",
                      "personality": "crushes the Bundesliga then gets a 500 Internal Error in Europe"},
    "BVB":           {"name": "Dortmund",         "lang": "en",
                      "personality": "terrifies you in the first leg then throws a NullPointerException in the second"},
    "Atleti":        {"name": "Atlético",         "lang": "en",
                      "personality": "parks the bus so hard even the VAR system can't find the attack folder"},
}

# ── Rivalry pairs (derby detection) ──────────────────────────────────────────
RIVALRY_PAIRS: list[tuple[str, str]] = [
    ("Alhilal_FC", "AlNassrFC"),    # الكلاسيكو السعودي
    ("ittihad",    "ALAHLI_FC"),    # ديربي جدة
    ("LFC",        "ManCity"),      # Liverpool–City
    ("realmadrid", "FCBarcelona"),  # El Clásico
    ("ManUtd",     "Arsenal"),      # historic PL rivalry
    ("ManUtd",     "LFC"),          # North-West derby
    ("realmadrid", "Atleti"),       # Madrid derby
    ("juventusfc", "FCBarcelona"),  # Juve–Barca
    ("FCBayern",   "BVB"),          # Der Klassiker
]

# ── Event detection engine ────────────────────────────────────────────────────
# Order matters: trophy > goal > loss > conceded > win > generic
# NOTE: \b word boundaries don't work with Arabic script; Arabic patterns use plain search.
_EVENT_PATTERNS: dict[str, list[str]] = {
    "trophy":   [r"\bchampion(s)?\b", r"\btitle\b", r"\btrophy\b", r"\bcup\b", r"🏆",
                 "بطل", "لقب", "بطولة", "كأس"],
    "goal":     [r"\bgoal\b", r"\bscores?\b", r"\bGOAL\b", r"\bgolazo\b", r"⚽",
                 "يسجل", "هدف", "أهداف"],
    "loss":     [r"\blos(e|t|ing)\b", r"\bdefeat(ed)?\b",
                 "يخسر", "خسارة", "هزيمة", "انهيار"],
    "conceded": [r"\bconcede(d|s)?\b", r"\bgave away\b",
                 "يستقبل", "يتلقى"],
    "win":      [r"\bwin(s|ning)?\b", r"\bwon\b", r"\bvictory\b", r"\b3 points\b",
                 "يفوز", "فوز", "انتصار"],
}


def detect_event(text: str) -> str:
    """Return the dominant football event in tweet text, or 'generic'."""
    for event, patterns in _EVENT_PATTERNS.items():
        for pat in patterns:
            if re.search(pat, text, re.IGNORECASE):
                return event
    return "generic"


def detect_derby(username: str, tweet_text: str) -> bool:
    """Return True when the tweet references a known rival of `username`."""
    for pair in RIVALRY_PAIRS:
        if username in pair:
            rival = pair[1] if pair[0] == username else pair[0]
            rival_profile = CLUB_PROFILES.get(rival, {})
            rival_name    = rival_profile.get("name", rival)
            if rival.lower() in tweet_text.lower() or rival_name.lower() in tweet_text.lower():
                return True
    return False


# ── Env validation ────────────────────────────────────────────────────────────

def validate_env() -> None:
    required = {
        "OPENAI_API_KEY":  OPENAI_API_KEY,
        "X_API_KEY":       X_API_KEY,
        "X_API_SECRET":    X_API_SECRET,
        "X_ACCESS_TOKEN":  X_ACCESS_TOKEN,
        "X_ACCESS_SECRET": X_ACCESS_SECRET,
    }
    log.info("Checking env vars …")
    for key, val in required.items():
        if val:
            masked = ("*" * max(0, len(val) - 4)) + val[-4:]
        else:
            masked = "(MISSING)"
        log.info(f"  {key}: {masked}")

    missing = [k for k, v in required.items() if not v]
    if missing:
        log.error(f"Missing required env vars: {', '.join(missing)}")
        sys.exit(1)


# ── Persistent state ──────────────────────────────────────────────────────────

def load_state() -> dict:
    try:
        with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}
    # Backwards-compatible defaults
    s.setdefault("last_mention_id",     None)
    s.setdefault("last_seen_by_target", {})   # {username: last_tweet_id}
    s.setdefault("replied_tweet_ids",   [])   # dedupe set, trimmed to 500
    s.setdefault("actions_log",         [])   # Unix timestamps of all actions
    s.setdefault("target_last_acted",   {})   # {username: Unix timestamp}
    s.setdefault("recovery_tweets_log", [])   # timestamps for recovery tweets
    return s


def save_state(state: dict) -> None:
    STATE_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    state["replied_tweet_ids"]   = state.get("replied_tweet_ids",   [])[-500:]
    state["actions_log"]         = [t for t in state.get("actions_log",         []) if now - t < 86400]
    state["recovery_tweets_log"] = [t for t in state.get("recovery_tweets_log", []) if now - t < 86400]
    with open(STATE_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ── Pending drafts (DRY_RUN output) ──────────────────────────────────────────

def load_pending() -> list:
    """Load existing pending drafts; return empty list if file absent/corrupt."""
    try:
        with open(PENDING_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_pending_draft(text: str, draft_type: str = "recovery") -> None:
    """Append a generated draft to PENDING_FILE_PATH for human review.

    Each entry records:
      text         – the tweet that would have been posted
      type         – draft category (always "recovery" in recovery mode)
      generated_at – UTC ISO-8601 timestamp
      status       – "pending" (unchanged until a human promotes/discards it)
    """
    PENDING_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    drafts = load_pending()
    entry = {
        "text":         text,
        "type":         draft_type,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status":       "pending",
    }
    drafts.append(entry)
    with open(PENDING_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(drafts, f, ensure_ascii=False, indent=2)
    log.info(
        f"[DRAFT] Saved to {PENDING_FILE_PATH}  "
        f"(total pending: {len(drafts)})"
    )


# ── Global Safety Governor ────────────────────────────────────────────────────

def can_act(state: dict) -> bool:
    """Return True only when ALL three rate-limit constraints are satisfied.

    Constraints:
      1. now - actions_log[-1] >= RATE_MIN_GAP  (≥45 s gap)
      2. count(actions in last 3600s) < RATE_MAX_HOUR  (10/hr)
      3. count(actions in last 86400s) < RATE_MAX_DAY  (100/day)
    """
    now = time.time()
    log_ts: list[float] = [t for t in state.get("actions_log", []) if now - t < 86400]
    state["actions_log"] = log_ts  # prune in-memory copy

    # 1. Minimum gap
    if log_ts:
        gap = now - log_ts[-1]
        if gap < RATE_MIN_GAP:
            log.info(f"Governor: gap {gap:.0f}s < {RATE_MIN_GAP}s – skip")
            return False

    # 2. Hourly cap
    hour_count = sum(1 for t in log_ts if now - t < 3600)
    if hour_count >= RATE_MAX_HOUR:
        log.info(f"Governor: hourly cap {hour_count}/{RATE_MAX_HOUR} – skip")
        return False

    # 3. Daily cap
    if len(log_ts) >= RATE_MAX_DAY:
        log.info(f"Governor: daily cap {len(log_ts)}/{RATE_MAX_DAY} – skip")
        return False

    return True


def record_action(state: dict) -> None:
    """Append current timestamp and immediately flush state to disk."""
    state.setdefault("actions_log", []).append(time.time())
    save_state(state)


# ── API clients ───────────────────────────────────────────────────────────────

def build_clients() -> tuple:
    ai = OpenAI(api_key=OPENAI_API_KEY)
    client = tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_SECRET,
        wait_on_rate_limit=True,
    )
    return ai, client


# ── AI reply generation ───────────────────────────────────────────────────────

_STYLE_SEEDS: list[str] = [
    "cache miss", "404 offside", "timeout", "ping spike", "hotfix",
    "memory leak", "rollback", "null pointer", "stack overflow",
    "server down", "patch update", "debug mode", "laggy VAR",
    "cpu overload", "latency issue", "infinite loop", "merge conflict",
    "rate limited", "buffer overflow", "garbage collected",
    "deployment failed", "firewall breach", "kernel panic",
]

# ── Dynamic system-prompt builder ─────────────────────────────────────────────

def _build_system_prompt(
    username: str,
    event: str,
    is_derby: bool,
    lang: str,
) -> str:
    """Build a contextual system prompt tailored to club, event, and language."""
    profile      = CLUB_PROFILES.get(username, {})
    club_name    = profile.get("name", username)
    personality  = profile.get("personality", "a regular club")

    # Language rule
    if lang == "ar-sa":
        lang_rule = (
            "الرد يجب أن يكون باللهجة العربية السعودية فقط. "
            "مسموح فقط بالمصطلحات التقنية بالإنجليزي (Bug, Lag, 404…). "
            "ممنوع منعاً باتاً الردود الإنجليزية الكاملة."
        )
    else:
        lang_rule = (
            "Reply ONLY in English. "
            "Do NOT switch to Arabic. "
            "Tech terms may be English (Bug, Lag, 404…)."
        )

    # Event-mode instruction
    event_instructions: dict[str, str] = {
        "goal":     "⚽ EVENT: Goal scored – fast sarcastic celebration or mock surprise",
        "win":      "🏅 EVENT: Win – celebratory sarcasm or mock the defeated rival's weakness",
        "loss":     ("💥 EVENT — MELTDOWN MODE: heavy loss detected.\n"
                     "Use dramatic failure metaphors: server crash · 404 defense · "
                     "system collapse · critical bug · memory leak · kernel panic"),
        "trophy":   ("🏆 EVENT — TROPHY MODE: championship won.\n"
                     "Mock absent rivals. Legacy sarcasm. "
                     "Treat history like open-source code nobody else can run."),
        "conceded": "🚨 EVENT: Goal conceded – defensive failure sarcasm, VAR jokes",
        "generic":  "⚽ General football moment – sharp sarcastic tech commentary",
    }
    event_block = event_instructions.get(event, event_instructions["generic"])

    # Derby boost
    derby_block = ""
    if is_derby:
        derby_block = (
            "\n🔥 DERBY MODE ACTIVE: this is a rivalry match. "
            "Amplify sarcasm ×1.5 – maximum banter, still safe and clean."
        )

    return f"""\
You are @BugKSA – a legendary autonomous football banter AI.
Ratio: 80 % football banter + 20 % tech metaphors.
Tone: sharp, witty, mocking, playful. NEVER abusive or hateful.

🎭 Club personality for this reply:
  {club_name} → {personality}

🌍 Language rule (STRICT – breaking this = invalid reply):
  {lang_rule}

{event_block}{derby_block}

⚙️ Golden rules:
- Output exactly ONE line, ≤260 characters
- Use the provided style seed to vary the punchline
- Allowed tech vocabulary: Lag · Timeout · Bug · 404 · Patch · Deployment failed ·
  Memory leak · Server crash · Firewall breach · Cache clear · Kernel panic · Null pointer
- FORBIDDEN: politics · religion · hate · harassment · doxxing · personal attacks
- Mock teams and situations ONLY – never individuals personally
- If the tweet is sensitive or ambiguous → give a safe evasive football joke

✅ Self-check before outputting (regenerate if any check fails):
  1. Language matches the rule above
  2. Club personality ({club_name}) is reflected
  3. Event mode ({event}) is applied
  4. Sarcasm is present
  5. At least one tech metaphor/keyword is present
  6. Content is safe and clean
"""


# ── Generation quality validator ──────────────────────────────────────────────

_TECH_KEYWORDS = {
    "lag", "timeout", "bug", "404", "patch", "server", "crash", "firewall",
    "cache", "deployment", "memory", "leak", "loop", "null", "error", "stack",
    "overflow", "hotfix", "debug", "kernel", "panic", "cpu", "buffer", "ping",
    "سيرفر", "لاق", "باق",
}


def _validate_reply(reply: str, lang: str, event: str) -> tuple[bool, str]:
    """Basic generation quality check. Returns (passed, fail_reason)."""
    if len(reply) < 15:
        return False, "reply too short"

    lower = reply.lower()

    # Must contain at least one tech keyword
    if not any(kw in lower for kw in _TECH_KEYWORDS):
        return False, "no tech metaphor"

    # Meltdown / trophy mode: prefer dramatic language (soft check only – log warning)
    if event == "loss":
        heavy_terms = {"crash", "404", "collapse", "leak", "panic", "null", "bug"}
        if not any(t in lower for t in heavy_terms):
            log.debug("Meltdown mode: soft check – missing heavy failure term")

    # Hard forbidden content check
    forbidden = {"hate", "terrorist", "bomb", "kill", "attack"}
    if any(w in lower for w in forbidden):
        return False, "forbidden content detected"

    return True, ""


# ── AI reply generation ───────────────────────────────────────────────────────

def generate_reply(
    ai: OpenAI,
    tweet_text: str,
    style_seed: str,
    *,
    username: str = "",
    event: str = "generic",
    is_derby: bool = False,
    lang: str = "en",
) -> str:
    """Generate a contextual sarcastic reply with up to 3 self-validation retries."""
    system   = _build_system_prompt(username, event, is_derby, lang)
    user_msg = f"Style seed: '{style_seed}'\n\nTweet:\n{tweet_text}"

    for attempt in range(3):
        try:
            resp = ai.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user_msg},
                ],
                temperature=min(0.92 + attempt * 0.05, 1.1),
                max_completion_tokens=100,
            )
            text  = resp.choices[0].message.content.strip()
            reply = " ".join(text.splitlines()).strip()[:260]

            ok, reason = _validate_reply(reply, lang, event)
            if ok:
                if attempt > 0:
                    log.info(f"Generation check: passed on attempt {attempt + 1}")
                return reply

            log.info(f"Generation check fail (attempt {attempt + 1}/{3}): {reason} → retrying")
        except Exception as e:
            log.warning(f"OpenAI generate_reply error (attempt {attempt + 1}): {e}")

    # Fallback – guaranteed to be safe
    if lang == "ar-sa":
        return "VAR راجع الحركة… السيرفر وقف. ⚽🤖"
    return "VAR stuck in an infinite loop – system timeout. ⚽🤖"


def generate_recovery_tweet(ai: OpenAI) -> str:
    """Harmless human-like football status update for recovery mode.
    No links, no hashtags, no mentions."""
    topics = [
        "watching a match tonight",
        "thinking about last night's game",
        "impressive football statistics",
        "waiting for the weekend fixtures",
        "a classic goal I still remember",
        "how tactics have changed in modern football",
        "that feeling when your team scores late",
    ]
    prompt = (
        f"Write a single casual, human-sounding football status tweet about: "
        f"{random.choice(topics)}. "
        "Rules: no links, no hashtags, no @mentions, max 200 characters, "
        "sound like a genuine football fan, one sentence only."
    )
    try:
        resp = ai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.88,
            max_completion_tokens=80,
        )
        text = resp.choices[0].message.content.strip()
        return " ".join(text.splitlines()).strip()[:200]
    except Exception as e:
        log.warning(f"OpenAI generate_recovery_tweet error: {e}")
        return "Football: where logic goes to retire. ⚽"


# ── Tweet eligibility filter ──────────────────────────────────────────────────

_LINK_RE    = re.compile(r"https?://\S+|t\.co/\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"@\w+")


def is_eligible_club_tweet(tweet) -> tuple[bool, str]:
    """Return (eligible, reason_if_rejected).

    Rejects:
      - Retweets (text prefix or referenced_tweets type)
      - Quote tweets (referenced_tweets type)
      - Tweets containing any URL/link
      - Tweets shorter than 8 characters (after stripping links)
      - Tweets with 3 or more @mentions
    """
    text: str = tweet.text or ""

    # Retweet (text-level)
    if text.startswith("RT "):
        return False, "retweet prefix"

    # Quote tweet or retweet via referenced_tweets API field
    refs = getattr(tweet, "referenced_tweets", None)
    if refs:
        for ref in refs:
            ref_type = getattr(ref, "type", "")
            if ref_type in ("quoted", "retweeted"):
                return False, f"referenced_tweet type={ref_type}"

    # Any link → skip (likely ad or self-promo)
    if _LINK_RE.search(text):
        return False, "contains link/url"

    # Too short
    clean = _LINK_RE.sub("", text).strip()
    if len(clean) < 8:
        return False, f"too short ({len(clean)} chars)"

    # 3+ @mentions → likely a thread reply / ad
    if len(_MENTION_RE.findall(text)) >= 3:
        return False, f"too many mentions ({len(_MENTION_RE.findall(text))})"

    return True, ""


# ── Post wrappers ─────────────────────────────────────────────────────────────

def post_reply(
    client: tweepy.Client, text: str, reply_to_id: str, state: dict
) -> bool:
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would reply to {reply_to_id}: {text!r}")
        record_action(state)
        return True
    try:
        client.create_tweet(text=text, in_reply_to_tweet_id=reply_to_id, user_auth=True)
        record_action(state)
        log.info(f"✅ Replied to {reply_to_id}: {text!r}")
        return True
    except Exception as e:
        log.error(f"create_tweet (reply) failed: {e}")
        return False


def post_tweet(client: tweepy.Client, text: str, state: dict) -> bool:
    if DRY_RUN:
        log.info("[DRY_RUN] create_tweet SKIPPED (DRY_RUN=true) — draft already saved to pending.json")
        record_action(state)
        return True
    try:
        client.create_tweet(text=text, user_auth=True)
        record_action(state)
        log.info(f"✅ Tweeted: {text!r}")
        return True
    except Exception as e:
        log.error(f"create_tweet (status) failed: {e}")
        return False


# ── Bot identity & target resolution ─────────────────────────────────────────

def get_bot_identity(client: tweepy.Client) -> tuple[str, str]:
    me = client.get_me(user_auth=True)
    return str(me.data.id), str(me.data.username)


def resolve_target_ids(
    client: tweepy.Client, usernames: list[str]
) -> dict[str, str]:
    """Return {username: user_id}. Logs but does not abort on failures."""
    result: dict[str, str] = {}
    for username in usernames:
        try:
            r = client.get_user(username=username, user_auth=True)
            if r and r.data:
                result[username] = str(r.data.id)
                log.info(f"  @{username} → id={r.data.id}")
        except Exception as e:
            log.warning(f"  @{username}: resolve failed – {e}")
    return result


# ── Mentions mode ─────────────────────────────────────────────────────────────

def run_mentions_mode(
    client: tweepy.Client, ai: OpenAI, my_id: str, state: dict
) -> bool:
    """Process at most 1 mention per cycle. Returns True if an action was taken."""
    replied_set = set(state.get("replied_tweet_ids", []))

    try:
        resp = client.get_users_mentions(
            id=my_id,
            since_id=state.get("last_mention_id"),
            user_auth=True,
            max_results=10,
            tweet_fields=["id", "text", "author_id"],
        )
    except Exception as e:
        log.warning(f"Mentions fetch error: {e}")
        return False

    if not resp or not resp.data:
        log.info("Mentions: none new")
        return False

    # Always advance the cursor so old mentions are not reprocessed after restart
    if resp.meta and resp.meta.get("newest_id"):
        state["last_mention_id"] = resp.meta["newest_id"]

    # Process at most 1 per cycle
    for item in resp.data[:1]:
        tid = str(item.id)
        if tid in replied_set:
            log.info(f"Mention {tid}: already replied – skip")
            continue

        if not can_act(state):
            return False

        seed  = random.choice(_STYLE_SEEDS)
        event = detect_event(item.text)
        reply = generate_reply(
            ai, item.text, seed,
            event=event, lang="ar-sa",  # mentions default to Saudi Arabic
        )
        log.info(f"Mention {tid}: replying (seed={seed!r}, event={event})")
        if post_reply(client, reply, tid, state):
            replied_set.add(tid)
            state["replied_tweet_ids"] = list(replied_set)
            save_state(state)
            return True

    return False


# ── Sniping mode ──────────────────────────────────────────────────────────────

def run_sniping_mode(
    client: tweepy.Client,
    ai: OpenAI,
    target_ids: dict[str, str],
    state: dict,
) -> bool:
    """Try to reply to ONE eligible tweet from ONE target per cycle.
    Returns True if an action was taken."""
    replied_set = set(state.get("replied_tweet_ids", []))
    now = time.time()

    targets = list(target_ids.items())
    random.shuffle(targets)  # avoid always hitting the same account first

    for username, uid in targets:
        # Per-target 4-hour cooldown (allows broader coverage across all clubs)
        last_acted = state.get("target_last_acted", {}).get(username, 0.0)
        if now - last_acted < 14400:
            remaining_h = (14400 - (now - last_acted)) / 3600
            log.info(f"Snipe @{username}: cooldown {remaining_h:.1f}h remaining – skip")
            continue

        since_id = state.get("last_seen_by_target", {}).get(username)
        try:
            result = client.get_users_tweets(
                id=uid,
                since_id=since_id,
                user_auth=True,
                max_results=5,
                exclude=["retweets", "replies"],
                tweet_fields=["id", "text", "referenced_tweets"],
            )
        except Exception as e:
            log.warning(f"Snipe @{username}: fetch error – {e}")
            continue

        if not result or not result.data:
            log.info(f"Snipe @{username}: no new tweets")
            continue

        # Advance cursor for this target
        if result.meta and result.meta.get("newest_id"):
            state.setdefault("last_seen_by_target", {})[username] = result.meta["newest_id"]

        for tweet in result.data:
            tid = str(tweet.id)
            if tid in replied_set:
                log.info(f"Snipe @{username} {tid}: already replied – skip")
                continue

            eligible, reason = is_eligible_club_tweet(tweet)
            if not eligible:
                log.info(f"Snipe @{username} {tid}: ineligible ({reason})")
                continue

            if not can_act(state):
                return False

            seed      = random.choice(_STYLE_SEEDS)
            event     = detect_event(tweet.text)
            is_derby  = detect_derby(username, tweet.text)
            lang      = CLUB_PROFILES.get(username, {}).get("lang", "en")
            reply = generate_reply(
                ai, tweet.text, seed,
                username=username,
                event=event,
                is_derby=is_derby,
                lang=lang,
            )
            log.info(
                f"Snipe @{username} {tid}: replying "
                f"(event={event}, derby={is_derby}, lang={lang}, seed={seed!r})"
            )
            if post_reply(client, reply, tid, state):
                replied_set.add(tid)
                state["replied_tweet_ids"] = list(replied_set)
                state.setdefault("target_last_acted", {})[username] = now
                save_state(state)
                return True  # one action per cycle; stop here

    return False


# ── Recovery mode ─────────────────────────────────────────────────────────────

def run_recovery_mode(client: tweepy.Client, ai: OpenAI, state: dict) -> bool:
    """Post one harmless status tweet when within recovery limits.
    Replies are completely disabled in recovery mode.
    Returns True if an action was taken."""
    now = time.time()
    rec_log: list[float] = [
        t for t in state.get("recovery_tweets_log", []) if now - t < 86400
    ]
    state["recovery_tweets_log"] = rec_log

    # Recovery daily cap
    if len(rec_log) >= RECOVERY_MAX_DAY:
        log.info(f"Recovery: daily cap {len(rec_log)}/{RECOVERY_MAX_DAY} – skip")
        return False

    # Recovery minimum gap (2 hours)
    if rec_log:
        gap = now - rec_log[-1]
        if gap < RECOVERY_MIN_GAP:
            log.info(f"Recovery: gap {gap/3600:.1f}h < {RECOVERY_MIN_GAP/3600:.0f}h – skip")
            return False

    # Global governor also applies
    if not can_act(state):
        return False

    text = generate_recovery_tweet(ai)

    # Always log the draft clearly so it is visible in Railway logs
    log.info(f"[DRAFT] Recovery tweet generated ──────────────────────────")
    log.info(f"[DRAFT] {text!r}")
    log.info(f"[DRAFT] ────────────────────────────────────────────────────")

    # In dry-run, save to pending.json for human review; never call create_tweet
    if DRY_RUN:
        save_pending_draft(text, draft_type="recovery")

    if post_tweet(client, text, state):
        state["recovery_tweets_log"].append(now)
        save_state(state)
        return True

    return False


# ── Persistent storage probe ──────────────────────────────────────────────────

def probe_data_dir() -> None:
    """Verify the data directory is writable, then seed missing data files.

    1. Writes and removes a sentinel file to confirm write access.
       Fails fast (sys.exit) if the Railway Volume is not mounted so the
       bot never silently writes to an ephemeral container layer.
    2. Creates state.json and pending.json with empty defaults on first run
       so their presence on the Volume is immediately visible after deploy.
    """
    data_dir = STATE_FILE_PATH.parent
    sentinel = data_dir / ".write_probe"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("ok")
        sentinel.unlink()
    except OSError as exc:
        log.error(
            f"Storage probe FAILED: cannot write to {data_dir}\n"
            f"  {exc}\n"
            f"  → Create a Railway Volume named 'bot_data' and mount it at /app/data\n"
            f"    CLI: railway volume create bot_data\n"
            f"         railway volume mount bot_data --service <id> --mount-path /app/data"
        )
        sys.exit(1)

    log.info(f"Storage probe: {data_dir} is writable")

    # ── First-run file initialisation ────────────────────────────────────────
    if not STATE_FILE_PATH.exists():
        default_state: dict = {
            "last_mention_id":     None,
            "last_seen_by_target": {},
            "replied_tweet_ids":   [],
            "actions_log":         [],
            "target_last_acted":   {},
            "recovery_tweets_log": [],
        }
        STATE_FILE_PATH.write_text(
            json.dumps(default_state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info(f"  Created {STATE_FILE_PATH} (first run)")

    if not PENDING_FILE_PATH.exists():
        PENDING_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        PENDING_FILE_PATH.write_text("[]", encoding="utf-8")
        log.info(f"  Created {PENDING_FILE_PATH} (first run)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    log.info("=" * 60)
    log.info("BugKSA Bot – Starting up")
    log.info("=" * 60)

    validate_env()
    probe_data_dir()

    log.info(f"  DRY_RUN       : {DRY_RUN}  {'← no posts will reach X' if DRY_RUN else '← LIVE posting enabled'}")
    log.info(f"  RECOVERY_MODE : {RECOVERY_MODE}  {'← replies/sniping disabled' if RECOVERY_MODE else '← full mode'}")
    log.info(f"  OPENAI_MODEL  : {OPENAI_MODEL}")
    log.info(f"  STATE_FILE    : {STATE_FILE_PATH}")
    log.info(f"  PENDING_FILE  : {PENDING_FILE_PATH}  {'← drafts written here' if DRY_RUN else '← not used (DRY_RUN=false)'}")
    log.info(f"  Rate limits   : gap≥{RATE_MIN_GAP}s | {RATE_MAX_HOUR}/hr | {RATE_MAX_DAY}/day")
    log.info(f"  Recovery caps : {RECOVERY_MAX_DAY}/day | gap≥{RECOVERY_MIN_GAP//3600}h")
    log.info(f"  Targets       : {len(TARGET_USERNAMES)} club accounts (inactive in recovery mode)")

    ai, client = build_clients()
    state = load_state()

    try:
        my_id, my_username = get_bot_identity(client)
        log.info(f"  Bot identity  : @{my_username} (id={my_id})")
    except Exception as e:
        log.error(f"Twitter authentication failed: {e}")
        sys.exit(1)

    target_ids: dict[str, str] = {}
    if not RECOVERY_MODE:
        log.info("Resolving target account IDs …")
        target_ids = resolve_target_ids(client, TARGET_USERNAMES)
        log.info(f"Resolved {len(target_ids)}/{len(TARGET_USERNAMES)} targets.")

    log.info("=" * 60)
    log.info("Poll loop starting …")

    cycle = 0
    while True:
        cycle += 1
        log.info(f"── Cycle {cycle} " + "─" * 40)

        acted = False

        if RECOVERY_MODE:
            # Replies disabled; only harmless status tweets
            acted = run_recovery_mode(client, ai, state)
        else:
            # Normal mode: mentions first, then sniping
            acted = run_mentions_mode(client, ai, my_id, state)
            if not acted:
                acted = run_sniping_mode(client, ai, target_ids, state)

        if not acted:
            log.info("Cycle complete: no action taken.")

        save_state(state)

        sleep_s = random.randint(45, 90)  # 45–90 seconds per spec
        log.info(f"Sleeping {sleep_s}s …")
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
