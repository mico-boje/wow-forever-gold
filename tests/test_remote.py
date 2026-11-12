"""Snapshots collected on GitHub Actions: artifact import and sync."""

from __future__ import annotations

import gzip
import io
import json
import zipfile
from datetime import UTC, datetime

import httpx
import pytest
import respx

from wowgold import db
from wowgold.remote import API, WORKFLOW_FILE, import_artifact, sync

REPO = "me/wowgold"


def _artifact(snaps: list[tuple[str, int, str, str]], *, path_override=None) -> bytes:
    """snaps: (fetched_at, house, kind, sha)."""
    buf = io.BytesIO()
    manifest = {
        "version": 1,
        "houses": [
            {
                "region": "eu",
                "house_id": 113,
                "name": "Horde Auction House",
                "faction": "horde",
                "first_seen": "x",
                "last_seen": "2026-11-12T00:00:00+00:00",
            }
        ],
        "rulesets": [{"region": "eu", "house_id": 113, "ruleset_id": 1, "ruleset_name": "Normal"}],
        "snapshots": [],
    }
    with zipfile.ZipFile(buf, "w") as zf:
        for at, house, kind, sha in snaps:
            path = path_override or f"raw/eu/{house}/{kind}/{at[:13]}-{sha}.json.gz"
            zf.writestr(path, gzip.compress(json.dumps({"auctions": []}).encode()))
            manifest["snapshots"].append(
                {
                    "region": "eu",
                    "ruleset_id": 1,
                    "house_id": house,
                    "kind": kind,
                    "fetched_at": at,
                    "http_status": 200,
                    "outcome": "stored",
                    "last_modified": None,
                    "sha256": sha,
                    "n_auctions": 0,
                    "bytes": 10,
                    "path": path,
                }
            )
        zf.writestr("manifest.json", json.dumps(manifest))
    return buf.getvalue()


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "w.sqlite3")
    yield c
    c.close()


def test_import_in_order_skipping_older_and_unchanged(conn, tmp_path):
    a = _artifact(
        [
            ("2026-11-12T02:40:00+00:00", 113, "commodities", "bbb"),
            ("2026-11-12T01:40:00+00:00", 113, "commodities", "aaa"),  # out of order in the file
        ]
    )
    r = import_artifact(conn, tmp_path, a)
    assert (r.stored, r.unchanged, r.skipped) == (2, 0, 0)
    rows = conn.execute("SELECT fetched_at, path FROM snapshots ORDER BY id").fetchall()
    assert [x[0][11:16] for x in rows] == ["01:40", "02:40"]  # added oldest first
    assert (tmp_path / "raw/eu/113/commodities/2026-11-12T02-bbb.json.gz").exists()
    assert conn.execute("SELECT faction FROM houses").fetchone()[0] == "horde"

    # the same artifact again, and an hour with identical data
    assert import_artifact(conn, tmp_path, a).skipped == 2
    same = import_artifact(
        conn, tmp_path, _artifact([("2026-11-12T03:40:00+00:00", 113, "commodities", "bbb")])
    )
    assert same.unchanged == 1 and same.stored == 0

    with pytest.raises(ValueError):
        import_artifact(
            conn,
            tmp_path,
            _artifact(
                [("2026-11-12T05:40:00+00:00", 113, "auctions", "ccc")], path_override="../evil.gz"
            ),
        )


@respx.mock(assert_all_called=False)
def test_sync_downloads_new_artifacts_and_reenables_workflow(respx_mock, conn, tmp_path):
    def art(i, name, expired=False):
        return {"id": i, "name": name, "expired": expired, "created_at": f"2026-11-12T0{i}:45:00Z"}

    respx_mock.get(f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}").respond(
        json={"state": "disabled_inactivity"}
    )
    enable = respx_mock.put(f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/enable").respond(
        204
    )
    respx_mock.get(f"{API}/repos/{REPO}/actions/artifacts").respond(
        json={
            "artifacts": [
                art(3, "ah-snapshots-20261112T034000Z"),
                art(1, "ah-snapshots-20261112T014000Z"),
                art(2, "something-else"),
                art(4, "ah-snapshots-old", expired=True),
            ]
        }
    )
    z1 = respx_mock.get(f"{API}/repos/{REPO}/actions/artifacts/1/zip").respond(
        content=_artifact([("2026-11-12T01:40:00+00:00", 113, "auctions", "aaa")])
    )
    z3 = respx_mock.get(f"{API}/repos/{REPO}/actions/artifacts/3/zip").respond(
        content=_artifact([("2026-11-12T03:40:00+00:00", 113, "auctions", "ccc")])
    )

    r = sync(conn, tmp_path, REPO, token="t")
    assert (r.artifacts, r.stored) == (2, 2)
    assert enable.called and r.workflow == "active" and "re-enabled" in r.notes[0]
    assert z1.call_count == z3.call_count == 1
    assert z1.calls[0].request.headers["Authorization"] == "Bearer t"

    again = sync(conn, tmp_path, REPO, token="t")
    assert again.artifacts == 0 and z1.call_count == 1  # remembered


@respx.mock(assert_all_called=False)
def test_collect_falls_back_to_direct_fetch_when_github_fails(
    respx_mock, conn, tmp_path, monkeypatch
):
    monkeypatch.setenv("WOWGOLD_GITHUB_TOKEN", "t")  # don't ask the real gh CLI
    from wowgold.cli import _remote_sync
    from wowgold.config import Settings

    respx_mock.get(url__startswith=API).mock(side_effect=httpx.ConnectError("offline"))
    s = Settings(wowgold_data_dir=tmp_path, wowgold_remote_repo=REPO)
    assert _remote_sync(conn, s) is False


@respx.mock(assert_all_called=False)
def test_collect_fetches_directly_when_github_has_nothing_new(
    respx_mock, conn, tmp_path, monkeypatch
):
    from wowgold.cli import _remote_sync
    from wowgold.config import Settings

    monkeypatch.setenv("WOWGOLD_GITHUB_TOKEN", "t")
    respx_mock.get(f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}").respond(404)
    respx_mock.get(f"{API}/repos/{REPO}/actions/artifacts").respond(json={"artifacts": []})
    s = Settings(wowgold_data_dir=tmp_path, wowgold_remote_repo=REPO)
    assert _remote_sync(conn, s) is False  # no snapshots at all: fetch directly
    db.insert_snapshot(
        conn,
        db.SnapshotRow("eu", 1, 113, "auctions", datetime.now(UTC).isoformat(), 200, "stored"),
    )
    assert _remote_sync(conn, s) is True  # recent data, GitHub just hasn't run yet
