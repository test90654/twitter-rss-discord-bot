import os
import io
import re
import subprocess
import json
import unicodedata
import requests
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import time

from PIL import Image
from pydantic import BaseModel
from google import genai
from google.genai import types

TARGET_USER = "mulanakiba_chuo"
BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "data" / "mulan_queue"
QUEUE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = BASE_DIR.parent / "last_tweet_id_mulanakiba_chuo.txt"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 使うモデル（上から順に試し、存在しない・使えないモデルなら次へ）
# 環境変数 GEMINI_MODEL を設定すればそれを最優先で使う
MODEL_CANDIDATES = [m for m in [
    os.environ.get("GEMINI_MODEL", "").strip(),
    "gemini-3.8-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
] if m]

# 縦長画像を分割するときの設定
STRIP_ASPECT = 1.2      # 1枚の切り出しの高さ = 幅 × この値
STRIP_OVERLAP = 0.2     # 切り出し同士の重なり（行が境目で切れて落ちるのを防ぐ）
SPLIT_THRESHOLD = 1.6   # 高さが 幅×この値 を超えたら分割する

# 必須のハッシュタグ
REQUIRED_HASHTAG = "#ムラ中買取"


# ============================================================
# 状態管理・ツイート取得（元のまま）
# ============================================================
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


# ============================================================
# 画像の取得（原寸で取る）
# ============================================================
def to_original_size_url(url):
    """
    X(Twitter)の画像URLはそのままだと縮小版が返ることがある。
    name=orig を付けて原寸画像を取得する（小さい文字の読み落とし対策）。
    """
    if not url or "pbs.twimg.com/media" not in url:
        return url
    p = urlparse(url)
    q = parse_qs(p.query)
    path = p.path
    # 例: /media/XXXX.jpg → /media/XXXX + format=jpg
    m = re.match(r"^(.*?/media/[^./]+)\.(jpg|jpeg|png|webp)$", path)
    if m:
        path = m.group(1)
        q["format"] = [m.group(2)]
    q["name"] = ["orig"]
    return urlunparse(p._replace(path=path, query=urlencode({k: v[0] for k, v in q.items()})))

def download_image(url, save_path):
    for target in [to_original_size_url(url), url]:  # 原寸が取れなければ元のURLで再挑戦
        try:
            res = requests.get(target, timeout=20)
            if res.status_code == 200 and res.content:
                with open(save_path, "wb") as f:
                    f.write(res.content)
                return res.content
        except Exception as e:
            print(f"Failed to download image {target}: {e}")
    return None


# ============================================================
# Gemini OCR
# ============================================================
class OcrItem(BaseModel):
    category: str       # 見出し（赤文字などのシリーズ名・カテゴリー名）。無ければ空
    name: str           # 商品名（キャラ名・バージョン・状態なども含める）
    price: str          # 買取価格（数字のみ。数字でない表記はそのまま）
    update_date: str    # 更新日（行ごとに無ければ空）
    model_number: str   # 型番・JAN（無ければ空）

class OcrResult(BaseModel):
    update_date: str    # 画像全体の更新日（無ければ空）
    items: list[OcrItem]


BASE_RULES = """
あなたは買取表の画像から商品データを1行も漏らさず書き起こすデータ入力担当です。

【最重要】
- 表に載っている商品は「すべて」出力すること。要約・省略・「他○件」などのまとめは絶対にしない。
- 似た商品・同じシリーズの商品が続いても、1行ずつ別々に出力する。
- 型番が同じでも、キャラクター違い・バージョン違い・状態違い（新品/開封品など）は別の行にする。
- 読み取りにくい文字も推測して埋め、行自体は必ず出力する（価格や型番が読めなければその欄だけ空にする）。
- 表が複数列（左右2段など）に分かれている場合は、左の列を上から下まで読み、次に右の列を読む。

【各欄のルール】
- category: その商品が属する見出し（赤文字・色付き帯・太字などのシリーズ名/カテゴリー名）。直前の見出しを引き継ぐ。無ければ空文字。
- name: 商品名。category と同じ文字は繰り返さなくてよい。
- price: 買取価格。「¥」「円」「,」は除き数字だけ（例: 12000）。「要相談」など数字でない場合はその文字列のまま。
- update_date: 画像内に書かれた更新日。行ごとに無ければ空文字（全体の更新日は update_date に別途入れる）。
- model_number: 型番やJANコード。無ければ空文字。

【除外するもの】
注意事項、営業時間、住所、宣伝文句、更新アナウンス、ロゴなど商品ではない文言。
"""


def _detect_mime(img_bytes):
    try:
        fmt = (Image.open(io.BytesIO(img_bytes)).format or "JPEG").lower()
        return {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(fmt, "image/jpeg")
    except Exception:
        return "image/jpeg"

def _img_part(img_bytes):
    return types.Part.from_bytes(data=img_bytes, mime_type=_detect_mime(img_bytes))

def _make_config():
    kwargs = dict(
        response_mime_type="application/json",
        response_schema=OcrResult,
        max_output_tokens=32768,
    )
    # 画像を高解像度で読ませる（対応していないSDKバージョンなら付けない）
    try:
        return types.GenerateContentConfig(media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH, **kwargs)
    except Exception:
        return types.GenerateContentConfig(**kwargs)


class ModelUnavailable(Exception):
    pass

_working_model = None  # 一度使えたモデルを覚えておく

def _call_gemini(client, contents):
    """モデル候補を順に試し、混雑・レート制限は待って再試行する"""
    global _working_model
    models = [_working_model] if _working_model else MODEL_CANDIDATES
    last_err = None
    for model in models:
        for attempt in range(5):
            try:
                resp = client.models.generate_content(model=model, contents=contents, config=_make_config())
                _working_model = model
                return resp
            except Exception as e:
                msg = str(e)
                last_err = e
                if any(s in msg for s in ["404", "NOT_FOUND", "not found", "is not supported"]):
                    print(f"  ⚠️ モデル {model} は使えないため次の候補へ: {msg[:120]}")
                    break
                if any(s in msg for s in ["429", "500", "502", "503", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE", "timed out", "overloaded"]):
                    wait = 5 * (2 ** attempt)
                    print(f"  ⚠️ Gemini混雑/制限中。{wait}秒後に再試行 ({attempt+1}/5)")
                    time.sleep(wait)
                    continue
                raise
    raise ModelUnavailable(f"どのモデルでも解析できませんでした: {last_err}")

def _parse_response(resp):
    """構造化出力をパース（parsed が無いときは text を JSON として読む）"""
    try:
        cands = getattr(resp, "candidates", None) or []
        if cands and str(getattr(cands[0], "finish_reason", "")).endswith("MAX_TOKENS"):
            print("  ⚠️ 出力が上限で途中終了しました（この部分は取りこぼしの可能性あり）")
    except Exception:
        pass

    result = getattr(resp, "parsed", None)
    if isinstance(result, OcrResult):
        return result
    text = (getattr(resp, "text", "") or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text).strip()
    try:
        return OcrResult.model_validate(json.loads(text))
    except Exception as e:
        print(f"  ⚠️ OCR結果のJSON解析に失敗: {e}")
        return OcrResult(update_date="", items=[])


def split_into_strips(img_bytes):
    """
    縦長の買取表を、重なりを持たせた横長の帯に分割する。
    1枚の画像に商品が大量にあると、モデルが途中を読み飛ばしやすいため。
    """
    img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = img.size
    if h <= w * SPLIT_THRESHOLD:
        return []
    strip_h = int(w * STRIP_ASPECT)
    step = int(strip_h * (1 - STRIP_OVERLAP))
    strips = []
    top = 0
    while True:
        bottom = min(top + strip_h, h)
        buf = io.BytesIO()
        img.crop((0, top, w, bottom)).save(buf, format="JPEG", quality=95)
        strips.append(buf.getvalue())
        if bottom >= h:
            break
        top += step
    return strips


def _norm(s):
    s = unicodedata.normalize("NFKC", str(s or ""))
    return re.sub(r"\s+", "", s).lower()

def _dedupe_key(it):
    return (_norm(it.category), _norm(it.name), _norm(it.price), _norm(it.model_number))

def _merge(base, extra):
    """重複（分割の重なり部分など）を除いて追加する"""
    seen = {_dedupe_key(it) for it in base}
    added = 0
    for it in extra:
        k = _dedupe_key(it)
        if k not in seen and it.name.strip():
            base.append(it)
            seen.add(k)
            added += 1
    return added


def analyze_image_with_gemini(image_bytes):
    """
    買取表画像から商品リストを抽出する。
      1. 縦長なら帯に分割し、帯ごとに抽出（全体画像も一緒に渡して見出しや更新日を判断させる）
      2. 最後に全体画像と抽出結果を見比べさせ、漏れている商品だけを追加で抽出
    戻り値: (items: list[dict], 使ったモデル名)
    """
    if not GEMINI_API_KEY:
        print("❌ GEMINI_API_KEY が設定されていません。")
        return [], None

    client = genai.Client(api_key=GEMINI_API_KEY)
    full_part = _img_part(image_bytes)
    items: list[OcrItem] = []
    overall_date = ""

    try:
        strips = split_into_strips(image_bytes)
        if not strips:
            print("  - 全体を一括で解析")
            res = _parse_response(_call_gemini(client, [full_part, BASE_RULES + "\n上の買取表画像から商品をすべて抽出してください。"]))
            overall_date = res.update_date
            _merge(items, res.items)
        else:
            print(f"  - 縦長のため {len(strips)} 分割して解析")
            for i, strip in enumerate(strips, 1):
                prompt = BASE_RULES + f"""
1枚目は買取表の全体画像、2枚目はその一部を切り出した画像（{i}/{len(strips)}枚目、上から順）です。
【2枚目に写っている商品行だけ】をすべて抽出してください。
- 見出し（category）が2枚目の範囲より上にある場合は、1枚目の全体画像から判断して付けること。
- 更新日も1枚目の全体画像から読み取ること。
- 2枚目の上端・下端で文字が半分切れている行は、1枚目の全体画像で確認して出力してよい（重複は後で除くので漏らさないことを優先）。
"""
                res = _parse_response(_call_gemini(client, [full_part, _img_part(strip), prompt]))
                overall_date = overall_date or res.update_date
                added = _merge(items, res.items)
                print(f"    分割 {i}/{len(strips)}: {len(res.items)} 件読み取り（新規 {added} 件）")

        # --- 漏れチェック（見直しパス） ---
        listed = "\n".join(f"- [{it.category}] {it.name} / {it.price} / {it.model_number}" for it in items)
        check_prompt = BASE_RULES + f"""
以下は、上の買取表画像からすでに抽出済みの商品リストです（{len(items)}件）。
画像の上から下まで1行ずつ見比べ、【このリストに載っていない商品だけ】を抽出してください。
すべて載っている場合は items を空配列にしてください。リストにある商品を再度出力しないこと。

{listed}
"""
        res = _parse_response(_call_gemini(client, [full_part, check_prompt]))
        added = _merge(items, res.items)
        print(f"  - 見直しパス: 漏れ {added} 件を追加")

    except Exception as e:
        print(f"❌ Gemini解析エラー: {e}")

    # ダッシュボード用の形式に変換（シリーズ名・カテゴリー名を商品名の先頭に付ける）
    out = []
    for it in items:
        cat, name = it.category.strip(), it.name.strip()
        full_name = name if (not cat or name.startswith(cat)) else f"{cat} {name}"
        out.append({
            "name": full_name,
            "price": it.price.replace("¥", "").replace("円", "").replace(",", "").strip(),
            "update_date": (it.update_date or overall_date).strip(),
            "model_number": it.model_number.strip(),
        })
    return out, _working_model


# ============================================================
# Discord通知・メイン処理
# ============================================================
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
                print(f"🤖 GeminiでOCR解析を実行中 ({img_filename})...")
                parsed_items, used_model = analyze_image_with_gemini(img_bytes)
                print(f"✨ 抽出結果: {len(parsed_items)} 件（モデル: {used_model}）")

                tweet_total_parsed += len(parsed_items)
                has_new_media = True

                tweet_url = f"https://x.com/{TARGET_USER}/status/{tweet_id}"
                meta_data = {
                    "tweet_id": f"{tweet_id}_{idx+1}",
                    "original_tweet_id": tweet_id,
                    "text": text,
                    "image_file": img_filename,
                    "tweet_url": tweet_url,
                    "ocr_model": used_model,
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
