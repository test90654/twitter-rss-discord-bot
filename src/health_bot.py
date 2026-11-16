from datetime import date, datetime, timedelta
import json
import os
import re
import sys
import time
import unicodedata

import gspread
from google import genai
from google.genai import types
from pydantic import BaseModel
import requests

# 食事・運動・体重の記録ボット（減量用）
#
# ・Discord の専用チャンネルに送った写真やメモを Gemini で読み取り、Google スプレッドシートに記録する
#   食事の写真 → メニューとカロリーを推定 / Google Fit の画面 → 歩数と運動 / 「体重 72.4」→ 体重
# ・記録するたびに「今日あと何 kcal」を返信する
# ・毎週月曜に先週のまとめと、今週の本庄常勝軒の予定（佑二郎の作戦）を送る
#
# このリポジトリは公開なので、記録はスプレッドシート（非公開）にだけ置き、ログには中身を出さない。
#
# 使い方:
#   python src/health_bot.py            # 新しいメッセージを処理（15分ごと）
#   python src/health_bot.py --report   # 週間レポートを送る（月曜）

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import prize_calendar as pc  # noqa: E402  Gemini の呼び出し（モデル切り替え・再試行）を使い回す
import ramen_weekly as rw    # noqa: E402  常勝軒の予定と登場率

JST = pc.JST
DISCORD_API = "https://discord.com/api/v10"
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("DISCORD_HEALTH_CHANNEL_ID", "")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID_HEALTH", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GCP_SA_KEY = os.environ.get("GCP_SA_KEY", "")

LOG_SHEET = "記録"
DAY_SHEET = "日別"
SETTINGS_SHEET = "設定"
LOG_HEADER = ["日付", "時刻", "種類", "内容", "摂取kcal", "消費kcal", "たんぱく質g", "歩数", "体重kg", "確かさ", "メッセージID", "返信ID"]
DAY_HEADER = ["日付", "摂取kcal", "目標kcal", "差", "消費kcal（推定）", "歩数", "運動kcal", "たんぱく質g", "体重kg"]
DEFAULT_SETTINGS = [
    ["身長cm", ""], ["体重kg", ""], ["年齢", ""], ["性別", ""], ["目標体重kg", ""],
    ["1日に減らすkcal", "500"],
    ["佑二郎のkcal", "1500"],
    ["最後に読んだメッセージID", ""],
]
KCAL_PER_KG_FAT = 7200

# よく行くお店のメニュー（「お店メニュー」タブ。初期値は data/lunch_shops.json）
MENU_SHEET = "お店メニュー"
MENU_HEADER = ["店名", "メニュー", "kcal", "たんぱく質g", "メモ", "出典"]
SHOPS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lunch_shops.json")


def load_shops():
    try:
        with open(SHOPS_FILE, encoding="utf-8") as f:
            return json.load(f)["shops"]
    except (OSError, ValueError, KeyError):
        return []


def norm(text):
    t = unicodedata.normalize("NFKC", str(text)).lower()
    t = "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in t)  # カタカナ→ひらがな
    return re.sub(r"[\s・()（）「」【】\-ー〜~]", "", t)


# ============================================================
# スプレッドシート
# ============================================================
class Store:
    def __init__(self, sh):
        self.sh = sh
        self.log = self._ws(LOG_SHEET, LOG_HEADER)
        self.settings_ws = self._ws(SETTINGS_SHEET, ["項目", "値"], DEFAULT_SETTINGS)
        self.rows = [r + [""] * (len(LOG_HEADER) - len(r)) for r in self.log.get_all_values()[1:]]
        self.settings = {r[0]: (r[1] if len(r) > 1 else "") for r in self.settings_ws.get_all_values()[1:] if r and r[0]}
        self.dirty_settings = False
        self.shops = load_shops()
        seed = [[sh["name"], m[0], m[1], m[2], m[3], "目安"] for sh in self.shops for m in sh["menus"]]
        self.menu_ws = self._ws(MENU_SHEET, MENU_HEADER, seed)
        self.menus = [r + [""] * (len(MENU_HEADER) - len(r)) for r in self.menu_ws.get_all_values()[1:] if r and r[0] and r[1]]

    def _ws(self, title, header, initial=()):
        try:
            return self.sh.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = self.sh.add_worksheet(title=title, rows=1000, cols=len(header))
            ws.update([header, *initial], "A1")
            try:
                ws.freeze(rows=1)
            except Exception:
                pass
            return ws

    def get(self, key, default=None):
        v = str(self.settings.get(key, "")).strip()
        return v if v else default

    def num(self, key, default=None):
        try:
            return float(re.sub(r"[^\d.]", "", self.get(key, "")) or "x")
        except ValueError:
            return default

    def set(self, key, value):
        self.settings[key] = str(value)
        self.dirty_settings = True

    def add(self, rows):
        if rows:
            self.log.append_rows(rows, value_input_option="USER_ENTERED")
            self.rows += rows

    def delete_by_message(self, ids):
        """メッセージID・返信IDが一致する行を消す。消した行を返す"""
        ids = {str(i) for i in ids}
        keep, gone = [], []
        for r in self.rows:
            (gone if (r[10] in ids or r[11] in ids) else keep).append(r)
        if gone:
            self.log.batch_clear([f"A2:L{len(self.rows) + 1}"])
            if keep:
                self.log.update(keep, "A2", value_input_option="USER_ENTERED")
            self.rows = keep
        return gone

    def update_kcal(self, ids, kcal):
        ids = {str(i) for i in ids}
        hit = [i for i, r in enumerate(self.rows) if (r[10] in ids or r[11] in ids) and r[2] in ("食事", "運動")]
        if len(hit) != 1:
            return None
        i = hit[0]
        col = "E" if self.rows[i][2] == "食事" else "F"
        self.rows[i][4 if col == "E" else 5] = str(kcal)
        self.log.update([[kcal]], f"{col}{i + 2}")
        return self.rows[i]

    def set_reply_id(self, msg_id, reply_id):
        cells = []
        for i, r in enumerate(self.rows):
            if r[10] == str(msg_id) and not r[11]:
                r[11] = str(reply_id)
                cells.append(gspread.Cell(i + 2, 12, str(reply_id)))
        if cells:
            self.log.update_cells(cells)

    # ---- お店メニュー ----
    def shop_names(self):
        names = [sh["name"] for sh in self.shops]
        names += [r[0] for r in self.menus if r[0] not in names]
        return names

    def find_shop(self, text):
        """文章に出てくるお店（別名でも）。長い名前を優先"""
        t = norm(text)
        cands = []
        for name in self.shop_names():
            aliases = next((sh["aliases"] for sh in self.shops if sh["name"] == name), [])
            for a in [name, *aliases]:
                if norm(a) and norm(a) in t:
                    cands.append((len(norm(a)), name))
        return max(cands)[1] if cands else None

    def shop_menus(self, shop):
        return [r for r in self.menus if r[0] == shop]

    def find_menu(self, shop, name):
        """登録メニューから名前で探す（完全一致→部分一致）"""
        n = norm(name)
        rows = self.shop_menus(shop) if shop else self.menus
        exact = [r for r in rows if norm(r[1]) == n or norm(f"{r[0]}{r[1]}") == n]
        if exact:
            return exact[0]
        part = [r for r in rows if n and (n in norm(r[1]) or norm(r[1]) in n)]
        return min(part, key=lambda r: abs(len(norm(r[1])) - len(n))) if part else None

    def drop_estimates(self, shop):
        keep = [r for r in self.menus if not (r[0] == shop and r[5] == "目安")]
        if len(keep) != len(self.menus):
            self.menu_ws.batch_clear([f"A2:F{len(self.menus) + 1}"])
            if keep:
                self.menu_ws.update(keep, "A2", value_input_option="USER_ENTERED")
            self.menus = keep

    def add_menus(self, rows):
        """同じ店・同じメニュー名があれば上書き、無ければ追加"""
        existing = {(r[0], norm(r[1])): i for i, r in enumerate(self.menus)}
        new = []
        for row in rows:
            key = (row[0], norm(row[1]))
            if key in existing:
                i = existing[key]
                self.menus[i] = [str(x) for x in row]
                self.menu_ws.update([row], f"A{i + 2}", value_input_option="USER_ENTERED")
            else:
                new.append(row)
                existing[key] = len(self.menus)
                self.menus.append([str(x) for x in row])
        if new:
            self.menu_ws.append_rows(new, value_input_option="USER_ENTERED")
        return len(rows)

    def save(self):
        if self.dirty_settings:
            existing = {r[0]: i for i, r in enumerate(self.settings_ws.get_all_values()) if r}
            for key, value in self.settings.items():
                if key in existing:
                    self.settings_ws.update([[value]], f"B{existing[key] + 1}")
                else:
                    self.settings_ws.append_row([key, value])
                    existing[key] = len(existing)
            self.dirty_settings = False


def open_store():
    if not (GCP_SA_KEY and SPREADSHEET_ID):
        raise ValueError("GCP_SA_KEY と SPREADSHEET_ID_HEALTH を設定してください。")
    info = json.loads(GCP_SA_KEY)
    gc = gspread.service_account_from_dict(info, http_client=gspread.BackOffHTTPClient)
    try:
        return Store(gc.open_by_key(SPREADSHEET_ID))
    except (gspread.SpreadsheetNotFound, gspread.exceptions.APIError) as e:
        # 共有し忘れが一番多いので、共有先のメールアドレスを Discord に知らせる
        send(f"⚠️ 記録用のスプレッドシートを開けませんでした。\nスプレッドシートの「共有」に **{info.get('client_email')}** を「編集者」で追加してください。")
        raise SystemExit(f"スプレッドシートを開けません: {type(e).__name__}")


# ============================================================
# Discord
# ============================================================
def discord(method, path, **kw):
    for _ in range(5):
        r = requests.request(method, DISCORD_API + path, headers={"Authorization": f"Bot {BOT_TOKEN}"}, timeout=30, **kw)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Discord API {method} {path.split('?')[0]} → {r.status_code}")
        return r.json() if r.content else None
    raise RuntimeError("Discord API の制限が解除されませんでした")


def send(text, reply_to=None):
    if not (BOT_TOKEN and CHANNEL_ID):
        print("Discord の設定が無いので送信しません")
        return None
    body = {"content": text[:1990], "allowed_mentions": {"parse": []}}
    if reply_to:
        body["message_reference"] = {"message_id": str(reply_to), "fail_if_not_exists": False}
    return discord("POST", f"/channels/{CHANNEL_ID}/messages", json=body)


def fetch_messages(after):
    """after より新しいメッセージを古い順に。初回は直近24時間だけ"""
    out = []
    if after:
        cursor = after
        while True:
            batch = discord("GET", f"/channels/{CHANNEL_ID}/messages?after={cursor}&limit=100")
            if not batch:
                break
            batch.sort(key=lambda m: int(m["id"]))
            out += batch
            cursor = batch[-1]["id"]
            if len(batch) < 100:
                break
    else:
        since = datetime.now(JST) - timedelta(hours=24)
        out = [m for m in discord("GET", f"/channels/{CHANNEL_ID}/messages?limit=50") if msg_time(m) >= since]
        out.sort(key=lambda m: int(m["id"]))
    return out


def msg_time(m):
    return datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00")).astimezone(JST)


# ============================================================
# 計算
# ============================================================
def num(v, default=0.0):
    try:
        return float(str(v).replace(",", "")) if str(v).strip() else default
    except ValueError:
        return default


def current_weight(store, upto=None):
    ws = [(r[0], num(r[8])) for r in store.rows if r[2] == "体重" and num(r[8]) > 0 and (upto is None or r[0] <= upto)]
    return max(ws)[1] if ws else store.num("体重kg")


def bmr(store, weight=None):
    """基礎代謝（Mifflin-St Jeor 式）。プロフィールが足りなければ None"""
    h, a, w = store.num("身長cm"), store.num("年齢"), weight or current_weight(store)
    sex = store.get("性別", "")
    if not (h and a and w and sex):
        return None
    male = sex.startswith(("男", "m", "M"))
    return 10 * w + 6.25 * h - 5 * a + (5 if male else -161)


def day_summary(store, day):
    rows = [r for r in store.rows if r[0] == day]
    intake = sum(num(r[4]) for r in rows if r[2] == "食事")
    protein = sum(num(r[6]) for r in rows if r[2] == "食事")
    exercise = sum(num(r[5]) for r in rows if r[2] == "運動")
    steps = max([num(r[7]) for r in rows if r[2] == "歩数"] or [0])  # 1日に何度送っても最大値（累計）を使う
    weight = current_weight(store, day)
    w_today = [num(r[8]) for r in rows if r[2] == "体重" and num(r[8]) > 0]
    b = bmr(store, weight)
    walk = steps * (weight or 65) * 0.0005  # 歩数の消費 ≒ 歩数 × 体重 × 0.0005 kcal
    out = {"day": day, "intake": intake, "protein": protein, "exercise": exercise, "steps": steps,
           "walk": walk, "weight": w_today[-1] if w_today else None, "meals": sum(1 for r in rows if r[2] == "食事")}
    if b:
        burn = b * 1.2 + walk + exercise           # 1.2 = 座り仕事中心の生活
        floor = max(b, 1200)                       # 目標がこれより下がらないようにする
        target = max(burn - (store.num("1日に減らすkcal", 500) or 500), floor)
        out.update(bmr=b, burn=burn, target=target, balance=intake - burn)
    return out


def today_line(store, day):
    s = day_summary(store, day)
    if "target" not in s:
        return f"今日の合計: {s['intake']:,.0f}kcal（目標を出すには「設定」を送ってください）"
    left = s["target"] - s["intake"]
    mark = "✅" if left >= 0 else "⚠️"
    act = f"／歩数 {s['steps']:,.0f}" if s["steps"] else ""
    return f"{mark} 今日 {s['intake']:,.0f} / 目標 {s['target']:,.0f}kcal（{'あと ' + format(left, ',.0f') if left >= 0 else format(-left, ',.0f') + ' オーバー'}）{act}"


# ============================================================
# Gemini で読み取る
# ============================================================
class Entry(BaseModel):
    kind: str          # 食事 / 運動 / 歩数 / 体重 / その他
    description: str   # 何を食べたか・何をしたか（短く）
    kcal_in: int       # 食事の摂取カロリー
    kcal_out: int      # 運動の消費カロリー
    protein_g: float
    steps: int
    weight_kg: float
    days_ago: int      # 0=今日、1=昨日
    confidence: str    # 高 / 中 / 低
    shop: str          # お店の名前（分かれば。登録店なら登録名）
    menu_match: str    # 登録メニューに当たるならそのメニュー名（無ければ空）


class Parsed(BaseModel):
    entries: list[Entry]
    comment: str       # 一言（無ければ空）


PROMPT = """あなたは減量中の人の食事・運動記録アシスタントです。送られてきた写真とメモから記録を作ってください。
いまの日時: {now}（日本時間）
{ramen}
メモ: 「{text}」

記録の種類（entries に1件ずつ）:
- 食事: 写真やメモの料理を1品ずつ、または1食まとめて。kcal_in に推定カロリー、protein_g にたんぱく質。
  量が分からなければ一般的な1人前で推定し、confidence を「低」にする。
  ラーメンはスープを半分残す前提にしない（普通に食べた想定）。二郎系は量が多いので多めに見積もる。
- 運動: ジム・ランニング・筋トレなど。画面に消費カロリーがあればその値、無ければ体重{weight}kgとして時間と強さから推定し kcal_out に。
- 歩数: Google Fit やスマホの歩数の画面なら、その日の歩数を steps に。
  画面の「合計消費カロリー」は基礎代謝込みなので記録しない。ウォーキングなど個別の運動が表示されていれば「運動」として別に記録する。
- 体重: 体重計の写真や「体重72.4」なら weight_kg に。
- その他: 記録するものが無い雑談など（entries は空でもよい）

よく行くお店の登録メニュー（kcal は登録値）:
{shops}
- メモや写真が上のお店の料理なら、shop にお店の名前、menu_match に一番近い登録メニュー名をそのまま書く
  （「中盛」「大盛」などの量も合わせる。ライスなど別の品は別の entry にする）。登録に無い料理なら menu_match は空
- 登録メニューに当たったときの kcal_in・protein_g は登録値と同じにする

ルール:
- days_ago: 「昨日」「昨夜」などとあれば 1。画面に日付があり今日でなければその日数。それ以外は 0
- 使わない数値の項目は 0
- description は「二郎系ラーメン（野菜マシ）」「ジム 筋トレ45分」のように短く
- comment: 記録についての一言（20字以内、責めない口調）。無ければ空
"""


def shops_context(store, text):
    """プロンプトに入れるお店のメニュー。お店の名前が出てきたらそのお店だけ詳しく"""
    shop = store.find_shop(text or "")
    shops = [shop] if shop else store.shop_names()
    lines = []
    for name in shops:
        items = "、".join(f"{r[1]}({num(r[2]):.0f})" for r in store.shop_menus(name))
        lines.append(f"・{name}: {items}")
    return "\n".join(lines) or "（なし）"


def parse_message(client, text, images, store, now):
    ramen = ramen_today_context(now.date())
    prompt = PROMPT.format(now=f"{now:%Y-%m-%d %H:%M}", text=text or "（なし）", weight=current_weight(store) or 65,
                           ramen=ramen, shops=shops_context(store, text))
    parts = [types.Part.from_bytes(data=b, mime_type=m) for b, m in images] + [prompt]
    cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=Parsed, max_output_tokens=4096)
    resp = pc._call_gemini(client, parts, cfg)
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, Parsed):
        return parsed
    return Parsed.model_validate(json.loads((resp.text or "{}").strip().strip("`").removeprefix("json")))


def ramen_today_context(day):
    """今日の常勝軒の限定メニュー（写真がラーメンなら、それが何かを当てる手がかりにする）"""
    try:
        data = rw.load_data()
        names = []
        for w in data.get("weeks", {}).values():
            for d in w["days"]:
                if d.get("date") == day.isoformat() and not d.get("closed"):
                    names += [m.get("same_as") or m.get("name") for m in d.get("menus", [])]
        names += data.get("daily", {}).get(day.isoformat(), {}).get("menus", [])
        names = list(dict.fromkeys(n for n in names if n))
        if names:
            return f"参考: 今日の本庄常勝軒（ラーメン屋）の限定は「{'・'.join(names)}」。佑二郎は二郎系（約{1500}kcal）。"
    except Exception:
        pass
    return "参考: 本庄常勝軒の佑二郎は二郎系ラーメン（約1500kcal）。"


# ============================================================
# メッセージの処理
# ============================================================
def handle_command(store, m, text, now):
    """文字だけで済むコマンド。処理したら返信文を返す"""
    t = unicodedata.normalize("NFKC", text).strip()
    ref = (m.get("message_reference") or {}).get("message_id")
    day = now.date().isoformat()

    if ref and re.fullmatch(r"(取消|取り消し|取消し|削除|消して|けして)", t):
        gone = store.delete_by_message([ref])
        return f"🗑 {len(gone)}件 取り消しました\n{today_line(store, day)}" if gone else "取り消す記録が見つかりませんでした"
    mk = re.fullmatch(r"(\d{2,5})\s*(kcal|キロカロリー|カロリー)?", t)
    if ref and mk:
        row = store.update_kcal([ref], int(mk.group(1)))
        return f"✏️ {row[3]} を {int(mk.group(1)):,}kcal に直しました\n{today_line(store, row[0])}" if row else "直す記録が1件に絞れませんでした（取消して送り直してください）"

    if t.startswith("設定"):
        keys = {"身長cm": r"身長\s*(\d{3}(?:\.\d)?)", "体重kg": r"体重\s*(\d{2,3}(?:\.\d)?)", "年齢": r"(?:年齢\s*(\d{2})|(\d{2})\s*歳)",
                "目標体重kg": r"目標\s*(?:体重)?\s*(\d{2,3}(?:\.\d)?)", "1日に減らすkcal": r"減らす\s*(\d{3,4})"}
        changed = []
        for key, pat in keys.items():
            mm = re.search(pat, t)
            if mm:
                store.set(key, next(g for g in mm.groups() if g))
                changed.append(f"{key.replace('cm', '').replace('kg', '')} {store.get(key)}")
        ms = re.search(r"(男性|女性|男|女)", t)
        if ms:
            store.set("性別", ms.group(1)[0])
            changed.append(f"性別 {ms.group(1)[0]}")
        b = bmr(store)
        lines = [f"⚙️ 設定しました: {'、'.join(changed) or '（変更なし）'}"]
        if b:
            s = day_summary(store, day)
            lines.append(f"基礎代謝 約{b:,.0f}kcal → 運動なしの日の目標 約{s['target'] - s['walk'] - s['exercise']:,.0f}kcal/日")
            goal, w = store.num("目標体重kg"), current_weight(store)
            # 実際に減らせる量（目標は基礎代謝を下回らないようにしているので、設定値より小さくなることがある）
            cut = b * 1.2 - (s["target"] - s["walk"] - s["exercise"])
            if cut < (store.num("1日に減らすkcal", 500) or 500) - 1:
                lines.append(f"※体を壊さないよう目標は基礎代謝より下げないので、1日に減らせるのは約{cut:,.0f}kcalです。歩くと目標が増えます")
            if goal and w and w > goal and cut > 0:
                weeks = (w - goal) * KCAL_PER_KG_FAT / (cut * 7)
                lines.append(f"目標 {goal:g}kg まで あと{w - goal:.1f}kg（このペースで約{weeks:.0f}週）")
        else:
            lines.append("目標カロリーを出すには「設定 身長170 体重80 40歳 男 目標72」のように送ってください")
        return "\n".join(lines)

    mw = re.fullmatch(r"(?:体重\s*)?(\d{2,3}\.\d|\d{2,3})\s*(?:kg|キロ)?", t) if (t.startswith("体重") or re.search(r"kg|キロ", t)) else None
    if mw:
        w = float(mw.group(1))
        prev = current_weight(store)
        store.add([[day, f"{now:%H:%M}", "体重", "体重", "", "", "", "", w, "高", m["id"], ""]])
        diff = f"（前回から {w - prev:+.1f}kg）" if prev and prev != w else ""
        goal = store.num("目標体重kg")
        rest = f"\n目標まであと {w - goal:.1f}kg" if goal and w > goal else ("\n🎉 目標達成！" if goal else "")
        return f"⚖️ 体重 {w:g}kg を記録しました{diff}{rest}"

    ms = re.fullmatch(r"歩数\s*([\d,]+)\s*歩?", t)
    if ms:
        steps = int(ms.group(1).replace(",", ""))
        store.add([[day, f"{now:%H:%M}", "歩数", "歩数", "", "", "", steps, "", "高", m["id"], ""]])
        return f"👟 歩数 {steps:,}歩 を記録しました\n{today_line(store, day)}"

    mu = re.fullmatch(r"(.+?)\s*(?:の)?\s*いつもの", t)
    if mu:
        shop = store.find_shop(mu.group(1))
        if not shop:
            return f"「{mu.group(1)}」というお店が見つかりませんでした（「お店」で一覧）"
        usual = usual_meal(store, shop)
        if not usual:
            return f"{shop} の記録がまだありません。一度「{shop} ○○」で記録すると、次から「いつもの」が使えます"
        store.add([[day, f"{now:%H:%M}", "食事", d, k, "", p or "", "", "", "登録", m["id"], ""] for d, k, p in usual])
        lines = [f"🍽 {d} 約{k:,}kcal" for d, k, p in usual]
        return "\n".join(lines) + f"（いつもの）\n{today_line(store, day)}\n-# 違っていたら、このメッセージに返信で「取消」"

    mm = re.fullmatch(r"メニュー\s*(.+)", t)
    if mm and not t.startswith("メニュー登録"):
        shop = store.find_shop(mm.group(1))
        if not shop:
            return f"「{mm.group(1)}」というお店が見つかりませんでした（「お店」で一覧）"
        rows = store.shop_menus(shop)
        body = "\n".join(f"・{r[1]} {num(r[2]):,.0f}kcal" + (f"（{r[4]}）" if r[4] else "") for r in rows)
        return f"📖 **{shop}** の登録メニュー（{len(rows)}品）\n{body}\n-# メニュー表の写真に「メニュー登録 {shop}」と付けて送ると追加・更新できます"
    if t in ("お店", "店", "お店一覧"):
        return "🏠 **登録しているお店**\n" + "\n".join(f"・{n}（{len(store.shop_menus(n))}品）" for n in store.shop_names())

    if t in ("今日", "きょう", "今日は?", "残り", "あと何kcal"):
        return today_detail(store, day)
    if t in ("週", "今週", "レポート", "週間レポート"):
        return weekly_report(store, now)
    if t in ("ヘルプ", "help", "使い方", "?"):
        return HELP
    return None


HELP = """**使い方**
・食事の写真やメモ（「から揚げ定食」「昨日の夜 佑二郎」）→ カロリーを推定して記録
・Google Fit の画面のスクショ → 歩数と運動を記録（「歩数 8000」「ジム45分」でもOK）
・「体重 72.4」→ 体重を記録
・記録への返信で「取消」→ 取り消し／「800kcal」→ カロリーを直す
・「今日」→ 今日の内訳　「週」→ 今週のまとめ
・「設定 身長170 体重80 40歳 男 目標72」→ 目標カロリーの計算に使います
**よく行くお店**
・「楊喜家 ラーメン中盛 ライス」→ 登録メニューの値で記録　「常勝軒 いつもの」→ いつものメニュー
・メニュー表の写真＋「メニュー登録 花田」→ メニューを読み取って登録
・「お店」→ 登録店の一覧　「メニュー 楊喜家」→ そのお店の登録メニュー"""


def today_detail(store, day):
    rows = [r for r in store.rows if r[0] == day]
    lines = [f"📋 **{day[5:].replace('-', '/')} の記録**"]
    for r in rows:
        if r[2] == "食事":
            lines.append(f"・{r[1]} {r[3]} {num(r[4]):,.0f}kcal")
        elif r[2] == "運動":
            lines.append(f"・{r[1]} {r[3]} −{num(r[5]):,.0f}kcal")
    s = day_summary(store, day)
    if s["steps"]:
        lines.append(f"・歩数 {s['steps']:,.0f}歩（−{s['walk']:,.0f}kcal）")
    if len(lines) == 1:
        lines.append("・まだ記録がありません")
    lines.append(today_line(store, day))
    if s["protein"]:
        lines.append(f"たんぱく質 {s['protein']:.0f}g")
    return "\n".join(lines)


def reply_text(entries, comment, store, now):
    lines = []
    icons = {"食事": "🍽", "運動": "🏃", "歩数": "👟", "体重": "⚖️"}
    days = set()
    for e in entries:
        day = (now.date() - timedelta(days=max(0, e.days_ago))).isoformat()
        days.add(day)
        when = "" if day == now.date().isoformat() else f"（{day[5:].replace('-', '/')}の分）"
        if e.kind == "食事":
            low = "（推定・ざっくり）" if e.confidence == "低" else ""
            if e.confidence == "登録":
                low = "（登録メニュー）"
            lines.append(f"{icons[e.kind]} {e.description} 約{e.kcal_in:,}kcal{low}{when}")
        elif e.kind == "運動":
            lines.append(f"{icons[e.kind]} {e.description} −{e.kcal_out:,}kcal{when}")
        elif e.kind == "歩数":
            lines.append(f"{icons[e.kind]} 歩数 {e.steps:,}歩{when}")
        elif e.kind == "体重":
            lines.append(f"{icons[e.kind]} 体重 {e.weight_kg:g}kg{when}")
    if comment:
        lines.append(comment)
    for day in sorted(days):
        line = today_line(store, day)
        lines.append(line if day == now.date().isoformat() else line.replace("今日", day[5:].replace("-", "/")))
    lines.append("-# 違っていたら、このメッセージに返信で「取消」か「800kcal」のように送ってください")
    return "\n".join(lines)


def entries_to_rows(entries, m, now):
    rows = []
    for e in entries:
        if e.kind not in ("食事", "運動", "歩数", "体重"):
            continue
        day = (now.date() - timedelta(days=max(0, min(e.days_ago, 7)))).isoformat()
        rows.append([day, f"{now:%H:%M}", e.kind, e.description,
                     e.kcal_in if e.kind == "食事" else "", e.kcal_out if e.kind == "運動" else "",
                     round(e.protein_g, 1) if e.kind == "食事" and e.protein_g else "",
                     e.steps if e.kind == "歩数" else "", e.weight_kg if e.kind == "体重" else "",
                     e.confidence, m["id"], ""])
    return rows


def download_images(m):
    out = []
    for a in m.get("attachments", [])[:4]:
        ctype = a.get("content_type", "")
        if not ctype.startswith("image/"):
            continue
        r = requests.get(a["url"], timeout=60)
        r.raise_for_status()
        out.append((r.content, ctype.split(";")[0]))
    return out


def apply_registered(store, entries, text):
    """登録メニューに当たった食事は、Gemini の推定ではなく登録値を使う"""
    shop_in_text = store.find_shop(text or "")
    for e in entries:
        if e.kind != "食事":
            continue
        shop = shop_in_text or (store.find_shop(e.shop) if e.shop else None)
        row = store.find_menu(shop, e.menu_match) if e.menu_match else None
        if row is None and shop:
            row = store.find_menu(shop, e.description.replace(shop, ""))
        if row:
            e.kcal_in = int(num(row[2]))
            e.protein_g = num(row[3])
            e.description = f"{row[0]} {row[1]}"
            e.confidence = "登録"
        elif shop and not e.description.startswith(shop):
            e.description = f"{shop} {e.description}"


class MenuItem(BaseModel):
    name: str
    kcal: int
    protein_g: float
    note: str


class MenuBoard(BaseModel):
    shop: str
    items: list[MenuItem]


MENU_PROMPT = """これは飲食店のメニュー表・券売機・お品書きの写真です。載っている料理をすべて読み取ってください。
お店: {shop}
- name: メニューに書かれた名前のまま（サイズ違いが別の値段なら「（並）」「（大盛）」のように別の行にする）
- kcal: 一般的な量と作り方で1人前を推定（ラーメンはスープも半分程度飲む前提）。値段は使わない
- protein_g: たんぱく質の推定
- note: 「辛い」「野菜多め」など kcal に関わる特徴があれば短く（無ければ空）
- ドリンク・お酒・トッピングの小物（100kcal 未満）は省いてよい。ライス・餃子などのサイドは入れる
- shop: 写真から店名が読めればその名前、読めなければ「{shop}」
"""


def register_menu(store, client, text, images):
    name = re.sub(r"^\s*メニュー(?:表)?登録\s*", "", unicodedata.normalize("NFKC", text)).strip()
    shop = store.find_shop(name) or name or "（店名なし）"
    cfg = types.GenerateContentConfig(response_mime_type="application/json", response_schema=MenuBoard, max_output_tokens=8192)
    parts = [types.Part.from_bytes(data=b, mime_type=mt) for b, mt in images] + [MENU_PROMPT.format(shop=shop)]
    resp = pc._call_gemini(client, parts, cfg)
    board = resp.parsed if isinstance(getattr(resp, "parsed", None), MenuBoard) else MenuBoard.model_validate(json.loads(resp.text))
    if shop == "（店名なし）" and board.shop:
        shop = store.find_shop(board.shop) or board.shop
    rows = [[shop, it.name, it.kcal, round(it.protein_g, 1), it.note, "写真から読み取り"] for it in board.items if it.name and it.kcal > 0]
    if not rows:
        return "メニューを読み取れませんでした。メニュー表全体が写るように撮り直してみてください。"
    store.drop_estimates(shop)  # 最初に入れておいた目安のメニューは、本物のメニューで置き換える
    store.add_menus(rows)
    sample = "\n".join(f"・{r[1]} 約{r[2]:,}kcal" for r in rows[:8])
    more = f"\n…ほか {len(rows) - 8}品" if len(rows) > 8 else ""
    return (f"📖 **{shop}** のメニューを {len(rows)}品 登録しました（kcalは推定）\n{sample}{more}\n"
            f"-# 値はスプレッドシートの「お店メニュー」タブで直せます。次からは「{shop} ○○」で登録値を使います")


def usual_meal(store, shop):
    """そのお店でいちばんよく頼む組み合わせ（1回の記録＝1メッセージ単位。ラーメン＋ライスなど）"""
    from collections import Counter
    visits = {}
    for r in store.rows:
        if r[2] == "食事" and r[3].startswith(shop):
            visits.setdefault(r[10], []).append(r)
    if not visits:
        return None
    combo, _ = Counter(tuple(sorted(r[3] for r in v)) for v in visits.values()).most_common(1)[0]
    items = []
    for desc in combo:
        same = [r for r in store.rows if r[2] == "食事" and r[3] == desc]
        mid = lambda col: sorted(num(r[col]) for r in same)[len(same) // 2]
        items.append((desc, int(mid(4)), round(mid(6), 1)))
    return items


def process_messages(store, client):
    msgs = fetch_messages(store.get("最後に読んだメッセージID"))
    done = 0
    for m in msgs:
        try:
            if m.get("author", {}).get("bot") or m.get("type") not in (0, 19):
                continue
            now = msg_time(m)
            text = m.get("content", "")
            images = download_images(m)
            if not text and not images:
                if not m.get("attachments"):
                    send("⚠️ メッセージの文字が読めません。Discord Developer Portal でボットの **MESSAGE CONTENT INTENT** をオンにしてください。")
                continue
            reply = None if images else handle_command(store, m, text, now)
            if reply is None and images and re.match(r"\s*メニュー(?:表)?登録", text or ""):
                reply = register_menu(store, client, text, images)
            if reply is None:
                parsed = parse_message(client, text, images, store, now)
                apply_registered(store, parsed.entries, text)
                rows = entries_to_rows(parsed.entries, m, now)
                store.add(rows)
                reply = reply_text([e for e in parsed.entries if e.kind in ("食事", "運動", "歩数", "体重")],
                                   parsed.comment, store, now) if rows else "記録するものが見つかりませんでした（「ヘルプ」で使い方）"
                if rows and bmr(store) is None and not store.get("案内済み"):
                    reply += "\n\n" + HELP
                    store.set("案内済み", "1")
            sent = send(reply, reply_to=m["id"])
            if sent:
                store.set_reply_id(m["id"], sent["id"])
            done += 1
        except pc.QuotaExhausted:
            print("::warning::Gemini の今日の上限に達しました。残りは次回処理します")
            break
        except Exception as e:
            # ログは公開されるので、メッセージの中身は出さない
            print(f"::warning::メッセージ {m['id']} の処理に失敗: {type(e).__name__}")
            try:
                send("⚠️ うまく読み取れませんでした。もう一度送ってみてください。", reply_to=m["id"])
            except Exception:
                pass
        store.set("最後に読んだメッセージID", m["id"])
    print(f"処理したメッセージ: {done}件")
    return done


# ============================================================
# 日別シートと週間レポート
# ============================================================
def write_day_sheet(store):
    days = sorted({r[0] for r in store.rows if re.match(r"\d{4}-\d{2}-\d{2}$", r[0])})
    out = [DAY_HEADER]
    for d in reversed(days):
        s = day_summary(store, d)
        out.append([d, round(s["intake"]), round(s["target"]) if "target" in s else "",
                    round(s["intake"] - s["target"]) if "target" in s else "",
                    round(s["burn"]) if "burn" in s else "", int(s["steps"]) or "", round(s["exercise"]) or "",
                    round(s["protein"]) or "", s["weight"] or ""])
    ws = store._ws(DAY_SHEET, DAY_HEADER)
    ws.clear()
    ws.update(out, "A1", value_input_option="USER_ENTERED")


def weekly_report(store, now, last_week=False):
    """last_week=True なら先週（月〜日）、False なら今週の今日まで"""
    monday = rw.week_monday(now.date()) - timedelta(days=7 if last_week else 0)
    end = monday + timedelta(days=6) if last_week else now.date()
    days = [(monday + timedelta(days=i)).isoformat() for i in range((end - monday).days + 1)]
    sums = [day_summary(store, d) for d in days]
    logged = [s for s in sums if s["meals"]]
    title = "先週" if last_week else "今週"
    lines = [f"📊 **{title}のまとめ（{monday:%m/%d}〜{end:%m/%d}）**"]
    if not logged:
        lines.append("食事の記録がありませんでした")
    else:
        avg_in = sum(s["intake"] for s in logged) / len(logged)
        lines.append(f"・食事を記録した日: {len(logged)}/{len(days)}日")
        lines.append(f"・平均の摂取: {avg_in:,.0f}kcal/日")
        if all("target" in s for s in logged):
            avg_t = sum(s["target"] for s in logged) / len(logged)
            bal = sum(s["balance"] for s in logged)
            ok = sum(1 for s in logged if s["intake"] <= s["target"])
            lines.append(f"・目標以内の日: {ok}/{len(logged)}日（平均の目標 {avg_t:,.0f}kcal）")
            lines.append(f"・カロリー収支: {bal:+,.0f}kcal → 体脂肪にすると約 {bal / KCAL_PER_KG_FAT:+.2f}kg")
        top = sorted(((num(r[4]), r[3], r[0]) for r in store.rows if r[2] == "食事" and r[0] in days), reverse=True)[:3]
        if top:
            lines.append("・カロリーが多かった食事: " + "、".join(f"{d[5:].replace('-', '/')} {n}（{k:,.0f}）" for k, n, d in top))
    steps = [s["steps"] for s in sums if s["steps"]]
    if steps:
        lines.append(f"・平均歩数: {sum(steps) / len(steps):,.0f}歩（記録 {len(steps)}日）")
    ex = [r for r in store.rows if r[2] == "運動" and r[0] in days]
    if ex:
        lines.append(f"・運動: {len(ex)}回 −{sum(num(r[5]) for r in ex):,.0f}kcal")
    ws = sorted((r[0], num(r[8])) for r in store.rows if r[2] == "体重" and num(r[8]) > 0)
    in_week = [w for d, w in ws if d in days]
    before = [w for d, w in ws if d < days[0]]
    if in_week:
        base = before[-1] if before else (in_week[0] if len(in_week) > 1 else None)
        goal = store.num("目標体重kg")
        txt = f"・体重: {in_week[-1]:g}kg" + (f"（{base:g}kg から {in_week[-1] - base:+.1f}kg）" if base else "")
        if goal and in_week[-1] > goal:
            txt += f"、目標まであと {in_week[-1] - goal:.1f}kg"
        lines.append(txt)

    plan = ramen_plan(store, now, monday if not last_week else rw.week_monday(now.date()))
    if plan:
        lines += ["", *plan]
    return "\n".join(lines)


def ramen_plan(store, now, monday):
    """今週の常勝軒の予定と、佑二郎に行くときの調整案"""
    try:
        data = rw.load_data()
        watch = rw.load_watch()
        weeks = rw.merged_weeks(data)
    except Exception:
        return []
    wk = monday.isoformat()
    lines = [f"🍜 **今週の常勝軒**"]
    week = weeks.get(wk)
    hits = []
    if week and week.get("board"):
        menus = rw.grouped_menus(week, watch)
        lines.append("・" + "／".join(f"{'・'.join(d) or '?'} {raws[0] if len(raws) == 1 else name}" for g, (name, d, raws) in menus.items()))
        hits = [(menus[g][0], menus[g][1]) for g in menus if g.startswith("watch:")]
    else:
        lines.append("・今週の予定はまだ出ていません（出たら別に通知します）")
    stats, _ = rw.compute_stats(weeks, watch)
    kcal = store.num("佑二郎のkcal", 1500) or 1500
    if hits:
        for name, days in hits:
            s = day_summary(store, now.date().isoformat())
            normal = (s.get("target") or 2000) / 3            # ふつうの1食分
            extra = max(0, kcal - normal)
            others = 6
            per_day = extra / others
            walk_steps = extra / ((current_weight(store) or 65) * 0.0005)
            lines.append(f"🔔 **{'・'.join(days) or '?'}曜に{name}**（約{kcal:,.0f}kcal、ふつうの1食より +{extra:,.0f}kcal）")
            lines.append(f"　行くなら: ほかの6日を 1日 −{per_day:,.0f}kcal ずつ控えめに"
                         f"／または 週の合計で +{walk_steps:,.0f}歩 で相殺できます")
    else:
        for w in watch:
            s = stats.get("watch:" + w)
            if s:
                tip = "→ そろそろ来そう" if s["interval"] and s["since"] >= s["interval"] else ""
                lines.append(f"・{w.split('|')[0]}: 来週出る確率 約{s['p_next']:.0%}（最後は{s['since']}週前）{tip}")
    return lines


def main():
    for name, v in [("DISCORD_BOT_TOKEN", BOT_TOKEN), ("DISCORD_HEALTH_CHANNEL_ID", CHANNEL_ID), ("GEMINI_API_KEY", GEMINI_API_KEY)]:
        if not v:
            raise SystemExit(f"{name} が設定されていません")
    store = open_store()
    now = datetime.now(JST)
    try:
        if "--report" in sys.argv:
            send(weekly_report(store, now, last_week=True))
            print("週間レポートを送りました")
        else:
            process_messages(store, genai.Client(api_key=GEMINI_API_KEY))
        write_day_sheet(store)
    finally:
        store.save()


if __name__ == "__main__":
    main()
