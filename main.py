"""
BugKSA – Saudi Football Banter Bot (API-Football Edition)
==========================================================
Fetches live events from API-Football (fixtures & live scores),
posts them in 80 % serious + 20 % sarcastic punchline format.

Architecture
------------
  Layer 1 – Daily cap         (≤ MAX_TWEETS_PER_DAY)
  Layer 2 – Gemini punchline  (GEMINI_CONSTITUTION enforces identity)

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
import tempfile
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

MAX_TWEETS_PER_DAY = 50        # absolute daily ceiling
POLL_INTERVAL_S    = 15 * 60   # 15 minutes between cycles
HUMANIZE_MIN_S     = 30        # minimum sleep between posts within a cycle
HUMANIZE_MAX_S     = 90        # maximum sleep between posts within a cycle

# API-Football league / season (override via env if free-tier restricts league 307)
SAUDI_PRO_LEAGUE_ID = int(os.getenv("FOOTBALL_LEAGUE_ID", "307"))
CURRENT_SEASON      = int(os.getenv("FOOTBALL_SEASON",    "2024"))

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
    if STATE_FILE.exists():
        try:
            with STATE_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "posted_event_ids": [],   # deduplication keys
        "tweets_today":     [],   # Unix timestamps of posts in last 24 h
    }


def save_state(state: dict) -> None:
    cutoff = time.time() - 86400
    state["tweets_today"]    = [t for t in state.get("tweets_today", []) if t > cutoff]
    state["posted_event_ids"] = state.get("posted_event_ids", [])[-1000:]
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


def tweets_today(state: dict) -> int:
    cutoff = time.time() - 86400
    return sum(1 for t in state.get("tweets_today", []) if t > cutoff)


# ── Twitter clients ───────────────────────────────────────────────────────────


def make_twitter_clients() -> tuple["tweepy.Client", "tweepy.API"]:
    """Return (v2 Client for posting, v1.1 API for media upload)."""
    v2 = tweepy.Client(
        consumer_key=X_API_KEY,
        consumer_secret=X_API_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_SECRET,
        wait_on_rate_limit=True,
    )
    auth = tweepy.OAuth1UserHandler(
        X_API_KEY, X_API_SECRET,
        X_ACCESS_TOKEN, X_ACCESS_SECRET,
    )
    v1 = tweepy.API(auth, wait_on_rate_limit=True)
    return v2, v1


# ── Gemini client ─────────────────────────────────────────────────────────────


def make_gemini_client() -> "genai.Client":
    client = genai.Client(api_key=GEMINI_API_KEY)
    log.info("[Gemini] client ready – model: %s", GEMINI_MODEL)
    return client


# ── Gemini punchline generation ───────────────────────────────────────────────


def generate_punchline(client: "genai.Client", news_text: str) -> str:
    """Generate a sarcastic punchline via Gemini.

    Returns the punchline string, or '' on any error.
    The caller must NOT call this for injury news (safety filter).
    """
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


# ── Image upload ──────────────────────────────────────────────────────────────


def upload_player_image(v1: "tweepy.API", image_url: str) -> int | None:
    """Download player image from *image_url* and upload to Twitter v1.1.

    Returns media_id (int) on success, None on any failure.
    The temp file is always deleted after upload.
    """
    if not image_url:
        return None
    try:
        resp = requests.get(image_url, timeout=10)
        resp.raise_for_status()
        # Determine extension from Content-Type
        ct = resp.headers.get("Content-Type", "")
        ext = ".png" if "png" in ct else ".gif" if "gif" in ct else ".jpg"
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(resp.content)
            tmp_path = tmp.name
        try:
            media = v1.media_upload(tmp_path)
            log.info("Image uploaded → media_id=%s", media.media_id)
            return media.media_id
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as e:
        log.warning("Image upload failed (%s): %s", image_url[:60], e)
        return None


# ── Tweet composition: 80 % news + 20 % punchline ────────────────────────────


def build_tweet_text(news: str, punchline: str) -> str:
    """Return final tweet: serious news + blank line + punchline (≤ 280 chars)."""
    if not punchline:
        return news[:280]
    combined = f"{news}\n\n{punchline}"
    if len(combined) <= 280:
        return combined
    # Trim news to fit, preserving the full punchline
    overhead = len("\n\n") + len(punchline)
    trimmed_news = news[: 280 - overhead - 1].rstrip() + "…"
    return f"{trimmed_news}\n\n{punchline}"


# ── Post one tweet ────────────────────────────────────────────────────────────


def post_tweet(
    v2: "tweepy.Client",
    state: dict,
    text: str,
    media_ids: list[int] | None = None,
) -> bool:
    """Post *text* (with optional *media_ids*) using the v2 Client.

    Respects the daily cap and DRY_RUN flag.
    Returns True on success (or dry-run), False otherwise.
    """
    count = tweets_today(state)
    if count >= MAX_TWEETS_PER_DAY:
        log.warning("Daily cap reached (%d/%d) – skipping", count, MAX_TWEETS_PER_DAY)
        return False

    if DRY_RUN:
        log.info("[DRY_RUN] Would tweet (%d chars) | media=%s | %r",
                 len(text), media_ids, text[:80])
        state.setdefault("tweets_today", []).append(time.time())
        save_state(state)
        return True

    try:
        kwargs: dict = {"text": text, "user_auth": True}
        if media_ids:
            kwargs["media_ids"] = media_ids
        v2.create_tweet(**kwargs)
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
    """Call API-Football endpoint and return the `response` list, or [].

    Retries up to 3 times on 429 (rate limit) with exponential back-off.
    """
    headers = {
        "x-apisports-key": FOOTBALL_API_KEY,
    }
    for attempt in range(3):
        try:
            r = requests.get(
                f"{FOOTBALL_API_BASE}/{endpoint}",
                headers=headers,
                params=params,
                timeout=15,
            )
            if r.status_code == 429:
                wait = 60 * (2 ** attempt)   # 60 s → 120 s → 240 s
                log.warning("[API-Football] 429 rate-limit on %s – waiting %ds", endpoint, wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json().get("response", [])
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


# ── Fetch: match results ──────────────────────────────────────────────────────


def fetch_recent_fixtures(league_id: int, season: int) -> list[dict]:
    """Return finished fixtures from the last 2 days."""
    from_date = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    to_date   = datetime.now().strftime("%Y-%m-%d")
    return _football_get("fixtures", {
        "league": league_id,
        "season": season,
        "from":   from_date,
        "to":     to_date,
        "status": "FT",
    })


def format_fixture_news(fix: dict) -> tuple[str, str | None]:
    """Return (news_text, image_url=None) for a finished match."""
    league  = fix.get("league", {})
    teams   = fix.get("teams", {})
    goals   = fix.get("goals", {})
    fixture = fix.get("fixture", {})
    home    = teams.get("home", {}).get("name", "؟")
    away    = teams.get("away", {}).get("name", "؟")
    hg      = goals.get("home") or 0
    ag      = goals.get("away") or 0
    date_s  = (fixture.get("date") or "")[:10]
    news = (
        f"⚽ نتيجة | {league.get('name', 'الدوري')}\n"
        f"{home} {hg} – {ag} {away}\n"
        f"📅 {date_s}"
    )
    return news, None          # no player image for match results


# ── Main event-processing loop ────────────────────────────────────────────────


def process_events(
    v2:     "tweepy.Client",
    v1:     "tweepy.API",
    gemini: "genai.GenerativeModel",
    state:  dict,
) -> int:
    """Fetch fixtures and post new results.  Returns count of tweets posted."""

    posted     = 0
    posted_ids = set(state.get("posted_event_ids", []))

    def _record_posted(key: str) -> None:
        posted_ids.add(key)
        state.setdefault("posted_event_ids", []).append(key)
        save_state(state)

    def _maybe_upload_image(img_url: str | None) -> list[int]:
        if not img_url:
            return []
        mid = upload_player_image(v1, img_url)
        return [mid] if mid else []

    # ── Match results ─────────────────────────────────────────────────────────
    log.info("Fetching fixtures …")
    for fix in fetch_recent_fixtures(SAUDI_PRO_LEAGUE_ID, CURRENT_SEASON):
        if tweets_today(state) >= MAX_TWEETS_PER_DAY:
            break
        key = f"fixture_{fix.get('fixture', {}).get('id', '')}"
        if not key or key in posted_ids:
            continue

        news, img_url  = format_fixture_news(fix)
        punchline      = generate_punchline(gemini, news)
        tweet_text     = build_tweet_text(news, punchline)
        media_ids      = _maybe_upload_image(img_url)

        if post_tweet(v2, state, tweet_text, media_ids or None):
            _record_posted(key)
            posted += 1
            time.sleep(random.uniform(HUMANIZE_MIN_S, HUMANIZE_MAX_S))

    return posted


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    log.info("=" * 60)
    log.info(
        "BugKSA bot starting | DRY_RUN=%s | model=%s | max/day=%d",
        DRY_RUN, GEMINI_MODEL, MAX_TWEETS_PER_DAY,
    )
    log.info("=" * 60)

    v2, v1   = make_twitter_clients()
    gemini   = make_gemini_client()
    state    = load_state()

    cycle = 0
    while True:
        cycle += 1
        log.info("── Cycle %d ── tweets_today=%d/%d", cycle, tweets_today(state), MAX_TWEETS_PER_DAY)
        try:
            n = process_events(v2, v1, gemini, state)
            log.info("Cycle %d complete: posted %d tweet(s)", cycle, n)
        except Exception as e:
            log.error("Cycle %d unhandled error: %s", cycle, e)

        # Humanized interval: 15 min ± 1 min
        sleep_s = POLL_INTERVAL_S + random.randint(-60, 60)
        log.info("Sleeping %ds (%.1f min) …", sleep_s, sleep_s / 60)
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
