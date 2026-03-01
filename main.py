"""
BugKSA – Saudi Football Banter Bot
====================================
Data sources (priority order):
  1. 365Scores JSON API  – primary, free, no key required, Saudi-focused
  2. API-Football        – fallback, requires key (season ≤ 2024 on free plan)

Architecture
------------
  Layer 1 – Daily cap         (≤ MAX_TWEETS_PER_DAY)
  Layer 2 – Gemini punchline  (دستور القصف الساخر – سخرية كروية سعودية)
  Layer 3 – Live mode         (goals + cards + FT every 5 min during matches)
  Layer 4 – Silent mode       (FT summaries only – no wasted Twitter quota)

Environment variables
---------------------
  X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET
  GEMINI_API_KEY
  GEMINI_MODEL                  (default: gemini-1.5-flash)
  FOOTBALL_API_KEY              (optional – API-Football fallback only)
  DRY_RUN                       (1/true/yes → no real posts)
  STATE_FILE_PATH               (default: /app/data/state.json)
  FOOTBALL_LEAGUE_IDS           (comma-separated IDs, default: 307)
  FOOTBALL_SEASON               (default: 2024)
  SCORES365_COMPETITION_ID      (default: 653 – Saudi Pro League on 365Scores)
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
GEMINI_API_KEY = _env("GEMINI_API_KEY")
GEMINI_MODEL   = os.getenv("GEMINI_MODEL", "gemini-1.5-flash").strip()

# API-Football (optional fallback)
FOOTBALL_API_KEY  = _env("FOOTBALL_API_KEY", required=False)
FOOTBALL_API_BASE = "https://v3.football.api-sports.io"

# Bot behaviour
DRY_RUN           = os.getenv("DRY_RUN", "false").lower() in ("1", "true", "yes")
STATE_FILE        = Path(os.getenv("STATE_FILE_PATH", "/app/data/state.json").strip())
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

MAX_TWEETS_PER_DAY   = 50
POLL_INTERVAL_LIVE_S = 5 * 60   # 5 minutes when live matches are happening
POLL_INTERVAL_IDLE_S = 15 * 60  # 15 minutes when no live matches
HUMANIZE_MIN_S       = 15
HUMANIZE_MAX_S       = 45

# API-Football leagues / season (fallback)
LEAGUE_IDS     = [
    int(x.strip())
    for x in os.getenv("FOOTBALL_LEAGUE_IDS", "307").split(",")
    if x.strip()
]
CURRENT_SEASON = int(os.getenv("FOOTBALL_SEASON", "2024"))

# ── 365Scores config (PRIMARY) ────────────────────────────────────────────────

SCORES365_BASE        = "https://webws.365scores.com"
SCORES365_COMPETITION = int(os.getenv("SCORES365_COMPETITION_ID", "653"))

# Base query params sent with every 365Scores request
_365_BASE_PARAMS = {
    "appTypeId":     5,
    "langId":        1,        # 1=English names; use 32 for Arabic
    "timezoneName":  "Asia/Riyadh",
    "userCountryId": 215,      # Saudi Arabia
}

# Headers that mimic a normal browser visit
_365_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
    "Referer":         "https://www.365scores.com/",
    "Origin":          "https://www.365scores.com",
}

# 365Scores event type IDs
_365_GOAL          = 3
_365_YELLOW_CARD   = 4
_365_RED_CARD      = 5
_365_SUBSTITUTION  = 6
_365_OWN_GOAL      = 7
_365_PENALTY_OK    = 8
_365_PENALTY_MISS  = 9
_365_MATCH_END     = 10
_365_SECOND_YELLOW = 11   # second yellow → red
_365_VAR           = 14

# 365Scores game statusGroup values
_365_STATUS_NOT_STARTED = 1
_365_STATUS_LIVE        = 2
_365_STATUS_FINISHED    = 3

# API-Football: statuses considered "in progress"
_LIVE_STATUSES = {"1H", "HT", "2H", "ET", "BT", "P", "LIVE"}

# ── Gemini Constitution ───────────────────────────────────────────────────────

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
    state: dict = {}
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            pass

    # Migrate old state format: actions_log → tweets_today
    if "actions_log" in state and "tweets_today" not in state:
        cutoff = time.time() - 86400
        state["tweets_today"] = [t for t in state["actions_log"] if t > cutoff]
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


# ── Twitter client ────────────────────────────────────────────────────────────


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


# ── Tweet composition ─────────────────────────────────────────────────────────


def build_tweet_text(news: str, punchline: str) -> str:
    """Return final tweet: news + blank line + punchline (≤ 280 chars)."""
    if not punchline:
        return news[:280]
    combined = f"{news}\n\n{punchline}"
    if len(combined) <= 280:
        return combined
    overhead     = len("\n\n") + len(punchline)
    trimmed_news = news[: 280 - overhead - 1].rstrip() + "…"
    return f"{trimmed_news}\n\n{punchline}"


def post_tweet(v2: "tweepy.Client", state: dict, text: str) -> bool:
    """Post text via v2. Respects daily cap and DRY_RUN."""
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


# ══════════════════════════════════════════════════════════════════════════════
#  PRIMARY DATA SOURCE – 365Scores
# ══════════════════════════════════════════════════════════════════════════════


def _365scores_get(path: str, params: dict) -> dict:
    """Call 365Scores JSON API. Returns parsed body or {} on error."""
    merged = {**_365_BASE_PARAMS, **params}
    for attempt in range(3):
        try:
            r = requests.get(
                f"{SCORES365_BASE}{path}",
                headers=_365_HEADERS,
                params=merged,
                timeout=15,
            )
            if r.status_code == 429:
                wait = 30 * (2 ** attempt)
                log.warning("[365Scores] 429 rate-limit – waiting %ds", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.error("[365Scores] %s params=%s → %s", path, params, e)
            return {}
    return {}


def fetch_365_live() -> list[dict]:
    """Return currently live Saudi Pro League games from 365Scores."""
    body  = _365scores_get("/web/games/current/", {"competitions": SCORES365_COMPETITION})
    games = body.get("games") or []
    live  = [g for g in games if g.get("statusGroup") == _365_STATUS_LIVE]
    log.info("[365Scores] /current → %d game(s) total, %d live", len(games), len(live))
    return live


def fetch_365_today() -> list[dict]:
    """Return all of today's games (any status) for the configured competition."""
    today = datetime.utcnow().strftime("%d/%m/%Y")   # 365Scores uses DD/MM/YYYY
    body  = _365scores_get("/web/games/", {
        "competitions": SCORES365_COMPETITION,
        "startDate":    today,
        "endDate":      today,
    })
    games = body.get("games") or []
    log.info("[365Scores] today=%s → %d game(s)", today, len(games))
    return games


def fetch_365_events(game_id: int) -> list[dict]:
    """Return all in-game events for a specific 365Scores game."""
    body = _365scores_get("/web/game/", {
        "gameId":    game_id,
        "isPreGame": "false",
    })
    game   = body.get("game") or {}
    events = game.get("events") or []
    log.info("[365Scores] game=%d → %d event(s)", game_id, len(events))
    return events


def _event_key_365(game_id: int, event: dict) -> str:
    minute    = event.get("gameTime", 0)
    added     = event.get("addedTime") or 0
    etype     = event.get("type", 0)
    player_id = (event.get("player") or {}).get("id", 0)
    return f"365_{game_id}_{minute}_{added}_{etype}_{player_id}"


def _minute_str_365(event: dict) -> str:
    minute = event.get("gameTime", "?")
    added  = event.get("addedTime") or 0
    return f"{minute}+{added}'" if added else f"{minute}'"


def format_365_event(event: dict, game: dict) -> str | None:
    """Format a 365Scores event to Arabic news text.

    Returns None for events we intentionally skip (substitutions, VAR, etc.).
    """
    etype  = event.get("type")
    minute = _minute_str_365(event)
    player = (event.get("player") or {}).get("name") or "لاعب"

    home_c = game.get("homeCompetitor") or {}
    away_c = game.get("awayCompetitor") or {}
    home   = home_c.get("name", "؟")
    away   = away_c.get("name", "؟")
    hg     = home_c.get("score") or 0
    ag     = away_c.get("score") or 0
    score_line = f"{home} {hg} – {ag} {away}"

    # competitorNum: 1 = home team, 2 = away team
    comp      = event.get("competitorNum")
    team_name = home if comp == 1 else away if comp == 2 else "الفريق"

    if etype == _365_GOAL:
        return f"⚽ هدف! {player} ({team_name})\n{score_line}\n⏱ {minute}"
    if etype == _365_OWN_GOAL:
        return f"⚽ هدف في المرمى! {player} ({team_name})\n{score_line}\n⏱ {minute}"
    if etype == _365_PENALTY_OK:
        return f"⚽ ركلة جزاء | {player} ({team_name})\n{score_line}\n⏱ {minute}"
    if etype == _365_PENALTY_MISS:
        return f"❌ ركلة جزاء ضائعة! {player} ({team_name})\n{score_line}\n⏱ {minute}"
    if etype in (_365_RED_CARD, _365_SECOND_YELLOW):
        return f"🟥 كرت أحمر! {player} ({team_name})\n{score_line}\n⏱ {minute}"
    if etype == _365_YELLOW_CARD:
        return f"🟨 كرت أصفر | {player} ({team_name})\n{score_line}\n⏱ {minute}"
    if etype == _365_MATCH_END:
        return f"🏁 انتهت المباراة!\n{score_line}"

    return None   # substitutions, VAR, kickoff → skip silently


def format_365_result(game: dict) -> str:
    """Format a finished 365Scores game as a compact result tweet."""
    home  = (game.get("homeCompetitor") or {}).get("name", "؟")
    away  = (game.get("awayCompetitor") or {}).get("name", "؟")
    hg    = (game.get("homeCompetitor") or {}).get("score") or 0
    ag    = (game.get("awayCompetitor") or {}).get("score") or 0
    start = (game.get("startTime") or "")[:10]
    comp  = game.get("competitionName") or "الدوري السعودي"
    return (
        f"⚽ نتيجة | {comp}\n"
        f"{home} {hg} – {ag} {away}\n"
        f"📅 {start}"
    )


# ══════════════════════════════════════════════════════════════════════════════
#  FALLBACK DATA SOURCE – API-Football
# ══════════════════════════════════════════════════════════════════════════════


def _football_get(endpoint: str, params: dict) -> list:
    """Call API-Football endpoint. Returns `response` list or []."""
    if not FOOTBALL_API_KEY:
        return []
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
            body       = r.json()
            api_errors = body.get("errors")
            if api_errors:
                log.error("[API-Football] /%s returned errors: %s", endpoint, api_errors)
                return []
            results = body.get("response", [])
            if not results:
                log.warning(
                    "[API-Football] /%s params=%s → 0 results (quota or no data)",
                    endpoint, params,
                )
            return results
        except requests.HTTPError as e:
            code = getattr(r, "status_code", None)
            if code == 403:
                log.warning("[API-Football] 403 Forbidden on /%s – check key", endpoint)
            else:
                log.error("[API-Football] HTTP %s on /%s: %s", code, endpoint, e)
            return []
        except Exception as e:
            log.error("[API-Football] %s %s: %s", endpoint, params, e)
            return []
    log.error("[API-Football] %s gave 429 after 3 retries – skipping", endpoint)
    return []


def check_football_api_quota() -> None:
    """Log API-Football quota at startup so exhaustion is immediately visible."""
    if not FOOTBALL_API_KEY:
        log.info("[API-Football] no key configured – fallback disabled")
        return
    try:
        r = requests.get(
            f"{FOOTBALL_API_BASE}/status",
            headers={"x-apisports-key": FOOTBALL_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        body    = r.json().get("response", {})
        sub     = body.get("subscription", {})
        req     = body.get("requests", {})
        plan    = sub.get("plan", "?")
        current = req.get("current", "?")
        limit   = req.get("limit_day", "?")
        log.info("[API-Football] quota: %s/%s requests | plan=%s", current, limit, plan)
        if isinstance(current, int) and isinstance(limit, int) and current >= limit:
            log.error(
                "[API-Football] ❌ DAILY QUOTA EXHAUSTED (%d/%d) – "
                "fallback disabled until midnight UTC",
                current, limit,
            )
    except Exception as e:
        log.warning("[API-Football] could not check quota: %s", e)


def fetch_today_fixtures(league_id: int) -> list[dict]:
    today    = datetime.utcnow().strftime("%Y-%m-%d")
    fixtures = _football_get("fixtures", {
        "league": league_id,
        "season": CURRENT_SEASON,
        "date":   today,
    })
    log.info("[API-Football][league=%d] today=%s → %d fixture(s)", league_id, today, len(fixtures))
    return fixtures


def fetch_live_fixtures(league_ids: list[int]) -> list[dict]:
    """Return in-progress fixtures across all configured leagues."""
    live: list[dict] = []
    for league_id in league_ids:
        for fix in fetch_today_fixtures(league_id):
            status = fix.get("fixture", {}).get("status", {}).get("short", "")
            if status in _LIVE_STATUSES:
                live.append(fix)
                log.info(
                    "[API-Football][league=%d] fixture %s LIVE (status=%s)",
                    league_id, fix.get("fixture", {}).get("id"), status,
                )
    return live


def fetch_fixture_events(fixture_id: int) -> list[dict]:
    return _football_get("fixtures/events", {"fixture": fixture_id})


def fetch_recent_fixtures(league_ids: list[int]) -> list[dict]:
    now       = datetime.utcnow()
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


def _event_key(fixture_id: int, event: dict) -> str:
    elapsed   = event.get("time", {}).get("elapsed", 0)
    extra     = event.get("time", {}).get("extra") or 0
    player_id = event.get("player", {}).get("id", 0)
    etype     = event.get("type", "")
    detail    = event.get("detail", "")
    return f"live_{fixture_id}_{elapsed}_{extra}_{etype}_{detail}_{player_id}"


def _minute_str(event: dict) -> str:
    elapsed = event.get("time", {}).get("elapsed", "?")
    extra   = event.get("time", {}).get("extra")
    return f"{elapsed}+{extra}'" if extra else f"{elapsed}'"


def format_live_event(event: dict, fixture: dict) -> str | None:
    event_type = (event.get("type") or "").lower()
    detail     = event.get("detail") or ""
    player     = event.get("player", {}).get("name") or "لاعب"
    team_name  = event.get("team",   {}).get("name") or "الفريق"
    minute     = _minute_str(event)

    home  = fixture.get("teams", {}).get("home", {}).get("name", "؟")
    away  = fixture.get("teams", {}).get("away", {}).get("name", "؟")
    goals = fixture.get("goals", {})
    sh    = goals.get("home") if goals.get("home") is not None else 0
    sa    = goals.get("away") if goals.get("away") is not None else 0
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

    return None


def format_fixture_news(fix: dict) -> str:
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


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN EVENT LOOP
# ══════════════════════════════════════════════════════════════════════════════


def _try_post(
    v2: "tweepy.Client",
    gemini: "genai.Client",
    state: dict,
    news: str,
    key: str,
    posted_ids: set,
) -> bool:
    """Generate punchline via Gemini then post tweet. Returns False if cap hit."""
    if tweets_today(state) >= MAX_TWEETS_PER_DAY:
        log.warning("Daily cap reached – stopping early")
        save_state(state)
        return False

    punchline  = generate_punchline(gemini, news)
    tweet_text = build_tweet_text(news, punchline)

    if post_tweet(v2, state, tweet_text):
        posted_ids.add(key)
        state.setdefault("posted_event_ids", []).append(key)
        save_state(state)
        time.sleep(random.uniform(HUMANIZE_MIN_S, HUMANIZE_MAX_S))

    return True   # cap not hit (tweet may or may not have posted)


def process_events(
    v2:     "tweepy.Client",
    gemini: "genai.Client",
    state:  dict,
) -> tuple[int, bool]:
    """Fetch and post new live events or FT results.

    Returns (tweets_posted, has_live_matches).

    Priority:
      1. 365Scores live games       → tweet goals, cards, FT events (primary)
      2. API-Football live fixtures → fallback if 365Scores returns nothing
      3. 365Scores today FT games   → silent mode, FT summaries
      4. API-Football recent FT     → final fallback for FT summaries
    """
    posted_ids = set(state.get("posted_event_ids", []))
    posted     = 0

    # ── 1. 365Scores: live games ──────────────────────────────────────────────
    log.info("Fetching live games from 365Scores (competition=%d) …", SCORES365_COMPETITION)
    live_365 = fetch_365_live()

    if live_365:
        log.info("%d live game(s) via 365Scores", len(live_365))
        for game in live_365:
            game_id = game.get("id")
            if not game_id:
                continue

            events     = fetch_365_events(game_id)
            new_events = [e for e in events if _event_key_365(game_id, e) not in posted_ids]
            log.info(
                "[365Scores] game=%d: %d event(s) total, %d new",
                game_id, len(events), len(new_events),
            )

            for event in new_events:
                key  = _event_key_365(game_id, event)
                news = format_365_event(event, game)

                if not news:
                    # Mark non-tweetable events (subs, VAR) as seen so we skip next cycle
                    posted_ids.add(key)
                    state.setdefault("posted_event_ids", []).append(key)
                    continue

                cap_hit = not _try_post(v2, gemini, state, news, key, posted_ids)
                if cap_hit:
                    return posted, True
                posted += 1

        return posted, True

    # ── 2. API-Football: live games (fallback) ────────────────────────────────
    log.info("365Scores: 0 live games – trying API-Football fallback …")
    live_apif = fetch_live_fixtures(LEAGUE_IDS)

    if live_apif:
        log.info("%d live fixture(s) via API-Football", len(live_apif))
        for fixture in live_apif:
            fixture_id = fixture.get("fixture", {}).get("id")
            if not fixture_id:
                continue

            events     = fetch_fixture_events(fixture_id)
            new_events = [e for e in events if _event_key(fixture_id, e) not in posted_ids]

            for event in new_events:
                key  = _event_key(fixture_id, event)
                news = format_live_event(event, fixture)

                if not news:
                    posted_ids.add(key)
                    state.setdefault("posted_event_ids", []).append(key)
                    continue

                cap_hit = not _try_post(v2, gemini, state, news, key, posted_ids)
                if cap_hit:
                    return posted, True
                posted += 1

        return posted, True

    # ── 3. Silent mode: 365Scores today's finished games ─────────────────────
    log.info("No live matches – checking today's FT results via 365Scores …")
    today_games  = fetch_365_today()
    finished_365 = [
        g for g in today_games
        if g.get("statusGroup") == _365_STATUS_FINISHED
        and f"365_ft_{g.get('id')}" not in posted_ids
    ]

    if finished_365:
        log.info("%d new finished game(s) from 365Scores", len(finished_365))
        for game in finished_365:
            key  = f"365_ft_{game['id']}"
            news = format_365_result(game)

            cap_hit = not _try_post(v2, gemini, state, news, key, posted_ids)
            if cap_hit:
                break
            posted += 1

        return posted, False

    # ── 4. Silent mode: API-Football recent FT (final fallback) ──────────────
    log.info("No 365Scores FT results – trying API-Football FT fallback …")
    raw_fixtures = fetch_recent_fixtures(LEAGUE_IDS)
    new_fixtures = [
        (f"fixture_{fix.get('fixture', {}).get('id', '')}", fix)
        for fix in raw_fixtures
        if f"fixture_{fix.get('fixture', {}).get('id', '')}" not in posted_ids
    ]

    if not new_fixtures:
        log.info("Silent – no new fixtures found, Twitter not contacted")
        return 0, False

    log.info("%d new FT fixture(s) via API-Football", len(new_fixtures))
    for key, fix in new_fixtures:
        news     = format_fixture_news(fix)
        cap_hit  = not _try_post(v2, gemini, state, news, key, posted_ids)
        if cap_hit:
            break
        posted += 1

    return posted, False


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    log.info("=" * 60)
    log.info(
        "BugKSA bot starting | DRY_RUN=%s | model=%s | max/day=%d",
        DRY_RUN, GEMINI_MODEL, MAX_TWEETS_PER_DAY,
    )
    log.info(
        "Sources: 365Scores (competition=%d) primary | "
        "API-Football (leagues=%s season=%d) fallback",
        SCORES365_COMPETITION, LEAGUE_IDS, CURRENT_SEASON,
    )
    log.info("Poll: %ds live / %ds idle", POLL_INTERVAL_LIVE_S, POLL_INTERVAL_IDLE_S)
    log.info("=" * 60)

    v2     = make_twitter_v2()
    gemini = make_gemini_client()
    check_football_api_quota()
    state  = load_state()

    if not state.get("posted_event_ids"):
        log.warning(
            "State empty – fresh start or lost state file. "
            "Recent fixtures will be treated as new."
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
                "Cycle %d complete: posted=%d | live=%s",
                cycle, n, is_live,
            )
        except Exception as e:
            log.error("Cycle %d unhandled error: %s", cycle, e, exc_info=True)
            is_live = False

        sleep_s = (
            POLL_INTERVAL_LIVE_S + random.randint(-15, 15)
            if is_live
            else POLL_INTERVAL_IDLE_S + random.randint(-60, 60)
        )
        log.info(
            "Sleeping %ds (%.1f min) [mode=%s] …",
            sleep_s, sleep_s / 60, "LIVE" if is_live else "idle",
        )
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
