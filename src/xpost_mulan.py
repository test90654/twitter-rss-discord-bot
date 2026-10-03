import os
import subprocess
import json
import requests
from pathlib import Path
import time
from google import genai
from google.genai import types

TARGET_USER = "mulanakiba_chuo"
BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "data" / "mulan_queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = BASE_DIR.parent / "last_tweet_id_mulanakiba_chuo.txt"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 必須のハッシュタグ
REQUIRED_HASHTAG = "#ムラ中買取"

def load_last_tweet_id():
    if STATE_FILE.exists():
        try:
            return STATE_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return None

def save_last_tweet_id(tweet_id):
    try:
        STATE_FILE.write_text(str(tweet_id), encoding="utf-8")
    except Exception as e:
        print(f"Failed to save state: {e}")

def fetch_tweets():
    print(f"Fetching tweets for @{TARGET_USER} (Hashtag Filter Test)...")
    env = os.environ.copy()
    result = subprocess.run(
        ["python", "-m", "twitter_cli.cli", "user-posts", TARGET_USER, "--max", "20", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore", env=env
    )
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
        return []
    
    raw_output = result.stdout.strip()
    if not raw_output:
        return []
    
    try:
        data = json.loads(raw_output)
        tweets = data.get("tweets", data.get("data", data if isinstance(data, list) else [data]))
        return tweets if isinstance(tweets, list) else []
    except json.JSONDecodeError as e:
        print(f"JSON Decode Error: {e}")
        return []

# --- Gemini OCR 解析関数（自動リトライ付き） ---
def analyze_image_with_gemini(image_bytes):
    if not GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY が設定されていません。")
        return ""
    
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = """
    添付された買取表の画像を読み取り、以下のルールに従ってMarkdownの表形式（テーブル）で出力してください。
    余計な挨拶、解説文、およびツイートの冒頭にあるような宣伝文句や更新アナウンスは絶対に含めず、表の中にある個別の商品データのみを出力してください。

    【出力フォーマット（Markdownテーブル）】
    | シリーズ / キャラクター | 買取価格 | 更新日 | 型番 |
    |---|---|---|---|
    | 商品名が入る | 価格が入る | 更新日が入る | 型番が入る |

    【処理ルール】
    1. **更新日の付与**: 画像内から更新日を読み取り、各行に反映する。
    2. **不要な文言の除外**: 注意事項、営業時間、宣伝文句などのノイズ文言はすべて除外する。
    3. **シリーズ名・カテゴリー名の付与**: 赤文字等で記載されているシリーズ名やカテゴリー名を、商品名の先頭に必ず含める。
    4. **重複・バリエーションの処理**: 型番が同じでもキャラクター違いやバージョン違いがある場合は絶対に統合せず別行として出力する。
    """

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'),
                    prompt
                ]
            )
            return response.text
        except Exception as e:
            if "503" in str(e) and attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2
                print(f"⚠️ Gemini混雑中(503)。{wait_time}秒後に再試行... ({attempt+1}/{max_retries})")
                time.sleep(wait_time)
            else:
                print(f"❌ Gemini解析エラー: {e}")
                return ""
    return ""

def parse_gemini_output(text):
    items = []
    if not text:
        return items
    lines = text.strip().split("\n")
    for line in lines:
        if "|" in line and "---" not in line and "シリーズ" not in line and "買取価格" not in line:
            parts = [p.strip() for p in line.split("|")]
            parts = [p for p in parts if p != ""]
            if len(parts) >= 4:
                price_str = parts[1].replace("¥", "").replace(",", "")
                items.append({
                    "name": parts[0],
                    "price": price_str,
                    "update_date": parts[2],
                    "model_number": parts[3]
                })
    return items

def download_image(url, save_path):
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            with open(save_path, "wb") as f:
                f.write(res.content)
            return res.content  # バイトデータを返す（OCR用）
    except Exception as e:
        print(f"Failed to download image {url}: {e}")
    return None

def send_to_discord(webhook_url, tweet_text, tweet_url, parsed_count=0):
    payload = {
        "content": f"🚨 **【@{TARGET_USER} 買取情報・OCR解析完了】** 🚨\n\n📌 **抽出された商品数:** `{parsed_count}件`\n\n{tweet_text}\n\n👉 元ツイート: <{tweet_url}>"
    }
    response = requests.post(webhook_url, json=payload)
    if response.status_code == 204:
        print("Successfully sent to Discord with OCR info!")

def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    tweets = fetch_tweets()
    if not tweets:
        print("No tweets fetched.")
        return

    last_sent_id = load_last_tweet_id()
    newest_fetched_id = None
    count = 0
    valid_tweets_to_send = []

    for i, tweet in enumerate(tweets):
        if not isinstance(tweet, dict):
            continue
            
        tweet_id = str(tweet.get("id", tweet.get("id_str", "")))
        text = tweet.get("text", tweet.get("full_text", ""))

        if not tweet_id:
            continue

        if i == 0:
            newest_fetched_id = tweet_id

        # 前回の処理済みIDに到達したら終了
        if tweet_id == last_sent_id:
            print(f"Reached last processed tweet ID: {tweet_id}")
            break
        
        # 【重要】「#ムラ中買取」が含まれていない場合はスキップする
        if REQUIRED_HASHTAG not in text:
            print(f"Skipping tweet {tweet_id} (No hashtag)")
            continue
            
        print(f"Target tweet found! ID: {tweet_id}")

        # メディア（画像）のURLを抽出
        image_urls = []
        entities = tweet.get("entities", {})
        media_list = entities.get("media", [])
        for m in media_list:
            if m.get("type") == "photo":
                image_urls.append(m.get("media_url_https") or m.get("media_url"))
                
        if not image_urls and "media" in tweet:
            for m in tweet.get("media", []):
                if isinstance(m, dict):
                    image_urls.append(m.get("url") or m.get("media_url") or m.get("media_url_https"))

        if not image_urls:
            print(f"-> Tweet {tweet_id} has hashtag, but no images attached.")
            continue

        tweet_total_parsed = 0
        has_new_media = False

        # 画像のダウンロードとメタデータ保存 ＆ OCR処理
        for idx, img_url in enumerate(image_urls):
            if not img_url:
                continue
            file_extension = img_url.split("?")[0].split(".")[-1]
            if file_extension not in ["jpg", "jpeg", "png"]:
                file_extension = "jpg"
                
            img_filename = f"{tweet_id}_{idx+1}.{file_extension}"
            img_path = QUEUE_DIR / img_filename
            meta_path = QUEUE_DIR / f"{tweet_id}_{idx+1}.json"
            
            if img_path.exists():
                print(f"-> Image already exists: {img_filename}")
                continue
                
            print(f"Downloading image: {img_filename}")
            img_bytes = download_image(img_url, img_path)
            if img_bytes:
                print(f"🤖 Geminiで事前OCR解析を実行中 ({img_filename})...")
                raw_text = analyze_image_with_gemini(img_bytes)
                parsed_items = parse_gemini_output(raw_text)
                print(f"✨ 抽出結果: {len(parsed_items)} 件")
                
                tweet_total_parsed += len(parsed_items)
                has_new_media = True

                tweet_url = f"https://x.com/{TARGET_USER}/status/{tweet_id}"
                meta_data = {
                    "tweet_id": f"{tweet_id}_{idx+1}",
                    "original_tweet_id": tweet_id,
                    "text": text,
                    "image_file": img_filename,
                    "tweet_url": tweet_url,
                    "parsed_items": parsed_items
                }
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta_data, f, ensure_ascii=False, indent=2)
                count += 1

        if has_new_media:
            valid_tweets_to_send.append({
                "text": text,
                "url": f"https://x.com/{TARGET_USER}/status/{tweet_id}",
                "count": tweet_total_parsed
            })

    # Discord通知と状態保存
    if valid_tweets_to_send:
        valid_tweets_to_send.reverse()
        for vt in valid_tweets_to_send:
            if webhook_url:
                send_to_discord(webhook_url, vt["text"], vt["url"], vt["count"])
        if newest_fetched_id:
            save_last_tweet_id(newest_fetched_id)

    print(f"Successfully collected {count} new valid買取 images/metadata with OCR.")

if __name__ == "__main__":
    main()
