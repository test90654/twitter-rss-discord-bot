import os
import requests

QUERY = "bookoffnishigu1 買取"
# Yahoo!リアルタイム検索のURLをJina Reader経由で取得（JSがレンダリングされる）
SEARCH_URL = f"https://search.yahoo.co.jp/realtime/search?p={requests.utils.quote(QUERY)}"
API_URL = f"https://r.jina.ai/{SEARCH_URL}"

WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

def check_and_notify():
    if not WEBHOOK_URL:
        print("Error: DISCORD_WEBHOOK_URL is not set.")
        return

    print(f"Fetching rendered search results for: {QUERY}")
    headers = {"Accept": "text/plain"}

    try:
        response = requests.get(API_URL, headers=headers, timeout=15)
        if response.status_code != 200:
            print(f"Failed to fetch data: {response.status_code}, {response.text}")
            return

        page_text = response.text
        print("Successfully fetched page content. Checking for keywords...")

        if "買取" in page_text:
            lines = page_text.split("\n")
            matched_lines = [line for line in lines if "買取" in line]
            
            if matched_lines:
                combined_text = "\n".join(matched_lines[:3])
                message = {
                    "content": f"📢 **Yahoo!リアルタイム検索経由で「買取」の告知を検知しました！**\n\n> {combined_text}\n\n🔗 検索結果URL:\n{SEARCH_URL}"
                }
                
                res = requests.post(WEBHOOK_URL, json=message)
                if res.status_code == 204:
                    print("Successfully sent notification to Discord!")
                else:
                    print(f"Failed to send Discord notification: {res.status_code}")
        else:
            print("No matching keywords found in the search results.")

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    check_and_notify()