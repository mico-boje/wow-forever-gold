"""Fetch raw auction house snapshots and store them on disk + in SQLite.

- Houses are discovered via every ruleset's auction index, then deduplicated: each
  house is fetched once (the API has been observed to serve identical data per ruleset).
- `If-Modified-Since` avoids re-downloading unchanged houses; a content hash catches
  servers that ignore it.
- Raw bodies are written to data/raw/{region}/{house}/{kind}/{timestamp}.json.gz.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import db
from .blizzard import ApiResponse, AuctionHouse, ForeverApi, Ruleset
from .blizzard.forever import faction_from_house_name

KINDS = ("auctions", "commodities")


@dataclass
class HouseTarget:
    house: AuctionHouse
    ruleset_id: int  # ruleset used to fetch it
    ruleset_ids: list[int] = field(default_factory=list)  # every ruleset that lists it


@dataclass
class SnapshotSummary:
    houses: int = 0
    stored: int = 0
    unchanged: int = 0
    not_modified: int = 0
    errors: list[str] = field(default_factory=list)


def discover_houses(api: ForeverApi, rulesets: Iterable[Ruleset]) -> list[HouseTarget]:
    targets: dict[int, HouseTarget] = {}
    for rs in rulesets:
        houses, _ = api.auction_index(rs.id)
        for house in houses:
            target = targets.setdefault(house.id, HouseTarget(house=house, ruleset_id=rs.id))
            target.ruleset_ids.append(rs.id)
    return sorted(targets.values(), key=lambda t: t.house.id)


def count_auctions(payload: object) -> int:
    if isinstance(payload, dict) and isinstance(payload.get("auctions"), list):
        return len(payload["auctions"])
    return 0  # empty houses come back 200 without an `auctions` array


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _raw_path(
    data_dir: Path, region: str, house_id: int, kind: str, stamp: str, digest: str
) -> Path:
    # The content hash keeps two snapshots taken within the same second from colliding.
    safe = stamp.replace(":", "").replace("-", "").replace("+00:00", "Z")
    return data_dir / "raw" / region / str(house_id) / kind / f"{safe}-{digest[:12]}.json.gz"


def store_response(
    conn: sqlite3.Connection,
    data_dir: Path,
    region: str,
    target: HouseTarget,
    kind: str,
    resp: ApiResponse,
) -> str:
    """Record one fetch result; returns the outcome string."""
    fetched_at = _now()
    base = dict(
        region=region,
        ruleset_id=target.ruleset_id,
        house_id=target.house.id,
        kind=kind,
        fetched_at=fetched_at,
        http_status=resp.status,
    )
    if resp.not_modified:
        db.insert_snapshot(conn, db.SnapshotRow(**base, outcome="not_modified"))
        return "not_modified"
    if not resp.ok:
        db.insert_snapshot(conn, db.SnapshotRow(**base, outcome="error"))
        return "error"

    digest = hashlib.sha256(resp.content).hexdigest()
    payload = json.loads(resp.content) if resp.content else {}
    n = count_auctions(payload)
    prev = db.latest_stored(conn, region, target.house.id, kind)
    common = dict(
        last_modified=resp.last_modified, sha256=digest, n_auctions=n, bytes=len(resp.content)
    )
    if prev is not None and prev["sha256"] == digest:
        db.insert_snapshot(conn, db.SnapshotRow(**base, outcome="unchanged", **common))
        return "unchanged"

    path = _raw_path(data_dir, region, target.house.id, kind, fetched_at, digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(resp.content))
    db.insert_snapshot(conn, db.SnapshotRow(**base, outcome="stored", path=str(path), **common))
    return "stored"


def run_snapshot(
    api: ForeverApi,
    conn: sqlite3.Connection,
    data_dir: Path,
    *,
    house_ids: set[int] | None = None,
    kinds: Iterable[str] = KINDS,
    on_progress: Callable[[str], None] | None = None,
) -> SnapshotSummary:
    region = api.client.region
    summary = SnapshotSummary()
    rulesets = api.rulesets()
    if not rulesets:
        summary.errors.append("ruleset index unavailable")
        return summary

    names = {r.id: r.name for r in rulesets}
    targets = discover_houses(api, rulesets)
    now = _now()
    for t in targets:
        db.upsert_house(
            conn, region, t.house.id, t.house.name, now, faction_from_house_name(t.house.name)
        )
        for rid in t.ruleset_ids:
            db.link_house_ruleset(conn, region, t.house.id, rid, names.get(rid, str(rid)))
    conn.commit()

    if house_ids:
        targets = [t for t in targets if t.house.id in house_ids]
    summary.houses = len(targets)

    for t in targets:
        for kind in kinds:
            prev = db.latest_stored(conn, region, t.house.id, kind)
            ims = prev["last_modified"] if prev is not None else None
            fetch = api.house_auctions if kind == "auctions" else api.house_commodities
            resp = fetch(t.ruleset_id, t.house.id, if_modified_since=ims)
            outcome = store_response(conn, data_dir, region, t, kind, resp)
            conn.commit()
            if outcome == "error":
                summary.errors.append(f"house {t.house.id} {kind}: HTTP {resp.status}")
            else:
                setattr(summary, outcome, getattr(summary, outcome) + 1)
            if on_progress:
                on_progress(f"house {t.house.id} ({t.house.name or '?'}) {kind}: {outcome}")
    return summary
