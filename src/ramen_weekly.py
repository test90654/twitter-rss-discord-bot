from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
import unicodedata

from google import genai
from google.genai import types
from pydantic import BaseModel

# ラーメン屋（@jyosho_yuki）の「今週の予定」画像を Gemini で読み取り、限定メニューの登場履歴を貯める。
#
# ・本文に「今週の予定」とある画像付きポストだけを読む（画像は黒板の「今週のプレミアム常勝軒」）
# ・週ごとの限定メニューを data/ramen_weekly.json に記録し、メニューごとの登場率を data/ramen_stats.md に出す
# ・data/ramen_watch.txt に書いたメニューが出た週は Discord に通知する
#   出ない週は「見送りOK」か「レア枠あり」かを判定して通知する
#
# 使い方:
#   python src/ramen_weekly.py                    # 新しいポストを確認（毎日の実行）
#   python src/ramen_weekly.py --backfill 400     # 過去の投稿をさかのぼって履歴を作る（初回）
#   python src/ramen_weekly.py --tweet <URL/ID>   # 特定のポストだけ読み直す
#   python src/ramen_weekly.py --stats            # 記録済みの履歴から登場率だけ出し直す
#   GEMINI_API_KEY=... python src/ramen_weekly.py --ocr-only 画像ファイル

# Xの取得・Gemini呼び出し・Discord通知は prize_calendar.py の部品を使い回す
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("CALENDAR_TARGET_USER", os.environ.get("RAMEN_TARGET_USER", "jyosho_yuki"))
import prize_calendar as pc  # noqa: E402

TARGET_USER = os.environ.get("RAMEN_TARGET_USER", "jyosho_yuki")
STORE_NAME = os.environ.get("RAMEN_STORE_NAME", "本庄常勝軒")
KEYWORD = "今週の予定"

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FILE = BASE_DIR / "data" / "ramen_weekly.json"
STATS_FILE = BASE_DIR / "data" / "ramen_stats.md"
WATCH_FILE = BASE_DIR / "data" / "ramen_watch.txt"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL_RAMEN") or os.environ.get("DISCORD_WEBHOOK_URL") or ""
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# 判定のしきい値
RARE_RATE = 0.25        # 登場率がこれ以下のメニューは「レア」
MIN_WEEKS_FOR_RATE = 4  # 履歴がこの週数未満なら確率は参考値扱い
WEEKDAYS = "月火水木金土日"


def post_url(post):
    return f"https://x.com/{TARGET_USER}/status/{post.get('id', '')}"


def normalize_menu(name):
    """表記ゆれ（全角半角・空白・飾り記号）を吸収した比較用のキー"""
    s = unicodedata.normalize("NFKC", str(name)).lower()
    s = re.sub(r"[\s・･\-ー〜~!！?？★☆◎○●◆◇■□※【】\[\]（）()「」『』]", "", s)
    s = re.sub(r"(限定|数量限定|新作|new)$", "", s)
    # カタカナはひらがなにそろえる（「ミソ」と「みそ」を同じに扱う）
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def week_monday(d):
    return d - timedelta(days=d.weekday())


# ============================================================
# 「今週の予定」画像を読み取る
# ============================================================
class MenuItem(BaseModel):
    name: str      # 黒板の表記のまま
    same_as: str   # 過去に出たメニューと同じものなら、その過去の名前（無ければ空）


class DayMenu(BaseModel):
    day: int              # 日にち（「9日」なら 9。読めなければ 0）
    weekday: str          # "月" など（無ければ空）
    closed: bool          # 休業日
    menus: list[MenuItem]  # その日に出す限定メニュー
    note: str             # 補足（無ければ空）


class WeeklySchedule(BaseModel):
    is_weekly_schedule: bool  # 週の予定表の画像か
    days: list[DayMenu]


OCR_PROMPT = """これはラーメン屋「本庄常勝軒」の、1週間の限定メニューの予定表の写真です。
黒板に「今週のプレミアム常勝軒」という見出しがあり、その下に「9日(月)」のような日付・曜日と、
「・赤とん」のようにその日の限定メニューが手書きで縦書きされています（ふつうは1日1品）。

- is_weekly_schedule: このような週の予定表なら true。ラーメンの写真や告知だけの画像なら false にして days は空にする
- days: 予定表の日ごとに1件（左から順に）
  - day: 日にちの数字（「9日」なら 9。読めなければ 0）
  - weekday: 曜日1文字（月・火・水・木・金・土・日）
  - closed: 休み・定休日・臨時休業なら true
  - menus: その日の限定メニュー
    - name: 黒板の表記のまま（先頭の「・」は除く。縦書きの改行はつなげる）
    - same_as: 下の「過去に出たメニュー」に同じメニュー（ひらがな・漢字の違い、「麺」の有無などの書き方の違いだけのもの）があれば、
      その名前をそのまま書く。無ければ空文字。似ているだけの別メニュー（味が違うなど）は空文字にする
  - note: 「売切」「夜のみ」などの補足（無ければ空文字）

注意:
- 黒板には前の週の消し跡がうっすら残っていることがある。はっきり書かれた白い文字だけを読み、薄い消し跡は無視する
- 読めない部分を推測で補わない

過去に出たメニュー:
{known}
"""


def ocr_config():
    kwargs = dict(response_mime_type="application/json", response_schema=WeeklySchedule, max_output_tokens=8192)
    try:
        return types.GenerateContentConfig(media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH, **kwargs)
    except Exception:
        return types.GenerateContentConfig(**kwargs)


def ocr_schedule(client, image_bytes, mime="image/jpeg", known=()):
    part = types.Part.from_bytes(data=image_bytes, mime_type=mime)
    prompt = OCR_PROMPT.format(known="\n".join(f"- {k}" for k in known) or "（まだ無し）")
    resp = pc._call_gemini(client, [part, prompt], ocr_config())
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, WeeklySchedule):
        return parsed
    text = re.sub(r"^```(?:json)?|```$", "", (getattr(resp, "text", "") or "").strip()).strip()
    return WeeklySchedule.model_validate(json.loads(text))


def resolve_week(sched, posted):
    """黒板の日にち・曜日と投稿日から、その週の月曜日を決める。
    黒板は日にちしか書かれていないので、投稿日の前後10日で日にちと曜日が合う日を探す"""
    posted_d = (posted or datetime.now(pc.JST)).date()
    best = None
    for d in sched.days:
        if not (1 <= d.day <= 31):
            continue
        for off in range(-10, 11):
            c = posted_d + timedelta(days=off)
            if c.day != d.day:
                continue
            if d.weekday and d.weekday in WEEKDAYS and WEEKDAYS[c.weekday()] != d.weekday:
                continue
            if best is None or abs(off) < abs(best[0]):
                best = (off, c)
        if best:
            return week_monday(best[1])
    # 日付が読めなければ投稿日の週（日曜の投稿は翌週分とみなす）
    return week_monday(posted_d + timedelta(days=1) if posted_d.weekday() == 6 else posted_d)


def day_dates(sched, monday):
    """各日の実際の日付（曜日から。曜日が無ければ並び順）"""
    out = []
    for i, d in enumerate(sched.days):
        idx = WEEKDAYS.index(d.weekday) if d.weekday and d.weekday in WEEKDAYS else i
        out.append((monday + timedelta(days=idx)).isoformat())
    return out


# ============================================================
# 履歴（data/ramen_weekly.json）
# ============================================================
def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"seen_ids": [], "weeks": {}}


def save_data(data):
    data["seen_ids"] = sorted(set(data["seen_ids"]), key=int)
    data["weeks"] = dict(sorted(data["weeks"].items()))
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def load_watch():
    if not WATCH_FILE.exists():
        return []
    lines = [l.split("#")[0].strip() for l in WATCH_FILE.read_text(encoding="utf-8").splitlines()]
    return [l for l in lines if l]


# 比較用キー → 黒板に書かれたことのある表記（「みそ角ふじ麺」と「味噌角ふじ」など）。監視ワードの照合に使う
ALIASES = {}


def week_menus(week):
    """その週に出た限定メニュー（比較用キー → 表示名, 曜日のリスト）"""
    out = {}
    for d in week["days"]:
        if d.get("closed"):
            continue
        for m in d.get("menus", []):
            name = m.get("same_as") or m.get("name", "") if isinstance(m, dict) else m
            key = normalize_menu(name)
            if not key:
                continue
            raw = m.get("name", "") if isinstance(m, dict) else m
            ALIASES.setdefault(key, set()).update({key, normalize_menu(raw)})
            disp, days = out.setdefault(key, (name, []))
            if d.get("weekday") and d["weekday"] not in days:
                days.append(d["weekday"])
    return out


# ============================================================
# 登場率
# ============================================================
def compute_stats(weeks, upto=None):
    """メニューごとの登場週数・登場率・最後に出た週・平均間隔。upto を渡すとその週より前だけで計算する"""
    keys = sorted(k for k in weeks if upto is None or k < upto)
    n = len(keys)
    stats = {}
    for i, wk in enumerate(keys):
        for key, (disp, days) in week_menus(weeks[wk]).items():
            s = stats.setdefault(key, {"name": disp, "weeks": [], "idx": [], "weekdays": {}})
            s["name"] = disp  # 最新の表記にする
            s["weeks"].append(wk)
            s["idx"].append(i)
            for wd in days:
                s["weekdays"][wd] = s["weekdays"].get(wd, 0) + 1
    for s in stats.values():
        k = len(s["weeks"])
        s["count"] = k
        s["rate"] = k / n if n else 0.0
        # 来週出る確率の推定（ラプラス補正：履歴が少ないうちに 0% / 100% と言い切らない）
        s["p_next"] = (k + 1) / (n + 2)
        gaps = [b - a for a, b in zip(s["idx"], s["idx"][1:])]
        s["avg_gap"] = sum(gaps) / len(gaps) if gaps else None
        s["since"] = n - 1 - s["idx"][-1]  # 最後に出てから何週空いているか
        s["last"] = s["weeks"][-1]
    return stats, n


def find_watched(stats_or_menus, watch):
    """監視ワードに部分一致するメニューのキー。監視ワードは「味噌|みそ」のように | で別の書き方を並べられる"""
    hits = {}
    for w in watch:
        alts = [normalize_menu(a) for a in w.split("|") if normalize_menu(a)]
        for key in stats_or_menus:
            names = ALIASES.get(key, {key})
            if any(a in n for a in alts for n in names):
                hits.setdefault(w, []).append(key)
    return hits


def judge_week(week_key, weeks, watch):
    """その週の判定と通知メッセージを作る"""
    week = weeks[week_key]
    menus = week_menus(week)
    stats, n = compute_stats(weeks, upto=week_key)  # この週より前の履歴で評価する
    enough = n >= MIN_WEEKS_FOR_RATE
    monday = date.fromisoformat(week_key)
    title = f"🍜 **{STORE_NAME} 今週の予定（{monday:%m/%d}〜{monday + timedelta(days=6):%m/%d}）**"

    lines = []
    for key, (disp, days) in menus.items():
        s = stats.get(key)
        if s is None:
            tag = "🆕初登場"
        elif enough:
            tag = f"登場率 {s['rate']:.0%}（{s['count']}/{n}週）"
            if s["rate"] <= RARE_RATE:
                tag += " ⭐レア"
        else:
            tag = f"{s['count']}/{n}週"
        lines.append(f"・{disp}（{'・'.join(days) or '曜日不明'}）… {tag}")
    closed = [d.get("weekday") or d.get("date") for d in week["days"] if d.get("closed")]
    if closed:
        lines.append(f"・休み: {'・'.join(c for c in closed if c)}")

    hits = find_watched(menus, watch)
    rare = [k for k in menus if k not in stats or (enough and stats[k]["rate"] <= RARE_RATE)]
    if hits:
        names = "、".join(menus[k][0] + f"（{'・'.join(menus[k][1]) or '曜日不明'}）" for k in dict.fromkeys(k for ks in hits.values() for k in ks))
        verdict = f"🔔 **狙いのメニューが出ます！** {names}"
    elif rare:
        verdict = f"👀 **レア枠あり**：{'、'.join(menus[k][0] for k in rare)}（狙い以外ですが、次はいつ出るか分かりません）"
    elif menus:
        verdict = "😴 **今週は見送りでOK**：よく出るメニューだけなので、また近いうちに出そうです"
    else:
        verdict = "限定メニューは読み取れませんでした"

    # 狙いのメニューで今週出ないものは、来週の見込みを添える
    outlook = []
    watched_keys = find_watched(stats, watch)
    for w in watch:
        if w in hits:
            continue
        ks = watched_keys.get(w)
        if not ks:
            outlook.append(f"・{w}: まだ一度も出ていません")
            continue
        s = max((stats[k] for k in ks), key=lambda x: x["count"])
        txt = f"・{w}: 来週出る確率 約{s['p_next']:.0%}（{s['count']}/{n}週、最後は{s['last'][5:].replace('-', '/')}の週"
        if s["avg_gap"]:
            txt += f"、平均{s['avg_gap']:.1f}週おき"
            if s["since"] >= s["avg_gap"]:
                txt += "→そろそろ来そう"
        outlook.append(txt + "）")

    msg = [title, verdict, "", *lines]
    if outlook:
        msg += ["", "**狙いのメニューの見込み**", *outlook]
    if not enough:
        msg.append(f"\n※履歴が{n}週分なので、確率は参考値です")
    msg.append(week.get("post_url", ""))
    return "\n".join(msg), bool(hits)


def write_stats_md(weeks, watch):
    stats, n = compute_stats(weeks)
    watched = {k for ks in find_watched(stats, watch).values() for k in ks}
    rows = sorted(stats.values(), key=lambda s: (-s["count"], s["name"]))
    first = min(weeks) if weeks else "-"
    out = [f"# {STORE_NAME} 限定メニュー登場率", "",
           f"{first} の週から {n}週分（自動更新: {datetime.now(pc.JST):%Y-%m-%d %H:%M}）", "",
           "| メニュー | 登場 | 登場率 | 来週の見込み | 平均間隔 | 最後に出た週 | よく出る曜日 |",
           "|---|---|---|---|---|---|---|"]
    for s in rows:
        star = "🔔 " if normalize_menu(s["name"]) in watched else ""
        wd = "・".join(sorted(s["weekdays"], key=lambda x: -s["weekdays"][x])[:3]) or "-"
        gap = f"{s['avg_gap']:.1f}週" if s["avg_gap"] else "-"
        out.append(f"| {star}{s['name']} | {s['count']}/{n} | {s['rate']:.0%} | {s['p_next']:.0%} | {gap} | {s['last']} | {wd} |")
    STATS_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"登場率を data/{STATS_FILE.name} に書きました（{len(rows)}メニュー / {n}週）")


# ============================================================
# ポストの処理
# ============================================================
def is_schedule_post(post):
    return KEYWORD in unicodedata.normalize("NFKC", pc.post_text(post)) and bool(pc.image_urls(post))


def known_menu_names(data):
    names = {}
    for wk in data["weeks"].values():
        for key, (disp, _) in week_menus(wk).items():
            names[key] = disp
    return sorted(names.values())


def process_post(client, post, data, force=False):
    """本文に「今週の予定」があり画像付きのポストを読み取り、週の予定として記録して週のキーを返す"""
    pid = str(post.get("id"))
    if (pid in data["seen_ids"] and not force) or not is_schedule_post(post):
        return None
    posted = pc.post_time(post)
    found = None
    for n, url in enumerate(pc.image_urls(post), start=1):
        img, mime = pc.download_image(url)
        sched = ocr_schedule(client, img, mime if mime.startswith("image/") else "image/jpeg", known_menu_names(data))
        if sched.is_weekly_schedule and sched.days:
            found = sched
            print(f"[今週の予定] {post_url(post)} 画像{n}（モデル: {pc._working_model}）")
            break
    data["seen_ids"].append(pid)
    if not found:
        print(f"::warning::「今週の予定」のポストですが予定表を読み取れませんでした: {post_url(post)}")
        return None
    monday = resolve_week(found, posted)
    wk = monday.isoformat()
    days = []
    for d, iso in zip(found.days, day_dates(found, monday)):
        row = d.model_dump()
        row["date"] = iso
        days.append(row)
        names = "、".join(m.name + (f"（= {m.same_as}）" if m.same_as and m.same_as != m.name else "") for m in d.menus)
        print(f"  {iso} {d.weekday or '?'} {'休み' if d.closed else names or '-'}")
    data["weeks"][wk] = {"post_url": post_url(post), "posted_at": posted.isoformat() if posted else "", "days": days}
    return wk


def main():
    if "--ocr-only" in sys.argv:
        path = sys.argv[sys.argv.index("--ocr-only") + 1]
        client = genai.Client(api_key=GEMINI_API_KEY)
        res = ocr_schedule(client, Path(path).read_bytes(), "image/png" if path.lower().endswith(".png") else "image/jpeg")
        print(json.dumps(res.model_dump(), ensure_ascii=False, indent=1))
        return

    data = load_data()
    watch = load_watch()
    if "--stats" in sys.argv:
        write_stats_md(data["weeks"], watch)
        return
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY が設定されていません。")
    client = genai.Client(api_key=GEMINI_API_KEY)

    if "--tweet" in sys.argv:
        post = pc.fetch_post(sys.argv[sys.argv.index("--tweet") + 1])
        posts = [post] if post else []
        force = True
    else:
        n = int(sys.argv[sys.argv.index("--backfill") + 1]) if "--backfill" in sys.argv else 40
        posts = pc.run_twitter_cli(["user-posts", TARGET_USER, "--max", str(n)])
        print(f"[X取得] @{TARGET_USER} の投稿 {len(posts)}件")
        if "--backfill" in sys.argv:
            # 投稿一覧で届かない古い週は検索で拾う（「今週の予定」は週1回なので件数は少ない）
            try:
                found = pc.run_twitter_cli(["search", KEYWORD, "--from", TARGET_USER, "--type", "Latest", "--max", str(n)])
                ids = {str(p.get("id")) for p in posts}
                posts += [p for p in found if str(p.get("id")) not in ids]
                print(f"[X検索] 「{KEYWORD}」 {len(found)}件")
            except Exception as e:
                print(f"::warning::検索に失敗したので投稿一覧の範囲だけ読みます: {e}")
        force = False
        print(f"  うち「{KEYWORD}」の画像付きポスト {sum(1 for p in posts if is_schedule_post(p))}件")
    backfill = "--backfill" in sys.argv

    # 古い順に読む（過去に出たメニューの名前を、後の週の表記ゆれの照合に使うため）
    posts = sorted(posts, key=lambda p: pc.post_time(p) or datetime.min.replace(tzinfo=pc.JST))
    new_weeks = []
    try:
        for post in posts:
            wk = process_post(client, post, data, force=force)
            if wk:
                new_weeks.append(wk)
    except pc.QuotaExhausted as e:
        # 読めなかったポストは seen_ids に入れていないので、次回の実行で続きから読む
        print(f"::warning::Gemini の今日の上限に達しました。続きは次回の実行で読みます: {e}")
    finally:
        save_data(data)
        write_stats_md(data["weeks"], watch)

    # 通知は最新の週だけ（さかのぼり読み込みのときは古い週の通知を送らない）
    if new_weeks:
        latest = max(new_weeks)
        msg, hit = judge_week(latest, data["weeks"], watch)
        print(msg)
        if backfill:
            msg = f"📚 過去の「今週の予定」を {len(set(new_weeks))}週分 読み込みました\n\n" + msg
        pc.send_discord_notification(DISCORD_WEBHOOK_URL, msg[:1990])
    else:
        print("新しい「今週の予定」はありませんでした")


if __name__ == "__main__":
    main()
