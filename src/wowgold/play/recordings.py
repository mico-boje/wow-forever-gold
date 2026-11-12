"""Sessions recorded by the WowGoldLogger addon (v3): read them from WoW's SavedVariables file
and keep them in the database.

The addon only records between `/wgl start` and `/wgl stop`, as a raw event log (see the code
table at the top of the addon). WoW writes SavedVariables on /reload or logout, so a session
shows up here after one of those.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from glob import glob
from pathlib import Path
from typing import Any

from ..luatable import parse_saved_variables

ADDON_DB_VAR = "WowGoldLoggerDB"
FILE_NAME = "WowGoldLogger.lua"
# Where WoW keeps SavedVariables, seen from WSL or Linux (Wine/Lutris installs vary: set
# WOWGOLD_ADDON_FILE if it's elsewhere).
SEARCH_GLOBS = (
    "/mnt/c/Program Files (x86)/World of Warcraft/*/WTF/Account/*/SavedVariables/" + FILE_NAME,
    "/mnt/c/Program Files/World of Warcraft/*/WTF/Account/*/SavedVariables/" + FILE_NAME,
    "/mnt/*/World of Warcraft/*/WTF/Account/*/SavedVariables/" + FILE_NAME,
    "/mnt/*/Games/World of Warcraft/*/WTF/Account/*/SavedVariables/" + FILE_NAME,
    os.path.expanduser("~/Games/*/drive_c/Program Files*/World of Warcraft/*/WTF/Account/*/")
    + "SavedVariables/"
    + FILE_NAME,
)


@dataclass(frozen=True)
class Event:
    t: int  # seconds since the session started
    code: str
    fields: tuple[str, ...]

    def int(self, i: int, default: int | None = None) -> int | None:
        try:
            return int(self.fields[i])
        except (IndexError, ValueError):
            return default

    def float(self, i: int) -> float | None:
        try:
            return float(self.fields[i])
        except (IndexError, ValueError):
            return None

    def str(self, i: int) -> str:
        return self.fields[i] if i < len(self.fields) else ""


@dataclass
class Recording:
    id: str
    started: datetime
    stopped: datetime | None  # None: still recording when the file was written
    char: dict[str, Any]
    note: str | None
    events: list[Event] = field(default_factory=list)
    ended: str | None = None  # stop | stale (finished on a later login) | None
    hidden: bool = False

    @property
    def cls(self) -> str | None:
        c = self.char.get("class")
        return str(c).lower() if c else None

    @property
    def level(self) -> int | None:
        try:
            return int(self.char.get("level"))
        except (TypeError, ValueError):
            return None

    @property
    def faction(self) -> str | None:
        f = self.char.get("faction")
        return str(f).lower() if f else None

    @property
    def professions(self) -> dict[str, int]:
        out = {}
        for s in _as_list(self.char.get("skills")):
            if isinstance(s, dict) and s.get("name"):
                try:
                    out[str(s["name"]).lower()] = int(s.get("rank") or 0)
                except (TypeError, ValueError):
                    continue
        return out


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [
            value[k]
            for k in sorted(
                value,
                key=lambda k: (not str(k).isdigit(), int(k) if str(k).isdigit() else 0, str(k)),
            )
        ]
    return []


def parse_event(line: str) -> Event | None:
    parts = str(line).split("|")
    if len(parts) < 2:
        return None
    try:
        t = int(parts[0])
    except ValueError:
        return None
    return Event(t, parts[1], tuple(parts[2:]))


def _ts(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def parse_recording(raw: dict[str, Any], *, current: bool = False) -> Recording | None:
    rid = raw.get("id")
    started = _ts(raw.get("start"))
    if not rid or started is None:
        return None
    events = [e for e in (parse_event(x) for x in _as_list(raw.get("events"))) if e]
    char = raw.get("char") if isinstance(raw.get("char"), dict) else {}
    return Recording(
        id=str(rid),
        started=started,
        stopped=None if current else _ts(raw.get("stop")),
        char=char,
        note=str(raw["note"]) if raw.get("note") else None,
        events=events,
        ended=None if current else str(raw.get("ended") or "stop"),
    )


def read_file(path: Path) -> list[Recording]:
    """Finished sessions, plus the one still recording (stopped=None) if any."""
    data = parse_saved_variables(path.read_text(encoding="utf-8")).get(ADDON_DB_VAR) or {}
    if not isinstance(data, dict):
        return []
    out = [
        r
        for r in (
            parse_recording(x) for x in _as_list(data.get("recordings")) if isinstance(x, dict)
        )
        if r
    ]
    if isinstance(data.get("current"), dict) and (
        cur := parse_recording(data["current"], current=True)
    ):
        out.append(cur)
    return out


# -- database --------------------------------------------------------------------------------


def _payload(rec: Recording) -> str:
    return json.dumps(
        {
            "char": rec.char,
            "ended": rec.ended,
            "events": ["|".join([str(e.t), e.code, *e.fields]) for e in rec.events],
        },
        separators=(",", ":"),
    )


@dataclass
class StoreResult:
    new: int = 0
    updated: int = 0  # a session seen earlier while still recording, now longer or finished
    unchanged: int = 0


def store(conn: sqlite3.Connection, recs: list[Recording]) -> StoreResult:
    res = StoreResult()
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    for rec in recs:
        row = conn.execute(
            "SELECT n_events, stopped_at FROM recordings WHERE id = ?", (rec.id,)
        ).fetchone()
        stopped = rec.stopped.isoformat(timespec="seconds") if rec.stopped else None
        if row is not None and row[0] == len(rec.events) and row[1] == stopped:
            res.unchanged += 1
            continue
        if row is not None and row[1] is not None and stopped is None:
            res.unchanged += 1  # never replace a finished session with an older, open copy
            continue
        conn.execute(
            """INSERT INTO recordings (id, started_at, stopped_at, char_name, realm, cls, level,
                   faction, note, n_events, payload, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET stopped_at = excluded.stopped_at,
                   n_events = excluded.n_events, payload = excluded.payload,
                   note = excluded.note, imported_at = excluded.imported_at""",
            (
                rec.id,
                rec.started.isoformat(timespec="seconds"),
                stopped,
                rec.char.get("name"),
                rec.char.get("realm"),
                rec.cls,
                rec.level,
                rec.faction,
                rec.note,
                len(rec.events),
                _payload(rec),
                stamp,
            ),
        )
        if row is None:
            res.new += 1
        else:
            res.updated += 1
    conn.commit()
    return res


def _from_row(row: sqlite3.Row | tuple) -> Recording:
    (rid, started, stopped, note, hidden, payload) = row
    data = json.loads(payload)
    return Recording(
        id=rid,
        started=datetime.fromisoformat(started),
        stopped=datetime.fromisoformat(stopped) if stopped else None,
        char=data.get("char") or {},
        note=note,
        events=[e for e in (parse_event(x) for x in data.get("events") or []) if e],
        ended=data.get("ended"),
        hidden=bool(hidden),
    )


def load(
    conn: sqlite3.Connection, *, rid: str | None = None, include_hidden: bool = True
) -> list[Recording]:
    sql = "SELECT id, started_at, stopped_at, note, hidden, payload FROM recordings"
    where, args = [], []
    if rid is not None:
        where.append("id = ?")
        args.append(rid)
    if not include_hidden:
        where.append("hidden = 0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC"
    return [_from_row(tuple(r)) for r in conn.execute(sql, args)]


def set_hidden(conn: sqlite3.Connection, rid: str, hidden: bool) -> None:
    conn.execute("UPDATE recordings SET hidden = ? WHERE id = ?", (int(hidden), rid))
    conn.commit()


# -- finding and auto-importing the addon's file -------------------------------------------------


def find_files(configured: Path | None = None) -> list[Path]:
    if configured:
        return [configured] if configured.exists() else []
    found = {Path(p) for pattern in SEARCH_GLOBS for p in glob(pattern)}
    return sorted(found)


@dataclass
class AutoImport:
    files: list[Path]
    read: list[Path] = field(default_factory=list)  # changed since last time
    result: StoreResult = field(default_factory=StoreResult)
    errors: list[str] = field(default_factory=list)


def _state_path(data_dir: Path) -> Path:
    return data_dir / "addon_import.json"


def auto_import(
    conn: sqlite3.Connection, data_dir: Path, configured: Path | None = None, *, force=False
) -> AutoImport:
    """Import every WowGoldLogger file found whose modification time changed."""
    try:
        state = json.loads(_state_path(data_dir).read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    out = AutoImport(find_files(configured))
    for path in out.files:
        try:
            mtime = path.stat().st_mtime
        except OSError as exc:
            out.errors.append(f"{path}: {exc}")
            continue
        if not force and state.get(str(path)) == mtime:
            continue
        try:
            r = store(conn, read_file(path))
        except Exception as exc:  # a half-written or foreign file shouldn't stop the collector
            out.errors.append(f"{path}: {exc}")
            continue
        out.read.append(path)
        out.result.new += r.new
        out.result.updated += r.updated
        out.result.unchanged += r.unchanged
        state[str(path)] = mtime
    _state_path(data_dir).parent.mkdir(parents=True, exist_ok=True)
    _state_path(data_dir).write_text(json.dumps(state, indent=2))
    return out
