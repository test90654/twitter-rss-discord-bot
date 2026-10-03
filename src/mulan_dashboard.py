import streamlit as st
import os
import json
from pathlib import Path
from PIL import Image
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials

# --- 1. 初期設定・パス設定 ---
# src/ フォルダ内にあるため、parent.parent でプロジェクトのルート（一番上の階層）を指定
BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "data" / "mulan_queue"
DONE_DIR = BASE_DIR / "data" / "mulan_done"

# 安全にディレクトリを自動作成
try:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    DONE_DIR.mkdir(parents=True, exist_ok=True)
except Exception as e:
    st.warning(f"ディレクトリの作成中に警告が発生しました: {e}")

# ページタイトル（ブラウザのタブに表示される名前）を設定
st.set_page_config(
    page_title="ムーラン買取データ 承認ダッシュボード",
    page_icon="📦",
    layout="wide"
)

# 画面上のメインタイトル
st.title("📦 ムーラン買取データ 承認ダッシュボード")
st.markdown("X（Twitter）から自動収集した買取表画像を確認し、スプレッドシートへスムーズに転記するための管理画面です。")

# --- 2. Googleスプレッドシート接続関数 ---
def append_to_sheet(row_data):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    
    creds_path = BASE_DIR / "credentials.json" 
    if not creds_path.exists():
        raise FileNotFoundError(f"認証ファイルが見つかりません: {creds_path}")
        
    creds = Credentials.from_service_account_file(creds_path, scopes=scope)
    client = gspread.authorize(creds)
    
    sheet_key = os.environ.get("GOOGLE_SHEET_KEY", "YOUR_SPREADSHEET_KEY_HERE")
    sheet = client.open_by_key(sheet_key).sheet1
    
    # B列〜E列に対応するデータを追加 [商品名, 価格, 更新日, 型番]
    sheet.append_row([
        row_data["name"],
        row_data["price"],
        row_data["update_date"],
        row_data["model_number"]
    ])

# --- 3. 未処理キューの読み込み ---
if not QUEUE_DIR.exists():
    st.info("`data/mulan_queue/` フォルダを準備中です。")
    st.stop()

json_files = sorted(list(QUEUE_DIR.glob("*.json")))

if not json_files:
    st.info("🎉 現在、未処理の買取データはありません。GitHub Actionsによるデータ収集をお待ちください。")
    st.stop()

# サイドバーで処理するアイテムを選択
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

    # 画面を2分割（左：画像＆元ツイート、右：編集フォーム）
    col1, col2 = st.columns([1, 1], gap="large")

    with col1:
        st.subheader("📷 買取表プレビュー")
        if image_path.exists():
            img = Image.open(image_path)
            st.image(img, use_column_width=True)
        else:
            st.error(f"画像ファイルが見つかりません: {image_filename}")

        with st.expander("🐦 元ツイートの本文とリンク"):
            st.markdown(tweet_text)
            st.markdown(f"[🔗 X(Twitter)で元ツイートを開く]({tweet_url})")

    with col2:
        st.subheader("✍️ データ確認・修正・承認")
        st.markdown("画像を参考にしながら商品情報を入力・確認し、スプレッドシートへ転記してください。")
        
        today_str = datetime.now().strftime("%Y-%m-%d")

        with st.form(key=f"form_{tweet_id}"):
            edited_name = st.text_input("商品名", value="")
            edited_price = st.text_input("価格 (例: 2500 または 2,500)", value="")
            edited_date = st.text_input("更新日 (YYYY-MM-DD)", value=today_str)
            edited_model = st.text_input("型番", value="")
            
            st.markdown("---")
            
            col_btn1, col_btn2 = st.columns(2)
            with col_btn1:
                approve_btn = st.form_submit_button("✅ 承認してスプレッドシートへ転記", type="primary")
            with col_btn2:
                skip_btn = st.form_submit_button("🗑️ このデータをスキップ（削除）")

            if approve_btn:
                if not edited_name:
                    st.error("❌ 商品名を入力してください。")
                else:
                    row_data = {
                        "name": edited_name,
                        "price": edited_price,
                        "update_date": edited_date,
                        "model_number": edited_model
                    }
                    
                    try:
                        append_to_sheet(row_data)
                        st.success("🎉 スプレッドシートへの転記が完了しました！")
                        
                        if image_path.exists():
                            image_path.rename(DONE_DIR / image_path.name)
                        selected_json.rename(DONE_DIR / selected_json.name)
                        
                        st.rerun()
                        
                    except Exception as e:
                        st.error(f"❌ 転記エラーが発生しました: {e}")

            if skip_btn:
                if image_path.exists():
                    image_path.rename(DONE_DIR / image_path.name)
                selected_json.rename(DONE_DIR / selected_json.name)
                st.warning("⚠️ このデータをスキップしました（キューから除外）。")
                st.rerun()
