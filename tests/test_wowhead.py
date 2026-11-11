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


def test_npc_page_becomes_its_loot_table(tmp_path):
    from wowgold.wowhead.sources import sources_from_entity_page

    record = {
        "title": "Muculent Ooze - NPC - Forever",
        "listviews": {
            "drops": {
                "template": {"entityType": 3},
                "data": [
                    {
                        "id": 11834,
                        "name": "1Un'Goro Soil",
                        "count": 300,
                        "outof": 1000,
                        "stack": [1, 3],
                    },
                    {
                        "id": 15736,
                        "name": "3Pattern: Devilsaur Leggings",
                        "count": 5,
                        "outof": 1000,
                    },
                ],
            },
            "skinning": {
                "template": {"entityType": 3},
                "data": [{"id": 4304, "count": 40, "outof": 80}],
            },
            "sells": {"template": {"entityType": 3}, "data": [{"id": 1, "count": 1, "outof": 1}]},
            "same-model-as": {"template": {"entityType": 1}, "data": [{"id": 6559}]},
        },
    }
    by = {(s.kind, s.item_id): s for s in sources_from_entity_page(record, "npc", 6559)}
    assert set(by) == {("drop", 11834), ("drop", 15736), ("skinning", 4304)}
    soil = by[("drop", 11834)]
    assert soil.chance == pytest.approx(0.3 * 2)  # mean stack of 1-3
    assert soil.source_id == 6559 and soil.source_name == "Muculent Ooze"
    assert soil.from_page and soil.samples == 1000
    assert by[("drop", 15736)].item_name == "Pattern: Devilsaur Leggings"

    save_capture(tmp_path, {"url": "https://www.wowhead.com/forever/npc=6559", **record})
    assert {s.source_id for s in load_sources(tmp_path)} == {6559}


def test_spell_list_pages_are_captured(tmp_path):
    from wowgold.wowhead.capture import page_key

    base = "https://www.wowhead.com/forever/spells"
    assert page_key(f"{base}/professions/alchemy") == "spells-professions-alchemy"
    assert page_key(f"{base}/professions/alchemy/#0+1") == "spells-professions-alchemy"
    assert (
        page_key(f"{base}/secondary-skills/first-aid?filter=1")
        == "spells-secondary-skills-first-aid"
    )
    assert page_key("https://www.wowhead.com/forever/item=5") == "item-5"
    assert page_key("https://www.wowhead.com/classic/spells/professions/alchemy") is None
    script = userscript(8765, "t")
    assert "@match        https://www.wowhead.com/forever/spells/*" in script
    assert "@updateURL    http://127.0.0.1:8765/wowgold-capture.user.js" in script
    assert save_capture(tmp_path, {"url": f"{base}/professions/alchemy"}) == (
        "spells-professions-alchemy"
    )


def test_js_style_listview_data():
    from wowgold.wowhead.jsdata import js_to_json, parse_js

    assert parse_js("[{a:1,'b':'it\\'s',\"c\":undefined,d:[1,2,],}]") == [
        {"a": 1, "b": "it's", "c": None, "d": [1, 2]}
    ]
    # keys inside strings stay untouched
    assert parse_js('[{"t":"a, b: c", n:1e3}]') == [{"t": "a, b: c", "n": 1000.0}]
    assert js_to_json('{x:"say \\"hi\\""}') == '{"x":"say \\"hi\\""}'

    script = (
        'var listviewspells = [{"id":1,"creates":[2,1,1],quality:-1,popularity:207}];\n'
        "new Listview({template: 'spell', id: 'spells', note: \"a:b\", data: listviewspells});"
    )
    ((tab, template, rows),) = parse_raw_listviews(script)
    assert (tab, template) == ("spells", "spell")
    assert rows == [{"id": 1, "creates": [2, 1, 1], "quality": -1, "popularity": 207}]


def test_numeric_keys_in_js_data():
    from wowgold.wowhead.jsdata import parse_js

    assert parse_js("{16591: {0: [1.5, -2e3]}, a: 3}") == {"16591": {"0": [1.5, -2000.0]}, "a": 3}
