"""
diagnose.py  –  BugKSA bot diagnostic runner
Usage: python diagnose.py
Checks every layer without posting anything to Twitter.
"""
import os, json, time, sys, requests
from pathlib import Path
from datetime import datetime, timedelta

SEP = "─" * 60

# ── helpers ──────────────────────────────────────────────────────────────────
def env(name):
    v = (os.getenv(name) or "").strip()
    return v or None

# ── 1. Env vars ───────────────────────────────────────────────────────────────
print(SEP)
print("1.  ENV VARS")
print(SEP)

FOOTBALL_API_KEY  = env("FOOTBALL_API_KEY")
FOOTBALL_API_BASE = "https://v3.football.api-sports.io"
LEAGUE_IDS   = [int(x) for x in (env("FOOTBALL_LEAGUE_IDS") or "307").split(",") if x.strip()]
SEASON       = int(env("FOOTBALL_SEASON") or "2025")
DRY_RUN      = (env("DRY_RUN") or "false").lower() in ("1","true","yes")
STATE_PATH   = Path(env("STATE_FILE_PATH") or "/app/data/state.json")
MAX_TWEETS   = 50

ok = True
for var in ["FOOTBALL_API_KEY","X_API_KEY","X_API_SECRET","X_ACCESS_TOKEN","X_ACCESS_SECRET","GEMINI_API_KEY"]:
    val = env(var)
    status = "✅" if val else "❌ MISSING"
    masked = (val[:4]+"…"+val[-4:]) if val and len(val) > 8 else val
    print(f"  {status}  {var} = {masked}")
    if not val:
        ok = False

print(f"\n  DRY_RUN      = {DRY_RUN}")
print(f"  LEAGUE_IDS   = {LEAGUE_IDS}")
print(f"  SEASON       = {SEASON}")
print(f"  STATE_FILE   = {STATE_PATH}  (exists={STATE_PATH.exists()})")

# ── 2. State file ─────────────────────────────────────────────────────────────
print()
print(SEP)
print("2.  STATE FILE")
print(SEP)

if STATE_PATH.exists():
    try:
        state = json.loads(STATE_PATH.read_text())
    except Exception as e:
        print(f"  ❌  Cannot parse state: {e}")
        state = {}
else:
    print("  ⚠️  State file does not exist – will start fresh")
    state = {}

cutoff = time.time() - 86400
tweets_today_cnt = sum(1 for t in state.get("tweets_today", []) if t > cutoff)
posted_ids_cnt   = len(state.get("posted_event_ids", []))
old_actions_cnt  = sum(1 for t in state.get("actions_log", []) if t > cutoff)

print(f"  tweets_today (new key) : {tweets_today_cnt}/{MAX_TWEETS}")
print(f"  actions_log  (old key) : {old_actions_cnt}  ← old format, NOT counted by current code")
print(f"  posted_event_ids       : {posted_ids_cnt} dedup keys stored")

# Detect old-format state (actions_log but no tweets_today)
if "actions_log" in state and "tweets_today" not in state:
    print("  ⚠️  STALE STATE: state file uses OLD keys (actions_log / next_action_after)")
    print("      Current code uses 'tweets_today'. Cap counter starts at 0 – OK for tweets.")
    print("      'posted_event_ids' also missing → all events look NEW on restart.")

if tweets_today_cnt >= MAX_TWEETS:
    print("  ❌  DAILY CAP REACHED – bot will NOT tweet until tomorrow")

# ── 3. Timezone check ─────────────────────────────────────────────────────────
print()
print(SEP)
print("3.  TIMEZONE / DATE CHECK")
print(SEP)

now_utc   = datetime.utcnow()
now_local = datetime.now()
today_utc   = now_utc.strftime("%Y-%m-%d")
today_local = now_local.strftime("%Y-%m-%d")

print(f"  datetime.utcnow()  = {now_utc.strftime('%Y-%m-%d %H:%M')} UTC")
print(f"  datetime.now()     = {now_local.strftime('%Y-%m-%d %H:%M')} LOCAL  ← bot uses THIS for ?date=")
print(f"  Saudi time (UTC+3) = {(now_utc + timedelta(hours=3)).strftime('%Y-%m-%d %H:%M')} AST")

if today_utc != today_local:
    print("  ⚠️  LOCAL DATE ≠ UTC DATE — server timezone affects which fixtures are fetched!")
else:
    print("  ✅  Local and UTC dates match")

# ── 4. API-Football ───────────────────────────────────────────────────────────
print()
print(SEP)
print("4.  API-FOOTBALL  –  TODAY'S FIXTURES")
print(SEP)

LIVE_STATUSES = {"1H","HT","2H","ET","BT","P","LIVE"}

if not FOOTBALL_API_KEY:
    print("  ❌  FOOTBALL_API_KEY missing – cannot test API")
else:
    headers = {"x-apisports-key": FOOTBALL_API_KEY}

    # Quota check
    try:
        r = requests.get(f"{FOOTBALL_API_BASE}/status", headers=headers, timeout=10)
        rj = r.json().get("response", {})
        sub = rj.get("subscription", {})
        req = rj.get("requests", {})
        print(f"  Subscription plan : {sub.get('plan','?')}")
        print(f"  API requests today: {req.get('current','?')} / {req.get('limit_day','?')}")
        if req.get("current", 0) >= req.get("limit_day", 999999):
            print("  ❌  DAILY API QUOTA EXHAUSTED – all calls return 0 results!")
        else:
            print("  ✅  API quota OK")
    except Exception as e:
        print(f"  ⚠️  Could not fetch /status: {e}")

    # Fixtures per league
    total_live = 0
    for league_id in LEAGUE_IDS:
        date_str = today_local  # same as bot
        print(f"\n  League {league_id} | date={date_str} | season={SEASON}")
        try:
            r = requests.get(
                f"{FOOTBALL_API_BASE}/fixtures",
                headers=headers,
                params={"league": league_id, "season": SEASON, "date": date_str},
                timeout=15,
            )
            data = r.json()
            fixtures = data.get("response", [])
            errors   = data.get("errors", {})

            if errors:
                print(f"    ❌  API errors: {errors}")
            elif not fixtures:
                print(f"    ⚠️  0 fixtures returned")
                # Try yesterday and tomorrow to debug
                for delta, label in [(-1,"yesterday"), (+1,"tomorrow")]:
                    alt = (datetime.now() + timedelta(days=delta)).strftime("%Y-%m-%d")
                    r2 = requests.get(f"{FOOTBALL_API_BASE}/fixtures", headers=headers,
                                      params={"league": league_id, "season": SEASON, "date": alt}, timeout=15)
                    cnt = len(r2.json().get("response", []))
                    print(f"    ℹ️   {label} ({alt}): {cnt} fixture(s)")
            else:
                print(f"    ✅  {len(fixtures)} fixture(s) found:")
                for f in fixtures:
                    fix   = f.get("fixture", {})
                    teams = f.get("teams", {})
                    st    = fix.get("status", {})
                    home  = teams.get("home", {}).get("name", "?")
                    away  = teams.get("away", {}).get("name", "?")
                    short = st.get("short", "?")
                    long_ = st.get("long",  "?")
                    kick  = fix.get("date","")[:16]
                    is_live = "🔴 LIVE" if short in LIVE_STATUSES else ""
                    print(f"      [{short:4}] {home} vs {away}  kick={kick}  {is_live}")
                    if short in LIVE_STATUSES:
                        total_live += 1
                        fid = fix.get("id")
                        # Check events
                        r3 = requests.get(f"{FOOTBALL_API_BASE}/fixtures/events",
                                          headers=headers, params={"fixture": fid}, timeout=15)
                        events = r3.json().get("response", [])
                        print(f"        └─ fixture_id={fid} | events={len(events)}")
        except Exception as e:
            print(f"    ❌  Request failed: {e}")

    print(f"\n  TOTAL LIVE fixtures : {total_live}")
    if total_live == 0:
        print("  → Bot has nothing to tweet (no live matches found by API)")

# ── 5. Summary ────────────────────────────────────────────────────────────────
print()
print(SEP)
print("5.  DIAGNOSIS SUMMARY")
print(SEP)

issues = []
if tweets_today_cnt >= MAX_TWEETS:
    issues.append("Daily cap reached (tweets_today >= 50)")
if not FOOTBALL_API_KEY:
    issues.append("FOOTBALL_API_KEY missing")
if DRY_RUN:
    issues.append("DRY_RUN=true (tweets logged but NOT posted)")
if today_utc != today_local:
    issues.append(f"Timezone mismatch: server uses {today_local} but UTC is {today_utc}")

if issues:
    print("  PROBLEMS FOUND:")
    for i in issues:
        print(f"   ❌  {i}")
else:
    print("  ✅  No obvious config issues – check API response above for 0-fixture root cause")

# ── 6. 365Scores test ─────────────────────────────────────────────────────────
print()
print(SEP)
print("6.  365SCORES  –  LIVE GAMES  (PRIMARY SOURCE)")
print(SEP)

COMP_ID     = int(env("SCORES365_COMPETITION_ID") or "653")
BASE365     = "https://webws.365scores.com"
BASE_PARAMS = {"appTypeId": 5, "langId": 1, "timezoneName": "Asia/Riyadh", "userCountryId": 215}
HDR365 = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
    "Accept":     "application/json, text/plain, */*",
    "Referer":    "https://www.365scores.com/",
    "Origin":     "https://www.365scores.com",
}

print(f"  competition_id = {COMP_ID}  (env SCORES365_COMPETITION_ID)")
try:
    r = requests.get(
        f"{BASE365}/web/games/current/",
        headers=HDR365,
        params={**BASE_PARAMS, "competitions": COMP_ID},
        timeout=15,
    )
    print(f"  HTTP status: {r.status_code}")
    if r.status_code == 200:
        games = r.json().get("games") or []
        live  = [g for g in games if g.get("statusGroup") == 2]
        done  = [g for g in games if g.get("statusGroup") == 3]
        print(f"  ✅  {len(games)} game(s): {len(live)} live, {len(done)} finished")
        for g in live:
            home = (g.get("homeCompetitor") or {}).get("name", "?")
            away = (g.get("awayCompetitor") or {}).get("name", "?")
            hg   = (g.get("homeCompetitor") or {}).get("score", "?")
            ag   = (g.get("awayCompetitor") or {}).get("score", "?")
            st   = g.get("shortStatusText", "?")
            print(f"    🔴 [{st}] {home} {hg}–{ag} {away}  id={g.get('id')}")
        if not live:
            print("  ℹ️   No live games right now – test again during match hours")
    elif r.status_code == 403:
        print("  ❌  403 Forbidden – 365Scores blocked this IP or User-Agent")
    else:
        print(f"  ⚠️  Unexpected {r.status_code}: {r.text[:200]}")
except Exception as e:
    print(f"  ❌  Request failed: {e}")

try:
    today_str = datetime.now().strftime("%d/%m/%Y")
    r2 = requests.get(
        f"{BASE365}/web/games/",
        headers=HDR365,
        params={**BASE_PARAMS, "competitions": COMP_ID, "startDate": today_str, "endDate": today_str},
        timeout=15,
    )
    if r2.status_code == 200:
        all_games = r2.json().get("games") or []
        print(f"\n  Today ({today_str}) – all statuses: {len(all_games)} game(s)")
        for g in all_games:
            home = (g.get("homeCompetitor") or {}).get("name", "?")
            away = (g.get("awayCompetitor") or {}).get("name", "?")
            hg   = (g.get("homeCompetitor") or {}).get("score", "?")
            ag   = (g.get("awayCompetitor") or {}).get("score", "?")
            sg   = g.get("statusGroup", "?")
            st   = g.get("shortStatusText", "?")
            kick = (g.get("startTime") or "")[:16]
            print(f"    [{sg}/{st}] {home} {hg}–{ag} {away}  kick={kick}")
    else:
        print(f"  ⚠️  /web/games/ returned {r2.status_code}")
except Exception as e:
    print(f"  ❌  Today-games request failed: {e}")

print()
