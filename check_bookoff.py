import os
import requests

# 監視したいXアカウントのURL
TARGET_URL = "https://x.com/bookoffnishigu1"

# Jina Reader APIのURL（Webページをテキスト化して取得できる無料API）
API_URL = f"https://r.jina.ai/{TARGET_URL}"

# DiscordのWebhook URLを環境変数から取得
WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

def check_and_notify():
    if not WEBHOOK_URL:
        print("Error: DISCORD_WEBHOOK_URL is not set.")
        return

    print(f"Fetching data via API for: {TARGET_URL}")
    
    headers = {
        "Accept": "text/plain"
    }

    try:
        response = requests.get(API_URL, headers=headers)
        if response.status_code != 200:
            print(f"Failed to fetch data: {response.status_code}, {response.text}")
            return

        page_text = response.text
        
        # デバッグ用：取得したテキストの一部をログに出力
        print("Successfully fetched page content. Checking for keywords...")

        # ツイート単位や行単位、あるいはページ全体から「買取」というキーワードを検出する
        # ※Jina Readerはツイート内容をテキストとしてきれいに抽出してくれます
        if "買取" in page_text:
            # 簡易的にキーワードが含まれていることを検知
            # ※実際のツイート文面やリンクをより綺麗に切り出したい場合は行ごとに分割してチェックします
            
            lines = page_text.split("\n")
            matched_lines = [line for line in lines if "買取" in line]
            
            if matched_lines:
                combined_text = "\n".join(matched_lines[:3]) # 該当箇所の抜粋
                message = {
                    "content": f"📢 **ブックオフ新宿西口店から「買取」に関する告知の可能性があります！**\n\n【検出されたテキスト抜粋】\n> {combined_text}\n\n🔗 確認用リンク:\n{TARGET_URL}"
                }
                
                response = requests.post(WEBHOOK_URL, json=message)
                if response.status_code == 204:
                    print("Successfully sent notification to Discord!")
                else:
                    print(f"Failed to send Discord notification: {response.status_code}")
        else:
            print("No matching keywords found in the current feed.")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    check_and_notify()