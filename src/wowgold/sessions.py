"""Record play sessions (manually, with a timer, or from the in-game logger addon)."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .luatable import parse_saved_variables
from .methods.models import Kind, Method
from .planner.engine import throughput_multiplier
from .planner.profile import CharacterProfile
from .segments import (
    Proposal,
    Segment,
    load_item_meta,
    parse_segment,
    propose,
    store_segment,
)

ADDON_DB_VAR = "WowGoldLoggerDB"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# -- parsing helpers --------------------------------------------------------------------

_COIN = re.compile(r"(\d+)\s*([gsc])", re.IGNORECASE)


def parse_coins(text: str) -> int:
    """'3g 20s 5c' / '3g20s' / '1250' (copper) -> copper."""
    text = text.strip()
    if text.isdigit():
        return int(text)
    parts = _COIN.findall(text)
    if not parts or _COIN.sub("", text).strip():
        raise ValueError(f"can't read coins {text!r}; use e.g. 3g20s5c")
    factor = {"g": 10000, "s": 100, "c": 1}
    return sum(int(n) * factor[unit.lower()] for n, unit in parts)


def parse_item_spec(conn: sqlite3.Connection, spec: str) -> tuple[int, int]:
    """'13463:14', 'Dreamfoil:14' or 'Dreamfoil=14' -> (item_id, qty)."""
    name, sep, qty = spec.rpartition(":") if ":" in spec else spec.rpartition("=")
    if not sep or not qty.strip().isdigit():
        raise ValueError(f"expected item:qty, got {spec!r}")
    name = name.strip()
    if name.isdigit():
        return int(name), int(qty)
    rows = conn.execute(
        "SELECT item_id FROM items WHERE name = ? COLLATE NOCASE", (name,)
    ).fetchall()
    if len(rows) != 1:
        found = "no item" if not rows else f"{len(rows)} items"
        raise ValueError(f"{found} named {name!r}; use the item ID instead")
    return rows[0][0], int(qty)


# -- profile -----------------------------------------------------------------------------


def load_profile(path: Path) -> CharacterProfile | None:
    if not path.exists():
        return None
    return CharacterProfile.model_validate(json.loads(path.read_text()))


def save_profile(path: Path, profile: CharacterProfile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(profile.model_dump(mode="json", by_alias=True), indent=2))


# -- logging -------------------------------------------------------------------------------


def log_session(
    conn: sqlite3.Connection,
    method: Method,
    profile: CharacterProfile,
    *,
    duration_s: int,
    items: dict[int, int],
    coins_copper: int = 0,
    source: str = "manual",
    started_at: str | None = None,
    external_id: str | None = None,
    notes: str | None = None,
) -> int | None:
    """Store a session; returns its id, or None if `external_id` was already imported."""
    if method.kind == Kind.CRAFT:
        raise ValueError("crafting methods don't need yield calibration")
    if duration_s <= 0:
        raise ValueError("session duration must be positive")
    if (
        external_id
        and conn.execute("SELECT 1 FROM sessions WHERE external_id = ?", (external_id,)).fetchone()
    ):
        return None
    cur = conn.execute(
        """
        INSERT INTO sessions (external_id, method_id, started_at, duration_s, cls, level, gear,
                              epic_mount, multiplier, coins_copper, source, notes, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            external_id,
            method.id,
            started_at or _now(),
            duration_s,
            profile.cls,
            profile.level,
            str(profile.gear),
            int(profile.epic_mount),
            throughput_multiplier(method, profile),
            coins_copper,
            source,
            notes,
            _now(),
        ),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO session_items VALUES (?, ?, ?)",
        [(sid, item_id, qty) for item_id, qty in items.items() if qty > 0],
    )
    conn.commit()
    return sid


def delete_session(conn: sqlite3.Connection, session_id: int) -> bool:
    cur = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    return cur.rowcount > 0


def list_sessions(conn: sqlite3.Connection, method_id: str | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT s.*, COALESCE(SUM(si.qty), 0) AS items
        FROM sessions s LEFT JOIN session_items si ON si.session_id = s.id
        {where} GROUP BY s.id ORDER BY s.started_at DESC
    """
    if method_id:
        return conn.execute(query.format(where="WHERE s.method_id = ?"), (method_id,)).fetchall()
    return conn.execute(query.format(where="")).fetchall()


# -- timer -------------------------------------------------------------------------------


@dataclass
class ActiveSession:
    method_id: str
    started_at: str
    paused_s: float = 0.0
    paused_at: str | None = None

    def active_seconds(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        elapsed = (now - datetime.fromisoformat(self.started_at)).total_seconds()
        paused = self.paused_s
        if self.paused_at:
            paused += (now - datetime.fromisoformat(self.paused_at)).total_seconds()
        return max(0, int(elapsed - paused))


class SessionTimer:
    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self) -> ActiveSession | None:
        if not self.path.exists():
            return None
        return ActiveSession(**json.loads(self.path.read_text()))

    def _save(self, s: ActiveSession) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(s.__dict__))

    def start(self, method_id: str, now: datetime | None = None) -> ActiveSession:
        if self.get():
            raise ValueError("a session is already running; stop or cancel it first")
        s = ActiveSession(method_id, (now or datetime.now(UTC)).isoformat(timespec="seconds"))
        self._save(s)
        return s

    def pause(self, now: datetime | None = None) -> ActiveSession:
        s = self._require()
        if s.paused_at:
            raise ValueError("already paused")
        s.paused_at = (now or datetime.now(UTC)).isoformat(timespec="seconds")
        self._save(s)
        return s

    def resume(self, now: datetime | None = None) -> ActiveSession:
        s = self._require()
        if not s.paused_at:
            raise ValueError("not paused")
        now = now or datetime.now(UTC)
        s.paused_s += (now - datetime.fromisoformat(s.paused_at)).total_seconds()
        s.paused_at = None
        self._save(s)
        return s

    def stop(self) -> ActiveSession:
        s = self._require()
        self.path.unlink()
        return s

    def _require(self) -> ActiveSession:
        s = self.get()
        if s is None:
            raise ValueError("no session is running; start one with `wowgold session start`")
        return s


# -- addon import --------------------------------------------------------------------------


@dataclass
class AddonFile:
    sessions: list[dict[str, Any]]  # v0.1: explicitly started/stopped sessions
    segments: list[dict[str, Any]]  # v0.2: automatically recorded, finished segments
    open_segment: bool  # a segment still in progress (imported next time)

    @property
    def item_ids(self) -> set[int]:
        ids: set[int] = set()
        for raw in self.sessions:
            if isinstance(raw.get("items"), dict):
                ids.update(int(k) for k in raw["items"])
        for raw in self.segments:
            for src in (raw.get("sources") or {}).values():
                if isinstance(src, dict) and isinstance(src.get("items"), dict):
                    ids.update(int(k) for k in src["items"])
        return ids


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [value[k] for k in sorted(value, key=str)]
    return []


def read_addon_file(path: Path) -> AddonFile:
    data = parse_saved_variables(path.read_text(encoding="utf-8")).get(ADDON_DB_VAR) or {}
    if not isinstance(data, dict):
        data = {}
    return AddonFile(
        sessions=[r for r in _as_list(data.get("sessions")) if isinstance(r, dict)],
        segments=[r for r in _as_list(data.get("segments")) if isinstance(r, dict) and r.get("id")],
        open_segment=isinstance(data.get("open"), dict),
    )


@dataclass
class SegmentResult:
    row_id: int
    segment: Segment
    proposal: Proposal


@dataclass
class ImportSummary:
    imported: int = 0  # v0.1 sessions
    duplicates: int = 0
    skipped: list[str] = field(default_factory=list)
    item_ids: set[int] = field(default_factory=set)
    segments: list[SegmentResult] = field(default_factory=list)
    segment_duplicates: int = 0


def _character(default: CharacterProfile, cls: str | None, level: int | None) -> CharacterProfile:
    """The saved profile, with the class/level the addon recorded at the time."""
    update: dict[str, Any] = {}
    if cls:
        update["cls"] = cls.lower()
    if level:
        update["level"] = level
    return default.model_copy(update=update)


def segment_session_prefix(external_id: str) -> str:
    return f"seg:{external_id}:"


def clear_segment_sessions(conn: sqlite3.Connection, external_id: str) -> int:
    prefix = segment_session_prefix(external_id)
    cur = conn.execute(
        "DELETE FROM sessions WHERE substr(external_id, 1, ?) = ?", (len(prefix), prefix)
    )
    conn.commit()
    return cur.rowcount


def apply_segment(
    conn: sqlite3.Connection,
    row_id: int,
    seg: Segment,
    proposal: Proposal,
    methods: dict[str, Method],
    profile: CharacterProfile,
    status: str,
) -> int:
    """Replace the segment's sessions with the proposal's assignments."""
    clear_segment_sessions(conn, seg.external_id)
    character = _character(profile, seg.cls, seg.level)
    created = 0
    for a in proposal.assignments:
        method = methods.get(a.method_id)
        if method is None or a.seconds <= 0:
            continue
        log_session(
            conn,
            method,
            character,
            duration_s=a.seconds,
            items=a.items,
            coins_copper=a.copper,
            source="addon",
            started_at=seg.started_at,
            external_id=segment_session_prefix(seg.external_id) + a.method_id,
            notes=f"segment #{row_id} ({seg.zone}, {a.activity})",
        )
        created += 1
    set_segment_status(conn, row_id, status, proposal)
    return created


def set_segment_status(
    conn: sqlite3.Connection, row_id: int, status: str, proposal: Proposal | None = None
) -> None:
    if proposal is None:
        conn.execute("UPDATE addon_segments SET status = ? WHERE id = ?", (status, row_id))
    else:
        conn.execute(
            "UPDATE addon_segments SET status = ?, proposal = ? WHERE id = ?",
            (status, proposal.to_json(), row_id),
        )
    conn.commit()


def _import_legacy_session(
    conn: sqlite3.Connection,
    raw: dict[str, Any],
    methods: dict[str, Method],
    default_profile: CharacterProfile,
    summary: ImportSummary,
) -> None:
    sid = str(raw.get("id") or "")
    method = methods.get(str(raw.get("method") or ""))
    label = f"session {sid or '?'} ({raw.get('method')})"
    if not sid or raw.get("stop") is None:
        summary.skipped.append(f"{label}: unfinished")
        return
    if method is None:
        summary.skipped.append(f"{label}: unknown method")
        return
    if method.kind == Kind.CRAFT:
        summary.skipped.append(f"{label}: crafting method")
        return
    duration = int(raw.get("active") or 0)
    if duration <= 0:
        summary.skipped.append(f"{label}: no active time")
        return
    items = (
        {int(k): int(v) for k, v in raw["items"].items() if int(v) > 0}
        if isinstance(raw.get("items"), dict)
        else {}
    )
    profile = _character(
        default_profile,
        str(raw["class"]) if raw.get("class") else None,
        int(raw["level"]) if raw.get("level") else None,
    )
    started = datetime.fromtimestamp(int(raw.get("start") or 0), UTC).isoformat(timespec="seconds")
    new_id = log_session(
        conn,
        method,
        profile,
        duration_s=duration,
        items=items,
        coins_copper=int(raw.get("copper") or 0),
        source="addon",
        started_at=started,
        external_id=f"addon:{sid}",
        notes=raw.get("zone"),
    )
    if new_id is None:
        summary.duplicates += 1
    else:
        summary.imported += 1
        summary.item_ids.update(items)


def import_addon_data(
    conn: sqlite3.Connection,
    data: AddonFile,
    methods: dict[str, Method],
    default_profile: CharacterProfile,
) -> ImportSummary:
    """Import v0.1 sessions and v0.2 segments. Segments that match a method confidently
    (or were labelled in game) become sessions right away; the rest wait for review.
    Item metadata should be synced first, since activity detection uses item types."""
    summary = ImportSummary()
    for raw in data.sessions:
        _import_legacy_session(conn, raw, methods, default_profile, summary)

    meta = load_item_meta(conn)
    method_list = list(methods.values())
    for raw in data.segments:
        seg = parse_segment(raw)
        row_id = store_segment(conn, raw, seg)
        if row_id is None:
            summary.segment_duplicates += 1
            continue
        proposal = propose(seg, method_list, meta)
        if proposal.status in ("auto", "labelled"):
            apply_segment(conn, row_id, seg, proposal, methods, default_profile, "applied")
        else:
            status = "ignored" if proposal.status == "empty" else proposal.status
            set_segment_status(conn, row_id, status, proposal)
        summary.segments.append(SegmentResult(row_id, seg, proposal))
        for a in proposal.assignments:
            summary.item_ids.update(a.items)
    conn.commit()
    return summary


def import_addon_sessions(
    conn: sqlite3.Connection,
    path: Path,
    methods: dict[str, Method],
    default_profile: CharacterProfile,
) -> ImportSummary:
    """Read the WowGoldLogger SavedVariables file and import it (safe to re-run)."""
    return import_addon_data(conn, read_addon_file(path), methods, default_profile)
