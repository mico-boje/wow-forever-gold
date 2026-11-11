"""Read-only access to the VMaNGOS world database (vanilla 1.12 data, GPL-2.0).

VMaNGOS publishes its world DB as SQLite (`db-sqlite-*.zip` on the `db_latest` release of
github.com/vmangos/core). It holds loot tables, spawn points with respawn times, and item
vendor prices: the raw material for guessing what a farm is worth. It describes *vanilla*,
not Forever, so everything derived from it is a hypothesis to be tested in game.

Templates are versioned by content patch; we always read the latest (1.12 = patch 10).
"""

from __future__ import annotations

import sqlite3
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import httpx

PATCH = 10  # 1.12
RELEASE_API = "https://api.github.com/repos/vmangos/core/releases/tags/db_latest"
DB_NAME = "mangos.sqlite"

# creature_template.rank
RANK_NORMAL, RANK_ELITE, RANK_RARE_ELITE, RANK_BOSS, RANK_RARE = 0, 1, 2, 3, 4
CREATURE_TYPE_CRITTER = 8
GO_TYPE_CHEST = 3  # herb and mineral nodes are "chests" with a lock

# faction_template masks
MASK_PLAYER, MASK_ALLIANCE, MASK_HORDE = 1, 2, 4

CONTINENTS = (0, 1)  # Eastern Kingdoms, Kalimdor; everything else is an instance


def db_path(data_dir: Path) -> Path:
    return data_dir / "vanilla" / DB_NAME


def fetch(data_dir: Path, *, client: httpx.Client | None = None) -> tuple[Path, str]:
    """Download the latest VMaNGOS SQLite dump and extract the world DB.

    Returns (path, asset name). Skips the download if that asset is already installed."""
    target_dir = data_dir / "vanilla"
    target_dir.mkdir(parents=True, exist_ok=True)
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=120)
    try:
        release = client.get(RELEASE_API, headers={"Accept": "application/vnd.github+json"})
        release.raise_for_status()
        asset = next(a for a in release.json()["assets"] if a["name"].startswith("db-sqlite-"))
        version_file = target_dir / "VERSION"
        out = target_dir / DB_NAME
        if (
            out.exists()
            and version_file.exists()
            and version_file.read_text().strip() == asset["name"]
        ):
            return out, asset["name"]
        zip_path = target_dir / asset["name"]
        with client.stream("GET", asset["browser_download_url"]) as resp:
            resp.raise_for_status()
            with zip_path.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
    finally:
        if own:
            client.close()
    with zipfile.ZipFile(zip_path) as zf:
        member = next(n for n in zf.namelist() if n.endswith("/" + DB_NAME) or n == DB_NAME)
        with zf.open(member) as src, out.open("wb") as dst:
            while chunk := src.read(1 << 20):
                dst.write(chunk)
    zip_path.unlink()
    version_file.write_text(asset["name"] + "\n")
    return out, asset["name"]


def connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; run `wowgold vanilla fetch` first")
    # Read-only, and shared across the web app's worker threads.
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _latest(table: str, key: str = "entry") -> str:
    """Subquery selecting the newest row (<= PATCH) of a patch-versioned template table."""
    return f"""
        (SELECT t.* FROM {table} t
         JOIN (SELECT {key} AS k, MAX(patch) AS p FROM {table} WHERE patch <= {PATCH}
               GROUP BY {key}) m ON m.k = t.{key} AND m.p = t.patch)
    """


IN_PATCH = f"patch_min <= {PATCH} AND patch_max >= {PATCH}"


# -- items -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class VItem:
    item_id: int
    name: str
    quality: int  # 0 poor (grey) .. 5 legendary
    sell_price: int
    bind: int  # 1 = bind on pickup (can't be sold on the AH)
    iclass: int  # 2 weapon, 4 armor, 7 trade goods, ...
    buy_price: int = 0  # what a vendor charges, if it sells it


def load_items(conn: sqlite3.Connection) -> dict[int, VItem]:
    return {
        r["entry"]: VItem(
            r["entry"],
            r["name"],
            r["quality"],
            r["sell_price"],
            r["bonding"],
            r["class"],
            r["buy_price"],
        )
        for r in conn.execute(
            f"SELECT entry, name, quality, sell_price, bonding, class, buy_price "
            f"FROM {_latest('item_template')}"
        )
    }


# -- loot --------------------------------------------------------------------------------------


@dataclass
class LootTables:
    """Expected items per loot roll, following the MaNGOS loot rules:

    - ungrouped rows roll independently with their chance;
    - each group (groupid > 0) drops at most one item: explicit chances first, the rest of the
      100% split evenly among zero-chance rows;
    - a negative mincountOrRef references reference_loot_template, rolled `maxcount` times;
    - negative chances are quest-only drops and rows with conditions are situational; both
      are skipped.
    """

    conn: sqlite3.Connection
    rows: dict[str, dict[int, list[sqlite3.Row]]] = field(default_factory=dict)
    _cache: dict[tuple[str, int], dict[int, float]] = field(default_factory=dict)

    def _table(self, table: str) -> dict[int, list[sqlite3.Row]]:
        if table not in self.rows:
            by_entry: dict[int, list[sqlite3.Row]] = {}
            for r in self.conn.execute(
                f"SELECT entry, item, ChanceOrQuestChance AS chance, groupid, "
                f"mincountOrRef AS mincount, maxcount FROM {table} "
                f"WHERE {IN_PATCH} AND condition_id = 0"
            ):
                by_entry.setdefault(r["entry"], []).append(r)
            self.rows[table] = by_entry
        return self.rows[table]

    def expected(self, table: str, entry: int, _depth: int = 0) -> dict[int, float]:
        """item_id -> expected count per loot roll."""
        key = (table, entry)
        if key in self._cache:
            return self._cache[key]
        out: dict[int, float] = {}
        if not entry or _depth > 5:
            return out

        def add(item: int, n: float) -> None:
            out[item] = out.get(item, 0.0) + n

        groups: dict[int, list[sqlite3.Row]] = {}
        for r in self._table(table).get(entry, []):
            if r["chance"] < 0:
                continue
            if r["mincount"] < 0:
                ref = self.expected("reference_loot_template", -r["mincount"], _depth + 1)
                times = (r["chance"] / 100) * max(r["maxcount"], 1)
                for item, n in ref.items():
                    add(item, n * times)
            elif r["groupid"] > 0:
                groups.setdefault(r["groupid"], []).append(r)
            else:
                add(r["item"], r["chance"] / 100 * (r["mincount"] + r["maxcount"]) / 2)
        for rows in groups.values():
            explicit = [r for r in rows if r["chance"] > 0]
            equal = [r for r in rows if r["chance"] == 0]
            used = min(sum(r["chance"] for r in explicit), 100.0)
            for r in explicit:
                add(r["item"], r["chance"] / 100 * (r["mincount"] + r["maxcount"]) / 2)
            if equal:
                share = (100.0 - used) / 100 / len(equal)
                for r in equal:
                    add(r["item"], share * (r["mincount"] + r["maxcount"]) / 2)
        self._cache[key] = out
        return out


# -- creatures ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Creature:
    entry: int
    name: str
    level_min: int
    level_max: int
    rank: int
    ctype: int
    loot_id: int
    skinning_loot_id: int
    gold_min: int
    gold_max: int
    hostile_to: frozenset[str]  # factions that can attack it: {"alliance", "horde"}
    pickpocket_loot_id: int = 0


def _attackable_by(ft: sqlite3.Row | None) -> frozenset[str]:
    if ft is None:
        return frozenset()
    out = set()
    for faction, mask in (("alliance", MASK_ALLIANCE), ("horde", MASK_HORDE)):
        friendly = (ft["our_mask"] & mask) or (ft["friendly_mask"] & mask)
        if not friendly:
            out.add(faction)
    return frozenset(out)


def load_creatures(conn: sqlite3.Connection) -> dict[int, Creature]:
    factions = {r["id"]: r for r in conn.execute("SELECT * FROM faction_template")}
    out = {}
    for r in conn.execute(
        f"""SELECT entry, name, level_min, level_max, rank, type, loot_id, skinning_loot_id,
                   pickpocket_loot_id,
                   gold_min, gold_max, faction, npc_flags
            FROM {_latest("creature_template")}"""
    ):
        if r["npc_flags"]:  # vendors, quest givers, trainers, flight masters...
            continue
        out[r["entry"]] = Creature(
            entry=r["entry"],
            name=r["name"],
            level_min=r["level_min"],
            level_max=r["level_max"],
            rank=r["rank"],
            ctype=r["type"],
            loot_id=r["loot_id"],
            skinning_loot_id=r["skinning_loot_id"],
            gold_min=r["gold_min"],
            gold_max=r["gold_max"],
            hostile_to=_attackable_by(factions.get(r["faction"])),
            pickpocket_loot_id=r["pickpocket_loot_id"],
        )
    return out


@dataclass(frozen=True)
class Spawn:
    entry: int
    map: int
    x: float
    y: float
    respawn_s: float
    p_active: float = 1.0  # chance this spawn point is populated (node pools)


def load_creature_spawns(conn: sqlite3.Connection, maps=CONTINENTS) -> list[Spawn]:
    marks = ",".join("?" * len(maps))
    return [
        Spawn(r[0], r[1], r[2], r[3], max((r[4] + r[5]) / 2, 30.0))
        for r in conn.execute(
            f"""SELECT id, map, position_x, position_y, spawntimesecsmin, spawntimesecsmax
                FROM creature WHERE {IN_PATCH} AND map IN ({marks})""",
            maps,
        )
    ]


# -- gathering nodes ---------------------------------------------------------------------------

# Vanilla skill needed to gather each node. The DB stores this in Lock.dbc, which isn't
# included, so it's listed here (well known and stable since 1.x).
HERB_SKILL = {
    "Peacebloom": 1,
    "Silverleaf": 1,
    "Earthroot": 15,
    "Mageroyal": 50,
    "Briarthorn": 70,
    "Stranglekelp": 85,
    "Bruiseweed": 100,
    "Wild Steelbloom": 115,
    "Grave Moss": 120,
    "Kingsblood": 125,
    "Liferoot": 150,
    "Fadeleaf": 160,
    "Goldthorn": 170,
    "Khadgar's Whisker": 185,
    "Wintersbite": 195,
    "Firebloom": 205,
    "Purple Lotus": 210,
    "Arthas' Tears": 220,
    "Sungrass": 230,
    "Blindweed": 235,
    "Ghost Mushroom": 245,
    "Gromsblood": 250,
    "Golden Sansam": 260,
    "Dreamfoil": 270,
    "Mountain Silversage": 280,
    "Plaguebloom": 285,
    "Icecap": 290,
    "Black Lotus": 300,
}
ORE_SKILL = {
    "Copper Vein": 1,
    "Tin Vein": 65,
    "Incendicite Mineral Vein": 65,
    "Silver Vein": 75,
    "Ooze Covered Silver Vein": 75,
    "Lesser Bloodstone Deposit": 75,
    "Iron Deposit": 125,
    "Indurium Mineral Vein": 150,
    "Gold Vein": 155,
    "Ooze Covered Gold Vein": 155,
    "Mithril Deposit": 175,
    "Ooze Covered Mithril Deposit": 175,
    "Truesilver Deposit": 230,
    "Ooze Covered Truesilver Deposit": 230,
    "Dark Iron Deposit": 230,
    "Small Thorium Vein": 245,
    "Ooze Covered Thorium Vein": 245,
    "Rich Thorium Vein": 275,
    "Ooze Covered Rich Thorium Vein": 275,
}


@dataclass(frozen=True)
class NodeType:
    entry: int
    name: str
    profession: str  # herbalism | mining
    skill: int
    loot_id: int
    rolls: float  # loot rolls per node (mineral veins can be mined several times)


def load_node_types(conn: sqlite3.Connection) -> dict[int, NodeType]:
    out = {}
    for r in conn.execute(
        f"SELECT entry, name, data1, data4, data5 FROM {_latest('gameobject_template')} "
        f"WHERE type = {GO_TYPE_CHEST}"
    ):
        if r["name"] in HERB_SKILL:
            prof, skill, rolls = "herbalism", HERB_SKILL[r["name"]], 1.0
        elif r["name"] in ORE_SKILL:
            prof, skill = "mining", ORE_SKILL[r["name"]]
            rolls = (r["data4"] + r["data5"]) / 2 if r["data5"] else 1.0
        else:
            continue
        out[r["entry"]] = NodeType(r["entry"], r["name"], prof, skill, r["data1"], max(rolls, 1.0))
    return out


def load_node_spawns(
    conn: sqlite3.Connection, node_types: dict[int, NodeType], maps=CONTINENTS
) -> list[Spawn]:
    """Node spawn points with the probability that each is populated at a given moment.

    Nodes live in pools: a spawn point's group pool shows one of its candidates (by chance),
    and a zone's mother pool keeps only `max_limit` of its groups active at once."""
    pools = {
        r["entry"]: r["max_limit"]
        for r in conn.execute(f"SELECT entry, max_limit FROM pool_template WHERE {IN_PATCH}")
    }
    members: dict[int, list[sqlite3.Row]] = {}
    member_of: dict[int, sqlite3.Row] = {}
    for r in conn.execute(f"SELECT guid, pool_entry, chance FROM pool_gameobject WHERE {IN_PATCH}"):
        members.setdefault(r["pool_entry"], []).append(r)
        member_of[r["guid"]] = r
    children: dict[int, int] = {}
    mother_of: dict[int, int] = {}
    for r in conn.execute("SELECT pool_id, mother_pool FROM pool_pool"):
        children[r["mother_pool"]] = children.get(r["mother_pool"], 0) + 1
        mother_of[r["pool_id"]] = r["mother_pool"]

    def p_member(row: sqlite3.Row) -> float:
        pool, rows = row["pool_entry"], members[row["pool_entry"]]
        limit = pools.get(pool, 1) or len(rows)
        if limit > 1:
            return min(1.0, limit / len(rows))
        if row["chance"] > 0:
            return row["chance"] / 100
        explicit = sum(r["chance"] for r in rows if r["chance"] > 0)
        n_equal = sum(1 for r in rows if r["chance"] == 0)
        return max(0.0, 100 - explicit) / 100 / max(n_equal, 1)

    def p_pool(pool: int) -> float:
        mother = mother_of.get(pool)
        if mother is None:
            return 1.0
        limit = pools.get(mother, 1) or children[mother]
        return min(1.0, limit / children[mother]) * p_pool(mother)

    marks = ",".join("?" * len(maps))
    out = []
    for r in conn.execute(
        f"""SELECT guid, id, map, position_x, position_y, spawntimesecsmin, spawntimesecsmax
            FROM gameobject WHERE {IN_PATCH} AND map IN ({marks})""",
        maps,
    ):
        if r["id"] not in node_types:
            continue
        p = 1.0
        if (m := member_of.get(r["guid"])) is not None:
            p = p_member(m) * p_pool(m["pool_entry"])
        respawn = max((r["spawntimesecsmin"] + r["spawntimesecsmax"]) / 2, 60.0)
        out.append(Spawn(r["id"], r["map"], r["position_x"], r["position_y"], respawn, p))
    return out
