"""Recorded sessions: timeline, kill speed against the model, and calibration of discovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.test_discovery import CLOTH, build_world, profile
from wowgold import db
from wowgold.discovery import vanilla as v
from wowgold.discovery.discover import VanillaData, kills_per_hour, mob_camps
from wowgold.planner.pricing import PriceBook
from wowgold.play import calibration, recordings
from wowgold.play.digest import AFK_GAP, digest
from wowgold.play.recordings import Recording, parse_event

START = datetime(2026, 11, 12, 18, tzinfo=UTC)
OGRE = 1


@pytest.fixture
def data(tmp_path):
    conn = v.connect(build_world(tmp_path / "mangos.sqlite"))
    yield VanillaData.load(conn)
    conn.close()


def _rec(lines: list[str], rid="r1", cls="WARLOCK", level=52, end=None) -> Recording:
    evs = [parse_event(x) for x in lines]
    stop = START + timedelta(seconds=end if end is not None else evs[-1].t)
    char = {"class": cls, "level": level, "faction": "Horde"}
    return Recording(rid, START, stop, char, None, evs, "stop")


def _ogre_session(gap: int, n: int = 12, rid="r1") -> Recording:
    """n ogre kills, `gap` seconds apart, at two spots (respawns visible)."""
    lines = ["0|Z|Felwood||1448", "0|P|1448|40|50"]
    for i in range(n):
        x = 40 if i % 2 == 0 else 45
        lines.append(f"{(i + 1) * gap}|K|{OGRE}|{i:04X}|{x}|50")
    return _rec(lines, rid)


def test_timeline_pauses_and_afk():
    rec = _rec(
        [
            "0|Z|Felwood||1448",
            "10|C|1",
            "40|C|0",
            "100|U|pause",
            "700|U|resume",
            "800|P|1448|1|1",
            "2000|P|1448|2|2",  # 20 minutes of nothing: away
        ]
    )
    d = digest(rec)
    assert d.duration_s == 2000 and d.paused_s == 600 and d.combat_s == 30
    assert d.active_s == 100 + 100 + AFK_GAP
    assert d.zones == [("Felwood", 100 + 100 + AFK_GAP)]


def test_kill_speed_and_respawns(data):
    model = kills_per_hour(data.creatures[OGRE], 52, "warlock")  # 48/h -> 75 s per kill
    d = digest(_ogre_session(gap=100), data=data)
    assert d.kill_speed == pytest.approx((3600 / model) / 100)
    ogre = d.mobs[0]
    assert ogre.name == "Ogre" and ogre.kills == 12 and ogre.model_rate == model
    # each spot is killed every 200 s
    assert ogre.respawn_s == 200 and ogre.respawn_pairs == 10

    book = PriceBook(house_ids=[1])
    valued = digest(_ogre_session(gap=100), data=data, book=book)
    # coins (300 per kill) + the loot table's items at vendor price (no AH prices given)
    assert valued.expected_value > ogre.kills * 300 and valued.value == 0  # looted nothing


def test_calibration_feeds_discovery(tmp_path, data):
    conn = db.connect(tmp_path / "w.sqlite3")
    # two warlock sessions, slower than the model; one hidden session that would say faster
    recordings.store(conn, [_ogre_session(100, 40, "a"), _ogre_session(100, 40, "b")])
    recordings.store(conn, [_ogre_session(20, 40, "fast")])
    recordings.set_hidden(conn, "fast", True)
    cal = calibration.build(conn, data)
    raw = (3600 / kills_per_hour(data.creatures[OGRE], 52, "warlock")) / 100  # 0.75
    n = 2 * 39
    assert cal.sessions == 2
    assert cal.speed("warlock") == pytest.approx((raw * n + calibration.PRIOR_GAPS) / (n + 30))
    assert cal.speed("mage") == 1.0
    assert cal.respawns[OGRE].seconds == 200

    # discovery: slower kills, and the measured respawn instead of vanilla's 300-420 s
    slow = profile(kill_speed=cal.speed("warlock"))
    assert kills_per_hour(data.creatures[OGRE], 52, "warlock", slow.kill_speed) < 48
    data.respawn = {OGRE: 200.0}
    book = PriceBook(house_ids=[1])
    from tests.test_discovery import _price

    book.prices[CLOTH] = _price(CLOTH, market=10000, listed=1000, vendor_sell=100)
    (camp, *_) = mob_camps(data, slow, book)
    assert "measured in your sessions" in camp.method.description
    assert "kill_speed" not in slow.model_dump(by_alias=True)  # never saved with the profile


def test_sessions_pages(tmp_path):
    from fastapi.testclient import TestClient

    from wowgold.config import Settings
    from wowgold.web.app import create_app

    settings = Settings(
        wowgold_data_dir=tmp_path,
        wowgold_region="eu",
        wowgold_houses=[],
        wowgold_addon_file=tmp_path / "missing.lua",
    )
    conn = db.connect(settings.db_path)
    rec = _ogre_session(100, 12, "s1")
    rec.note = "ogres"
    recordings.store(conn, [rec])
    conn.close()
    client = TestClient(create_app(settings))
    page = client.get("/sessions").text
    assert "ogres" in page and 'href="/sessions/s1"' in page
    assert "WOWGOLD_ADDON_FILE is set but doesn't exist" in page
    detail = client.get("/sessions/s1").text
    assert "NPC 1" in detail and "Don&#39;t use for calibration" in detail  # no vanilla data
    assert client.get("/sessions/nope").status_code == 404
    client.post("/sessions/s1/hide", data={"hidden": "1"})
    assert "Use for calibration again" in client.get("/sessions/s1").text
    assert (
        client.post("/sessions/import", headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    r = client.post("/sessions/import", follow_redirects=False)
    assert r.status_code == 303 and "no%20addon%20file%20found" in r.headers["location"]
