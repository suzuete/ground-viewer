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
import re
import unicodedata
from datetime import date, datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

try:
    import jpholiday          # 祝日判定(pip install jpholiday)。無くても土日のみで動く
except ImportError:
    jpholiday = None

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
HORIZON_DAYS = 60        # 空きのある日を何日先まで調べるか
MAX_PAGES = 12           # 1つのカレンダーで「次週」を押す最大回数
TIME_DETAIL = True       # 時間帯(9:00〜11:00など)まで取得する
TIME_WEEKDAYS = True     # 平日も時間帯まで取得する(Falseにすると土日祝のみ。実行時間が短くなる)
TIME_HORIZON_DAYS = 45   # 時間帯は何日先まで取得するか
BATCH_SIZE = 10          # 日付を同時に選べる上限(サイトの仕様: 最大10件)


def is_weekend_or_holiday(d):
    return d.weekday() >= 5 or (jpholiday is not None and jpholiday.is_holiday(d))


NOTIFY = True             # 新しい空きが出たら通知ファイル(notify.json)を作る → ワークフローがGitHubのIssueにする
NOTIFY_WEEKDAYS = False   # True にすると、平日の空きも通知する(Falseなら土日祝のみ)
NOTIFY_FILE = "notify.json"
DOW_JA = "月火水木金土日"


RULES_FILE = "notify_rules.json"   # 通知の条件(曜日・施設・時間帯)。GitHub上で編集できる
RULES = None                       # main() が読み込む。None のときは標準の条件(NOTIFY_WEEKDAYS)を使う


def load_rules():
    try:
        with open(RULES_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"[notify] {RULES_FILE} を読めません({e})。標準の条件で通知します")
        return None
    rules = data.get("rules") if isinstance(data, dict) else None
    if not rules:
        print(f"[notify] {RULES_FILE} に条件(rules)がありません。標準の条件で通知します")
        return None
    return rules


def _norm_period(p):
    p = unicodedata.normalize("NFKC", str(p)).replace("~", "〜").replace(" ", "")
    return re.sub(r"(?<!\d)0(\d):", r"\1:", p)


def _minutes(t):
    m = re.match(r"\s*(\d{1,2}):(\d{2})", unicodedata.normalize("NFKC", str(t)))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def matches_rules(s, rules):
    """どれか1つの条件に当てはまれば True。1つの条件の中では、書いた項目がすべて当てはまる必要がある。"""
    d = date.fromisoformat(s["date"])
    for r in rules:
        if r.get("sources") and s["source"] not in r["sources"]:
            continue
        if r.get("days"):
            tags = {DOW_JA[d.weekday()]}
            if jpholiday is not None and jpholiday.is_holiday(d):
                tags.add("祝")
            if not (tags & set(r["days"])):
                continue
        if r.get("grounds") and not any(g in s["ground"] for g in r["grounds"]):
            continue
        if r.get("courts") and not any(c in s["court"] for c in r["courts"]):
            continue
        if r.get("times") or r.get("after") or r.get("before"):
            start = _minutes(s["period"])
            if start is None:
                continue                       # 時間帯が分からない枠(要確認など)は、時間の条件がある場合は対象外
            if r.get("times") and _norm_period(s["period"]) not in [_norm_period(t) for t in r["times"]]:
                continue
            if r.get("after") and start < _minutes(r["after"]):
                continue
            if r.get("before") and start >= _minutes(r["before"]):
                continue
        return True
    return False


def find_new_open(old_slots, new_slots):
    """前回と比べて、新しく空いた枠を返す。
    ・前回は空きでなかった枠(満席・受付前など)が空いた
    ・前回は無かった枠が出て、同じ日・施設・面に、前回は空きが1つも無かった
    (「要確認」から時間帯の表示に変わっただけのときは、新しい空きとみなさない)"""
    k = lambda s: (s["source"], s["date"], s["ground"], s["court"], s["period"])
    b = lambda s: (s["source"], s["date"], s["ground"], s["court"])
    old_status = {k(s): s["status"] for s in old_slots}
    old_open_blocks = {b(s) for s in old_slots if s["status"] == "open"}
    out = []
    for s in new_slots:
        if s["status"] != "open":
            continue
        if RULES is not None:
            if not matches_rules(s, RULES):
                continue
        elif not NOTIFY_WEEKDAYS and not is_weekend_or_holiday(date.fromisoformat(s["date"])):
            continue
        if k(s) in old_status:
            if old_status[k(s)] != "open":
                out.append(s)
        elif b(s) not in old_open_blocks:
            out.append(s)
    return out


def write_notification(new, source_names, test=False):
    owner = os.environ.get("GITHUB_REPOSITORY_OWNER", "")
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    mention = f"@{owner} " if owner else ""
    site = f"https://{owner}.github.io/{repo.split('/')[-1]}/" if owner and repo else ""
    if test:
        title = "【テスト】空き通知の確認"
        body = f"{mention}これは通知のテストです。このメールが届けば、設定は成功しています。"
    else:
        lines, shown = [], 0
        by_date = {}
        for s in sorted(new, key=lambda s: (s["date"], s["ground"], s["court"], s["period"])):
            by_date.setdefault(s["date"], {}).setdefault(
                (source_names.get(s["source"], s["source"]), s["ground"], s["court"]), []).append(s["period"])
        for d, groups in by_date.items():
            dt = date.fromisoformat(d)
            lines.append(f"\n**{dt.month}/{dt.day}({DOW_JA[dt.weekday()]})**")
            for (src, g, c), periods in groups.items():
                if shown >= 30:
                    break
                lines.append(f"- {src} {g} {c}: " + " / ".join(periods))
                shown += 1
        if shown < len({(s["date"], s["ground"], s["court"]) for s in new}):
            lines.append("\nほかにも空きがあります。サイトで確認してください。")
        first = sorted(new, key=lambda s: (s["date"], s["ground"]))[0]
        fd = date.fromisoformat(first["date"])
        title = f"【空き通知】{len(new)}枠 {fd.month}/{fd.day}({DOW_JA[fd.weekday()]}) {first['ground']} ほか"
        body = f"{mention}新しい空きが出ました。\n" + "\n".join(lines) + (f"\n\n一覧: {site}" if site else "")
    with open(NOTIFY_FILE, "w", encoding="utf-8") as f:
        json.dump({"title": title, "body": body}, f, ensure_ascii=False)
    plain = (title + "\n" + body).replace("**", "").replace(mention, "")   # LINE用(装飾なしの文章)
    with open("notify_line.txt", "w", encoding="utf-8") as f:
        f.write(plain)


def holidays_between(start, end):
    out, d = [], start
    while d <= end:
        if jpholiday is not None and jpholiday.is_holiday(d):
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


# ---- 日付選択画面(カレンダー) ----
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


def _go_next(page, sec, info):
    """カレンダーを「次週」へ進め、新しい内容を返す。進めなければ None。"""
    nxt = sec.get_by_role("button", name="次月または次週の表示へ切り替え")
    if nxt.count() == 0 or not nxt.first.is_enabled():
        return None
    before = _page_key(info)
    nxt.first.click()
    for _ in range(10):                      # 画面が切り替わるまで最大3秒待つ
        page.wait_for_timeout(300)
        new = sec.evaluate(_JS_READ)
        if _page_key(new) != before:
            return new
    return None


def _open_results(page):
    """トップ → 施設選択 → 空き状況(カレンダー)画面まで進む。"""
    resp = page.goto(KAWAGUCHI_URL, wait_until="domcontentloaded", timeout=60000)
    if resp is not None and resp.status >= 400:
        raise RuntimeError(f"川口市サイトがHTTP {resp.status}を返しました")
    page.get_by_role("button", name="施設を検索する").click()
    for fid in KAWAGUCHI_FACILITY_IDS:
        page.locator(f"#facility-{fid}").check()
    page.get_by_role("button", name="空き状況を確認する").click()
    page.locator(".facility-calender-content").first.wait_for(timeout=30000)
    page.wait_for_timeout(500)


def _classify_day(label):
    """カレンダーの日付ボタンの説明文 → 状態。分類できない(情報なし等)ものは None。"""
    if "空きです" in label:
        return "open"
    if "満室" in label:
        return "full"
    if "抽選" in label:
        return "lottery"
    if "期間外" in label:
        return "outside"
    if "休館" in label or "施設都合" in label:
        return "closed"
    return None


def _crawl_calendars(page):
    """全カレンダーを先へ進めながら、各日の状態を集める。
    {(日付, 施設, 面): {"idx": カレンダー番号, "status": open/full/lottery/outside/closed}}"""
    today = datetime.now(JST).date()
    limit = today + timedelta(days=HORIZON_DAYS)
    sections = page.locator(".facility-calender-content")
    if sections.count() == 0:
        raise RuntimeError("カレンダーが見つかりません(画面構造を確認してください)")
    found, unknown = {}, set()
    for i in range(sections.count()):
        sec = sections.nth(i)
        info = sec.evaluate(_JS_READ)
        for _ in range(MAX_PAGES):
            cells = info["cells"]
            if not cells or not cells[0]:
                break
            start = _resolve_start(info["year"], info["month"], cells[0]["day"])
            if start is None:
                print(f"[kawaguchi] 日付を決められません: {info['ground']} {info['court']}")
                break
            for k, c in enumerate(cells):
                d = start + timedelta(days=k)
                if not c or not (today <= d <= limit):
                    continue
                st = _classify_day(c["status"])
                if st:
                    found[(d.isoformat(), info["ground"], info["court"])] = {"idx": i, "status": st}
                elif "情報なし" not in c["status"]:
                    unknown.add(c["status"])
            if start + timedelta(days=len(cells) - 1) >= limit:
                break
            info = _go_next(page, sec, info)
            if info is None:
                break
    if unknown:
        print(f"[kawaguchi] 未対応の表示(無視しました): {sorted(unknown)}")
    return found


def _select_dates(page, targets):
    """targets: [(カレンダー番号, 日付, 施設, 面)]。日付セルをクリックして選び、選んだ数を返す。"""
    sections = page.locator(".facility-calender-content")
    by_sec = {}
    for t in targets:
        by_sec.setdefault(t[0], []).append(t)
    selected = 0
    for idx, items in by_sec.items():
        sec = sections.nth(idx)
        info = sec.evaluate(_JS_READ)
        for _, d, ground, court in sorted(items, key=lambda t: t[1]):
            if (info["ground"], info["court"]) != (ground, court):
                break                          # 並び順が変わっていたら諦める
            for _ in range(MAX_PAGES):
                cells = info["cells"]
                start = _resolve_start(info["year"], info["month"], cells[0]["day"]) \
                    if cells and cells[0] else None
                if start is None or d < start:
                    break
                if d <= start + timedelta(days=len(cells) - 1):
                    sec.locator(".calendar-grid .calendar-day").nth((d - start).days).click()
                    selected += 1
                    break
                info = _go_next(page, sec, info)
                if info is None:
                    break
    return selected


# ---- 時間選択画面 ----
_JS_TIME = r"""
() => [...document.querySelectorAll('.facility-section')].flatMap(fs => {
  const ground = (fs.querySelector('.facility-name-text')?.textContent || '').trim();
  return [...fs.querySelectorAll('.room-accordion')].flatMap(acc => {
    const head = acc.querySelector('.room-name');
    let court = '';
    if (head) {
      const t = [...head.childNodes].find(n => n.nodeType === 3);
      court = (t ? t.textContent : head.textContent).trim();
    }
    const cols = [...acc.querySelectorAll('thead th')].map(t => t.textContent.trim());
    return [...acc.querySelectorAll('td.availability-cell')].map(td => ({
      ground, court, col: cols[td.cellIndex] || '',
      date: td.dataset.date || '', time: td.dataset.timeString || '', cls: td.className,
      title: td.getAttribute('title') || ''
    }));
  });
});
"""


def _fmt_time(t):
    try:
        a, b = t.split("-")
        f = lambda x: f"{int(x.split(':')[0])}:{x.split(':')[1]}"
        return f"{f(a)}〜{f(b)}"
    except Exception:
        return t


def _cell_status(cls, title):
    if "期間外" in title:
        return "outside"
    if "availability-available" in cls:
        return "open"
    if "lottery" in cls:
        return "lottery"
    return "full"


def _time_slots(rows):
    """時間選択画面の行 → {(日付, 施設, 面): [slot, ...]}。満席の枠も記録する。"""
    result = {}
    for r in rows:
        key = (r["date"], r["ground"], r["court"])
        result.setdefault(key, [])
        court = r["court"] if r["col"] in ("", "全面") else f'{r["court"]} {r["col"]}'
        result[key].append(slot(r["date"], r["ground"], court, _fmt_time(r["time"]),
                                _cell_status(r["cls"], r.get("title", "")), "kawaguchi"))
    return result


def _read_time_page(page):
    page.get_by_role("button", name="時間選択画面へ").first.click()
    page.locator(".time-table").first.wait_for(timeout=30000)
    page.wait_for_timeout(500)
    return page.evaluate(_JS_TIME)


ACCEPT_MONTHS_AHEAD = 1   # 受付は翌月分まで(例: 10月中は11月末まで。12月分は11/1から受付)


def is_outside_acceptance(d, today):
    return d.year * 12 + d.month > today.year * 12 + today.month + ACCEPT_MONTHS_AHEAD


TEMPLATES = {}            # {"施設|面": ["9:00〜11:00", ...]} 時間割(受付期間外の日の表示に使う)
OUTSIDE_DETAIL = True     # 受付期間外の日も、時間選択画面から時間帯を取得してみる


def _fetch_time_details(page, targets, size, detailed, stop_on_error=False):
    """日付を選んで時間選択画面を読み、detailed に追加する。stop_on_error時は、失敗したら False を返して中止。"""
    for i in range(0, len(targets), size):
        batch = targets[i:i + size]
        try:
            _open_results(page)                # 毎回最初からやり直す(状態を引きずらない)
            if _select_dates(page, batch) == 0:
                if stop_on_error:
                    return False
                continue
            detailed.update(_time_slots(_read_time_page(page)))
        except Exception as e:
            print(f"[kawaguchi] 時間帯の取得に失敗(日単位の表示で代用): {e}")
            if stop_on_error:
                return False
    return True


def _learn_templates(slots_by_key):
    """取得できた時間帯から、施設・面ごとの時間割を覚える。"""
    for slots in slots_by_key.values():
        by_court = {}
        for s in slots:
            by_court.setdefault((s["ground"], s["court"]), []).append(s["period"])
        for (g, c), periods in by_court.items():
            if periods:
                TEMPLATES[f"{g}|{c}"] = periods


def _assemble(found, detailed, outside_detail, today):
    result = []
    for key in sorted(found):
        d, g, c = key
        st = found[key]["status"]
        outside = st == "outside" or (st == "open" and is_outside_acceptance(date.fromisoformat(d), today))
        if outside:                            # まだ受付が始まっていない日
            if key in outside_detail:
                result += outside_detail[key]
            elif TEMPLATES.get(f"{g}|{c}"):    # 取れない場合は、同じ施設の時間割で表示
                result += [slot(d, g, c, p, "outside", "kawaguchi") for p in TEMPLATES[f"{g}|{c}"]]
            else:
                result.append(slot(d, g, c, "受付期間外", "outside", "kawaguchi"))
        elif st == "open":
            if key in detailed:
                result += detailed[key]
            else:                              # 時間帯を調べなかった日
                result.append(slot(d, g, c, "要確認", "open", "kawaguchi"))
        elif st == "full":
            result.append(slot(d, g, c, "終日", "full", "kawaguchi"))
        elif st == "lottery":
            result.append(slot(d, g, c, "抽選", "lottery", "kawaguchi"))
        elif st == "closed":
            result.append(slot(d, g, c, "休館", "closed", "kawaguchi"))
    return result


def fetch_kawaguchi(page):
    _open_results(page)
    found = _crawl_calendars(page)
    today = datetime.now(JST).date()

    detailed, outside_detail = {}, {}
    if TIME_DETAIL:
        horizon = today + timedelta(days=TIME_HORIZON_DAYS)
        targets, outside_targets = [], []
        for (d_s, g, c), v in found.items():
            d = date.fromisoformat(d_s)
            if v["status"] != "open" or not (TIME_WEEKDAYS or is_weekend_or_holiday(d)):
                continue
            if is_outside_acceptance(d, today):
                outside_targets.append((v["idx"], d, g, c))
            elif d <= horizon:
                targets.append((v["idx"], d, g, c))
        targets.sort()
        outside_targets.sort()
        key_of = lambda t: (t[1].isoformat(), t[2], t[3])

        _fetch_time_details(page, targets, BATCH_SIZE, detailed)
        missing = [t for t in targets if key_of(t) not in detailed]
        if missing:                            # 取れなかった分は、少人数でやり直す
            print(f"[kawaguchi] 取得できなかった {len(missing)} 件を再試行します")
            _fetch_time_details(page, missing, 3, detailed)
            missing = [t for t in targets if key_of(t) not in detailed]
        print(f"[kawaguchi] 時間帯を取得できた枠: {len(targets) - len(missing)} / {len(targets)}")
        for t in missing:
            print(f"  未取得: {t[1]} {t[2]} {t[3]}")
        _learn_templates(detailed)

        if OUTSIDE_DETAIL and outside_targets:   # 受付期間外の日の時間帯
            ok = _fetch_time_details(page, outside_targets, BATCH_SIZE, outside_detail, stop_on_error=True)
            for slots in outside_detail.values():
                for sl in slots:
                    if sl["status"] == "open":
                        sl["status"] = "outside"   # まだ予約できない
            got = sum(1 for t in outside_targets if key_of(t) in outside_detail)
            note = "" if ok else "(取得できないため、同じ施設の時間割で代用)"
            print(f"[kawaguchi] 受付期間外の日の時間帯: {got} / {len(outside_targets)}{note}")
            _learn_templates(outside_detail)

    return _assemble(found, detailed, outside_detail, today)


# ---------------- 戸田市 ----------------
TODA_URL = "https://yoyaku.city.toda.saitama.jp/yoyaku/"
TODA_PLACES = ["道満グリーンパーク", "北部公園野球場", "笹目公園野球場"]   # 取得する場所(施設の中の「野球場」を全て調べる)

# 表示中のカレンダー(1週間分)から、開始日・場所・施設・各コマの状態を読む
_JS_TODA = r"""
() => {
  const inp = document.querySelector('input[name$="displayStartDate"]');
  const body = document.body.innerText;
  const pick = re => ((body.match(re) || [])[1] || '').replace(/(場所を再選択|施設を再選択)\s*$/, '').trim();
  const rows = [...document.querySelectorAll('#areaToKomaDisplay tr')].map(tr =>
    [...tr.children].map(td => ({
      t: td.getAttribute('data-original-title') || td.getAttribute('title') || '',
      s: td.getAttribute('initialdisplay') || (td.querySelector('.komaStatus')?.textContent || '').trim()
    })));
  return {start: inp ? inp.value : '', place: pick(/選択中の場所：([^\n]*)/), fac: pick(/選択中の施設：([^\n]*)/), rows};
}
"""


def _toda_status(sym):
    if "○" in sym or "◯" in sym:
        return "open"
    if "×" in sym:
        return "full"
    if "抽" in sym:
        return "lottery"
    if any(k in sym for k in ("整備", "休", "閉", "不可")):
        return "closed"
    return None                                # 「―」など(過去のコマ・対象外)は記録しない


def _toda_time(t):
    m = re.findall(r"(\d{1,2}):(\d{2})", t)
    return f"{int(m[0][0])}:{m[0][1]}〜{int(m[1][0])}:{m[1][1]}" if len(m) == 2 else t


def _toda_slots_from_info(info, place, today, limit):
    """読み取った1週間分 → ([slot...], 記録できる状態が1つでもあったか, 開始日)"""
    try:
        start = datetime.strptime(info["start"], "%Y/%m/%d").date()
    except ValueError:
        return [], False, None
    court = unicodedata.normalize("NFKC", info["fac"]) or "野球場"
    out = []
    for row in info["rows"]:
        label = next((c["t"] for c in row if c["t"]), "")
        for k, c in enumerate(row):
            d = start + timedelta(days=k)
            st = _toda_status(c["s"])
            if st is None or not (today <= d <= limit):
                continue
            out.append(slot(d.isoformat(), place, court, _toda_time(c["t"] or label), st, "toda"))
    return out, bool(out), start


def _toda_next(page, info):
    nxt = page.locator("a.clickable", has=page.locator("i.fa-arrow-circle-right"))
    if nxt.count() == 0:
        return None
    before = info["start"]
    nxt.first.click()
    for _ in range(15):                          # 画面が切り替わるまで最大4.5秒待つ
        page.wait_for_timeout(300)
        new = page.evaluate(_JS_TODA)
        if new["start"] != before:
            return new
    return None


def _toda_collect(page, place, found):
    today = datetime.now(JST).date()
    limit = today + timedelta(days=HORIZON_DAYS)
    info = page.evaluate(_JS_TODA)
    for _ in range(MAX_PAGES):
        got, any_status, start = _toda_slots_from_info(info, place, today, limit)
        found += got
        if start is None or start + timedelta(days=6) >= limit:
            break
        if not any_status and start > today:     # 先の週が全部「―」なら、これ以上は受付前
            break
        info = _toda_next(page, info)
        if info is None:
            break


def _toda_wait(page):
    """場所を選んだあとの画面を待つ。'calendar'(カレンダー)か 'facilities'(施設の一覧)を返す。"""
    for _ in range(34):
        if page.locator("#areaToKomaDisplay").count() > 0:
            return "calendar"
        if page.get_by_role("button", name=re.compile(r"^野球場")).count() > 0:
            return "facilities"
        page.wait_for_timeout(300)
    return None


def _toda_wait_calendar(page):
    """施設を選んだあと、カレンダーが表示されるまで待つ(最大15秒)。"""
    for _ in range(50):
        if page.locator("#areaToKomaDisplay").count() > 0 and \
                page.locator('input[name$="displayStartDate"]').count() > 0:
            return True
        page.wait_for_timeout(300)
    return False


def _toda_screen_hint(page):
    """失敗したとき、いま画面にあるボタンの名前を、原因調査の手がかりとして返す。"""
    try:
        names = page.evaluate("""() => [...document.querySelectorAll('input[type=button],button')]
            .map(e => (e.value || e.innerText || '').trim()).filter(Boolean).slice(0, 25)""")
        return "ボタン: " + " / ".join(names)
    except Exception:
        return ""


def fetch_toda(page):
    found = []
    for place in TODA_PLACES:
        page.goto(TODA_URL, wait_until="domcontentloaded", timeout=60000)
        page.locator("#searchButton").click()
        page.get_by_role("button", name=place, exact=True).click()
        state = _toda_wait(page)
        if state == "calendar":                  # 施設が1つだけの場所は、すぐカレンダーが出る
            _toda_collect(page, place, found)
        elif state == "facilities":              # 施設の一覧から「野球場…」を1つずつ調べる
            names = []
            for b in page.get_by_role("button", name=re.compile(r"^野球場")).all():
                n = (b.get_attribute("value") or b.inner_text() or "").strip()   # <input type="button"> は value に文字が入る
                if n and n not in names:
                    names.append(n)
            for n in names:
                page.get_by_role("button", name=re.compile(rf"^{re.escape(n)}$")).click()
                if not _toda_wait_calendar(page):
                    raise RuntimeError(f"戸田市: {place} {n} のカレンダーを開けませんでした(画面: {_toda_screen_hint(page)})")
                _toda_collect(page, place, found)
                page.locator("#shisetsuReSelection").click()
                for _ in range(34):              # 施設の一覧に戻るまで待つ
                    if page.locator("#areaToKomaDisplay").count() == 0 and \
                            page.get_by_role("button", name=re.compile(r"^野球場")).count() > 0:
                        break
                    page.wait_for_timeout(300)
        else:
            raise RuntimeError(f"戸田市: {place} の画面を開けませんでした({_toda_screen_hint(page)})")
    if not found:
        raise RuntimeError("戸田市: 空き状況を取得できませんでした(画面構造を確認してください)")
    print(f"[toda] {len(found)} コマ(うち空き {sum(1 for x in found if x['status'] == 'open')})")
    return found


# window: 実行を許可する日本時間の時間帯(開始時, 終了時)。None なら常時
SOURCES = [
    {"id": "kawaguchi", "name": "川口市", "url": "https://kawaguchi-city.rakuriza.jp/",
     "fn": fetch_kawaguchi, "window": None, "interval_min": 20, "hours": "終日"},
    {"id": "toda", "name": "戸田市", "url": "https://yoyaku.city.toda.saitama.jp/yoyaku/",
     "fn": fetch_toda, "window": (9, 24), "interval_min": 20, "hours": "9:00〜23:59"},
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

    TEMPLATES.update(old.get("slot_templates", {}))
    global RULES
    RULES = load_rules()
    old_slots = old.get("slots", [])
    for fn in (NOTIFY_FILE, "notify_line.txt"):
        if os.path.exists(fn):
            os.remove(fn)
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
        "holidays": holidays_between(now.date(), now.date() + timedelta(days=120)) if jpholiday else old.get("holidays", []),
        "sources": [{"id": s["id"], "name": s["name"], "url": s["url"],
                     "interval_min": s["interval_min"], "hours": s["hours"]} for s in SOURCES],
        "source_status": status,
        "slot_templates": TEMPLATES,
        "slots": [x for lst in slots_by_src.values() for x in lst],
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)

    names = {x["id"]: x["name"] for x in SOURCES}
    if os.environ.get("NOTIFY_TEST") == "true":
        write_notification([], names, test=True)
        print("[notify] テスト通知を作成しました")
    elif NOTIFY and old_slots:             # 初回(前回データなし)は通知しない
        new = find_new_open(old_slots, out["slots"])
        if new:
            write_notification(new, names)
            print(f"[notify] 新しい空き {len(new)} 枠を通知します")


if __name__ == "__main__":
    main()
