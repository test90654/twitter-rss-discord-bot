import streamlit as st
import os
import json
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

st.title("📦 ムーラン買取データ 承認ダッシュボード (Gemini OCR連携)")
st.markdown("X（Twitter）から自動収集した買取表画像を確認し、スプレッドシートへスムーズに転記できます。")

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

# --- 3. Gemini による画像解析関数 ---
def analyze_image_with_gemini(image_path):
    if not API_KEY:
        st.error("❌ GEMINI_API_KEY が設定されていません。環境変数を確認してください。")
        return ""
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()

        client = genai.Client(api_key=API_KEY)
        prompt = """
        添付された買取表の画像を読み取り、以下のルールに従ってパイプ ( | ) 区切りの一覧表形式で出力してください。
        余計な挨拶や解説文は一切含めず、表の行データのみを出力してください。

        【出力フォーマット（1行につき）】
        商品名 | 価格 | 更新日 | 型番

        【処理ルール】
        1. **更新日の付与**: 画像内から更新日を読み取り、各行に反映する。
        2. **レイアウトの整形**: 「価格」の右側に「更新日」と「型番」を配置する。
        3. **不要な文言の除外**: 注意事項、営業時間などのノイズ文言はすべて除外する。
        4. **シリーズ名・カテゴリー名の付与**: 赤文字等で記載されているシリーズ名やカテゴリー名を、各商品の先頭に必ず付与する。
        5. **重複・バリエーションの処理**: 型番が同じでもキャラクター違いやバージョン違いがある場合は絶対に統合せず別行として出力する。
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'),
                prompt
            ]
        )
        return response.text
    except Exception as e:
        st.error(f"Gemini解析エラー: {e}")
        return ""

def parse_gemini_output(text):
    items = []
    if not text:
        return items
    lines = text.strip().split("\n")
    for line in lines:
        if "|" in line and "---" not in line and "商品名" not in line:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4:
                items.append({
                    "name": parts[0],
                    "price": parts[1],
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

    col1, col2 = st.columns([1, 1], gap="large")

    with col1:
        st.subheader("📷 買取表プレビュー")
        if image_path.exists():
            img = Image.open(image_path)
            st.image(img, width="stretch")
        else:
            st.error(f"画像ファイルが見つかりません: {image_filename}")

        with st.expander("🐦 元ツイートの本文とリンク"):
            st.markdown(tweet_text)
            st.markdown(f"[🔗 X(Twitter)で元ツイートを開く]({tweet_url})")

        if st.button("🤖 Gemini Flashで画像から自動抽出する", type="secondary"):
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
                        item["match_status"] = f"一致 (マスター名: {official_name})"
                    else:
                        item["match_status"] = "マスター未登録"

                st.session_state[session_key] = parsed_list
                st.success(f"✨ 解析完了！ {len(parsed_list)}件のデータを抽出しました。")

    with col2:
        st.subheader("✍️ 抽出データ確認・一括/個別承認")
        
        parsed_items = st.session_state.get(session_key, [])

        if parsed_items:
            st.info(f"📌 Geminiによって **{len(parsed_items)}件** の商品が検出されています。")
            
            for idx, item in enumerate(parsed_items):
                with st.expander(f"📦 [{idx+1}] {item['name']} (¥{item['price']}) - {item['match_status']}"):
                    st.write(f"**更新日:** {item['update_date']}")
                    st.write(f"**型番/JAN:** {item['model_number']}")
                    st.write(f"**照合結果:** {item['match_status']}")

            if st.button("🚀 抽出された全データをスプレッドシートへ一括書き込み", type="primary"):
                with st.spinner("スプレッドシートへ書き込み中..."):
                    try:
                        gc = get_gspread_client()
                        master_db = load_master_db(gc)
                        
                        for item in parsed_items:
                            model_num = item["model_number"]
                            if model_num in master_db:
                                official_name = master_db[model_num]
                                item["match_status"] = f"一致 (マスター名: {official_name})"
                            else:
                                item["match_status"] = "マスター未登録"
                                
                            append_to_result_sheet(gc, item)
                            
                        st.success("🎉 すべてのデータの書き込みが完了しました！")
                        
                        if image_path.exists():
                            image_path.rename(DONE_DIR / image_path.name)
                        selected_json.rename(DONE_DIR / selected_json.name)
                        
                        del st.session_state[session_key]
                        st.rerun()
                        
                    except Exception as e:
                        st.error(f"❌ 書き込みエラー: {e}")
            
            st.markdown("---")

        today_str = datetime.now().strftime("%Y-%m-%d")
        default_name = parsed_items[0]["name"] if parsed_items else (meta.get("name", tweet_text[:30] if tweet_text else ""))
        default_price = parsed_items[0]["price"] if parsed_items else ""
        default_date = parsed_items[0]["update_date"] if parsed_items else today_str
        default_model = parsed_items[0]["model_number"] if parsed_items else ""

        with st.form(key=f"form_{tweet_id}"):
            edited_name = st.text_input("商品名", value=default_name)
            edited_price = st.text_input("価格 (例: 2500 または 2,500)", value=str(default_price))
            edited_date = st.text_input("更新日 (YYYY-MM-DD)", value=default_date)
            edited_model = st.text_input("型番 / JAN", value=str(default_model))
            
            st.markdown("---")
            
            col_btn1, col_btn2 = st.columns(2)
            with col_btn1:
                approve_btn = st.form_submit_button("✅ この1件をスプレッドシートへ転記", type="primary")
            with col_btn2:
                skip_btn = st.form_submit_button("🗑️ このデータをスキップ（削除）")

            if approve_btn:
                if not edited_name:
                    st.error("❌ 商品名を入力してください。")
                else:
                    try:
                        gc = get_gspread_client()
                        master_db = load_master_db(gc)
                        
                        match_status = "未照合"
                        if edited_model in master_db:
                            official_name = master_db[model_num]
                            match_status = f"一致 (マスター名: {official_name})"
                        else:
                            match_status = "マスター未登録"

                        row_data = {
                            "name": edited_name,
                            "price": edited_price,
                            "update_date": edited_date,
                            "model_number": edited_model,
                            "match_status": match_status
                        }
                        
                        append_to_result_sheet(gc, row_data)
                        st.success("🎉 スプレッドシートへの転記が完了しました！")
                        
                        if image_path.exists():
                            image_path.rename(DONE_DIR / image_path.name)
                        selected_json.rename(DONE_DIR / selected_json.name)
                        
                        if session_key in st.session_state:
                            del st.session_state[session_key]
                            
                        st.rerun()
                        
                    except Exception as e:
                        st.error(f"❌ 転記エラーが発生しました: {e}")

            if skip_btn:
                if image_path.exists():
                    image_path.rename(DONE_DIR / image_path.name)
                selected_json.rename(DONE_DIR / selected_json.name)
                if session_key in st.session_state:
                    del st.session_state[session_key]
                st.warning("⚠️ このデータをスキップしました（キューから除外）。")
                st.rerun()
