"""Discovery from a tiny synthetic VMaNGOS-shaped world database."""

from __future__ import annotations

import sqlite3

import pytest
import yaml

from wowgold.discovery import vanilla as v
from wowgold.discovery.discover import (
    CLASS_KILL_SPEED,
    VanillaData,
    _share_nodes,
    find_farms,
    gathering_zones,
    kills_per_hour,
    mob_camps,
    parse_levels,
    profile_at_level,
    rank,
    skinning_skill_for,
    sweep,
    to_yaml_entries,
)
from wowgold.discovery.zones import _match_zone
from wowgold.methods.models import Method
from wowgold.planner.engine import PlanOptions
from wowgold.planner.pricing import ItemPrice, PriceBook
from wowgold.planner.profile import CharacterProfile

LOOT_COLS = (
    "entry INT, item INT, ChanceOrQuestChance REAL, groupid INT, mincountOrRef INT, "
    "maxcount INT, condition_id INT DEFAULT 0, patch_min INT DEFAULT 0, patch_max INT DEFAULT 10"
)
SCHEMA = f"""
CREATE TABLE item_template (entry INT, patch INT, name TEXT, quality INT, sell_price INT,
    bonding INT, class INT, buy_price INT DEFAULT 0, spellid_1 INT DEFAULT 0,
    spellid_2 INT DEFAULT 0, required_skill_rank INT DEFAULT 0);
CREATE TABLE spell_template (entry INT, build INT, name TEXT, effect1 INT,
    effectItemType1 INT DEFAULT 0, effectTriggerSpell1 INT DEFAULT 0,
    effectBasePoints1 INT DEFAULT 0, effectDieSides1 INT DEFAULT 1, effectBaseDice1 INT DEFAULT 0,
    recoveryTime INT DEFAULT 0, categoryRecoveryTime INT DEFAULT 0,
    {", ".join(f"reagent{i} INT DEFAULT 0, reagentCount{i} INT DEFAULT 0" for i in range(1, 9))});
CREATE TABLE skill_line_ability (build INT, skill_id INT, spell_id INT,
    learn_on_get_skill INT DEFAULT 0, min_value INT DEFAULT 0);
CREATE TABLE npc_trainer (spell INT, spellcost INT, reqskillvalue INT);
CREATE TABLE npc_trainer_template (spell INT, spellcost INT, reqskillvalue INT);
CREATE TABLE npc_vendor (item INT);
CREATE TABLE faction_template (id INT, our_mask INT, friendly_mask INT, hostile_mask INT);
CREATE TABLE creature_template (entry INT, patch INT, name TEXT, level_min INT, level_max INT,
    rank INT, type INT, loot_id INT, skinning_loot_id INT, gold_min INT, gold_max INT,
    faction INT, npc_flags INT, pickpocket_loot_id INT DEFAULT 0);
CREATE TABLE creature (guid INT, id INT, map INT, position_x REAL, position_y REAL,
    spawntimesecsmin INT, spawntimesecsmax INT, patch_min INT DEFAULT 0, patch_max INT DEFAULT 10);
CREATE TABLE creature_loot_template ({LOOT_COLS});
CREATE TABLE skinning_loot_template ({LOOT_COLS});
CREATE TABLE reference_loot_template ({LOOT_COLS});
CREATE TABLE pickpocketing_loot_template ({LOOT_COLS});
CREATE TABLE gameobject_loot_template ({LOOT_COLS});
CREATE TABLE gameobject_template (entry INT, patch INT, type INT, name TEXT, data1 INT,
    data4 INT, data5 INT);
CREATE TABLE gameobject (guid INT, id INT, map INT, position_x REAL, position_y REAL,
    spawntimesecsmin INT, spawntimesecsmax INT, patch_min INT DEFAULT 0, patch_max INT DEFAULT 10);
CREATE TABLE pool_template (entry INT, max_limit INT, description TEXT,
    patch_min INT DEFAULT 0, patch_max INT DEFAULT 10);
CREATE TABLE pool_gameobject (guid INT, pool_entry INT, chance REAL,
    patch_min INT DEFAULT 0, patch_max INT DEFAULT 10);
CREATE TABLE pool_pool (pool_id INT, mother_pool INT);
CREATE TABLE area_template (entry INT, map_id INT, zone_id INT, name TEXT);
CREATE TABLE quest_template (ZoneOrSort INT, ReqCreatureOrGOId1 INT, ReqCreatureOrGOId2 INT,
    ReqCreatureOrGOId3 INT, ReqCreatureOrGOId4 INT);
"""

CLOTH, BONES, PATTERN, HIDE, HERB, GLOVES, QUEST = 100, 101, 102, 103, 104, 105, 106
VIAL, DRAUGHT, RECIPE_ITEM, ELIXIR = 107, 108, 109, 110
DRAUGHT_SPELL, TRANSMUTE_SPELL = 1001, 1003


def build_world(path):
    """Write the synthetic world DB to `path`."""
    c = sqlite3.connect(path)
    c.executescript(SCHEMA)
    c.executemany(
        "INSERT INTO item_template (entry, patch, name, quality, sell_price, bonding, class) "
        "VALUES (?,10,?,?,?,?,?)",
        [
            (CLOTH, "Runecloth", 1, 100, 0, 7),
            (BONES, "Old Bone", 0, 50, 0, 15),
            (PATTERN, "Pattern: Cool Pants", 2, 1000, 2, 9),
            (HIDE, "Rugged Hide", 1, 500, 0, 7),
            (HERB, "Dreamfoil", 1, 200, 0, 7),
            (GLOVES, "Warrior's Gloves", 1, 33, 0, 4),
            (QUEST, "Quest Thing", 1, 0, 1, 12),
        ],
    )
    c.executemany(
        "INSERT INTO faction_template VALUES (?,?,?,?)",
        [(14, 8, 0, 1), (85, 4, 4, 2)],  # 14: monster; 85: horde-friendly (Orgrimmar)
    )
    c.executemany(
        "INSERT INTO creature_template VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
        [
            # entry, patch, name, lvl, lvl, rank, type, loot, skin, gold, gold, faction, npc
            (1, 0, "Old Ogre", 50, 50, 0, 7, 1, 0, 100, 100, 14, 0),
            (1, 10, "Ogre", 50, 52, 0, 7, 1, 0, 200, 400, 14, 0),  # newest patch wins
            (2, 10, "Boar", 50, 50, 0, 1, 2, 2, 0, 0, 14, 0),
            (3, 10, "Grunt", 50, 50, 1, 7, 1, 0, 0, 0, 85, 0),  # friendly to horde
            (4, 10, "Vendor", 50, 50, 0, 7, 1, 0, 0, 0, 14, 128),  # has npc flags
        ],
    )
    # Ogres: 4 spawns close together, Boars far away. Respawn 360 s -> 10 kills/h each.
    c.executemany(
        "INSERT INTO creature (guid, id, map, position_x, position_y, spawntimesecsmin, "
        "spawntimesecsmax) VALUES (?,?,?,?,?,?,?)",
        [(i, 1, 1, 1000 + 20 * i, 1000, 300, 420) for i in range(4)]
        + [(10 + i, 2, 1, 5000 + 20 * i, 5000, 360, 360) for i in range(2)]
        + [(20, 3, 1, 1010, 1010, 360, 360)],
    )
    c.executemany(
        "INSERT INTO creature_loot_template (entry, item, ChanceOrQuestChance, groupid, "
        "mincountOrRef, maxcount) VALUES (?,?,?,?,?,?)",
        [
            (1, CLOTH, 50, 0, 1, 3),  # 0.5 × 2 = 1.0 cloth/kill
            (1, QUEST, -80, 0, 1, 1),  # quest drop: skipped
            (1, BONES, 30, 1, 1, 1),  # group 1: 30% bones,
            (1, GLOVES, 0, 1, 1, 1),  # the remaining 70% split between gloves
            (1, CLOTH, 0, 1, 1, 1),  # and one more cloth (35% each)
            (1, 500, 10, 0, -500, 2),  # reference rolled twice at 10%
            (2, BONES, 100, 0, 1, 1),
        ],
    )
    c.execute(
        "INSERT INTO reference_loot_template (entry, item, ChanceOrQuestChance, groupid, "
        "mincountOrRef, maxcount) VALUES (500, ?, 5, 0, 1, 1)",
        (PATTERN,),
    )
    c.execute(
        "INSERT INTO skinning_loot_template (entry, item, ChanceOrQuestChance, groupid, "
        "mincountOrRef, maxcount) VALUES (2, ?, 100, 0, 1, 1)",
        (HIDE,),
    )
    # Herb nodes: a zone pool keeps 2 of its 4 spawn-point groups up; each group shows one
    # of two node types.
    c.executemany(
        "INSERT INTO gameobject_template VALUES (?,10,3,?,?,0,0)",
        [(900, "Dreamfoil", 900), (901, "Peacebloom", 901), (902, "Barrel", 902)],
    )
    c.execute(
        "INSERT INTO gameobject_loot_template (entry, item, ChanceOrQuestChance, groupid, "
        "mincountOrRef, maxcount) VALUES (900, ?, 100, 0, 1, 3)",
        (HERB,),
    )
    guid = 100
    c.execute(
        "INSERT INTO pool_template (entry, max_limit, description) VALUES "
        "(1, 2, 'Felwood - Herbs (Master Pool)')"
    )
    for group in range(4):
        pool = 10 + group
        c.execute(
            "INSERT INTO pool_template (entry, max_limit, description) VALUES (?, 1, ?)",
            (pool, "Dreamfoil / Peacebloom"),
        )
        c.execute("INSERT INTO pool_pool VALUES (?, 1)", (pool,))
        for node in (900, 901):
            c.execute(
                "INSERT INTO gameobject (guid, id, map, position_x, position_y, "
                "spawntimesecsmin, spawntimesecsmax) VALUES (?,?,1,?,?,300,300)",
                (guid, node, 2000 + 100 * group, 2000),
            )
            c.execute(
                "INSERT INTO pool_gameobject (guid, pool_entry, chance) VALUES (?,?,0)",
                (guid, pool),
            )
            guid += 1
    c.execute("INSERT INTO area_template VALUES (361, 1, 0, 'Felwood')")
    # Recipes: a trainer-taught alchemy draught (2 Dreamfoil + a vendor vial) and a transmute
    # with a 2-day cooldown taught by a vendor-sold recipe item.
    c.executemany(
        "INSERT INTO item_template (entry, patch, name, quality, sell_price, bonding, class, "
        "buy_price, spellid_1, required_skill_rank) VALUES (?,10,?,?,?,0,?,?,?,?)",
        [
            (VIAL, "Crystal Vial", 1, 10, 7, 40, 0, 0),
            (DRAUGHT, "Dreamfoil Draught", 1, 500, 0, 0, 0, 0),
            (RECIPE_ITEM, "Recipe: Transmute Bone", 2, 1250, 9, 5000, 2002, 250),
            (ELIXIR, "Bone Elixir", 1, 100, 0, 0, 0, 0),
        ],
    )
    c.executemany(
        "INSERT INTO spell_template (entry, build, name, effect1, effectItemType1, "
        "effectTriggerSpell1, categoryRecoveryTime, reagent1, reagentCount1, reagent2, "
        "reagentCount2) VALUES (?,5875,?,?,?,?,?,?,?,?,?)",
        [
            (DRAUGHT_SPELL, "Dreamfoil Draught", 24, DRAUGHT, 0, 0, HERB, 2, VIAL, 1),
            (TRANSMUTE_SPELL, "Transmute: Bone", 24, ELIXIR, 0, 172800000, BONES, 1, 0, 0),
            (2001, "Dreamfoil Draught", 36, 0, DRAUGHT_SPELL, 0, 0, 0, 0, 0),
            (2002, "Recipe: Transmute Bone", 36, 0, TRANSMUTE_SPELL, 0, 0, 0, 0, 0),
        ],
    )
    c.executemany(
        "INSERT INTO skill_line_ability VALUES (5875, 171, ?, 0, ?)",
        [(DRAUGHT_SPELL, 215), (TRANSMUTE_SPELL, 265)],
    )
    c.execute("INSERT INTO npc_trainer VALUES (2001, 1000, 200)")
    c.executemany("INSERT INTO npc_vendor VALUES (?)", [(VIAL,), (RECIPE_ITEM,)])
    c.execute("INSERT INTO quest_template VALUES (361, 1, 0, 0, 0)")  # kill ogres in Felwood
    c.commit()
    c.close()
    return path


@pytest.fixture
def world(tmp_path):
    conn = v.connect(build_world(tmp_path / "mangos.sqlite"))
    yield conn
    conn.close()


def _price(item_id, market=None, listed=0, vendor_sell=0, name=None):
    return ItemPrice(item_id, name, market, listed, vendor_sell, None, None, listed * 0.25, False)


@pytest.fixture
def book():
    prices = [
        _price(CLOTH, market=10000, listed=1000, vendor_sell=100, name="Runecloth"),
        _price(PATTERN, market=500000, listed=8, vendor_sell=1000, name="Pattern: Cool Pants"),
        _price(GLOVES, market=1400000, listed=5, vendor_sell=33),  # troll listings
        _price(HERB, market=30000, listed=500, vendor_sell=200, name="Dreamfoil"),
        _price(HIDE, market=5000, listed=200, vendor_sell=500),
    ]
    return PriceBook(house_ids=[1], prices={p.item_id: p for p in prices})


def profile(**kw) -> CharacterProfile:
    base = {"class": "warlock", "level": 52, "faction": "horde", "professions": {}}
    return CharacterProfile(**{**base, **kw})


def test_loot_rules(world):
    loot = v.LootTables(world).expected("creature_loot_template", 1)
    assert loot[CLOTH] == pytest.approx(1.0 + 0.35)
    assert loot[BONES] == pytest.approx(0.30)
    assert loot[GLOVES] == pytest.approx(0.35)
    assert loot[PATTERN] == pytest.approx(0.10 * 2 * 0.05)
    assert QUEST not in loot


def test_latest_patch_and_filters(world):
    creatures = v.load_creatures(world)
    assert creatures[1].name == "Ogre" and creatures[1].gold_max == 400
    assert 4 not in creatures  # vendor
    assert creatures[3].hostile_to == frozenset({"alliance"})
    assert creatures[1].hostile_to == frozenset({"alliance", "horde"})


def test_node_pools(world):
    types = v.load_node_types(world)
    assert set(types) == {900, 901} and types[900].skill == 270
    spawns = v.load_node_spawns(world, types)
    # 2 of 4 groups up, each showing one of two nodes: 0.25 per spawn point and type.
    assert all(s.p_active == pytest.approx(0.25) for s in spawns)
    assert sum(s.p_active for s in spawns) == pytest.approx(2.0)


def test_share_nodes_respects_respawn():
    out = _share_nodes(100, {1: 9.0, 2: 1.0}, {1: 1000.0, 2: 3.0})
    assert out[2] == pytest.approx(3.0)  # capped by respawns
    assert out[1] == pytest.approx(90.0)  # its own share; the slack isn't reassigned


def test_kill_rates_and_skinning_skill():
    ogre = v.Creature(1, "Ogre", 50, 50, 0, 7, 1, 0, 0, 0, frozenset())
    assert kills_per_hour(ogre, 50) == pytest.approx(40)
    assert kills_per_hour(ogre, 55) > kills_per_hour(ogre, 50) > kills_per_hour(ogre, 48)
    assert skinning_skill_for(5) == 1 and skinning_skill_for(15) == 50
    assert skinning_skill_for(50) == 250


def test_zone_names_from_pool_descriptions():
    names = ["Felwood", "The Barrens", "Stranglethorn Vale"]
    assert _match_zone("Dreamfoil - Felwood - Group 1 - Master", names) == "Felwood"
    assert _match_zone("Barrens - Stagnant Oasis - Minerals", names) == "The Barrens"
    assert _match_zone("Herbs in Dire Maul", names) is None


def test_mob_camp_candidates(world, book):
    data = VanillaData.load(world)
    prof = profile()
    cands = mob_camps(data, prof, book)
    camp = next(c for c in cands if "Ogre" in c.method.name)
    m = camp.method
    assert m.zone == "Felwood"
    assert m.confidence == "hypothesis"
    # 4 ogres respawning every 6 min = 40 kills/h, below what a level 52 kills (40 × 1.0)
    assert "40 kills/h" in camp.evidence[0]
    yields = {i.item: i for i in m.yields}
    assert yields[CLOTH].qty == pytest.approx(40 * 1.35)
    assert GLOVES not in yields  # white armor: vendor value only, despite the AH listing
    assert PATTERN in yields
    # coins (300c avg) + bones (no AH price) + gloves at vendor
    assert m.vendor_copper_per_hour == round(40 * (300 + 0.30 * 50 + 0.35 * 33))
    assert not any("Grunt" in c.method.name for c in cands)  # friendly to horde

    boar = next(c for c in cands if c.method.id.endswith("_skin"))
    assert boar.method.requirements.professions == {"skinning": 250}


def test_gathering_and_ranking(world, book):
    data = VanillaData.load(world)
    prof = profile(professions={"herbalism": 300})
    cands = gathering_zones(data, prof, book)
    assert {c.method.requirements.professions["herbalism"] for c in cands} == {1, 270}
    ranked = rank(cands, prof, book, PlanOptions())
    assert len(ranked) == 1  # one group per zone/profession
    best = ranked[0]
    assert best.method.requirements.professions == {"herbalism": 270}
    assert best.evaluation.gold_per_hour > 0
    assert any("hypothesis" in f for f in best.evaluation.flags)

    entries = to_yaml_entries(ranked)
    round_trip = yaml.safe_load(yaml.safe_dump(entries))
    assert Method.model_validate(round_trip[0]).id == best.method.id


def test_fetch_downloads_and_extracts(tmp_path):
    import io
    import zipfile

    import httpx
    import respx

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("sqlite-dump/mangos.sqlite", b"db-bytes")
        zf.writestr("sqlite-dump/logs.sqlite", b"other")
    url = "https://github.com/vmangos/core/releases/download/db_latest/db-sqlite-1.zip"
    with respx.mock() as router:
        router.get(v.RELEASE_API).mock(
            return_value=httpx.Response(
                200,
                json={"assets": [{"name": "db-sqlite-1.zip", "browser_download_url": url}]},
            )
        )
        dl = router.get(url).mock(return_value=httpx.Response(200, content=buf.getvalue()))
        path, name = v.fetch(tmp_path)
        assert name == "db-sqlite-1.zip"
        assert path.read_bytes() == b"db-bytes"
        assert not (tmp_path / "vanilla" / "db-sqlite-1.zip").exists()
        v.fetch(tmp_path)  # same version installed: no second download
        assert dl.call_count == 1


def test_wowhead_sources_extend_vanilla_loot(world, book):
    from wowgold.wowhead.sources import ItemSource

    fel = 248819
    book.prices[fel] = _price(fel, market=400000, listed=40, vendor_sell=0, name="Fel Crystal")
    data = VanillaData.load(world)
    placed, unplaced = data.add_sources(
        [
            ItemSource(fel, "drop", "npc", 1, "Ogre", 0.05, 1000, (361,)),
            ItemSource(fel, "drop", "npc", 777777, "Forever Demon", 0.5, 20, ()),
            ItemSource(fel, "herbalism", "object", 123456, "Dreamfoil", 0.1, 300, ()),
            ItemSource(fel, "drop", "npc", 2, "Boar", None, None, ()),  # no chance: ignored
        ]
    )
    assert [p.source_id for p in placed] == [1, 123456]
    assert [u.source_id for u in unplaced] == [777777]

    camp = next(c for c in mob_camps(data, profile(), book) if c.method.name == "Ogre camp")
    yields = {i.item: i.qty for i in camp.method.yields}
    assert yields[fel] == pytest.approx(40 * 0.05)
    assert any("Wowhead: 5.0%" in e for e in camp.evidence)

    herbs = gathering_zones(data, profile(professions={"herbalism": 300}), book)
    assert any(fel in {i.item for i in c.method.yields} for c in herbs)


def test_class_kill_speed_and_mage_aoe():
    ogre = v.Creature(1, "Ogre", 50, 50, 0, 7, 1, 0, 0, 0, frozenset())
    assert kills_per_hour(ogre, 50, "hunter") == pytest.approx(40 * CLASS_KILL_SPEED["hunter"])
    assert kills_per_hour(ogre, 50, "priest") < kills_per_hour(ogre, 50, "warlock")
    # mages AoE mobs 5+ levels below them, but not elites
    mage_low = kills_per_hour(ogre, 55, "mage")
    assert mage_low == pytest.approx(kills_per_hour(ogre, 55, "rogue") * 1.05 * 2.0)
    elite = v.Creature(2, "Brute", 50, 50, 1, 7, 1, 0, 0, 0, frozenset())
    assert kills_per_hour(elite, 55, "mage") == pytest.approx(
        kills_per_hour(elite, 55, "rogue") * 1.05
    )


def test_rogues_pickpocket(world, book, tmp_path):
    rw = sqlite3.connect(tmp_path / "mangos.sqlite")
    rw.execute("UPDATE creature_template SET pickpocket_loot_id = 1 WHERE entry = 1")
    rw.execute(
        "INSERT INTO pickpocketing_loot_template (entry, item, ChanceOrQuestChance, groupid, "
        "mincountOrRef, maxcount) VALUES (1, ?, 50, 0, 1, 1)",
        (CLOTH,),
    )
    rw.commit()
    rw.close()
    data = VanillaData.load(world)

    def cloth(cls):
        camp = next(
            c
            for c in mob_camps(data, profile(**{"class": cls}), book)
            if c.method.name == "Ogre camp"
        )
        return {i.item: i.qty for i in camp.method.yields}[CLOTH], camp

    rogue, camp = cloth("rogue")
    warlock, _ = cloth("warlock")
    assert rogue == pytest.approx(warlock + 40 * 0.5)  # same 40 kills (respawn-capped)
    assert "pickpocketing" in camp.method.description


def test_profile_at_level():
    p = profile(
        level=60, professions={"herbalism": 300, "skinning": 150}, gear="preraid", epic_mount=True
    )
    at30 = profile_at_level(p, 30)
    assert at30.professions == {"herbalism": 150, "skinning": 150}
    assert at30.gear == "leveling" and not at30.epic_mount
    assert profile_at_level(p, 60).gear == "preraid"


def test_parse_levels():
    assert parse_levels("20-40") == [20, 25, 30, 35, 40]
    assert parse_levels("42-30", step=10) == [30, 40, 42]
    assert parse_levels("15, 45") == [15, 45]
    with pytest.raises(ValueError):
        parse_levels("50-70")


def test_sweep_ranks_each_level(world, book):
    data = VanillaData.load(world)
    prof = profile(level=60, professions={"herbalism": 300})
    out = sweep(data, prof, book, PlanOptions(), [20, 52])
    assert [p.level for p, _ in out] == [20, 52]
    low, high = out[0][1], out[1][1]
    assert not any("Ogre" in c.method.name for c in low)  # level 50+ mobs at level 20
    assert any("Ogre" in c.method.name for c in high)
    direct = find_farms(data, profile_at_level(prof, 52), book, PlanOptions())
    assert [c.method.id for c in high] == [c.method.id for c in direct]
    gph = [c.evaluation.gold_per_hour for c in high]
    assert gph == sorted(gph, reverse=True)


def test_leads_from_unplaceable_sources(world, book):
    from wowgold.discovery.discover import find_leads
    from wowgold.wowhead.sources import ItemSource

    fel = 248819
    book.prices[fel] = _price(fel, market=400000, listed=1000, vendor_sell=0, name="Fel Crystal")
    data = VanillaData.load(world)

    def src(sid, name, **kw):
        base = dict(
            item_id=fel,
            kind="drop",
            source_type="npc",
            source_id=sid,
            source_name=name,
            chance=0.05,
            samples=500,
            zones=(361,),
            level=(50, 51),
            react=(-1, -1),
        )
        return ItemSource(**{**base, **kw})

    sources = [
        src(900001, "Fel Imp"),
        src(900001, "Fel Imp", item_id=CLOTH, chance=0.5),  # same NPC: one lead, two items
        src(900002, "Friendly Guard", react=(-1, 1)),
        src(900003, "Dungeon Boss", boss=True),
        src(900004, "Big Elite", elite=True),
        src(900005, "Too High", level=(58, 60)),
        src(900006, "Skinnable", kind="skinning"),  # needs skinning 255
        src(
            900007,
            "Fel Bloom",
            kind="herbalism",
            source_type="object",
            level=None,
            skill=290,
            react=None,
        ),
    ]
    placed, unplaced = data.add_sources(sources)
    assert not placed and len(unplaced) == len(sources)

    prof = profile(professions={"herbalism": 300})
    leads = find_leads(data, unplaced, prof, book, PlanOptions())
    names = [lead.source.source_name for lead in leads]
    assert set(names) == {"Fel Imp", "Fel Bloom"}
    imp = next(lead for lead in leads if lead.source.source_name == "Fel Imp")
    assert {i.item_id for i in imp.items} == {fel, CLOTH}
    assert imp.zones == ["Felwood"]
    assert imp.gold_per_hour > 0

    with_elites = find_leads(data, unplaced, prof, book, PlanOptions(), elites=True)
    assert "Big Elite" in [lead.source.source_name for lead in with_elites]
    assert "Dungeon Boss" not in [lead.source.source_name for lead in with_elites]


def test_instance_only_vanilla_mob_is_a_lead(world, book, tmp_path):
    from wowgold.wowhead.sources import ItemSource

    rw = sqlite3.connect(tmp_path / "mangos.sqlite")
    rw.execute("UPDATE creature SET map = 109 WHERE id = 2")  # boars only in an instance
    rw.commit()
    rw.close()
    data = VanillaData.load(world)
    placed, unplaced = data.add_sources(
        [ItemSource(248819, "drop", "npc", 2, "Boar", 0.1, 100, ())]
    )
    assert not placed and unplaced[0].source_id == 2


def test_npc_page_replaces_vanilla_loot_and_verifies_camp(world, book):
    from wowgold.wowhead.sources import ItemSource

    data = VanillaData.load(world)
    before = next(c for c in mob_camps(data, profile(), book) if c.method.name == "Ogre camp")
    assert before.pages[0][:2] == ("npc", 1) and before.verified == 0
    assert before.verified_label == "0/1"

    page = dict(
        kind="drop", source_type="npc", source_id=1, source_name="Ogre", zones=(), from_page=True
    )
    data.add_sources([ItemSource(item_id=HERB, chance=0.5, samples=400, **page)])
    after = next(c for c in mob_camps(data, profile(), book) if c.method.name == "Ogre camp")
    yields = {i.item for i in after.method.yields}
    assert yields == {HERB}  # vanilla cloth and pattern are gone: Forever table replaces them
    assert after.verified == 1
    assert any("Forever loot table from Wowhead (n=400)" in e for e in after.evidence)


def test_small_npc_page_only_patches(world, book):
    from wowgold.wowhead.sources import ItemSource

    data = VanillaData.load(world)
    data.add_sources([ItemSource(HERB, "drop", "npc", 1, "Ogre", 0.5, 12, (), from_page=True)])
    camp = next(c for c in mob_camps(data, profile(), book) if c.method.name == "Ogre camp")
    assert {HERB, CLOTH} <= {i.item for i in camp.method.yields}
    assert camp.verified == 0
