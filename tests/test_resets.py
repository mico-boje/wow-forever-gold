"""Auction house resets: normal price, sales and restocking, and the buy-out plan."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta

import pytest

from wowgold import db
from wowgold.market.resets import Rung, _evaluate, history, plan_resets
from wowgold.planner.pricing import PriceBook, build_price_book

NOW = datetime(2026, 11, 16, 12, tzinfo=UTC)
HERB, CRASH = 100, 200


def test_buy_out_depth():
    # 3 cheap listings at 1g, 2 at 1.2g, then the wall at 3g; normal price 2.5g
    ladder = [Rung(10000, 3), Rung(12000, 2), Rung(30000, 5)]
    best = _evaluate(ladder, normal=25000, sold=10, capture=0.5, budget=10**9, horizon=3, cut=0.05)
    profit, units, cost, max_paid, relist, wall, expected, revenue, leftover, bound = best
    # buying both cheap rungs: relist at the normal price (under the 3g wall), all 5 sell
    assert (units, cost, max_paid, relist, wall) == (5, 54000, 12000, 25000, 30000)
    assert expected == 5 and revenue == pytest.approx(5 * 25000 * 0.95)
    assert profit == pytest.approx(revenue - cost) and not bound

    # a budget for 2 of the 3 cheapest: the third is still listed at 1g and undercuts you,
    # so a partial buy-out of a price level is never a reset
    assert _evaluate(ladder, 25000, 10, 0.5, 20000, 3, 0.05) is None
    # a budget for that whole first level (3g) is: relist under the 1.2g level left behind
    best = _evaluate(ladder, 25000, 10, 0.5, 30000, 3, 0.05)
    assert best[1:4] == (3, 30000, 10000) and best[4] == int(12000 * 0.99) and not best[9]

    # nothing cheap enough: the next listing is barely above
    assert _evaluate([Rung(10000, 1), Rung(10500, 1)], 25000, 10, 1, 10**9, 3, 0.05) is None


def _db(tmp_path):
    conn = db.connect(tmp_path / "w.sqlite3")
    conn.execute(
        "INSERT INTO items (item_id, name, sell_price, source, fetched_at) VALUES "
        "(100, 'Dreamfoil', 10, 'blizzard', 'x'), (200, 'Crashing Herb', 10, 'blizzard', 'x')"
    )
    # 48 hourly snapshots of history: Dreamfoil steady at 2.5g, 50 listed, 1 sold and 2 new
    # listed per hour; Crashing Herb halving every day
    for h in range(48, 0, -1):
        ts = (NOW - timedelta(hours=h)).isoformat(timespec="seconds")
        sid = 1000 - h
        conn.execute(
            "INSERT INTO snapshots (id, region, ruleset_id, house_id, kind, fetched_at, "
            "http_status, outcome, ingested_at) "
            "VALUES (?, 'eu', 1, 1, 'commodities', ?, 200, 'stored', ?)",
            (sid, ts, ts),
        )
        qty = 50 + (48 - h)  # grows by 1 an hour: +2 listed, -1 sold
        crash = int(40000 * 0.5 ** ((48 - h) / 24))
        for item, price, q in ((HERB, 25000, qty), (CRASH, crash, 50)):
            conn.execute(
                "INSERT INTO price_stats VALUES (?, 'eu', 1, 'commodities', ?, ?, '', 10, ?, "
                "?, ?, ?, ?, ?, 1, 0, 3600)",
                (sid, ts, item, q, price, price, price, price, price),
            )
    # the latest full snapshot: someone dumped 4 Dreamfoil at 1g; the wall is at 2.4g
    raw = tmp_path / "raw.json.gz"
    auctions = [
        {"id": i, "item": {"id": HERB}, "quantity": 1, "unit_price": 10000} for i in range(4)
    ] + [{"id": 10, "item": {"id": HERB}, "quantity": 20, "unit_price": 24000}]
    auctions += [{"id": 20, "item": {"id": CRASH}, "quantity": 5, "unit_price": 1000}]
    raw.write_bytes(gzip.compress(json.dumps({"auctions": auctions}).encode()))
    ts = NOW.isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO snapshots (id, region, ruleset_id, house_id, kind, fetched_at, http_status,"
        " outcome, ingested_at, path) VALUES (2000, 'eu', 1, 1, 'commodities', ?, 200, "
        "'stored', ?, ?)",
        (ts, ts, str(raw)),
    )
    conn.commit()
    return conn


def test_history_and_plan(tmp_path):
    conn = _db(tmp_path)
    normal, flows = history(conn, "eu", [1], {HERB, CRASH}, now=NOW)
    assert normal[HERB] == (25000, 48)
    f = flows[HERB]
    assert f.sold_per_day == pytest.approx(24) and f.restock_per_day == pytest.approx(48)

    book = build_price_book(conn, "eu", [1], {HERB, CRASH}, now=NOW)
    plans = plan_resets(conn, "eu", [1], book, budget=10**9, now=NOW)
    (plan,) = plans  # the crashing herb is left out
    assert plan.item_id == HERB and plan.units == 4 and plan.cost == 40000
    assert plan.relist == int(24000 * 0.99) and plan.wall == 24000
    assert plan.capture == pytest.approx(24 / (24 + 48))  # new listings take a share
    assert plan.profit > 0 and plan.roi > 1
    assert any("relist faster" in f for f in plan.flags)

    # with 2g to spend: only part of the 1g listings, which would undercut you: no reset
    assert plan_resets(conn, "eu", [1], book, budget=20000, now=NOW) == []

    # without trend data, a crashing item isn't skipped, but it's relisted at what it
    # fetched over the last day, not its week's median
    crash = next(
        p
        for p in plan_resets(conn, "eu", [1], PriceBook(house_ids=[1]), 10**9, now=NOW)
        if p.item_id == CRASH
    )
    assert crash.relist < normal[CRASH][0] or crash.normal <= 40000 * 0.5**1.4


def test_resets_page(tmp_path):
    from fastapi.testclient import TestClient

    from wowgold.config import Settings
    from wowgold.web.app import create_app

    settings = Settings(wowgold_data_dir=tmp_path, wowgold_region="eu", wowgold_houses=[])
    client = TestClient(create_app(settings))
    page = client.get("/resets").text
    assert "No gold recorded by the addon yet" in page
    page = client.get("/resets?gold=12g34s").text
    assert "No auction data for that faction yet." in page
    assert "can&#39;t read coins" in client.get("/resets?gold=lots").text
