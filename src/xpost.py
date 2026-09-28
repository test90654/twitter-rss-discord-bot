os.environ.get  # 念のためインポート確認
import os
import subprocess
import json
import requests

TARGET_USER = "bookoffnishigu1"
STATE_FILE = "last_tweet_id.txt"

EXCLUDE_KEYWORDS = [
    "営業時間",
    "査定受付",
    "精算時間",
    "本日の営業時間",
    "営業時間のご案内",
]

def load_last_tweet_id():
    """前回通知したツイートIDを読み込む"""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return None

def save_last_tweet_id(tweet_id):
    """今回通知したツイートIDを保存する"""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(tweet_id))
    except Exception as e:
        print(f"Failed to save state: {e}")

def fetch_tweets():
    """twitter-cliを実行して最新ポストを取得する"""
    print(f"Fetching tweets for @{TARGET_USER}...")
    
    # GitHub Secrets から渡される環境変数をそのまま利用
    env = os.environ.copy()
    
    result = subprocess.run(
        ["python", "-m", "twitter_cli.cli", "user-posts", TARGET_USER, "--max", "5", "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
        env=env
    )
    
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        return []
    
    try:
        data = json.loads(result.stdout.strip())
    except Exception as e:
        print(f"JSON Parse Error: {e}")
        return []

    if isinstance(data, dict):
        return data.get("tweets", data.get("data", [data]))
    elif isinstance(data, list):
        return data
    return []

def send_to_discord(webhook_url, tweet_text, tweet_url):
    """Discordへ通知を送信"""
    payload = {
        "content": f"🚨 **【ブックオフプラス新宿駅西口店 買取情報】** 🚨\n\n{tweet_text}\n\n🔗 {tweet_url}"
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

    last_sent_id = load_last_tweet_id()
    latest_valid_tweet = None

    # 有効な最新ツイートを走査
    for item in tweets:
        if isinstance(item, dict):
            text = item.get("text", item.get("full_text", ""))
            tweet_id = str(item.get("id", item.get("id_str", "")))
        else:
            continue

        if not tweet_id or not text:
            continue

        # すでに通知済みのIDに到達したら終了
        if tweet_id == last_sent_id:
            print("Reached already notified tweet. Stopping check.")
            break

        # 除外キーワードチェック
        if any(kw in text for kw in EXCLUDE_KEYWORDS):
            print(f"Skipping (excluded): {text[:30]}...")
            continue

        # 新着の有効なツイートを発見
        tweet_url = f"https://x.com/{TARGET_USER}/status/{tweet_id}"
        latest_valid_tweet = {"id": tweet_id, "text": text, "url": tweet_url}
        break  # 一番新しい1件だけを処理対象にする

    if latest_valid_tweet:
        print(f"New valid tweet found: {latest_valid_tweet['text'][:30]}...")
        send_to_discord(webhook_url, latest_valid_tweet["text"], latest_valid_tweet["url"])
        save_last_tweet_id(latest_valid_tweet["id"])
    else:
        print("No new valid tweets to notify.")

if __name__ == "__main__":
    main()