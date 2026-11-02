"""Xのポストを取得して data/x_posts/<アカウント>.json に保存する。

figure-kaitori-bot（スプレッドシートへの書き込み担当）はXのCookieを持たないので、
ここで取得したポストを公開リポジトリのファイル経由で読み取る。

  - nakanofactory: ナカノファクトリーの「本日の商品」（買取価格）
  - amagi_amuse:   マンガ倉庫甘木店アミューズの「新景品入荷予定一覧」と「【新プライズ情報】」

ポストIDで重ね合わせ、新しい順に最大 KEEP 件を残す。内容が変わらなければファイルも変えない
（毎時のコミットが増えないように）。取得に失敗したアカウントは前回のファイルをそのまま残す。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "data" / "x_posts"
KEEP = 300

# (保存名, twitter-cli の引数)
SOURCES = {
    "nakanofactory": [
        ["user-posts", "nakanofactory", "--max", "20"],
    ],
    "amagi_amuse": [
        ["search", "新景品入荷予定一覧", "--from", "amagi_amuse", "--type", "Latest", "--max", "5"],
        ["search", "【新プライズ情報】", "--from", "amagi_amuse", "--type", "Latest", "--max", "40"],
    ],
}


def run_cli(args):
    result = subprocess.run(
        [sys.executable, "-m", "twitter_cli.cli", *args, "--json"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore", env=os.environ.copy(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:300])
    data = json.loads(result.stdout.strip() or "[]")
    if isinstance(data, dict):
        data = data.get("tweets") or data.get("data") or []
    if isinstance(data, dict):
        data = [data]
    return [t for t in data if isinstance(t, dict) and t.get("id")]


def slim(post):
    """必要な項目だけ残す（閲覧数などの変わりやすい値を入れると毎回差分が出るため）"""
    return {
        "id": str(post.get("id")),
        "text": post.get("text") or post.get("full_text") or "",
        "createdAt": post.get("createdAt") or post.get("created_at") or "",
        "media": [
            {"type": m.get("type", ""), "url": m.get("url", "")}
            for m in (post.get("media") or []) if m.get("url")
        ],
    }


def main():
    if not (os.environ.get("TWITTER_AUTH_TOKEN") and os.environ.get("TWITTER_CT0")):
        print("TWITTER_AUTH_TOKEN / TWITTER_CT0 が未設定のため飛ばします。")
        return
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, queries in SOURCES.items():
        path = OUT_DIR / f"{name}.json"
        try:
            old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except Exception:
            old = []
        posts = {p["id"]: p for p in old if isinstance(p, dict) and p.get("id")}
        ok = False
        for args in queries:
            try:
                fetched = run_cli(args)
                ok = True
            except Exception as e:
                print(f"[{name}] 取得に失敗しました（{' '.join(args[:2])}）: {e}")
                continue
            for p in fetched:
                posts[str(p["id"])] = slim(p)
            print(f"[{name}] {' '.join(args[:2])}: {len(fetched)}件")
        if not ok:
            continue
        merged = sorted(posts.values(), key=lambda p: int(p["id"]), reverse=True)[:KEEP]
        text = json.dumps(merged, ensure_ascii=False, indent=1) + "\n"
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            path.write_text(text, encoding="utf-8")
            print(f"[{name}] {path.relative_to(BASE_DIR)} を更新しました（{len(merged)}件）")
        else:
            print(f"[{name}] 変更なし")


if __name__ == "__main__":
    main()
