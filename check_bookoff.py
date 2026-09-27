import os
import requests
from bs4 import BeautifulSoup

<<<<<<< HEAD
# Yahoo!リアルタイム検索で検索するキーワード
# 例: アカウント名や店舗名 ＋ 「買取」
QUERY = "bookoffnishigu1 買取"
URL = f"https://search.yahoo.co.jp/realtime/search?p={requests.utils.quote(QUERY)}"
=======
# FxTwitterの公開APIを利用してXのツイートを取得（Xの直接ブロックを回避）
USERNAME = "bookoffnishigu1"
API_URL = f"https://api.fxtwitter.com/{USERNAME}"
>>>>>>> 8eb8574776355cbcf60aa79bdf9f45e64293cee5

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

def check_and_notify():
    if not WEBHOOK_URL:
        print("Error: DISCORD_WEBHOOK_URL is not set.")
        return

<<<<<<< HEAD
    print(f"Fetching Yahoo! Realtime Search for: {QUERY}")
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        response = requests.get(URL, headers=headers, timeout=10)
=======
    print(f"Fetching tweets via FxTwitter API for: @{USERNAME}")
    
    try:
        response = requests.get(API_URL, timeout=10)
>>>>>>> 8eb8574776355cbcf60aa79bdf9f45e64293cee5
        if response.status_code != 200:
            print(f"Failed to fetch Yahoo search: {response.status_code}")
            return

<<<<<<< HEAD
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # ページ内からツイート（投稿）のテキスト要素を抽出
        # Yahoo!リアルタイム検索の検索結果からキーワードに一致する文面を探す
        found_texts = []
        for el in soup.find_all(['p', 'div', 'span']):
            text = el.get_text()
            if "買取" in text and len(text) > 10 and len(text) < 300:
                if text not in found_texts:
                    found_texts.append(text)

        if found_texts:
            # 抽出した最新のテキストの断片
            latest_text = found_texts[0]
            print(f"Found match: {latest_text[:30]}...")

            message = {
                "content": f"📢 **Yahoo!リアルタイム検索経由で「買取」の告知を検知しました！**\n\n> {latest_text}\n\n🔗 検索結果URL:\n{URL}"
            }
            
            response = requests.post(WEBHOOK_URL, json=message)
            if response.status_code == 204:
                print("Successfully sent notification to Discord!")
            else:
                print(f"Failed to send Discord notification: {response.status_code}")
        else:
            print("No matching posts found on Yahoo Realtime.")

=======
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
        
>>>>>>> 8eb8574776355cbcf60aa79bdf9f45e64293cee5
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    check_and_notify()
