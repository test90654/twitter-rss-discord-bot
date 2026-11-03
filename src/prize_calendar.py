from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

import gspread
from google import genai
from google.genai import types
from pydantic import BaseModel
import requests

# プライズの入荷予定・入荷報告を、マンガ倉庫甘木店アミューズ（@amagi_amuse）のXのポストから集める。
#
# ・毎月の「YYYY年M月の新景品入荷予定一覧です！」のポスト: 添付の入荷予定表（画像）を Gemini で読み取り、
#   入荷日・メーカー・商品名を「入荷予定」タブに書く（同じポストは2回読まない）
# ・毎日の「【新プライズ情報】<商品名> 入荷いたしました！」のポスト: 本文の商品名を「入荷報告」タブに書く
#
# 特定のポストだけ読み直す:
#   python src/prize_calendar.py --tweet https://x.com/amagi_amuse/status/<ID>
# 画像の読み取りだけ試す（シートは触らない）:
#   GEMINI_API_KEY=... python src/prize_calendar.py --ocr-only 画像ファイル [年 月]

TARGET_USER = os.environ.get("CALENDAR_TARGET_USER", "amagi_amuse")
SCHEDULE_QUERY = "新景品入荷予定一覧"
ARRIVAL_HEADING = "【新プライズ情報】"
JST = timezone(timedelta(hours=9))

GCP_SA_KEY_ENV = os.environ.get("GCP_SA_KEY")
LOCAL_CREDENTIALS_FILE = r"C:\Users\chukyotokukai\Documents\youtube_to_sheets\credentials.json"

raw_id = os.environ.get("SPREADSHEET_ID_CALENDAR", "")
m_id = re.search(r"([a-zA-Z0-9-_]{25,})", raw_id)
SPREADSHEET_ID = m_id.group(1) if m_id else raw_id.strip()

DISCORD_WEBHOOK_URL = (
    os.environ.get("DISCORD_WEBHOOK_URL_CALENDAR")
    or os.environ.get("DISCORD_WEBHOOK_URL")
    or ""
)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# 環境変数 GEMINI_MODEL を設定すればそれを最優先で使う（figure-kaitori-bot の千葉鑑定団東金のOCRと同じ候補）
MODEL_CANDIDATES = [m for m in [
    os.environ.get("GEMINI_MODEL", "").strip(),
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
    "gemini-3.5-flash-lite",
] if m]
# 画像の読み取りに向かない・無料枠のないモデル（一覧から自動で足すときに除く）
_SKIP_MODEL_WORDS = ("tts", "image", "live", "audio", "embedding", "thinking", "computer-use", "robotics", "native")


SHEET_SCHEDULE = "入荷予定"
SHEET_ARRIVALS = "入荷報告"
HEADERS_SCHEDULE = ["年月", "入荷日", "メーカー", "商品名", "画像", "ポストURL", "読み取り日時"]
HEADERS_ARRIVALS = ["入荷日", "商品名", "ポストURL"]

# 入荷予定表のメーカーの略号
MAKER_CODES = {
    "BP": "バンダイスピリッツ", "SG": "セガ", "FR": "フリュー", "TA": "タイトー",
    "SS": "システムサービス", "SK": "エスケイジャパン", "AM": "アミューズ",
}


def normalize_name(name):
    name = unicodedata.normalize("NFKC", str(name))
    return re.sub(r"\s+", " ", name).strip()


def send_discord_notification(webhook_url, message):
    if not webhook_url:
        return
    try:
        res = requests.post(webhook_url, json={"content": message}, timeout=10)
        print("Discord通知を送信しました。" if res.status_code == 204 else f"Discord通知に失敗しました (Status: {res.status_code})")
    except Exception as e:
        print(f"Discord通信エラー: {e}")


# ============================================================
# Xのポストを取得する（twitter-cli）
# ============================================================
def check_twitter_secrets():
    """twitter-cli は TWITTER_AUTH_TOKEN と TWITTER_CT0 が両方あるときだけ使う（足りないとブラウザから探して失敗する）"""
    missing = [k for k in ("TWITTER_AUTH_TOKEN", "TWITTER_CT0") if not os.environ.get(k, "").strip()]
    if missing:
        print(f"::error::Secrets {', '.join(missing)} is empty")
        raise RuntimeError(
            f"XのCookieが設定されていません: {', '.join(missing)}。"
            "このリポジトリの Settings → Secrets and variables → Actions に登録してください"
            "（ブラウザで x.com にログインし、Cookie の auth_token と ct0 の値）。"
        )


def run_twitter_cli(args):
    check_twitter_secrets()
    result = subprocess.run(
        # 固定ポストも読めるようにしたラッパー経由で twitter-cli を動かす（入荷予定一覧は固定されていることが多い）
        [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "twitter_cli_runner.py"), *args, "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore", env=os.environ.copy(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"ポストを取得できませんでした: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout.strip() or "[]")
    if isinstance(data, dict):
        data = data.get("tweets") or data.get("data") or []
    if isinstance(data, dict):
        data = [data]
    return [t for t in data if isinstance(t, dict)]


RECENT_POSTS_MAX = 100  # 検索が使えないときに読む直近ポスト数
_recent_posts = None

def recent_posts():
    """アカウントの直近ポスト（1回の実行で1度だけ取得）"""
    global _recent_posts
    if _recent_posts is None:
        _recent_posts = run_twitter_cli(["user-posts", TARGET_USER, "--max", str(RECENT_POSTS_MAX)])
    return _recent_posts


def search_posts(query, max_count):
    try:
        return run_twitter_cli(["search", query, "--from", TARGET_USER, "--type", "Latest", "--max", str(max_count)])
    except RuntimeError as e:
        # X の検索は twitter-cli 側で失敗することがある（ClientTransaction の初期化エラーなど）。
        # その場合はアカウントの直近ポストを読み、本文で絞り込む
        print(f"検索に失敗したため直近 {RECENT_POSTS_MAX} 件のポストから「{query}」を探します: {str(e)[:120]}")
        return [p for p in recent_posts() if query in post_text(p)][:max_count]


def fetch_post(tweet_id):
    tweet_id = re.sub(r".*/status/(\d+).*", r"\1", str(tweet_id).strip())
    posts = run_twitter_cli(["tweet", tweet_id, "--max", "1"])
    return next((p for p in posts if str(p.get("id")) == tweet_id), posts[0] if posts else None)


def post_text(post):
    return post.get("text") or post.get("full_text") or ""


def post_url(post):
    return f"https://x.com/{TARGET_USER}/status/{post.get('id', '')}"


def post_time(post):
    created = post.get("createdAt") or post.get("created_at") or ""
    try:
        return parsedate_to_datetime(created).astimezone(JST)
    except Exception:
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(JST)
        except Exception:
            return None


def image_urls(post):
    urls = []
    for m in post.get("media") or []:
        url = m.get("url") or ""
        if m.get("type", "photo") == "photo" and url:
            # 元の解像度で取る
            urls.append(url.split("?")[0] + "?name=orig")
    return urls


def schedule_month(text, when):
    """本文の「2026年11月」から年月を決める。なければ投稿日の翌月（月末に翌月分が出るため）"""
    m = re.search(r"(\d{4})年\s*(\d{1,2})月", normalize_name(text))
    if m:
        return int(m.group(1)), int(m.group(2))
    if when is None:
        when = datetime.now(JST)
    y, mo = (when.year, when.month + 1) if when.day >= 20 else (when.year, when.month)
    return (y + 1, 1) if mo == 13 else (y, mo)


# ============================================================
# 入荷予定表（画像）を読み取る
# ============================================================
class ScheduleItem(BaseModel):
    day: int
    maker: str
    name: str


class ScheduleResult(BaseModel):
    items: list[ScheduleItem]


SCHEDULE_PROMPT = """これはゲームセンターのプライズ（景品）の入荷予定表です。
商品ごとのマスに、商品写真・入荷日（「6日」のような青い札）・メーカーの略号（BP、SG、FR、TA、SS などの札）・商品名（青い帯の白い文字）が書かれています。

すべてのマスについて、次を読み取ってください。
- day: 入荷日の数字（「6日」なら 6、「11・12日」のように複数なら最初の日）
- maker: メーカーの略号（札の文字のまま。読めなければ空文字）
- name: 商品名。帯に書かれた文字を、改行をスペースに置き換えて上から順につなげる。作品名・シリーズ名・キャラクター名をすべて含める

注意:
- 見えている文字だけを書く。隠れて読めない部分を推測で補わない
- 「イチオシ!!」などの飾りの文字は商品名に含めない
- 表のタイトルや注意書きは含めない
"""


def make_config():
    kwargs = dict(response_mime_type="application/json", response_schema=ScheduleResult, max_output_tokens=32768)
    try:
        return types.GenerateContentConfig(media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH, **kwargs)
    except Exception:
        return types.GenerateContentConfig(**kwargs)


class ModelUnavailable(Exception):
    pass


class QuotaExhausted(ModelUnavailable):
    """すべてのモデルで1日の無料枠を使い切った"""
    pass


_working_model = None      # 一度使えたモデルを覚えておく
_exhausted_models = set()  # この実行中に1日の上限に達したモデル（翌日まで使えない）
_dead_models = set()       # 存在しない・提供終了のモデル（この実行中は二度と試さない）
_discovered = False


def _model_version(name):
    m = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else 0.0


def _discover_models(client):
    """API のモデル一覧から、使える Flash 系のモデルを候補の後ろに足す（新しい版から順に）。
    一覧を取れなくても、決め打ちの候補だけで続ける"""
    global _discovered
    if _discovered:
        return
    _discovered = True
    try:
        names = []
        for mdl in client.models.list():
            name = (getattr(mdl, "name", "") or "").split("/")[-1]
            actions = getattr(mdl, "supported_actions", None) or []
            if not name.startswith("gemini-") or "flash" not in name:
                continue
            if actions and "generateContent" not in actions:
                continue
            if any(w in name for w in _SKIP_MODEL_WORDS):
                continue
            names.append(name)
    except Exception as e:
        print(f"  モデル一覧を取得できませんでした（決め打ちの候補で続けます）: {str(e)[:120]}")
        return
    available = set(names)
    # 決め打ちの候補のうち、一覧にないものは使えないとみなす（GEMINI_MODEL で指定したものは残す）
    pinned = os.environ.get("GEMINI_MODEL", "").strip()
    if available:
        for m in MODEL_CANDIDATES:
            if m not in available and m != pinned:
                _dead_models.add(m)
    # プレビュー版は無料枠がないことが多いので後回しにする
    extra = sorted((n for n in available if n not in MODEL_CANDIDATES),
                   key=lambda n: ("preview" in n or "exp" in n, "lite" in n, -_model_version(n), n))
    MODEL_CANDIDATES.extend(extra)
    usable = [m for m in MODEL_CANDIDATES if m not in _dead_models]
    print(f"  使うモデルの候補: {usable}")


def _is_daily_quota(msg):
    """1日あたりの上限（PerDay）に達したエラーか。待っても今日は回復しないので、別のモデルに切り替える"""
    return "RESOURCE_EXHAUSTED" in msg and ("PerDay" in msg or re.search(r"retry in \d+h", msg) is not None)


def _call_gemini(client, contents, config):
    """モデル候補を順に試す。一時的な混雑・分あたりの制限は待って再試行し、
    1日の上限に達したモデルは飛ばして次の候補へ切り替える（無料枠はモデルごとに別）"""
    global _working_model
    _discover_models(client)
    models = ([_working_model] if _working_model else []) + [m for m in MODEL_CANDIDATES if m != _working_model]
    models = [m for m in models if m not in _exhausted_models and m not in _dead_models]
    last_err = None
    for model in models:
        for attempt in range(5):
            try:
                resp = client.models.generate_content(model=model, contents=contents, config=config)
                _working_model = model
                return resp
            except Exception as e:
                msg = str(e)
                last_err = e
                if _is_daily_quota(msg):
                    print(f"  モデル {model} は今日の上限に達したため次の候補へ")
                    _exhausted_models.add(model)
                    break
                if any(s in msg for s in ["404", "NOT_FOUND", "not found", "is not supported"]):
                    print(f"  モデル {model} は使えないため以後は使いません: {msg[:120]}")
                    _dead_models.add(model)
                    if _working_model == model:
                        _working_model = None
                    break
                if any(s in msg for s in ["429", "500", "502", "503", "504", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "DEADLINE", "timed out", "overloaded"]):
                    wait = 5 * (2 ** attempt)
                    print(f"  Gemini混雑/制限中。{wait}秒後に再試行 ({attempt + 1}/5)")
                    time.sleep(wait)
                    continue
                raise
    # 使えるモデルがすべて今日の上限に達した（提供終了のモデルは数えない）
    alive = [m for m in MODEL_CANDIDATES if m not in _dead_models]
    if _exhausted_models and all(m in _exhausted_models for m in alive):
        raise QuotaExhausted(f"すべてのモデルで今日の上限に達しました: {sorted(_exhausted_models)}")
    raise ModelUnavailable(f"どのモデルでも解析できませんでした: {last_err}")




def parse_schedule_response(resp):
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, ScheduleResult):
        return parsed
    text = re.sub(r"^```(?:json)?|```$", "", (getattr(resp, "text", "") or "").strip()).strip()
    return ScheduleResult.model_validate(json.loads(text))


def download_image(url):
    res = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
    res.raise_for_status()
    return res.content, res.headers.get("Content-Type", "image/jpeg").split(";")[0]


def ocr_schedule(client, image_bytes, mime="image/jpeg"):
    part = types.Part.from_bytes(data=image_bytes, mime_type=mime)
    result = parse_schedule_response(_call_gemini(client, [part, SCHEDULE_PROMPT], make_config()))
    items = []
    for it in result.items:
        name = normalize_name(it.name).replace("イチオシ!!", "").strip()
        maker = normalize_name(it.maker).upper()
        if name and 1 <= it.day <= 31:
            items.append((it.day, maker, name))
    return items


# ============================================================
# 入荷報告（本文）を読み取る
# ============================================================
def parse_arrival(text):
    """「【新プライズ情報】\n\n<商品名>\n\n入荷いたしました！」から商品名を取り出す"""
    text = text.replace("\r", "")
    i = text.find(ARRIVAL_HEADING)
    if i < 0:
        return None
    body = text[i + len(ARRIVAL_HEADING):]
    m = re.search(r"入荷(?:いた|致)しました|入荷しました", body)
    if not m:
        return None
    name = normalize_name(body[:m.start()])
    name = re.sub(r"#\S+", "", name).strip()
    return name or None


# ============================================================
# スプレッドシート
# ============================================================
def init_worksheet(spreadsheet, sheet_name, headers):
    try:
        ws = spreadsheet.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=sheet_name, rows=2000, cols=100)

    try:
        ws.freeze(rows=0)
        ws.freeze(rows=1)
    except Exception:
        pass

    row1 = ws.row_values(1)
    if not row1 or row1[:len(headers)] != headers:
        end_col_letter = chr(ord('A') + len(headers) - 1)
        ws.update(values=[headers], range_name=f"A1:{end_col_letter}1")
        try:
            ws.format(f"A1:{end_col_letter}1", {
                "backgroundColor": {"red": 0.88, "green": 0.91, "blue": 0.95},
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER"
            })
        except Exception:
            pass

    return ws


def open_spreadsheet():
    if GCP_SA_KEY_ENV and GCP_SA_KEY_ENV.strip():
        print("GitHub Secrets から認証情報を読み込みます。")
        info = json.loads(GCP_SA_KEY_ENV)
        gc = gspread.service_account_from_dict(info, http_client=gspread.BackOffHTTPClient)
    elif os.path.exists(LOCAL_CREDENTIALS_FILE):
        print("ローカル認証ファイルから読み込みます。")
        gc = gspread.service_account(filename=LOCAL_CREDENTIALS_FILE, http_client=gspread.BackOffHTTPClient)
    else:
        raise ValueError("認証情報が見つかりません。Secretsの GCP_SA_KEY を確認してください。")
    return gc.open_by_key(SPREADSHEET_ID)




def recorded_urls(ws, col):
    return {row[col] for row in ws.get_all_values()[1:] if len(row) > col and row[col]}


def record_schedule(client, ws, post, done):
    url = post_url(post)
    if url in done:
        return 0
    when = post_time(post)
    year, month = schedule_month(post_text(post), when)
    imgs = image_urls(post)
    print(f"[入荷予定] {year}年{month}月 {url}（画像 {len(imgs)}枚）")
    rows = []
    for n, img_url in enumerate(imgs, start=1):
        data, mime = download_image(img_url)
        items = ocr_schedule(client, data, mime if mime.startswith("image/") else "image/jpeg")
        print(f"  画像{n}: {len(items)}件（モデル: {_working_model}）")
        for day, maker, name in items:
            rows.append([f"{year}-{month:02d}", f"{year}-{month:02d}-{day:02d}", MAKER_CODES.get(maker, maker), name,
                         n, url, datetime.now(JST).strftime("%Y-%m-%d %H:%M")])
    if not rows:
        print("  商品を読み取れませんでした（入荷予定表ではない画像の可能性があります）")
        return 0
    rows.sort(key=lambda r: (r[1], r[4]))
    ws.append_rows(rows, value_input_option="RAW")
    done.add(url)
    send_discord_notification(
        DISCORD_WEBHOOK_URL,
        f"🗓️ **【プライズ入荷予定】{year}年{month}月** を {len(rows)}件 読み取りました\n{url}",
    )
    return len(rows)


def record_arrivals(ws, posts, done):
    rows = []
    for post in sorted(posts, key=lambda p: post_time(p) or datetime.min.replace(tzinfo=JST)):
        url = post_url(post)
        if url in done:
            continue
        name = parse_arrival(post_text(post))
        when = post_time(post)
        if not name or when is None:
            continue
        rows.append([when.strftime("%Y-%m-%d"), name, url])
        done.add(url)
    if rows:
        ws.append_rows(rows, value_input_option="RAW")
    print(f"[入荷報告] {len(rows)}件を追加しました")
    return len(rows)


def main():
    if "--ocr-only" in sys.argv:
        args = sys.argv[sys.argv.index("--ocr-only") + 1:]
        with open(args[0], "rb") as f:
            data = f.read()
        client = genai.Client(api_key=GEMINI_API_KEY)
        for day, maker, name in ocr_schedule(client, data, "image/png" if args[0].lower().endswith(".png") else "image/jpeg"):
            print(f"{day:>2}日 {maker:<3} {name}")
        return

    if not SPREADSHEET_ID:
        raise ValueError("スプレッドシートIDが未設定です。Secretsの SPREADSHEET_ID_CALENDAR を設定してください。")
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY が設定されていません。")

    ss = open_spreadsheet()
    ws_schedule = init_worksheet(ss, SHEET_SCHEDULE, HEADERS_SCHEDULE)
    ws_arrivals = init_worksheet(ss, SHEET_ARRIVALS, HEADERS_ARRIVALS)
    client = genai.Client(api_key=GEMINI_API_KEY)

    done_schedule = recorded_urls(ws_schedule, HEADERS_SCHEDULE.index("ポストURL"))
    if "--tweet" in sys.argv:
        post = fetch_post(sys.argv[sys.argv.index("--tweet") + 1])
        if post is None:
            raise RuntimeError("指定のポストを取得できませんでした。")
        done_schedule.discard(post_url(post))
        record_schedule(client, ws_schedule, post, done_schedule)
        return

    try:
        schedule_posts = [p for p in search_posts(SCHEDULE_QUERY, 5) if SCHEDULE_QUERY in post_text(p) and image_urls(p)]
        if not schedule_posts and _recent_posts is not None:
            # 見つからないとき、どこまで遡って読めたかと、似たポストを出しておく
            times = [t for t in (post_time(p) for p in _recent_posts) if t]
            oldest = min(times).strftime("%Y-%m-%d") if times else "不明"
            print(f"[入荷予定] 直近 {len(_recent_posts)} 件（{oldest} 以降）に「{SCHEDULE_QUERY}」の画像付きポストはありませんでした")
            for p in _recent_posts:
                if "入荷予定" in post_text(p):
                    print(f"  似たポスト: {post_url(p)} 画像{len(image_urls(p))}枚 / {post_text(p)[:60]!r}")
        for post in schedule_posts:
            record_schedule(client, ws_schedule, post, done_schedule)
    except QuotaExhausted as e:
        # 読めなかったポストは記録していないので、次回の実行で読み直す
        print(f"::warning::Gemini daily quota exhausted; schedule will be retried next run: {e}")

    done_arrivals = recorded_urls(ws_arrivals, HEADERS_ARRIVALS.index("ポストURL"))
    record_arrivals(ws_arrivals, search_posts(ARRIVAL_HEADING, 40), done_arrivals)


if __name__ == "__main__":
    main()
