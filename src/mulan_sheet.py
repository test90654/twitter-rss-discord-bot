"""
ムーラン買取データの書き込み先シートまわり（ダッシュボードと自動登録で共通）。
登録済み判定のキーをここ1か所で決めて、両方から同じように二重登録を防ぐ。
"""
import json
import os
import re
import unicodedata
from pathlib import Path

import gspread
from google.oauth2.service_account import Credentials

BASE_DIR = Path(__file__).resolve().parent.parent

TARGET_SPREADSHEET_ID = os.environ.get("TARGET_SPREADSHEET_ID", "1EQooFe5QbdCDe1wJj_lI8rxKoQFpre4E-1WbiPyJPx4")
RESULT_SHEET_NAME = os.environ.get("RESULT_SHEET_NAME", "sheet1")
# 店舗ごとの書き込み先シート（タブ名）。店舗名の無い古いデータは DEFAULT_STORE として扱う
DEFAULT_STORE = "ムラ中"
STORE_SHEETS = {
    "ムラ中": RESULT_SHEET_NAME,
    "ムーラン本店": os.environ.get("HONTEN_SHEET_NAME", "ムーラン本店"),
}
SHEET_HEADER = ["作品名", "商品名（シリーズ / キャラクター）", "買取価格", "更新日時", "型番"]

_SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
_LOCAL_CREDENTIALS = Path(r"C:\Users\chukyotokukai\Documents\rashinban\credentials.json")


def sheet_name_for(store):
    return STORE_SHEETS.get(store, store)


def get_gspread_client():
    """
    認証情報は 環境変数 GCP_CREDENTIALS_JSON（Render） → GCP_SA_KEY（GitHub Actions）
    → リポジトリ直下の credentials.json → 手元PCの credentials.json の順に探す
    """
    for env_name in ("GCP_CREDENTIALS_JSON", "GCP_SA_KEY"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            info = json.loads(raw)
            return gspread.authorize(Credentials.from_service_account_info(info, scopes=_SCOPE))

    creds_path = BASE_DIR / "credentials.json"
    if not creds_path.exists():
        if _LOCAL_CREDENTIALS.exists():
            creds_path = _LOCAL_CREDENTIALS
        else:
            raise FileNotFoundError(
                "認証ファイルが見つかりません。Renderでは環境変数 `GCP_CREDENTIALS_JSON`、"
                "GitHub Actionsでは `GCP_SA_KEY` を設定してください。"
            )
    return gspread.authorize(Credentials.from_service_account_file(str(creds_path), scopes=_SCOPE))


def get_result_sheet(client_gspread, store):
    """店舗に対応するシートを開く（無ければ見出し付きで作る）"""
    name = sheet_name_for(store)
    spreadsheet = client_gspread.open_by_key(TARGET_SPREADSHEET_ID)
    try:
        return spreadsheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=name, rows=1000, cols=10)
        sheet.update(range_name="A1:E1", values=[SHEET_HEADER])
        return sheet


def _norm(value):
    """比較用に表記ゆれを吸収する（全角/半角・空白・大文字小文字・日付の区切り）"""
    s = unicodedata.normalize("NFKC", str(value or ""))
    s = re.sub(r"\s+", "", s).lower()
    return s.replace("-", "/")


def item_key(name, model_number, update_date):
    """
    登録済み判定に使うキー。商品名＋型番/JAN＋更新日 が一致したら同じ商品とみなす。
    （価格は手で直すことがあるので判定に含めない）
    """
    return (_norm(name), _norm(model_number), _norm(update_date))


def read_rows(sheet):
    """B:商品名 C:価格 D:更新日 E:型番/JAN の行を返す（1行目の見出しは除く）"""
    rows = []
    for row in sheet.get_all_values()[1:]:
        row = row + [""] * (5 - len(row))
        rows.append(row[:5])
    return rows


def keys_from_rows(rows):
    return {item_key(r[1], r[4], r[3]) for r in rows if r[1].strip()}


def fetch_registered_keys(client_gspread, store):
    """その店舗のシートに既に登録されている商品のキー一覧を取得"""
    return keys_from_rows(read_rows(get_result_sheet(client_gspread, store)))


def append_to_result_sheet(sheet, row_data):
    """A列はジャンル列なので触らず、B列（商品名）の最終行の次の行の B〜E 列に書き込む"""
    next_row = len(sheet.col_values(2)) + 1
    if next_row > sheet.row_count:
        sheet.add_rows(100)
    sheet.update(
        range_name=f"B{next_row}:E{next_row}",
        values=[[
            row_data["name"],
            row_data["price"],
            row_data["update_date"],
            row_data["model_number"],
        ]],
    )
