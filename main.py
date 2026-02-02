import time
import tweepy
from openai import OpenAI

# ====== ضع مفاتيحك بين "" فقط ======
OPENAI_API_KEY = ""
X_API_KEY = ""
X_API_SECRET = ""
X_ACCESS_TOKEN = ""
X_ACCESS_SECRET = ""
# ==================================

client_ai = OpenAI(api_key=OPENAI_API_KEY)

# OAuth1 (مهم جدًا)
auth = tweepy.OAuth1UserHandler(
    X_API_KEY,
    X_API_SECRET,
    X_ACCESS_TOKEN,
    X_ACCESS_SECRET
)

api_v1 = tweepy.API(auth)

# Client v2
client = tweepy.Client(
    consumer_key=X_API_KEY,
    consumer_secret=X_API_SECRET,
    access_token=X_ACCESS_TOKEN,
    access_token_secret=X_ACCESS_SECRET,
    wait_on_rate_limit=True
)

# حسابات الأندية
TARGET_ACCOUNTS = [
    "16975244",   # الهلال
    "192134541",  # النصر
    "198617866",  # الاتحاد
    "22609390",   # الأهلي
    "136691494",  # ريال مدريد
    "19705747"    # برشلونة
]

def bugksa_brain(text):
    prompt = f"""
You are a sarcastic football fan.
1. Detect language of tweet.
2. Reply in SAME language.
3. Style: 80% football sarcasm, 20% tech humor.
4. One short line only.
Tweet: {text}
"""
    r = client_ai.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}]
    )
    return r.choices[0].message.content.strip()

def run_bot():
    me = api_v1.verify_credentials()
    my_id = me.id
    print("✅ Logged in as:", me.screen_name)

    replied = set()
    last_mentions_id = None
    last_tweets = {u: None for u in TARGET_ACCOUNTS}

    while True:
        # ===== منشن =====
        mentions = client.get_users_mentions(
            id=my_id,
            since_id=last_mentions_id,
            user_auth=True
        )

        if mentions and mentions.data:
            last_mentions_id = mentions.meta["newest_id"]
            for tweet in mentions.data:
                if tweet.id in replied:
                    continue
                reply = bugksa_brain(tweet.text)
                client.create_tweet(
                    text=reply,
                    in_reply_to_tweet_id=tweet.id,
                    user_auth=True
                )
                replied.add(tweet.id)
                print("📩 منشن:", reply)

        # ===== قنص =====
        for user_id in TARGET_ACCOUNTS:
            tweets = client.get_users_tweets(
                id=user_id,
                since_id=last_tweets[user_id],
                max_results=5,
                user_auth=True
            )

            if tweets and tweets.data:
                last_tweets[user_id] = tweets.meta["newest_id"]
                for tweet in tweets.data:
                    if tweet.id in replied:
                        continue
                    if tweet.text.startswith("RT"):
                        continue
                    reply = bugksa_brain(tweet.text)
                    client.create_tweet(
                        text=reply,
                        in_reply_to_tweet_id=tweet.id,
                        user_auth=True
                    )
                    replied.add(tweet.id)
                    print("🎯 نادي:", reply)

        time.sleep(180)

if __name__ == "__main__":
    run_bot()