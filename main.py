"""
BugKSA – Saudi Football Banter Bot (API-Football Edition)
==========================================================
Fetches live events from API-Football (fixtures & live scores),
posts them in 80 % serious + 20 % sarcastic punchline format.

Architecture
------------
  Layer 1 – Daily cap         (≤ MAX_TWEETS_PER_DAY)
  Layer 2 – Gemini punchline  (GEMINI_CONSTITUTION enforces identity)
  Layer 3 – Live mode         (goals + cards every 2 min during matches)

Environment variables
---------------------
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET
  GEMINI_API_KEY
  FOOTBALL_API_KEY          (API key from api-sports.io direct subscription)
  GEMINI_MODEL              (default: gemini-1.5-flash)
  DRY_RUN                   (1/true/yes → no real posts)
  STATE_FILE_PATH           (default: /app/data/state.json)
"""

import os
import json
import time
import random
import logging
from pathlib import Path
from datetime import datetime, timedelta

import requests
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

def _env(name: str, required: bool = True) -> str:
    v = (os.getenv(name) or "").strip()
    if required and not v:
        raise RuntimeError(f"Missing required env var: {name}")
    return v


# Twitter / X
X_API_KEY       = _env("X_API_KEY")
X_API_SECRET    = _env("X_API_SECRET")
X_ACCESS_TOKEN  = _env("X_ACCESS_TOKEN")
X_ACCESS_SECRET = _env("X_ACCESS_SECRET")

# Gemini
GEMINI_API_KEY  = _env("GEMINI_API_KEY")
GEMINI_MODEL    = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

# API-Sports (direct subscription – https://api-sports.io)
FOOTBALL_API_KEY  = _env("FOOTBALL_API_KEY")
FOOTBALL_API_BASE = "https://v3.football.api-sports.io"

# Bot behaviour
DRY_RUN           = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
STATE_FILE        = Path(os.getenv("STATE_FILE_PATH", "/app/data/state.json").strip())
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

MAX_TWEETS_PER_DAY   = 50        # absolute daily ceiling
POLL_INTERVAL_LIVE_S = 2 * 60   # 2 minutes when live matches are happening
POLL_INTERVAL_IDLE_S = 15 * 60  # 15 minutes when no live matches
HUMANIZE_MIN_S       = 15        # minimum sleep between posts within a cycle
HUMANIZE_MAX_S       = 45        # maximum sleep between posts within a cycle

# API-Football leagues / season
# FOOTBALL_LEAGUE_IDS: comma-separated league IDs (default: 307 = Saudi Pro League)
# Example for Saudi + Premier League: FOOTBALL_LEAGUE_IDS=307,39
LEAGUE_IDS     = [
    int(x.strip())
    for x in os.getenv("FOOTBALL_LEAGUE_IDS", "307").split(",")
    if x.strip()
]
CURRENT_SEASON = int(os.getenv("FOOTBALL_SEASON", "2025"))

# Fixture statuses considered "in progress" (checked in Python, no live= param needed)
_LIVE_STATUSES = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE"}

# ── Gemini Constitution (system prompt – used verbatim) ───────────────────────

GEMINI_CONSTITUTION = """
أنت الذكاء الاصطناعي الساخر لحساب تويتر الرياضي @BugKSA.
مهمتك: قراءة الخبر الذي يزودك به المستخدم، وكتابة "قفلة ساخرة" (Punchline) واحدة فقط.

القواعد:
1. اللهجة: سعودية بيضاء، لاذعة، ومستفزة كروياً (طقطقة جماهيرية).
2. الهدف: السخرية من التكتيك، الإدارة، ضياع الفرص، أو برود اللاعبين.
3. التنسيق: ابدأ بـ "🤖 تحليل البوت:" أو "🤖 تعليق النظام:" واكتب الذبة في سطر واحد فقط.
4. الابتكار: لا تكرر نفس الذبة، ونوع في الأهداف (مدرب، لاعب، إدارة).
5. الممنوعات: ممنوع تماماً السخرية من "الإصابات"، وممنوع تكرار الخبر أو المجاملة.
"""

# ── State management ──────────────────────────────────────────────────────────


def load_state() -> dict:
    state = {}
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass

    # Migrate old state format: actions_log → tweets_today
    if "actions_log" in state and "tweets_today" not in state:
        cutoff = time.time() - 86400
        state["tweets_today"] = [
            t for t in state["actions_log"] if t > cutoff
        ]
        migrated = len(state["tweets_today"])
        if migrated:
            log.warning(
                "Migrated %d tweet(s) from old 'actions_log' → 'tweets_today'",
                migrated,
            )

    state.setdefault("posted_event_ids", [])
    state.setdefault("tweets_today", [])
    return state


def save_state(state: dict) -> None:
    cutoff = time.time() - 86400
    state["tweets_today"]     = [t for t in state.get("tweets_today", []) if t > cutoff]
    state["posted_event_ids"] = state.get("posted_event_ids", [])[-2000:]
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


def tweets_today(state: dict) -> int:
    cutoff = time.time() - 86400
    return sum(1 for t in state.get("tweets_today", []) if t > cutoff)


# ── Twitter client (v2 only) ──────────────────────────────────────────────────


def make_twitter_v2() -> "tweepy.Client":
    return tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_SECRET,
        wait_on_rate_limit=False,
    )


# ── Gemini client ─────────────────────────────────────────────────────────────


def make_gemini_client() -> "genai.Client":
    client = genai.Client(api_key=GEMINI_API_KEY)
    log.info("[Gemini] client ready – model: %s", GEMINI_MODEL)
    return client


def check_football_api_quota() -> None:
    """Log API-Football quota status at startup to catch exhaustion early."""
    try:
        r = requests.get(
            f"{FOOTBALL_API_BASE}/status",
            headers={"x-apisports-key": FOOTBALL_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        body = r.json().get("response", {})
        sub  = body.get("subscription", {})
        req  = body.get("requests", {})
        plan    = sub.get("plan", "?")
        current = req.get("current", "?")
        limit   = req.get("limit_day", "?")
        log.info(
            "[API-Football] quota: %s/%s requests used today | plan=%s",
            current, limit, plan,
        )
        if isinstance(current, int) and isinstance(limit, int) and current >= limit:
            log.error(
                "[API-Football] ❌ DAILY QUOTA EXHAUSTED (%d/%d) – "
                "bot will get 0 fixtures until midnight UTC!",
                current, limit,
            )
    except Exception as e:
        log.warning("[API-Football] Could not check quota: %s", e)


# ── Gemini punchline generation ───────────────────────────────────────────────


def generate_punchline(client: "genai.Client", news_text: str) -> str:
    """Generate a sarcastic punchline via Gemini. Returns '' on any error."""
    prompt = f"الخبر:\n{news_text}\n\nاكتب القفلة الساخرة الآن (سطر واحد فقط):"
    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                system_instruction=GEMINI_CONSTITUTION,
                max_output_tokens=100,
                temperature=0.9,
            ),
        )
        text = (resp.text or "").strip()
        if text and not text.startswith("🤖"):
            text = "🤖 تعليق النظام: " + text
        return text[:240]
    except Exception as e:
        log.error("[Gemini] punchline error: %s", e)
        return ""


# ── Tweet composition: 80 % news + 20 % punchline ────────────────────────────


def build_tweet_text(news: str, punchline: str) -> str:
    """Return final tweet: news + blank line + punchline (≤ 280 chars)."""
    if not punchline:
        return news[:280]
    combined = f"{news}\n\n{punchline}"
    if len(combined) <= 280:
        return combined
    overhead = len("\n\n") + len(punchline)
    trimmed_news = news[: 280 - overhead - 1].rstrip() + "…"
    return f"{trimmed_news}\n\n{punchline}"


# ── Post one tweet ────────────────────────────────────────────────────────────


def post_tweet(v2: "tweepy.Client", state: dict, text: str) -> bool:
    """Post *text* via v2. Respects daily cap and DRY_RUN flag."""
    count = tweets_today(state)
    if count >= MAX_TWEETS_PER_DAY:
        log.warning("Daily cap reached (%d/%d) – skipping", count, MAX_TWEETS_PER_DAY)
        return False

    if DRY_RUN:
        log.info("[DRY_RUN] Would tweet (%d chars): %r", len(text), text[:80])
        state.setdefault("tweets_today", []).append(time.time())
        save_state(state)
        return True

    try:
        v2.create_tweet(text=text, user_auth=True)
        state.setdefault("tweets_today", []).append(time.time())
        save_state(state)
        remaining = MAX_TWEETS_PER_DAY - tweets_today(state)
        log.info("posted=tweet | remaining_today=%d | %r", remaining, text[:60])
        return True
    except Exception as e:
        log.error("create_tweet failed: %s", e)
        return False


# ── API-Football helpers ──────────────────────────────────────────────────────


def _football_get(endpoint: str, params: dict) -> list:
    """Call API-Football endpoint, return `response` list or []."""
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    for attempt in range(3):
        try:
            r = requests.get(
                f"{FOOTBALL_API_BASE}/{endpoint}",
                headers=headers,
                params=params,
                timeout=15,
            )
            if r.status_code == 429:
                wait = 60 * (2 ** attempt)
                log.warning("[API-Football] 429 on %s – waiting %ds", endpoint, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            body = r.json()
            api_errors = body.get("errors")
            if api_errors:
                log.error(
                    "[API-Football] /%s returned errors: %s", endpoint, api_errors
                )
                return []
            results = body.get("response", [])
            if not results:
                log.warning(
                    "[API-Football] /%s params=%s → 0 results (quota exhausted or no data)",
                    endpoint, params,
                )
            return results
        except requests.HTTPError as e:
            if r.status_code == 403:
                log.warning(
                    "[API-Football] 403 Forbidden on /%s – "
                    "تحقق من صلاحية FOOTBALL_API_KEY في api-sports.io",
                    endpoint,
                )
            else:
                log.error("[API-Football] HTTP error %s: %s", endpoint, e)
            return []
        except Exception as e:
            log.error("[API-Football] %s %s: %s", endpoint, params, e)
            return []
    log.error("[API-Football] %s gave 429 after 3 retries – skipping", endpoint)
    return []


# ── Fetch helpers ─────────────────────────────────────────────────────────────


def fetch_today_fixtures(league_id: int) -> list[dict]:
    """Fetch ALL of today's fixtures for one league (any status)."""
    today = datetime.utcnow().strftime("%Y-%m-%d")  # API-Football dates are UTC
    fixtures = _football_get("fixtures", {
        "league": league_id,
        "season": CURRENT_SEASON,
        "date":   today,
    })
    log.info(
        "[league=%d] today=%s → %d fixture(s) total",
        league_id, today, len(fixtures),
    )
    return fixtures


def fetch_live_fixtures(league_ids: list[int]) -> list[dict]:
    """Return in-progress fixtures across all configured leagues.

    Fetches today's fixtures per league then filters by status in Python.
    Does NOT use ?live=all (requires higher API tiers) – works on free plans.
    """
    live: list[dict] = []
    for league_id in league_ids:
        for fix in fetch_today_fixtures(league_id):
            status = fix.get("fixture", {}).get("status", {}).get("short", "")
            if status in _LIVE_STATUSES:
                live.append(fix)
                log.info(
                    "[league=%d] fixture %s is LIVE (status=%s)",
                    league_id,
                    fix.get("fixture", {}).get("id"),
                    status,
                )
    return live


def fetch_fixture_events(fixture_id: int) -> list[dict]:
    """Return all events for a specific fixture."""
    return _football_get("fixtures/events", {"fixture": fixture_id})


def fetch_recent_fixtures(league_ids: list[int]) -> list[dict]:
    """Return finished fixtures from the last 2 days for all leagues."""
    now = datetime.utcnow()
    from_date = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    to_date   = now.strftime("%Y-%m-%d")
    results: list[dict] = []
    for league_id in league_ids:
        results.extend(_football_get("fixtures", {
            "league": league_id,
            "season": CURRENT_SEASON,
            "from":   from_date,
            "to":     to_date,
            "status": "FT",
        }))
    return results


# ── Event key for deduplication ───────────────────────────────────────────────


def _event_key(fixture_id: int, event: dict) -> str:
    elapsed    = event.get("time", {}).get("elapsed", 0)
    extra      = event.get("time", {}).get("extra") or 0
    player_id  = event.get("player", {}).get("id", 0)
    event_type = event.get("type", "")
    detail     = event.get("detail", "")
    return f"live_{fixture_id}_{elapsed}_{extra}_{event_type}_{detail}_{player_id}"


# ── Event formatters ──────────────────────────────────────────────────────────


def _minute_str(event: dict) -> str:
    elapsed = event.get("time", {}).get("elapsed", "?")
    extra   = event.get("time", {}).get("extra")
    return f"{elapsed}+{extra}'" if extra else f"{elapsed}'"


def format_live_event(event: dict, fixture: dict) -> str | None:
    """Format a live event into Arabic news text.

    Returns None for events we skip (substitutions, VAR reviews, etc.).
    Goals and cards (yellow/red) are always returned.
    """
    event_type = (event.get("type") or "").lower()
    detail     = event.get("detail") or ""
    player     = event.get("player", {}).get("name") or "لاعب"
    team_name  = event.get("team",   {}).get("name") or "الفريق"
    minute     = _minute_str(event)

    home = fixture.get("teams", {}).get("home", {}).get("name", "؟")
    away = fixture.get("teams", {}).get("away", {}).get("name", "؟")

    # Live fixtures expose current score under "goals"
    goals = fixture.get("goals", {})
    sh = goals.get("home") if goals.get("home") is not None else 0
    sa = goals.get("away") if goals.get("away") is not None else 0
    score_line = f"{home} {sh} – {sa} {away}"

    if event_type == "goal":
        if detail == "Own Goal":
            return f"⚽ هدف في المرمى! {player} ({team_name})\n{score_line}\n⏱ {minute}"
        elif detail == "Penalty":
            return f"⚽ ركلة جزاء | {player} ({team_name})\n{score_line}\n⏱ {minute}"
        elif detail == "Missed Penalty":
            return f"❌ ركلة جزاء ضائعة! {player} ({team_name})\n{score_line}\n⏱ {minute}"
        else:
            return f"⚽ هدف! {player} ({team_name})\n{score_line}\n⏱ {minute}"

    elif event_type == "card":
        if "Red" in detail or "Second" in detail:
            return f"🟥 كرت أحمر! {player} ({team_name})\n{score_line}\n⏱ {minute}"
        elif "Yellow" in detail:
            return f"🟨 كرت أصفر | {player} ({team_name})\n{score_line}\n⏱ {minute}"

    # Substitutions, VAR, and other events – skip silently
    return None


def format_fixture_news(fix: dict) -> str:
    """Format a finished match result."""
    league  = fix.get("league", {})
    teams   = fix.get("teams", {})
    goals   = fix.get("goals", {})
    fixture = fix.get("fixture", {})
    home    = teams.get("home", {}).get("name", "؟")
    away    = teams.get("away", {}).get("name", "؟")
    hg      = goals.get("home") or 0
    ag      = goals.get("away") or 0
    date_s  = (fixture.get("date") or "")[:10]
    return (
        f"⚽ نتيجة | {league.get('name', 'الدوري')}\n"
        f"{home} {hg} – {ag} {away}\n"
        f"📅 {date_s}"
    )


# ── Main event-processing loop ────────────────────────────────────────────────


def process_events(
    v2:     "tweepy.Client",
    gemini: "genai.Client",
    state:  dict,
) -> tuple[int, bool]:
    """Fetch and post new live events or FT results.

    Returns (tweets_posted, has_live_matches).

    Priority:
      1. Live matches exist → poll events every 2 min, tweet goals + cards.
      2. No live matches    → check FT results, tweet summaries (silent mode).
    """
    posted_ids = set(state.get("posted_event_ids", []))
    posted = 0

    # ── Step 1: Live fixtures ─────────────────────────────────────────────────
    log.info("Fetching live fixtures for leagues=%s …", LEAGUE_IDS)
    live_fixtures = fetch_live_fixtures(LEAGUE_IDS)

    if live_fixtures:
        log.info("%d live fixture(s) in progress", len(live_fixtures))

        for fixture in live_fixtures:
            fixture_id = fixture.get("fixture", {}).get("id")
            if not fixture_id:
                continue

            events = fetch_fixture_events(fixture_id)
            new_events = [e for e in events if _event_key(fixture_id, e) not in posted_ids]

            for event in new_events:
                key  = _event_key(fixture_id, event)
                news = format_live_event(event, fixture)

                if not news:
                    # Mark skipped events (substitutions, etc.) as seen
                    posted_ids.add(key)
                    state.setdefault("posted_event_ids", []).append(key)
                    continue

                if tweets_today(state) >= MAX_TWEETS_PER_DAY:
                    log.warning("Daily cap reached – stopping early")
                    save_state(state)
                    return posted, True

                punchline  = generate_punchline(gemini, news)
                tweet_text = build_tweet_text(news, punchline)

                if post_tweet(v2, state, tweet_text):
                    posted += 1
                    posted_ids.add(key)
                    state.setdefault("posted_event_ids", []).append(key)
                    save_state(state)
                    time.sleep(random.uniform(HUMANIZE_MIN_S, HUMANIZE_MAX_S))

        return posted, True

    # ── Step 2: No live matches – check FT results (silent mode) ──────────────
    log.info("No live fixtures – checking FT results …")
    raw_fixtures = fetch_recent_fixtures(LEAGUE_IDS)

    new_fixtures = [
        (f"fixture_{fix.get('fixture', {}).get('id', '')}", fix)
        for fix in raw_fixtures
        if f"fixture_{fix.get('fixture', {}).get('id', '')}" not in posted_ids
    ]

    if not new_fixtures:
        log.info("Silent – no new FT fixtures found, Twitter not contacted")
        return 0, False

    log.info("%d new FT fixture(s) queued for posting", len(new_fixtures))
    for key, fix in new_fixtures:
        if tweets_today(state) >= MAX_TWEETS_PER_DAY:
            log.warning("Daily cap reached – stopping early")
            break

        news       = format_fixture_news(fix)
        punchline  = generate_punchline(gemini, news)
        tweet_text = build_tweet_text(news, punchline)

        if post_tweet(v2, state, tweet_text):
            posted_ids.add(key)
            state.setdefault("posted_event_ids", []).append(key)
            save_state(state)
            posted += 1
            time.sleep(random.uniform(HUMANIZE_MIN_S, HUMANIZE_MAX_S))

    return posted, False


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    log.info("=" * 60)
    log.info(
        "BugKSA bot starting | DRY_RUN=%s | model=%s | max/day=%d",
        DRY_RUN, GEMINI_MODEL, MAX_TWEETS_PER_DAY,
    )
    log.info("=" * 60)

    v2     = make_twitter_v2()
    gemini = make_gemini_client()
    check_football_api_quota()
    state  = load_state()

    if not state.get("posted_event_ids"):
        log.warning(
            "State is empty – this may be a fresh start or lost state file. "
            "Old fixtures from the last 2 days will be treated as new."
        )

    cycle = 0
    while True:
        cycle += 1
        log.info(
            "── Cycle %d ── tweets_today=%d/%d",
            cycle, tweets_today(state), MAX_TWEETS_PER_DAY,
        )
        try:
            n, is_live = process_events(v2, gemini, state)
            log.info(
                "Cycle %d complete: posted %d tweet(s) | live=%s",
                cycle, n, is_live,
            )
        except Exception as e:
            log.error("Cycle %d unhandled error: %s", cycle, e)
            is_live = False

        # Smart interval: 2 min during live matches, 15 min otherwise
        if is_live:
            sleep_s = POLL_INTERVAL_LIVE_S + random.randint(-15, 15)
        else:
            sleep_s = POLL_INTERVAL_IDLE_S + random.randint(-60, 60)

        log.info(
            "Sleeping %ds (%.1f min) … [mode=%s]",
            sleep_s, sleep_s / 60, "LIVE" if is_live else "idle",
        )
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
