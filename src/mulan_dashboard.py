import streamlit as st
import os
import json
import tempfile
from pathlib import Path
from PIL import Image
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

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
st.markdown("GitHub Actions側で事前解析された買取データをプレビューしながら、スムーズにスプレッドシートへ登録できます。")

# --- 設定値（環境変数から取得） ---
TARGET_SPREADSHEET_ID = os.environ.get("TARGET_SPREADSHEET_ID", "1EQooFe5QbdCDe1wJj_lI8rxKoQFpre4E-1WbiPyJPx4")
RESULT_SHEET_NAME = os.environ.get("RESULT_SHEET_NAME", "sheet1")

# --- 2. スプレッドシート書き込み関数 ---
def get_gspread_client():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    
    creds_json_str = os.environ.get("GCP_CREDENTIALS_JSON", "")
    if creds_json_str:
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as temp:
                temp.write(creds_json_str)
                temp_path = temp.name
            creds = Credentials.from_service_account_file(temp_path, scopes=scope)
            return gspread.authorize(creds)
        except Exception as e:
            st.error(f"環境変数のJSON解析エラー: {e}")

    creds_path = BASE_DIR / "credentials.json"
    if not creds_path.exists():
        alt_path = Path(r"C:\Users\chukyotokukai\Documents\rashinban\credentials.json")
        if alt_path.exists():
            creds_path = alt_path
        else:
            raise FileNotFoundError(f"認証ファイルが見つかりません。Renderの場合は環境変数 `GCP_CREDENTIALS_JSON` を設定してください。")
            
    creds = Credentials.from_service_account_file(creds_path, scopes=scope)
    return gspread.authorize(creds)

def append_to_result_sheet(client_gspread, row_data):
    try:
        spreadsheet = client_gspread.open_by_key(TARGET_SPREADSHEET_ID)
        try:
            sheet = spreadsheet.worksheet(RESULT_SHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            sheet = spreadsheet.add_worksheet(title=RESULT_SHEET_NAME, rows=1000, cols=10)
            sheet.append_row(["商品名", "価格", "更新日", "型番/JAN"])
            
        sheet.append_row([
            row_data["name"],
            row_data["price"],
            row_data["update_date"],
            row_data["model_number"]
        ])
    except Exception as e:
        raise Exception(f"結果シートへの書き込みエラー: {e}")

# --- 3. 未処理キューの読み込み ---
if not QUEUE_DIR.exists():
    st.info("`data/mulan_queue/` フォルダを準備中です。")
    st.stop()

json_files = sorted(list(QUEUE_DIR.glob("*.json")))

if not json_files:
    st.info("🎉 現在、未処理の買取データはありません。GitHub Actionsによるデータ収集・OCR完了をお待ちください。")
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

    parsed_items = meta.get("parsed_items", [])

    # 一括登録・スキップを実行する関数
    def execute_batch_save(indices):
        if not indices:
            st.warning("⚠️ 登録する項目が選択されていません。")
            return
        try:
            gc = get_gspread_client()
            for idx in indices:
                item = parsed_items[idx]
                append_to_result_sheet(gc, item)
                
            st.success(f"🎉 選択された {len(indices)} 件の登録が完了しました！")
            
            if image_path.exists():
                image_path.rename(DONE_DIR / image_path.name)
            selected_json.rename(DONE_DIR / selected_json.name)
            
            st.rerun()
            
        except Exception as e:
            st.error(f"❌ 一括登録エラー: {e}")

    def execute_skip():
        if image_path.exists():
            image_path.rename(DONE_DIR / image_path.name)
        selected_json.rename(DONE_DIR / selected_json.name)
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

    with col_list:
        st.subheader("✍️ 事前解析データ確認・個別/チェック一括登録")
        
        if not parsed_items:
            st.warning("⚠️ このJSONには事前解析データ（parsed_items）が含まれていません。GitHub Actions側のOCRスクリプトを確認してください。")
            st.json(meta)
        else:
            st.markdown(f"📌 GitHub Actions側で解析された **{len(parsed_items)}件** のデータです。各行で修正・個別登録が可能です。")
            
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
                        st.markdown(f"**[{idx+1}] 登録用データ**")

                    c1, c2 = st.columns([2, 1])
                    c3, c4 = st.columns([1, 1])
                    
                    with c1:
                        new_name = st.text_input("商品名", value=item.get("name", ""), key=f"name_{tweet_id}_{idx}")
                    with c2:
                        new_price = st.text_input("価格", value=item.get("price", ""), key=f"price_{tweet_id}_{idx}")
                    with c3:
                        new_date = st.text_input("更新日", value=item.get("update_date", ""), key=f"date_{tweet_id}_{idx}")
                    with c4:
                        new_model = st.text_input("型番/JAN", value=item.get("model_number", ""), key=f"model_{tweet_id}_{idx}")

                    parsed_items[idx]["name"] = new_name
                    parsed_items[idx]["price"] = new_price
                    parsed_items[idx]["update_date"] = new_date
                    parsed_items[idx]["model_number"] = new_model

                    if st.button(f"✅ この [{idx+1}] 件だけを登録", key=f"single_btn_{tweet_id}_{idx}"):
                        try:
                            gc = get_gspread_client()
                            single_data = {
                                "name": new_name,
                                "price": new_price,
                                "update_date": new_date,
                                "model_number": new_model
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
