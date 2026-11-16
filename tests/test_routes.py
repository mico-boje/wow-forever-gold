"""Farming routes: loop ordering, routes on farms, Routes.lua, and the addon's RXP guides."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from tests.test_discovery import build_world, profile
from wowgold.discovery import vanilla as v
from wowgold.discovery.discover import VanillaData, forever_camps, gathering_zones, mob_camps
from wowgold.discovery.routes import make_route, order_loop
from wowgold.ingame import add_route, load_routes, remove_route, routes_lua, write_routes

ADDON = Path(__file__).parent.parent / "addon" / "WowGoldLogger"


def _length(tour):
    return sum(math.dist(tour[i], tour[(i + 1) % len(tour)]) for i in range(len(tour)))


def test_loop_order_and_merging():
    # points on a circle, shuffled: the loop should go round it, not zig-zag
    circle = [(math.cos(a) * 100, math.sin(a) * 100) for a in [i * math.pi / 8 for i in range(16)]]
    shuffled = circle[::3] + circle[1::3] + circle[2::3]
    tour = order_loop(shuffled)
    assert sorted(tour) == sorted(shuffled)
    assert _length(tour) == pytest.approx(_length(circle), rel=0.01)

    # spawns closer than 30 yards are one stop; big routes are thinned to 60 stops
    r = make_route("world", 1, [(0, 0), (5, 5), (200, 0)])
    assert len(r.points) == 2 and r.kind == "world" and r.map_id == 1
    many = make_route("map", 2548, [(i % 50, i // 50) for i in range(2500)])
    assert len(many.points) <= 60
    assert r.to_dict()["points"][0] == [round(r.points[0][0], 1), round(r.points[0][1], 1)]


@pytest.fixture
def data(tmp_path):
    conn = v.connect(build_world(tmp_path / "mangos.sqlite"))
    yield VanillaData.load(conn)
    conn.close()


def test_farms_carry_routes(data, tmp_path):
    from tests.test_discovery import HERB, _price
    from wowgold.planner.pricing import PriceBook
    from wowgold.wowhead.entities import Entity, EntityInfo, MapPoint
    from wowgold.wowhead.sources import ItemSource

    book = PriceBook(house_ids=[1], prices={HERB: _price(HERB, market=30000, listed=500)})
    # vanilla camp: the 4 ogre spawns (20 yards apart -> merged into 2-3 stops) on Kalimdor
    from tests.test_discovery import CLOTH

    book.prices[CLOTH] = _price(CLOTH, market=10000, listed=1000, vendor_sell=100)
    (camp, *_) = mob_camps(data, profile(), book)
    assert camp.route.kind == "world" and camp.route.map_id == 1
    assert camp.route.mobs == ["Ogre"]
    assert all(1000 <= x <= 1060 and y == 1000 for x, y in camp.route.points)

    # vanilla gathering zone: the herb spawn points
    herbs = gathering_zones(data, profile(professions={"herbalism": 300}), book)
    assert all(c.route and c.route.kind == "world" for c in herbs)

    # Forever camp: Wowhead positions with the game's map id
    pts = [MapPoint(16591, "Riverglades", 40 + i * 0.5, 50, 2548) for i in range(6)]
    ents = {("npc", 777001): Entity(EntityInfo("npc", 777001, "Imp", (51, 52), (-1, -1)), pts)}
    src = ItemSource(HERB, "drop", "npc", 777001, "Imp", 0.5, 2000, (16591,), from_page=True)
    (fc,) = forever_camps(data, profile(), book, ents, [src])
    assert fc.route.kind == "map" and fc.route.map_id == 2548 and len(fc.route.points) >= 2
    assert fc.route.mobs == ["Imp"]


def test_routes_file(tmp_path):
    route = {
        "kind": "map",
        "map": 2548,
        "range": 25,
        "mobs": ["Bog Beast", "Swamp Ogre"],
        "points": [[59.4, 53.8], [60.1, 55]],
    }
    add_route(tmp_path, "hyp_a", 'Bog "Beast" camp', "Riverglades", "note", route)
    add_route(tmp_path, "hyp_b", "Other", "Desolace", "", {**route, "kind": "world", "map": 1})
    add_route(tmp_path, "hyp_a", "Bog Beast camp", "Riverglades", "again", route)  # replaces
    assert [r["id"] for r in load_routes(tmp_path)] == ["hyp_a", "hyp_b"]
    with pytest.raises(ValueError):
        add_route(tmp_path, "bad", "x", "", "", {"kind": "map", "map": "2548", "points": []})

    lua = routes_lua(load_routes(tmp_path))
    assert 'name = "Bog Beast camp"' in lua and "points = { 59.4,53.8,60.1,55 }" in lua
    assert 'mobs = { "Bog Beast", "Swamp Ogre" }' in lua
    with pytest.raises(ValueError):
        add_route(tmp_path, "bad", "x", "", "", {**route, "mobs": "Bog Beast"})
    addon = tmp_path / "AddOns" / "WowGoldLogger"
    addon.mkdir(parents=True)
    assert write_routes(tmp_path, addon) == [addon / "Routes.lua"]
    assert (addon / "Routes.lua").read_text() == lua
    remove_route(tmp_path, "hyp_b")
    assert [r["id"] for r in load_routes(tmp_path)] == ["hyp_a"]


def test_send_route_from_farms_page(tmp_path):
    from fastapi.testclient import TestClient

    from wowgold.config import Settings
    from wowgold.web.app import create_app

    addon = tmp_path / "WowGoldLogger"
    addon.mkdir()
    settings = Settings(
        wowgold_data_dir=tmp_path,
        wowgold_region="eu",
        wowgold_houses=[],
        wowgold_addon_dir=addon,
    )
    client = TestClient(create_app(settings))
    form = {
        "id": "hyp_x",
        "name": "Ogre camp",
        "zone": "Felwood",
        "note": "Ogre camp: 5g/h",
        "item": "4306",
        "route": json.dumps(
            {"kind": "world", "map": 1, "range": 25, "points": [[1000, 1000], [1040, 1000]]}
        ),
    }
    r = client.post(
        "/farms/route",
        data=form,
        headers={"Referer": "http://testserver/farms?cls=mage"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"].startswith("/farms?cls=mage&route=sent")
    text = (addon / "Routes.lua").read_text()
    assert 'id = "hyp_x"' in text and "item = 4306" in text
    page = client.get(r.headers["location"]).text
    assert 'Sent "Ogre camp" to the game' in page and "Routes in the game" in page
    bad = client.post("/farms/route", data={**form, "route": "nope"}, follow_redirects=False)
    assert "route=error" in bad.headers["location"]
    client.post("/farms/route/remove", data={"id": "hyp_x"})
    assert 'id = "hyp_x"' not in (addon / "Routes.lua").read_text()


ROUTE_STUBS = r"""
DEFAULT_CHAT_FRAME = { AddMessage = function(self, m) table.insert(messages, m) end }
messages = {}
handlers = {}
function CreateFrame()
    local f = { events = {} }
    function f:RegisterEvent(e) self.events[e] = true end
    function f:SetScript(n, fn) handlers[#handlers + 1] = fn end
    return f
end
-- Kalimdor (1414) is 10000 yards across from (0, 0); Felwood (1448) is the 1000x1000-yard
-- square at (500, 500). Without a target map the game answers with the continent map.
function CreateVector2D(x, y) return { x = x, y = y, GetXY = function(s) return s.x, s.y end } end
Enum = { UIMapType = { Zone = 3 } }
C_Map = {
  GetMapInfo = function(id)
    return { name = ({ [2548] = "Riverglades", [1448] = "Felwood", [1414] = "Kalimdor" })[id] }
  end,
  GetMapChildrenInfo = function(id) return { { mapID = 1448, name = "Felwood" } } end,
  GetMapInfoAtPosition = function(id, x, y) return { mapID = 1448 } end,
  GetMapPosFromWorldPos = function(continent, pos, override)
    if continent ~= 1 then return nil end
    if override == 1448 then
      return 1448, CreateVector2D((pos.x - 500) / 1000, (pos.y - 500) / 1000)
    end
    return 1414, CreateVector2D(pos.x / 10000, pos.y / 10000)
  end,
}
-- HereBeDragons, as bundled with RXPGuides: Felwood's map spans world y 1500..500 (its "x",
-- west to east) and world x 1500..500 (its "y", north to south)
LibStub = function(name)
  if name ~= "HereBeDragons-2.0" then return nil end
  return { GetZoneCoordinatesFromWorld = function(self, x, y, zone)
    if zone ~= 1448 then return nil end
    return (1500 - x) / 1000, (1500 - y) / 1000
  end }
end
registered = {}
RXPGuides = { RegisterGuide = function(text) table.insert(registered, text) end }
WowGoldLogger = {}
"""


def test_addon_registers_rxp_loops():
    lupa = pytest.importorskip("lupa.lua51")
    rt = lupa.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(ROUTE_STUBS)
    rt.execute(
        routes_lua(
            [
                {
                    "id": "fc",
                    "name": "Bog Beast camp",
                    "zone": "Riverglades",
                    "note": "5g/h",
                    "kind": "map",
                    "map": 2548,
                    "range": 25,
                    "item": 4306,
                    "mobs": ["Bog Beast", "Mire, Lord"],
                    "points": [[59.4, 53.8], [60.1, 55.0]],
                },
                {
                    "id": "vc",
                    "name": "Ogre camp",
                    "zone": "Felwood",
                    "note": "",
                    "kind": "world",
                    "map": 1,
                    "range": 25,
                    "points": [[1000, 1000], [1040, 1000], [9000, 9000]],
                },  # last: off the map
                {
                    "id": "ek",
                    "name": "Nowhere",
                    "zone": "?",
                    "note": "",
                    "kind": "world",
                    "map": 0,
                    "range": 25,
                    "points": [[1, 1]],
                },
            ]
        )
    )
    rt.execute((ADDON / "WowGoldLoggerRoutes.lua").read_text())
    g = rt.globals()
    guides = [g.registered[i] for i in range(1, len(g.registered) + 1)]
    assert guides[0] == (
        "#group WowGold farms\n#name WowGold: Bog Beast camp (fc)\n"
        "#displayname WowGold: Bog Beast camp\nstep\n"
        "    .openmap 2548 >>The route is drawn on this map\n"
        "    .mob Bog Beast >>Kill Bog Beast, Mire Lord\n"
        "    .mob Mire Lord\n"
        "    .collect 4306,9999\n"
        "    .loop 25,2548,59.4,53.8,60.1,55 >>5g/h"
    )
    # world (1000, 1000) -> 50/50; (1040, 1000) is 40 yards further north: y 46
    assert guides[1].endswith(
        "    .xp 61 >>Farm the route\n    .loop 25,1448,50,50,50,46 >>Ogre camp"
    )
    assert len(guides) == 2  # the route that can't be placed is skipped
    status = g.WowGoldLogger.routes.status
    assert status["fc"] == "on the Riverglades map (2548), 2 stops, first at 59.4, 53.8"
    assert "couldn't place" in status["ek"]


def test_world_route_lands_on_its_zone_map_even_without_a_name_match():
    lupa = pytest.importorskip("lupa.lua51")
    rt = lupa.LuaRuntime(unpack_returned_tuples=True)
    rt.execute(ROUTE_STUBS)
    rt.execute("WowGoldLoggerRoutes = {}")
    rt.execute((ADDON / "WowGoldLoggerRoutes.lua").read_text())
    route = rt.eval(
        '{ id = "x", name = "x", zone = "Somewhere Else", kind = "world", map = 1,'
        " points = { 1000, 1000, 1040, 1000 } }"
    )
    mapcoords = rt.globals().WowGoldLogger.routes.mapCoords
    map_id, coords = mapcoords(route)
    assert map_id == 1448  # the zone under the route's centre, not Kalimdor (1414)
    assert [coords[i] for i in range(1, len(coords) + 1)] == [50, 50, 50, 46]
