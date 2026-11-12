"""Collect auction snapshots on GitHub Actions while your computer is off.

The Blizzard API only serves the auction house as it is right now, so an hour nobody fetched
is lost. A scheduled workflow (`.github/workflows/collect.yml`) runs `wowgold remote export`
every hour and uploads the raw snapshots as a workflow artifact (kept `RETENTION_DAYS`).
Your machine's collector then runs `wowgold remote sync` instead of fetching itself: it
downloads the artifacts it hasn't seen, adds their snapshots in time order, and ingests them as
usual, so hours collected while you were away fill in.

An artifact holds the raw files (`raw/<region>/<house>/<kind>/*.json.gz`, as written locally)
and `manifest.json` with the houses and one row per snapshot.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from . import db

ARTIFACT_PREFIX = "ah-snapshots-"
MANIFEST = "manifest.json"
WORKFLOW_FILE = "collect.yml"
RETENTION_DAYS = 30
API = "https://api.github.com"


# -- in the workflow ------------------------------------------------------------------------------


def export_snapshot(api, out_dir: Path) -> Any:
    """Fetch every house once into `out_dir` (raw files + manifest). Returns the summary."""
    from .snapshot import run_snapshot

    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        conn = db.connect(Path(tmp) / "work.sqlite3")
        try:
            summary = run_snapshot(api, conn, out_dir)
            houses = [dict(r) for r in conn.execute("SELECT * FROM houses")]
            rulesets = [dict(r) for r in conn.execute("SELECT * FROM house_rulesets")]
            snaps = []
            for r in conn.execute("SELECT * FROM snapshots WHERE outcome = 'stored'"):
                row = dict(r)
                row["path"] = str(Path(row["path"]).relative_to(out_dir))
                snaps.append(row)
        finally:
            conn.close()
    manifest = {"version": 1, "houses": houses, "rulesets": rulesets, "snapshots": snaps}
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=1))
    return summary


# -- on your machine ------------------------------------------------------------------------


def github_token() -> str | None:
    """WOWGOLD_GITHUB_TOKEN or GITHUB_TOKEN, else the GitHub CLI's login."""
    for var in ("WOWGOLD_GITHUB_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run(
                [gh, "auth", "token"], capture_output=True, text=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        token = out.stdout.strip()
        return token or None
    return None


@dataclass
class SyncSummary:
    artifacts: int = 0  # downloaded this time
    stored: int = 0  # snapshots added
    unchanged: int = 0  # same data as the previous snapshot of that house
    skipped: int = 0  # older than what's already here (e.g. collected locally meanwhile)
    pending: int = 0  # artifacts still to download (limit reached)
    workflow: str | None = None  # the workflow's state, if known
    notes: list[str] = field(default_factory=list)


def _state_path(data_dir: Path) -> Path:
    return data_dir / "remote_sync.json"


def _load_state(data_dir: Path) -> dict:
    try:
        return json.loads(_state_path(data_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(data_dir: Path, state: dict) -> None:
    _state_path(data_dir).parent.mkdir(parents=True, exist_ok=True)
    _state_path(data_dir).write_text(json.dumps(state, indent=1))


def _client(token: str | None) -> httpx.Client:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # httpx drops the Authorization header when a redirect leaves api.github.com
    return httpx.Client(headers=headers, timeout=60, follow_redirects=True)


def list_artifacts(client: httpx.Client, repo: str) -> list[dict]:
    """Unexpired snapshot artifacts, oldest first."""
    out, page = [], 1
    while page <= 20:
        r = client.get(
            f"{API}/repos/{repo}/actions/artifacts", params={"per_page": 100, "page": page}
        )
        r.raise_for_status()
        batch = r.json().get("artifacts") or []
        out += [a for a in batch if a["name"].startswith(ARTIFACT_PREFIX) and not a["expired"]]
        if len(batch) < 100:
            break
        page += 1
    return sorted(out, key=lambda a: a["created_at"])


def check_workflow(client: httpx.Client, repo: str, notes: list[str]) -> str | None:
    """The collect workflow's state. GitHub disables scheduled workflows in public repos
    after 60 days without commits; turn it back on when that happened."""
    r = client.get(f"{API}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}")
    if r.status_code == 404:
        notes.append(f"no {WORKFLOW_FILE} workflow in {repo} (on its default branch)")
        return None
    r.raise_for_status()
    state = r.json().get("state")
    if state == "disabled_inactivity":
        e = client.put(f"{API}/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/enable")
        if e.status_code in (200, 204):
            notes.append(
                "GitHub had paused the collect workflow (no commits in 60 days): re-enabled"
            )
            state = "active"
        else:
            notes.append(
                f"the collect workflow is paused and couldn't be re-enabled ({e.status_code})"
            )
    return state


def import_artifact(conn: sqlite3.Connection, data_dir: Path, payload: bytes) -> SyncSummary:
    """Add one artifact's snapshots, oldest first, skipping any older than what's stored."""
    out = SyncSummary()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        manifest = json.loads(zf.read(MANIFEST))
        for h in manifest.get("houses") or []:
            db.upsert_house(
                conn, h["region"], h["house_id"], h.get("name"), h["last_seen"], h.get("faction")
            )
        for r in manifest.get("rulesets") or []:
            db.link_house_ruleset(
                conn, r["region"], r["house_id"], r["ruleset_id"], r["ruleset_name"]
            )
        for s in sorted(manifest.get("snapshots") or [], key=lambda s: s["fetched_at"]):
            region, house, kind = s["region"], s["house_id"], s["kind"]
            latest = conn.execute(
                "SELECT MAX(fetched_at) FROM snapshots WHERE region = ? AND house_id = ? "
                "AND kind = ?",
                (region, house, kind),
            ).fetchone()[0]
            if latest is not None and s["fetched_at"] <= latest:
                out.skipped += 1  # ingest diffs each snapshot against the one before it
                continue
            prev = db.latest_stored(conn, region, house, kind)
            fields = {k: s.get(k) for k in ("last_modified", "sha256", "n_auctions", "bytes")}
            base = dict(
                region=region,
                ruleset_id=s["ruleset_id"],
                house_id=house,
                kind=kind,
                fetched_at=s["fetched_at"],
                http_status=s.get("http_status") or 200,
            )
            if prev is not None and prev["sha256"] and prev["sha256"] == s.get("sha256"):
                db.insert_snapshot(conn, db.SnapshotRow(**base, outcome="unchanged", **fields))
                out.unchanged += 1
                continue
            rel = Path(s["path"])
            if rel.is_absolute() or ".." in rel.parts or rel.parts[:1] != ("raw",):
                raise ValueError(f"unexpected path in artifact: {s['path']}")
            target = data_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(s["path"]))
            db.insert_snapshot(
                conn, db.SnapshotRow(**base, outcome="stored", path=str(target), **fields)
            )
            out.stored += 1
    conn.commit()
    return out


def sync(
    conn: sqlite3.Connection,
    data_dir: Path,
    repo: str,
    *,
    token: str | None = None,
    limit: int = 200,
) -> SyncSummary:
    """Download and add every snapshot artifact not imported yet (oldest first)."""
    state = _load_state(data_dir)
    done = set(state.get("imported") or [])
    total = SyncSummary()
    with _client(token or github_token()) as client:
        try:
            total.workflow = check_workflow(client, repo, total.notes)
        except httpx.HTTPError as exc:
            total.notes.append(f"couldn't check the workflow: {exc}")
        todo = [a for a in list_artifacts(client, repo) if a["id"] not in done]
        total.pending = max(0, len(todo) - limit)
        for a in todo[:limit]:
            r = client.get(f"{API}/repos/{repo}/actions/artifacts/{a['id']}/zip")
            r.raise_for_status()
            got = import_artifact(conn, data_dir, r.content)
            total.artifacts += 1
            total.stored += got.stored
            total.unchanged += got.unchanged
            total.skipped += got.skipped
            done.add(a["id"])
            state["imported"] = sorted(done)[-5000:]
            state["last_artifact"] = a["name"]
            _save_state(data_dir, state)
    return total


def status(data_dir: Path) -> dict:
    return _load_state(data_dir)


__all__ = ["SyncSummary", "export_snapshot", "import_artifact", "status", "sync"]
