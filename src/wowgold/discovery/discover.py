"""Turn vanilla loot and spawn data into farm hypotheses, priced at current Forever AH prices.

Two generators:

- **Mob camps.** Every hostile mob gets an expected value per kill (loot tables × price, plus
  coins). Spawns are binned into a grid; for each 3×3-cell window (~450 yd square) a solo
  player kills the most valuable mobs first, limited by how fast they kill and by how fast
  the mobs respawn. The best non-overlapping windows become candidates.
- **Gathering zones.** Per zone and profession, active nodes (after pool limits) give a node
  density; time per node = travel to the nearest active node + gathering, capped by respawns.

Each candidate is a regular `Method` (confidence: hypothesis) and is scored by the planner's
own `evaluate`, so AH cut, demand caps and class/mount modifiers apply exactly as for curated
methods. All rates are rough guesses; the point is to rank what to try, then measure it.
"""

from __future__ import annotations

import math
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field, replace

from ..methods.models import Confidence, Faction, GearTier, ItemQty, Kind, Method, Requirements
from ..planner.engine import Evaluation, PlanOptions, _sell, eligibility, evaluate
from ..planner.pricing import PriceBook
from ..planner.profile import CharacterProfile
from ..wowhead.sources import ItemSource
from . import vanilla as v
from .zones import ZoneIndex, build_zone_index

# -- model constants (guesses; tune once sessions measure real rates) ---------------------------

CELL = 150.0  # yards; camps are 3×3 cells
KILLS_PER_HOUR_EVEN = 40.0  # solo kills/h incl. looting and resting, mob at your level
KILLS_PER_LEVEL_ABOVE = 0.12  # +12%/level you out-level the mob
KILLS_PER_LEVEL_BELOW = 0.15  # -15%/level the mob out-levels you
MAX_KILL_SPEEDUP = 2.5
ELITE_FACTOR = 0.25
# Solo kill speed by class, including downtime (eating/drinking, pet management), relative
# to an average class. Guesses from Classic experience; tune with measured sessions.
CLASS_KILL_SPEED = {
    "hunter": 1.25,
    "warlock": 1.2,
    "mage": 1.05,
    "rogue": 1.0,
    "druid": 0.95,
    "shaman": 0.95,
    "warrior": 0.85,
    "paladin": 0.85,
    "priest": 0.8,
}
# Mages can pull and AoE packs of mobs well below their level.
MAGE_AOE_MIN_GAP = 5
MAGE_AOE_FACTOR = 2.0
# Leveling sweep: assumed gathering/profession skill per character level (capped by --prof).
SKILL_PER_LEVEL = 5
# A captured Wowhead NPC/object page replaces the vanilla loot table once it's based on at
# least this many kills/loots; below that it only adds/overrides the items it lists.
PAGE_REPLACE_MIN_SAMPLES = 50
# When a captured page replaces a loot table, vanilla items it doesn't list are kept only if
# Wowhead's sample was too small to have seen them: fewer than this many drops expected at the
# vanilla rate. Items it should have seen but didn't are taken as removed in Forever.
UNSEEN_EXPECTED_DROPS = 3.0
MAX_LEVELS_ABOVE = 2  # skip mobs more than this many levels above you
SPEED_YD_S = 7.0 * 1.6  # ground mount; the planner scales gathering for no mount / epic mount
ROUTE_FACTOR = 1.5  # real paths vs. straight lines, and nodes you can't reach first
GATHER_S = 6.0  # dismount, cast, loot
NODE_CELL = 250.0
YIELD_REL_SD = 0.5  # hypothesis yields: ±50% (1 sd)
MIN_YIELD_COPPER_H = 2000  # items worth less than 20s/h go into the vendor bucket


@dataclass
class VanillaData:
    items: dict[int, v.VItem]
    creatures: dict[int, v.Creature]
    creature_spawns: list[v.Spawn]
    node_types: dict[int, v.NodeType]
    node_spawns: list[v.Spawn]
    loot: v.LootTables
    zones: ZoneIndex
    # Forever loot captured from Wowhead, overriding/adding to vanilla loot:
    # ("creature" | "skinning" | "node", entry) -> item -> expected count per kill/skin/roll
    extra: dict[tuple[str, int], dict[int, float]] = field(default_factory=dict)
    extra_notes: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    area_names: dict[int, str] = field(default_factory=dict)  # area/zone id -> name
    # Loot tables replaced wholesale by a captured Wowhead NPC/object page -> its sample size
    replaced: dict[tuple[str, int], int] = field(default_factory=dict)
    # Respawn times measured in your recorded sessions (seconds), by creature entry
    respawn: dict[int, float] = field(default_factory=dict)

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> VanillaData:
        node_types = v.load_node_types(conn)
        return cls(
            items=v.load_items(conn),
            creatures=v.load_creatures(conn),
            creature_spawns=v.load_creature_spawns(conn),
            node_types=node_types,
            node_spawns=v.load_node_spawns(conn, node_types),
            loot=v.LootTables(conn),
            zones=build_zone_index(conn),
            area_names={r[0]: r[1] for r in conn.execute("SELECT entry, name FROM area_template")},
        )

    def add_sources(self, sources: list[ItemSource]) -> tuple[list[ItemSource], list[ItemSource]]:
        """Merge captured Wowhead sources into the loot model.

        Returns (placed, unplaced): unplaced sources name an NPC/object with no spawn points
        in the open world of the vanilla data (new in Forever, or only inside instances), so
        there's no camp to build. `find_leads` turns those into leads.
        """
        spawned_mobs = {s.entry for s in self.creature_spawns}
        spawned_nodes = {s.entry for s in self.node_spawns}
        node_by_name: dict[str, list[int]] = defaultdict(list)
        for nt in self.node_types.values():
            if nt.entry in spawned_nodes:
                node_by_name[nt.name].append(nt.entry)
        placed, unplaced = [], []
        page_samples: dict[tuple[str, int], int] = {}
        for src in sources:
            if src.chance is None or src.chance <= 0:
                continue
            keys: list[tuple[str, int]] = []
            mob = src.source_id in self.creatures and src.source_id in spawned_mobs
            if src.kind == "drop" and mob:
                keys = [("creature", src.source_id)]
            elif src.kind == "skinning" and mob:
                keys = [("skinning", src.source_id)]
            elif src.kind in ("mining", "herbalism", "object"):
                if src.source_id in self.node_types:
                    keys = [("node", e) for e in node_by_name[self.node_types[src.source_id].name]]
                else:
                    keys = [("node", e) for e in node_by_name.get(src.source_name, [])]
            if not keys:
                unplaced.append(src)
                continue
            placed.append(src)
            if src.from_page:
                for key in keys:
                    page_samples[key] = max(page_samples.get(key, 0), src.samples or 0)
                note = None  # summarised once per page below
            else:
                note = f"Wowhead: {src.chance:.1%} item {src.item_id}" + (
                    f" (n={src.samples})" if src.samples else ""
                )
            for key in keys:
                self.extra.setdefault(key, {})[src.item_id] = src.chance
                if note:
                    self.extra_notes.setdefault(key, []).append(note)
        # A captured NPC/object page lists everything it drops in Forever: with enough
        # samples, it replaces the vanilla loot table instead of patching it.
        for key, n in page_samples.items():
            if n >= PAGE_REPLACE_MIN_SAMPLES:
                self.replaced[key] = n
                self.extra_notes.setdefault(key, []).insert(
                    0, f"Forever loot table from Wowhead (n={n}), plus rare drops too rare to show"
                )
        return placed, unplaced


@dataclass
class Candidate:
    method: Method
    evidence: list[str] = field(default_factory=list)
    evaluation: Evaluation | None = None
    eligible: bool = True
    group: str = ""  # candidates with the same group are alternatives (e.g. skill tiers)
    # Wowhead pages that verify this farm: ("npc" | "object", id, name), most valuable first
    pages: list[tuple[str, int, str]] = field(default_factory=list)
    verified: int = 0  # how many of `pages` have a Forever loot table captured
    # "craft before selling": extra copper/h from crafting the yields, and how
    craft_extra: float = 0.0
    craft_notes: list[str] = field(default_factory=list)

    @property
    def total_per_hour(self) -> float:
        return (self.evaluation.gold_per_hour if self.evaluation else 0.0) + self.craft_extra

    @property
    def verified_label(self) -> str:
        if not self.pages:
            return ""
        return f"{self.verified}/{len(self.pages)}"


# -- pricing helpers -----------------------------------------------------------------------------


def vendor_only(vi: v.VItem | None) -> bool:
    """Items to value at vendor price only: soulbound loot, and white/grey weapons and armor
    (nobody buys those; their AH listings are mostly troll prices)."""
    if vi is None:
        return False
    return vi.bind == 1 or (vi.quality <= 1 and vi.iclass in (2, 4))


def unit_value(item_id: int, data: VanillaData, book: PriceBook) -> float:
    """Value of one more unit, ignoring demand caps (used for pre-ranking only)."""
    vi = data.items.get(item_id)
    vendor = max(book.get(item_id).vendor_sell, vi.sell_price if vi else 0)
    if vendor_only(vi):
        return vendor
    ah = book.get(item_id).ah_net or 0
    return max(ah, vendor)


def _loot(data: VanillaData, key: tuple[str, int], base: dict[int, float]) -> dict[int, float]:
    extra = data.extra.get(key)
    if key in data.replaced:
        samples = data.replaced[key]
        rare = {
            item: q
            for item, q in base.items()
            if q * samples < UNSEEN_EXPECTED_DROPS and item not in (extra or {})
        }
        return {**rare, **(extra or {})}
    return {**base, **extra} if extra else base


def creature_loot(c: v.Creature, data: VanillaData) -> dict[int, float]:
    base = data.loot.expected("creature_loot_template", c.loot_id)
    return _loot(data, ("creature", c.entry), base)


def skinning_loot(c: v.Creature, data: VanillaData) -> dict[int, float]:
    base = data.loot.expected("skinning_loot_template", c.skinning_loot_id)
    return _loot(data, ("skinning", c.entry), base)


def node_loot(nt: v.NodeType, data: VanillaData) -> dict[int, float]:
    base = data.loot.expected("gameobject_loot_template", nt.loot_id)
    return _loot(data, ("node", nt.entry), base)


def skinning_skill_for(level: int) -> int:
    if level <= 10:
        return 1
    if level <= 20:
        return (level - 10) * 10
    return level * 5


def candidate_item_ids(data: VanillaData) -> set[int]:
    """Every item a candidate could produce, for building the price book up front."""
    ids: set[int] = set()
    for c in data.creatures.values():
        ids |= creature_loot(c, data).keys() | skinning_loot(c, data).keys()
        ids |= pickpocket_loot(c, data).keys()
    for nt in data.node_types.values():
        ids |= node_loot(nt, data).keys()
    return ids


# -- building methods ----------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:48]


def _method_from_rates(
    *,
    mid: str,
    name: str,
    kind: Kind,
    zone: str,
    reqs: Requirements,
    items_per_hour: dict[int, float],
    coins_per_hour: float,
    data: VanillaData,
    book: PriceBook,
    description: str,
    risk: list[str],
) -> Method:
    yields, vendor = [], coins_per_hour
    for item_id, qty in sorted(items_per_hour.items(), key=lambda kv: -kv[1]):
        vi = data.items.get(item_id)
        price = book.get(item_id)
        tradeable = not vendor_only(vi)
        if (
            tradeable
            and price.market is not None
            and qty * unit_value(item_id, data, book) >= MIN_YIELD_COPPER_H
        ):
            name_ = price.name or (vi.name if vi else f"item {item_id}")
            yields.append(
                ItemQty(
                    item=item_id,
                    name=name_,
                    qty=float(f"{qty:.3g}"),
                    sd=float(f"{qty * YIELD_REL_SD:.3g}"),
                )
            )
        else:
            vendor += qty * max(price.vendor_sell, vi.sell_price if vi else 0)
    return Method(
        id=mid,
        name=name,
        kind=kind,
        zone=zone,
        description=description,
        requirements=reqs,
        yields=yields,
        vendor_copper_per_hour=max(round(vendor), 0 if yields else 1),
        confidence=Confidence.HYPOTHESIS,
        risk=risk,
        notes="Generated by `wowgold discover` from vanilla (VMaNGOS) data; untested in Forever.",
    )


# -- mob camps -----------------------------------------------------------------------------------


def kills_per_hour(
    c: v.Creature, player_level: int, cls: str | None = None, speed: float = 1.0
) -> float:
    """Solo kills per hour of mob `c`. `speed` scales the model by your measured kill speed
    (play.calibration)."""
    gap = player_level - c.level_max
    if gap >= 0:
        rate = KILLS_PER_HOUR_EVEN * min(1 + KILLS_PER_LEVEL_ABOVE * gap, MAX_KILL_SPEEDUP)
    else:
        rate = KILLS_PER_HOUR_EVEN * max(1 + KILLS_PER_LEVEL_BELOW * gap, 0.2)
    rate *= CLASS_KILL_SPEED.get(cls or "", 1.0) * speed
    if c.rank == v.RANK_ELITE:
        return rate * ELITE_FACTOR
    if cls == "mage" and gap >= MAGE_AOE_MIN_GAP:
        rate *= MAGE_AOE_FACTOR
    return rate


def pickpocket_loot(c: v.Creature, data: VanillaData) -> dict[int, float]:
    return data.loot.expected("pickpocketing_loot_template", c.pickpocket_loot_id)


def can_pickpocket(c: v.Creature, profile: CharacterProfile) -> bool:
    return profile.cls == "rogue" and bool(c.pickpocket_loot_id)


def _farmable(c: v.Creature, profile: CharacterProfile, elites: bool) -> bool:
    if profile.faction not in c.hostile_to or c.ctype == v.CREATURE_TYPE_CRITTER:
        return False
    if c.rank not in (v.RANK_NORMAL, v.RANK_ELITE) or (c.rank == v.RANK_ELITE and not elites):
        return False
    return c.level_max <= profile.level + MAX_LEVELS_ABOVE


def mob_camps(
    data: VanillaData,
    profile: CharacterProfile,
    book: PriceBook,
    *,
    elites: bool = False,
    limit: int = 60,
    min_spacing_cells: int = 4,
) -> list[Candidate]:
    skinning = profile.professions.get("skinning", 0)
    ev: dict[int, float] = {}  # copper per kill
    rate: dict[int, float] = {}
    for c in data.creatures.values():
        if not _farmable(c, profile, elites):
            continue
        value = sum(q * unit_value(i, data, book) for i, q in creature_loot(c, data).items())
        value += (c.gold_min + c.gold_max) / 2
        if c.skinning_loot_id and skinning >= skinning_skill_for(c.level_max):
            value += sum(q * unit_value(i, data, book) for i, q in skinning_loot(c, data).items())
        if can_pickpocket(c, profile):
            value += sum(q * unit_value(i, data, book) for i, q in pickpocket_loot(c, data).items())
        if value > 0:
            ev[c.entry] = value
            rate[c.entry] = kills_per_hour(c, profile.level, profile.cls, profile.kill_speed)

    # supply (respawns per hour) per cell per creature
    cells: dict[tuple[int, int, int], dict[int, float]] = defaultdict(lambda: defaultdict(float))
    for s in data.creature_spawns:
        if s.entry in ev:
            respawn = data.respawn.get(s.entry, s.respawn_s)
            cells[(s.map, int(s.x // CELL), int(s.y // CELL))][s.entry] += (
                3600 / respawn * s.p_active
            )

    def allocate(supply: dict[int, float]) -> tuple[float, dict[int, float]]:
        """Spend one hour killing the most valuable mobs first."""
        time_left, kills, value = 1.0, {}, 0.0
        for entry in sorted(supply, key=lambda e: -ev[e] * rate[e]):
            n = min(supply[entry], time_left * rate[entry])
            if n <= 0:
                break
            kills[entry] = n
            value += n * ev[entry]
            time_left -= n / rate[entry]
        return value, kills

    windows = []
    for map_id, cx, cy in cells:
        supply: dict[int, float] = defaultdict(float)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for entry, n in cells.get((map_id, cx + dx, cy + dy), {}).items():
                    supply[entry] += n
        value, kills = allocate(supply)
        windows.append((value, map_id, cx, cy, kills))
    windows.sort(key=lambda w: -w[0])

    chosen: list[tuple[float, int, int, int, dict[int, float]]] = []
    for w in windows:
        if len(chosen) >= limit:
            break
        if any(
            c[1] == w[1] and max(abs(c[2] - w[2]), abs(c[3] - w[3])) < min_spacing_cells
            for c in chosen
        ):
            continue
        chosen.append(w)

    out = []
    for _, map_id, cx, cy, kills in chosen:
        x, y = (cx + 0.5) * CELL, (cy + 0.5) * CELL
        zone = data.zones.zone_at(map_id, x, y) or ("Eastern Kingdoms", "Kalimdor")[map_id]
        mobs = sorted(kills, key=lambda e: -kills[e] * ev[e])
        out.extend(_camp_methods(data, profile, book, zone, map_id, x, y, mobs, kills))
    return out


def _camp_methods(data, profile, book, zone, map_id, x, y, mobs, kills) -> list[Candidate]:
    creatures = [data.creatures[e] for e in mobs]
    top = creatures[0]
    names = ", ".join(dict.fromkeys(c.name for c in creatures[:3]))
    level = max(1, min(60, max(c.level_max for c in creatures) - MAX_LEVELS_ABOVE))
    elite = any(c.rank == v.RANK_ELITE for c in creatures)
    hostile_to_all = all({"alliance", "horde"} <= c.hostile_to for c in creatures)
    faction = Faction.ANY if hostile_to_all else Faction(profile.faction)
    gear = GearTier.PRERAID if elite else GearTier.LEVELING

    per_hour: dict[int, float] = defaultdict(float)
    skin_per_hour: dict[int, float] = defaultdict(float)
    coins = 0.0
    for e, n in kills.items():
        c = data.creatures[e]
        coins += n * (c.gold_min + c.gold_max) / 2
        for item, q in creature_loot(c, data).items():
            per_hour[item] += n * q
        for item, q in skinning_loot(c, data).items():
            skin_per_hour[item] += n * q
        if can_pickpocket(c, profile):
            for item, q in pickpocket_loot(c, data).items():
                per_hour[item] += n * q

    respawns = sorted(
        {
            int(data.respawn.get(s.entry, s.respawn_s))
            for s in data.creature_spawns
            if s.entry in kills
        }
    )
    measured = [e for e in mobs if e in data.respawn]
    evidence = [
        f"{data.creatures[e].name} (lvl {data.creatures[e].level_min}-"
        f"{data.creatures[e].level_max}{', elite' if data.creatures[e].rank else ''}): "
        f"{kills[e]:.0f} kills/h"
        for e in mobs[:6]
    ]
    for e in mobs:
        for kind in ("creature", "skinning"):
            evidence += [
                f"{data.creatures[e].name}: {n}" for n in data.extra_notes.get((kind, e), [])
            ]
    where = f"{zone}, around world ({x:.0f}, {y:.0f}) on {('Eastern Kingdoms', 'Kalimdor')[map_id]}"
    desc = (
        f"Kill {names}. {where}. Respawn {respawns[0] // 60}-{respawns[-1] // 60} min"
        + (" (measured in your sessions)" if measured else "")
        + ". "
        f"Kill rates assume a level {profile.level} {profile.cls}"
        + (" (pickpocketing first)" if any(can_pickpocket(c, profile) for c in creatures) else "")
        + "."
    )
    risk = ["competition"] + (["elite_mobs"] if elite else [])
    base_id = f"hyp_{_slug(zone)}_{_slug(top.name)}"
    group = base_id  # several camps of the same mob in a zone: keep the best
    pages = [("npc", e, data.creatures[e].name) for e in mobs[:6]]
    verified = sum(("creature", e) in data.replaced for e in mobs[:6])
    cands = [
        Candidate(
            _method_from_rates(
                mid=base_id,
                name=f"{top.name} camp",
                kind=Kind.FARM,
                zone=zone,
                reqs=Requirements(level=level, faction=faction, gear=gear),
                items_per_hour=per_hour,
                coins_per_hour=coins,
                data=data,
                book=book,
                description=desc,
                risk=risk,
            ),
            evidence,
            group=group,
            pages=pages,
            verified=verified,
        )
    ]
    if skin_per_hour:
        skill = skinning_skill_for(max(c.level_max for c in creatures if c.skinning_loot_id))
        merged = defaultdict(float, per_hour)
        for item, q in skin_per_hour.items():
            merged[item] += q
        cands.append(
            Candidate(
                _method_from_rates(
                    mid=base_id + "_skin",
                    name=f"{top.name} camp + skinning",
                    kind=Kind.FARM,
                    zone=zone,
                    reqs=Requirements(
                        level=level, faction=faction, gear=gear, professions={"skinning": skill}
                    ),
                    items_per_hour=merged,
                    coins_per_hour=coins,
                    data=data,
                    book=book,
                    description=desc,
                    risk=risk,
                ),
                evidence,
                group=group,
                pages=pages,
                verified=verified,
            )
        )
    return cands


# -- gathering -----------------------------------------------------------------------------------


def _zone_levels(data: VanillaData) -> dict[str, int]:
    """25th-percentile level of hostile mobs per zone: roughly where it's safe to gather."""
    levels: dict[str, list[int]] = defaultdict(list)
    zone_cache: dict[tuple[int, int, int], str | None] = {}
    for s in data.creature_spawns:
        c = data.creatures.get(s.entry)
        if (
            c is None
            or c.ctype == v.CREATURE_TYPE_CRITTER
            or c.rank != v.RANK_NORMAL
            or not c.loot_id
        ):
            continue
        key = (s.map, int(s.x // 250), int(s.y // 250))
        if key not in zone_cache:
            zone_cache[key] = data.zones.zone_at(s.map, s.x, s.y)
        if zone := zone_cache[key]:
            levels[zone].append(c.level_max)
    return {z: sorted(lv)[len(lv) // 4] for z, lv in levels.items() if lv}


def _share_nodes(
    capacity: float, active: dict[int, float], supply: dict[int, float]
) -> dict[int, float]:
    """Split the nodes you can reach per hour between node types in proportion to how many
    are up, capped by each type's respawn rate. Time freed by a capped type is *not* given to
    the others: that would assume camping a few respawn spots, which is a separate farm."""
    total = sum(active.values())
    return {e: min(capacity * a / total, supply[e]) if total else 0.0 for e, a in active.items()}


def gathering_zones(
    data: VanillaData, profile: CharacterProfile, book: PriceBook
) -> list[Candidate]:
    zone_level = _zone_levels(data)
    # spawn -> zone, cached per cell
    by_zone: dict[tuple[str, str], list[v.Spawn]] = defaultdict(list)
    cache: dict[tuple[int, int, int], str | None] = {}
    for s in data.node_spawns:
        key = (s.map, int(s.x // NODE_CELL), int(s.y // NODE_CELL))
        if key not in cache:
            cache[key] = data.zones.zone_at(s.map, s.x, s.y)
        if zone := cache[key]:
            by_zone[(zone, data.node_types[s.entry].profession)].append(s)

    out = []
    for (zone, prof), spawns in by_zone.items():
        area = len({(s.map, int(s.x // NODE_CELL), int(s.y // NODE_CELL)) for s in spawns})
        area *= NODE_CELL**2
        tiers = sorted({data.node_types[s.entry].skill for s in spawns})
        for tier in tiers:
            use = [s for s in spawns if data.node_types[s.entry].skill <= tier]
            active: dict[int, float] = defaultdict(float)
            supply: dict[int, float] = defaultdict(float)
            for s in use:
                active[s.entry] += s.p_active
                supply[s.entry] += s.p_active * 3600 / s.respawn_s
            total_active = sum(active.values())
            if total_active <= 0:
                continue
            spacing = 0.5 / math.sqrt(total_active / area)
            per_node_s = ROUTE_FACTOR * spacing / SPEED_YD_S + GATHER_S
            nodes = _share_nodes(3600 / per_node_s, active, supply)
            nodes_h = sum(nodes.values())
            per_hour: dict[int, float] = defaultdict(float)
            evidence = []
            for entry, a in sorted(active.items(), key=lambda kv: -kv[1]):
                nt = data.node_types[entry]
                n = nodes[entry]
                evidence.append(f"{nt.name} (skill {nt.skill}): ~{a:.0f} up at once, {n:.0f}/h")
                for item, q in node_loot(nt, data).items():
                    per_hour[item] += n * q * nt.rolls
            for entry in active:
                evidence += [
                    f"{data.node_types[entry].name}: {n}"
                    for n in data.extra_notes.get(("node", entry), [])
                ]
            # merge duplicate node names (several template entries per node type)
            evidence = list(dict.fromkeys(evidence))
            level = max(1, zone_level.get(zone, 1))
            method = _method_from_rates(
                mid=f"hyp_{prof}_{_slug(zone)}_{tier}",
                name=f"{prof.capitalize()} in {zone} (skill {tier})",
                kind=Kind.GATHER,
                zone=zone,
                reqs=Requirements(level=level, professions={prof: tier}),
                items_per_hour=per_hour,
                coins_per_hour=0,
                data=data,
                book=book,
                description=(
                    f"Ride a loop through {zone} gathering everything up to skill {tier}. "
                    f"~{total_active:.0f} nodes up at once, ~{spacing:.0f} yd apart; "
                    f"~{nodes_h:.0f} nodes/h."
                ),
                risk=["competition"],
            )
            # one Wowhead object page per node name (vanilla has several entries per node)
            by_name: dict[str, int] = {}
            for entry, _a in sorted(active.items(), key=lambda kv: -kv[1]):
                by_name.setdefault(data.node_types[entry].name, entry)
            pages = [("object", e, name) for name, e in by_name.items()]
            verified = sum(("node", e) in data.replaced for _, e, _ in pages)
            out.append(
                Candidate(
                    method,
                    evidence,
                    group=f"gather:{prof}:{zone}",
                    pages=pages,
                    verified=verified,
                )
            )
    return out


# -- ranking -------------------------------------------------------------------------------------


def rank(
    candidates: list[Candidate],
    profile: CharacterProfile,
    book: PriceBook,
    opts: PlanOptions,
) -> list[Candidate]:
    """Score with the planner and keep the best eligible variant per group."""
    for c in candidates:
        c.evaluation = evaluate(c.method, profile, book, opts)
        c.eligible = not eligibility(c.method, profile, opts)
    best: dict[str, Candidate] = {}
    for c in candidates:
        if not c.eligible:
            continue
        cur = best.get(c.group)
        if cur is None or c.evaluation.gold_per_hour > cur.evaluation.gold_per_hour:
            best[c.group] = c
    ranked = sorted(best.values(), key=lambda c: -c.evaluation.gold_per_hour)
    # unique ids (two camps of the same mob in one zone)
    seen: dict[str, int] = {}
    for c in ranked:
        n = seen.get(c.method.id, 0)
        seen[c.method.id] = n + 1
        if n:
            c.method = c.method.model_copy(update={"id": f"{c.method.id}_{n + 1}"})
    return ranked


def to_yaml_entries(cands: list[Candidate]) -> list[dict]:
    out = []
    for c in cands:
        d = c.method.model_dump(
            mode="json",
            exclude_defaults=True,
            exclude={"calibration_hours", "calibration_sessions"},
        )
        d["kind"] = c.method.kind.value
        d["confidence"] = c.method.confidence.value
        if c.evaluation:
            d["notes"] = (
                f"{c.method.notes} Predicted ~{c.evaluation.gold_per_hour / 10000:.1f}g/h at "
                "discovery time. Evidence: " + "; ".join(c.evidence)
            )
        out.append(d)
    return out


# -- putting it together ---------------------------------------------------------------------


def find_farms(
    data: VanillaData,
    profile: CharacterProfile,
    book: PriceBook,
    opts: PlanOptions,
    *,
    kind: str = "all",
    elites: bool = False,
    extra: list[Candidate] | None = None,
) -> list[Candidate]:
    cands: list[Candidate] = []
    if kind in ("all", "farm"):
        cands += mob_camps(data, profile, book, elites=elites)
        cands += extra or []
    if kind in ("all", "gather"):
        cands += gathering_zones(data, profile, book)
    return rank(cands, profile, book, opts)


def profile_at_level(profile: CharacterProfile, level: int) -> CharacterProfile:
    """The same character at another level while leveling: profession skill keeps pace with
    level (SKILL_PER_LEVEL per level, never above what you entered), no epic mount before 60,
    and leveling gear below 60."""
    skills = {p: min(s, max(1, SKILL_PER_LEVEL * level)) for p, s in profile.professions.items()}
    update = {"level": level, "professions": skills}
    if level < 60:
        update |= {"epic_mount": False, "gear": GearTier.LEVELING}
    return profile.model_copy(update=update)


def sweep(
    data: VanillaData,
    profile: CharacterProfile,
    book: PriceBook,
    opts: PlanOptions,
    levels: list[int],
    *,
    kind: str = "all",
    elites: bool = False,
) -> list[tuple[CharacterProfile, list[Candidate]]]:
    """Best farms at each level, for the same class, faction and professions."""
    out = []
    for level in levels:
        at = profile_at_level(profile, level)
        out.append((at, find_farms(data, at, book, opts, kind=kind, elites=elites)))
    return out


def parse_levels(spec: str, step: int = 5) -> list[int]:
    """'20-60' -> 20, 25, ..., 60; '15,30,45' -> those levels."""
    levels: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = (int(x) for x in part.split("-", 1))
            if lo > hi:
                lo, hi = hi, lo
            levels.update(range(lo, hi + 1, max(step, 1)))
            levels.add(hi)
        elif part:
            levels.add(int(part))
    if not levels or min(levels) < 1 or max(levels) > 60:
        raise ValueError("levels must be between 1 and 60, e.g. 20-60 or 15,30,45")
    return sorted(levels)


# -- leads: Forever-only sources without spawn data ----------------------------------------------

LEAD_NODES_PER_HOUR = 40.0  # if the nodes are plentiful; we don't know their spawns
NAME_FAMILY_MIN = 4  # "Twilight Peon" + "Twilight Grunt": same family when the zone is unknown


@dataclass
class LeadItem:
    item_id: int
    name: str
    chance: float
    samples: int | None
    value_per_hour: float  # copper, demand-capped
    per_hour: float = 0.0  # units per hour
    source_name: str = ""  # which mob/node drops it (in a group)


@dataclass
class LeadMember:
    """One mob or object in a lead, with what it's worth if you farm only it."""

    source: ItemSource
    kinds: list[str]
    rate: float  # kills or loots per hour if it were the only target
    value_per_kill: float  # copper, uncapped
    gold_per_hour: float  # copper per hour farming only this, demand-capped
    items: list[LeadItem]
    flags: list[str]
    spawns: int | None = None  # spawn points on Wowhead's map, if its NPC page was captured


@dataclass
class Lead:
    source: ItemSource  # representative row (the best member)
    kind: str
    zones: list[str]
    rate: float  # kills or nodes per hour assumed
    items: list[LeadItem]
    gold_per_hour: float  # copper per hour from these items alone, if the source is plentiful
    flags: list[str] = field(default_factory=list)
    members: list[LeadMember] = field(default_factory=list)
    title: str = ""
    grouped_by: str = ""  # zone | name | "" (single source)
    mix_per_hour: float | None = None  # clearing everything in the group, if grouped

    @property
    def best(self) -> LeadMember | None:
        return self.members[0] if self.members else None


def _enrich(src: ItemSource, entities: dict | None) -> ItemSource:
    """Fill a source's level, zones and hostility from its captured NPC/object page."""
    ent = (entities or {}).get((src.source_type, src.source_id))
    info = ent.info if ent else None
    if info is None:
        return src
    return replace(
        src,
        level=src.level or info.level,
        zones=src.zones or info.zones,
        react=src.react or info.react,
        elite=src.elite or info.elite,
        boss=src.boss or info.boss,
        source_name=src.source_name or info.name,
    )


def _member(
    rows: list[ItemSource],
    data: VanillaData,
    profile: CharacterProfile,
    book: PriceBook,
    opts: PlanOptions,
    elites: bool,
) -> LeadMember | None:
    """Score one mob/object from its captured drop rows; None if this character can't farm it."""
    src = rows[0]
    flags: list[str] = []
    kinds = sorted({r.kind for r in rows})
    if src.source_type == "npc":
        if src.boss or src.friendly_to(profile.faction):
            return None
        if src.elite and not elites:
            return None
        lo, hi = src.level or (profile.level, profile.level)
        if lo > profile.level + MAX_LEVELS_ABOVE:
            return None
        keep = []
        for r in rows:
            if r.kind == "pickpocket" and profile.cls != "rogue":
                continue
            if r.kind == "skinning":
                need = skinning_skill_for(hi)
                if profile.professions.get("skinning", 0) < need:
                    continue
                flags.append(f"skinning {need}")
            keep.append(r)
        rows = keep
        mob = v.Creature(
            0,
            src.source_name,
            lo,
            hi,
            v.RANK_ELITE if src.elite else v.RANK_NORMAL,
            0,
            0,
            0,
            0,
            0,
            frozenset(),
        )
        rate = kills_per_hour(mob, profile.level, profile.cls, profile.kill_speed)
        if src.level is None:
            flags.append("level unknown")
        if src.elite:
            flags.append("elite")
    else:
        prof = src.kind if src.kind in ("mining", "herbalism") else None
        skill = max((r.skill or 0) for r in rows)
        if prof and profile.professions.get(prof, 0) < skill:
            return None
        if prof:
            flags.append(f"{prof} {skill}")
        rate = LEAD_NODES_PER_HOUR
    items, total, per_kill = [], 0.0, 0.0
    for r in rows:
        price = book.get(r.item_id)
        vi = data.items.get(r.item_id)
        produced = r.chance * rate * opts.hours
        value = (
            produced * unit_value(r.item_id, data, book)
            if price.market is None
            else _sell(produced, price, opts)[2]
        ) / opts.hours
        name = price.name or (vi.name if vi else r.item_name or f"item {r.item_id}")
        items.append(
            LeadItem(r.item_id, name, r.chance, r.samples, value, r.chance * rate, src.source_name)
        )
        total += value
        per_kill += r.chance * unit_value(r.item_id, data, book)
    if total <= 0:
        return None
    if any((i.samples or 0) < 30 for i in items):
        flags.append("few samples")
    items.sort(key=lambda i: -i.value_per_hour)
    return LeadMember(src, kinds, rate, per_kill, total, items, list(dict.fromkeys(flags)))


def _mix(
    members: list[LeadMember], data: VanillaData, book: PriceBook, opts: PlanOptions
) -> tuple[float, float, list[LeadItem]]:
    """Clearing a group: kill whatever comes up, in proportion to how many there are (spawn
    points when known, else equal). Returns (copper/h, kills/h, items)."""
    known = [m.spawns for m in members if m.spawns]
    default = sorted(known)[len(known) // 2] if known else 1
    weights = [m.spawns or default for m in members]
    hours_per_cycle = sum(w / m.rate for w, m in zip(weights, members, strict=True))
    if hours_per_cycle <= 0:
        return 0.0, 0.0, []
    cycles = 1 / hours_per_cycle  # per hour
    produced: dict[int, float] = defaultdict(float)
    best_row: dict[int, LeadItem] = {}
    for w, m in zip(weights, members, strict=True):
        for it in m.items:
            produced[it.item_id] += cycles * w * it.chance
            if it.item_id not in best_row or it.chance > best_row[it.item_id].chance:
                best_row[it.item_id] = it
    items, total = [], 0.0
    for item_id, per_hour in produced.items():
        price = book.get(item_id)
        amount = per_hour * opts.hours
        value = (
            amount * unit_value(item_id, data, book)
            if price.market is None
            else _sell(amount, price, opts)[2]
        ) / opts.hours
        row = best_row[item_id]
        items.append(
            LeadItem(item_id, row.name, row.chance, row.samples, value, per_hour, row.source_name)
        )
        total += value
    items.sort(key=lambda i: -i.value_per_hour)
    return total, cycles * sum(weights), items


def _family(name: str) -> str | None:
    word = (name or "").split(" ")[0]
    return word if len(word) >= NAME_FAMILY_MIN and len(name.split(" ")) > 1 else None


def find_leads(
    data: VanillaData,
    unplaced: list[ItemSource],
    profile: CharacterProfile,
    book: PriceBook,
    opts: PlanOptions,
    *,
    elites: bool = False,
    entities: dict | None = None,
    zone_names: dict[int, str] | None = None,
) -> list[Lead]:
    """Rank sources we can't build a camp for: value of the captured items alone, assuming the
    mobs/nodes are plentiful. A lead says where to look, not what it pays.

    Mobs in the same zone become one lead ("clear the area"): their drops add up, at a kill
    rate mixed from each mob's. Mobs with no known zone group by name family (Twilight Peon,
    Twilight Grunt, ...). Objects (chests, nodes) stay single."""
    by_source: dict[tuple[str, int], list[ItemSource]] = defaultdict(list)
    for src in unplaced:
        if src.chance and src.chance > 0:
            by_source[(src.source_type, src.source_id)].append(_enrich(src, entities))
    # an item page and the NPC's own page can both list a drop: keep the bigger sample
    for key, rows in by_source.items():
        best: dict[tuple[str, int], ItemSource] = {}
        for r in rows:
            k = (r.kind, r.item_id)
            if k not in best or (r.samples or 0) > (best[k].samples or 0):
                best[k] = r
        by_source[key] = list(best.values())

    names = {**data.area_names, **(zone_names or {})}
    members: list[LeadMember] = []
    for (stype, sid), rows in by_source.items():
        m = _member(rows, data, profile, book, opts, elites)
        if m is None:
            continue
        ent = (entities or {}).get((stype, sid))
        if ent is not None and ent.points:
            m.spawns = len(ent.points)
        members.append(m)

    groups: dict[tuple, list[LeadMember]] = defaultdict(list)
    zoneless_family: dict[str, int] = defaultdict(int)
    for m in members:
        src = m.source
        if src.source_type == "npc" and not src.zones and (fam := _family(src.source_name)):
            zoneless_family[fam] += 1
    for m in members:
        src = m.source
        if src.source_type != "npc":
            groups[("single", src.source_type, src.source_id)].append(m)
        elif src.zones:
            groups[("zone", src.zones[0])].append(m)
        elif (fam := _family(src.source_name)) and zoneless_family[fam] > 1:
            groups[("name", fam)].append(m)
        else:
            groups[("single", "npc", src.source_id)].append(m)

    leads = []
    for key, ms in groups.items():
        ms.sort(key=lambda m: -m.gold_per_hour)
        top = ms[0]
        zones = [names.get(z, f"zone {z}") for z in top.source.zones]
        flags = list(dict.fromkeys(f for m in ms for f in m.flags if f != "few samples"))
        if any("few samples" in m.flags for m in ms):
            flags.append("few samples")
        if len(ms) == 1:
            leads.append(
                Lead(
                    top.source,
                    top.kinds[0] if len(top.kinds) == 1 else "drop",
                    zones,
                    top.rate,
                    top.items,
                    top.gold_per_hour,
                    flags,
                    ms,
                    top.source.source_name,
                )
            )
            continue
        mix, kills, items = _mix(ms, data, book, opts)
        if key[0] == "zone":
            title, grouped_by = f"{len(ms)} mobs in {zones[0] if zones else '?'}", "zone"
        else:
            title, grouped_by = f"{key[1]} mobs ({len(ms)}, zone unknown)", "name"
            flags.insert(0, "grouped by name: check they share a camp")
        if top.gold_per_hour > mix:
            flags.append(f"{top.source.source_name} alone: more than clearing everything")
        leads.append(
            Lead(
                top.source,
                "drop",
                zones,
                kills,
                items,
                max(mix, top.gold_per_hour),
                flags,
                ms,
                title,
                grouped_by,
                mix,
            )
        )
    leads.sort(key=lambda lead: -lead.gold_per_hour)
    return leads


# -- camps for mobs that are new in Forever (positions from Wowhead) ------------------------------

FOREVER_RESPAWN_S = 360  # Wowhead doesn't publish respawn times; vanilla mobs mostly use 5-7 min
MAP_CELL_PCT = 3.0  # % of a zone map per grid cell (~100-150 yards on most zones)


def _best_cluster(points) -> tuple[int, float, float, int | None, str | None]:
    """(spawns, x, y, zone id, zone name) of the densest 3x3-cell window."""
    by_zone: dict[tuple, list] = defaultdict(list)
    for p in points:
        by_zone[(p.zone_id, p.zone_name)].append(p)
    best = (0, 0.0, 0.0, None, None)
    for (zone_id, zone_name), pts in by_zone.items():
        cells: dict[tuple[int, int], list] = defaultdict(list)
        for p in pts:
            cells[(int(p.x // MAP_CELL_PCT), int(p.y // MAP_CELL_PCT))].append(p)
        for cx, cy in cells:
            window = [
                p
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for p in cells.get((cx + dx, cy + dy), ())
            ]
            if len(window) > best[0]:
                x = sum(p.x for p in window) / len(window)
                y = sum(p.y for p in window) / len(window)
                best = (len(window), x, y, zone_id, zone_name)
    return best


def forever_camps(
    data: VanillaData,
    profile: CharacterProfile,
    book: PriceBook,
    entities: dict,
    unplaced: list[ItemSource],
    *,
    elites: bool = False,
) -> list[Candidate]:
    """Camps for mobs with no vanilla spawns, from their captured Wowhead page: map positions
    (spawn count and place), level/hostility, and the drop rates captured for them."""
    loot: dict[int, list[ItemSource]] = defaultdict(list)
    for src in unplaced:
        if src.source_type == "npc" and src.chance and src.kind in ("drop", "skinning"):
            loot[src.source_id].append(src)
    out = []
    for npc_id, sources in loot.items():
        ent = entities.get(("npc", npc_id))
        if ent is None or not ent.points:
            continue
        info = ent.info
        first = sources[0]
        level = (info.level if info else None) or first.level
        elite = (info.elite if info else False) or first.elite
        name = (info.name if info else "") or first.source_name or f"NPC {npc_id}"
        if (info and (info.boss or info.friendly_to(profile.faction))) or first.boss:
            continue
        if first.friendly_to(profile.faction) or (elite and not elites):
            continue
        lo, hi = level or (profile.level, profile.level)
        if lo > profile.level + MAX_LEVELS_ABOVE:
            continue
        spawns, x, y, zone_id, zone_name = _best_cluster(ent.points)
        if not spawns:
            continue
        mob = v.Creature(
            npc_id,
            name,
            lo,
            hi,
            v.RANK_ELITE if elite else v.RANK_NORMAL,
            0,
            0,
            0,
            0,
            0,
            frozenset(),
        )
        respawn = data.respawn.get(npc_id, FOREVER_RESPAWN_S)
        kills = min(
            kills_per_hour(mob, profile.level, profile.cls, profile.kill_speed),
            spawns * 3600 / respawn,
        )
        skinning_need = skinning_skill_for(hi)
        can_skin = profile.professions.get("skinning", 0) >= skinning_need
        per_hour: dict[int, float] = defaultdict(float)
        skinned = False
        samples = 0
        for src in sources:
            if src.kind == "skinning":
                if not can_skin:
                    continue
                skinned = True
            per_hour[src.item_id] += kills * src.chance
            samples = max(samples, src.samples or 0)
        if not per_hour:
            continue
        zone = (
            zone_name
            or data.area_names.get(zone_id or 0)
            or (data.area_names.get(info.zones[0]) if info and info.zones else None)
            or f"zone {zone_id}"
        )
        total = len(ent.points)
        reqs = Requirements(
            level=max(1, min(60, hi - MAX_LEVELS_ABOVE)),
            gear=GearTier.PRERAID if elite else GearTier.LEVELING,
            professions={"skinning": skinning_need} if skinned else {},
        )
        method = _method_from_rates(
            mid=f"hyp_{_slug(zone)}_{_slug(name)}",
            name=f"{name} camp" + (" + skinning" if skinned else ""),
            kind=Kind.FARM,
            zone=zone,
            reqs=reqs,
            items_per_hour=per_hour,
            coins_per_hour=0,
            data=data,
            book=book,
            description=(
                f"Kill {name} (new in Forever). {zone}, around map ({x:.0f}, {y:.0f}): "
                f"{spawns} spawns close together, {total} on Wowhead's map. Respawn "
                + (
                    f"{respawn / 60:.1f} min (measured in your sessions)"
                    if npc_id in data.respawn
                    else f"assumed {FOREVER_RESPAWN_S // 60} min"
                )
                + "; coins not included."
                f" Kill rates assume a level "
                f"{profile.level} {profile.cls}."
            ),
            risk=["competition"]
            + ([] if npc_id in data.respawn else ["assumed_respawn"])
            + (["elite_mobs"] if elite else []),
        )
        evidence = [
            f"{name} (lvl {lo}-{hi}{', elite' if elite else ''}): {kills:.0f} kills/h",
            f"positions: {spawns} spawns near ({x:.0f}, {y:.0f}) of {total} from Wowhead",
        ] + [
            f"Wowhead: {s.chance:.1%} item {s.item_id}" + (f" (n={s.samples})" if s.samples else "")
            for s in sorted(sources, key=lambda s: -(s.chance or 0))[:5]
        ]
        out.append(
            Candidate(
                method,
                evidence,
                group=f"fcamp:{npc_id}",
                pages=[("npc", npc_id, name)],
                verified=1 if samples >= PAGE_REPLACE_MIN_SAMPLES else 0,
            )
        )
    return out
