"""Where prices are heading, from the collector's hourly history.

Per item, over the last 7 days (the hourly rows are kept for 14):

- **drift**: the fitted price change per day (least squares on the log of the median price per
  time block, so single odd snapshots don't move it);
- **change_24h / change_7d**: today's price against the same hours one day / one week ago;
- **swing**: how far the hourly prices scatter around that trend (robust spread).

Valuation uses the drift *asymmetrically*: a falling price is projected to when you'd sell
(`HORIZON_DAYS` ahead), a rising one is not (launch spikes reverse). The drift only counts in
proportion to how much history it rests on: nothing under `MIN_TRUST_H`, fully from
`FULL_TRUST_H`.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median

WINDOW_DAYS = 7
HORIZON_DAYS = 2.0  # farm or craft now, sold over the next ~2 days
MIN_TRUST_H = 24.0  # below a day, a slope is mostly intraday noise
FULL_TRUST_H = 72.0
MIN_FACTOR = 0.25  # never project below a quarter of today's price
FALLING_PER_DAY = -0.05  # flag "falling" at 5%/day or worse
SWING_FLAG = 0.25  # flag "swings" at ±25% around the trend
MIN_BLOCKS = 4


@dataclass(frozen=True)
class Trend:
    item_id: int
    history_h: float  # span of this item's price history in the window
    drift: float | None  # fitted change per day (-0.1 = falling 10%/day)
    change_24h: float | None
    change_7d: float | None
    swing: float | None  # typical scatter around the trend (0.2 = ±20%)
    blocks: int  # time blocks the fit used

    @property
    def trust(self) -> float:
        """0..1: how much of the drift to believe, from how much history it rests on."""
        return max(0.0, min(1.0, (self.history_h - MIN_TRUST_H) / (FULL_TRUST_H - MIN_TRUST_H)))

    @property
    def sell_factor(self) -> float:
        """Multiplier for today's price when valuing what you'll sell (≤ 1)."""
        if not self.drift or self.drift >= 0 or not self.trust:
            return 1.0
        per_day = math.log1p(max(self.drift, -0.99)) * self.trust
        return max(MIN_FACTOR, math.exp(per_day * HORIZON_DAYS))

    @property
    def falling(self) -> bool:
        return self.trust > 0 and self.drift is not None and self.drift <= FALLING_PER_DAY

    @property
    def swings(self) -> bool:
        return (
            self.trust > 0
            and self.swing is not None
            and self.swing >= SWING_FLAG
            and self.blocks >= 2 * MIN_BLOCKS
        )

    def label(self) -> str:
        """Short arrow label for tables: ↓8%/d, ↑3%/d, or ''."""
        if self.drift is None or self.trust == 0:
            return ""
        pct = self.drift * 100
        if abs(pct) < 1:
            return "→"
        return f"{'↑' if pct > 0 else '↓'}{abs(pct):.0f}%/d"


def _block_hours(history_h: float) -> int:
    return 1 if history_h < 24 else 3 if history_h < 72 else 6


def _fit(points: list[tuple[float, float]]) -> tuple[float | None, float | None, int]:
    """(drift per day, swing, blocks) from (hours, price) points."""
    span = points[-1][0] - points[0][0]
    size = _block_hours(span)
    blocks: dict[int, list[float]] = {}
    for t, p in points:
        blocks.setdefault(int(t // size), []).append(p)
    xs = [(k + 0.5) * size / 24 for k in sorted(blocks)]  # days
    ys = [math.log(median(blocks[k])) for k in sorted(blocks)]
    n = len(xs)
    if n < MIN_BLOCKS:
        return None, None, n
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None, n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    # Swing from the hourly prices (not the block medians): intraday swings matter for when
    # you list.
    resid = [math.log(p) - (my + slope * (t / 24 - mx)) for t, p in points]
    mad = median(abs(r) for r in resid) * 1.4826
    return math.expm1(slope), math.expm1(mad), n


def _change(points: list[tuple[float, float]], now_h: float, back_h: float) -> float | None:
    """Median price of the last 3 hours against the median ±3 hours around `back_h` ago."""
    recent = [p for t, p in points if t >= now_h - 3]
    then = [p for t, p in points if abs(t - (now_h - back_h)) <= 3]
    if not recent or not then:
        return None
    return median(recent) / median(then) - 1


def load_trends(
    conn: sqlite3.Connection,
    region: str,
    house_ids: list[int],
    item_ids: set[int],
    *,
    now: datetime | None = None,
) -> dict[int, Trend]:
    if not house_ids or not item_ids:
        return {}
    now = now or datetime.now(UTC)
    start = now - timedelta(days=WINDOW_DAYS)
    house_marks = ",".join("?" * len(house_ids))
    ids = sorted(item_ids)
    series: dict[int, list[tuple[float, float]]] = {}
    # SQLite caps bound parameters; chunk the item list.
    for i in range(0, len(ids), 5000):
        chunk = ids[i : i + 5000]
        item_marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""
            SELECT item_id, substr(ts, 1, 13) AS hour,
                   SUM(market_unit * quantity) * 1.0 / SUM(quantity)
            FROM price_stats
            WHERE region = ? AND house_id IN ({house_marks}) AND item_id IN ({item_marks})
              AND ts >= ? AND quantity > 0 AND market_unit > 0
            GROUP BY item_id, hour
            """,
            (region, *house_ids, *chunk, start.isoformat(timespec="seconds")),
        )
        for item_id, hour, price in rows:
            at = datetime.fromisoformat(hour + ":00:00+00:00")
            series.setdefault(item_id, []).append(((at - start).total_seconds() / 3600, price))
    now_h = (now - start).total_seconds() / 3600
    out = {}
    for item_id, points in series.items():
        points.sort()
        drift, swing, blocks = _fit(points)
        out[item_id] = Trend(
            item_id=item_id,
            history_h=points[-1][0] - points[0][0],
            drift=drift,
            change_24h=_change(points, now_h, 24),
            change_7d=_change(points, now_h, 24 * 7),
            swing=swing,
            blocks=blocks,
        )
    return out


# -- how much history the numbers rest on -------------------------------------------------------


@dataclass(frozen=True)
class Maturity:
    history_h: float  # since the collector's first snapshot of these houses
    sales_h: float  # hours of sales diffs in the last 24h

    @property
    def level(self) -> str:
        h = self.history_h
        return "first-look" if h < 24 else "early" if h < 72 else "settling" if h < 168 else "solid"

    def describe(self) -> str:
        h = self.history_h
        span = f"{h:.0f} hours" if h < 48 else f"{h / 24:.1f} days"
        return {
            "first-look": f"Only {span} of price history: prices are a snapshot and "
            "price trends aren't used yet (from 1 day).",
            "early": f"{span} of price history: trends count partly (fully from 3 days); "
            "a falling price is valued lower, a rising one at today's price.",
            "settling": f"{span} of price history: trends count fully; week-on-week changes "
            "appear after 7 days.",
            "solid": f"{span} of price history.",
        }[self.level] + (
            ""
            if self.sales_h >= 12
            else f" Sales per day are estimated from listings until 12 hours of sales are "
            f"measured ({self.sales_h:.0f} so far)."
        )


def load_maturity(
    conn: sqlite3.Connection,
    region: str,
    house_ids: list[int],
    sales_h: float,
    *,
    now: datetime | None = None,
) -> Maturity:
    if not house_ids:
        return Maturity(0.0, sales_h)
    marks = ",".join("?" * len(house_ids))
    first = conn.execute(
        f"SELECT MIN(fetched_at) FROM snapshots WHERE region = ? AND house_id IN ({marks}) "
        "AND outcome = 'stored'",
        (region, *house_ids),
    ).fetchone()[0]
    if not first:
        return Maturity(0.0, sales_h)
    hours = ((now or datetime.now(UTC)) - datetime.fromisoformat(first)).total_seconds() / 3600
    return Maturity(max(0.0, hours), sales_h)
