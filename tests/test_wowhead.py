"""Wowhead capture server and parsing of captured pages."""

from __future__ import annotations

import pytest

from wowgold.wowhead.capture import (
    captured,
    get_token,
    save_capture,
    userscript,
)
from wowgold.wowhead.sources import load_sources, parse_raw_listviews, sources_from_capture

FEL_CRYSTAL = 248819

LISTVIEWS = {
    "dropped-by": {
        "template": "npc",
        "data": [
            {
                "id": 6115,
                "name": "Roaming Felguard",
                "minlevel": 51,
                "maxlevel": 52,
                "location": [361],
                "count": 42,
                "outof": 1000,
            },
            {"id": 999999, "name": "New Forever Demon", "location": [16], "count": 5, "outof": 50},
        ],
    },
    "mined-from-object": {
        "template": "object",
        "data": [{"id": 175404, "name": "Rich Thorium Vein", "count": 30, "outof": 600}],
    },
    "comments": {"template": "comment", "data": [{"id": 1, "body": "nice"}]},
}

RAW = """
var x = 1;
new Listview({template: 'npc', id: 'skinned-from', name: WH.TERMS.skinnedfrom,
  extraCols: [Listview.extraCols.count], sort: ['-percent'],
  data: [{"id":6115,"name":"Roaming Felguard","count":3,"outof":12,"note":"a [b] {c}"}]});
new Listview({template: 'npc', id: 'dropped-by', data: [{"id":1,"count":1,"outof":2}]});
"""


def test_listviews_become_sources():
    sources = sources_from_capture({"listviews": LISTVIEWS, "raw": [RAW]}, FEL_CRYSTAL)
    by = {(s.kind, s.source_id): s for s in sources}
    felguard = by[("drop", 6115)]
    assert felguard.chance == pytest.approx(0.042)
    assert felguard.samples == 1000 and felguard.zones == (361,)
    assert felguard.level == (51, 52)
    assert by[("mining", 175404)].source_type == "object"
    # skinned-from only exists in the raw script; dropped-by from raw is ignored because the
    # structured listview already had it
    assert by[("skinning", 6115)].chance == pytest.approx(0.25)
    assert ("drop", 1) not in by
    assert all(s.kind != "comment" for s in sources)


def test_raw_parser_handles_brackets_in_strings():
    tabs = parse_raw_listviews(RAW)
    assert [t[0] for t in tabs] == ["skinned-from", "dropped-by"]
    assert tabs[0][2][0]["note"] == "a [b] {c}"


def test_save_capture_keys_by_page(tmp_path):
    key = save_capture(
        tmp_path,
        {
            "url": f"https://www.wowhead.com/forever/item={FEL_CRYSTAL}/fel-crystal#dropped-by",
            "listviews": LISTVIEWS,
        },
    )
    assert key == f"item-{FEL_CRYSTAL}"
    assert f"item-{FEL_CRYSTAL}" in captured(tmp_path)
    assert {s.source_id for s in load_sources(tmp_path)} >= {6115, 175404}
    with pytest.raises(ValueError):
        save_capture(tmp_path, {"url": "https://www.wowhead.com/classic/item=1"})


def test_token_is_stable_and_embedded(tmp_path):
    token = get_token(tmp_path)
    assert token == get_token(tmp_path)
    script = userscript(8765, token)
    assert token in script and "127.0.0.1:8765/capture" in script
    assert "@match        https://www.wowhead.com/forever/item=*" in script


def test_parses_react_elite_boss_skill_and_item_name():
    record = {
        "title": "Fel Crystal - Item - Forever",
        "listviews": {
            "dropped-by": {
                "template": {"entityType": 1},  # real pages put an object here
                "data": [
                    {
                        "id": 246017,
                        "name": "Unstable Sentinel",
                        "classification": 1,
                        "location": [36, -1],
                        "count": 103,
                        "outof": 1667,
                        "react": [-1, 1],
                    },
                    {"id": 4421, "name": "Charlga", "boss": 1, "count": 1, "outof": 10},
                ],
            },
            "gathered-from-object": {
                "data": [
                    {"id": 142142, "name": "Sungrass", "skill": 230, "count": 13, "outof": 90}
                ],
            },
        },
    }
    by = {s.source_id: s for s in sources_from_capture(record, FEL_CRYSTAL)}
    sentinel = by[246017]
    assert sentinel.elite and not sentinel.boss
    assert sentinel.zones == (36,)  # -1 dropped
    assert sentinel.friendly_to("horde") and not sentinel.friendly_to("alliance")
    assert sentinel.item_name == "Fel Crystal"
    assert by[4421].boss
    assert by[142142].kind == "herbalism" and by[142142].skill == 230
