"""Forever zones: Wowhead zone pages, the capture queue, gathering loops and leads."""

from __future__ import annotations

import json

import pytest

from tests.test_discovery import HERB, _price, build_world, profile
from wowgold.discovery import vanilla as v
from wowgold.discovery.discover import (
    FOREVER_ZONE_YD,
    VanillaData,
    find_leads,
    forever_gathering,
)
from wowgold.planner.engine import PlanOptions
from wowgold.wowhead.capture import page_key, userscript
from wowgold.wowhead.entities import Entity, EntityInfo, MapPoint
from wowgold.wowhead.sources import ItemSource
from wowgold.wowhead.zones import load_zones, zone_from_capture, zone_queue

RIVER = 16591
GLOWCAP, GLOW_HERB = 990001, 248900  # a Forever-only herb node and what it gives

ZONE_RECORD = {
    "url": f"https://www.wowhead.com/forever/zone={RIVER}/riverglades",
    "title": "Riverglades - Zone - World of Warcraft: Forever",
    "captured_at": "2026-11-15T10:00:00+00:00",
    "listviews": {
        "npcs": {
            "template": "npc",
            "data": [
                {
                    "id": 900001,
                    "name": "Fen Lurker",
                    "minlevel": 38,
                    "maxlevel": 39,
                    "react": [-1, -1],
                    "classification": 0,
                },
                {
                    "id": 900002,
                    "name": "Swamp Ogre",
                    "minlevel": 40,
                    "maxlevel": 41,
                    "react": [-1, -1],
                    "classification": 1,
                },
                {
                    "id": 900003,
                    "name": "Powderfuse Guard",
                    "minlevel": 45,
                    "maxlevel": 45,
                    "react": [1, 1],
                    "classification": 0,
                },
                {
                    "id": 900004,
                    "name": "Ombassa",
                    "minlevel": 41,
                    "maxlevel": 41,
                    "react": [-1, -1],
                    "classification": 3,
                },
            ],
        },
    },
    # objects only in the raw page script, as a variable (Wowhead does both)
    "raw": [
        "var lvobjects = [{id: 900, name: 'Dreamfoil', type: -2},"
        f" {{id: {GLOWCAP}, name: 'Glowcap Cluster', type: -3, skill: 150}},"
        " {id: 990002, name: 'Smuggler\\'s Strongbox', type: 3},"
        " {id: 990003, name: 'Wanted Poster', type: 9}];\n"
        "new Listview({template: 'object', id: 'objects', data: lvobjects});"
    ],
}


def test_zone_pages_are_captured():
    assert page_key(f"https://www.wowhead.com/forever/zone={RIVER}/riverglades") == f"zone-{RIVER}"
    assert "@match        https://www.wowhead.com/forever/zone=*" in userscript(8765, "t")


def test_zone_page_parsing(tmp_path):
    z = zone_from_capture(ZONE_RECORD, RIVER, herbs={"Dreamfoil"}, ores=set())
    assert z.name == "Riverglades"
    assert [n.name for n in z.npcs] == ["Fen Lurker", "Swamp Ogre", "Ombassa", "Powderfuse Guard"]
    assert z.npcs[1].elite and z.npcs[2].boss and not z.npcs[3].hostile_to("horde")
    kinds = {o.name: (o.kind, o.skill) for o in z.objects}
    assert kinds == {
        "Dreamfoil": ("herb", None),  # recognised by its vanilla name
        "Glowcap Cluster": ("herb", 150),
        "Smuggler's Strongbox": ("chest", None),
        "Wanted Poster": ("other", None),
    }
    assert z.level == (38, 39)  # normal hostile mobs (not the elite, boss or guard)

    caps = tmp_path / "wowhead" / "captures"
    caps.mkdir(parents=True)
    (caps / f"zone-{RIVER}.json").write_text(json.dumps(ZONE_RECORD))
    assert load_zones(tmp_path)[RIVER].name == "Riverglades"


def test_zone_queue():
    zones = {RIVER: zone_from_capture(ZONE_RECORD, RIVER)}
    other = 16550
    queues = zone_queue(zones, {RIVER, other}, "horde")
    assert [q.name for q in queues] == ["zone 16550", "Riverglades"]
    assert [r.key for r in queues[0].rows] == [f"zone-{other}"]  # just its page, for now
    keys = [r.key for r in queues[1].rows]
    # zone page, then nodes (herbs before chests), then hostile mobs (normal before elite;
    # no boss, no guards)
    assert keys == [
        f"zone-{RIVER}",
        f"object-{GLOWCAP}",
        "object-990002",
        "npc-900001",
        "npc-900002",
    ]
    assert queues[1].rows[1].note == "herb, skill 150"
    assert queues[1].rows[4].note == "lvl 40–41, elite"


@pytest.fixture
def data(tmp_path):
    conn = v.connect(build_world(tmp_path / "mangos.sqlite"))
    yield VanillaData.load(conn)
    conn.close()


def test_forever_gathering_loop(data, book):
    zones = {RIVER: zone_from_capture(ZONE_RECORD, RIVER, herbs={"Dreamfoil"})}
    book.prices[GLOW_HERB] = _price(GLOW_HERB, market=20000, listed=500, name="Glowcap")
    # 20 Glowcap spawn points spread over the zone, and Dreamfoil (a vanilla node) seen there too
    pts = [MapPoint(RIVER, "Riverglades", 10 + 4 * i, 30 + (i % 5) * 8) for i in range(20)]
    entities = {
        ("object", GLOWCAP): Entity(EntityInfo("object", GLOWCAP, "Glowcap Cluster"), pts),
        ("object", 900): Entity(
            EntityInfo("object", 900, "Dreamfoil"),
            [MapPoint(RIVER, "Riverglades", 50, 50), MapPoint(361, "Felwood", 5, 5)],
        ),
    }
    unplaced = [
        ItemSource(GLOW_HERB, "herbalism", "object", GLOWCAP, "Glowcap Cluster", 2.0, 400, (RIVER,))
    ]
    herbalist = profile(professions={"herbalism": 300})

    (cand,) = forever_gathering(data, herbalist, book, entities, unplaced, {RIVER}, zones)
    m = cand.method
    assert m.name == "Herbalism in Riverglades" and m.zone == "Riverglades"
    assert m.requirements.professions == {"herbalism": 270}  # Dreamfoil's vanilla skill
    assert m.requirements.level == 38
    assert "assumed_respawn" in m.risk and f"{FOREVER_ZONE_YD:.0f}-yard" in m.description
    got = {i.item: i.qty for i in m.yields}
    assert got[GLOW_HERB] > 0 and got[HERB] > 0  # captured loot + vanilla node loot
    assert got[GLOW_HERB] > got[HERB]  # 20 Glowcap points vs one Dreamfoil (Felwood's ignored)

    # no herbalism, or too little skill for Glowcap (Dreamfoil alone is 270 anyway)
    assert forever_gathering(data, profile(), book, entities, unplaced, {RIVER}, zones) == []
    low = profile(professions={"herbalism": 100})
    assert forever_gathering(data, low, book, entities, unplaced, {RIVER}, zones) == []


def test_leads_take_levels_from_zone_pages(data, book):
    zones = {RIVER: zone_from_capture(ZONE_RECORD, RIVER)}
    book.prices[GLOW_HERB] = _price(GLOW_HERB, market=20000, listed=500, name="Glowcap")
    src = ItemSource(GLOW_HERB, "drop", "npc", 900001, "Fen Lurker", 0.2, 300, ())
    _, unplaced = data.add_sources([src])
    (lead,) = find_leads(
        data,
        unplaced,
        profile(level=40),
        book,
        PlanOptions(),
        zone_names={RIVER: "Riverglades"},
        zone_pages=zones,
    )
    assert lead.source.level == (38, 39) and lead.zones == ["Riverglades"]
    assert "level unknown" not in lead.flags


@pytest.fixture
def book():
    from wowgold.planner.pricing import PriceBook

    return PriceBook(house_ids=[1], prices={HERB: _price(HERB, market=30000, listed=500)})


def test_captured_listviews_use_entity_type_templates():
    """The userscript copies g_listviews, whose template is an object, not a name."""
    record = {
        "title": "Riverglades - Zone - WoW Forever",
        "listviews": {
            "npcs": {
                "template": {"entityType": 1, "columns": []},
                "data": [
                    {
                        "id": 1,
                        "name": "Frog",
                        "minlevel": 1,
                        "maxlevel": 1,
                        "react": [0, 0],
                        "type": 8,
                    },
                    {
                        "id": 2,
                        "name": "Abbot Schuncke",
                        "minlevel": 53,
                        "maxlevel": 53,
                        "react": [0, None],
                        "classification": 1,
                        "type": 7,
                    },
                    {
                        "id": 3,
                        "name": "Bog Beast",
                        "minlevel": 38,
                        "maxlevel": 40,
                        "react": [-1, -1],
                        "classification": 0,
                        "type": 1,
                    },
                ],
            },
            "objects": {
                "template": {"entityType": 2},
                "data": [{"id": 1623, "name": "Wild Steelbloom", "skill": 115, "type": -3}],
            },
            "drops": {"template": {"entityType": 3}, "data": [{"id": 9, "name": "Some Boots"}]},
        },
    }
    z = zone_from_capture(record, RIVER)
    assert [n.name for n in z.npcs] == ["Frog", "Bog Beast", "Abbot Schuncke"]
    assert z.npcs[0].critter and z.level == (38, 40)
    assert [(o.name, o.kind, o.skill) for o in z.objects] == [("Wild Steelbloom", "herb", 115)]
    (q,) = zone_queue({RIVER: z}, set(), "horde", level=40)
    assert [r.key for r in q.rows] == [f"zone-{RIVER}", "object-1623", "npc-3", "npc-2"]
