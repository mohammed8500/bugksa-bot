import time
import tweepy
from openai import OpenAI

# =======================
# 🔑 ضع مفاتيحك هنا
# =======================
OPENAI_API_KEY = ""
X_API_KEY = ""
X_API_SECRET = ""
X_ACCESS_TOKEN = ""
X_ACCESS_SECRET = ""

# =======================
# إعداد OpenAI
# =======================
ai_client = OpenAI(api_key=OPENAI_API_KEY)

# =======================
# إعداد X (تويتر)
# =======================
client = tweepy.Client(
    consumer_key=X_API_KEY,
    consumer_secret=X_API_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_SECRET,
    wait_on_rate_limit=True
)

# =======================
# حسابات الأندية (سعودي + عالمي)
# =======================
TARGET_ACCOUNTS = [
    "187642106",   # الهلال
    "192134541",   # النصر
    "136691494",   # الاتحاد
    "165338563",   # الأهلي
    "136691494",   # ريال مدريد
    "19705747",    # برشلونة
    "14573900",    # مان سيتي
    "19672628"     # ليفربول
]

# لمنع التكرار
replied_tweets = set()

# =======================
# 🧠 عقل BugKSA
# =======================
def bugksa_brain(text):
    prompt = f"""
You are @BugKSA, a sarcastic football fan.
Rules:
- Detect the language of the tweet.
- Reply in the SAME language.
- Style: 80% football sarcasm, 20% tech humor.
- One short line only.
- Do NOT repeat the same joke.
Tweet: {text}
"""
    try:
        response = ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}]
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print("AI Error:", e)
        return "🤖⚽ السيرفر تعثر… الكورة ما وقفت."

# =======================
# تشغيل البوت
# =======================
def run_bot():
    me = client.get_me(user_auth=True)
    my_id = me.data.id
    print(f"🚀 BugKSA شغّال | ID: {my_id}")

    last_mention_id = None
    last_tweet_ids = {uid: None for uid in TARGET_ACCOUNTS}

    while True:
        try:
            # 1️⃣ الرد على المنشنز
            mentions = client.get_users_mentions(id=my_id, since_id=last_mention_id, user_auth=True)
            if mentions and mentions.data:
                last_mention_id = mentions.meta["newest_id"]
                for tweet in mentions.data:
                    if tweet.id in replied_tweets:
                        continue
                    reply = bugksa_brain(tweet.text)
                    client.create_tweet(text=reply, in_reply_to_tweet_id=tweet.id)
                    replied_tweets.add(tweet.id)
                    print("📩 رد على منشن:", reply)

            # 2️⃣ قنص حسابات الأندية
            for user_id in TARGET_ACCOUNTS:
                tweets = client.get_users_tweets(id=user_id, since_id=last_tweet_ids[user_id], max_results=5)
                if tweets and tweets.data:
                    last_tweet_ids[user_id] = tweets.meta["newest_id"]
                    for tweet in tweets.data:
                        if tweet.id in replied_tweets:
                            continue
                        if tweet.text.startswith("RT"):
                            continue
                        reply = bugksa_brain(tweet.text)
                        client.create_tweet(text=reply, in_reply_to_tweet_id=tweet.id)
                        replied_tweets.add(tweet.id)
                        print("🎯 قنص نادي:", reply)

            time.sleep(180)

        except Exception as e:
            print("⚠️ خطأ:", e)
            time.sleep(60)

# =======================
if __name__ == "__main__":
    run_bot()