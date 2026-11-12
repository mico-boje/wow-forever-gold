"""Price trends, trend-adjusted selling prices, and how much history the numbers rest on."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from wowgold import db
from wowgold.market.trends import HORIZON_DAYS, Maturity, Trend, load_trends
from wowgold.planner.pricing import build_price_book, trend_flags
from wowgold.web.app import sell_rate

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)
FALLING, NOISY, NEW, STEADY = 1, 2, 3, 4


def _db(tmp_path, series: dict[int, list[tuple[float, int]]], kind="commodities"):
    """series: item -> [(hours before NOW, price)]; one snapshot per hour."""
    conn = db.connect(tmp_path / "t.sqlite3")
    by_hour: dict[float, list[tuple[int, int]]] = {}
    for item, pts in series.items():
        for h, price in pts:
            by_hour.setdefault(h, []).append((item, price))
    for sid, h in enumerate(sorted(by_hour, reverse=True), start=1):
        ts = (NOW - timedelta(hours=h)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO snapshots (id, region, ruleset_id, house_id, kind, fetched_at, "
            "http_status, outcome, ingested_at, n_auctions) "
            "VALUES (?, 'eu', 1, 1, ?, ?, 200, 'stored', ?, 1)",
            (sid, kind, ts, ts),
        )
        for item, price in by_hour[h]:
            conn.execute(
                "INSERT INTO price_stats VALUES (?, 'eu', 1, ?, ?, ?, '', 1, 50, ?, ?, ?, ?, ?, "
                "1, 0, 3600)",
                (sid, kind, ts, item, price, price, price, price, price),
            )
    return conn


def _falling(days=4, per_day=-0.10, start=10000):
    hours = int(days * 24)
    return [(h, round(start * (1 + per_day) ** ((hours - h) / 24))) for h in range(hours, -1, -1)]


def test_trend_fit(tmp_path):
    noisy = [(h, 10000 if h % 6 < 3 else 16000) for h in range(96, -1, -1)]
    conn = _db(
        tmp_path,
        {
            FALLING: _falling(),
            NOISY: noisy,
            NEW: [(h, 5000) for h in range(10, -1, -1)],
            STEADY: [(h, 7000) for h in range(96, -1, -1)],
        },
    )
    trends = load_trends(conn, "eu", [1], {FALLING, NOISY, NEW, STEADY}, now=NOW)

    t = trends[FALLING]
    assert t.drift == pytest.approx(-0.10, abs=0.005)
    assert t.change_24h == pytest.approx(-0.10, abs=0.01)
    assert t.change_7d is None  # only 4 days of history
    assert t.trust == 1 and t.falling and not t.swings
    assert t.sell_factor == pytest.approx(0.9**HORIZON_DAYS, rel=0.01)
    assert t.label() == "↓10%/d"

    n = trends[NOISY]
    assert abs(n.drift) < 0.03 and n.swings and not n.falling

    new = trends[NEW]  # 10 hours: too little to use
    assert new.trust == 0 and new.sell_factor == 1 and new.label() == "" and not new.falling

    assert trends[STEADY].label() == "→" and trends[STEADY].sell_factor == 1


def test_rising_prices_are_not_projected_up():
    t = Trend(1, history_h=100, drift=0.2, change_24h=0.2, change_7d=None, swing=0.05, blocks=16)
    assert t.sell_factor == 1 and t.label() == "↑20%/d"


def test_partial_trust_and_floor():
    half = Trend(1, history_h=48, drift=-0.2, change_24h=None, change_7d=None, swing=0, blocks=16)
    assert half.trust == pytest.approx(0.5)
    assert half.sell_factor == pytest.approx(math.exp(math.log(0.8) * 0.5 * HORIZON_DAYS))
    crash = Trend(1, history_h=100, drift=-0.9, change_24h=None, change_7d=None, swing=0, blocks=16)
    assert crash.sell_factor == 0.25


def test_price_book_sells_at_trend_price(tmp_path):
    conn = _db(tmp_path, {FALLING: _falling(), STEADY: [(h, 7000) for h in range(96, -1, -1)]})
    conn.execute(
        "INSERT INTO items (item_id, name, sell_price, source, fetched_at) "
        "VALUES (1, 'Falling Herb', 1, 'blizzard', 'x')"
    )
    book = build_price_book(conn, "eu", [1], {FALLING, STEADY}, now=NOW)
    p = book.get(FALLING)
    today = _falling()[-1][1]
    assert p.market == today  # buying: today's price
    assert p.ah_net == round(today * p.sell_factor * 0.95)  # selling: where the trend goes
    assert book.get(STEADY).ah_net == round(7000 * 0.95)
    assert book.maturity.level == "settling" and book.maturity.history_h == pytest.approx(96)

    (flag,) = trend_flags(book, [FALLING, STEADY])
    assert flag.startswith("prices falling: Falling Herb -10%/day")

    r = sell_rate(book, FALLING)
    assert r.trend_class == "down" and "valued at 81%" in r.trend_title()
    flat = build_price_book(conn, "eu", [1], {FALLING}, now=NOW, trends=False)
    assert flat.get(FALLING).ah_net == round(today * 0.95)


def test_sales_coverage_counts_each_feed_once(tmp_path):
    conn = _db(tmp_path, {STEADY: [(h, 7000) for h in range(10, 0, -1)]})
    # the same 10 hours again, as the house's auctions feed
    for sid in range(1, 11):
        ts = (NOW - timedelta(hours=11 - sid)).isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO snapshots (id, region, ruleset_id, house_id, kind, fetched_at, "
            "http_status, outcome, ingested_at, n_auctions) "
            "VALUES (?, 'eu', 1, 1, 'auctions', ?, 200, 'stored', ?, 1)",
            (100 + sid, ts, ts),
        )
        conn.execute(
            "INSERT INTO price_stats VALUES (?, 'eu', 1, 'auctions', ?, 99, '', 1, 1, 5, 5, 5, "
            "5, 5, 0, 0, 3600)",
            (100 + sid, ts),
        )
    book = build_price_book(conn, "eu", [1], {STEADY}, now=NOW)
    assert book.sales_coverage_h == pytest.approx(10)  # not 20
    assert book.get(STEADY).sold_per_day == pytest.approx(24)  # 1 per hour


def test_maturity_text():
    assert Maturity(9, 9).level == "first-look"
    assert "estimated from listings" in Maturity(9, 9).describe()
    assert "estimated" not in Maturity(30, 20).describe()
    assert Maturity(200, 24).level == "solid"
