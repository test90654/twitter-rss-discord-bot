import os
import subprocess
import json
import requests
from pathlib import Path

TARGET_USER = "mulanakiba_chuo"
BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "data" / "mulan_queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

# 必須のハッシュタグ
REQUIRED_HASHTAG = "#ムラ中買取"

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

def download_image(url, save_path):
    try:
        res = requests.get(url, timeout=10)
        if res.status_code == 200:
            with open(save_path, "wb") as f:
                f.write(res.content)
            return True
    except Exception as e:
        print(f"Failed to download image {url}: {e}")
    return False

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
                if isinstance(m, dict) and "url" in m:
                    image_urls.append(m.get("url"))

        if not image_urls:
            print(f"-> Tweet {tweet_id} has hashtag, but no images attached.")
            continue

        # 画像のダウンロードとメタデータ保存
        for idx, img_url in enumerate(image_urls):
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
            if download_image(img_url, img_path):
                meta_data = {
                    "tweet_id": tweet_id,
                    "text": text,
                    "image_file": img_filename,
                    "tweet_url": f"https://x.com/{TARGET_USER}/status/{tweet_id}"
                }
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta_data, f, ensure_ascii=False, indent=2)
                count += 1

    print(f"Successfully collected {count} new valid買取 images/metadata.")

if __name__ == "__main__":
    main()
