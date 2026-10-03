import os
import subprocess
import json
import requests
from pathlib import Path
from google import genai
from google.genai import types
from PIL import Image
import io
import time

TARGET_USER = "mulanakiba_chuo"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
QUEUE_DIR = Path(BASE_DIR).parent / "data" / "mulan_queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = os.path.join(BASE_DIR, "..", "last_tweet_id_mulanakiba_chuo.txt")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

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
                last_id = f.read().strip()
                print(f"📄 読み込んだ前回の最後のTweet ID: {last_id}")
                return last_id
        except Exception as e:
            print(f"⚠️ 状態ファイルの読み込みエラー: {e}")
    print("📄 前回のTweet IDが見つかりません（初回実行またはファイルなし）")
    return None

def save_last_tweet_id(tweet_id):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(str(tweet_id))
        print(f"💾 状態ファイルを更新しました (Tweet ID: {tweet_id})")
    except Exception as e:
        print(f"❌ 状態ファイルの保存エラー: {e}")

def fetch_tweets():
    print(f"🔍 Fetching tweets for @{TARGET_USER}...")
    env = os.environ.copy()
    result = subprocess.run(
        ["python", "-m", "twitter_cli.cli", "user-posts", TARGET_USER, "--max", "5", "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore", env=env
    )
    print(f"🔍 twitter-cli returncode: {result.returncode}")
    if result.returncode != 0:
        print(f"❌ twitter-cli error output: {result.stderr}")
        return []
    raw_output = result.stdout.strip()
    if not raw_output:
        print("⚠️ twitter-cliの出力が空です。")
        return []
    try:
        data = json.loads(raw_output)
        print(f"📥 取得したJSONの型: {type(data)}")
        return data
    except json.JSONDecodeError as e:
        print(f"❌ JSONパースエラー: {e}")
        print(f"生出力の先頭100文字: {raw_output[:100]}")
        return []

def should_exclude(text: str) -> bool:
    for keyword in EXCLUDE_KEYWORDS:
        if keyword in text:
            print(f"🚫 除外キーワード '{keyword}' にヒットしました: {text[:30]}...")
            return True
    return False

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

def send_to_discord(webhook_url, tweet_text, tweet_url, parsed_count=0):
    payload = {
        "content": f"🚨 **【@{TARGET_USER} 買取情報・OCR解析完了】** 🚨\n\n📌 **抽出された商品数:** `{parsed_count}件`\n\n{tweet_text}\n\n👉 元ツイート: <{tweet_url}>"
    }
    response = requests.post(webhook_url, json=payload)
    if response.status_code == 204:
        print("✅ Discord通知送信成功!")
    else:
        print(f"⚠️ Discord通知失敗: {response.status_code}, {response.text}")

def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    tweets = fetch_tweets()
    if not tweets:
        print("⚠️ 処理対象のツイートがありませんでした。")
        return
        
    if isinstance(tweets, dict):
        tweets = tweets.get("tweets", tweets.get("data", [tweets]))
    if not isinstance(tweets, list):
        print(f"⚠️ 予期しないツイートデータの形式です: {type(tweets)}")
        return

    print(f"📊 取得したツイート総数: {len(tweets)}")

    last_sent_id = load_last_tweet_id()
    valid_tweets_to_send = []
    newest_fetched_id = None

    for i, tweet in enumerate(tweets):
        if isinstance(tweet, dict):
            text = tweet.get("text", tweet.get("full_text", ""))
            tweet_id = str(tweet.get("id", tweet.get("id_str", "")))
            
            # デバッグ用：ツイートのキーと中身をログに出力
            print(f"\n--- [Tweet #{i}] ID: {tweet_id} ---")
            print(f"テキスト前半: {text[:40]}...")
            print(f"利用可能なキー一覧: {list(tweet.keys())}")
            
            # 画像URLのキー候補を広く探す
            media_urls = tweet.get("media_urls", tweet.get("photos", tweet.get("media", [])))
            print(f"検出されたメディア情報: {media_urls}")
        else:
            continue

        if not tweet_id or not text:
            print("⚠️ IDまたはテキストが空のためスキップします。")
            continue
            
        if i == 0:
            newest_fetched_id = tweet_id
            
        if tweet_id == last_sent_id:
            print(f"🛑 前回のID ({last_sent_id}) に到達したため、ループを中断します。")
            break
            
        if should_exclude(text):
            print("🚫 除外対象のツイートのためスキップします。")
            continue

        tweet_url = f"https://x.com/{TARGET_USER}/status/{tweet_id}"
        
        parsed_items = []
        saved_image_filename = None

        if media_urls:
            # 配列または文字列のケースに対応
            img_url = media_urls[0] if isinstance(media_urls, list) else media_urls
            print(f"📷 画像URLを発見しました: {img_url}")
            try:
                img_res = requests.get(img_url)
                if img_res.status_code == 200:
                    saved_image_filename = f"{tweet_id}_0.jpg"
                    image_path = QUEUE_DIR / saved_image_filename
                    
                    with open(image_path, "wb") as f:
                        f.write(img_res.content)
                    print(f"💾 画像を保存しました: {image_path}")
                    
                    print("🤖 Geminiで事前OCR解析を実行中...")
                    raw_text = analyze_image_with_gemini(img_res.content)
                    parsed_items = parse_gemini_output(raw_text)
                    print(f"✨ 抽出結果: {len(parsed_items)} 件")
                else:
                    print(f"❌ 画像ダウンロード失敗 (Status: {img_res.status_code})")
            except Exception as e:
                print(f"❌ 画像処理中の例外エラー: {e}")
        else:
            print("ℹ️ このツイートには画像メディアが含まれていません。")

        if saved_image_filename:
            meta_data = {
                "tweet_id": tweet_id,
                "text": text,
                "tweet_url": tweet_url,
                "image_file": saved_image_filename,
                "parsed_items": parsed_items
            }
            json_path = QUEUE_DIR / f"{tweet_id}.json"
            with open(json_path, "w", encoding="utf-8") as jf:
                json.dump(meta_data, jf, ensure_ascii=False, indent=2)
            print(f"📦 キューJSONを保存しました: {json_path}")

        valid_tweets_to_send.append({"id": tweet_id, "text": text, "url": tweet_url, "count": len(parsed_items)})

    if valid_tweets_to_send:
        print(f"🚀 Discordに通知するツイート数: {len(valid_tweets_to_send)}")
        valid_tweets_to_send.reverse()
        for vt in valid_tweets_to_send:
            if webhook_url:
                send_to_discord(webhook_url, vt["text"], vt["url"], vt["count"])
        if newest_fetched_id:
            save_last_tweet_id(newest_fetched_id)
    else:
        print("ℹ️️ 新着で送信対象となるツイートはありませんでした。")

if __name__ == "__main__":
    main()
