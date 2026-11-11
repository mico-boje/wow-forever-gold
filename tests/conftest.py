from __future__ import annotations

import httpx
import pytest
import respx

from wowgold.blizzard import BlizzardClient, ForeverApi

BASE = "https://eu.api.blizzard.com"


def house_ref(rid: int, hid: int, name: str) -> dict:
    return {
        "key": {
            "href": f"{BASE}/data/wow/ruleset/{rid}/auctions/{hid}?namespace=dynamic-forever-eu"
        },
        "name": name,
        "id": hid,
    }


@pytest.fixture
def mock_api():
    with respx.mock(assert_all_called=False) as router:
        router.post("https://oauth.battle.net/token").mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 86399})
        )
        yield router


@pytest.fixture
def client(mock_api) -> BlizzardClient:
    c = BlizzardClient("eu", "id", "secret", backoff_s=0)
    yield c
    c.close()


@pytest.fixture
def api(client) -> ForeverApi:
    return ForeverApi(client)


@pytest.fixture
def forever_routes(mock_api):
    """Two rulesets that list the same two houses; house 112 is empty."""
    mock_api.get(f"{BASE}/data/wow/ruleset/index").mock(
        return_value=httpx.Response(
            200,
            json={"rulesets": [{"id": 1, "name": "PvP"}, {"id": 2, "name": "Normal"}]},
        )
    )
    for rid in (1, 2):
        # Only the trailing-slash variant works here; /index 404s.
        mock_api.get(f"{BASE}/data/wow/ruleset/{rid}/auctions/").mock(
            return_value=httpx.Response(
                200,
                json={
                    "auctions": [house_ref(rid, 110, "Alliance"), house_ref(rid, 112, "Blackwater")]
                },
            )
        )
        mock_api.get(f"{BASE}/data/wow/ruleset/{rid}/auctions/index").mock(
            return_value=httpx.Response(404)
        )
    mock_api.get(f"{BASE}/data/wow/ruleset/1/auctions/110").mock(
        return_value=httpx.Response(
            200,
            json={"auctions": [{"id": 1, "item": {"id": 2589}, "buyout": 1200, "quantity": 20}]},
            headers={"Last-Modified": "Tue, 11 Nov 2026 10:00:00 GMT"},
        )
    )
    mock_api.get(f"{BASE}/data/wow/ruleset/1/auctions/110/commodities").mock(
        return_value=httpx.Response(200, json={"auctions": []})
    )
    mock_api.get(f"{BASE}/data/wow/ruleset/1/auctions/112").mock(
        return_value=httpx.Response(200, json={"id": 112, "name": "Blackwater"})
    )
    mock_api.get(f"{BASE}/data/wow/ruleset/1/auctions/112/commodities").mock(
        return_value=httpx.Response(200, json={"id": 112})
    )
    return mock_api
