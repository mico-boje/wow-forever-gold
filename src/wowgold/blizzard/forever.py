"""WoW: Forever-specific API wrappers (rulesets and auction houses).

Observed structure (community probing, Nov 2026):
  /data/wow/ruleset/index                               -> rulesets (PvP, Normal, RP, RP-PvP)
  /data/wow/ruleset/{id}/auctions/  (or .../index)      -> auction house index
  /data/wow/ruleset/{id}/auctions/{houseId}             -> regular auctions
  /data/wow/ruleset/{id}/auctions/{houseId}/commodities -> commodity auctions
Every ruleset has been reported to list the *same* houses, so callers should dedupe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import namespaces
from .client import ApiResponse, BlizzardClient

AUCTION_INDEX_PATHS = (
    "/data/wow/ruleset/{rid}/auctions/",
    "/data/wow/ruleset/{rid}/auctions/index",
)
_HOUSE_HREF = re.compile(r"/auctions/(\d+)(?:[/?]|$)")


@dataclass(frozen=True)
class Ruleset:
    id: int
    name: str


@dataclass(frozen=True)
class AuctionHouse:
    id: int
    name: str | None


def parse_rulesets(payload: dict[str, Any]) -> list[Ruleset]:
    items = payload.get("rulesets") or payload.get("results") or []
    out = []
    for item in items:
        data = item.get("data", item)
        if "id" in data:
            out.append(Ruleset(id=int(data["id"]), name=_name(data.get("name")) or str(data["id"])))
    return out


def parse_auction_index(payload: Any) -> list[AuctionHouse]:
    """Extract auction houses from an index payload of unknown exact shape.

    Walks the JSON looking for objects with a `key.href` pointing at `/auctions/{id}`,
    falling back to objects with a numeric `id` inside an `auctions` list.
    """
    found: dict[int, AuctionHouse] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            href = (
                (node.get("key") or {}).get("href") if isinstance(node.get("key"), dict) else None
            )
            if href and (m := _HOUSE_HREF.search(href)):
                hid = int(m.group(1))
                found.setdefault(hid, AuctionHouse(id=hid, name=_name(node.get("name"))))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    if not found and isinstance(payload, dict):
        for item in payload.get("auctions") or []:
            if isinstance(item, dict) and isinstance(item.get("id"), int):
                found[item["id"]] = AuctionHouse(id=item["id"], name=_name(item.get("name")))
    return sorted(found.values(), key=lambda h: h.id)


def faction_from_house_name(name: str | None) -> str | None:
    """'Alliance Auction House' -> 'alliance' (names are only returned for valid locales)."""
    if not name:
        return None
    first = name.split()[0].lower()
    return first if first in {"alliance", "horde", "blackwater"} else None


def _name(value: Any) -> str | None:
    # Names come back as a plain string when `locale` is set, or a locale dict otherwise.
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("en_US") or value.get("en_GB") or next(iter(value.values()), None)
    return None


class ForeverApi:
    def __init__(self, client: BlizzardClient) -> None:
        self.client = client

    @property
    def ns_dynamic(self) -> str:
        return namespaces.dynamic(self.client.region)

    def rulesets(self, *, resolve_names: bool = True) -> list[Ruleset]:
        resp = self.client.get("/data/wow/ruleset/index", self.ns_dynamic)
        rulesets = parse_rulesets(resp.json()) if resp.ok else []
        if not resolve_names:
            return rulesets
        # The index has no names (Nov 2026); the per-ruleset detail does.
        out = []
        for rs in rulesets:
            if rs.name == str(rs.id):
                detail = self.client.get(f"/data/wow/ruleset/{rs.id}", self.ns_dynamic)
                if detail.ok and (name := _name(detail.json().get("name"))):
                    rs = Ruleset(id=rs.id, name=name)
            out.append(rs)
        return sorted(out, key=lambda r: r.id)

    def auction_index(self, ruleset_id: int) -> tuple[list[AuctionHouse], ApiResponse]:
        """Try the known index path variants; return houses and the response used."""
        last: ApiResponse | None = None
        for template in AUCTION_INDEX_PATHS:
            last = self.client.get(template.format(rid=ruleset_id), self.ns_dynamic)
            if last.ok:
                return parse_auction_index(last.json()), last
        assert last is not None
        return [], last

    def house_auctions(
        self, ruleset_id: int, house_id: int, *, if_modified_since: str | None = None
    ) -> ApiResponse:
        return self.client.get(
            f"/data/wow/ruleset/{ruleset_id}/auctions/{house_id}",
            self.ns_dynamic,
            if_modified_since=if_modified_since,
        )

    def house_commodities(
        self, ruleset_id: int, house_id: int, *, if_modified_since: str | None = None
    ) -> ApiResponse:
        return self.client.get(
            f"/data/wow/ruleset/{ruleset_id}/auctions/{house_id}/commodities",
            self.ns_dynamic,
            if_modified_since=if_modified_since,
        )
