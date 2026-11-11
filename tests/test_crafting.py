"""Recipes (vanilla + Wowhead) and craft profitability."""

from __future__ import annotations

import json

import pytest

from tests.test_discovery import (
    BONES,
    DRAUGHT,
    DRAUGHT_SPELL,
    HERB,
    RECIPE_ITEM,
    TRANSMUTE_SPELL,
    VIAL,
    _price,
    build_world,
)
from wowgold.crafting.profit import (
    CraftContext,
    CraftOptions,
    evaluate_recipe,
    farm_craft_uplift,
    processing_options,
    rank_crafts,
)
from wowgold.crafting.recipes import (
    load_vanilla_recipes,
    load_wowhead_recipes,
    merge_recipes,
    recipe_from_wowhead_row,
)
from wowgold.discovery import vanilla as v
from wowgold.planner.engine import ItemLine
from wowgold.planner.pricing import PriceBook


@pytest.fixture
def world(tmp_path):
    conn = v.connect(build_world(tmp_path / "mangos.sqlite"))
    yield conn
    conn.close()


@pytest.fixture
def ctx(world):
    items = v.load_items(world)
    vendor = frozenset({VIAL, RECIPE_ITEM})
    return CraftContext(vendor, {i: items[i].buy_price for i in vendor}, items)


@pytest.fixture
def book():
    prices = [
        _price(HERB, market=30000, listed=500, vendor_sell=200, name="Dreamfoil"),
        _price(DRAUGHT, market=100000, listed=40, vendor_sell=500, name="Dreamfoil Draught"),
    ]
    return PriceBook(house_ids=[1], prices={p.item_id: p for p in prices})


def test_vanilla_recipes(world):
    recipes = load_vanilla_recipes(world)
    assert set(recipes) == {DRAUGHT_SPELL, TRANSMUTE_SPELL}
    draught = recipes[DRAUGHT_SPELL]
    assert draught.profession == "alchemy" and draught.skill == 200
    assert draught.reagents == [(HERB, 2.0), (VIAL, 1.0)] and draught.output == DRAUGHT
    assert draught.output_qty == 1.0 and draught.cooldown_s == 0
    assert [(ls.kind, ls.cost) for ls in draught.learn] == [("trainer", 1000)]
    transmute = recipes[TRANSMUTE_SPELL]
    assert transmute.skill == 250 and transmute.cooldown_s == 2 * 86400
    (learn,) = transmute.learn
    assert (learn.kind, learn.item_id, learn.vendor, learn.cost) == (
        "recipe",
        RECIPE_ITEM,
        True,
        5000,
    )


def test_evaluate_recipe(world, ctx, book):
    draught = load_vanilla_recipes(world)[DRAUGHT_SPELL]
    ev = evaluate_recipe(draught, book, ctx, CraftOptions())
    vial = next(ln for ln in ev.reagents if ln.item_id == VIAL)
    assert vial.source == "vendor" and vial.unit_cost == 40
    assert ev.cost == pytest.approx(2 * 30000 + 40)
    assert ev.output_unit == pytest.approx(95000)  # after the 5% AH cut
    assert ev.profit == pytest.approx(95000 - 60040)
    assert ev.crafts_demand == pytest.approx(40 * 0.25 * 0.5)  # estimated sales × your share
    assert ev.crafts_mats == pytest.approx(250)  # 500 Dreamfoil listed / 2 per craft
    assert ev.crafts_per_day == pytest.approx(5) and ev.limited_by == "demand"
    assert ev.profit_per_day == pytest.approx(5 * (95000 - 60040))
    assert ev.learn_cost == 1000 and ev.learn_label == "trainer"
    assert ev.payback_crafts == pytest.approx(1000 / (95000 - 60040))


def test_rank_filters_by_skill_and_missing_mats(world, ctx, book):
    recipes = list(load_vanilla_recipes(world).values())
    ranked = rank_crafts(recipes, book, ctx, CraftOptions(), professions={"alchemy": 220})
    assert [e.recipe.spell_id for e in ranked] == [DRAUGHT_SPELL]
    # the transmute needs Old Bone, which is neither listed nor sold by vendors
    everything = rank_crafts(recipes, book, ctx, CraftOptions(), professions={"alchemy": 300})
    assert TRANSMUTE_SPELL not in [e.recipe.spell_id for e in everything]
    assert rank_crafts(recipes, book, ctx, CraftOptions(), professions={"tailoring": 300}) == []


def test_processing_and_farm_uplift(world, ctx, book):
    recipes = list(load_vanilla_recipes(world).values())
    proc = processing_options(recipes, book, ctx, CraftOptions(), {"alchemy": 220})
    herb = proc[HERB]
    assert herb.raw_value == pytest.approx(28500)
    assert herb.value_per_unit == pytest.approx((95000 - 40) / 2)
    assert herb.units_per_day == pytest.approx(5 * 2)
    assert BONES not in proc

    lines = [ItemLine(HERB, "Dreamfoil", 60, 60, 0, 60 * 28500)]
    per_hour, notes = farm_craft_uplift(lines, proc, hours=2, sell_days=3)
    assert per_hour == pytest.approx(30 * (herb.value_per_unit - 28500) / 2)  # 10/day × 3 days
    assert "Dreamfoil Draught" in notes[0]


WOWHEAD_ROW = {
    "cat": 11,
    "creates": [249431, 1, 1],
    "id": 1249632,
    "learnedat": 215,
    "name": "Viridian Dye",
    "reagents": [[249274, 2], [8925, 1]],
    "skill": [171],
    "source": [6],
    "trainingcost": 10000,
}


def test_wowhead_recipe_rows(tmp_path, world):
    r = recipe_from_wowhead_row(WOWHEAD_ROW)
    assert (r.profession, r.skill, r.output, r.output_qty) == ("alchemy", 215, 249431, 1.0)
    assert r.reagents == [(249274, 2.0), (8925, 1.0)]
    assert [(ls.kind, ls.cost) for ls in r.learn] == [("trainer", 10000)]
    transmute = recipe_from_wowhead_row({**WOWHEAD_ROW, "id": 5, "name": "Transmute: X"})
    assert transmute.cooldown_s == 2 * 86400
    assert recipe_from_wowhead_row({**WOWHEAD_ROW, "skill": [999]}) is None

    caps = tmp_path / "wowhead" / "captures"
    caps.mkdir(parents=True)
    (caps / "item-249431.json").write_text(
        json.dumps({"listviews": {"created-by-spell": {"data": [WOWHEAD_ROW]}}})
    )
    (caps / "item-900.json").write_text(
        json.dumps({"listviews": {"teaches-recipe": {"data": [WOWHEAD_ROW]}}})
    )
    forever = load_wowhead_recipes(tmp_path)
    assert list(forever) == [1249632]
    assert {ls.kind for ls in forever[1249632].learn} == {"trainer", "recipe"}

    merged = merge_recipes(load_vanilla_recipes(world), forever)
    assert set(merged) == {DRAUGHT_SPELL, TRANSMUTE_SPELL, 1249632}
