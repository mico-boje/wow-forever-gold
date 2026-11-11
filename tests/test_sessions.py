from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from wowgold import db
from wowgold.luatable import LuaParseError, parse_saved_variables
from wowgold.methods.calibration import (
    apply_calibration,
    calibrate,
    gamma_posterior,
    load_session_totals,
)
from wowgold.methods.models import Method, load_methods
from wowgold.planner.profile import CharacterProfile
from wowgold.sessions import (
    SessionTimer,
    log_session,
    parse_coins,
    parse_item_spec,
)

METHODS_DIR = Path(__file__).parent.parent / "data" / "methods"


def herb_method(**kw) -> Method:
    base = {
        "id": "herb",
        "name": "Herbs",
        "kind": "gather",
        "yields": [{"item": 1, "name": "Herb", "qty": 10, "sd": 5}],
        "vendor_copper_per_hour": 1000,
        "class_modifiers": {"druid": 2.0},
    }
    return Method.model_validate({**base, **kw})


def mage(**kw) -> CharacterProfile:
    return CharacterProfile(**{"class": "mage", "level": 60, "faction": "horde", **kw})


# -- Lua SavedVariables parser ---------------------------------------------------------


def test_parse_saved_variables():
    text = r"""
WowGoldLoggerDB = {
    ["sessions"] = {
        {
            ["method"] = "herb_felwood",
            ["items"] = { [13463] = 14, [13465] = 3, },
            ["zone"] = "Fel\"wood\\",
            ["active"] = 2700.5,
            ["done"] = true,
            ["none"] = nil,
        }, -- [1]
    },
    ["hex"] = 0x1F, ["neg"] = -2, ["exp"] = 1e3,
}
--[[ block
comment ]]
Other = { 1, 2; 3 }
"""
    out = parse_saved_variables(text)
    db_ = out["WowGoldLoggerDB"]
    s = db_["sessions"][0]
    assert s["items"] == {13463: 14, 13465: 3}
    assert s["zone"] == 'Fel"wood\\'
    assert s["active"] == 2700.5 and s["done"] is True and s["none"] is None
    assert (db_["hex"], db_["neg"], db_["exp"]) == (31, -2, 1000.0)
    assert out["Other"] == [1, 2, 3]
    with pytest.raises(LuaParseError):
        parse_saved_variables("X = { [1] = }")
    with pytest.raises(LuaParseError):
        parse_saved_variables("X = os.execute('rm -rf /')")


# -- calibration math --------------------------------------------------------------------


def test_gamma_posterior():
    # Prior 10 ± 5 /h is worth 0.4h of data (beta = 10/25); data dominates quickly.
    mean, sd = gamma_posterior(10, 5, count=0, exposure_h=0)
    assert (mean, sd) == pytest.approx((10, 5))
    mean, _ = gamma_posterior(10, 5, count=200, exposure_h=10)  # observed 20/h
    assert 19.5 < mean < 20.0
    # No prior: plain observed rate.
    assert gamma_posterior(0, 0, count=6, exposure_h=2)[0] == 3
    # sd 0 in the method file means "use half the mean".
    assert gamma_posterior(10, 0, 0, 0) == pytest.approx((10, 5))


def test_calibrate_normalizes_by_multiplier_and_adds_new_items(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    m = herb_method()
    conn.execute(
        "INSERT INTO items (item_id, name, quality, sell_price, source, fetched_at)"
        " VALUES (99, 'Grey Junk', 'POOR', 50, 'blizzard', 'x'),"
        " (7, 'Lucky Gem', 'UNCOMMON', 0, 'blizzard', 'x')"
    )
    # A druid (2x modifier) gathers 60 herbs in 1h -> 30 per base-hour.
    log_session(
        conn,
        m,
        mage(**{"class": "druid"}),
        duration_s=3600,
        items={1: 60, 99: 10, 7: 1},
        coins_copper=2000,
    )
    totals = load_session_totals(conn)["herb"]
    assert totals.base_hours == pytest.approx(2.0)
    assert totals.vendor_copper == 2000 + 10 * 50  # greys become vendor income
    assert 7 in totals.item_counts and 99 not in totals.item_counts

    cal, report = calibrate(m, totals, {7: "Lucky Gem"})
    herb = next(r for r in report if r.item_id == 1)
    assert herb.observed_rate == pytest.approx(30)
    assert 25 < herb.posterior_mean < 30  # pulled slightly towards the 10/h estimate
    gem = next(r for r in report if r.item_id == 7)
    assert gem.new and gem.posterior_mean == pytest.approx(0.5)
    assert {y.item for y in cal.yields} == {1, 7}
    # Vendor: (1000*1h prior + 2500) / (1 + 2) base-hours.
    assert cal.vendor_copper_per_hour == round(3500 / 3)
    assert cal.calibration_hours == pytest.approx(2) and cal.confidence == "estimate"

    log_session(conn, m, mage(), duration_s=3600, items={1: 25})
    cal2 = apply_calibration(conn, [m])[0]
    assert cal2.calibration_sessions == 2 and cal2.confidence == "measured"  # 3 base-hours


def test_crafts_and_bad_sessions_are_rejected(tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    methods = {m.id: m for m in load_methods(METHODS_DIR)}
    with pytest.raises(ValueError, match="crafting"):
        log_session(conn, methods["craft_thorium_bar"], mage(), duration_s=60, items={})
    with pytest.raises(ValueError, match="positive"):
        log_session(conn, herb_method(), mage(), duration_s=0, items={})


# -- parsing helpers + timer ---------------------------------------------------------------


def test_parse_coins_and_items(tmp_path):
    assert parse_coins("3g 20s 5c") == 32005
    assert parse_coins("1250") == 1250
    assert parse_coins("15s") == 1500
    with pytest.raises(ValueError):
        parse_coins("lots")
    conn = db.connect(tmp_path / "t.sqlite3")
    conn.execute(
        "INSERT INTO items (item_id, name, source, fetched_at) VALUES"
        " (13463, 'Dreamfoil', 'blizzard', 'x'), (5, 'Twin', 'blizzard', 'x'),"
        " (6, 'Twin', 'blizzard', 'x')"
    )
    assert parse_item_spec(conn, "dreamfoil:14") == (13463, 14)
    assert parse_item_spec(conn, "13463=3") == (13463, 3)
    with pytest.raises(ValueError, match="2 items"):
        parse_item_spec(conn, "Twin:1")
    with pytest.raises(ValueError, match="item:qty"):
        parse_item_spec(conn, "Dreamfoil")


def test_timer_excludes_paused_time(tmp_path):
    t0 = datetime(2026, 11, 11, 12, tzinfo=UTC)
    timer = SessionTimer(tmp_path / "active.json")
    timer.start("herb", now=t0)
    with pytest.raises(ValueError, match="already running"):
        timer.start("herb", now=t0)
    timer.pause(now=t0 + timedelta(minutes=10))
    timer.resume(now=t0 + timedelta(minutes=40))
    active = timer.get()
    assert active.active_seconds(now=t0 + timedelta(minutes=60)) == 30 * 60
    timer.stop()
    assert timer.get() is None


# -- CLI end-to-end -------------------------------------------------------------------------


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from wowgold.config import get_settings

    monkeypatch.setenv("WOWGOLD_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WOWGOLD_METHODS_DIR", str(METHODS_DIR))
    monkeypatch.setenv("WOWGOLD_HOUSES", "[]")
    monkeypatch.setenv("BLIZZARD_CLIENT_ID", "")
    get_settings.cache_clear()
    conn = db.connect(tmp_path / "wowgold.sqlite3")
    conn.execute(
        "INSERT INTO items (item_id, name, source, fetched_at) VALUES"
        " (13463, 'Dreamfoil', 'blizzard', 'x')"
    )
    conn.commit()
    conn.close()
    yield tmp_path
    get_settings.cache_clear()


def run(*args):
    from wowgold import cli

    return CliRunner().invoke(cli.app, list(args))


def test_cli_profile_session_and_show(cli_env):
    r = run("session", "log", "herb_felwood", "--minutes", "60", "-i", "Dreamfoil:20")
    assert r.exit_code != 0 and "profile" in r.output  # needs a saved profile first

    r = run("profile", "set", "-c", "druid", "-l", "60", "-f", "horde", "-p", "herbalism:300")
    assert r.exit_code == 0, r.output
    assert "level 60 horde druid" in run("profile", "show").output

    r = run(
        "session",
        "log",
        "herb_felwood",
        "--minutes",
        "60",
        "-i",
        "Dreamfoil:20",
        "-i",
        "13465:5",
        "--coins",
        "1g",
    )
    assert r.exit_code == 0, r.output
    assert "Logged session #1" in r.output

    r = run("session", "start", "herb_felwood")
    assert r.exit_code == 0, r.output
    assert "herb_felwood" in run("session", "status").output
    r = run("session", "stop", "-i", "Nonexistent:3")
    assert r.exit_code != 0  # bad item: timer must survive
    assert run("session", "cancel").exit_code == 0

    r = run("methods", "show", "herb_felwood")
    assert r.exit_code == 0, r.output
    assert "1 session(s)" in r.output and "Dreamfoil" in r.output
    assert "Mountain Silversage" in r.output and "(new)" not in r.output  # 13465 is known
    assert "86s 96c" in r.output  # 1g coins over 1.15 base-hours (druid modifier)

    listing = run("session", "list")
    assert "herb_felwood" in listing.output
    assert run("session", "delete", "1").exit_code == 0
    assert "No sessions" in run("session", "list").output
