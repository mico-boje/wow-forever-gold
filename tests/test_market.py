from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from tests.conftest import BASE
from wowgold import db
from wowgold.items import load_overrides, sync_items
from wowgold.market.ingest import ingest_pending, prune
from wowgold.market.query import find_items, format_money, house_price
from wowgold.market.stats import Listing, estimate_sales, parse_listings, summarize
from wowgold.snapshot import run_snapshot


def L(aid, item=1, price=100, qty=1, left="LONG", variant=""):  # noqa: N802
    return Listing(aid, item, variant, price, qty, left)


# -- pure stats -------------------------------------------------------------------


def test_parse_listings_handles_both_kinds_and_skips_bid_only():
    payload = {
        "auctions": [
            {
                "id": 1,
                "item": {"id": 5, "bonus_lists": [9, 3]},
                "buyout": 500,
                "quantity": 1,
                "time_left": "LONG",
            },
            {"id": 2, "item": {"id": 6}, "unit_price": 20, "quantity": 40, "time_left": "SHORT"},
            {"id": 3, "item": {"id": 7}, "bid": 10, "quantity": 1, "time_left": "LONG"},
            {"id": 4, "item": {"id": 8}, "buyout": 900, "quantity": 3, "time_left": "LONG"},
        ]
    }
    got = parse_listings(payload)
    assert [(li.item_id, li.variant, li.unit_price, li.quantity) for li in got] == [
        (5, "3,9", 500, 1),
        (6, "", 20, 40),
        (8, "", 300, 3),
    ]
    assert parse_listings({"id": 112}) == []  # empty house


def test_summarize_is_quantity_weighted_and_ignores_high_outliers():
    # 100 units: 20 @ 10c, 60 @ 12c, 19 @ 15c, 1 @ 99999c (troll listing)
    st = summarize([(12, 60), (10, 20), (15, 19), (99999, 1)])
    assert st.quantity == 100 and st.num_auctions == 4
    assert st.min_unit == 10
    assert st.p10_unit == 10
    assert st.p25_unit == 12
    assert st.median_unit == 12
    # cheapest 25 units: 20 @ 10 + 5 @ 12 -> 10.4 -> 10
    assert st.market_unit == 10


def test_summarize_single_unit():
    st = summarize([(777, 1)])
    assert (st.min_unit, st.median_unit, st.market_unit) == (777, 777, 777)


def test_estimate_sales_rules():
    prev = [
        L(1, item=1, qty=1, left="LONG"),  # gone, couldn't expire in 1h -> sold
        L(2, item=1, qty=1, left="SHORT"),  # gone, could have expired -> ambiguous
        L(3, item=2, qty=40, left="VERY_LONG"),  # partially bought: 40 -> 15
        L(4, item=3, qty=5, left="MEDIUM"),  # gone after 1h; MEDIUM may expire -> ambiguous
        L(5, item=4, qty=1, left="LONG"),  # still there unchanged
    ]
    cur = [L(3, item=2, qty=15, left="VERY_LONG"), L(5, item=4), L(6, item=1)]
    sales = estimate_sales(prev, cur, interval_s=3600)
    assert (sales[(1, "")].sold_qty, sales[(1, "")].ambiguous_qty) == (1, 1)
    assert sales[(2, "")].sold_qty == 25
    assert (sales[(3, "")].sold_qty, sales[(3, "")].ambiguous_qty) == (0, 5)
    assert (4, "") not in sales

    # With a 20-minute gap a vanished MEDIUM listing cannot have expired.
    assert estimate_sales([L(4, item=3, qty=5, left="MEDIUM")], [], 1200)[(3, "")].sold_qty == 5
    # With a 3h gap even LONG listings may have expired.
    assert estimate_sales([L(1, left="LONG")], [], 3 * 3600)[(1, "")].ambiguous_qty == 1


def test_format_money():
    assert format_money(1234567) == "123g 45s 67c"
    assert format_money(305) == "3s 05c"
    assert format_money(7) == "7c"
    assert format_money(None) == "-"


# -- ingest / query / prune against mocked API -----------------------------------


def _house_110(forever_routes, auctions, last_modified):
    forever_routes.get(f"{BASE}/data/wow/ruleset/1/auctions/110").mock(
        return_value=httpx.Response(
            200, json={"auctions": auctions}, headers={"Last-Modified": last_modified}
        )
    )


def test_ingest_two_snapshots_estimates_sales(api, forever_routes, tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    _house_110(
        forever_routes,
        [
            {"id": 1, "item": {"id": 2589}, "buyout": 100, "quantity": 1, "time_left": "LONG"},
            {"id": 2, "item": {"id": 2589}, "buyout": 120, "quantity": 1, "time_left": "LONG"},
            {"id": 3, "item": {"id": 4306}, "buyout": 900, "quantity": 1, "time_left": "LONG"},
        ],
        "Tue, 11 Nov 2026 10:00:00 GMT",
    )
    run_snapshot(api, conn, tmp_path, kinds=["auctions"])
    first = ingest_pending(conn)
    assert first.snapshots == 2  # houses 110 and 112 (empty)

    # Second snapshot: auction 1 bought, 4306 sold out entirely.
    _house_110(
        forever_routes,
        [{"id": 2, "item": {"id": 2589}, "buyout": 120, "quantity": 1, "time_left": "LONG"}],
        "Tue, 11 Nov 2026 11:00:00 GMT",
    )
    run_snapshot(api, conn, tmp_path, kinds=["auctions"])
    assert ingest_pending(conn).snapshots == 1  # 112 unchanged -> not re-ingested

    rows = {
        (r["item_id"], r["snapshot_id"]): r
        for r in conn.execute("SELECT * FROM price_stats WHERE house_id = 110")
    }
    first_id, second_id = sorted({sid for _, sid in rows})
    assert rows[(2589, first_id)]["sold_qty"] is None  # no previous snapshot to diff
    assert rows[(2589, second_id)]["sold_qty"] == 1
    sold_out = rows[(4306, second_id)]
    assert (sold_out["quantity"], sold_out["sold_qty"]) == (0, 1)

    conn.execute(
        "INSERT INTO items (item_id, name, source, fetched_at) VALUES "
        "(2589, 'Linen Cloth', 'blizzard', 'x')"
    )
    assert [m.item_id for m in find_items(conn, "linen")] == [2589]
    hp = house_price(conn, "eu", 2589, 110)
    assert hp.min_unit == 120 and hp.quantity == 1 and hp.faction == "alliance"
    assert hp.sold_24h == 1
    assert house_price(conn, "eu", 4306, 110).quantity == 0  # sold out now


def test_prune_keeps_latest_raw_and_rolls_up_old_rows(api, forever_routes, tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    for hour in (10, 11, 12):
        _house_110(
            forever_routes,
            [
                {
                    "id": hour,
                    "item": {"id": 2589},
                    "buyout": hour,
                    "quantity": 1,
                    "time_left": "LONG",
                }
            ],
            f"Tue, 11 Nov 2026 {hour}:00:00 GMT",
        )
        run_snapshot(api, conn, tmp_path, kinds=["auctions"], house_ids={110})
    ingest_pending(conn)
    old_ts = (datetime.now(UTC) - timedelta(days=30)).isoformat(timespec="seconds")
    conn.execute("UPDATE price_stats SET ts = ? WHERE market_unit = 10", (old_ts,))

    summary = prune(conn, keep_raw=2, keep_hourly_days=14)
    assert summary.raw_files_deleted == 1
    assert len(list((tmp_path / "raw").rglob("*.json.gz"))) == 2
    assert summary.hourly_rows_rolled_up == 1
    daily = conn.execute("SELECT * FROM price_daily").fetchall()
    assert len(daily) == 1 and daily[0]["market_avg"] == 10


def test_items_sync_records_missing_and_overrides(client, mock_api, tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    mock_api.get(f"{BASE}/data/wow/item/2589").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 2589,
                "name": "Linen Cloth",
                "quality": {"type": "COMMON"},
                "item_class": {"name": "Trade Goods"},
                "sell_price": 13,
                "is_stackable": True,
            },
        )
    )
    mock_api.get(f"{BASE}/data/wow/item/720").mock(return_value=httpx.Response(404))
    mock_api.get(
        f"{BASE}/data/wow/item/9399", params__contains={"namespace": "static-classic1x-eu"}
    ).mock(return_value=httpx.Response(200, json={"id": 9399, "name": "Precision Arrow"}))
    mock_api.get(f"{BASE}/data/wow/item/9399").mock(return_value=httpx.Response(404))
    summary = sync_items(conn, client, item_ids=[2589, 720, 9399])
    assert (summary.found, summary.classic_era, summary.missing) == (1, 1, 1)
    era = conn.execute("SELECT name, source FROM items WHERE item_id = 9399").fetchone()
    assert tuple(era) == ("Precision Arrow", "classic_era")

    overrides = tmp_path / "item_overrides.csv"
    overrides.write_text("item_id,name,quality\n720,Mystery Item,RARE\n2589,Wrong Name,\n")
    assert load_overrides(conn, overrides) == 2
    names = dict(conn.execute("SELECT item_id, name FROM items"))
    assert names == {2589: "Linen Cloth", 720: "Mystery Item", 9399: "Precision Arrow"}

    # A later failed lookup must not wipe the override.
    sync_items(conn, client, item_ids=[720])
    assert (
        conn.execute("SELECT name FROM items WHERE item_id = 720").fetchone()[0] == "Mystery Item"
    )
