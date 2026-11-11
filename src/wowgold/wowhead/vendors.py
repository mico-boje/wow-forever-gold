"""Forever vendor prices from captured item pages ("Sold by" tables).

Matters most for the Black Market vendor (Mister Graphed in Powderfuse Port), who sells
high-end reagents at a fixed price with unlimited stock: crafters can buy them there, and
nobody pays more than that on the AH.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .capture import capture_dir
from .sources import parse_raw_listviews


@dataclass(frozen=True)
class VendorOffer:
    item_id: int
    price: float  # copper per unit
    vendor: str
    npc_id: int
    limited: bool  # limited stock (restocks slowly)


def _money(cost) -> int | None:
    """Wowhead cost: [[copper]] or [[copper, [[item, n], ...], [[currency, n], ...]]].
    Gold-only offers return the copper; anything also costing items or currencies, None."""
    if not isinstance(cost, list) or not cost:
        return None
    first = cost[0]
    if isinstance(first, list):
        if not first or not isinstance(first[0], int | float):
            return None
        if any(extra for extra in first[1:]):
            return None
        return int(first[0])
    return int(first) if isinstance(first, int | float) else None


def offers_from_capture(record: dict, item_id: int) -> list[VendorOffer]:
    tables = []
    lv = (record.get("listviews") or {}).get("sold-by")
    if isinstance(lv, dict) and lv.get("data"):
        tables.append(lv["data"])
    else:
        for script in record.get("raw") or []:
            tables += [rows for tab, _, rows in parse_raw_listviews(script) if tab == "sold-by"]
    out = []
    for rows in tables:
        for row in rows:
            if not isinstance(row, dict):
                continue
            money = _money(row.get("cost"))
            if not money:
                continue
            stack = row.get("stack") or 1
            stock = row.get("stock")
            out.append(
                VendorOffer(
                    item_id=item_id,
                    price=money / max(int(stack), 1),
                    vendor=str(row.get("name") or "vendor"),
                    npc_id=int(row.get("id") or 0),
                    limited=isinstance(stock, int) and stock > 0,
                )
            )
    return out


def load_vendor_offers(data_dir: Path) -> dict[int, VendorOffer]:
    """Cheapest unlimited (else cheapest) gold-only vendor offer per captured item."""
    best: dict[int, VendorOffer] = {}
    for path in sorted(capture_dir(data_dir).glob("item-*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for offer in offers_from_capture(record, int(path.stem.split("-", 1)[1])):
            cur = best.get(offer.item_id)
            if cur is None or (offer.limited, offer.price) < (cur.limited, cur.price):
                best[offer.item_id] = offer
    return best
