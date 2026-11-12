"""Forever mobs from Wowhead map positions; capture history, outdated pages, new arrivals."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.test_discovery import _price, build_world, profile
from wowgold import db
from wowgold.config import Settings
from wowgold.discovery import vanilla as v
from wowgold.discovery.discover import FOREVER_RESPAWN_S, VanillaData, forever_camps
from wowgold.market.firstseen import new_arrivals
from wowgold.planner.pricing import PriceBook
from wowgold.web.app import create_app
from wowgold.wowhead.capture import get_token, previous_capture, save_capture
from wowgold.wowhead.entities import Entity, EntityInfo, MapPoint, entity_info, map_points
from wowgold.wowhead.refresh import (
    load_settings,
    mark_patch,
    page_changes,
    recapture_queue,
    stale_reason,
)
from wowgold.wowhead.sources import ItemSource

IMP, CRYSTAL = 777001, 248819
NPC_SCRIPT = (
    '$.extend(g_npcs[777001], {"classification":0,"displayName":"Fel Imp","id":777001,'
    '"location":[16591],"maxlevel":52,"minlevel":51,"name":"Fel Imp","react":[-1,-1],'
    "\"type\":3});\nnew Listview({template: 'item', id: 'drops', data: []});"
)


def test_entity_info_and_map_points():
    info = entity_info({"raw": [NPC_SCRIPT]}, "npc", IMP)
    assert info == EntityInfo("npc", IMP, "Fel Imp", (51, 52), (-1, -1), False, False, (16591,))
    assert not info.friendly_to("horde")
    # nested per-zone, per-floor dicts (as Wowhead's mapper data)
    nested = {
        "16591": {"0": {"count": 2, "coords": [[40.1, 50.2], [41, 51]], "uiMapName": "Riverglades"}}
    }
    pts = map_points({"mapper": nested})
    assert pts == [
        MapPoint(16591, "Riverglades", 40.1, 50.2),
        MapPoint(16591, "Riverglades", 41, 51),
    ]
    # list form, read from the raw page script
    raw = "var g_mapperData = {16591: [{count: 1, coords: [[10, 20]]}]};"
    assert map_points({"raw": [raw]}) == [MapPoint(16591, None, 10.0, 20.0)]


@pytest.fixture
def data(tmp_path):
    conn = v.connect(build_world(tmp_path / "mangos.sqlite"))
    yield VanillaData.load(conn)
    conn.close()


def test_forever_camp_from_positions(data):
    book = PriceBook(
        house_ids=[1],
        prices={CRYSTAL: _price(CRYSTAL, market=400000, listed=500, name="Fel Crystal")},
    )
    unplaced = [
        ItemSource(CRYSTAL, "drop", "npc", IMP, "Fel Imp", 0.05, 2000, (16591,), from_page=True)
    ]
    # 6 spawns close together, 2 far away
    points = [MapPoint(16591, "Riverglades", 40 + i * 0.5, 50) for i in range(6)]
    points += [MapPoint(16591, "Riverglades", 80, 10), MapPoint(16591, "Riverglades", 82, 12)]
    entities = {("npc", IMP): Entity(EntityInfo("npc", IMP, "Fel Imp", (51, 52), (-1, -1)), points)}
    (camp,) = forever_camps(data, profile(), book, entities, unplaced)
    m = camp.method
    assert m.name == "Fel Imp camp" and m.zone == "Riverglades"
    supply = 6 * 3600 / FOREVER_RESPAWN_S  # 60 kills/h of respawns; a level 52 warlock: 48
    kills = min(40 * 1.2, supply)
    assert {i.item: i.qty for i in m.yields}[CRYSTAL] == pytest.approx(kills * 0.05)
    assert "6 spawns close together, 8 on Wowhead's map" in m.description
    assert camp.verified == 1 and "assumed_respawn" in m.risk

    friendly = {("npc", IMP): Entity(EntityInfo("npc", IMP, "Fel Imp", (51, 52), (-1, 1)), points)}
    assert forever_camps(data, profile(), book, friendly, unplaced) == []  # friendly to horde
    no_points = {("npc", IMP): Entity(entities[("npc", IMP)].info, [])}
    assert forever_camps(data, profile(), book, no_points, unplaced) == []


def _npc_record(rows, at="x"):
    return {
        "url": "https://www.wowhead.com/forever/npc=5",
        "title": "Ogre - NPC - Forever",
        "listviews": {"drops": {"template": {"entityType": 3}, "data": rows}},
    }


def test_history_and_changes(tmp_path):
    row = lambda i, n, out=1000: {"id": i, "name": f"item{i}", "count": n, "outof": out}  # noqa: E731
    save_capture(tmp_path, _npc_record([row(1, 100), row(2, 50), row(3, 10)]))
    assert previous_capture(tmp_path, "npc-5") is None
    save_capture(tmp_path, _npc_record([row(1, 102), row(2, 200), row(4, 30)]))
    assert previous_capture(tmp_path, "npc-5") is not None
    pc = page_changes(tmp_path, "npc-5")
    got = {(c.kind, c.label) for c in pc.changes}
    assert got == {("rate", "item2"), ("new", "item4"), ("gone", "item3")}  # item1 barely moved


def test_outdated_and_queue(tmp_path):
    now = datetime(2026, 11, 30, tzinfo=UTC)
    settings = load_settings(tmp_path)
    assert settings["stale_days"] == 14
    assert stale_reason("2026-11-25T00:00:00+00:00", settings, now) is None
    assert stale_reason("2026-11-01T00:00:00+00:00", settings, now) == "29 days old"
    caps = tmp_path / "wowhead" / "captures"
    caps.mkdir(parents=True)
    old = "2026-11-01T00:00:00+00:00"
    (caps / "spells-professions-alchemy.json").write_text(json.dumps({"captured_at": old}))
    (caps / "item-7.json").write_text(json.dumps({"captured_at": old}))
    (caps / "npc-5.json").write_text(
        json.dumps({"captured_at": old, **_npc_record([{"id": 7, "count": 10, "outof": 100}])})
    )
    (caps / "item-8.json").write_text(json.dumps({"captured_at": "2026-11-29T00:00:00+00:00"}))
    queue = recapture_queue(tmp_path, lambda i: 1000.0 if i == 7 else 0.0, now=now)
    assert [r.key for r in queue] == ["spells-professions-alchemy", "item-7", "npc-5"]
    # small sample doubles the npc page: 0.1 × 1000 × 2
    assert queue[2].priority == pytest.approx(200)
    mark_patch(tmp_path, now)
    assert len(recapture_queue(tmp_path, lambda i: 1.0, now=now)) == 4  # item-8 too, now


def test_new_arrivals(tmp_path):
    conn = db.connect(tmp_path / "w.sqlite3")
    start = datetime(2026, 11, 11, 11, tzinfo=UTC)
    conn.execute(
        "INSERT INTO snapshots (region, ruleset_id, house_id, kind, fetched_at, "
        "http_status, outcome) VALUES ('eu', 1, 1, 'commodities', ?, 200, 'stored')",
        (start.isoformat(),),
    )
    for item, hours in ((1, 0), (2, 3), (3, 30)):
        conn.execute(
            "INSERT INTO price_stats (snapshot_id, region, house_id, kind, ts, item_id, variant, "
            "num_auctions, quantity, min_unit, p10_unit, p25_unit, median_unit, market_unit) "
            "VALUES (1, 'eu', 1, 'commodities', ?, ?, '', 1, 5, 100, 100, 100, 100, 100)",
            ((start + timedelta(hours=hours)).isoformat(), item),
        )
    now = start + timedelta(days=2)
    # 1 and 2 were there in the collector's first hours: not "new"
    assert set(new_arrivals(conn, "eu", now=now)) == {3}
    assert new_arrivals(conn, "eu", now=start + timedelta(days=20)) == {}


def test_capture_page_refresh_controls(tmp_path):
    client = TestClient(
        create_app(Settings(wowgold_data_dir=tmp_path, wowgold_region="eu", wowgold_houses=[]))
    )
    rec = {**_npc_record([{"id": 7, "count": 10, "outof": 100}]), "token": get_token(tmp_path)}
    assert client.post("/capture", json=rec).status_code == 200
    page = client.get("/capture").text
    assert "Outdated captures" in page and "Nothing outdated." in page
    assert "function isDone(tr)" in page
    bad = client.post(
        "/capture/refresh",
        data={"action": "mark-patch"},
        headers={"Origin": "https://evil.example"},
    )
    assert bad.status_code == 403
    client.post("/capture/refresh", data={"action": "mark-patch"})
    page = client.get("/capture").text
    assert 'data-key="npc-5" data-at=' in page and "captured before the last patch mark" in page
    client.post("/capture/refresh", data={"stale_days": "30"})
    assert load_settings(tmp_path)["stale_days"] == 30
