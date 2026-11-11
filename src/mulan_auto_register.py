"""
ムーラン買取データの自動登録（GitHub Actions で OCR の直後に動かす）。

data/mulan_queue/ の新しいデータについて、各商品名を参照リスト（入荷カレンダー・登録済みの商品名・
ポスト本文）と照らし合わせ、綴りに間違いが無いと判定できた商品だけをスプレッドシートに書き込む。
判定できなかった商品はそのままキューに残し、ダッシュボードに「読み間違いの疑い」と置き換え候補を出す。

  python src/mulan_auto_register.py            # 通常（新しいデータだけ・シートに書く）
  python src/mulan_auto_register.py --dry-run  # 判定だけ表示（シートもJSONも変えない）
  python src/mulan_auto_register.py --dry-run --all  # 古いデータも含めて判定だけ試す
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import gspread
import requests

import unicodedata

from mulan_name_check import (
    build_refs, build_vocab, check_name, match_by_model, refs_from_text, same_item_name, strip_headings,
)
from mulan_sheet import (
    DEFAULT_STORE, STORE_SHEETS, get_gspread_client, get_result_sheet,
    item_key, keys_from_rows, read_rows, sheet_name_for,
)

BASE_DIR = Path(__file__).resolve().parent.parent
QUEUE_DIR = BASE_DIR / "data" / "mulan_queue"
JST = timezone(timedelta(hours=9))

# これより古いポストのデータは自動登録しない（ダッシュボードで手直し・スキップ済みのものを登録し直さないため）
MAX_AGE_DAYS = float(os.environ.get("MULAN_AUTO_MAX_AGE_DAYS", "3"))

_raw_cal = os.environ.get("SPREADSHEET_ID_CALENDAR", "")
_m = re.search(r"([a-zA-Z0-9-_]{25,})", _raw_cal)
CALENDAR_SPREADSHEET_ID = _m.group(1) if _m else _raw_cal.strip()
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

_PRICE_RE = re.compile(r"^\d{2,7}$")
_DATE_RE = re.compile(r"^\d{4}/\d{1,2}/\d{1,2}$")


def post_datetime(meta):
    posted = str(meta.get("posted_at") or "")
    try:
        return datetime.strptime(posted, "%Y/%m/%d %H:%M").replace(tzinfo=JST)
    except ValueError:
        pass
    try:
        tid = int(str(meta.get("original_tweet_id") or meta.get("tweet_id", "")).split("_")[0])
        return datetime.fromtimestamp(((tid >> 22) + 1288834974657) / 1000, tz=JST)
    except ValueError:
        return None


def calendar_names():
    """入荷カレンダーの「入荷予定」（D列）と「入荷報告」（B列）の商品名"""
    if not CALENDAR_SPREADSHEET_ID:
        print("SPREADSHEET_ID_CALENDAR が未設定なので、入荷カレンダーは参照しません")
        return []
    try:
        sa_key = os.environ.get("GCP_SA_KEY", "").strip()
        gc = gspread.service_account_from_dict(json.loads(sa_key)) if sa_key else get_gspread_client()
        ss = gc.open_by_key(CALENDAR_SPREADSHEET_ID)
    except Exception as e:
        print(f"⚠️ 入荷カレンダーを開けませんでした（参照せずに続けます）: {e}")
        return []
    out = []
    for tab, col in (("入荷予定", 3), ("入荷報告", 1)):
        try:
            rows = ss.worksheet(tab).get_all_values()[1:]
        except Exception as e:
            print(f"⚠️ 入荷カレンダーの「{tab}」を読めませんでした: {e}")
            continue
        out += [(r[col], f"入荷カレンダー/{tab}") for r in rows if len(r) > col and r[col].strip()]
    print(f"入荷カレンダー: {len(out)} 件")
    return out


def trusted_sheet_names(rows, source, before=None):
    """
    シートに登録済みの商品名のうち、参照名として信用できるもの。
    シートにもOCRの読み間違いがそのまま登録されていることがあるため、
    同じ表記が「別々の更新日」に2回以上登録されている商品名だけを使う。
    before を渡すと、その日付より前に登録されたものだけを数える（判定の試験用）。
    """
    dates = {}
    for r in rows:
        name, date = r[1].strip(), r[3].strip()
        if not name or (before and date and date >= before):
            continue
        dates.setdefault(name, set()).add(date)
    return [(n, source) for n, ds in dates.items() if len(ds) >= 2]


def model_key(value):
    """型番/JANの比較用（短すぎるものは別の商品と当たりやすいので使わない）"""
    s = re.sub(r"[\s\-‐－]", "", unicodedata.normalize("NFKC", str(value or ""))).upper()
    return s if len(s) >= 4 else ""


def normalize_price(value):
    return re.sub(r"[,，円¥\s]", "", str(value or ""))


def send_discord(message):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        requests.post(DISCORD_WEBHOOK_URL, json={"content": message[:1900]}, timeout=10)
    except Exception as e:
        print(f"Discord通知エラー: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="判定だけ表示し、シートもJSONも変えない")
    ap.add_argument("--all", action="store_true", help="自動判定済み・古いデータも対象にする")
    args = ap.parse_args()

    now = datetime.now(JST)
    targets = []
    for path in sorted(QUEUE_DIR.glob("*.json")):
        meta = json.loads(path.read_text(encoding="utf-8"))
        if not args.all:
            if meta.get("auto_checked_at"):
                continue
            dt = post_datetime(meta)
            if dt is None or now - dt > timedelta(days=MAX_AGE_DAYS):
                continue
        if any(not it.get("_registered") for it in meta.get("parsed_items", [])):
            targets.append((path, meta))

    if not targets:
        print("自動登録の対象となる新しいデータはありません")
        return 0
    print(f"対象: {len(targets)} ファイル")

    gc = get_gspread_client()
    # 店舗ごとのシートを読み、登録済みの商品名を参照リストに、キーを二重登録防止に使う
    sheets, keys, next_rows = {}, {}, {}
    name_dates = {}  # 店舗ごとの（商品名, 更新日）。型番が空の行の二重登録防止に使う
    price_rows = {}  # 店舗ごと・更新日ごとの（商品名, 価格）。名前を手で直して登録済みの行の検出に使う
    model_names = {}  # 型番/JAN → シートに登録済みの商品名
    shared = calendar_names()
    for store in sorted({(m.get("store") or DEFAULT_STORE) for _, m in targets} | set(STORE_SHEETS)):
        sheet = get_result_sheet(gc, store)
        rows = read_rows(sheet)
        sheets[store] = sheet
        keys[store] = keys_from_rows(rows)
        name_dates[store] = {(k[0], k[2]) for k in keys[store]}
        price_rows[store] = {}
        for r in rows:
            if r[1].strip():
                k = item_key(r[1], "", r[3])
                price_rows[store].setdefault(k[2], []).append((r[1], normalize_price(r[2])))
        next_rows[store] = len(sheet.col_values(2)) + 1
        shared += trusted_sheet_names(rows, f"登録済み/{sheet_name_for(store)}")
        # 型番/JAN → 登録済みの商品名（手で直した名前を引くのに使う。店舗をまたいで共通）
        for r in rows:
            m = model_key(r[4])
            if m and r[1].strip():
                model_names.setdefault(m, {}).setdefault(r[1].strip(), set()).add(r[3].strip())
    base_refs = shared
    vocab = build_vocab(build_refs(base_refs))

    summary = []
    for path, meta in targets:
        store = meta.get("store") or DEFAULT_STORE
        refs = build_refs(refs_from_text(meta.get("text", "")) + base_refs)
        to_write, auto_n, suspect_n, unknown_n = [], 0, 0, 0

        for it in meta.get("parsed_items", []):
            if it.get("_registered"):
                continue
            result = check_name(it.get("name", ""), refs)
            # 型番/JANが同じ商品がシートに登録済みなら、その名前（手で直した名前）を使う
            model_hit = None
            m = model_key(it.get("model_number"))
            if m and m in model_names:
                model_hit = match_by_model(
                    it.get("name", ""), [(n, len(ds)) for n, ds in model_names[m].items()], vocab)
            if model_hit:
                result = dict(result, status="verified", ref=model_hit[0], source="型番/JAN一致",
                              score=1.0, suggestion="",
                              reason=f"型番/JAN {it.get('model_number')} で登録済みの「{model_hit[0]}」と一致")
            it["_check"] = result
            price = normalize_price(it.get("price"))
            date_ok = bool(_DATE_RE.match(str(it.get("update_date") or "")))
            if result["status"] == "verified" and _PRICE_RE.match(price) and date_ok:
                # 型番/JANで引けた名前があればそれを、無ければ先頭の見出し（パレード など）を外した名前を書く
                name = model_hit[0] if model_hit else strip_headings(it.get("name", ""))
                if model_hit and name != it.get("name"):
                    it["_ocr_name"] = it.get("name")
                    if args.dry_run:
                        print(f"[dry-run:model] {it.get('name')}  →  {name}  ({it.get('model_number')})")
                model, date = it.get("model_number", ""), it.get("update_date", "")
                # 二重登録の判定:
                #  ・見出しを外す前の名前でも調べる（ダッシュボードでOCRの名前のまま手で登録済みのことがある）
                #  ・型番が片方だけ空でも同じ商品とみなす（OCRが画像の重なり部分を2回読み、片方だけ型番を拾うことがある）
                #  ・型番が違う同名の商品は別の商品として登録する
                dup = False
                for n in {name, it.get("name", "")}:
                    k = item_key(n, model, date)
                    nd = (k[0], k[2])
                    if k in keys[store]:
                        dup = True  # 名前・型番・更新日が同じ
                    elif k[1] == "" and nd in name_dates[store]:
                        dup = True  # 型番なし ↔ 同名・同日の行がある
                    elif item_key(n, "", date) in keys[store]:
                        dup = True  # 型番あり ↔ 同名・同日で型番なしの行がある
                if args.all and (item_key(name, "", date)[0], item_key("", "", date)[2]) in name_dates[store]:
                    dup = True  # 過去分の一括登録では、同名・同日の行があれば型番が違っても登録しない
                if not dup:
                    # 同じ日・同じ価格で、表記ゆれだけが違う名前の行がある（手で直した名前で登録済み）なら登録しない
                    for other_name, other_price in price_rows[store].get(item_key("", "", date)[2], []):
                        if other_price == price and same_item_name(name, other_name):
                            dup = True
                            if args.dry_run:
                                print(f"[dry-run:skip-similar] {name} / {price}  ≒ {other_name}")
                            break
                if not dup:
                    to_write.append([name, price, date, model])
                k = item_key(name, model, date)
                keys[store].add(k)
                name_dates[store].add((k[0], k[2]))
                price_rows[store].setdefault(k[2], []).append((name, price))
                it["name"] = name
                it["price"] = price
                it["_registered"] = True
                it["_auto_registered"] = True
                auto_n += 1
            elif result["status"] == "suspect":
                suspect_n += 1
            else:
                unknown_n += 1
            mark = "✅" if it.get("_auto_registered") else ("⚠️" if result["status"] == "suspect" else "❔")
            print(f"  {mark} {it.get('name')} / {it.get('price')}  {result['reason']}"
                  + (f"  → 候補: {result['suggestion']}" if result["suggestion"] else ""))

        if args.dry_run:
            print(f"[dry-run] {path.name}: 自動登録 {auto_n}（シートに追加 {len(to_write)} 行） / 疑い {suspect_n} / 不明 {unknown_n}")
            for row in to_write:
                print(f"[dry-run:write] {store}\t" + "\t".join(str(v) for v in row))
            continue

        if to_write:
            sheet = sheets[store]
            start = next_rows[store]
            end = start + len(to_write) - 1
            if end > sheet.row_count:
                sheet.add_rows(end - sheet.row_count + 100)
            sheet.update(range_name=f"B{start}:E{end}", values=to_write)
            next_rows[store] = end + 1

        meta["auto_checked_at"] = now.strftime("%Y/%m/%d %H:%M")
        path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"{path.name}: 自動登録 {auto_n} 件（シートに {len(to_write)} 行追加） / 要確認 {suspect_n + unknown_n} 件")
        summary.append(f"・{store} {meta.get('posted_at', '')}: 自動登録 {auto_n} 件 / 要確認 {suspect_n + unknown_n} 件"
                       + (f"（読み間違いの疑い {suspect_n} 件）" if suspect_n else ""))

    if summary and not args.dry_run:
        send_discord("🤖 **ムーラン買取データ 自動登録**\n" + "\n".join(summary)
                     + "\n要確認の商品はダッシュボードで確認してください。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
