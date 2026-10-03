import streamlit as st
import os
import json
import tempfile
from pathlib import Path
from PIL import Image
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import time

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

# --- 4. サイドバー：復元（アンドゥ）エリアの設置 ---
st.sidebar.title("📋 未処理キュー一覧")
done_files = sorted(list(DONE_DIR.glob("*.json")), key=lambda x: x.stat().st_mtime, reverse=True)

if done_files:
    st.sidebar.markdown("---")
    st.sidebar.subheader("↩️ 直近の完了データを復元 (アンドゥ)")
    restore_target = st.sidebar.selectbox("復元するデータを選択", done_files, format_func=lambda x: x.name)
    if st.sidebar.button("♻️ 選択したデータを未処理に戻す"):
        try:
            meta_path = DONE_DIR / restore_target.name
            with open(meta_path, "r", encoding="utf-8") as f:
                meta_data = json.load(f)
            img_filename = meta_data.get("image_file")
            
            meta_path.rename(QUEUE_DIR / restore_target.name)
            if img_filename:
                done_img = DONE_DIR / img_filename
                if done_img.exists():
                    done_img.rename(QUEUE_DIR / img_filename)
                    
            st.sidebar.success(f"🎉 '{restore_target.name}' をキューに復元しました！")
            time.sleep(1)
            st.rerun()
        except Exception as e:
            st.sidebar.error(f"復元エラー: {e}")

st.sidebar.markdown("---")

# --- 3. 未処理キューの読み込み ---
if not QUEUE_DIR.exists():
    st.info("`data/mulan_queue/` フォルダを準備中です。")
    st.stop()

json_files = sorted(list(QUEUE_DIR.glob("*.json")))

if not json_files:
    st.info("🎉 現在、未処理の買取データはありません。GitHub Actionsによるデータ収集・OCR完了をお待ちください。")
    st.stop()

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

    items_session_key = f"items_{tweet_id}"
    if items_session_key not in st.session_state:
        st.session_state[items_session_key] = meta.get("parsed_items", [])

    parsed_items = st.session_state[items_session_key]

    # 完了判定（リストが空になったら、あるいは一括登録ですべて処理されたらDONEへ移動して次へ進む）
    def check_and_complete_if_empty():
        if not st.session_state[items_session_key]:
            if image_path.exists():
                image_path.rename(DONE_DIR / image_path.name)
            selected_json.rename(DONE_DIR / selected_json.name)
            st.toast("🎉 このデータの全項目の登録が完了しました！次のデータへ進みます。", icon="🚀")
            time.sleep(1)
            st.rerun()

    def execute_batch_save(indices_to_save):
        if not indices_to_save:
            st.warning("⚠️ 登録する項目が選択されていません。")
            return
        try:
            gc = get_gspread_client()
            sorted_indices = sorted(indices_to_save, reverse=True)
            
            for idx in sorted_indices:
                item = parsed_items[idx]
                append_to_result_sheet(gc, item)
                parsed_items.pop(idx)
                
            st.session_state[items_session_key] = parsed_items
            st.toast(f"🎉 選択された {len(indices_to_save)} 件を登録しました！", icon="✅")
            
            meta["parsed_items"] = parsed_items
            with open(selected_json, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            # ★ 一括登録した結果、リストが空になったか、あるいはすべて処理し終えたとみなして完了扱いにしたい場合、
            # もし「一括登録ボタンを押したら残りがあっても強制的にこのデータを完了にして次に進む」仕様にする場合はここで完了処理を呼びます。
            # 今回は「リストに残っている全アイテムを一括登録した時（またはチェックされたものが全件だった時）」に自動完了させるか、
            # あるいは「チェックしたものを一括登録したら、このデータ自体を完了（DONE）にして次のファイルへ進む」挙動にします。
            
            # チェックされた項目が全件、もしくはリスト全体を処理した場合は完了フォルダへ移動
            if not parsed_items or len(indices_to_save) >= len(parsed_items) or len(parsed_items) == 0:
                if image_path.exists():
                    image_path.rename(DONE_DIR / image_path.name)
                selected_json.rename(DONE_DIR / selected_json.name)
                if items_session_key in st.session_state:
                    del st.session_state[items_session_key]
                st.toast("🎉 このデータの処理が完了しました！次のデータへ進みます。", icon="🚀")
                time.sleep(1)
                st.rerun()
            else:
                st.rerun()
            
        except Exception as e:
            st.error(f"❌ 一括登録エラー: {e}")

    def execute_skip():
        if image_path.exists():
            image_path.rename(DONE_DIR / image_path.name)
        selected_json.rename(DONE_DIR / selected_json.name)
        if items_session_key in st.session_state:
            del st.session_state[items_session_key]
        st.warning("⚠️ このデータをスキップしました（キューから除外）。")
        st.rerun()

    if parsed_items:
        st.sidebar.markdown("### 🚀 一括操作パネル")
        if st.sidebar.button("🚀 チェックした項目を一括登録", type="primary", key="sb_batch_btn"):
            indices = [i for i in range(len(parsed_items)) if st.session_state.get(f"chk_{tweet_id}_{i}", True)]
            execute_batch_save(indices)
            
        if st.sidebar.button("🗑️ このデータを丸ごとスキップ", key="sb_skip_btn"):
            execute_skip()

    col_img, col_list = st.columns([1, 1.3], gap="large")

    with col_img:
        st.subheader("📷 買取表プレビュー")
        if image_path.exists():
            img = Image.open(image_path)
            st.image(img, width=650)
        else:
            st.error(f"画像ファイルが見つかりません: {image_filename}")

        with st.expander("🐦 元ツイートの本文とリンク"):
            st.markdown(tweet_text)
            st.markdown(f"[🔗 X(Twitter)で元ツイートを開く]({tweet_url})")

    with col_list:
        st.subheader(f"✍️️ 事前解析データ確認 (残り: {len(parsed_items)}件)")
        
        if not parsed_items:
            st.success("✨ すべての項目が登録されました！自動的に次のデータへ移動します...")
            check_and_complete_if_empty()
        else:
            # --- 📄 ページネーション処理 (1ページあたり5件表示) ---
            ITEMS_PER_PAGE = 5
            total_items = len(parsed_items)
            total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
            
            page_key = f"page_{tweet_id}"
            if page_key not in st.session_state:
                st.session_state[page_key] = 0
            
            if st.session_state[page_key] >= total_pages:
                st.session_state[page_key] = total_pages - 1

            # --- 🔄 ページネーションバー（上部） ---
            c_p1, c_p2, c_p3 = st.columns([1, 2, 1])
            with c_p1:
                if st.button("◀ 前へ", key=f"prev_top_{tweet_id}", disabled=(st.session_state[page_key] == 0)):
                    st.session_state[page_key] -= 1
                    st.rerun()
            with c_p2:
                st.markdown(f"<div style='text-align: center; font-weight: bold;'>ページ {st.session_state[page_key] + 1} / {total_pages} (全 {total_items} 件)</div>", unsafe_allow_html=True)
            with c_p3:
                if st.button("次へ ▶", key=f"next_top_{tweet_id}", disabled=(st.session_state[page_key] >= total_pages - 1)):
                    st.session_state[page_key] += 1
                    st.rerun()

            current_page = st.session_state[page_key]
            start_idx = current_page * ITEMS_PER_PAGE
            end_idx = min(start_idx + ITEMS_PER_PAGE, total_items)

            if st.button("☑ このページの項目をすべて選択する", key=f"select_all_{tweet_id}_{current_page}"):
                for i in range(start_idx, end_idx):
                    st.session_state[f"chk_{tweet_id}_{i}"] = True
                st.rerun()

            selected_indices = []
            
            # 現在のページの5件だけを描画
            for idx in range(start_idx, end_idx):
                item = parsed_items[idx]
                with st.container(border=True):
                    c_chk, c_status = st.columns([1, 3])
                    with c_chk:
                        is_checked = st.checkbox("選択", value=st.session_state.get(f"chk_{tweet_id}_{idx}", True), key=f"chk_{tweet_id}_{idx}")
                        if is_checked:
                            selected_indices.append(idx)
                    with c_status:
                        st.markdown(f"**[{idx+1}] 未登録アイテム**")

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
                            
                            parsed_items.pop(idx)
                            st.session_state[items_session_key] = parsed_items
                            
                            meta["parsed_items"] = parsed_items
                            with open(selected_json, "w", encoding="utf-8") as f:
                                json.dump(meta, f, ensure_ascii=False, indent=2)
                                
                            st.toast(f"✅ 商品 [{idx+1}] をスプレッドシートに登録しました！", icon="🎉")
                            check_and_complete_if_empty()
                            st.rerun()
                        except Exception as e:
                            st.error(f"❌ 登録エラー: {e}")

            # --- 🔄 ページネーションバー（下部） ---
            c_bp1, c_bp2, c_bp3 = st.columns([1, 2, 1])
            with c_bp1:
                if st.button("◀ 前へ", key=f"prev_bot_{tweet_id}", disabled=(st.session_state[page_key] == 0)):
                    st.session_state[page_key] -= 1
                    st.rerun()
            with c_bp2:
                st.markdown(f"<div style='text-align: center; font-weight: bold;'>ページ {st.session_state[page_key] + 1} / {total_pages}</div>", unsafe_allow_html=True)
            with c_bp3:
                if st.button("次へ ▶", key=f"next_bot_{tweet_id}", disabled=(st.session_state[page_key] >= total_pages - 1)):
                    st.session_state[page_key] += 1
                    st.rerun()

            st.markdown("---")
            
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                if st.button("🚀 チェックした項目を一括登録 (下部)", type="primary"):
                    execute_batch_save(selected_indices)
            with col_b2:
                if st.button("🗑️ このデータを丸ごとスキップ (下部)"):
                    execute_skip()
