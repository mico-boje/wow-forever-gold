"""House-dependent AH cut, confirming your house, and Forever vendor prices."""

from __future__ import annotations

import gzip
import json

import pytest
from fastapi.testclient import TestClient

from wowgold import db
from wowgold.config import Settings
from wowgold.crafting.profit import CraftContext, buy_price
from wowgold.market.myhouse import (
    find_listing,
    house_source,
    load_my_houses,
    set_my_house,
)
from wowgold.planner.pricing import (
    AH_CUT,
    NEUTRAL_AH_CUT,
    ItemPrice,
    PriceBook,
    ah_cut,
    build_price_book,
    cap_prices,
    pick_houses,
)
from wowgold.web.app import create_app
from wowgold.wowhead.vendors import load_vendor_offers, offers_from_capture

LINEN = 2589


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "wowgold.sqlite3")
    now = "2026-11-11T12:00:00+00:00"
    for hid, faction, name in (
        (113, "horde", "Horde Auction House"),
        (114, "horde", "Horde Auction House"),
        (116, "blackwater", "Blackwater Auction House"),
    ):
        c.execute(
            "INSERT INTO houses (region, house_id, name, faction, first_seen, last_seen) "
            "VALUES ('eu', ?, ?, ?, ?, ?)",
            (hid, name, faction, now, now),
        )
    # raw snapshots: house 114 has the fingerprint listing (1 Linen at 12g 34s 56c)
    for hid, auctions in (
        (113, [{"id": 1, "item": {"id": LINEN}, "quantity": 20, "unit_price": 1500}]),
        (114, [{"id": 2, "item": {"id": LINEN}, "quantity": 1, "unit_price": 123456}]),
        (116, []),
    ):
        path = tmp_path / f"raw-{hid}.json.gz"
        path.write_bytes(gzip.compress(json.dumps({"auctions": auctions}).encode()))
        c.execute(
            "INSERT INTO snapshots (region, ruleset_id, house_id, kind, fetched_at, http_status, "
            "outcome, path) VALUES ('eu', 1, ?, 'commodities', ?, 200, 'stored', ?)",
            (hid, now, str(path)),
        )
    c.commit()
    yield c
    c.close()


def test_cut_depends_on_house(conn):
    assert ah_cut("horde") == AH_CUT and ah_cut("blackwater") == NEUTRAL_AH_CUT
    assert build_price_book(conn, "eu", [113], set()).cut == AH_CUT
    assert build_price_book(conn, "eu", [116], set()).cut == NEUTRAL_AH_CUT
    p = ItemPrice(1, "x", 10000, 5, 0, None, None, 1.0, False, NEUTRAL_AH_CUT)
    assert p.ah_net == 8500


def test_cap_prices():
    book = PriceBook(
        house_ids=[1],
        prices={
            1: ItemPrice(1, "Fel Crystal", 600000, 9, 0, None, None, 1.0, False),
            2: ItemPrice(2, "Herb", 30000, 9, 0, None, None, 1.0, False),
        },
    )
    assert cap_prices(book, {1: 450000, 2: 450000, 3: 1}) == [1]
    assert book.get(1).market == 450000 and book.get(2).market == 30000


def test_find_listing_and_my_house(conn, tmp_path):
    (match,) = find_listing(conn, "eu", LINEN, 123456)
    assert (match.house_id, match.faction, match.quantity) == (114, "horde", 1)
    assert find_listing(conn, "eu", LINEN, 999) == []
    # a stack listed at a total buyout also matches
    assert [m.house_id for m in find_listing(conn, "eu", LINEN, 1500)] == [113]

    assert load_my_houses(tmp_path, "eu") == {}
    set_my_house(tmp_path, "eu", "horde", 114)
    assert load_my_houses(tmp_path, "eu") == {"horde": 114}
    assert load_my_houses(tmp_path, "us") == {}
    mine = load_my_houses(tmp_path, "eu")
    assert pick_houses(conn, "eu", "horde", None, None, mine) == [114]
    assert pick_houses(conn, "eu", "horde", None, [113], mine) == [113]  # .env wins
    assert pick_houses(conn, "eu", "horde", [116], None, mine) == [116]  # explicit wins
    assert house_source([114], "horde", explicit=None, configured=None, mine=mine) == "confirmed"
    assert house_source([113], "horde", explicit=None, configured=None, mine={}) == "guess"
    set_my_house(tmp_path, "eu", "horde", None)
    assert load_my_houses(tmp_path, "eu") == {}


def _sold_by(cost, stock=-1, stack=1):
    return {
        "listviews": {
            "sold-by": {
                "data": [
                    {
                        "id": 263384,
                        "name": "Mister Graphed",
                        "cost": cost,
                        "stack": stack,
                        "stock": stock,
                    }
                ]
            }
        }
    }


def test_vendor_offers(tmp_path):
    (offer,) = offers_from_capture(_sold_by([[450000]]), 248819)
    assert (offer.price, offer.vendor, offer.limited) == (450000, "Mister Graphed", False)
    assert offers_from_capture(_sold_by([[0, [[123, 5]]]]), 1) == []  # costs items too
    assert offers_from_capture(_sold_by([[1000]], stack=5), 1)[0].price == 200
    assert offers_from_capture(_sold_by([[1000]], stock=3), 1)[0].limited

    caps = tmp_path / "wowhead" / "captures"
    caps.mkdir(parents=True)
    (caps / "item-248819.json").write_text(json.dumps(_sold_by([[450000]])))
    offers = load_vendor_offers(tmp_path)
    assert offers[248819].price == 450000

    ctx = CraftContext(frozenset(), {}, {}).with_offers(offers)
    book = PriceBook(
        house_ids=[1],
        prices={248819: ItemPrice(248819, "Fel Crystal", 500000, 3, 0, None, None, 1.0, False)},
    )
    assert buy_price(248819, book, ctx) == (450000.0, "vendor (Mister Graphed)", 3)


def test_houses_page(conn, tmp_path):
    client = TestClient(
        create_app(Settings(wowgold_data_dir=tmp_path, wowgold_region="eu", wowgold_houses=[]))
    )
    page = client.get("/houses").text
    assert "Find your house" in page and "15%" in page and 'class="on">Houses' in page
    found = client.get("/houses?item=2589&price=12g34s56c").text
    assert "House <strong>114</strong>" in found and "This is mine" in found
    assert "No house has" in client.get("/houses?item=2589&price=1g").text
    assert "read coins" in client.get("/houses?item=2589&price=lots").text

    bad = client.post(
        "/houses/mine", data={"house_id": "114"}, headers={"Origin": "https://evil.example"}
    )
    assert bad.status_code == 403
    ok = client.post("/houses/mine", data={"house_id": "114"}, follow_redirects=False)
    assert ok.status_code == 303
    assert load_my_houses(tmp_path, "eu") == {"horde": 114}
    assert '<span class="chip ok">yours</span>' in client.get("/houses").text
