"""Turn recorded activity segments (from the WowGoldLogger addon) into method sessions.

A segment is a stretch of play in one zone. The addon records, per loot source
("Creature:7105", "GameObject:181270", ...), what was looted and which spell (if any)
opened it, plus kills by NPC ID. Here we:

1. classify each source as an activity: herbalism / mining / skinning / fishing / kill,
   from the gathering spell that opened it, falling back to what it dropped;
2. split the segment's active time between activities by number of loot/kill events;
3. match each activity to the methods for that activity in that zone, by how much of the
   loot is the method's yield items;
4. propose session(s): confident single-activity segments are applied automatically,
   mixed or unclear ones wait for review, unmatched ones can become method drafts.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from .methods.models import Activity, Method

# Gathering spell IDs, all ranks (Classic Era). Forever may add new ones; the item-based
# fallback below still works then.
SPELL_ACTIVITY: dict[int, Activity] = {
    **dict.fromkeys((2366, 2368, 3570, 11993), Activity.HERBALISM),
    **dict.fromkeys((2575, 2576, 3564, 10248), Activity.MINING),
    **dict.fromkeys((8613, 8617, 8618, 10768), Activity.SKINNING),
    **dict.fromkeys((7620, 7731, 7732, 18248), Activity.FISHING),
}
FISHING_BOBBER = 35591
SUBCLASS_ACTIVITY = {
    ("Trade Goods", "Herb"): Activity.HERBALISM,
    ("Trade Goods", "Metal & Stone"): Activity.MINING,
    ("Trade Goods", "Leather"): Activity.SKINNING,
}
OTHER = "other"

MIN_ACTIVITY_SECONDS = 5 * 60  # ignore activities shorter than this
MIN_SHARE = 0.25  # activities below this share of a segment aren't assigned
AUTO_SHARE = 0.7  # a segment this dominated by one activity can be applied automatically
MIN_OVERLAP = 0.5  # share of looted (non-grey) items that must be the method's items
MIN_MARGIN = 0.15  # best match must beat the runner-up by this much overlap


@dataclass(frozen=True)
class ItemMeta:
    name: str | None
    item_class: str | None
    item_subclass: str | None
    quality: str | None
    sell_price: int | None


@dataclass
class Source:
    key: str
    kind: str  # Creature | GameObject | unknown
    obj_id: int | None
    loots: int = 0
    items: dict[int, int] = field(default_factory=dict)
    copper: int = 0
    via: int | None = None  # spell that opened the loot window


@dataclass
class Segment:
    external_id: str
    zone: str
    map_id: int | None
    started_at: str
    active_s: int
    cls: str | None
    level: int | None
    label: str | None
    sources: list[Source]
    kills: dict[int, int]
    copper: int


@dataclass
class ActivityTotals:
    activity: str  # Activity value or OTHER
    events: int = 0
    items: dict[int, int] = field(default_factory=dict)
    copper: int = 0
    npc_ids: set[int] = field(default_factory=set)
    share: float = 0.0
    seconds: int = 0

    def add(self, src: Source) -> None:
        self.events += src.loots
        self.copper += src.copper
        for item_id, qty in src.items.items():
            self.items[item_id] = self.items.get(item_id, 0) + qty
        if src.kind == "Creature" and src.obj_id is not None:
            self.npc_ids.add(src.obj_id)


@dataclass(frozen=True)
class Candidate:
    method_id: str
    overlap: float


@dataclass
class Assignment:
    method_id: str
    activity: str
    seconds: int
    items: dict[int, int]
    copper: int


@dataclass
class Proposal:
    status: str  # auto | labelled | review | unmatched | empty
    assignments: list[Assignment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    activities: list[ActivityTotals] = field(default_factory=list)
    candidates: dict[str, list[Candidate]] = field(default_factory=dict)

    def to_json(self) -> str:
        data = asdict(self)
        for a in data["activities"]:
            a["npc_ids"] = sorted(a["npc_ids"])
        return json.dumps(data)


# -- parsing -------------------------------------------------------------------------------


def _int_keys(d: Any) -> dict[int, int]:
    if isinstance(d, list):  # a Lua table with keys 1..n parses as a list
        return {i + 1: int(v) for i, v in enumerate(d)}
    if not isinstance(d, dict):
        return {}
    return {int(k): int(v) for k, v in d.items()}


def parse_segment(raw: dict[str, Any]) -> Segment:
    sources = []
    for key, s in (raw.get("sources") or {}).items():
        kind, _, obj = str(key).partition(":")
        sources.append(
            Source(
                key=str(key),
                kind=kind if obj else "unknown",
                obj_id=int(obj) if obj.isdigit() else None,
                loots=int(s.get("loots") or 0),
                items=_int_keys(s.get("items")),
                copper=int(s.get("copper") or 0),
                via=int(s["via"]) if s.get("via") is not None else None,
            )
        )
    start = int(raw.get("start") or 0)
    return Segment(
        external_id=str(raw["id"]),
        zone=str(raw.get("zone") or ""),
        map_id=int(raw["map"]) if raw.get("map") is not None else None,
        started_at=datetime.fromtimestamp(start, UTC).isoformat(timespec="seconds"),
        active_s=int(raw.get("active") or 0),
        cls=str(raw["class"]).lower() if raw.get("class") else None,
        level=int(raw["level"]) if raw.get("level") else None,
        label=str(raw["label"]) if raw.get("label") else None,
        sources=sorted(sources, key=lambda s: s.key),
        kills=_int_keys(raw.get("kills")),
        copper=int(raw.get("copper") or 0),
    )


# -- classification ------------------------------------------------------------------------


def classify_source(src: Source, meta: dict[int, ItemMeta]) -> str:
    if src.via in SPELL_ACTIVITY:
        return SPELL_ACTIVITY[src.via]
    if src.kind == "GameObject" and src.obj_id == FISHING_BOBBER:
        return Activity.FISHING
    if src.kind == "unknown":
        return OTHER
    # Fall back to what dropped: mostly herbs -> herbalism, ore -> mining, leather -> skinning.
    votes: dict[Activity, int] = {}
    for item_id, qty in src.items.items():
        m = meta.get(item_id)
        act = SUBCLASS_ACTIVITY.get((m.item_class, m.item_subclass)) if m else None
        if act:
            votes[act] = votes.get(act, 0) + qty
    if votes:
        best = max(votes, key=votes.get)
        # A creature dropping some leather-armor-like trade goods isn't skinning unless
        # leather dominates; gathering activities need a clear majority.
        if votes[best] * 2 >= sum(src.items.values()):
            return best
    return Activity.KILL if src.kind == "Creature" else OTHER


def split_activities(seg: Segment, meta: dict[int, ItemMeta]) -> list[ActivityTotals]:
    classified = [(src, classify_source(src, meta)) for src in seg.sources]
    # Corpses you skinned: their regular loot belongs to the skinning activity, since a
    # skinning route's income includes what the beasts drop.
    skinned = {s.obj_id for s, a in classified if a == Activity.SKINNING and s.kind == "Creature"}
    totals: dict[str, ActivityTotals] = {}
    for src, act in classified:
        if act == Activity.KILL and src.obj_id in skinned:
            act = Activity.SKINNING
        totals.setdefault(act, ActivityTotals(act)).add(src)

    # Kills without loot (or with loot that didn't open a window) still took time.
    kills = sum(n for npc, n in seg.kills.items() if npc not in skinned)
    if kills:
        kill = totals.setdefault(Activity.KILL, ActivityTotals(Activity.KILL))
        kill.events = max(kill.events, kills)
        kill.npc_ids.update(npc for npc in seg.kills if npc not in skinned)

    total_events = sum(t.events for t in totals.values())
    for t in totals.values():
        t.share = t.events / total_events if total_events else 0.0
        t.seconds = round(seg.active_s * t.share)
    return sorted(totals.values(), key=lambda t: -t.events)


def _zone_matches(method: Method, zone: str) -> bool:
    return any(z.lower() == zone.lower() for z in method.all_zones)


def overlap(items: dict[int, int], method: Method, meta: dict[int, ItemMeta]) -> float:
    """Fraction of looted non-grey items that are among the method's yields."""
    relevant = {i: q for i, q in items.items() if not (meta.get(i) and meta[i].quality == "POOR")}
    total = sum(relevant.values())
    if not total:
        return 0.0
    yields = {y.item for y in method.yields}
    return sum(q for i, q in relevant.items() if i in yields) / total


def candidates(
    totals: ActivityTotals, zone: str, methods: list[Method], meta: dict[int, ItemMeta]
) -> list[Candidate]:
    out = [
        Candidate(m.id, overlap(totals.items, m, meta))
        for m in methods
        if m.activity == totals.activity and _zone_matches(m, zone)
    ]
    return sorted(out, key=lambda c: -c.overlap)


def propose(seg: Segment, methods: list[Method], meta: dict[int, ItemMeta]) -> Proposal:
    by_id = {m.id: m for m in methods}
    acts = split_activities(seg, meta)
    proposal = Proposal(status="empty", activities=acts)

    if seg.label:
        if seg.label not in by_id:
            proposal.status = "review"
            proposal.notes.append(f"labelled {seg.label!r}, which is not a known method")
            return proposal
        items: dict[int, int] = {}
        for t in acts:
            if t.activity != OTHER:
                for i, q in t.items.items():
                    items[i] = items.get(i, 0) + q
        proposal.status = "labelled"
        proposal.assignments.append(
            Assignment(seg.label, "labelled", seg.active_s, items, seg.copper)
        )
        return proposal

    significant = [
        t
        for t in acts
        if t.activity != OTHER and t.share >= MIN_SHARE and t.seconds >= MIN_ACTIVITY_SECONDS
    ]
    if not significant:
        proposal.notes.append("too short or no gathering/killing activity")
        return proposal

    unmatched = []
    for t in significant:
        cands = candidates(t, seg.zone, methods, meta)
        proposal.candidates[t.activity] = cands
        best = cands[0] if cands else None
        runner_up = cands[1].overlap if len(cands) > 1 else 0.0
        if best and best.overlap >= MIN_OVERLAP and best.overlap - runner_up >= MIN_MARGIN:
            proposal.assignments.append(
                Assignment(best.method_id, t.activity, t.seconds, dict(t.items), t.copper)
            )
        else:
            unmatched.append(t)
            why = (
                f"no {t.activity} method for {seg.zone}"
                if not cands
                else f"{t.activity}: best match {best.method_id} covers only "
                f"{best.overlap:.0%} of the loot"
                if best.overlap < MIN_OVERLAP
                else f"{t.activity}: {best.method_id} and {cands[1].method_id} match equally"
            )
            proposal.notes.append(why)

    dominant = significant[0].share >= AUTO_SHARE and len(significant) == 1
    if proposal.assignments and dominant and not unmatched:
        proposal.status = "auto"
    elif proposal.assignments:
        proposal.status = "review"
        proposal.notes.append("mixed activities: time is split by loot/kill events")
    else:
        proposal.status = "unmatched"
    return proposal


# -- drafts ----------------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def draft_method(
    seg: Segment, totals: ActivityTotals, meta: dict[int, ItemMeta], skill: int | None = None
) -> str:
    """YAML for a new method based on one segment's activity."""
    hours = max(totals.seconds, 1) / 3600
    kind = {
        Activity.HERBALISM: "gather",
        Activity.MINING: "gather",
        Activity.SKINNING: "gather",
        Activity.FISHING: "fishing",
        Activity.KILL: "farm",
    }.get(totals.activity, "farm")
    reqs = [f"level: {seg.level or 1}"]
    if totals.activity in (
        Activity.HERBALISM,
        Activity.MINING,
        Activity.SKINNING,
        Activity.FISHING,
    ):
        reqs.append(f"professions: {{{totals.activity}: {skill or 1}}}")
    vendor = totals.copper
    lines = [
        f"- id: {_slug(totals.activity + ' ' + seg.zone)}",
        f"  name: {totals.activity.capitalize()} in {seg.zone}  # TODO: describe the route/mobs",
        f"  kind: {kind}",
        f'  zone: "{seg.zone}"',
        f"  requirements: {{{', '.join(reqs)}}}  # TODO: check the minimum level/skill",
        "  yields:",
    ]
    for item_id, qty in sorted(totals.items.items(), key=lambda kv: -kv[1]):
        m = meta.get(item_id)
        if m and m.quality == "POOR":
            vendor += qty * (m.sell_price or 0)
            continue
        rate = qty / hours
        sd = math.sqrt(qty) / hours
        name = (m.name if m and m.name else f"item {item_id}").replace('"', "'")
        lines.append(f'    - {{item: {item_id}, name: "{name}", qty: {rate:.1f}, sd: {sd:.1f}}}')
    lines.append(f"  vendor_copper_per_hour: {round(vendor / hours)}")
    lines.append(
        f"  confidence: estimate  # from one {totals.seconds / 60:.0f}-minute segment "
        f"({seg.started_at[:10]})"
    )
    return "\n".join(lines)


# -- storage ---------------------------------------------------------------------------------


def load_item_meta(conn: sqlite3.Connection) -> dict[int, ItemMeta]:
    return {
        r[0]: ItemMeta(*r[1:])
        for r in conn.execute(
            "SELECT item_id, name, item_class, item_subclass, quality, sell_price FROM items"
        )
    }


def store_segment(conn: sqlite3.Connection, raw: dict[str, Any], seg: Segment) -> int | None:
    """Insert a segment; returns its row id, or None if already imported."""
    if conn.execute(
        "SELECT 1 FROM addon_segments WHERE external_id = ?", (seg.external_id,)
    ).fetchone():
        return None
    cur = conn.execute(
        """
        INSERT INTO addon_segments (external_id, zone, map_id, started_at, active_s, cls, level,
                                    label, payload, status, imported_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'new', ?)
        """,
        (
            seg.external_id,
            seg.zone,
            seg.map_id,
            seg.started_at,
            seg.active_s,
            seg.cls,
            seg.level,
            seg.label,
            json.dumps(raw, default=str),
            datetime.now(UTC).isoformat(timespec="seconds"),
        ),
    )
    return cur.lastrowid


def load_segment(conn: sqlite3.Connection, segment_id: int) -> tuple[sqlite3.Row, Segment]:
    row = conn.execute("SELECT * FROM addon_segments WHERE id = ?", (segment_id,)).fetchone()
    if row is None:
        raise ValueError(f"no segment #{segment_id}")
    return row, parse_segment(json.loads(row["payload"]))
