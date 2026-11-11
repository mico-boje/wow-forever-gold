"""Save Wowhead pages *you* open in your own browser, for personal use.

Nothing here fetches from Wowhead. A userscript (Tampermonkey / Violentmonkey) runs on the
Wowhead Forever pages you visit, reads the tables the page already shows ("Dropped by",
"Skinned from", "Mined from", ...) and posts them to the local web app (`wowgold ui`), whose
Capture page lists the most valuable Forever-only items to look up.

Posts must carry a random token (embedded in the userscript) so other websites can't write
to the local app.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..planner.pricing import _latest_snapshot_ids

WOWHEAD = "https://www.wowhead.com/forever"
MAX_BODY = 20 * 1024 * 1024
PAGE_RE = re.compile(r"wowhead\.com/forever/(item|npc|object)=(\d+)")


def capture_dir(data_dir: Path) -> Path:
    return data_dir / "wowhead" / "captures"


def get_token(data_dir: Path) -> str:
    path = data_dir / "wowhead" / "token"
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token


def captured(data_dir: Path) -> dict[str, str]:
    """'item-123' -> captured_at, for every saved page."""
    out = {}
    for path in capture_dir(data_dir).glob("*.json"):
        try:
            out[path.stem] = json.loads(path.read_text()).get("captured_at", "")
        except (OSError, json.JSONDecodeError):
            continue
    return out


def save_capture(data_dir: Path, payload: dict) -> str:
    match = PAGE_RE.search(str(payload.get("url", "")))
    if not match:
        raise ValueError("not a Wowhead Forever item/npc/object page")
    key = f"{match[1]}-{match[2]}"
    record = {
        "url": payload["url"],
        "title": str(payload.get("title", ""))[:300],
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "listviews": payload.get("listviews") or {},
        "raw": [s for s in payload.get("raw") or [] if isinstance(s, str)][:50],
    }
    out = capture_dir(data_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{key}.json").write_text(json.dumps(record))
    return key


# -- to-do list ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    item_id: int
    name: str
    market: int  # copper
    listed: int

    @property
    def url(self) -> str:
        return f"{WOWHEAD}/item={self.item_id}"


def pick_targets(
    conn: sqlite3.Connection,
    region: str,
    house_ids: list[int],
    exclude: set[int],
    *,
    limit: int = 30,
    min_listed: int = 20,
) -> list[Target]:
    """Most valuable items on the AH that aren't in `exclude` (the vanilla item list):
    market price × listed quantity (capped, so one huge stack doesn't dominate)."""
    snaps = _latest_snapshot_ids(conn, region, house_ids)
    if not snaps:
        return []
    marks = ",".join("?" * len(snaps))
    rows = conn.execute(
        f"""
        SELECT p.item_id, COALESCE(i.name, 'item ' || p.item_id), SUM(p.quantity),
               SUM(p.market_unit * p.quantity) / SUM(p.quantity)
        FROM price_stats p LEFT JOIN items i ON i.item_id = p.item_id
        WHERE p.snapshot_id IN ({marks}) AND p.quantity > 0
        GROUP BY p.item_id
        """,
        snaps,
    ).fetchall()
    targets = [
        Target(item_id, name, int(market or 0), qty)
        for item_id, name, qty, market in rows
        if item_id not in exclude and qty >= min_listed and market
    ]
    targets.sort(key=lambda t: -t.market * min(t.listed, 200))
    return targets[:limit]


def userscript(port: int, token: str) -> str:
    return USERSCRIPT.replace("__PORT__", str(port)).replace("__TOKEN__", token)


USERSCRIPT = r"""// ==UserScript==
// @name         wowgold capture
// @namespace    wowgold
// @version      1
// @description  Save the Wowhead Forever pages you open to your local wowgold server.
// @match        https://www.wowhead.com/forever/item=*
// @match        https://www.wowhead.com/forever/npc=*
// @match        https://www.wowhead.com/forever/object=*
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==
(function () {
  "use strict";
  const ENDPOINT = "http://127.0.0.1:__PORT__/capture";
  const TOKEN = "__TOKEN__";
  const page = unsafeWindow || window;

  function plain(value) {
    const seen = new WeakSet();
    return JSON.parse(JSON.stringify(value, (k, v) => {
      if (typeof v === "function") return undefined;
      if (v && typeof v === "object") {
        if (seen.has(v)) return undefined;
        seen.add(v);
        if (v instanceof Node) return undefined;
      }
      return v;
    }));
  }

  function listviews() {
    const out = {};
    const all = page.g_listviews || {};
    for (const id of Object.keys(all)) {
      const lv = all[id];
      try {
        out[id] = { template: lv.template, name: plain(lv.name), data: plain(lv.data || []) };
      } catch (e) { out[id] = { error: String(e) }; }
    }
    return out;
  }

  function raw() {
    return Array.from(document.scripts)
      .map((s) => s.textContent || "")
      .filter((t) => t.includes("Listview"))
      .map((t) => t.slice(0, 2000000));
  }

  function badge(text, ok) {
    let el = document.getElementById("wowgold-badge");
    if (!el) {
      el = document.createElement("div");
      el.id = "wowgold-badge";
      el.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:2147483647;" +
        "padding:6px 10px;border-radius:6px;font:13px sans-serif;color:#fff";
      document.body.appendChild(el);
    }
    el.style.background = ok ? "#2e7d32" : "#b71c1c";
    el.textContent = "wowgold: " + text;
  }

  function send() {
    const payload = {
      token: TOKEN, url: location.href, title: document.title,
      listviews: listviews(), raw: raw(),
    };
    GM_xmlhttpRequest({
      method: "POST", url: ENDPOINT, data: JSON.stringify(payload),
      headers: { "Content-Type": "application/json" },
      onload: (r) => badge(r.status === 200 ? "saved" : "error " + r.status, r.status === 200),
      onerror: () => badge("local server not running", false),
    });
  }

  setTimeout(send, 1500);
})();
"""
