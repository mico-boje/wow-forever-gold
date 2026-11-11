from __future__ import annotations

from dataclasses import astuple
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from wowgold import db
from wowgold.methods.models import Method, load_methods
from wowgold.segments import (
    ItemMeta,
    classify_source,
    draft_method,
    parse_segment,
    propose,
    split_activities,
)

METHODS_DIR = Path(__file__).parent.parent / "data" / "methods"
METHODS = load_methods(METHODS_DIR)

HERB = 11993
MINE = 10248
SKIN = 10768

META = {
    13463: ItemMeta("Dreamfoil", "Trade Goods", "Herb", "COMMON", 100),
    8846: ItemMeta("Gromsblood", "Trade Goods", "Herb", "COMMON", 100),
    13466: ItemMeta("Plaguebloom", "Trade Goods", "Herb", "COMMON", 100),
    13465: ItemMeta("Mountain Silversage", "Trade Goods", "Herb", "COMMON", 100),
    10620: ItemMeta("Thorium Ore", "Trade Goods", "Metal & Stone", "COMMON", 250),
    12365: ItemMeta("Dense Stone", "Trade Goods", "Metal & Stone", "COMMON", 250),
    8170: ItemMeta("Rugged Leather", "Trade Goods", "Leather", "COMMON", 100),
    14047: ItemMeta("Runecloth", "Trade Goods", "Cloth", "COMMON", 100),
    14256: ItemMeta("Felcloth", "Trade Goods", "Cloth", "UNCOMMON", 500),
    5000: ItemMeta("Broken Fang", "Miscellaneous", "Junk", "POOR", 300),
    9000: ItemMeta("Odd Trinket", "Miscellaneous", "Junk", "COMMON", 10),
}


def seg_raw(zone="Felwood", minutes=40, sources=None, kills=None, label=None, sid="1-0001"):
    return {
        "id": sid,
        "zone": zone,
        "start": 1762850000,
        "active": minutes * 60,
        "class": "DRUID",
        "level": 60,
        "label": label,
        "copper": 0,
        "sources": sources or {},
        "kills": kills or {},
    }


def src(loots, items, via=None, copper=0):
    out = {"loots": loots, "items": items, "copper": copper}
    if via is not None:
        out["via"] = via
    return out


FELWOOD_HERBS = {
    "GameObject:176584": src(10, {13463: 14}, HERB),
    "GameObject:176586": src(8, {8846: 12, 13466: 4}, HERB),
}


# -- classification ----------------------------------------------------------------------


def test_classify_by_spell_then_by_items():
    s = parse_segment(
        seg_raw(
            sources={
                "GameObject:1": src(1, {13463: 1}, HERB),
                "GameObject:2": src(1, {10620: 2, 12365: 1}),  # no spell: ore -> mining
                "GameObject:35591": src(1, {9000: 1}),  # fishing bobber
                "Creature:7105": src(1, {14047: 2}),  # cloth -> kill
                "GameObject:3": src(1, {9000: 1}),  # a chest: other
                "unknown": src(0, {14047: 1}),
            }
        )
    ).sources
    got = {x.key: classify_source(x, META) for x in s}
    assert got == {
        "GameObject:1": "herbalism",
        "GameObject:2": "mining",
        "GameObject:35591": "fishing",
        "Creature:7105": "kill",
        "GameObject:3": "other",
        "unknown": "other",
    }


def test_skinned_corpses_belong_to_skinning():
    # The addon keys sources by NPC, so a bear's normal loot and its skinning loot share one
    # entry; the skinning cast marks it. Its kills then count towards skinning, not killing.
    seg = parse_segment(
        seg_raw(
            zone="Winterspring",
            sources={
                "Creature:7444": src(24, {5000: 6, 8170: 30}, SKIN, copper=9000),
                "Creature:7105": src(5, {14047: 4}),
            },
            kills={7444: 12, 7105: 5},
        )
    )
    acts = {a.activity: a for a in split_activities(seg, META)}
    assert set(acts) == {"skinning", "kill"}
    assert acts["skinning"].copper == 9000 and acts["skinning"].items == {5000: 6, 8170: 30}
    assert acts["kill"].events == 5 and acts["kill"].npc_ids == {7105}


# -- matching -------------------------------------------------------------------------------


def test_pure_herbing_is_auto_matched():
    p = propose(parse_segment(seg_raw(sources=FELWOOD_HERBS)), METHODS, META)
    assert p.status == "auto"
    [a] = p.assignments
    assert (a.method_id, a.activity, a.seconds) == ("herb_felwood", "herbalism", 2400)


def test_same_zone_different_activity_picks_the_right_method():
    raw = seg_raw(
        sources={"Creature:7105": src(40, {14047: 35, 14256: 3}, copper=50000)}, kills={7105: 40}
    )
    p = propose(parse_segment(raw), METHODS, META)
    assert p.status == "auto"
    assert [a.method_id for a in p.assignments] == ["farm_felcloth_jadefire"]


def test_mixed_segment_is_split_but_needs_review():
    sources = {
        **FELWOOD_HERBS,  # 18 events
        "Creature:7105": src(18, {14047: 15, 14256: 1}, copper=20000),
    }
    p = propose(parse_segment(seg_raw(sources=sources, kills={7105: 18})), METHODS, META)
    assert p.status == "review"
    got = {a.method_id: a.seconds for a in p.assignments}
    assert got == {"herb_felwood": 1200, "farm_felcloth_jadefire": 1200}
    assert any("mixed" in n for n in p.notes)


def test_unmatched_zone_and_short_segments():
    p = propose(parse_segment(seg_raw(zone="Moonglade", sources=FELWOOD_HERBS)), METHODS, META)
    assert p.status == "unmatched" and "no herbalism method for Moonglade" in p.notes
    p = propose(parse_segment(seg_raw(minutes=3, sources=FELWOOD_HERBS)), METHODS, META)
    assert p.status == "empty"


def test_low_overlap_is_not_assigned():
    raw = seg_raw(sources={"GameObject:1": src(10, {13463: 2, 9000: 20}, HERB)})
    p = propose(parse_segment(raw), METHODS, META)
    assert p.status == "unmatched" and "covers only" in p.notes[0]


def test_ambiguous_methods_are_not_auto_assigned():
    a = Method.model_validate(
        {
            "id": "a",
            "name": "A",
            "kind": "gather",
            "zone": "Felwood",
            "requirements": {"professions": {"herbalism": 1}},
            "yields": [{"item": 13463, "name": "Dreamfoil", "qty": 1}],
        }
    )
    b = a.model_copy(update={"id": "b"})
    raw = seg_raw(sources={"GameObject:1": src(10, {13463: 10}, HERB)})
    p = propose(parse_segment(raw), [a, b], META)
    assert p.status == "unmatched" and "match equally" in p.notes[0]


def test_labelled_segments_skip_matching():
    p = propose(parse_segment(seg_raw(sources=FELWOOD_HERBS, label="herb_felwood")), METHODS, META)
    assert p.status == "labelled" and p.assignments[0].seconds == 2400
    p = propose(parse_segment(seg_raw(sources=FELWOOD_HERBS, label="nope")), METHODS, META)
    assert p.status == "review"


def test_draft_method_is_valid_yaml_and_a_valid_method():
    raw = seg_raw(
        zone="Silithus",
        minutes=60,
        sources={
            "Creature:11880": src(30, {14047: 30, 5000: 10}, copper=40000),
        },
        kills={11880: 30},
    )
    seg = parse_segment(raw)
    [t] = split_activities(seg, META)
    text = draft_method(seg, t, META)
    [entry] = yaml.safe_load(text)
    m = Method.model_validate(entry)
    assert m.id == "kill_silithus" and m.kind == "farm"
    assert m.yields[0].qty == pytest.approx(30)
    assert m.vendor_copper_per_hour == 40000 + 10 * 300  # greys -> vendor income


# -- CLI ---------------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from wowgold.config import get_settings

    monkeypatch.setenv("WOWGOLD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WOWGOLD_METHODS_DIR", str(METHODS_DIR))
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "")
    get_settings.cache_clear()
    conn = db.connect(tmp_path / "wowgold.sqlite3")
    conn.executemany(
        "INSERT INTO items (item_id, name, item_class, item_subclass, quality, sell_price,"
        " source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, 'blizzard', 'x')",
        [(i, *astuple(m)) for i, m in META.items()],
    )
    conn.commit()
    conn.close()
    yield tmp_path
    get_settings.cache_clear()


def run(*args):
    from wowgold import cli

    return CliRunner().invoke(cli.app, list(args), terminal_width=200)


def _lua_table(value, indent=0) -> str:
    if isinstance(value, dict):
        inner = ",\n".join(
            f"{'  ' * (indent + 1)}[{k if isinstance(k, int) else repr(str(k))}] = "
            f"{_lua_table(v, indent + 1)}"
            for k, v in value.items()
            if v is not None
        )
        return "{\n" + inner + "\n" + "  " * indent + "}"
    if isinstance(value, str):
        return '"' + value + '"'
    return str(value).lower() if isinstance(value, bool) else str(value)


def test_cli_import_review_assign_and_draft(cli_env):
    run("profile", "set", "-c", "druid", "-l", "60", "-f", "horde", "-p", "herbalism:300")
    segments = {
        1: seg_raw(sources=FELWOOD_HERBS, sid="a"),
        2: seg_raw(
            sid="b",
            sources={**FELWOOD_HERBS, "Creature:7105": src(18, {14047: 15}, copper=1000)},
            kills={7105: 18},
        ),
        3: seg_raw(
            sid="c",
            zone="Silithus",
            sources={"Creature:11880": src(30, {14047: 30})},
            kills={11880: 30},
        ),
    }
    saved = cli_env / "WowGoldLogger.lua"
    saved.write_text("WowGoldLoggerDB = " + _lua_table({"segments": segments}) + "\n")

    r = run("session", "import", str(saved))
    assert r.exit_code == 0, r.output
    assert "3 new" in r.output and "2 segment(s) need a look" in r.output

    r = run("segments", "list")
    assert "applied" in r.output and "review" in r.output and "unmatched" in r.output

    r = run("segments", "show", "2")
    assert r.exit_code == 0, r.output
    assert "herbalism" in r.output and "farm_felcloth_jadefire" in r.output

    assert run("segments", "accept", "2").exit_code == 0
    assert run("segments", "accept", "3").exit_code != 0  # nothing matched

    r = run("segments", "draft", "3")
    assert r.exit_code == 0 and "kill_silithus" in r.output
    assert (
        run(
            "segments", "assign", "3", "farm_air_elementals_silithus", "--activity", "kill"
        ).exit_code
        == 0
    )
    assert run("segments", "assign", "3", "x", "--activity", "kill").exit_code != 0

    conn = db.connect(cli_env / "wowgold.sqlite3")
    methods = sorted(r[0] for r in conn.execute("SELECT method_id FROM sessions"))
    assert methods == [
        "farm_air_elementals_silithus",
        "farm_felcloth_jadefire",
        "herb_felwood",
        "herb_felwood",
    ]
    conn.close()

    assert run("segments", "ignore", "3").exit_code == 0
    conn = db.connect(cli_env / "wowgold.sqlite3")
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 3
    conn.close()

    r = run("session", "import", str(saved))  # re-import: nothing new
    assert "0 new, 3 already imported" in r.output
