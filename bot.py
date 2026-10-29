"""Dead Frontier Bossmap Discord 機器人（GitHub Actions 版）

每次執行（排程每 15 分鐘一次）：
  1. 用 Playwright 開 dfprofiler bossmap，讀 Current Cycle 表格、地圖格子、
     Last World Reset，並攔截頁面自己發的 /bossmap/json/ 請求（座標與時間）
  2. 偵測「每日特殊王」與「活動王」；同一個遊戲日內，每隻第一次出現時發通報（附格子圖）
  3. 進入新的整點後的第一次執行，發整點報告（地圖截圖 + Current Cycle + 今日特殊王 ✅/❌），
     只保留最近 N 則整點報告，較舊的自動刪除
狀態存在 state.json，由 workflow 提交回 repo。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "state.json"

COLOR_DAILY = 0xE74C3C
COLOR_EVENT = 0xF39C12
COLOR_REPORT = 0x2B2D31


def log(*args) -> None:
    print(datetime.now(timezone.utc).strftime("[%H:%M:%S]"), *args, flush=True)


# ---------------------------------------------------------------- 設定與狀態

def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if os.environ.get("BOSSMAP_URL"):          # 測試用
        cfg["bossmap_url"] = os.environ["BOSSMAP_URL"]
    return cfg


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            log("state.json 壞掉，重新開始")
    return {}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")


# ---------------------------------------------------------------- 純邏輯（可測試）

_BREAK = re.compile(r"\s*(?:<br\s*/?>|\n|\+)\s*", re.IGNORECASE)
_COUNT = re.compile(r"^\s*(\d+)\s*x\s*(.+?)\s*$", re.IGNORECASE)


def split_bosses(text: str) -> list[tuple[int, str]]:
    """'2 x Flaming Zombie<br />1 x Riot Shield Guy' -> [(2,'Flaming Zombie'),(1,'Riot Shield Guy')]"""
    out = []
    for seg in _BREAK.split(text or ""):
        seg = seg.strip()
        if not seg or seg == "0":
            continue
        m = _COUNT.match(seg)
        out.append((int(m.group(1)), m.group(2)) if m else (1, seg))
    return out


def fmt_bosses(bosses: list[tuple[int, str]]) -> str:
    return " + ".join(f"{n} x {name}" for n, name in bosses)


@dataclass
class Row:
    slot: str
    bosses: list[tuple[int, str]]


@dataclass
class Event:
    bosses: list[tuple[int, str]]
    coords: list[tuple[int, int]]
    start: float | None
    end: float | None

    @property
    def duration_min(self) -> float:
        if self.start is None or self.end is None:
            return 0.0
        return (self.end - self.start) / 60

    @property
    def total(self) -> int:
        return sum(n for n, _ in self.bosses)


def _num(v):
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def parse_rows(raw_rows: list[list[str]]) -> list[Row]:
    rows = []
    for cells in raw_rows or []:
        if len(cells) < 2:
            continue
        slot, boss = cells[0].strip(), cells[1]
        bosses = split_bosses(boss)
        if slot and bosses:
            rows.append(Row(slot, bosses))
    return rows


def parse_events(payload, now: float) -> list[Event]:
    """bossmap/json 的內容 -> 仍存活的非任務事件"""
    events = []
    if not isinstance(payload, dict):
        return events
    for raw in payload.values():
        if not isinstance(raw, dict):
            continue
        if str(raw.get("event_type") or "").strip().lower() == "mission":
            continue
        bosses = split_bosses(str(raw.get("special_enemy_type") or ""))
        if not bosses:
            continue
        end = _num(raw.get("end_time"))
        if end is not None and end <= now:
            continue
        coords = []
        for loc in raw.get("locations") or []:
            if isinstance(loc, (list, tuple)) and len(loc) >= 2:
                x, y = _num(loc[0]), _num(loc[1])
                if x is not None and y is not None and (int(x), int(y)) not in coords:
                    coords.append((int(x), int(y)))
        events.append(Event(bosses, coords, _num(raw.get("start_time")), end))
    return events


_RESET = re.compile(r"Last World Reset:\s*(\d{4}-\d{2}-\d{2})\s*at\s*(\d{1,2}:\d{2})\s*GMT([+-]\d{4})")


def parse_reset(text: str, now: float) -> tuple[str, float]:
    """回傳 (遊戲日 key, 重置的 unix 時間)。抓不到時用每天 01:00 GMT-7 推算。"""
    m = _RESET.search(text or "")
    if m:
        date, hm, off = m.groups()
        tz = timezone(timedelta(hours=int(off[:3]), minutes=int(off[0] + off[3:])))
        dt = datetime.strptime(f"{date} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        return f"{date} {hm} GMT{off}", dt.timestamp()
    tz = timezone(timedelta(hours=-7))
    local = datetime.fromtimestamp(now, tz)
    reset = local.replace(hour=1, minute=0, second=0, microsecond=0)
    if local < reset:
        reset -= timedelta(days=1)
    return reset.strftime("%Y-%m-%d %H:%M GMT-0700"), reset.timestamp()


@dataclass
class Sighting:
    key: str                       # 顯示/記錄用名稱
    kind: str                      # "daily" | "event"
    slots: list[str] = field(default_factory=list)
    coords: list[tuple[int, int]] = field(default_factory=list)
    start: float | None = None
    end: float | None = None
    ghost: bool = False


def _match(name: str, wanted: list[str]) -> str | None:
    low = name.lower().strip()
    for w in wanted:
        if low == w.lower():
            return w
    for w in wanted:
        if w.lower() in low:
            return name.strip()
    return None


def detect(rows: list[Row], events: list[Event], slot_cells: dict[str, list[tuple[int, int]]],
           cfg: dict, reset_ts: float, json_ok: bool) -> dict[str, Sighting]:
    daily = cfg["daily_specials"]
    event_names = cfg["event_bosses"]
    window = float(cfg.get("daily_window_minutes", 90))
    b = cfg["map_bounds"]

    def inside(c):
        return b["x_min"] <= c[0] <= b["x_max"] and b["y_min"] <= c[1] <= b["y_max"]

    found: dict[str, Sighting] = {}

    def add(key, kind, slot=None, coords=(), start=None, end=None, ghost=False):
        # 開始時間早於本次重置 = 上一個遊戲日留下來的，不算今天
        if start is not None and start < reset_ts - 60:
            return
        s = found.setdefault(key, Sighting(key, kind))
        if slot and slot not in s.slots:
            s.slots.append(slot)
        for c in coords:
            if c not in s.coords:
                s.coords.append(c)
        if start is not None:
            s.start = start if s.start is None else min(s.start, start)
        if end is not None:
            s.end = end if s.end is None else max(s.end, end)
        s.ghost = s.ghost or ghost

    # 來源一：JSON 事件（有座標與時間）
    for ev in events:
        for _, name in ev.bosses:
            d = _match(name, daily)
            if d:
                if d.lower() == "devil hound":
                    # 一般 Wasteland 輪也會出 Devil Hound；每日那隻是「單隻 + 長時間」
                    if not (ev.total == 1 and ev.duration_min > window):
                        continue
                add(d, "daily", coords=ev.coords, start=ev.start, end=ev.end)
                continue
            e = _match(name, event_names)
            if e:
                add(e, "event", coords=ev.coords, start=ev.start, end=ev.end)

    # 來源二：畫面上的 Current Cycle 表格（Slot 標籤 + 地圖格子）
    for row in rows:
        is_ghost = row.slot.upper() == "GHOST"
        for _, name in row.bosses:
            d = _match(name, daily)
            if d:
                if d.lower() == "devil hound":
                    if is_ghost:
                        add(d, "daily", slot=row.slot, ghost=True)
                    elif d in found:
                        # JSON 已確認是每日那隻 → 補上 slot；
                        # 沒有 JSON 時無法和一般 Wasteland 的 Devil Hound 區分，保守略過
                        add(d, "daily", slot=row.slot)
                    continue
                add(d, "daily", slot=row.slot, coords=slot_cells.get(row.slot, []))
                continue
            e = _match(name, event_names)
            if e:
                add(e, "event", slot=row.slot, coords=slot_cells.get(row.slot, []))

    for s in found.values():
        if s.coords and not any(inside(c) for c in s.coords):
            s.ghost = True
        s.coords = [c for c in s.coords if inside(c)] or s.coords
    return found


def roll_game_day(state: dict, day_key: str) -> None:
    if state.get("game_day") != day_key:
        log(f"新的遊戲日：{day_key}（上一個：{state.get('game_day')}）")
        state["game_day"] = day_key
        state["seen_today"] = {}


def new_alerts(state: dict, sightings: dict[str, Sighting], now: float) -> list[Sighting]:
    seen = state.setdefault("seen_today", {})
    last = state.setdefault("event_last_seen", {})
    out = []
    for key, s in sightings.items():
        if s.kind == "event":
            last[key] = int(now // 3600 * 3600)   # 取整點，避免每次執行都改動 state
        if key in seen:
            continue
        seen[key] = {"kind": s.kind, "first_seen": now, "coords": [list(c) for c in s.coords[:6]],
                     "slots": s.slots, "ghost": s.ghost, "end": s.end}
        out.append(s)
    return out


# ----------------------------------------------------------------  內容

def ts(t: float, style: str = "t") -> str:
    return f"<t:{int(t)}:{style}>"


def status_lines(state: dict, cfg: dict, now: float) -> list[str]:
    seen = state.get("seen_today", {})
    lines = ["**Today's Daily Specials**"]
    for name in cfg["daily_specials"]:
        info = seen.get(name)
        if not info:
            lines.append(f"❌ {name}")
            continue
        icon = "👻" if info.get("ghost") else "✅"
        extra = f"spotted {ts(info['first_seen'])}"
        if info.get("ghost"):
            extra += " · GHOST (unreachable)"
        elif info.get("coords"):
            x, y = info["coords"][0]
            extra += f" · ({x}, {y})"
        if info.get("end"):
            extra += f" · until {ts(info['end'])}"
        lines.append(f"{icon} {name} — {extra}")

    # 活動王：今天出過的打勾；最近幾天出過但今天還沒出的打叉
    keep = float(cfg.get("event_expected_days", 3)) * 86400
    recent = {k for k, t in state.get("event_last_seen", {}).items() if now - t <= keep}
    today = {k for k, v in seen.items() if v.get("kind") == "event"}
    names = sorted(today | recent)
    if names:
        lines.append("")
        lines.append("**Today's Event Bosses**")
        for name in names:
            info = seen.get(name)
            if info:
                slots = f" · {'/'.join(info['slots'])}" if info.get("slots") else ""
                lines.append(f"✅ {name} — spotted {ts(info['first_seen'])}{slots}")
            else:
                lines.append(f"❌ {name}")
    return lines


def cycle_block(rows: list[Row], events: list[Event]) -> str:
    if rows:
        w = max(len(r.slot) for r in rows)
        body = [f"{r.slot.ljust(w)} | {fmt_bosses(r.bosses)}" for r in rows]
    else:
        body = [fmt_bosses(e.bosses) for e in events] or ["(no data)"]
    return "```\n" + "\n".join(body) + "\n```"


def build_report(rows, events, state, cfg, reset_text, now, has_image) -> dict:
    desc = [f"⏱️ **Last World Reset:** `{reset_text}`",
            f"🕒 **Last Checked:** {ts(now, 'f')}",
            ""]
    desc += status_lines(state, cfg, now)
    desc += ["", "**Current Cycle**"]
    block = cycle_block(rows, events)
    room = 4000 - len("\n".join(desc))
    if len(block) > room:
        block = block[: max(0, room - 20)] + "\n... (truncated)\n```"
    desc.append(block)
    embed = {"title": "📊 Dead Frontier Live Bossmap Tracker", "url": cfg["bossmap_urls"],
             "description": "\n".join(desc), "color": COLOR_REPORT}
    if has_image:
        embed["image"] = {"url": "attachment://bossmap.png"}
    return embed


def build_alert(s: Sighting, cfg: dict, has_image: bool) -> dict:
    name = s.key
    if s.ghost:
        head = f"👻 A {name} has spawned as a **GHOST** (off-map, unreachable)"
    elif s.coords:
        x, y = s.coords[0]
        head = f"✅ A {name} has spawned at **({x}, {y})**!"
    else:
        head = f"✅ A {name} has spawned!"
    lines = [head]
    if len(s.coords) > 1:
        more = ", ".join(f"({x}, {y})" for x, y in s.coords[1:6])
        lines.append(f"Other locations: {more}" + ("…" if len(s.coords) > 6 else ""))
    if s.slots:
        lines.append(f"Slot: {' / '.join(s.slots)}")
    if s.end:
        lines.append(f"Despawns at {ts(s.end)} ({ts(s.end, 'R')})")
    embed = {"title": ("Daily Special" if s.kind == "daily" else "Event Boss") + f": {name}",
             "url": cfg["bossmap_url"], "description": "\n".join(lines),
             "color": COLOR_DAILY if s.kind == "daily" else COLOR_EVENT,
             "footer": {"text": "Data: DFProfiler"}}
    if has_image:
        embed["image"] = {"url": "attachment://tile.png"}
    return embed


# ----------------------------------------------------------------  Webhook

class Webhook:
    def __init__(self, url: str):
        import requests
        self.http = requests.Session()
        base, _, query = url.partition("?")
        self.base = base.rstrip("/")
        self.params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)

    def _call(self, method, path="", **kw):
        params = dict(self.params)
        params.update(kw.pop("params", {}))
        for attempt in range(5):
            r = self.http.request(method, self.base + path, params=params, timeout=30, **kw)
            if r.status_code == 429:
                wait = float(r.json().get("retry_after", 2))
                log(f" 限流，等 {wait}s")
                time.sleep(wait + 0.5)
                continue
            return r
        return r

    def send(self, embed: dict, content: str = "", image: bytes | None = None,
             filename: str = "image.png") -> str | None:
        payload = {"embeds": [embed], "allowed_mentions": {"parse": ["roles", "everyone", "users"]}}
        if content:
            payload["content"] = content
        if image:
            payload["attachments"] = [{"id": 0, "filename": filename}]
            r = self._call("POST", params={"wait": "true"},
                           data={"payload_json": json.dumps(payload)},
                           files={"files[0]": (filename, image, "image/png")})
        else:
            r = self._call("POST", params={"wait": "true"}, json=payload)
        if r.status_code >= 300:
            log(f"發送失敗 HTTP {r.status_code}: {r.text[:300]}")
            return None
        return str(r.json().get("id"))

    def delete(self, message_id: str) -> bool:
        r = self._call("DELETE", f"/messages/{message_id}")
        if r.status_code in (200, 204, 404):
            return True
        log(f"刪除 {message_id} 失敗 HTTP {r.status_code}: {r.text[:200]}")
        return False


# ---------------------------------------------------------------- 抓網頁（Playwright）

JS_TABLES = r"""
() => {
  const out = {current: [], cells: []};
  for (const t of document.querySelectorAll('table')) {
    const head = (t.rows[0] ? t.rows[0].innerText : '').trim();
    if (!/^Current Cycle/i.test(head)) continue;
    for (const r of Array.from(t.rows).slice(1)) {
      const c = Array.from(r.cells).map(x => x.innerText.trim());
      if (c.length < 2 || /^slot$/i.test(c[0])) continue;
      out.current.push(c);
    }
  }
  for (const td of document.querySelectorAll('td.coord')) {
    const txt = (td.innerText || '').trim();
    if (!txt) continue;
    let x = null, y = null;
    for (const cls of td.classList) {
      let m = /^x(\d+)$/.exec(cls); if (m) x = +m[1];
      m = /^y(\d+)$/.exec(cls); if (m) y = +m[1];
    }
    if (x !== null && y !== null) out.cells.push([txt, x, y]);
  }
  return out;
}
"""

JS_TABLE_READY = r"""
() => Array.from(document.querySelectorAll('table')).some(t =>
  t.rows[0] && /^Current Cycle/i.test(t.rows[0].innerText.trim()) &&
  Array.from(t.rows).slice(1).some(r => r.cells.length >= 2 && !/^slot$/i.test(r.cells[0].innerText.trim())))
"""


@dataclass
class Snapshot:
    rows_raw: list
    cells: list
    payload: object
    page_text: str
    map_png: bytes | None


def map_locator(page):
    for sel in ("#boss-table", "table:has(td.coord)"):
        loc = page.locator(sel).first
        try:
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            pass
    return None


def scrape(page, url: str) -> Snapshot:
    payload = None
    try:
        with page.expect_response(lambda r: "/bossmap/json" in r.url, timeout=45000) as info:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        payload = info.value.json()
        log(f"攔截到 JSON：{len(payload) if isinstance(payload, dict) else '?'} 筆")
    except Exception as e:
        log(f"沒有攔截到 JSON（{type(e).__name__}），改用畫面表格")
        if not page.url.startswith(url):
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_function(JS_TABLE_READY, timeout=25000)
    except Exception:
        log("Current Cycle 表格在 25 秒內沒有內容")
    page.wait_for_timeout(1500)
    data = page.evaluate(JS_TABLES)
    text = page.inner_text("body")
    png = None
    loc = map_locator(page)
    if loc is not None:
        try:
            png = loc.screenshot(timeout=20000)
        except Exception as e:
            log(f"地圖截圖失敗：{e}")
    if png is None:
        png = page.screenshot(full_page=True)
    return Snapshot(data.get("current", []), data.get("cells", []), payload, text, png)


def tile_image(page, x: int, y: int) -> bytes | None:
    """點地圖格子，拿官方的 Fairview_XxY 格子圖；失敗就裁地圖上那一區放大。"""
    cell = page.locator(f"td.coord.x{x}.y{y}").first
    try:
        if not cell.count():
            return None
        cell.click(force=True, timeout=5000)
        img = page.locator("#mission-info img").first
        img.wait_for(state="attached", timeout=8000)
        for _ in range(16):
            src = img.get_attribute("src") or ""
            if f"{x}x{y}" in src:
                break
            page.wait_for_timeout(250)
        if f"{x}x{y}" in src:
            full = page.evaluate("s => new URL(s, location.href).href", src)
            r = page.request.get(full, timeout=20000)
            if r.ok and len(r.body()) > 500:
                return r.body()
    except Exception as e:
        log(f"格子圖 ({x},{y}) 取得失敗：{type(e).__name__}，改用地圖裁切")
    finally:
        page.evaluate("() => { const h = document.getElementById('mission-holder'); if (h) h.style.display = 'none'; }")
    try:
        box, m = cell.bounding_box(), map_locator(page)
        mb = m.bounding_box() if m else None
        if box and mb:
            pad = box["width"] * 7
            x0 = max(mb["x"], box["x"] - pad)
            y0 = max(mb["y"], box["y"] - pad)
            x1 = min(mb["x"] + mb["width"], box["x"] + box["width"] + pad)
            y1 = min(mb["y"] + mb["height"], box["y"] + box["height"] + pad)
            return page.screenshot(clip={"x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
    except Exception as e:
        log(f"地圖裁切失敗：{e}")
    return None


# ---------------------------------------------------------------- 主流程

def main() -> int:
    from playwright.sync_api import sync_playwright

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    dry = os.environ.get("DRY_RUN") == "1" or not webhook_url
    force = os.environ.get("FORCE_REPORT", "").lower() in ("1", "true", "yes")
    if dry:
        log("DRY_RUN：只印出內容，不發到 Discord")

    cfg = load_config()
    state = load_state()
    now = time.time()
    hook = None if dry else Webhook(webhook_url)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1400, "height": 1600}, device_scale_factor=2,
                                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                           "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
        snap = None
        # This safely looks for your new list, or falls back to the old single string if missing
        urls_to_try = cfg.get("bossmap_urls", [cfg.get("bossmap_url")])
    
        for url in urls_to_try:
            if not url:
                continue
            try:
                log(f"Attempting to scrape boss data from: {url}")
                snap = scrape(page, url)
                if snap:
                        break  # Success! Break out of the loop and continue running
            except Exception as e:
                log(f"Failed to load {url} due to error: {e}. Trying next fallback...")
    
        if not snap:
            log("All available bossmap mirrors are currently offline or timed out.")
            browser.close()
            return 1

        rows = parse_rows(snap.rows_raw)
        events = parse_events(snap.payload, now)
        slot_cells: dict[str, list[tuple[int, int]]] = {}
        for txt, x, y in snap.cells:
            slot_cells.setdefault(txt.strip(), []).append((x, y))
        day_key, reset_ts = parse_reset(snap.page_text, now)
        log(f"表格 {len(rows)} 列、JSON 事件 {len(events)} 筆、遊戲日 {day_key}")

        if not rows and not events:
            log("抓不到任何 boss 資料（網站可能改版或擋住），這次不發文")
            raise ValueError("No boss data retrieved from this URL")

        roll_game_day(state, day_key)
        sightings = detect(rows, events, slot_cells, cfg, reset_ts, json_ok=snap.payload is not None)
        alerts = new_alerts(state, sightings, now)
        log(f"目前追蹤到：{list(sightings)}；新通報：{[s.key for s in alerts]}")

        for s in alerts:
            tile = None
            if s.coords and not s.ghost:
                tile = tile_image(page, *s.coords[0])
            embed = build_alert(s, cfg, has_image=tile is not None)
            if dry:
                print(json.dumps(embed, ensure_ascii=False, indent=2))
                if tile:
                    (ROOT / f"debug_tile_{s.key.replace(' ', '_')}.png").write_bytes(tile)
            else:
                hook.send(embed, content=cfg.get("alert_mention", ""), image=tile, filename="tile.png")

        hour_key = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H")
        if force or state.get("last_report_hour") != hour_key:
            reset_text = day_key
            embed = build_report(rows, events, state, cfg, reset_text, now, has_image=bool(snap.map_png))
            if dry:
                print(json.dumps(embed, ensure_ascii=False, indent=2))
                if snap.map_png:
                    (ROOT / "debug_bossmap.png").write_bytes(snap.map_png)
                state["last_report_hour"] = hour_key
            else:
                mid = hook.send(embed, image=snap.map_png, filename="bossmap.png")
                if mid:
                    state["last_report_hour"] = hour_key
                    ids = state.setdefault("hourly_messages", [])
                    ids.append(mid)
                    keep = int(cfg.get("keep_hourly_messages", 12))
                    while len(ids) > keep:
                        old = ids[0]
                        if hook.delete(old):
                            ids.pop(0)
                        else:
                            break
        else:
            log(f"本小時（{hour_key} UTC）已發過整點報告")
        browser.close()

    # 清掉太久沒出現的活動王紀錄
    keep = float(cfg.get("event_expected_days", 3)) * 86400
    state["event_last_seen"] = {k: t for k, t in state.get("event_last_seen", {}).items() if now - t <= keep}
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
