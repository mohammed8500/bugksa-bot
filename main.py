import os
import time
import random
import json
import tweepy
from openai import OpenAI

# =========================
# 1) المفاتيح: Railway (ENV) أو يدوي للجوال/Colab
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
X_API_KEY = os.getenv("X_API_KEY", "")
X_API_SECRET = os.getenv("X_API_SECRET", "")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_SECRET = os.getenv("X_ACCESS_SECRET", "")

# لو بتشغله على الجوال/Colab بدون Variables:
# حط مفاتيحك بين الاقتباسات هنا (واترك اللي فوق مثل ما هو)
if not OPENAI_API_KEY:
    OPENAI_API_KEY = ""
if not X_API_KEY:
    X_API_KEY = ""
if not X_API_SECRET:
    X_API_SECRET = ""
if not X_ACCESS_TOKEN:
    X_ACCESS_TOKEN = ""
if not X_ACCESS_SECRET:
    X_ACCESS_SECRET = ""

def _clean(s: str) -> str:
    return (s or "").strip().replace("\r", "").replace("\n", "")

OPENAI_API_KEY  = _clean(OPENAI_API_KEY)
X_API_KEY       = _clean(X_API_KEY)
X_API_SECRET    = _clean(X_API_SECRET)
X_ACCESS_TOKEN  = _clean(X_ACCESS_TOKEN)
X_ACCESS_SECRET = _clean(X_ACCESS_SECRET)

if not all([OPENAI_API_KEY, X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_SECRET]):
    raise RuntimeError("❌ ناقص مفاتيح: تأكد حاطها في Railway Variables أو بين الاقتباسات داخل main.py")

# =========================
# 2) إعداد OpenAI + X
# =========================
ai = OpenAI(api_key=OPENAI_API_KEY)

# Tweepy v2 Client (OAuth 1.0a user context)
client = tweepy.Client(
    consumer_key=X_API_KEY,
    consumer_secret=X_API_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_SECRET,
    wait_on_rate_limit=True,
)

# =========================
# 3) الإعدادات (أندية + حسابات عالمية)
# =========================
# حط اليوزرات هنا (بدون @)
SAUDI_CLUBS = [
    "Alhilal_FC",     # الهلال (عدّل حسب الحساب الرسمي اللي تبيه)
    "ALAHLI_FC",      # الأهلي
    "ittihad",        # الاتحاد
    "AlNassrFC",      # النصر
]

GLOBAL_CLUBS = [
    "realmadrid",
    "FCBarcelona",
    "ManCity",
    "LFC",
]

TARGET_USERNAMES = list(dict.fromkeys(SAUDI_CLUBS + GLOBAL_CLUBS))  # بدون تكرار

# =========================
# 4) منع التكرار (ذاكرة بسيطة + ملف)
# =========================
STATE_FILE = "state.json"

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {
            "last_mention_id": None,
            "last_seen_by_user": {},     # {user_id: last_tweet_id}
            "replied_tweet_ids": [],     # آخر 500 رد
        }

def save_state(state):
    try:
        # قصّ القائمة لتبقى خفيفة
        state["replied_tweet_ids"] = state.get("replied_tweet_ids", [])[-500:]
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except:
        pass

state = load_state()
replied_set = set(state.get("replied_tweet_ids", []))

# =========================
# 5) توليد ردود ساخرة متنوعة (80% كورة / 20% تقني)
# =========================
ROAST_VARIANTS = [
    "You are @BugKSA: sarcastic football fan. 80% football, 20% tech. One-liner only. Avoid repeating the same tech joke (no constant 404).",
    "You are @BugKSA: witty Saudi football fan. 80% football banter, 20% geek references. One-liner. Use varied metaphors (lag, patch, ping, update, cache, timeout, hotfix).",
    "You are @BugKSA: sharp football troll but not hateful. 80% football, 20% tech. One-liner. Keep it fresh and short.",
]

def generate_reply(tweet_text: str) -> str:
    prompt = f"""
{random.choice(ROAST_VARIANTS)}

Rules:
1) Detect the language of the tweet (Arabic/English/Spanish/etc).
2) Reply in the SAME language.
3) Be short: one line.
4) No insults on race/religion, no harassment. Just football sarcasm.

Tweet:
{tweet_text}
""".strip()

    try:
        resp = ai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.9,
            max_tokens=80,
        )
        text = resp.choices[0].message.content.strip()
        # ضمان سطر واحد
        return " ".join(text.splitlines()).strip()[:280]
    except Exception:
        return "System lag… even VAR crashed. 🤖⚽"

# =========================
# 6) أدوات: جلب ID من اليوزر
# =========================
def get_user_id(username: str) -> str:
    r = client.get_user(username=username, user_auth=True)
    if not r or not r.data:
        raise RuntimeError(f"❌ ما قدرت أجيب ID لليوزر: {username}")
    return str(r.data.id)

def safe_create_reply(text: str, in_reply_to_tweet_id: str):
    # بعض الحالات لو الرد فاضي أو طويل…
    text = (text or "").strip()
    if not text:
        return None
    if len(text) > 280:
        text = text[:277] + "..."
    return client.create_tweet(text=text, in_reply_to_tweet_id=in_reply_to_tweet_id, user_auth=True)

# =========================
# 7) تشغيل البوت
# =========================
def run_bot():
    # تحقق من صحة الدخول (إذا فشل هنا = Variables غلط)
    me = client.get_me(user_auth=True)
    my_id = str(me.data.id)
    print(f"✅ Logged in as: @{me.data.username} (ID: {my_id})")

    # جهّز IDs للحسابات المستهدفة
    target_ids = []
    for u in TARGET_USERNAMES:
        try:
            uid = get_user_id(u)
            target_ids.append(uid)
            if uid not in state["last_seen_by_user"]:
                state["last_seen_by_user"][uid] = None
        except Exception as e:
            print(f"⚠️ تخطيت {u}: {e}")

    # حلقة
    while True:
        try:
            # 1) الرد على المنشنز
            mentions = client.get_users_mentions(
                id=my_id,
                since_id=state.get("last_mention_id"),
                user_auth=True,
                max_results=20,
            )

            if mentions and mentions.data:
                state["last_mention_id"] = mentions.meta.get("newest_id", state.get("last_mention_id"))
                for tw in mentions.data:
                    tid = str(tw.id)
                    if tid in replied_set:
                        continue
                    reply = generate_reply(tw.text)
                    safe_create_reply(reply, tid)
                    replied_set.add(tid)
                    state["replied_tweet_ids"].append(tid)
                    print(f"📩 Mention reply -> {reply}")

            # 2) قنص حسابات الأندية (آخر تغريداتهم)
            for uid in target_ids:
                since_id = state["last_seen_by_user"].get(uid)
                tweets = client.get_users_tweets(
                    id=uid,
                    since_id=since_id,
                    user_auth=True,
                    max_results=5,
                    exclude=["retweets", "replies"],
                )
                if tweets and tweets.data:
                    state["last_seen_by_user"][uid] = tweets.meta.get("newest_id", since_id)
                    for tw in tweets.data:
                        tid = str(tw.id)
                        if tid in replied_set:
                            continue
                        reply = generate_reply(tw.text)
                        safe_create_reply(reply, tid)
                        replied_set.add(tid)
                        state["replied_tweet_ids"].append(tid)
                        print(f"🎯 Club snipe -> {reply}")

            save_state(state)

            # سليب ذكي لتخفيف الحمل
            time.sleep(120 + random.randint(0, 60))

        except Exception as e:
            print(f"⚠️ Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_bot()