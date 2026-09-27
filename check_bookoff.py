import os
import requests

# FxTwitterの公開APIを利用してXのツイートを取得（Xの直接ブロックを回避）
USERNAME = "bookoffnishigu1"
API_URL = f"https://api.fxtwitter.com/{USERNAME}"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

def check_and_notify():
    if not WEBHOOK_URL:
        print("Error: DISCORD_WEBHOOK_URL is not set.")
        return

    print(f"Fetching tweets via FxTwitter API for: @{USERNAME}")
    
    try:
        response = requests.get(API_URL, timeout=10)
        if response.status_code != 200:
            print(f"Failed to fetch data: {response.status_code}, {response.text}")
            return

        data = response.json()
        tweets = data.get("tweets", [])

        if not tweets:
            print("No tweets found or failed to parse.")
            return

        # 最新の数件をチェック
        for tweet in tweets[:3]:
            text = tweet.get("text", "")
            link = tweet.get("url", f"https://x.com/{USERNAME}")
            
            print(f"Checking tweet: {text[:30]}...")

            # "買取" というキーワードが含まれているかチェック
            if "買取" in text:
                message = {
                    "content": f"📢 **ブックオフ新宿西口店から「買取」に関する告知が見つかりました！**\n\n> {text}\n\n🔗 ツイートリンク:\n{link}"
                }
                
                response = requests.post(WEBHOOK_URL, json=message)
                if response.status_code == 204:
                    print("Successfully sent notification to Discord!")
                else:
                    print(f"Failed to send Discord notification: {response.status_code}")
        
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    check_and_notify()
