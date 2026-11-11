from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wowgold import db
from wowgold.methods.models import Method, load_methods
from wowgold.planner.engine import PlanOptions, eligibility, evaluate, make_plan, upgrade_targets
from wowgold.planner.pricing import ItemPrice, PriceBook, build_price_book
from wowgold.planner.profile import CharacterProfile, parse_professions

METHODS_DIR = Path(__file__).parent.parent / "data" / "methods"


def price(item_id, market=None, listed=0, vendor_sell=0, vendor_buy=None, absorb=None, name=None):
    return ItemPrice(
        item_id=item_id,
        name=name,
        market=market,
        listed_qty=listed,
        vendor_sell=vendor_sell,
        vendor_buy=vendor_buy,
        sold_per_day=None,
        absorb_per_day=absorb if absorb is not None else listed * 0.25,
        absorb_measured=False,
    )


def book(*prices: ItemPrice) -> PriceBook:
    return PriceBook(house_ids=[1], prices={p.item_id: p for p in prices})


def profile(**kw) -> CharacterProfile:
    base = {"class": "mage", "level": 60, "faction": "horde", "professions": {}}
    return CharacterProfile(**{**base, **kw})


def gather(**kw) -> Method:
    base = {
        "id": "g",
        "name": "G",
        "kind": "gather",
        "yields": [{"item": 1, "name": "Herb", "qty": 10, "sd": 2}],
    }
    return Method.model_validate({**base, **kw})


def craft(**kw) -> Method:
    base = {
        "id": "c",
        "name": "C",
        "kind": "craft",
        "reagents": [
            {"item": 1, "name": "Herb", "qty": 2},
            {"item": 2, "name": "Vial", "qty": 1, "vendor": True},
        ],
        "outputs": [{"item": 3, "name": "Potion", "qty": 1}],
    }
    return Method.model_validate({**base, **kw})


# -- models ------------------------------------------------------------------------


def test_shipped_methods_load():
    methods = load_methods(METHODS_DIR)
    assert len(methods) >= 25
    assert {m.kind for m in methods} == {"gather", "farm", "fishing", "craft"}


def test_method_validation_errors():
    with pytest.raises(ValueError, match="reagents and outputs"):
        Method.model_validate({"id": "x", "name": "x", "kind": "craft"})
    with pytest.raises(ValueError, match="unknown profession"):
        gather(requirements={"professions": {"basketweaving": 1}})
    with pytest.raises(ValueError, match="needs yields"):
        Method.model_validate({"id": "x", "name": "x", "kind": "farm"})


def test_parse_professions_and_profile_validation():
    assert parse_professions(["herbalism:300,alchemy:275", "First Aid:300"]) == {
        "herbalism": 300,
        "alchemy": 275,
        "first_aid": 300,
    }
    with pytest.raises(ValueError):
        parse_professions(["herbalism"])
    with pytest.raises(ValueError):
        profile(**{"class": "deathknight"})


# -- eligibility ---------------------------------------------------------------------


def test_eligibility_reasons():
    m = gather(
        requirements={
            "level": 55,
            "professions": {"herbalism": 300},
            "classes": ["druid"],
            "gear": "preraid",
        }
    )
    reasons = eligibility(m, profile(level=50, professions={"herbalism": 250}), PlanOptions())
    assert [r.kind for r in reasons] == ["level", "profession", "class", "gear"]
    assert str(reasons[1]) == "herbalism 300 (you have 250)"
    assert eligibility(gather(), profile(), PlanOptions()) == []


# -- valuation ------------------------------------------------------------------------


def test_gathering_values_at_ah_net_and_caps_by_demand():
    # 10/h * 2h = 20 herbs. Capacity = 40 listed * 0.25/day * 3 days * 0.5 share = 15.
    b = book(price(1, market=1000, listed=40, vendor_sell=10))
    ev = evaluate(gather(), profile(), b, PlanOptions(hours=2))
    line = ev.lines[0]
    assert (line.produced, line.ah_units, line.vendor_units) == (20, 15, 5)
    assert line.value == 15 * 950 + 5 * 10  # 5% AH cut
    assert ev.gold_per_hour == pytest.approx(line.value / 2)
    assert ev.low < ev.gold_per_hour < ev.high
    assert any("market takes ~15 of 20" in f for f in ev.flags)


def test_unlisted_items_fall_back_to_vendor():
    ev = evaluate(gather(), profile(), book(price(1, vendor_sell=25)), PlanOptions(hours=1))
    assert ev.lines[0].value == 250
    assert any("not on the AH" in f for f in ev.flags)


def test_class_modifier_and_travel_speed():
    m = gather(class_modifiers={"druid": 1.5})
    b = book(price(1, market=100, listed=10_000))
    base = evaluate(m, profile(), b, PlanOptions(hours=1)).lines[0].produced
    assert evaluate(m, profile(**{"class": "druid"}), b, PlanOptions(hours=1)).lines[
        0
    ].produced == pytest.approx(base * 1.5)
    assert evaluate(m, profile(level=30), b, PlanOptions(hours=1)).lines[
        0
    ].produced == pytest.approx(base * 0.75)  # no mount yet


def test_vendor_coins_count():
    m = gather(yields=[], vendor_copper_per_hour=50_000, kind="farm")
    ev = evaluate(m, profile(gear="leveling"), book(), PlanOptions(hours=2))
    assert ev.total == 100_000
    # One gear tier above what the farm needs -> 10% faster.
    assert evaluate(m, profile(gear="fresh"), book(), PlanOptions(hours=2)).total == pytest.approx(
        110_000
    )


def test_craft_profit_uses_vendor_reagents_and_demand_cap():
    # cost = 2*300 (AH herb) + 50 (vendor vial) = 650; revenue = 2000*0.95 = 1900 -> 1250/craft
    b = book(
        price(1, market=300, listed=1000),
        price(2, vendor_buy=50, market=999_999, listed=5),  # vendor price wins
        price(3, market=2000, listed=80),  # capacity 80*.25*3*.5 = 30 crafts
    )
    ev = evaluate(craft(), profile(), b, PlanOptions(hours=2))
    assert ev.profit_per_craft == 1250
    assert ev.crafts == 30
    assert ev.total == 30 * 1250
    assert any("limited by demand" in f for f in ev.flags)


def test_craft_limited_by_listed_reagents():
    b = book(
        price(1, market=300, listed=10),
        price(2, vendor_buy=50),
        price(3, market=2000, listed=10_000),
    )
    ev = evaluate(craft(), profile(), b, PlanOptions(hours=2))
    assert ev.crafts == 5  # 10 herbs listed / 2 per craft


def test_unprofitable_craft_scores_zero():
    b = book(
        price(1, market=5000, listed=100),
        price(2, vendor_buy=50),
        price(3, market=2000, listed=100),
    )
    ev = evaluate(craft(), profile(), b, PlanOptions())
    assert ev.gold_per_hour == 0 and ev.crafts == 0
    assert "unprofitable at current prices" in ev.flags


def test_craft_with_unpriced_reagent_is_not_evaluated():
    ev = evaluate(craft(), profile(), book(price(2, vendor_buy=50)), PlanOptions())
    assert ev.gold_per_hour == 0
    assert any("can't evaluate" in f for f in ev.flags)


# -- planning ----------------------------------------------------------------------


def test_make_plan_ranks_and_suggests_upgrades():
    cheap = gather(id="cheap", name="Cheap")
    rich = gather(
        id="rich",
        name="Rich",
        yields=[{"item": 2, "name": "Lotus", "qty": 5}],
        requirements={"professions": {"herbalism": 300}},
    )
    druid_only = gather(id="druid", name="Druid", requirements={"classes": ["druid"]})
    b = book(price(1, market=100, listed=10_000), price(2, market=10_000, listed=10_000))
    p = profile(professions={"herbalism": 250})
    result = make_plan([cheap, rich, druid_only], p, b, PlanOptions())

    assert [e.method.id for e in result.ranked] == ["cheap"]
    assert {i.method.id for i in result.ineligible} == {"rich", "druid"}
    targets = upgrade_targets(result, p, b, PlanOptions())
    assert [ev.method.id for ev, _ in targets] == ["rich"]  # class-locked one isn't suggested


# -- price book from the database ------------------------------------------------------


def _snapshot(conn, sid, house, ts, rows, interval=None):
    conn.execute(
        "INSERT INTO snapshots (id, region, ruleset_id, house_id, kind, fetched_at, http_status,"
        " outcome, ingested_at, n_auctions)"
        " VALUES (?, 'eu', 1, ?, 'commodities', ?, 200, 'stored', ?, ?)",
        (sid, house, ts, ts, len(rows)),
    )
    for item_id, qty, market, sold in rows:
        conn.execute(
            "INSERT INTO price_stats VALUES (?, 'eu', ?, 'commodities', ?, ?, '', 1, ?, ?, ?, ?, ?,"
            " ?, ?, 0, ?)",
            (sid, house, ts, item_id, qty, market, market, market, market, market, sold, interval),
        )


def test_build_price_book_merges_houses_and_uses_measured_sales(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    conn.execute(
        "INSERT INTO items (item_id, name, sell_price, purchase_price, source, fetched_at)"
        " VALUES (1, 'Herb', 10, NULL, 'blizzard', 'x')"
    )
    # Two houses, latest snapshot each; house 1 has 12 hourly snapshots of sales history.
    for h in range(12):
        ts = (now - timedelta(hours=12 - h)).isoformat(timespec="seconds")
        _snapshot(conn, h + 1, 1, ts, [(1, 100, 200, 2)], interval=3600)
    _snapshot(conn, 100, 2, now.isoformat(timespec="seconds"), [(1, 300, 100, None)])

    b = build_price_book(conn, "eu", [1, 2], {1}, now=now)
    p = b.get(1)
    assert p.listed_qty == 400
    assert p.market == round((200 * 100 + 100 * 300) / 400)
    assert p.name == "Herb" and p.vendor_sell == 10
    # 12h of coverage on house 1, averaged over 2 houses -> 6h: below threshold -> estimate.
    assert b.sales_coverage_h == pytest.approx(6)
    assert not p.absorb_measured and p.absorb_per_day == 100

    b1 = build_price_book(conn, "eu", [1], {1}, now=now)
    assert b1.get(1).absorb_measured
    assert b1.get(1).absorb_per_day == pytest.approx(24 * 2 * 12 / 12)


# -- CLI ------------------------------------------------------------------------------


def test_plan_cli_smoke(tmp_path, monkeypatch):
    from wowgold import cli
    from wowgold.config import get_settings

    monkeypatch.setenv("WOWGOLD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WOWGOLD_METHODS_DIR", str(METHODS_DIR))
    monkeypatch.setenv("WOWGOLD_HOUSES", "[]")
    get_settings.cache_clear()
    conn = db.connect(tmp_path / "wowgold.sqlite3")
    db.upsert_house(conn, "eu", 113, "Horde Auction House", "x", "horde")
    now = datetime.now(UTC).isoformat(timespec="seconds")
    _snapshot(conn, 1, 113, now, [(13466, 500, 7000, None), (13465, 500, 7000, None)])
    conn.commit()
    conn.close()

    result = CliRunner().invoke(
        cli.app,
        ["plan", "-c", "druid", "-l", "60", "-f", "horde", "-p", "herbalism:300", "--region", "eu"],
    )
    get_settings.cache_clear()
    assert result.exit_code == 0, result.output
    assert "Eastern Plaguelands herbs" in result.output
    assert "house 113" in result.output
