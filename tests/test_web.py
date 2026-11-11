"""The local web UI, against a synthetic world DB and an empty auction database."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.test_discovery import build_world
from wowgold.config import Settings
from wowgold.methods.models import Method
from wowgold.sessions import load_profile
from wowgold.web.app import FarmForm, create_app, gold
from wowgold.wowhead.capture import get_token

FARM = "submitted=1&cls=warlock&faction=horde&level=52&house=1"


@pytest.fixture
def env(tmp_path):
    (tmp_path / "vanilla").mkdir()
    build_world(tmp_path / "vanilla" / "mangos.sqlite")
    settings = Settings(wowgold_data_dir=tmp_path, wowgold_region="eu", wowgold_houses=[])
    return TestClient(create_app(settings)), tmp_path


def test_farms_intro_without_profile(env):
    client, _ = env
    page = client.get("/farms").text
    assert "Find farms" in page and "Ogre camp" not in page
    assert client.get("/", follow_redirects=False).headers["location"] == "/farms"


def test_farms_results_and_details(env):
    client, _ = env
    page = client.get(f"/farms?{FARM}").text
    assert "Farms for a level 52 warlock" in page
    assert "Ogre camp" in page and "Felwood" in page
    assert "download as YAML" in page


def test_sweep(env):
    client, _ = env
    page = client.get(f"/farms?{FARM}&levels=20-52&step=32").text
    assert "Best farms while leveling" in page
    assert "Level 20" in page and "Level 52" in page
    assert 'class="chart"' in page


def test_form_errors(env):
    client, _ = env
    page = client.get("/farms?submitted=1&cls=warlock&faction=horde&level=abc&house=1").text
    assert "level: not a number" in page
    page = client.get(f"/farms?{FARM}&levels=50-70").text
    assert "levels must be between 1 and 60" in page


def test_save_profile(env):
    client, data_dir = env
    form = {
        "submitted": "1",
        "cls": "rogue",
        "faction": "alliance",
        "level": "40",
        "gear": "leveling",
        "prof_skinning": "200",
    }
    bad = client.post("/profile", data=form, headers={"Origin": "https://evil.example"})
    assert bad.status_code == 403
    ok = client.post("/profile", data=form, follow_redirects=False)
    assert ok.status_code == 303 and "saved=1" in ok.headers["location"]
    saved = load_profile(data_dir / "profile.json")
    assert saved.cls == "rogue" and saved.professions == {"skinning": 200}
    # the saved profile pre-fills the form and runs straight away (this test DB has no
    # auction houses, so it stops at "no auction data")
    page = client.get("/farms").text
    assert '<option value="rogue" selected>' in page
    assert "no auction data for that faction" in page


def test_yaml_download(env):
    client, _ = env
    resp = client.get(f"/farms/hypotheses.yaml?{FARM}")
    assert resp.status_code == 200
    entries = yaml.safe_load(resp.text)
    assert entries and all(Method.model_validate(e) for e in entries)


def test_capture_endpoints(env):
    client, data_dir = env
    assert "Wowhead capture" in client.get("/capture").text
    script = client.get("/wowgold-capture.user.js").text
    token = get_token(data_dir)
    assert token in script and "/capture" in script
    url = "https://www.wowhead.com/forever/item=248819"
    assert client.post("/capture", json={"token": "nope", "url": url}).status_code == 403
    resp = client.post("/capture", json={"token": token, "url": url, "listviews": {}})
    assert resp.json() == {"saved": "item-248819"}
    assert "item-248819" in client.get("/capture/status").json()


def test_status_page(env):
    client, _ = env
    page = client.get("/status").text
    assert "No auction data collected yet" in page
    assert "db-sqlite" not in page  # no VERSION file in the synthetic setup


def test_helpers():
    assert (
        gold(1234567) == "123g"
        and gold(123456) == "12.3g"
        and gold(250) == "2s"
        and gold(None) == "-"
    )
    form = FarmForm.from_request(
        {"submitted": "1", "cls": "mage", "faction": "horde", "level": "30", "prof_mining": "999"},
        None,
    )
    assert form.professions == {"mining": 300}
    assert "prof_mining=300" in form.query()


def test_verify_flow(env):
    client, data_dir = env
    farms = client.get(f"/farms?{FARM}").text
    assert 'href="/capture?' in farms and "Verify these farms on Wowhead" in farms

    todo = client.get(f"/capture?{FARM}").text
    assert "Verify farms on Wowhead" in todo
    assert 'data-key="npc-1"' in todo and "forever/npc=1" in todo
    assert "Ogre camp" in todo  # which farm the page verifies

    record = {
        "token": get_token(data_dir),
        "url": "https://www.wowhead.com/forever/npc=1",
        "title": "Ogre - NPC - Forever",
        "listviews": {
            "drops": {
                "template": {"entityType": 3},
                "data": [{"id": 100, "count": 500, "outof": 1000}],
            }
        },
    }
    assert client.post("/capture", json=record).json() == {"saved": "npc-1"}
    assert "npc-1" in client.get("/capture/status").json()
    assert ">verified</span>" in client.get(f"/farms?{FARM}").text


def test_sell_rates():
    from wowgold.planner.pricing import ItemPrice, PriceBook
    from wowgold.web.app import sell_rate

    book = PriceBook(
        house_ids=[1],
        prices={
            1: ItemPrice(1, "Herb", 30000, 400, 100, None, 120.0, 120.0, True),
            2: ItemPrice(2, "Rare", 900000, 3, 100, None, None, 0.75, False),
            3: ItemPrice(3, "Bone", None, 0, 50, None, None, 0.0, False),
        },
    )
    herb, rare, bone = sell_rate(book, 1), sell_rate(book, 2), sell_rate(book, 3)
    assert herb.label() == "120/day" and herb.days_to_sell(60) == pytest.approx(0.5)
    assert rare.label() == "~0.8/day" and rare.days_to_sell(3) == pytest.approx(4)
    assert bone.label() == "not on AH" and bone.days_to_sell(10) is None


def test_farm_details_show_sell_rates(env):
    client, _ = env
    page = client.get(f"/farms?{FARM}").text
    assert '<th class="num">Sells</th>' in page
    assert "Sells = units sold per day on your auction house" in page
