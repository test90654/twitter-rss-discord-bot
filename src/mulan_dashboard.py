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
import re
import unicodedata

# --- 1. 初期設定・パス設定 ---
BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "data" / "mulan_queue"
DONE_DIR = BASE_DIR / "data" / "mulan_done"

ITEMS_PER_PAGE = 5  # 1ページあたりの表示件数

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

# --- 買取表プレビューをスクロールに追従させるCSS ---
# ポイント：
#  ・列は標準で行の高さいっぱいに引き伸ばされるため、そのままでは sticky が効かない
#    → align-self: flex-start で列の高さを中身ぶんに縮める
#  ・目印（.preview-sticky-marker）を含む列だけを対象にし、右側の入力欄の列には影響させない
#  ・画像が画面より縦に長い場合は、プレビュー列の中だけでスクロールできるようにする
#  ・Streamlitのバージョンによって列の data-testid が "stColumn" / "column" と異なるので両方指定
st.markdown(
    """
    <style>
    div[data-testid="stColumn"]:has(.preview-sticky-marker),
    div[data-testid="column"]:has(.preview-sticky-marker) {
        position: sticky;
        top: 4rem;                       /* 上部ヘッダーの下に固定 */
        align-self: flex-start;
        max-height: calc(100vh - 5rem);
        overflow-y: auto;
        z-index: 1;
    }
    /* スマホなど列が縦に並ぶ幅では追従しない */
    @media (max-width: 640px) {
        div[data-testid="stColumn"]:has(.preview-sticky-marker),
        div[data-testid="column"]:has(.preview-sticky-marker) {
            position: static;
            max-height: none;
            overflow-y: visible;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# rerun をまたいでトーストを表示するためのメッセージ
if "flash_msg" in st.session_state:
    msg, icon = st.session_state.pop("flash_msg")
    st.toast(msg, icon=icon)

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

def get_result_sheet(client_gspread):
    spreadsheet = client_gspread.open_by_key(TARGET_SPREADSHEET_ID)
    try:
        return spreadsheet.worksheet(RESULT_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        sheet = spreadsheet.add_worksheet(title=RESULT_SHEET_NAME, rows=1000, cols=10)
        sheet.update(range_name="A1:E1", values=[["ジャンル", "商品名", "価格", "更新日", "型番/JAN"]])
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

def fetch_registered_keys(client_gspread):
    """スプレッドシートに既に登録されている商品のキー一覧を取得（B:商品名 C:価格 D:更新日 E:型番/JAN）"""
    sheet = get_result_sheet(client_gspread)
    keys = set()
    for row in sheet.get_all_values()[1:]:  # 1行目は見出し
        row = row + [""] * (5 - len(row))
        name, _price, date, model = row[1], row[2], row[3], row[4]
        if name.strip():
            keys.add(item_key(name, model, date))
    return keys

def append_to_result_sheet(client_gspread, row_data):
    try:
        sheet = get_result_sheet(client_gspread)

        # A列はジャンル列なので触らず、B列（商品名）の最終行の次の行の B〜E 列に書き込む
        next_row = len(sheet.col_values(2)) + 1
        if next_row > sheet.row_count:
            sheet.add_rows(100)

        sheet.update(
            range_name=f"B{next_row}:E{next_row}",
            values=[[
                row_data["name"],
                row_data["price"],
                row_data["update_date"],
                row_data["model_number"]
            ]]
        )
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
        restored = False
        try:
            meta_path = DONE_DIR / restore_target.name
            with open(meta_path, "r", encoding="utf-8") as f:
                meta_data = json.load(f)
            img_filename = meta_data.get("image_file")

            # 復元時は「登録済み」の印を外して、もう一度確認できるようにする
            for it in meta_data.get("parsed_items", []):
                it["_registered"] = False
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_data, f, ensure_ascii=False, indent=2)
            
            meta_path.rename(QUEUE_DIR / restore_target.name)
            if img_filename:
                done_img = DONE_DIR / img_filename
                if done_img.exists():
                    done_img.rename(QUEUE_DIR / img_filename)

            # 古いセッション状態を破棄
            rid = meta_data.get("tweet_id")
            for k in [k for k in st.session_state.keys() if isinstance(k, str) and k.endswith(f"_{rid}") or (isinstance(k, str) and f"_{rid}_" in k)]:
                del st.session_state[k]

            st.session_state["flash_msg"] = (f"🎉 '{restore_target.name}' をキューに復元しました！", "♻️")
            restored = True
        except Exception as e:
            st.sidebar.error(f"復元エラー: {e}")
        if restored:
            st.rerun()

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
    sync_msg_key = f"sync_msg_{tweet_id}"

    def sync_with_sheet(items):
        """
        スプレッドシートと照合し、既に登録されている商品に「登録済み」の印を付ける。
        JSONファイルが消えたり古くなったりしても、シートを正として二重登録を防ぐ。
        戻り値：新たに登録済みと判定した件数
        """
        keys = fetch_registered_keys(get_gspread_client())
        newly = 0
        for it in items:
            if not it.get("_registered") and item_key(it.get("name"), it.get("model_number"), it.get("update_date")) in keys:
                it["_registered"] = True
                newly += 1
        return newly

    # データを開いた直後・リロード直後にスプレッドシートと照合する
    if items_session_key not in st.session_state:
        items = meta.get("parsed_items", [])
        try:
            newly = sync_with_sheet(items)
            if newly:
                meta["parsed_items"] = items
                with open(selected_json, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)
            st.session_state[sync_msg_key] = ("ok", newly)
        except Exception as e:
            st.session_state[sync_msg_key] = ("error", str(e))
        st.session_state[items_session_key] = items

    # ※ 登録したアイテムはリストから削除せず「_registered」の印を付ける。
    #   これでページ番号・アイテム番号が固定され、登録後に確実に次のページへ移動できる。
    parsed_items = st.session_state[items_session_key]
    total_items = len(parsed_items)
    total_pages = max(1, (total_items + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)

    page_key = f"page_{tweet_id}"

    def is_done(item):
        return bool(item.get("_registered", False))

    def first_pending_page():
        """登録済みをパスして、最初に未登録アイテムがあるページを返す"""
        for i, it in enumerate(parsed_items):
            if not is_done(it):
                return i // ITEMS_PER_PAGE
        return 0

    # リロード直後（セッションが新しい）やデータを開いた直後は、続きのページから始める
    if page_key not in st.session_state:
        st.session_state[page_key] = first_pending_page()
    if st.session_state[page_key] >= total_pages or st.session_state[page_key] < 0:
        st.session_state[page_key] = first_pending_page()

    def page_range(p):
        start = p * ITEMS_PER_PAGE
        return range(start, min(start + ITEMS_PER_PAGE, total_items))

    def page_has_pending(p):
        return any(not is_done(parsed_items[i]) for i in page_range(p))

    def next_pending_page(p):
        """p の次のページから順に、未登録アイテムが残っているページを探す（最後まで行ったら先頭に戻る）"""
        order = list(range(p + 1, total_pages)) + list(range(0, p + 1))
        for q in order:
            if page_has_pending(q):
                return q
        return p

    def pending_count():
        return sum(1 for it in parsed_items if not is_done(it))

    def current_values(idx):
        """入力欄の最新値を取得（サイドバーのボタンは入力欄の描画前に実行されるため、session_stateから直接読む）"""
        item = parsed_items[idx]
        return {
            "name": st.session_state.get(f"name_{tweet_id}_{idx}", item.get("name", "")),
            "price": st.session_state.get(f"price_{tweet_id}_{idx}", item.get("price", "")),
            "update_date": st.session_state.get(f"date_{tweet_id}_{idx}", item.get("update_date", "")),
            "model_number": st.session_state.get(f"model_{tweet_id}_{idx}", item.get("model_number", "")),
        }

    def save_meta_json():
        meta["parsed_items"] = parsed_items
        with open(selected_json, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

    def complete_and_move():
        """全アイテム登録済みならDONEフォルダへ移動"""
        if image_path.exists():
            image_path.rename(DONE_DIR / image_path.name)
        if selected_json.exists():
            selected_json.rename(DONE_DIR / selected_json.name)
        for k in [items_session_key, page_key]:
            if k in st.session_state:
                del st.session_state[k]
        st.session_state["flash_msg"] = ("🎉 このデータの全項目の登録が完了しました！次のデータへ進みます。", "🚀")

    def register_items(indices, move_page_always):
        """
        指定アイテムをスプレッドシートに登録し、ページを移動する。
        move_page_always=True  : 一括登録 → 必ず次のページへ
        move_page_always=False : 1件登録 → そのページの未登録がなくなったら次のページへ
        成功したら True を返す（st.rerun は try の外で呼ぶ）
        """
        if not indices:
            st.warning("⚠️ 登録する項目が選択されていません。")
            return False
        skipped = 0
        try:
            gc = get_gspread_client()
            # 書き込む直前にシートの最新状態を確認（他の端末・タブで登録済みのものは書かない）
            sheet_keys = fetch_registered_keys(gc)
            for idx in sorted(indices):
                vals = current_values(idx)
                key = item_key(vals["name"], vals["model_number"], vals["update_date"])
                if key in sheet_keys:
                    skipped += 1
                else:
                    append_to_result_sheet(gc, vals)
                    sheet_keys.add(key)
                parsed_items[idx].update(vals)
                parsed_items[idx]["_registered"] = True
                save_meta_json()  # 1件ごとに保存（途中でエラーになっても二重登録を防ぐ）
        except Exception as e:
            st.error(f"❌ 登録エラー: {e}")
            return False

        st.session_state[items_session_key] = parsed_items
        skip_note = f"（うち {skipped} 件はシートに登録済みだったので書き込みを省略）" if skipped else ""

        if pending_count() == 0:
            complete_and_move()
            return True

        cur = st.session_state[page_key]
        if move_page_always or not page_has_pending(cur):
            new_page = next_pending_page(cur)
            st.session_state[page_key] = new_page
            st.session_state["flash_msg"] = (f"✅ {len(indices)} 件を登録しました！{skip_note} ページ {new_page + 1} へ移動します。", "🎉")
        else:
            st.session_state["flash_msg"] = (f"✅ {len(indices)} 件を登録しました！{skip_note}", "🎉")
        return True

    def checked_pending_indices_on_page(p):
        return [
            i for i in page_range(p)
            if not is_done(parsed_items[i]) and st.session_state.get(f"chk_{tweet_id}_{i}", True)
        ]

    def execute_skip():
        if image_path.exists():
            image_path.rename(DONE_DIR / image_path.name)
        if selected_json.exists():
            selected_json.rename(DONE_DIR / selected_json.name)
        for k in [items_session_key, page_key]:
            if k in st.session_state:
                del st.session_state[k]
        st.session_state["flash_msg"] = ("⚠️ このデータをスキップしました（キューから除外）。", "🗑️")
        st.rerun()

    # データが空 or 全件登録済みなら完了扱い
    if pending_count() == 0:
        complete_and_move()
        if parsed_items:
            st.session_state["flash_msg"] = ("✅ このデータはすべてスプレッドシートに登録済みでした。次のデータへ進みます。", "🚀")
        st.rerun()

    # スプレッドシート照合の結果表示
    sync_result = st.session_state.get(sync_msg_key)
    if sync_result:
        status, detail = sync_result
        if status == "ok" and detail:
            st.info(f"🔎 スプレッドシートと照合し、{detail} 件を登録済みとしてパスしました。")
        elif status == "error":
            st.warning(f"⚠️ スプレッドシートとの照合に失敗しました（登録済み判定はこの端末の記録のみ）: {detail}")

    if st.sidebar.button("🔎 スプレッドシートと照合し直す", key="sb_resync_btn"):
        resync_ok = False
        try:
            newly = sync_with_sheet(parsed_items)
            save_meta_json()
            st.session_state[sync_msg_key] = ("ok", newly)
            st.session_state[page_key] = first_pending_page()
            st.session_state["flash_msg"] = (f"🔎 照合完了：新たに {newly} 件を登録済みと判定しました。", "✅")
            resync_ok = True
        except Exception as e:
            st.sidebar.error(f"照合エラー: {e}")
        if resync_ok:
            st.rerun()

    # --- サイドバー一括操作 ---
    st.sidebar.markdown("### 🚀 一括操作パネル")
    if st.sidebar.button("🚀 チェックした項目を一括登録", type="primary", key="sb_batch_btn"):
        if register_items(checked_pending_indices_on_page(st.session_state[page_key]), move_page_always=True):
            st.rerun()
    if st.sidebar.button("🗑️ このデータを丸ごとスキップ", key="sb_skip_btn"):
        execute_skip()

    col_img, col_list = st.columns([1, 1.3], gap="large")

    with col_img:
        # この目印を含む列だけをスクロール追従（sticky）させる
        st.markdown('<div class="preview-sticky-marker"></div>', unsafe_allow_html=True)
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
        st.subheader(f"✍️ 事前解析データ確認 (未登録: {pending_count()} / 全 {total_items} 件)")

        def go_prev():
            st.session_state[page_key] = max(0, st.session_state[page_key] - 1)

        def go_next():
            st.session_state[page_key] = min(total_pages - 1, st.session_state[page_key] + 1)

        cur_page = st.session_state[page_key]

        # --- 🔄 ページネーションバー（上部） ---
        c_p1, c_p2, c_p3 = st.columns([1, 2, 1])
        with c_p1:
            st.button("◀ 前へ", key=f"prev_top_{tweet_id}", disabled=(cur_page == 0), on_click=go_prev)
        with c_p2:
            st.markdown(f"<div style='text-align: center; font-weight: bold;'>ページ {cur_page + 1} / {total_pages}</div>", unsafe_allow_html=True)
        with c_p3:
            st.button("次へ ▶", key=f"next_top_{tweet_id}", disabled=(cur_page >= total_pages - 1), on_click=go_next)

        if st.button("☑ このページの項目をすべて選択する", key=f"select_all_{tweet_id}_{cur_page}"):
            for i in page_range(cur_page):
                st.session_state[f"chk_{tweet_id}_{i}"] = True
            st.rerun()

        # 現在のページの5件だけを描画
        for idx in page_range(cur_page):
            item = parsed_items[idx]

            # 登録済みアイテムはコンパクトに表示
            if is_done(item):
                st.success(f"[{idx+1}] ✅ 登録済み： {item.get('name', '')} / {item.get('price', '')}")
                continue

            with st.container(border=True):
                c_chk, c_status = st.columns([1, 3])
                with c_chk:
                    st.checkbox("選択", value=True, key=f"chk_{tweet_id}_{idx}")
                with c_status:
                    st.markdown(f"**[{idx+1}] 未登録アイテム**")

                c1, c2 = st.columns([2, 1])
                c3, c4 = st.columns([1, 1])
                
                with c1:
                    st.text_input("商品名", value=item.get("name", ""), key=f"name_{tweet_id}_{idx}")
                with c2:
                    st.text_input("価格", value=item.get("price", ""), key=f"price_{tweet_id}_{idx}")
                with c3:
                    st.text_input("更新日", value=item.get("update_date", ""), key=f"date_{tweet_id}_{idx}")
                with c4:
                    st.text_input("型番/JAN", value=item.get("model_number", ""), key=f"model_{tweet_id}_{idx}")

                if st.button(f"✅ この [{idx+1}] 件だけを登録", key=f"single_btn_{tweet_id}_{idx}"):
                    if register_items([idx], move_page_always=False):
                        st.rerun()

        # --- 🔄 ページネーションバー（下部） ---
        c_bp1, c_bp2, c_bp3 = st.columns([1, 2, 1])
        with c_bp1:
            st.button("◀ 前へ", key=f"prev_bot_{tweet_id}", disabled=(cur_page == 0), on_click=go_prev)
        with c_bp2:
            st.markdown(f"<div style='text-align: center; font-weight: bold;'>ページ {cur_page + 1} / {total_pages}</div>", unsafe_allow_html=True)
        with c_bp3:
            st.button("次へ ▶", key=f"next_bot_{tweet_id}", disabled=(cur_page >= total_pages - 1), on_click=go_next)

        st.markdown("---")
        
        col_b1, col_b2 = st.columns(2)
        with col_b1:
            if st.button("🚀 チェックした項目を一括登録 (下部)", type="primary", key=f"bottom_batch_{tweet_id}"):
                if register_items(checked_pending_indices_on_page(cur_page), move_page_always=True):
                    st.rerun()
        with col_b2:
            if st.button("🗑️ このデータを丸ごとスキップ (下部)", key=f"bottom_skip_{tweet_id}"):
                execute_skip()
