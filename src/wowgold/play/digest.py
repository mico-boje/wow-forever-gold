"""What a recorded session measured.

From the raw event log:

- **time:** session length, paused time, active time (gaps of more than `AFK_GAP` without any
  event count as away), time in combat, and "busy" time between kills;
- **kills** per mob, and mobs that **died near you** without you killing them (other players
  farming the same spot);
- **kill speed:** for each pair of kills less than `BUSY_GAP` apart, the model's time per kill
  (discover.kills_per_hour) over the real gap; the session's figure is the median, so waiting
  for respawns or travelling between kills doesn't drag it down much;
- **respawns:** the same mob killed again at the same spot (within `RESPAWN_RADIUS` % of the
  map) — the lower quartile of those gaps estimates the respawn time;
- **loot:** what you received, attributed to the loot window (mob, node or chest) it came from,
  next to what the loot model expects for your kills.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median

from .recordings import Event, Recording

AFK_GAP = 300  # seconds without any event: away
BUSY_GAP = 180  # kills further apart than this: travelling, waiting or away
RESPAWN_RADIUS = 1.5  # % of the zone map (~40-60 yards)
RESPAWN_MIN_S = 60  # faster re-kills at a spot are two different spawns
RESPAWN_MIN_PAIRS = 3
LOOT_WINDOW_S = 10  # items received this long after a loot window opened came from it


@dataclass
class Kill:
    t: int
    npc: int
    uid: str
    map: int | None
    x: float | None
    y: float | None


@dataclass
class SourceStats:
    kind: str  # Creature | GameObject | unknown
    id: int | None
    name: str
    kills: int = 0
    others: int = 0  # died near you, killed by someone else
    loots: int = 0
    items: dict[int, int] = field(default_factory=dict)
    copper: int = 0
    level: tuple[int, int] | None = None
    model_rate: float | None = None  # model kills/h for this character (uncalibrated)
    expected_per_kill: float | None = None  # copper, from the loot model
    value: float = 0.0  # copper: items received from it (priced) + coins
    respawn_s: float | None = None
    respawn_pairs: int = 0

    @property
    def competition(self) -> float | None:
        seen = self.kills + self.others
        return self.others / seen if seen else None


@dataclass
class Digest:
    rec: Recording
    duration_s: int
    paused_s: int
    active_s: int
    combat_s: int
    busy_s: int
    zones: list[tuple[str, int]]  # (zone, active seconds), most first
    sources: list[SourceStats]
    items: dict[int, int]  # everything received
    copper_looted: int
    money_change: int | None  # money at the end minus at the start (vendor sales included)
    xp: int
    levels: list[int]
    deaths: int
    kill_gaps: list[tuple[int, float]]  # (npc, seconds) between consecutive kills ≤ BUSY_GAP
    kill_log: list[Kill] = field(default_factory=list)
    kill_speed: float | None = None  # model time per kill / real time per kill (median)
    value: float = 0.0  # copper: everything received, priced, + coins
    expected_value: float | None = None  # what the loot model expects for your kills

    @property
    def kills(self) -> int:
        return sum(s.kills for s in self.sources)

    @property
    def mobs(self) -> list[SourceStats]:
        return [s for s in self.sources if s.kind == "Creature"]

    @property
    def gold_per_hour(self) -> float | None:
        return self.value / (self.active_s / 3600) if self.active_s >= 60 else None

    @property
    def kills_per_hour(self) -> float | None:
        return self.kills / (self.active_s / 3600) if self.active_s >= 60 else None


def _timeline(events: list[Event], end: int) -> tuple[int, int, int]:
    """(paused, active, combat) seconds."""
    paused = active = combat = 0
    paused_at = combat_at = None
    last = 0
    for e in events:
        if paused_at is None:
            active += min(e.t - last, AFK_GAP)
        last = e.t
        if e.code == "U":
            what = e.str(0)
            if what == "pause" and paused_at is None:
                paused_at = e.t
                if combat_at is not None:
                    combat += e.t - combat_at
                    combat_at = None
            elif what == "resume" and paused_at is not None:
                paused += e.t - paused_at
                paused_at = None
        elif e.code == "C":
            if e.str(0) == "1" and combat_at is None:
                combat_at = e.t
            elif e.str(0) == "0" and combat_at is not None:
                combat += e.t - combat_at
                combat_at = None
    if paused_at is not None:
        paused += end - paused_at
    elif end > last:
        active += min(end - last, AFK_GAP)
    if combat_at is not None:
        combat += max(0, last - combat_at)
    return paused, active, combat


def respawn_gaps(kills: list[Kill]) -> dict[int, list[int]]:
    """npc -> gaps between killing it at a spot and killing it there again."""
    out: dict[int, list[int]] = defaultdict(list)
    by_npc: dict[int, list[Kill]] = defaultdict(list)
    for k in kills:
        if k.x is not None and k.y is not None:
            by_npc[k.npc].append(k)
    for npc, ks in by_npc.items():
        for i, a in enumerate(ks):
            for b in ks[i + 1 :]:
                if b.map != a.map:
                    continue
                if math.hypot(b.x - a.x, b.y - a.y) <= RESPAWN_RADIUS:
                    if b.t - a.t >= RESPAWN_MIN_S:
                        out[npc].append(b.t - a.t)
                    break  # the next kill at this spot is the one that matters
    return out


def lower_quartile(xs: list[float]) -> float:
    s = sorted(xs)
    return s[(len(s) - 1) // 4]


def digest(
    rec: Recording,
    *,
    data=None,  # discovery.discover.VanillaData, for mob names/levels and the loot model
    book=None,  # planner.pricing.PriceBook, to value what you looted
    names: dict[int, str] | None = None,  # NPC names for mobs not in the vanilla data
) -> Digest:
    events = sorted(rec.events, key=lambda e: e.t)
    end = events[-1].t if events else 0
    if rec.stopped:
        end = max(end, int((rec.stopped - rec.started).total_seconds()))
    paused, active, combat = _timeline(events, end)

    zone_time: dict[str, int] = defaultdict(int)
    zone, last_t, cur_map = "", 0, None
    pos: tuple[float, float] | None = None
    kills: list[Kill] = []
    died: dict[str, int] = {}
    sources: dict[tuple[str, int | None], SourceStats] = {}
    items: dict[int, int] = defaultdict(int)
    copper = xp = deaths = 0
    levels: list[int] = []
    money: list[int] = []
    loot_src: tuple[str, int | None] | None = None
    loot_at = -(10**9)
    slot_src: dict[int, tuple[str, int | None]] = {}

    def src(kind: str, sid: int | None) -> SourceStats:
        key = (kind, sid)
        if key not in sources:
            sources[key] = SourceStats(kind, sid, "")
        return sources[key]

    paused_now = False
    for e in events:
        if zone and not paused_now:
            zone_time[zone] += min(e.t - last_t, AFK_GAP)
        last_t = e.t
        c = e.code
        if c == "U":
            paused_now = e.str(0) == "pause"
        elif c == "Z":
            zone = e.str(0) or zone
            cur_map = e.int(2, cur_map)
        elif c == "P":
            cur_map = e.int(0, cur_map)
            x, y = e.float(1), e.float(2)
            if x is not None and y is not None:
                pos = (x, y)
        elif c == "K":
            npc = e.int(0)
            if npc is None:
                continue
            x, y = e.float(2), e.float(3)
            if x is None and pos:
                x, y = pos
            kills.append(Kill(e.t, npc, e.str(1), cur_map, x, y))
            src("Creature", npc).kills += 1
        elif c == "D":
            npc = e.int(0)
            if npc is not None:
                died[e.str(1)] = npc
        elif c == "L":
            kind = e.str(0) or "unknown"
            loot_src = (kind, e.int(1))
            loot_at = e.t
            slot_src = {}
            src(*loot_src).loots += 1
        elif c == "S":
            item, skind = e.int(1), e.str(3)
            if item is not None and skind:
                slot_src[item] = (skind, e.int(4))
        elif c == "I":
            item, n = e.int(0), e.int(1, 1)
            if item is None:
                continue
            items[item] += n
            if loot_src is not None and e.t - loot_at <= LOOT_WINDOW_S:
                key = slot_src.get(item, loot_src)
            else:
                key = ("unknown", None)
            s = src(*key)
            s.items[item] = s.items.get(item, 0) + n
        elif c == "M":
            n = e.int(0, 0) or 0
            copper += n
            key = loot_src if loot_src and e.t - loot_at <= LOOT_WINDOW_S else ("unknown", None)
            src(*key).copper += n
        elif c == "$":
            if (m := e.int(0)) is not None:
                money.append(m)
        elif c == "X":
            xp += e.int(0, 0) or 0
        elif c == "V":
            if (lv := e.int(0)) is not None:
                levels.append(lv)
        elif c == "R" and e.str(0) == "dead":
            deaths += 1

    mine = {k.uid for k in kills}
    for uid, npc in died.items():
        if uid not in mine:
            src("Creature", npc).others += 1

    for npc, gaps in respawn_gaps(kills).items():
        s = src("Creature", npc)
        s.respawn_pairs = len(gaps)
        if len(gaps) >= RESPAWN_MIN_PAIRS:
            s.respawn_s = lower_quartile(gaps)

    gaps = [
        (b.npc, b.t - a.t)
        for a, b in zip(kills, kills[1:], strict=False)
        if 0 < b.t - a.t <= BUSY_GAP
    ]
    out = Digest(
        rec=rec,
        duration_s=end,
        paused_s=paused,
        active_s=active,
        combat_s=combat,
        busy_s=int(sum(g for _, g in gaps)),
        zones=sorted(zone_time.items(), key=lambda z: -z[1]),
        sources=[],
        items=dict(items),
        copper_looted=copper,
        money_change=money[-1] - money[0] if len(money) >= 2 else None,
        xp=xp,
        levels=levels,
        deaths=deaths,
        kill_gaps=gaps,
        kill_log=kills,
    )
    _apply_models(out, sources, data, book, names or {})
    out.sources = sorted(sources.values(), key=lambda s: (-(s.value or 0), -s.kills, -s.loots))
    return out


def _apply_models(out: Digest, sources, data, book, names: dict[int, str]) -> None:
    """Names, levels, model kill rates, expected loot and values (whatever data is given)."""
    from ..discovery.discover import (
        creature_loot,
        kills_per_hour,
        skinning_loot,
        skinning_skill_for,
        unit_value,
    )

    rec = out.rec
    level, cls = rec.level or 1, rec.cls
    skinning = rec.professions.get("skinning", 0)
    for s in sources.values():
        c = data.creatures.get(s.id) if data is not None and s.kind == "Creature" else None
        if c is not None:
            s.name = c.name
            s.level = (c.level_min, c.level_max)
            s.model_rate = kills_per_hour(c, level, cls)
            if book is not None and s.kills:
                ev = sum(q * unit_value(i, data, book) for i, q in creature_loot(c, data).items())
                ev += (c.gold_min + c.gold_max) / 2
                if c.skinning_loot_id and skinning >= skinning_skill_for(c.level_max):
                    ev += sum(
                        q * unit_value(i, data, book) for i, q in skinning_loot(c, data).items()
                    )
                s.expected_per_kill = ev
        elif s.kind == "GameObject" and data is not None and s.id in data.node_types:
            s.name = data.node_types[s.id].name
        if not s.name:
            s.name = names.get(s.id or 0) or (
                f"{'NPC' if s.kind == 'Creature' else 'object'} {s.id}" if s.id else "unknown"
            )
        if book is not None:
            s.value = s.copper + sum(
                n * (unit_value(i, data, book) if data is not None else book.get(i).sell_value)
                for i, n in s.items.items()
            )
    if book is not None:
        out.value = sum(s.value for s in sources.values())
        exp = [s.expected_per_kill * s.kills for s in sources.values() if s.expected_per_kill]
        out.expected_value = sum(exp) if exp else None

    ratios = []
    for npc, gap in out.kill_gaps:
        s = sources.get(("Creature", npc))
        if s is not None and s.model_rate:
            ratios.append((3600 / s.model_rate) / gap)
    if len(ratios) >= 5:
        out.kill_speed = median(ratios)
