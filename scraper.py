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
from datetime import date, datetime, timezone, timedelta
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


KAWAGUCHI_URL = "https://kawaguchi-city.rakuriza.jp/?purpose=1%2C2%2C3%2C4&page=1&per_page=30"
KAWAGUCHI_FACILITY_IDS = [15, 16, 13, 10, 14, 8, 17]  # 記録時に選んだ施設
HORIZON_DAYS = 60      # 何日先まで取得するか
MAX_PAGES = 12         # 1つのカレンダーで「次週」を押す最大回数

# 1つのカレンダー(.facility-calender-content)から、施設名・表示月・各日の状態を読む
_JS_READ = r"""
(sec) => {
  const ground = (sec.closest('.facility-group')?.querySelector('.facility-name-text')?.textContent || '').trim();
  const head = sec.parentElement.querySelector('.calendar-facility-name');
  let court = '';
  if (head) {
    const t = [...head.childNodes].find(n => n.nodeType === 3);
    court = (t ? t.textContent : head.textContent).trim();
  }
  const num = s => parseInt((s || '').replace(/\D/g, ''), 10);
  const year = num(sec.querySelector('.year-text')?.textContent);
  const month = num(sec.querySelector('.month-year')?.textContent);
  const cells = [...sec.querySelectorAll('.calendar-grid .calendar-day')].map(c => {
    const m = (c.getAttribute('aria-label') || '').match(/日付\s*(\d+)\s*は(.*)/);
    return m ? {day: +m[1], status: m[2]} : null;
  });
  return {ground, court, year, month, cells};
}
"""


def _resolve_start(year, month, day):
    """先頭のセルは日曜日。その日付が日曜になる年月を、表示月→前月→翌月の順で探す。"""
    for delta in (0, -1, 1):
        y, m = year, month + delta
        if m < 1:
            y, m = y - 1, 12
        if m > 12:
            y, m = y + 1, 1
        try:
            d = date(y, m, day)
        except ValueError:
            continue
        if d.weekday() == 6:
            return d
    return None


def _page_key(info):
    first = info["cells"][0]["day"] if info["cells"] and info["cells"][0] else None
    return (info["year"], info["month"], first)


def fetch_kawaguchi(page):
    page.goto(KAWAGUCHI_URL)
    page.get_by_role("button", name="施設を検索する").click()
    for fid in KAWAGUCHI_FACILITY_IDS:
        page.locator(f"#facility-{fid}").check()
    page.get_by_role("button", name="空き状況を確認する").click()
    page.locator(".facility-calender-content").first.wait_for(timeout=30000)

    today = datetime.now(JST).date()
    limit = today + timedelta(days=HORIZON_DAYS)
    sections = page.locator(".facility-calender-content")
    count = sections.count()
    if count == 0:
        raise RuntimeError("カレンダーが見つかりません(画面構造を確認してください)")

    found = {}
    for i in range(count):
        sec = sections.nth(i)
        info = sec.evaluate(_JS_READ)
        for _ in range(MAX_PAGES):
            cells = info["cells"]
            if not cells or not cells[0]:
                break
            start = _resolve_start(info["year"], info["month"], cells[0]["day"])
            if start is None:
                print(f"[kawaguchi] 日付を決められませんでした: {info['ground']} {info['court']}")
                break
            for k, c in enumerate(cells):
                d = start + timedelta(days=k)
                if c and "空きです" in c["status"] and today <= d <= limit:
                    found[(d.isoformat(), info["ground"], info["court"])] = True
            if start + timedelta(days=len(cells) - 1) >= limit:
                break
            nxt = sec.get_by_role("button", name="次月または次週の表示へ切り替え")
            if nxt.count() == 0 or not nxt.first.is_enabled():
                break
            before = _page_key(info)
            nxt.first.click()
            for _ in range(10):               # 画面が切り替わるまで最大3秒待つ
                page.wait_for_timeout(300)
                info = sec.evaluate(_JS_READ)
                if _page_key(info) != before:
                    break
            else:
                break

    return [slot(d, g, c, "要確認", "open", "kawaguchi") for (d, g, c) in sorted(found)]


def fetch_toda(page):
    page.goto("https://yoyaku.city.toda.saitama.jp/yoyaku/")
    # TODO: 同上。9時台以外は画面が開かないため、稼働時間内に実行すること
    raise NotImplementedError("戸田市の操作手順を実装してください")


# window: 実行を許可する日本時間の時間帯(開始時, 終了時)。None なら常時
SOURCES = [
    {"id": "kawaguchi", "name": "川口市", "url": "https://kawaguchi-city.rakuriza.jp/",
     "fn": fetch_kawaguchi, "window": None},
    {"id": "toda", "name": "戸田市", "url": "https://yoyaku.city.toda.saitama.jp/yoyaku/",
     "fn": fetch_toda, "window": (9, 24)},   # 9:00〜23:59
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
