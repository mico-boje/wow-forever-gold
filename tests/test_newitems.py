"""New-in-Forever overview: names, Wowhead item lists, and the combined source list."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from tests.test_discovery import CLOTH, VIAL, _price, build_world
from wowgold import db
from wowgold.config import Settings
from wowgold.crafting.recipes import Recipe
from wowgold.discovery import vanilla as v
from wowgold.discovery.discover import VanillaData
from wowgold.discovery.newitems import build_overview
from wowgold.planner.pricing import PriceBook
from wowgold.web.app import create_app
from wowgold.wowhead.capture import page_key, userscript
from wowgold.wowhead.itemlists import hints_from_capture, load_item_hints
from wowgold.wowhead.names import (
    TOOLTIP_URL,
    ItemName,
    captured_item_names,
    fetch_tooltip_names,
    parse_tooltip,
    store_names,
    unnamed_item_ids,
)
from wowgold.wowhead.sources import ItemSource
from wowgold.wowhead.vendors import VendorOffer

FEL, SCALE, DYE, BAR, MYSTERY = 248819, 279385, 249430, 251291, 287013


def _write(tmp_path, key, record):
    caps = tmp_path / "wowhead" / "captures"
    caps.mkdir(parents=True, exist_ok=True)
    (caps / f"{key}.json").write_text(json.dumps(record))


def test_names_from_captures(tmp_path):
    _write(tmp_path, f"item-{FEL}", {"title": "Fel Crystal - Item - Forever"})
    _write(
        tmp_path,
        "npc-1044",
        {
            "listviews": {
                "drops": {
                    "data": [
                        {
                            "id": SCALE,
                            "name": "1Pristine Crimson Scale",
                            "classs": 7,
                            "count": 1,
                            "outof": 4,
                        }
                    ]
                }
            }
        },
    )
    _write(
        tmp_path,
        "spells-professions-alchemy",
        {
            "listviews": {
                "spells": {
                    "data": [
                        {"id": 1, "name": "Magenta Dye", "creates": [DYE, 1, 1]},
                        {"id": 2, "name": "Transmute: Legionite", "creates": [9, 1, 1]},
                        {"id": 3, "name": "Smelt Heavy Thorium", "creates": [BAR, 1, 1]},
                    ]
                }
            }
        },
    )
    names = captured_item_names(tmp_path)
    assert names[FEL].name == "Fel Crystal"
    assert names[SCALE] == ItemName("Pristine Crimson Scale", "COMMON")
    assert names[DYE].name == "Magenta Dye"
    assert 9 not in names and BAR not in names  # recipe actions aren't item names


def test_store_names_never_overwrites(tmp_path):
    conn = db.connect(tmp_path / "w.sqlite3")
    conn.execute(
        "INSERT INTO items (item_id, name, source, fetched_at) "
        "VALUES (1, 'Blizzard Name', 'blizzard', 'x')"
    )
    conn.execute("INSERT INTO items (item_id, source, fetched_at) VALUES (2, 'missing', 'x')")
    assert store_names(conn, {1: ItemName("Other"), 2: ItemName("Found"), 3: ItemName("New")}) == 2
    got = dict(conn.execute("SELECT item_id, name FROM items").fetchall())
    assert got == {1: "Blizzard Name", 2: "Found", 3: "New"}
    assert unnamed_item_ids(conn, {1, 2, 3, 4}) == [4]


def test_tooltip_parsing_and_fetching(tmp_path):
    payload = {
        "name": "Pristine Leather",
        "quality": 2,
        "tooltip": 'Sell Price: <span class="moneysilver">1</span> '
        '<span class="moneycopper">25</span>',
    }
    assert parse_tooltip(payload) == ItemName("Pristine Leather", "UNCOMMON", 125)
    conn = db.connect(tmp_path / "w.sqlite3")
    with respx.mock(assert_all_called=False) as router:
        router.get(TOOLTIP_URL.format(item_id=1)).mock(
            return_value=httpx.Response(200, json=payload)
        )
        router.get(TOOLTIP_URL.format(item_id=2)).mock(return_value=httpx.Response(404, json={}))
        router.get(TOOLTIP_URL.format(item_id=3)).mock(return_value=httpx.Response(429))
        router.get(TOOLTIP_URL.format(item_id=4)).mock(
            return_value=httpx.Response(200, json=payload)
        )
        found, missing = fetch_tooltip_names(conn, [1, 2, 3, 4], delay_s=0)
    assert (found, missing) == (1, 1)  # stopped at the 429, never asked for 4
    rows = dict(conn.execute("SELECT item_id, source FROM items").fetchall())
    assert rows == {1: "wowhead", 2: "wowhead-missing"}
    assert unnamed_item_ids(conn, {1, 2, 3}) == [3]  # 2 isn't asked again


def test_item_list_pages(tmp_path):
    base = "https://www.wowhead.com/forever/items"
    assert page_key(f"{base}/trade-goods") == "items-trade-goods"
    assert page_key(f"{base}/trade-goods#50") == "items-trade-goods"
    assert page_key(f"{base}?filter=166;12;0").startswith("items-all-")
    assert "@match        https://www.wowhead.com/forever/items*" in userscript(8765, "t")

    record = {
        "raw": [
            'var listviewitems = [{"id":279385,"name":"1Pristine Crimson Scale","classs":7,'
            '"source":[2],"sourcemore":[{"n":"Flamesnorting Whelp","t":1,"ti":1044}]},'
            '{"id":287013,"name":"3Mystery Plate","classs":4,"source":[5]}];'
            "new Listview({template: 'item', id: 'items', data: listviewitems});"
        ]
    }
    hints = {h.item_id: h for h in hints_from_capture(record)}
    scale = hints[SCALE]
    assert scale.name == "Pristine Crimson Scale" and scale.quality == 1
    assert [(r.kind, r.type, r.ref_id, r.name) for r in scale.refs] == [
        ("drop", "npc", 1044, "Flamesnorting Whelp")
    ]
    assert hints[MYSTERY].kinds == ["vendor"] and hints[MYSTERY].refs == []
    _write(tmp_path, "items-trade-goods", record)
    assert set(load_item_hints(tmp_path)) == {SCALE, MYSTERY}


@pytest.fixture
def world(tmp_path):
    conn = v.connect(build_world(tmp_path / "mangos.sqlite"))
    yield conn
    conn.close()


def test_overview_combines_every_source(world):
    from wowgold.wowhead.itemlists import ItemHint, SourceRef

    data = VanillaData.load(world)
    book = PriceBook(
        house_ids=[1],
        prices={
            FEL: _price(FEL, market=440000, listed=10, name="Fel Crystal"),
            SCALE: _price(SCALE, market=20000, listed=200, name="Pristine Crimson Scale"),
            CLOTH: _price(CLOTH, market=10000, listed=100, name="Runecloth"),
            MYSTERY: _price(MYSTERY, market=900000, listed=4),
        },
    )
    placed = [ItemSource(SCALE, "drop", "npc", 1, "Ogre", 0.24, 3000, (), from_page=True)]
    unplaced = [ItemSource(FEL, "drop", "npc", 999, "Fel Imp", 0.05, 200, (16,))]
    recipes = [
        Recipe(
            1,
            "Magenta Dye",
            "alchemy",
            215,
            [(SCALE, 2.0), (VIAL, 1.0)],
            DYE,
            1.0,
            source="wowhead",
        )
    ]
    hints = {
        MYSTERY: ItemHint(
            MYSTERY,
            "Mystery Plate",
            3,
            None,
            ["drop"],
            [SourceRef("drop", "npc", 4242, "Plate Golem")],
        )
    }
    o = build_overview(
        data,
        book,
        ah_items={FEL, SCALE, CLOTH, MYSTERY},
        placed=placed,
        unplaced=unplaced,
        recipes=recipes,
        offers={FEL: VendorOffer(FEL, 450000, "Mister Graphed", 1, False)},
        hints=hints,
        names={DYE: "Magenta Dye"},
        captured=set(),
        yours={SCALE: [("Creature", 1, 10, 3)]},
    )
    by = {it.item_id: it for it in o.items}
    assert CLOTH not in by  # vanilla item
    assert by[SCALE].status == "camp" and by[SCALE].used_in == 1
    assert {s.kind for s in by[SCALE].sources} == {"camp", "yours"}
    assert by[FEL].status == "lead" and any(s.kind == "vendor" for s in by[FEL].sources)
    assert by[DYE].status == "crafted" and by[DYE].name == "Magenta Dye"
    assert by[MYSTERY].status == "listed" and by[MYSTERY].name == "Mystery Plate"
    assert [p.key for p in o.to_capture] == ["npc-4242"]
    assert o.new_drops[0].mob == "Ogre" and o.new_drops[0].chance == 0.24
    assert o.items[0].item_id == FEL  # most market value per day first


def test_new_page(tmp_path):
    (tmp_path / "vanilla").mkdir()
    build_world(tmp_path / "vanilla" / "mangos.sqlite")
    client = TestClient(
        create_app(Settings(wowgold_data_dir=tmp_path, wowgold_region="eu", wowgold_houses=[]))
    )
    assert "Pick a faction" in client.get("/new").text
    page = client.get("/new?faction=horde").text
    assert 'class="on">New in Forever' in page and "no auction data" in page
    bad = client.post(
        "/new/fetch-names", data={"faction": "horde"}, headers={"Origin": "https://evil.example"}
    )
    assert bad.status_code == 403
