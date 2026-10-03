import os
import subprocess
import json
import requests
from pathlib import Path

TARGET_USER = "mulanakiba_chuo"
BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "data" / "mulan_queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

def fetch_tweets():
    print(f"Fetching tweets for @{TARGET_USER} (Collector Test)...")
    env = os.environ.copy()
    result = subprocess.run(
        ["python", "-m", "twitter_cli.cli", "user-posts", TARGET_USER, "--max", "5", "--json"],
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

def download_image(url, save_path):
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            with open(save_path, "wb") as f:
                f.write(res.content)
            return True
    except Exception as e:
        print(f"Failed to download image {url}: {e}")
    return false

def main():
    tweets = fetch_tweets()
    if not tweets:
        print("No tweets fetched.")
        return

    count = 0
    for tweet in tweets:
        if not isinstance(tweet, dict):
            continue
            
        tweet_id = str(tweet.get("id", tweet.get("id_str", "")))
        text = tweet.get("text", tweet.get("full_text", ""))
        
        # メディア（画像）のURLを抽出する処理（twitter_cliの出力構造に合わせて調整）
        # 一般的なentities/media構造またはextended_entitiesを探索
        image_urls = []
        
        # 例: entities -> media から取得するパターン
        entities = tweet.get("entities", {})
        media_list = entities.get("media", [])
        for m in media_list:
            if m.get("type") == "photo":
                image_urls.append(m.get("media_url_https") or m.get("media_url"))
                
        # もし上位階層に media_urls や photo があればそれも拾う
        if not image_urls and "media" in tweet:
            for m in tweet.get("media", []):
                if isinstance(m, dict) and "url" in m:
                    image_urls.append(m.get("url"))

        if not image_urls:
            continue

        # 既に処理済み（フォルダに存在）でなければ保存
        for idx, img_url in enumerate(image_urls):
            file_extension = img_url.split("?")[0].split(".")[-1]
            if file_extension not in ["jpg", "jpeg", "png"]:
                file_extension = "jpg"
                
            img_filename = f"{tweet_id}_{idx+1}.{file_extension}"
            img_path = QUEUE_DIR / img_filename
            meta_path = QUEUE_DIR / f"{tweet_id}_{idx+1}.json"
            
            if img_path.exists():
                continue # すでに取得済みならスキップ
                
            print(f"Downloading image for tweet {tweet_id}...")
            if download_image(img_url, img_path):
                # メタデータも一緒に保存
                meta_data = {
                    "tweet_id": tweet_id,
                    "text": text,
                    "image_file": img_filename,
                    "tweet_url": f"https://x.com/{TARGET_USER}/status/{tweet_id}"
                }
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta_data, f, ensure_ascii=False, indent=2)
                count += 1

    print(f"Successfully collected {count} new images/metadata.")

if __name__ == "__main__":
    main()
