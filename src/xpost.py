import os
import subprocess
import json
import requests

TARGET_USER = "bookoffnishigu1"
STATE_FILE = "last_tweet_id.txt"

# 営業時間や買取に関係ない定型ポストを弾くための除外キーワード
EXCLUDE_KEYWORDS = [
    "営業時間",
    "査定受付",
    "精算時間",
    "本日の営業時間",
    "営業時間のご案内",
]

def load_last_tweet_id():
    """前回通知したツイートIDを読み込む（GitHub Actions環境対応）"""
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
    """twitter-cliを使って最新のポストをJSON形式で取得する（文字コード安全対策済み）"""
    print(f"Fetching tweets for @{TARGET_USER}...")
    
    # 環境変数（GitHub Secretsから渡されるTWITTER_AUTH_TOKEN / TWITTER_CT0）を継承
    env = os.environ.copy()
    
    result = subprocess.run(
        ["python", "-m", "twitter_cli.cli", "user-posts", TARGET_USER, "--max", "10", "--json"],
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
        print("Warning: twitter-cli returned empty output.")
        return []
    
    try:
        data = json.loads(raw_output)
        return data
    except json.JSONDecodeError:
        print("Failed to parse JSON from twitter-cli output.")
        print(f"Raw output (first 200 chars): {raw_output[:200]}")
        return []

def should_exclude(text: str) -> bool:
    """不要なポスト（営業時間など）の判定"""
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in text:
            return True
    return False

def send_to_discord(webhook_url, tweet_text, tweet_url):
    """DiscordのWebhookへ通知を送信"""
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
        print("No tweets found or failed to fetch.")
        return

    # データの構造が辞書でラップされている場合のフォールバック
    if isinstance(tweets, dict):
        tweets = tweets.get("tweets", tweets.get("data", [tweets]))

    if not isinstance(tweets, list):
        print(f"Unexpected data format: {tweets}")
        return

    last_sent_id = load_last_tweet_id()
    latest_valid_tweet = None

    # 新着順に確認
    for tweet in tweets:
        # 型に応じた安全な値の抽出
        if isinstance(tweet, dict):
            text = tweet.get("text", tweet.get("full_text", ""))
            tweet_id = str(tweet.get("id", tweet.get("id_str", "")))
        elif isinstance(tweet, str):
            text = tweet
            tweet_id = ""
        else:
            continue

        if not tweet_id or not text:
            continue

        # すでに通知済みのIDに到達したらループを抜ける（重複防止）
        if tweet_id == last_sent_id:
            print("Reached already notified tweet. Stopping check.")
            break

        # フィルタリング判定（除外キーワード）
        if should_exclude(text):
            print(f"Skipping (excluded keyword): {text[:30]}...")
            continue

        # 有効な新着ツイートを発見
        tweet_url = f"https://x.com/{TARGET_USER}/status/{tweet_id}"
        latest_valid_tweet = {"id": tweet_id, "text": text, "url": tweet_url}
        break  # 一番新しい有効な1件を処理対象にする

    if latest_valid_tweet:
        print(f"Processing valid tweet: {latest_valid_tweet['text'][:30]}...")
        send_to_discord(webhook_url, latest_valid_tweet["text"], latest_valid_tweet["url"])
        save_last_tweet_id(latest_valid_tweet["id"])
    else:
        print("No new valid tweets to notify.")

if __name__ == "__main__":
    main()
