"""Calibrate method yields from measured play sessions.

Each yield rate (items per hour) is modelled as a Poisson rate with a Gamma prior taken
from the method file: mean = qty, sd = sd (or half the mean when no sd is given). Logged
sessions add counts and exposure, giving the conjugate posterior

    Gamma(alpha + total_count, beta + total_hours)

so a method file estimate of "14 ± 5 Dreamfoil/h" is worth about half an hour of real data
and is quickly overruled by actual sessions.

Sessions are normalized to the method's base rate by dividing out the character's
throughput multiplier at the time (class modifier, mount, gear): an hour logged by a druid
with a 1.15 modifier counts as 1.15 base-hours.

Coins/vendor income is not a count, so it uses a weighted average with the method-file
value counting as PRIOR_VENDOR_HOURS of data. Poor-quality (grey) items found in sessions
are converted to vendor income at their vendor price.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field

from .models import Confidence, ItemQty, Kind, Method

DEFAULT_REL_SD = 0.5
PRIOR_VENDOR_HOURS = 1.0
# Base-hours of sessions after which a method counts as "measured".
MEASURED_HOURS = 3.0


@dataclass
class SessionTotals:
    method_id: str
    sessions: int = 0
    hours: float = 0.0  # wall-clock active hours
    base_hours: float = 0.0  # hours x multiplier
    item_counts: dict[int, int] = field(default_factory=dict)
    vendor_copper: int = 0  # coins + grey items at vendor price


@dataclass(frozen=True)
class ItemCalibration:
    item_id: int
    name: str
    prior_mean: float
    prior_sd: float
    observed_rate: float | None  # count / base_hours
    posterior_mean: float
    posterior_sd: float
    new: bool  # seen in sessions but not in the method file


def gamma_posterior(
    prior_mean: float, prior_sd: float, count: float, exposure_h: float
) -> tuple[float, float]:
    """Posterior mean/sd of a Poisson rate under a Gamma(mean, sd) prior."""
    if prior_mean <= 0:
        # No prior information: fall back to the observed rate (flat prior).
        if exposure_h <= 0:
            return 0.0, 0.0
        return count / exposure_h, math.sqrt(max(count, 1)) / exposure_h
    sd = prior_sd if prior_sd > 0 else DEFAULT_REL_SD * prior_mean
    alpha = (prior_mean / sd) ** 2
    beta = prior_mean / sd**2
    a, b = alpha + count, beta + exposure_h
    return a / b, math.sqrt(a) / b


def load_session_totals(conn: sqlite3.Connection) -> dict[str, SessionTotals]:
    totals: dict[str, SessionTotals] = {}
    for r in conn.execute(
        "SELECT method_id, COUNT(*), SUM(duration_s), SUM(duration_s * multiplier),"
        " SUM(coins_copper) FROM sessions GROUP BY method_id"
    ):
        totals[r[0]] = SessionTotals(
            method_id=r[0],
            sessions=r[1],
            hours=r[2] / 3600,
            base_hours=r[3] / 3600,
            vendor_copper=r[4] or 0,
        )
    # Grey items -> vendor copper; everything else -> item counts. Item counts are summed
    # per session scaled by nothing: exposure (base_hours) already carries the multiplier.
    for method_id, item_id, qty, quality, sell in conn.execute(
        """
        SELECT s.method_id, si.item_id, SUM(si.qty), i.quality, i.sell_price
        FROM session_items si
        JOIN sessions s ON s.id = si.session_id
        LEFT JOIN items i ON i.item_id = si.item_id
        GROUP BY s.method_id, si.item_id
        """
    ):
        t = totals[method_id]
        if quality == "POOR":
            t.vendor_copper += qty * (sell or 0)
        else:
            t.item_counts[item_id] = qty
    return totals


def calibrate(
    method: Method, totals: SessionTotals | None, names: dict[int, str] | None = None
) -> tuple[Method, list[ItemCalibration]]:
    """Return the method with posterior yields, plus a per-item report."""
    if method.kind == Kind.CRAFT or totals is None or totals.base_hours <= 0:
        return method, []
    names = names or {}
    exposure = totals.base_hours
    report: list[ItemCalibration] = []
    yields: list[ItemQty] = []
    for iq in method.yields:
        count = totals.item_counts.get(iq.item, 0)
        mean, sd = gamma_posterior(iq.qty, iq.sd, count, exposure)
        yields.append(iq.model_copy(update={"qty": max(mean, 1e-9), "sd": sd}))
        prior_sd = iq.sd or DEFAULT_REL_SD * iq.qty
        report.append(
            ItemCalibration(
                iq.item, iq.name, iq.qty, prior_sd, count / exposure, mean, sd, new=False
            )
        )
    known = {iq.item for iq in method.yields}
    for item_id, count in sorted(totals.item_counts.items()):
        if item_id in known:
            continue
        mean, sd = gamma_posterior(0, 0, count, exposure)
        name = names.get(item_id, f"item {item_id}")
        yields.append(ItemQty(item=item_id, name=name, qty=mean, sd=sd))
        report.append(ItemCalibration(item_id, name, 0, 0, count / exposure, mean, sd, new=True))

    vendor = (method.vendor_copper_per_hour * PRIOR_VENDOR_HOURS + totals.vendor_copper) / (
        PRIOR_VENDOR_HOURS + exposure
    )
    calibrated = method.model_copy(
        update={
            "yields": yields,
            "vendor_copper_per_hour": round(vendor),
            "calibration_hours": exposure,
            "calibration_sessions": totals.sessions,
            "confidence": (
                Confidence.MEASURED if exposure >= MEASURED_HOURS else method.confidence
            ),
        }
    )
    return calibrated, report


def apply_calibration(conn: sqlite3.Connection, methods: list[Method]) -> list[Method]:
    totals = load_session_totals(conn)
    if not totals:
        return methods
    names = dict(conn.execute("SELECT item_id, name FROM items WHERE name IS NOT NULL"))
    return [calibrate(m, totals.get(m.id), names)[0] for m in methods]
