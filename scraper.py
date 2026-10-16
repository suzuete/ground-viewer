"""
川口市(らくりざ)と戸田市の野球場空き状況を取得して slots.json を更新する。

ポイント:
  - 取得できない/稼働時間外のサイトは、前回のデータをそのまま残す
    (戸田市が9時台しか動かなくても、一覧から消えない)
  - サイトごとの更新時刻と状態を source_status に記録する

fetch_kawaguchi / fetch_toda の中身は、各サイトの画面に合わせて書く必要がある。
`playwright codegen <URL>` で操作を記録し、TODO の部分に貼り付ける。
"""
import json
import os
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

JST = timezone(timedelta(hours=9))
PURPOSE = "野球"
OUT = "slots.json"
OPEN_MARKS = ("○", "◯", "空き", "空")


def slot(date, ground, court, period, status, source):
    return {"date": date, "ground": ground, "court": court,
            "period": period, "status": status, "source": source}


def status_of(text):
    return "open" if any(m in text for m in OPEN_MARKS) else "full"


def fetch_kawaguchi(page):
    page.goto("https://kawaguchi-city.rakuriza.jp/")
    # TODO: 「施設から探す」→ 利用目的に PURPOSE を選択 → 施設を選ぶ → 月表示の空き状況へ
    # TODO: 各セルから slot(...) を作って返す
    raise NotImplementedError("川口市の操作手順を実装してください")


def fetch_toda(page):
    page.goto("https://yoyaku.city.toda.saitama.jp/yoyaku/")
    # TODO: 同上。9時台以外は画面が開かないため、稼働時間内に実行すること
    raise NotImplementedError("戸田市の操作手順を実装してください")


# window: 実行を許可する日本時間の時間帯(開始時, 終了時)。None なら常時
SOURCES = [
    {"id": "kawaguchi", "name": "川口市", "url": "https://kawaguchi-city.rakuriza.jp/",
     "fn": fetch_kawaguchi, "window": None},
    {"id": "toda", "name": "戸田市", "url": "https://yoyaku.city.toda.saitama.jp/yoyaku/",
     "fn": fetch_toda, "window": (9, 10)},   # 9:00〜9:59
]


def in_window(window, now):
    return window is None or os.environ.get("FORCE") or window[0] <= now.hour < window[1]


def main():
    now = datetime.now(JST)
    try:
        with open(OUT, encoding="utf-8") as f:
            old = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        old = {}

    status = old.get("source_status", {})
    slots_by_src = {s["id"]: [] for s in SOURCES}
    for s in old.get("slots", []):
        slots_by_src.setdefault(s["source"], []).append(s)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for src in SOURCES:
            sid = src["id"]
            if not in_window(src["window"], now):
                print(f"[{sid}] 稼働時間外のためスキップ(前回データを維持)")
                continue
            page = browser.new_page()
            try:
                slots_by_src[sid] = src["fn"](page)
                status[sid] = {"ok": True, "at": now.isoformat(), "error": None}
                print(f"[{sid}] {len(slots_by_src[sid])} 件取得")
            except Exception as e:
                prev = status.get(sid, {})
                status[sid] = {"ok": False, "at": prev.get("at"), "error": str(e)}
                print(f"[{sid}] 失敗: {e}(前回データを維持)")
            finally:
                page.close()
        browser.close()

    out = {
        "updated": now.isoformat(),
        "holidays": old.get("holidays", []),   # 祝日は別途追加(例: 内閣府のCSVから生成)
        "sources": [{"id": s["id"], "name": s["name"], "url": s["url"]} for s in SOURCES],
        "source_status": status,
        "slots": [x for lst in slots_by_src.values() for x in lst],
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
