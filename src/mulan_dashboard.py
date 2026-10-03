import streamlit as st
import os
import json
import time
from pathlib import Path
from PIL import Image
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types

# --- 1. 初期設定・パス設定 ---
BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "data" / "mulan_queue"
DONE_DIR = BASE_DIR / "data" / "mulan_done"

try:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    st.warning(f"ディレクトリの作成中に警告が発生しました: {e}")

st.set_page_config(
    page_title="ムーラン買取データ 承認ダッシュボード",
    page_icon="📦",
    layout="wide"
)

st.title("📦 ムーラン買取データ 承認ダッシュボード")
st.markdown("X（Twitter）から自動収集した買取表画像をGeminiで解析し、個別の登録やチェックした項目の一括登録を行えます。")

# --- 設定値（環境変数から安全に取得） ---
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MASTER_SPREADSHEET_ID = "1CHnUUP_9uiZYaWzbpoaTIjYwyFY5YBAv4x5M3gka2T0"
MASTER_SHEET_NAME = "シート1"
TARGET_SPREADSHEET_ID = "1EQooFe5QbdCDe1wJj_lI8rxKoQFpre4E-1WbiPyJPx4"
RESULT_SHEET_NAME = "sheet1"

# --- 2. スプレッドシート & マスター読み込み関数 ---
def get_gspread_client():
    creds_path = BASE_DIR / "credentials.json"
    if not creds_path.exists():
        alt_path = Path(r"C:\Users\chukyotokukai\Documents\rashinban\credentials.json")
        if alt_path.exists():
            creds_path = alt_path
        else:
            raise FileNotFoundError(f"認証ファイルが見つかりません: {creds_path}")
            
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(creds_path, scopes=scope)
    return gspread.authorize(creds)

def load_master_db(client_gspread):
    try:
        spreadsheet = client_gspread.open_by_key(MASTER_SPREADSHEET_ID)
        sheet = spreadsheet.worksheet(MASTER_SHEET_NAME)
        records = sheet.get_all_values()
        
        master_db = {}
        for row in records[1:]:
            if len(row) >= 2:
                jan = row[0].strip()
                name = row[1].strip()
                if jan:
                    master_db[jan] = name
        return master_db
    except Exception as e:
        st.error(f"マスターデータの読み込みエラー: {e}")
        return {}

def append_to_result_sheet(client_gspread, row_data):
    try:
        spreadsheet = client_gspread.open_by_key(TARGET_SPREADSHEET_ID)
        try:
            sheet = spreadsheet.worksheet(RESULT_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title=RESULT_SHEET_NAME, rows=1000, cols=10)
            sheet.append_row(["商品名", "価格", "更新日", "型番/JAN", "マスター照合結果"])
            
        sheet.append_row([
            row_data["name"],
            row_data["price"],
            row_data["update_date"],
            row_data["model_number"],
            row_data["match_status"]
        ])
    except Exception as e:
        raise Exception(f"結果シートへの書き込みエラー: {e}")

# --- 3. Gemini による画像解析関数（自動リトライ付き） ---
def analyze_image_with_gemini(image_path):
    if not API_KEY:
        st.error("❌ GEMINI_API_KEY が設定されていません。環境変数を確認してください。")
        return ""
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
    except Exception as e:
        st.error(f"画像ファイルの読み込みエラー: {e}")
        return ""

    client = genai.Client(api_key=API_KEY)
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
                st.warning(f"⚠️ Geminiが混雑しています（503エラー）。{wait_time}秒後に自動再試行します... ({attempt+1}/{max_retries})")
                time.sleep(wait_time)
            else:
                st.error(f"Gemini解析エラー: {e}")
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

# --- 4. 未処理キューの読み込み ---
if not QUEUE_DIR.exists():
    st.info("`data/mulan_queue/` フォルダを準備中です。")
    st.stop()

json_files = sorted(list(QUEUE_DIR.glob("*.json")))

if not json_files:
    st.info("🎉 現在、未処理の買取データはありません。GitHub Actionsによるデータ収集をお待ちください。")
    st.stop()

st.sidebar.title("📋 未処理キュー一覧")
st.sidebar.markdown(f"残り件数: **{len(json_files)}件**")
selected_json = st.sidebar.selectbox("確認するデータを選択", json_files, format_func=lambda x: x.name)

if selected_json:
    with open(selected_json, "r", encoding="utf-8") as f:
        meta = json.load(f)
    
    tweet_id = meta.get("tweet_id")
    tweet_text = meta.get("text", "")
    tweet_url = meta.get("tweet_url", "")
    image_filename = meta.get("image_file")
    image_path = QUEUE_DIR / image_filename

    session_key = f"parsed_{tweet_id}"
    parsed_items = st.session_state.get(session_key, [])

    # 共通の一括登録・スキップを実行する関数
    def execute_batch_save(indices):
        if not indices:
            st.warning("⚠️ 登録する項目が選択されていません。")
            return
        try:
            gc = get_gspread_client()
            master_db = load_master_db(gc)
            
            for idx in indices:
                item = parsed_items[idx]
                model_num = item["model_number"]
                if model_num in master_db:
                    official_name = master_db[model_num]
                    item["match_status"] = f"一致 (マスター: {official_name})"
                else:
                    item["match_status"] = "マスター未登録"
                    
                append_to_result_sheet(gc, item)
                
            st.success(f"🎉 選択された {len(indices)} 件の登録が完了しました！")
            
            if image_path.exists():
                image_path.rename(DONE_DIR / image_path.name)
            selected_json.rename(DONE_DIR / selected_json.name)
            
            if session_key in st.session_state:
                del st.session_state[session_key]
            st.rerun()
            
        except Exception as e:
            st.error(f"❌ 一括登録エラー: {e}")

    def execute_skip():
        if image_path.exists():
            image_path.rename(DONE_DIR / image_path.name)
        selected_json.rename(DONE_DIR / selected_json.name)
        if session_key in st.session_state:
            del st.session_state[session_key]
        st.warning("⚠️ このデータをスキップしました（キューから除外）。")
        st.rerun()

    # --- サイドバーの一括操作パネル ---
    if parsed_items:
        st.sidebar.markdown("---")
        st.sidebar.subheader("🚀 一括操作パネル")
        if st.sidebar.button("🚀 チェックした項目を一括登録", type="primary", key="sb_batch_btn"):
            indices = [i for i in range(len(parsed_items)) if st.session_state.get(f"chk_{tweet_id}_{i}", True)]
            execute_batch_save(indices)
            
        if st.sidebar.button("🗑️ このデータを丸ごとスキップ", key="sb_skip_btn"):
            execute_skip()

    # 画面構成：左側に画像を固定、右側に編集・登録リスト
    col_img, col_list = st.columns([1, 1.3], gap="large")

    with col_img:
        st.subheader("📷 買取表プレビュー")
        if image_path.exists():
            img = Image.open(image_path)
            st.image(img, width="stretch")
        else:
            st.error(f"画像ファイルが見つかりません: {image_filename}")

        with st.expander("🐦 元ツイートの本文とリンク"):
            st.markdown(tweet_text)
            st.markdown(f"[🔗 X(Twitter)で元ツイートを開く]({tweet_url})")

        if st.button("🤖 Gemini Flashで画像から自動抽出する", type="primary"):
            with st.spinner("Geminiが画像を解析してテキスト化しています..."):
                raw_text = analyze_image_with_gemini(str(image_path))
                parsed_list = parse_gemini_output(raw_text)
                
                try:
                    gc = get_gspread_client()
                    master_db = load_master_db(gc)
                except Exception:
                    master_db = {}

                for item in parsed_list:
                    model_num = item["model_number"]
                    if model_num in master_db:
                        official_name = master_db[model_num]
                        item["match_status"] = f"一致 (マスター: {official_name})"
                    else:
                        item["match_status"] = "マスター未登録"

                st.session_state[session_key] = parsed_list
                st.success(f"✨ 解析完了！ {len(parsed_list)}件のデータを抽出しました。")
                # 💡 解析完了後に即座に画面を再描画して右側にリストを表示させる
                st.rerun()

    with col_list:
        st.subheader("✍️ 抽出データ一覧・個別/チェック一括登録")
        
        if not parsed_items:
            st.info("👈 左側のボタンを押して画像をGeminiで解析してください。")
        else:
            st.markdown(f"📌 **{len(parsed_items)}件** 検出。各行で修正・個別登録が可能です。（一括登録は**画面左上のサイドバー**からも行えます）")
            
            if st.button("☑ すべての項目を選択する"):
                for i in range(len(parsed_items)):
                    st.session_state[f"chk_{tweet_id}_{i}"] = True
                st.rerun()

            selected_indices = []
            
            for idx, item in enumerate(parsed_items):
                with st.container(border=True):
                    c_chk, c_status = st.columns([1, 3])
                    with c_chk:
                        is_checked = st.checkbox("選択", value=st.session_state.get(f"chk_{tweet_id}_{idx}", True), key=f"chk_{tweet_id}_{idx}")
                        if is_checked:
                            selected_indices.append(idx)
                    with c_status:
                        match_lbl = item.get('match_status', '未確認')
                        st.markdown(f"**[{idx+1}] 照合:** `{match_lbl}`")

                    c1, c2 = st.columns([2, 1])
                    c3, c4 = st.columns([1, 1])
                    
                    with c1:
                        new_name = st.text_input("商品名", value=item["name"], key=f"name_{tweet_id}_{idx}")
                    with c2:
                        new_price = st.text_input("価格", value=item["price"], key=f"price_{tweet_id}_{idx}")
                    with c3:
                        new_date = st.text_input("更新日", value=item["update_date"], key=f"date_{tweet_id}_{idx}")
                    with c4:
                        new_model = st.text_input("型番/JAN", value=item["model_number"], key=f"model_{tweet_id}_{idx}")

                    parsed_items[idx]["name"] = new_name
                    parsed_items[idx]["price"] = new_price
                    parsed_items[idx]["update_date"] = new_date
                    parsed_items[idx]["model_number"] = new_model

                    if st.button(f"✅ この [{idx+1}] 件だけを登録", key=f"single_btn_{tweet_id}_{idx}"):
                        try:
                            gc = get_gspread_client()
                            master_db = load_master_db(gc)
                            
                            model_num = new_model
                            if model_num in master_db:
                                official_name = master_db[model_num]
                                match_status = f"一致 (マスター: {official_name})"
                            else:
                                match_status = "マスター未登録"

                            single_data = {
                                "name": new_name,
                                "price": new_price,
                                "update_date": new_date,
                                "model_number": new_model,
                                "match_status": match_status
                            }
                            append_to_result_sheet(gc, single_data)
                            st.success(f"🎉 商品 [{idx+1}] をスプレッドシートに登録しました！")
                        except Exception as e:
                            st.error(f"❌ 登録エラー: {e}")

            st.markdown("---")
            
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                if st.button("🚀 チェックした項目を一括登録 (下部)", type="primary"):
                    execute_batch_save(selected_indices)
            with col_b2:
                if st.button("🗑️ このデータを丸ごとスキップ (下部)"):
                    execute_skip()
