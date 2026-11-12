"""Which auction house is yours.

The API lists 15 houses (Alliance / Horde / Blackwater for each ruleset) under every ruleset
with identical data, so it can't say which ruleset a house belongs to. Two ways to find out:

- **Fingerprint:** list any item in game at an unusual price (say 12g 34s 56c). After the next
  snapshot, exactly one house has that listing. `find_listing` searches the raw snapshots.
- **Manual:** pick it, e.g. after comparing a few prices in game.

The choice is stored per region and faction in data/my_houses.json and used wherever a house
isn't given explicitly (after WOWGOLD_HOUSES).
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


def _path(data_dir: Path) -> Path:
    return data_dir / "my_houses.json"


def load_my_houses(data_dir: Path, region: str) -> dict[str, int]:
    """faction -> house id you confirmed, for one region."""
    try:
        return {f: int(h) for f, h in json.loads(_path(data_dir).read_text())[region].items()}
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {}


def set_my_house(data_dir: Path, region: str, faction: str, house_id: int | None) -> None:
    try:
        data = json.loads(_path(data_dir).read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    mine = data.setdefault(region, {})
    if house_id is None:
        mine.pop(faction, None)
    else:
        mine[faction] = int(house_id)
    _path(data_dir).parent.mkdir(parents=True, exist_ok=True)
    _path(data_dir).write_text(json.dumps(data, indent=2))


@dataclass(frozen=True)
class ListingMatch:
    house_id: int
    faction: str | None
    kind: str  # auctions | commodities
    quantity: int
    fetched_at: str


def _matches(entry: dict, item_id: int, price: int) -> bool:
    if (entry.get("item") or {}).get("id") != item_id:
        return False
    qty = entry.get("quantity") or 1
    unit = entry.get("unit_price")
    buyout = entry.get("buyout")
    return price in (unit, buyout, entry.get("bid")) or (
        buyout is not None and buyout == price * qty
    )


def find_listing(
    conn: sqlite3.Connection, region: str, item_id: int, price: int
) -> list[ListingMatch]:
    """Houses whose latest snapshot has a listing of `item_id` at exactly `price` copper
    (per unit, or as the buyout of the whole stack)."""
    rows = conn.execute(
        """
        SELECT s.house_id, s.kind, s.path, s.fetched_at, h.faction
        FROM snapshots s JOIN houses h ON h.region = s.region AND h.house_id = s.house_id
        WHERE s.region = ? AND s.outcome = 'stored' AND s.path IS NOT NULL
          AND s.id IN (SELECT MAX(id) FROM snapshots WHERE region = ? AND outcome = 'stored'
                       GROUP BY house_id, kind)
        ORDER BY s.house_id
        """,
        (region, region),
    ).fetchall()
    out = []
    for house_id, kind, path, fetched_at, faction in rows:
        try:
            body = json.loads(gzip.decompress(Path(path).read_bytes()))
        except (OSError, ValueError):
            continue
        qty = sum(
            e.get("quantity") or 1
            for e in body.get("auctions") or []
            if _matches(e, item_id, price)
        )
        if qty:
            out.append(ListingMatch(house_id, faction, kind, qty, fetched_at))
    return out


def house_source(
    house_ids: list[int],
    faction: str,
    *,
    explicit: list[int] | None,
    configured: list[int] | None,
    mine: dict[str, int],
) -> str:
    """How the houses were chosen: chosen | configured | confirmed | guess."""
    if explicit:
        return "chosen"
    if configured:
        return "configured"
    if mine.get(faction) and house_ids == [mine[faction]]:
        return "confirmed"
    return "guess"
