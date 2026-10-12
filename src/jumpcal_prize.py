import os
import re
import sys
import json
import time
import html
import requests

PAGE_URL = "https://www.shonenjump.com/j/jumpcalendar/genre/amusement/"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "..", "data", "jumpcal_prize_seen.json")

RESALE_KEYWORDS = ["再販", "再生産", "再出荷", "復刻", "再入荷"]

# "1" にすると再販系の商品だけを通知する
RESALE_ONLY = os.environ.get("RESALE_ONLY", "0") == "1"
# 初回実行時（状態ファイルなし）は既存商品を全件通知せず、記録だけする
NOTIFY_ON_FIRST_RUN = os.environ.get("NOTIFY_ON_FIRST_RUN", "0") == "1"

TOKEN_RE = re.compile(
    r'<h5>(?P<heading>[^<]*)</h5>'
    r'|<li><span class="genre2">.*?</span>(?P<item>.*?)</li>',
    re.S,
)
TITLE_RE = re.compile(r'<span class="title2">(.*?)</span>', re.S)
LINK_RE = re.compile(r'<a\s[^>]*href="([^"]*)"', re.S)
PRICE_RE = re.compile(r'<span class="price2">(.*?)</span>', re.S)
MAKER_RE = re.compile(r'<span class="maker2">(.*?)</span>', re.S)
TAG_RE = re.compile(r"<[^>]+>")


def clean(fragment):
    return re.sub(r"\s+", " ", html.unescape(TAG_RE.sub("", fragment))).strip()


def fetch_page():
    response = requests.get(PAGE_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def parse_items(page_html):
    items = []
    release = ""
    for m in TOKEN_RE.finditer(page_html):
        if m.group("heading") is not None:
            release = clean(m.group("heading"))
            continue
        body = m.group("item")
        title_m = TITLE_RE.search(body)
        # 一部の項目は title2 の開始タグが欠けているので、価格欄の手前までをタイトルとして扱う
        title_html = title_m.group(1) if title_m else body.split('<span class="price2">')[0]
        title = clean(title_html)
        if not title:
            continue
        link_m = LINK_RE.search(title_html)
        price_m = PRICE_RE.search(body)
        maker_m = MAKER_RE.search(body)
        items.append({
            "title": title,
            "url": html.unescape(link_m.group(1)) if link_m else "",
            "price": clean(price_m.group(1)) if price_m else "",
            "maker": clean(maker_m.group(1)) if maker_m else "",
            "release": release,
        })
    return items


def is_prize(item):
    # 「アミューズメント専用景品」がプライズ。"100円" などはアーケードゲーム等なので除外
    # 価格「未定」はプライズメーカーの景品が多いため含める
    return "円" not in item["price"]


def is_resale(item):
    return any(k in item["title"] for k in RESALE_KEYWORDS)


def item_key(item):
    return f'{item["title"]}|{item["maker"]}'


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Failed to load state: {e}")
    return None


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def build_embed(item, kind, old_release=None):
    if kind == "changed":
        label, color = "📅 発売時期変更", 0xF1C40F
    elif is_resale(item):
        label, color = "🔁 再販", 0xE74C3C
    else:
        label, color = "🆕 新着", 0x3498DB
    release = item["release"] or "不明"
    if old_release:
        release = f"{old_release} → {release}"
    embed = {
        "title": f"{label}｜{item['title']}"[:256],
        "color": color,
        "fields": [
            {"name": "発売", "value": release[:1024], "inline": True},
            {"name": "メーカー", "value": (item["maker"] or "不明")[:1024], "inline": True},
        ],
        "footer": {"text": "ジャンプカレンダー（アミューズメント）"},
    }
    if item["url"].startswith("http"):
        embed["url"] = item["url"]
    return embed


def post_discord(webhook_url, embeds):
    # Discord は 1 メッセージあたり embed 10 件まで
    for i in range(0, len(embeds), 10):
        payload = {"embeds": embeds[i:i + 10]}
        if i == 0:
            payload["content"] = f"🎯 **ジャンプカレンダー プライズ更新** ({len(embeds)}件)\n<{PAGE_URL}>"
        for _ in range(5):
            response = requests.post(webhook_url, json=payload, timeout=30)
            if response.status_code == 429:
                wait = float(response.json().get("retry_after", 2))
                time.sleep(wait + 0.5)
                continue
            if response.status_code >= 300:
                print(f"Discord error {response.status_code}: {response.text}")
            break
        time.sleep(1)


def main():
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL is not set.")
        return

    items = []
    seen_keys = set()
    for it in parse_items(fetch_page()):
        # 同じ商品が複数の発売時期に載っている場合は最初の1件だけを使う
        if is_prize(it) and item_key(it) not in seen_keys:
            seen_keys.add(item_key(it))
            items.append(it)
    print(f"Parsed {len(items)} prize items.")
    if not items:
        # ページ構造が変わった可能性があるので、状態を上書きしない
        print("No items parsed. The page layout may have changed.")
        sys.exit(1)

    state = load_state()
    first_run = state is None
    state = state or {}

    embeds = []
    current = {}
    for item in items:
        key = item_key(item)
        current[key] = item["release"]
        if key not in state:
            kind = "new"
        elif state[key] != item["release"]:
            kind = "changed"
        else:
            continue
        if RESALE_ONLY and not is_resale(item):
            continue
        embeds.append(build_embed(item, kind, state.get(key) if kind == "changed" else None))

    if first_run and not NOTIFY_ON_FIRST_RUN:
        print(f"First run: recorded {len(current)} items without notifying.")
    elif embeds:
        print(f"Sending {len(embeds)} notifications...")
        post_discord(webhook_url, embeds)
    else:
        print("No updates.")

    # 掲載終了した商品は削除し、再掲載時に再び通知されるようにする
    save_state(current)


if __name__ == "__main__":
    main()
