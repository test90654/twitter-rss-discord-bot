import os
import subprocess
import json
import requests

TARGET_USER = "mulanakiba_chuo"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "..", "last_tweet_id_mulanakiba_chuo.txt")

EXCLUDE_KEYWORDS = [
    "営業時間",
    "査定受付",
    "精算時間",
    "本日の営業時間",
    "営業時間のご案内",
    "おはようございます",
    "定休日",
    "店頭買取業務休止",
    "買取価格等に関するお問い合わせ",
]

def load_last_tweet_id():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return None

def save_last_tweet_id(tweet_id):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(tweet_id))
    except Exception as e:
        print(f"Failed to save state: {e}")

def fetch_tweets():
    print(f"Fetching tweets for @{TARGET_USER}...")
    env = os.environ.copy()
    
    result = subprocess.run(
        ["python", "-m", "twitter_cli.cli", "user-posts", TARGET_USER, "--max", "20", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        env=env
    )
    
    if result.returncode != 0:
        print(f"Error fetching tweets: {result.stderr}")
        return []
    
    raw_output = result.stdout.strip()
    if not raw_output:
        return []
    
    try:
        return json.loads(raw_output)
    except json.JSONDecodeError:
        return []

def should_exclude(text: str) -> bool:
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in text:
            return True
    return False

def send_to_discord(webhook_url, tweet_text, tweet_url):
    payload = {
        "content": f"🚨 **【@{TARGET_USER} 買取情報】** 🚨\n\n{tweet_text}\n\n👉 元ツイート: <{tweet_url}>"
    }
    response = requests.post(webhook_url, json=payload)
    if response.status_code == 204:
        print("Successfully sent to Discord!")
    else:
        print(f"Failed to send to Discord: {response.status_code}, {response.text}")

def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("Error: DISCORD_WEBHOOK_URL environment variable is not set.")
        return

    tweets = fetch_tweets()
    if not tweets:
        print("No tweets found.")
        return

    if isinstance(tweets, dict):
        tweets = tweets.get("tweets", tweets.get("data", [tweets]))

    if not isinstance(tweets, list):
        return

    last_sent_id = load_last_tweet_id()
    valid_tweets_to_send = []
    newest_fetched_id = None

    for i, tweet in enumerate(tweets):
        if isinstance(tweet, dict):
            text = tweet.get("text", tweet.get("full_text", ""))
            tweet_id = str(tweet.get("id", tweet.get("id_str", "")))
        else:
            continue

        if not tweet_id or not text:
            continue

        if i == 0:
            newest_fetched_id = tweet_id

        if tweet_id == last_sent_id:
            break

        if should_exclude(text):
            continue

        tweet_url = f"https://x.com/{TARGET_USER}/status/{tweet_id}"
        valid_tweets_to_send.append({"id": tweet_id, "text": text, "url": tweet_url})

    if valid_tweets_to_send:
        valid_tweets_to_send.reverse()
        for vt in valid_tweets_to_send:
            send_to_discord(webhook_url, vt["text"], vt["url"])
        if newest_fetched_id:
            save_last_tweet_id(newest_fetched_id)
    else:
        print("No new valid tweets to notify.")

if __name__ == "__main__":
    main()
