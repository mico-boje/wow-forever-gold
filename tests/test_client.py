from __future__ import annotations

import httpx

from tests.conftest import BASE
from wowgold.blizzard import namespaces
from wowgold.blizzard.forever import parse_auction_index, parse_rulesets


def test_namespaces():
    assert namespaces.dynamic("eu") == "dynamic-forever-eu"
    assert namespaces.static("us") == "static-forever-us"
    assert namespaces.static("us", "1.60.1_70399") == "static-1.60.1_70399-forever-us"
    assert namespaces.profile("kr") == "profile-forever-kr"


def test_token_is_cached_and_sent(client, mock_api):
    route = mock_api.get(f"{BASE}/data/wow/region/index").mock(
        return_value=httpx.Response(200, json={"regions": []})
    )
    client.get("/data/wow/region/index", "dynamic-forever-eu")
    client.get("/data/wow/region/index", "dynamic-forever-eu")
    assert mock_api.routes[0].call_count == 1  # one token request
    req = route.calls.last.request
    assert req.headers["Authorization"] == "Bearer tok"
    assert req.url.params["namespace"] == "dynamic-forever-eu"
    assert req.url.params["locale"] == "en_US"


def test_retries_on_429_then_succeeds(client, mock_api):
    route = mock_api.get(f"{BASE}/x").mock(
        side_effect=[httpx.Response(429), httpx.Response(503), httpx.Response(200, json={"ok": 1})]
    )
    resp = client.get("/x", "ns")
    assert resp.ok and resp.json() == {"ok": 1}
    assert route.call_count == 3


def test_404_is_returned_not_raised(client, mock_api):
    mock_api.get(f"{BASE}/data/wow/realm/index").mock(return_value=httpx.Response(404))
    resp = client.get("/data/wow/realm/index", "dynamic-forever-eu")
    assert resp.status == 404 and not resp.ok


def test_parse_rulesets_handles_name_dicts():
    payload = {"rulesets": [{"id": 1, "name": {"en_US": "PvP"}}, {"id": 2, "name": "Normal"}]}
    assert [(r.id, r.name) for r in parse_rulesets(payload)] == [(1, "PvP"), (2, "Normal")]


def test_parse_auction_index_from_hrefs_and_fallback():
    payload = {
        "auctions": [
            {
                "key": {"href": f"{BASE}/data/wow/ruleset/1/auctions/113?namespace=x"},
                "name": "Horde",
            },
            {"key": {"href": f"{BASE}/data/wow/ruleset/1/auctions/110"}, "name": "Alliance"},
        ]
    }
    assert [(h.id, h.name) for h in parse_auction_index(payload)] == [
        (110, "Alliance"),
        (113, "Horde"),
    ]
    assert [h.id for h in parse_auction_index({"auctions": [{"id": 7, "name": "X"}]})] == [7]


def test_auction_index_falls_back_between_paths(api, forever_routes):
    houses, resp = api.auction_index(1)
    assert [h.id for h in houses] == [110, 112]
    assert resp.url.split("?")[0].endswith("/auctions/")


def test_ruleset_names_resolved_from_detail(api, mock_api):
    mock_api.get(f"{BASE}/data/wow/ruleset/index").mock(
        return_value=httpx.Response(200, json={"rulesets": [{"id": 15}, {"id": 1}]})
    )
    mock_api.get(f"{BASE}/data/wow/ruleset/1").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "PvP"})
    )
    mock_api.get(f"{BASE}/data/wow/ruleset/15").mock(return_value=httpx.Response(404))
    assert [(r.id, r.name) for r in api.rulesets()] == [(1, "PvP"), (15, "15")]


def test_faction_from_house_name():
    from wowgold.blizzard.forever import faction_from_house_name

    assert faction_from_house_name("Alliance Auction House") == "alliance"
    assert faction_from_house_name("Blackwater Auction House") == "blackwater"
    assert faction_from_house_name(None) is None
    assert faction_from_house_name("Neutral") is None
