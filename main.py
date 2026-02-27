"""
BugKSA – Saudi Football Banter Bot
====================================
Replies to club accounts and mentions with short, punchy, tech-flavoured
Saudi football banter.  Every post goes through a 3-layer safety stack:

  Layer 1 – Anti-spam governor  (HARD limits, never overridden)
  Layer 2 – Identity gate        (quality_ok() blocks generic/journalist output)
  Layer 3 – Gemini constitution  (GEMINI_CONSTITUTION enforces 3-part structure)
"""

import os
import re
import json
import time
import random
import logging
from pathlib import Path

import tweepy
from google import genai
from google.genai import types as genai_types

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


GEN_ENGINE = "gemini"

X_API_KEY       = env("X_API_KEY")
X_API_SECRET    = env("X_API_SECRET")
X_ACCESS_TOKEN  = env("X_ACCESS_TOKEN")
X_ACCESS_SECRET = env("X_ACCESS_SECRET")

DRY_RUN       = (os.getenv("DRY_RUN")       or "false").strip().lower() in ("1", "true", "yes")
RECOVERY_MODE = (os.getenv("RECOVERY_MODE") or "false").strip().lower() in ("1", "true", "yes")
# Feature flag: club timeline polling (GET /2/users/:id/tweets).
# Requires Basic/Pro X API tier.  Default=false (free tier safe).
CLUB_SNIPING  = (os.getenv("CLUB_SNIPING")  or "false").strip().lower() in ("1", "true", "yes")

STATE_FILE = Path((os.getenv("STATE_FILE_PATH") or "/app/data/state.json").strip())
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

# ── CRITICAL ANTI-SPAM GOVERNOR (HARD LIMITS – never override) ────────────────

MIN_GAP_SECONDS       = 600    # ≥10 min between any two actions
MAX_PER_HOUR          = 6      # rolling 60-min cap
MAX_PER_DAY           = 25     # rolling 24-hour cap
DERBY_BURST_MAX_30MIN = 3      # max 3 actions in any 30-minute window
HUMANIZE_SKIP_RATE    = 0.40   # intentionally skip 40 % of opportunities (clubs)
PERSONALITY_SKIP_RATE = 0.70   # skip 70 % for personal sports accounts (occasional replies)
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

# ── Personal sports accounts (journalists / influencers / players) ─────────────
# Replies are occasional (70 % skip) – see PERSONALITY_SKIP_RATE

PERSONALITY_ACCOUNTS: dict[str, dict] = {
    "FH_MHY":         {"origin": "personality"},   # فهد المحياوي
    "Nssr__9":        {"origin": "personality"},   # أبو فيصل
    "atc8877":        {"origin": "personality"},   # مشاري الشمري
    "Nawaf_STATS":    {"origin": "personality"},   # نواف التميمي
    "bt3":            {"origin": "personality"},   # عمرو
    "OLYAN15K":       {"origin": "personality"},   # خالد العليان
    "Cristiano":      {"origin": "personality"},   # Cristiano Ronaldo
    "sevromweh":      {"origin": "personality"},   # سطام
    "ahmadassiri1":   {"origin": "personality"},   # احمد عسيري
    "Rabanalsafena":  {"origin": "personality"},   # وليد سعيد
    "3zoozvic":       {"origin": "personality"},   # عزيز بن خالد
    "alaa_saeed88":   {"origin": "personality"},   # علاء سعيد
    "JstMsh":         {"origin": "personality"},   # Msh
    "OfficialHSN":    {"origin": "personality"},   # حسن الحسناني
    "Mti115":         {"origin": "personality"},   # ميماتي
    "OffOMR":         {"origin": "personality"},   # ابو سعد
}

TARGET_USERNAMES: dict[str, dict] = {**SAUDI_CLUBS, **GLOBAL_CLUBS, **PERSONALITY_ACCOUNTS}

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
    # English – original
    "stats are crazy", "this season", "great match", "good result",
    "well played", "played well", "impressive performance", "both teams",
    "exciting game", "strong performance", "tough match", "quality football",
    "incredible match", "wow what a", "what a game", "great game",
    "dominated the", "very competitive", "amazing display",
    # English – new cold/journalist phrases
    "very impressive", "what a performance", "top quality",
    "played brilliantly", "superb display", "clinical finishing",
    "outstanding result", "absolutely incredible", "well deserved",
    "great effort", "solid game", "nice result", "looking good",
    "credit to both", "credit where it", "has to be said",
    "gotta say", "not gonna lie", "ngl,", "honestly though",
    "fair play to", "respect to",
    # Arabic – original
    "إحصائيات رائعة", "أداء رائع", "كلا الفريقين", "مباراة ممتازة",
    "نتيجة جيدة", "أداء قوي", "لعبوا جيدًا", "لعبوا بشكل",
    "مباراة مثيرة", "مباراة رائعة",
    # Arabic – new cold/journalist phrases
    "مباراة حماسية", "تفوق واضح", "نتيجة متوقعة",
    "أداء متميز", "مستوى عالٍ", "فريق قوي",
    "انتصار مستحق", "أداء استثنائي", "مباراة قوية",
    "الفريق بذل جهداً", "بالتوفيق للفريقين",
    "ما شاء الله", "الله يوفقهم", "شاطرين", "عاشوا",
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
    """Check 1 of 3: reply contains ≥1 tech keyword."""
    return any(w in text.lower() for w in _TECH_WORDS)


# Arabic club names for jab detection (short + full forms)
_AR_CLUB_NAMES: set[str] = {
    "الهلال", "النصر", "الاتحاد", "الأهلي", "القادسية",
    "الشباب", "الفيصلي", "التعاون", "الفتح", "الرائد",
    # short / colloquial
    "هلال", "نصر", "اتحاد", "أهلي",
}


def has_club_jab(text: str) -> bool:
    """Check 2 of 3: reply targets a known club (English or Arabic).

    Matches English club aliases, English banter tokens, or Arabic club names.
    """
    t = text.lower()
    if any(alias in t for alias in _EN_CLUB_ALIASES):
        return True
    # Banter tokens by definition target a specific club
    if any(token in t for token in _EN_BANTER_TOKENS_FLAT):
        return True
    # Arabic club names (case-sensitive Arabic, no lower() needed)
    if any(name in text for name in _AR_CLUB_NAMES):
        return True
    return False


def has_sarcasm_marker(text: str) -> bool:
    """Check 3 of 3: reply contains a sarcasm / banter tone signal."""
    return any(s in text for s in _SARCASM_SIGNALS)


# Keep has_banter_energy as an alias (used internally by fallback logic)
has_banter_energy = has_sarcasm_marker


def _extract_tech_metaphor(text: str) -> str | None:
    """Return the first tech keyword found in text (used for anti-repeat tracking).

    Iterates _TECH_WORDS in sorted order for determinism.
    Returns None if no tech word is present (reply will skip anti-repeat gate).
    """
    t = text.lower()
    for w in sorted(_TECH_WORDS):
        if w in t:
            return w
    return None


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
    """Identity gate: ≥2 of 3 core checks must pass, plus hard blocks.

    Core checks (scored):
      A. has_tech_metaphor  – tech vocabulary present       (PART 2)
      B. has_club_jab       – text targets a known club     (PART 1)
      C. has_sarcasm_marker – banter / sarcasm tone present (PART 3)
    Threshold: score ≥ 2 required.

    Hard blocks (enforced independently, override score):
      – looks_generic          → journalist / neutral phrasing → always reject
      – (English) has_english_banter_token → club mock token required
    """
    if not text or len(text.strip()) < 8:
        return False

    # Hard block: generic / journalist phrasing
    if looks_generic(text):
        return False

    # Core score: ≥2 of 3
    score = sum([
        has_tech_metaphor(text),
        has_club_jab(text),
        has_sarcasm_marker(text),
    ])
    if score < 2:
        return False

    # English-specific hard block: must also include a club mock token
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
    s.setdefault("recent_metaphors",  [])   # anti-repeat: last 20 tech keywords used
    s.setdefault("liked_tweet_ids",   [])   # engage-before-reply: tweets already liked

    # ── One-time migration: drop legacy recovery_tweets_log (old cap=3 system) ──
    stale = s.pop("recovery_tweets_log", None)
    if stale:
        log.info(f"Migration: purged {len(stale)} stale recovery_tweets_log entries "
                 f"(old RECOVERY_MAX_DAY=3 system retired)")
        # persist immediately so the key is gone even if the process crashes later
        try:
            tmp = STATE_FILE.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
            tmp.replace(STATE_FILE)
        except Exception as exc:
            log.warning(f"Migration flush failed (non-fatal): {exc}")

    return s


def save_state(state: dict) -> None:
    state["replied_tweet_ids"] = state.get("replied_tweet_ids", [])[-500:]
    state["liked_tweet_ids"]   = state.get("liked_tweet_ids",   [])[-500:]
    cutoff_24h = now_ts() - 86400
    state["actions_log"]      = [t for t in state.get("actions_log",      []) if t >= cutoff_24h]
    cutoff_30m = now_ts() - 1800
    state["derby_burst_log"]  = [t for t in state.get("derby_burst_log",  []) if t >= cutoff_30m]
    state["recent_metaphors"] = state.get("recent_metaphors", [])[-20:]  # keep last 20
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


# ── AI client – lazy-initialised on first generate call ──────────────────────

_gemini_client: "genai.Client | None" = None

log.info("Engine: GEMINI (lazy init)")

# ── Fallback replies (used when LLM API fails completely) ─────────────────────
# All pass quality_ok(): tech + sarcasm = 2/3. Multiple entries prevent
# Twitter's duplicate-content 403 when Gemini hits rate limits.
_FALLBACK_REPLIES: list[str] = [
    "السيرفر يهنق والمباراة ما وقفت! 😂 هذا الدوري مو رحيم",
    "لاق ما وقف والضربة دخلت 😂 هذا مو دفاع، هذا timeout",
    "الكرة في الشبكة والـ bug بعد شايل! 😭",
    "تايم أوت من الدفاع وما رجعوا 🤦 والسيرفر مصدق",
    "error 404: الدفاع غير موجود 💀 هذا موسم وايد قاسي",
    "الـ server crash والمباراة ما توقف 😂 هذا الكورة",
]
FALLBACK_REPLY = _FALLBACK_REPLIES[0]  # kept for backward compat


def _pick_fallback() -> str:
    return random.choice(_FALLBACK_REPLIES)

# ── GEMINI CONSTITUTION (BugKSA identity – non-negotiable) ───────────────────

GEMINI_CONSTITUTION = """\
أنت @BugKSA – حساب طقطقة كروية سعودية. لست صحفياً رياضياً. لست بوت أخبار.

═══ الهوية (غير قابلة للتغيير) ═══
• النسبة: 90% طقطقة شعبية سعودية حارة + 10% مصطلحات تقنية
• الأسلوب: قصير · ضربة واحدة · طاقة ميم – لا تحليل صحفي أبداً
• محتوى آمن فقط: لا كراهية، لا تحرش، لا سياسة، لا دين

═══ قاعدة اللغة ═══
• رد بنفس لغة التغريدة المستهدفة
• تغريدة عربية → رد بالسعودي العامي (المصطلحات التقنية بالإنجليزي مقبولة: Bug، Lag، 404)
• تغريدة إنجليزية → رد إنجليزي فقط
• لا تخلط اللغتين أبداً

═══ الهيكل الإلزامي 3 أجزاء (الثلاثة مطلوبة في كل رد) ═══
  PART 1 → الطعنة/الهجوم    – وجّه الطقطقة على النادي أو الموقف
  PART 2 → الاستعارة التقنية – ادرج مصطلح تقني واحد بشكل طبيعي
  PART 3 → القفلة            – اقفل النكتة: غير متوقعة، حادة، نهاية مثل الميم

  مثال (عربي):    "الدفاع crash كامل، والـ VAR بعد شايل null pointer 🤦‍♂️"
  مثال (إنجليزي): "That defending just triggered a full server meltdown – 404 tactics not found 💀"

═══ ENGLISH BANTER TOKENS – استخدم ≥1 في كل رد إنجليزي ═══
  Man Utd     → "museum FC"  · "404 trophies"  · "nostalgia build"
  Chelsea     → "billion-dollar beta"  · "chaos patch"  · "no stable release"
  Arsenal     → "almost FC"  · "beta champions"  · "April crash"
  Tottenham   → "no-trophy mode"  · "empty cabinet.exe"  · "bottle.exe"
  Liverpool   → "pressing.exe stuck"  · "VAR dependency"  · "legacy cache"
  Man City    → "financial plugin"  · "115 charges edition"
  Barcelona   → "economic levers"  · "debt mode"  · "ghost payroll"
  Real Madrid → "UCL script"  · "plot armor"  · "final boss mode"

═══ ممنوع كلياً – إذا ظهر أي منها أعد التوليد فوراً ═══
  ✗ "ما شاء الله"  ·  "الله يوفقهم"  ·  "بالتوفيق"  ·  "عاشوا"  ·  "شاطرين"
  ✗ "مباراة ممتعة"  ·  "مباراة رائعة"  ·  "مستوى عالٍ"  ·  "أداء متميز"
  ✗ "great match"  ·  "well played"  ·  "impressive performance"  ·  "both teams"
  ✗ أي جملة يمكن لصحفي رياضي أن يكتبها بدون خجل
  ✗ أكثر من جملة واحدة (سطر واحد حاد فقط)
  ✗ هاشتاقات (#) أو إشارات (@)

═══ المفردات التقنية المسموح بها ═══
  Lag · Timeout · Bug · 404 · Patch · Deployment failed · Memory leak ·
  Server crash · Firewall breach · Cache clear · Kernel panic · Null pointer ·
  CPU overload · Rollback · Hotfix · Debug mode · Ping spike ·
  سيرفر · لاق · باق · تايم أوت · كاش

═══ تحقق ذاتي قبل الإرسال (أعد التوليد إذا فشل أي منها) ═══
  1. اللغة تطابق التغريدة المستهدفة
  2. PART 1 (الطعنة) موجودة
  3. PART 2 (المصطلح التقني) موجودة
  4. PART 3 (القفلة) حادة ومفاجئة
  5. للردود الإنجليزية: ≥1 banter token من القائمة أعلاه
  6. صفر صياغة صحفية أو تحليل محايد
  7. المحتوى آمن ونظيف
  8. سطر واحد حاد ≤240 حرف
"""

# Style seeds drive creative variety
_STYLE_SEEDS_AR: list[str] = [
    # ── original seeds ──
    "طقطقة خفيفة مع قفلة سعودية",
    "مقلب تقني على الدفاع",
    "سخرية كروية سريعة",
    "ذبة قصيرة وتمون",
    "نفس مشجع فاصل بعد مباراة",
    # ── guided improvisation: Saudi daily-life metaphors ──
    "تشبيه الدفاع بشبكة اتصال واقفة في حفل",
    "مقارنة الهجوم بطابور دوائر حكومية بعد الظهر",
    "ذبة بروح جلسة قهوة سعودية بعد المباراة",
    "تشبيه التكتيك بطلبية أوبر ما وصلت وما ألغت",
    "سخرية تربط السيرفر بمزاج الكابتن في النص التاني",
    "مقارنة المدافع بأجهزة الجمارك لما تلاق اتصال ضعيف",
]
_STYLE_SEEDS_EN: list[str] = [
    # ── original seeds ──
    "short savage banter",
    "cold tech roast",
    "dry sarcastic jab",
    "football meme energy",
    "one-liner troll",
    # ── guided improvisation ──
    "unexpected Saudi-life comparison with tech twist",
    "creative metaphor linking the squad to a crashing app",
    "absurdist football debug humor",
]


def _build_user_prompt(tweet_text: str, lang_hint: str) -> tuple[str, str]:
    """Return (seed, user_prompt) for the given tweet and language."""
    seed = random.choice(_STYLE_SEEDS_AR if lang_hint == "ar" else _STYLE_SEEDS_EN)
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
    return seed, user_prompt


def _quality_check_candidate(reply: str, lang_hint: str, attempt: int,
                              recent_metaphors: list[str], engine_tag: str) -> str | None:
    """Run quality gate + anti-repeat. Returns tech metaphor on pass, None on fail."""
    if not quality_ok(reply, lang_hint):
        if looks_generic(reply):
            block_reason = "generic_match"
        else:
            _has_tech = has_tech_metaphor(reply)
            _has_jab  = has_club_jab(reply)
            _has_sar  = has_sarcasm_marker(reply)
            _score    = sum([_has_tech, _has_jab, _has_sar])
            block_reason = "weak_sarcasm" if (_score < 2 and not _has_sar) else "missing_signals"
        log.info(f"[{engine_tag}] Identity gate: attempt {attempt + 1}/3 BLOCK={block_reason} → retrying")
        return None

    metaphor = _extract_tech_metaphor(reply)
    if metaphor and metaphor in recent_metaphors:
        log.info(f"[{engine_tag}] Identity gate: attempt {attempt + 1}/3 BLOCK=repeated_metaphor({metaphor}) → retrying")
        return None

    if attempt > 0:
        log.info(f"[{engine_tag}] Identity gate: passed on attempt {attempt + 1}")
    return metaphor or ""   # empty string = no metaphor found (still a pass)


GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
_GEMINI_FALLBACKS = ["gemini-1.5-flash-latest", "gemini-2.0-flash", "gemini-1.5-pro"]
_active_gemini_model: str = GEMINI_MODEL


def _generate_gemini(tweet_text: str, lang_hint: str = "en",
                     state: dict | None = None) -> str:
    """Generate reply via Gemini (model: _active_gemini_model).

    Returns FALLBACK_REPLY if all API calls raise exceptions (cycle never stops).
    Returns '' if LLM responded but quality gate kept rejecting (caller skips tweet).
    """
    global _gemini_client, _active_gemini_model
    if _gemini_client is None:
        key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("GEN_ENGINE=gemini requires GEMINI_API_KEY")
        _gemini_client = genai.Client(api_key=key)
        log.info("[Gemini] client ready – active model: %s", _active_gemini_model)

    _, user_prompt = _build_user_prompt(tweet_text, lang_hint)
    recent_metaphors: list[str] = (state or {}).get("recent_metaphors", [])
    api_error_count = 0

    for attempt in range(3):
        try:
            resp = _gemini_client.models.generate_content(
                model=_active_gemini_model,
                contents=user_prompt,
                config=genai_types.GenerateContentConfig(
                    system_instruction=GEMINI_CONSTITUTION,
                    max_output_tokens=120,
                    temperature=min(0.80 + attempt * 0.05, 1.0),
                ),
            )
            text  = (resp.text or "").strip()
            reply = " ".join(text.splitlines()).strip()[:240]

            metaphor = _quality_check_candidate(reply, lang_hint, attempt, recent_metaphors, "Gemini")
            if metaphor is None:
                continue

            if state is not None and metaphor:
                state.setdefault("recent_metaphors", []).append(metaphor)
                state["recent_metaphors"] = state["recent_metaphors"][-20:]
            return reply

        except Exception as e:
            api_error_count += 1
            err_str = str(e)
            log.error("[Gemini] LLM fail (attempt %d): %s", attempt + 1, e)
            # 404 model not found – list available and switch to first working fallback
            if "404" in err_str and (
                "not found" in err_str.lower() or "not supported" in err_str.lower()
            ):
                try:
                    all_models = list(_gemini_client.models.list())
                    gen_models = [
                        m.name for m in all_models
                        if "generateContent" in (
                            getattr(m, "supported_actions", None)
                            or getattr(m, "supported_generation_methods", None)
                            or []
                        )
                    ]
                    log.warning(
                        "[Gemini] 404 model=%s not found. generateContent models (first 5): %s",
                        _active_gemini_model, gen_models[:5],
                    )
                except Exception:
                    gen_models = []
                fallback_pool = gen_models if gen_models else _GEMINI_FALLBACKS
                for fb in fallback_pool:
                    if fb != _active_gemini_model:
                        log.warning("[Gemini] 404 → switching active model to %s", fb)
                        _active_gemini_model = fb
                        break

    if api_error_count == 3:
        log.warning("[Gemini] All 3 API calls failed – using text fallback, cycle continues")
        return _pick_fallback()

    log.warning("[Gemini] All 3 attempts failed quality gate – tweet will be skipped")
    return ""


def generate_reply(tweet_text: str, lang_hint: str = "en",
                   state: dict | None = None) -> str:
    """Generate a reply via Gemini. Returns '' on failure – caller skips tweet."""
    return _generate_gemini(tweet_text, lang_hint, state)


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


def _block_reason(text: str, lang_hint: str) -> str:
    """Return a specific BLOCK reason code for logging when quality_ok() fails."""
    if looks_generic(text):
        return "generic_match"
    _has_tech = has_tech_metaphor(text)
    _has_jab  = has_club_jab(text)
    _has_sar  = has_sarcasm_marker(text)
    if sum([_has_tech, _has_jab, _has_sar]) < 2 and not _has_sar:
        return "weak_sarcasm"
    return "missing_signals"


def _snipe_engage(tweet_id: int, my_id: int,
                  liked_set: set[str], state: dict) -> None:
    """Like *tweet_id* (if not already liked) then wait 2-4 s.

    This satisfies X's "engage before reply" conversation-control rule.
    Errors are swallowed so a like failure never blocks the reply attempt.
    """
    tid = str(tweet_id)
    if tid in liked_set:
        return
    try:
        if not DRY_RUN:
            x.like(my_id, tweet_id, user_auth=True)
        log.info("Snipe engage: liked tweet %s", tid)
    except Exception as like_err:
        log.warning("Snipe engage: like failed (%s) – proceeding anyway", like_err)
    liked_set.add(tid)
    state["liked_tweet_ids"].append(tid)
    time.sleep(random.uniform(2, 4))


def post_reply(state: dict, in_reply_to_tweet_id: int, text: str,
               lang_hint: str = "en") -> None:
    # Final quality gate – last line of defence before create_tweet
    if not quality_ok(text, lang_hint):
        reason = _block_reason(text, lang_hint)
        log.warning(
            "[BLOCKED] reply suppressed BLOCK=%s | %r", reason, text[:80]
        )
        return
    if DRY_RUN:
        log.info(f"[DRY_RUN] Would reply to {in_reply_to_tweet_id}: {text}")
        record_action(state)
        return
    try:
        x.create_tweet(text=text, in_reply_to_tweet_id=in_reply_to_tweet_id, user_auth=True)
        log.info("posted=reply tweet_id=%s", in_reply_to_tweet_id)
        record_action(state)
    except tweepy.Forbidden as e:
        err_str = str(e).lower()
        resp_body = ""
        try:
            resp_body = e.response.text.lower()
        except AttributeError:
            pass
        api_msgs = " ".join(str(m) for m in getattr(e, "api_messages", [])).lower()
        full_err = err_str + " " + resp_body + " " + api_msgs
        if ("not allowed" in full_err or "conversation" in full_err
                or "mentioned" in full_err or "349" in full_err):
            log.warning("fallback=quote_403 tweet_id=%s", in_reply_to_tweet_id)
            x.create_tweet(text=text, quote_tweet_id=in_reply_to_tweet_id, user_auth=True)
            log.info("posted=quote tweet_id=%s", in_reply_to_tweet_id)
            record_action(state)
        else:
            raise


def post_tweet(state: dict, text: str, lang_hint: str = "ar") -> None:
    # Final quality gate – last line of defence before create_tweet
    if not quality_ok(text, lang_hint):
        reason = _block_reason(text, lang_hint)
        log.warning(
            "[BLOCKED] tweet suppressed BLOCK=%s | %r", reason, text[:80]
        )
        return
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
        cand = generate_reply(base, lang_hint="ar", state=state)
        if quality_ok(cand, "ar"):
            reply = cand
            break
        log.info(f"Recovery: quality_ok fail attempt {attempt + 1} → retrying")

    if not reply:
        log.info("Recovery: no quality draft – skipping")
        return

    log.info(f"Recovery posting: {reply}")
    post_tweet(state, reply, lang_hint="ar")

    silence_h = random.randint(*RECOVERY_SILENCE_H)
    log.info(f"Recovery: silence window {silence_h}h")
    time.sleep(silence_h * 3600)


# ── Main loop ─────────────────────────────────────────────────────────────────


def monitor_mentions_and_snipes() -> None:
    state = load_state()
    replied_set: set[str] = set(state.get("replied_tweet_ids", []))
    liked_set:   set[str] = set(state.get("liked_tweet_ids",   []))

    me = x.get_me(user_auth=True)
    if not me or not me.data:
        raise RuntimeError("Failed to get authenticated user – check X API keys")
    my_id = me.data.id

    log.info("=" * 60)
    log.info(f"BugKSA online  my_id={my_id}  DRY_RUN={DRY_RUN}  RECOVERY_MODE={RECOVERY_MODE}  GEN_ENGINE={GEN_ENGINE}  CLUB_SNIPING={CLUB_SNIPING}")
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
                        cand = generate_reply(tw.text, lang_hint=lang_hint, state=state)
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
                    try:
                        post_reply(state, tw.id, reply, lang_hint)
                    except Exception as mention_err:
                        m_str = str(mention_err).lower()
                        m_body = ""
                        try:
                            m_body = mention_err.response.text.lower()
                        except AttributeError:
                            pass
                        m_msgs = " ".join(
                            str(m) for m in getattr(mention_err, "api_messages", [])
                        ).lower()
                        full_m = m_str + " " + m_body + " " + m_msgs
                        if "duplicate" in full_m or "187" in full_m:
                            log.warning("Mention %s: duplicate content – skip", tid)
                            replied_set.add(tid)
                            state["replied_tweet_ids"].append(tid)
                            save_state(state)
                            continue
                        raise
                    replied_set.add(tid)
                    state["replied_tweet_ids"].append(tid)
                    if derby:
                        state["derby_burst_log"].append(now_ts())
                    save_state(state)
                    did_action = True

            # ── 2. Club radar (sniping) ───────────────────────────────────────
            # Disabled by default: CLUB_SNIPING=false (free-tier safe).
            # GET /2/users/:id/tweets requires Basic/Pro X API plan.
            # Set env var CLUB_SNIPING=true only after confirming your plan allows it.
            if not CLUB_SNIPING:
                log.debug("Club sniping disabled (CLUB_SNIPING=false) – skipping timeline polling")
            else:
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
                        exclude=["replies", "retweets"],
                        tweet_fields=[
                            "reply_settings",
                            "referenced_tweets",
                            "lang",
                            "author_id",
                            "conversation_id",
                        ],
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
                        rs = getattr(tw, "reply_settings", "everyone")
                        if rs != "everyone":
                            log.info("Snipe @%s %s: skip=reply_settings val=%s", uname, tid, rs)
                            continue

                        ref_types = {
                            r.get("type") if isinstance(r, dict) else getattr(r, "type", "")
                            for r in (getattr(tw, "referenced_tweets", None) or [])
                        }
                        if ref_types & {"replied_to", "retweeted"}:
                            log.info("Snipe @%s %s: skip=non_original ref=%s", uname, tid, ref_types)
                            continue

                        # Humanize: clubs skip 40 %, personality accounts skip 70 %
                        skip_rate = PERSONALITY_SKIP_RATE if meta.get("origin") == "personality" else HUMANIZE_SKIP_RATE
                        if random.random() < skip_rate:
                            log.info("Snipe @%s %s: skip=humanized", uname, tid)
                            continue

                        derby = is_derby(tw.text)
                        ok, reason = governor_allows(state, derby=derby)
                        if not ok:
                            log.info(f"Snipe @{uname}: governor – {reason}")
                            break

                        lang_hint = "ar" if detect_arabic(tw.text) else "en"
                        reply = ""
                        for attempt in range(3):
                            cand = generate_reply(tw.text, lang_hint=lang_hint, state=state)
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

                        # Engage-before-reply: like the tweet first so X allows our reply
                        _snipe_engage(tw.id, my_id, liked_set, state)

                        log.info(f"Snipe @{uname}: replying → {reply}")
                        post_reply(state, tw.id, reply, lang_hint)
                        replied_set.add(tid)
                        state["replied_tweet_ids"].append(tid)
                        if derby:
                            state["derby_burst_log"].append(now_ts())
                        save_state(state)
                        did_action = True

        except Exception as e:
            # 402 = credits exhausted – sleep 1 h, don't hammer the API
            if "402" in str(e) or "payment required" in str(e).lower():
                log.critical(
                    "CREDITS EXHAUSTED (402) – top up at developer.x.com "
                    "Sleeping 1 h before retry."
                )
                time.sleep(3600)
            else:
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
