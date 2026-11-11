from __future__ import annotations

import gzip
import json
from pathlib import Path

import httpx

from tests.conftest import BASE
from wowgold import db
from wowgold.probe import Prober, save_report
from wowgold.snapshot import run_snapshot


def test_snapshot_dedupes_houses_and_stores_raw(api, forever_routes, tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    summary = run_snapshot(api, conn, tmp_path)

    assert summary.houses == 2  # same houses under both rulesets -> fetched once
    assert summary.stored == 4 and not summary.errors
    # Each house was fetched through ruleset 1 only.
    assert forever_routes.calls.call_count  # sanity
    rulesets = {r["house_id"]: r["rulesets"] for r in db.list_houses(conn, "eu")}
    assert set(rulesets[110].split(",")) == {"1", "2"}

    rows = conn.execute("SELECT * FROM snapshots ORDER BY house_id, kind").fetchall()
    by_key = {(r["house_id"], r["kind"]): r for r in rows}
    assert by_key[(110, "auctions")]["n_auctions"] == 1
    assert by_key[(112, "auctions")]["n_auctions"] == 0  # empty house -> zero, not an error
    stored = json.loads(gzip.decompress(Path(by_key[(110, "auctions")]["path"]).read_bytes()))
    assert stored["auctions"][0]["item"]["id"] == 2589


def test_second_snapshot_uses_if_modified_since_and_hash(api, forever_routes, tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    run_snapshot(api, conn, tmp_path)

    house_route = forever_routes.get(f"{BASE}/data/wow/ruleset/1/auctions/110")
    house_route.mock(return_value=httpx.Response(304))
    summary = run_snapshot(api, conn, tmp_path)

    assert (
        house_route.calls.last.request.headers["If-Modified-Since"]
        == "Tue, 11 Nov 2026 10:00:00 GMT"
    )
    assert summary.not_modified == 1
    assert summary.unchanged == 3  # identical bodies are not re-written
    assert summary.stored == 0
    files = list((tmp_path / "raw").rglob("*.json.gz"))
    assert len(files) == 4


def test_house_errors_are_reported(api, forever_routes, tmp_path):
    forever_routes.get(f"{BASE}/data/wow/ruleset/1/auctions/112").mock(
        return_value=httpx.Response(404)
    )
    conn = db.connect(tmp_path / "t.sqlite3")
    summary = run_snapshot(api, conn, tmp_path, house_ids={112}, kinds=["auctions"])
    assert summary.errors == ["house 112 auctions: HTTP 404"]


def test_set_faction(api, forever_routes, tmp_path):
    conn = db.connect(tmp_path / "t.sqlite3")
    run_snapshot(api, conn, tmp_path, kinds=["auctions"])
    assert db.set_faction(conn, "eu", 110, "alliance")
    assert not db.set_faction(conn, "eu", 999, "horde")
    factions = {r["house_id"]: r["faction"] for r in db.list_houses(conn, "eu")}
    assert factions == {110: "alliance", 112: "blackwater"}  # derived from house names
    assert db.set_faction(conn, "eu", 112, "horde")  # manual override sticks across snapshots
    run_snapshot(api, conn, tmp_path, kinds=["auctions"])
    assert {r["house_id"]: r["faction"] for r in db.list_houses(conn, "eu")}[112] == "horde"


def test_probe_records_working_and_failing_endpoints(client, forever_routes, tmp_path):
    forever_routes.get(url__regex=rf"{BASE}/data/wow/(realm|connected-realm|auctions)/.*").mock(
        return_value=httpx.Response(404)
    )
    forever_routes.get(
        url__regex=rf"{BASE}/data/wow/(item|media|profession|creature|playable|region|token).*"
    ).mock(return_value=httpx.Response(200, json={"id": 1}))
    forever_routes.get(url__regex=rf"{BASE}/data/wow/item-class/.*").mock(
        return_value=httpx.Response(403)
    )

    report = Prober(client, "1.60.1_70399").run()
    status = {r.name: r.status for r in report.results}

    assert status["realm index"] == 404
    assert status["item (static)"] == 200
    assert status["auction index /"] == 200
    assert status["auction index /index"] == 404
    assert status["house 110 auctions"] == 200
    versioned = next(r for r in report.results if r.name == "item (static versioned)")
    assert versioned.namespace == "static-1.60.1_70399-forever-eu"

    path = save_report(report, tmp_path)
    assert json.loads(path.read_text())["region"] == "eu"
