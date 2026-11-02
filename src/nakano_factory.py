from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
import subprocess
import sys
import unicodedata

import gspread
from gspread.utils import rowcol_to_a1
import requests

# ナカノファクトリー（中野ブロードウェイ）のプライズ買取価格
# 毎日Xに投稿される「【本日の商品】」の一覧（✨商品名 → 次の行に価格）を読み取り、
# ほかの店舗と同じ「現在一覧」「履歴ログ」の形式でスプレッドシートに記録する。
# Xの読み取りはほかの監視と同じ twitter-cli（TWITTER_AUTH_TOKEN / TWITTER_CT0）を使う。
# 店舗シートの形式は figure-kaitori-bot（ドラゴンボーン・千葉鑑定団など）と同じで、sheet-matcher がそのまま読める。
#
# ポストの文章だけで読み取りを確かめる（スプレッドシートは触らない）:
#   python src/nakano_factory.py --parse-only ポストを保存したファイル.txt

SHOP_LABEL = "ナカノファクトリー"
TARGET_USER = os.environ.get("NAKANO_TARGET_USER", "nakanofactory")
ITEMS_HEADING = "【本日の商品】"
JST = timezone(timedelta(hours=9))

# --- 認証 & 設定 (GitHub Secrets / ローカルファイル両対応) ---
GCP_SA_KEY_ENV = os.environ.get("GCP_SA_KEY")
LOCAL_CREDENTIALS_FILE = r"C:\Users\chukyotokukai\Documents\youtube_to_sheets\credentials.json"

raw_id = os.environ.get("SPREADSHEET_ID_NAKANO", "") or "1PxxpiArkgRAo-Y7fsmpafSHabx66ShE4VR0aSOvsveU"
m_id = re.search(r"([a-zA-Z0-9-_]{25,})", raw_id)
SPREADSHEET_ID = m_id.group(1) if m_id else raw_id.strip()

# 専用Webhookが無ければ共通のWebhookに通知
DISCORD_WEBHOOK_URL = (
    os.environ.get("DISCORD_WEBHOOK_URL_NAKANO")
    or os.environ.get("DISCORD_WEBHOOK_URL")
    or ""
)

SHEET_CURRENT = "現在一覧"
SHEET_HISTORY = "履歴ログ"

HEADERS_CURRENT = [
    "ID", "商品名", "現在価格", "前日比", "最高価格", "最低価格",
    "初回確認日", "最終確認日", "状態"
]
HEADERS_HISTORY = ["ID", "商品名"]


def normalize_name(name):
    # 半角カナ→全角、全角英数→半角などをそろえ、連続空白を1つの半角スペースにまとめる
    name = unicodedata.normalize("NFKC", str(name))
    return re.sub(r"\s+", " ", name).strip()


def send_discord_notification(webhook_url, message):
    if not webhook_url:
        return
    payload = {"content": message}
    try:
        res = requests.post(webhook_url, json=payload, timeout=10)
        if res.status_code == 204:
            print("Discord通知を送信しました。")
        else:
            print(f"Discord通知に失敗しました (Status: {res.status_code})")
    except Exception as e:
        print(f"Discord通信エラー: {e}")


# ============================================================
# Xのポストを取得する
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


def fetch_tweets(max_count=20):
    """twitter-cli で最新のポストを取得する（長いポストも全文が返る）"""
    check_twitter_secrets()
    print(f"@{TARGET_USER} のポストを取得します...")
    result = subprocess.run(
        [sys.executable, "-m", "twitter_cli.cli", "user-posts", TARGET_USER, "--max", str(max_count), "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore", env=os.environ.copy(),
    )
    if result.returncode != 0:
        raise RuntimeError(f"ポストを取得できませんでした: {result.stderr.strip()[:300]}")
    data = json.loads(result.stdout.strip() or "[]")
    if isinstance(data, dict):
        data = data.get("tweets") or data.get("data") or []
    return [t for t in data if isinstance(t, dict)]


def tweet_time(tweet):
    """ポストの投稿日時（日本時間）。分からなければ None"""
    created = tweet.get("createdAt") or tweet.get("created_at") or ""
    if not created:
        return None
    try:
        return parsedate_to_datetime(created).astimezone(JST)
    except Exception:
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(JST)
        except Exception:
            return None


def find_today_post(tweets, today):
    """今日（日本時間）の「本日の商品」のポストのうち、いちばん新しいもの"""
    best = None
    for t in tweets:
        text = t.get("text") or t.get("full_text") or ""
        when = tweet_time(t)
        if ITEMS_HEADING not in text or when is None or when.date() != today:
            continue
        if best is None or when > best[0]:
            best = (when, t)
    return best[1] if best else None


# ============================================================
# ポストの本文から商品名と価格を取り出す
# ============================================================
PRICE_RE = re.compile(r"^(各)?\s*([0-9][0-9,]*)\s*円")
DATE_PREFIX_RE = re.compile(r"^\d{1,2}/\d{1,2}\s*")
SET_RE = re.compile(r"\s*(\d+)\s*SET\s*$", re.IGNORECASE)


def clean_item_name(name):
    name = normalize_name(name).lstrip("✨").strip()
    name = DATE_PREFIX_RE.sub("", name)      # 先頭の発売日（10/30 など）
    name = name.rstrip("/／ ").strip()       # 末尾の「/」「//」
    return name


def split_set(name, price, each):
    """「A＆B 2SET」はセット価格を個数で割って1体ずつにする（「各」付きは1体の価格）。
    最後の語が「ジャイロ＆ジョニー」のように＆でつながっているときだけ分ける。分けられなければ None"""
    m = SET_RE.search(name)
    count = int(m.group(1)) if m else None
    base = SET_RE.sub("", name).strip() if m else name
    if not m and not each:
        return [(name, price)]
    head, _, last = base.rpartition(" ")
    parts = [p.strip() for p in re.split(r"[＆&]", last) if p.strip()]
    if len(parts) < 2 or (count and len(parts) != count):
        return None
    unit = price if each else round(price / len(parts))
    return [((head + " " + p).strip(), unit) for p in parts]


def parse_items(text):
    """「【本日の商品】」から次の「【…】」までを読み、{商品名: 価格} と 分けられなかったセット品 を返す。
    セット価格を割った単価より、単品として載っている価格を優先する"""
    start = text.find(ITEMS_HEADING)
    if start < 0:
        return {}, []
    body = text[start + len(ITEMS_HEADING):]
    nxt = re.search(r"\n\s*【", body)
    if nxt:
        body = body[:nxt.start()]

    singles, from_sets, skipped = {}, {}, []
    name = None        # 価格を待っている商品名
    parent_head = ""   # 直前の✨商品の、最後の語（キャラ名）を除いた部分。✨の付かない内訳の行に補う
    sub_item = False
    for raw in body.splitlines():
        line = normalize_name(raw)
        if not line or line.startswith("※"):
            continue
        m = PRICE_RE.match(line)
        if m:
            if not name:
                continue
            price = int(m.group(2).replace(",", ""))
            each = bool(m.group(1))
            if sub_item and re.search(r"[＆&]", name.rpartition(" ")[2]):
                each = True  # 「ゴン＆ヒソカ 1,200円」は1体ずつの価格
            pairs = split_set(name, price, each)
            if pairs is None:
                skipped.append(f"{name}: {price:,}円")
            else:
                target = from_sets if (len(pairs) > 1 and not each) else singles
                for n, p in pairs:
                    target.setdefault(n, p)
            name = None
            continue
        if line.startswith("✨"):
            name = clean_item_name(line)
            parent_head = SET_RE.sub("", name).rpartition(" ")[0]
            sub_item = False
        else:
            # ✨の付かない行は直前の商品の内訳（例: G.I.編のあとの「ゴン＆ヒソカ」「キルア」）
            name = (parent_head + " " + clean_item_name(line)).strip()
            sub_item = True
    items = dict(singles)
    for n, p in from_sets.items():
        items.setdefault(n, p)
    return items, skipped


# ============================================================
# スプレッドシートへの書き込み（figure-kaitori-bot の千葉鑑定団東金と同じ処理）
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


def safe_int(value, default=0):
    try:
        if value in ["", None, "削除"]:
            return default
        return int(str(value).replace(",", ""))
    except Exception:
        return default


def get_next_id(current_values):
    ids = []
    for row in current_values[1:]:
        if row and row[0]:
            try:
                ids.append(int(row[0]))
            except Exception:
                pass
    return max(ids) + 1 if ids else 1


def ensure_today_column(history_sheet, today):
    headers = history_sheet.row_values(1)
    if not headers:
        headers = ["ID", "商品名"]
        history_sheet.update(values=[headers], range_name="A1:B1")

    if today not in headers:
        next_col = len(headers) + 1
        if next_col > history_sheet.col_count:
            history_sheet.resize(cols=next_col + 30)

        col_a1 = rowcol_to_a1(1, next_col)
        history_sheet.update(values=[[today]], range_name=col_a1)
        headers.append(today)

        try:
            history_sheet.format(col_a1, {
                "backgroundColor": {"red": 0.88, "green": 0.91, "blue": 0.95},
                "textFormat": {"bold": True},
                "horizontalAlignment": "CENTER"
            })
        except Exception:
            pass

    return headers.index(today) + 1


def get_previous_price_from_history(history_row, date_col):
    for col in range(date_col - 1, 2, -1):
        if col - 1 < len(history_row):
            value = history_row[col - 1]
            prev_price = safe_int(value, None)
            if prev_price is not None:
                return prev_price
    return None


def write_items(current_sheet, history_sheet, items, today):
    """取得した {商品名: 価格} を現在一覧・履歴ログに反映する（千葉鑑定団船橋と同じ処理）"""
    date_col = ensure_today_column(history_sheet, today)

    current_values = current_sheet.get_all_values()
    history_values = history_sheet.get_all_values()

    current_map = {}
    for row_num, row in enumerate(current_values[1:], start=2):
        if len(row) >= 2 and row[1]:
            current_map[row[1]] = {"row_num": row_num, "id": row[0], "row": row}

    history_map = {}
    for row_num, row in enumerate(history_values[1:], start=2):
        if len(row) >= 2 and row[1]:
            history_map[row[1]] = {"row_num": row_num, "id": row[0], "row": row}

    next_id = get_next_id(current_values)

    current_updates = []
    history_updates = []
    color_requests = []
    new_current_rows = []
    new_history_rows = []
    price_change_messages = []

    fetched_names = set(items.keys())
    new_count = 0
    update_count = 0
    deleted_count = 0
    price_changed_count = 0

    def row_color(row_num, rgb):
        return {
            "repeatCell": {
                "range": {
                    "sheetId": current_sheet.id,
                    "startRowIndex": row_num - 1,
                    "endRowIndex": row_num,
                    "startColumnIndex": 0,
                    "endColumnIndex": 9
                },
                "cell": {"userEnteredFormat": {"backgroundColor": rgb}},
                "fields": "userEnteredFormat.backgroundColor"
            }
        }

    for name, price in items.items():
        if name in current_map:
            info = current_map[name]
            row_num = info["row_num"]
            row = info["row"]
            item_id = row[0]

            old_high = safe_int(row[4], price) if len(row) > 4 else price
            old_low = safe_int(row[5], price) if len(row) > 5 else price
            first_seen = row[6] if len(row) > 6 and row[6] else today

            if name in history_map:
                history_row_num = history_map[name]["row_num"]
                previous_price = get_previous_price_from_history(history_map[name]["row"], date_col)
            else:
                history_row_num = len(history_values) + len(new_history_rows) + 1
                previous_price = None
                new_history_rows.append([item_id, name])

            price_diff = 0 if previous_price is None else price - previous_price
            if price_diff != 0:
                price_changed_count += 1
                diff_sign = f"+{price_diff:,}" if price_diff > 0 else f"{price_diff:,}"
                price_change_messages.append(
                    f"📦 **【{SHOP_LABEL}】価格変動通知**\n"
                    f"【商品名】{name}\n"
                    f"【新価格】{price:,}円 (前日比: {diff_sign}円)\n"
                    f"----------------"
                )

            color_requests.append(row_color(row_num, {"red": 1.0, "green": 1.0, "blue": 1.0}))
            current_updates.append({
                "range": f"C{row_num}:I{row_num}",
                "values": [[price, price_diff, max(old_high, price), min(old_low, price), first_seen, today, "掲載中"]]
            })
            history_updates.append({"range": rowcol_to_a1(history_row_num, date_col), "values": [[price]]})
            update_count += 1

        else:
            item_id = next_id
            next_id += 1
            new_count += 1
            new_current_rows.append([item_id, name, price, 0, price, price, today, today, "掲載中"])
            new_history_rows.append([item_id, name])
            history_row_num = len(history_values) + len(new_history_rows)
            history_updates.append({"range": rowcol_to_a1(history_row_num, date_col), "values": [[price]]})

    for name, info in current_map.items():
        if name not in fetched_names:
            row_num = info["row_num"]
            row = info["row"]
            status = row[8] if len(row) > 8 else ""
            if status != "削除":
                deleted_count += 1
                current_updates.append({"range": f"H{row_num}:I{row_num}", "values": [[today, "削除"]]})
            color_requests.append(row_color(row_num, {"red": 0.95, "green": 0.85, "blue": 0.85}))
            if name in history_map:
                history_updates.append({
                    "range": rowcol_to_a1(history_map[name]["row_num"], date_col),
                    "values": [["削除"]]
                })

    # 商品名はXのポストの文字列なので、数式などとして解釈されないよう RAW で追加する
    if new_current_rows:
        current_sheet.append_rows(new_current_rows, value_input_option="RAW")
    if new_history_rows:
        history_sheet.append_rows(new_history_rows, value_input_option="RAW")
    if current_updates:
        current_sheet.batch_update(current_updates, value_input_option="USER_ENTERED")
    if history_updates:
        history_sheet.batch_update(history_updates, value_input_option="USER_ENTERED")
    if color_requests:
        current_sheet.spreadsheet.batch_update({"requests": color_requests})

    print("書き込み完了")
    print(f"新規商品: {new_count}件")
    print(f"既存更新: {update_count}件")
    print(f"価格変更: {price_changed_count}件")
    print(f"削除判定: {deleted_count}件")
    # GitHub の実行結果画面に要約を出す (ASCII のみ)
    print(
        f"::notice::fetched={len(items)} new={new_count} updated={update_count} "
        f"price_changed={price_changed_count} removed={deleted_count}"
    )

    if price_change_messages:
        chunk = []
        current_length = 0
        for msg in price_change_messages:
            if current_length + len(msg) + 1 > 1900:
                send_discord_notification(DISCORD_WEBHOOK_URL, "\n".join(chunk))
                chunk = []
                current_length = 0
            chunk.append(msg)
            current_length += len(msg) + 1
        if chunk:
            send_discord_notification(DISCORD_WEBHOOK_URL, "\n".join(chunk))


def main():
    if "--parse-only" in sys.argv:
        path = sys.argv[sys.argv.index("--parse-only") + 1]
        with open(path, encoding="utf-8") as f:
            items, skipped = parse_items(f.read())
        for n, p in items.items():
            print(f"{p:>6,}円  {n}")
        print(f"読み取り: {len(items)}件")
        for s in skipped:
            print(f"  セットのため除外: {s}")
        return

    now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    print(f"取得開始: {now.strftime('%Y-%m-%d %H:%M:%S')}")

    if not SPREADSHEET_ID:
        raise ValueError("スプレッドシートIDが未設定です。Secretsの SPREADSHEET_ID_NAKANO を設定してください。")

    post = find_today_post(fetch_tweets(), now.date())
    if post is None:
        # ポストがない日はプライズ品の買取をしていない。前日の一覧はそのまま残し、更新しない
        print("今日の「本日の商品」のポストはまだありません。シートは更新しません。")
        print("::notice::no buyback post today; sheet not updated")
        return

    post_url = f"https://x.com/{TARGET_USER}/status/{post.get('id', '')}"
    print(f"対象のポスト: {post_url}")
    items, skipped = parse_items(post.get("text") or post.get("full_text") or "")
    for s in skipped:
        print(f"  セットのため除外: {s}")

    if not items:
        msg = (
            f"⚠️ **【{SHOP_LABEL}】商品取得0件アラート**\n"
            f"今日のポストから商品を読み取れませんでした。投稿の形式が変わった可能性があります。\n{post_url}"
        )
        print(msg)
        send_discord_notification(DISCORD_WEBHOOK_URL, msg)
        print("::error::0 items parsed (see log)")
        sys.exit(1)

    print(f"取得件数: {len(items)}件")
    ss = open_spreadsheet()
    current_sheet = init_worksheet(ss, SHEET_CURRENT, HEADERS_CURRENT)
    history_sheet = init_worksheet(ss, SHEET_HISTORY, HEADERS_HISTORY)
    write_items(current_sheet, history_sheet, items, today)


if __name__ == "__main__":
    main()
