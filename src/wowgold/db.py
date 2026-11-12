"""SQLite storage for auction house metadata and snapshot bookkeeping.

Raw auction bodies live on disk (gzipped JSON); the database records what was fetched,
when, and where it is stored. Parsed price tables come in Phase 2.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS houses (
    region      TEXT NOT NULL,
    house_id    INTEGER NOT NULL,
    name        TEXT,
    faction     TEXT,              -- alliance | horde | blackwater; set manually (API doesn't say)
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    PRIMARY KEY (region, house_id)
);

CREATE TABLE IF NOT EXISTS house_rulesets (
    region        TEXT NOT NULL,
    house_id      INTEGER NOT NULL,
    ruleset_id    INTEGER NOT NULL,
    ruleset_name  TEXT,
    PRIMARY KEY (region, house_id, ruleset_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    region         TEXT NOT NULL,
    ruleset_id     INTEGER NOT NULL,
    house_id       INTEGER NOT NULL,
    kind           TEXT NOT NULL,     -- auctions | commodities
    fetched_at     TEXT NOT NULL,
    http_status    INTEGER NOT NULL,
    outcome        TEXT NOT NULL,     -- stored | unchanged | not_modified | error
    last_modified  TEXT,
    sha256         TEXT,
    n_auctions     INTEGER,
    bytes          INTEGER,
    path           TEXT
);

CREATE INDEX IF NOT EXISTS ix_snapshots_house
    ON snapshots (region, house_id, kind, fetched_at);

-- Per-item price statistics for one stored snapshot (prices in copper per unit).
-- `variant` distinguishes random-suffix items (bonus list IDs); '' for plain items.
CREATE TABLE IF NOT EXISTS price_stats (
    snapshot_id    INTEGER NOT NULL,
    region         TEXT NOT NULL,
    house_id       INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    ts             TEXT NOT NULL,
    item_id        INTEGER NOT NULL,
    variant        TEXT NOT NULL DEFAULT '',
    num_auctions   INTEGER NOT NULL,
    quantity       INTEGER NOT NULL,
    min_unit       INTEGER NOT NULL,
    p10_unit       INTEGER NOT NULL,
    p25_unit       INTEGER NOT NULL,
    median_unit    INTEGER NOT NULL,
    market_unit    INTEGER NOT NULL,
    sold_qty       INTEGER,   -- disappeared listings that were not about to expire (sold/cancelled)
    ambiguous_qty  INTEGER,   -- disappeared listings that may have expired
    interval_s     INTEGER,   -- seconds since the snapshot used for the sales diff
    PRIMARY KEY (snapshot_id, item_id, variant)
);

CREATE INDEX IF NOT EXISTS ix_price_stats_item
    ON price_stats (region, item_id, house_id, ts);

-- Daily roll-up of price_stats, kept after hourly rows are pruned.
CREATE TABLE IF NOT EXISTS price_daily (
    region         TEXT NOT NULL,
    house_id       INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    item_id        INTEGER NOT NULL,
    variant        TEXT NOT NULL,
    day            TEXT NOT NULL,
    samples        INTEGER NOT NULL,
    market_avg     INTEGER NOT NULL,
    min_low        INTEGER NOT NULL,
    quantity_avg   INTEGER NOT NULL,
    sold_qty       INTEGER,
    PRIMARY KEY (region, house_id, kind, item_id, variant, day)
);

CREATE TABLE IF NOT EXISTS items (
    item_id          INTEGER PRIMARY KEY,
    name             TEXT,
    quality          TEXT,
    item_class       TEXT,
    item_subclass    TEXT,
    level            INTEGER,
    required_level   INTEGER,
    sell_price       INTEGER,   -- vendor sell price (copper)
    purchase_price   INTEGER,   -- vendor buy price (copper)
    is_stackable     INTEGER,
    source           TEXT NOT NULL,   -- blizzard | override | missing
    fetched_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_items_name ON items (name COLLATE NOCASE);

-- Measured play sessions, used to calibrate method yields (Phase 4).
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id   TEXT UNIQUE,          -- addon session id, for idempotent imports
    method_id     TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    duration_s    INTEGER NOT NULL,     -- active time (excludes pauses)
    cls           TEXT,
    level         INTEGER,
    gear          TEXT,
    epic_mount    INTEGER,
    multiplier    REAL NOT NULL,        -- throughput multiplier of this character for the method
    coins_copper  INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL,        -- manual | timer | addon
    notes         TEXT,
    created_at    TEXT NOT NULL
);

-- Activity segments recorded by the WowGoldLogger addon (raw payload kept for re-matching).
CREATE TABLE IF NOT EXISTS addon_segments (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id  TEXT NOT NULL UNIQUE,
    zone         TEXT,
    map_id       INTEGER,
    started_at   TEXT NOT NULL,
    active_s     INTEGER NOT NULL,
    cls          TEXT,
    level        INTEGER,
    label        TEXT,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL,   -- applied | review | unmatched | ignored
    proposal     TEXT,            -- JSON of the latest match proposal
    imported_at  TEXT NOT NULL
);

-- Sessions recorded by the WowGoldLogger addon v3: a header and a raw event log, digested by
-- wowgold.play on read.
CREATE TABLE IF NOT EXISTS recordings (
    id           TEXT PRIMARY KEY,   -- the addon's session id
    started_at   TEXT NOT NULL,
    stopped_at   TEXT,
    char_name    TEXT,
    realm        TEXT,
    cls          TEXT,
    level        INTEGER,
    faction      TEXT,
    note         TEXT,
    n_events     INTEGER NOT NULL,
    payload      TEXT NOT NULL,      -- JSON: header + events
    hidden       INTEGER NOT NULL DEFAULT 0,  -- excluded from calibration
    imported_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_items (
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    item_id     INTEGER NOT NULL,
    qty         INTEGER NOT NULL,
    PRIMARY KEY (session_id, item_id)
);
"""

# Columns added after the first release; applied idempotently in connect().
MIGRATIONS = (("snapshots", "ingested_at", "TEXT"),)


@dataclass(frozen=True)
class SnapshotRow:
    region: str
    ruleset_id: int
    house_id: int
    kind: str
    fetched_at: str
    http_status: int
    outcome: str
    last_modified: str | None = None
    sha256: str | None = None
    n_auctions: int | None = None
    bytes: int | None = None
    path: str | None = None


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    return conn


def upsert_house(
    conn: sqlite3.Connection,
    region: str,
    house_id: int,
    name: str | None,
    now: str,
    faction: str | None = None,
) -> None:
    """Insert/refresh a house. A derived `faction` never overwrites a manually set one."""
    conn.execute(
        """
        INSERT INTO houses (region, house_id, name, faction, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (region, house_id) DO UPDATE SET
            name = COALESCE(excluded.name, houses.name),
            faction = COALESCE(houses.faction, excluded.faction),
            last_seen = excluded.last_seen
        """,
        (region, house_id, name, faction, now, now),
    )


def link_house_ruleset(
    conn: sqlite3.Connection, region: str, house_id: int, ruleset_id: int, ruleset_name: str
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO house_rulesets VALUES (?, ?, ?, ?)",
        (region, house_id, ruleset_id, ruleset_name),
    )


def set_faction(conn: sqlite3.Connection, region: str, house_id: int, faction: str | None) -> bool:
    cur = conn.execute(
        "UPDATE houses SET faction = ? WHERE region = ? AND house_id = ?",
        (faction, region, house_id),
    )
    return cur.rowcount > 0


def insert_snapshot(conn: sqlite3.Connection, row: SnapshotRow) -> None:
    conn.execute(
        """
        INSERT INTO snapshots (region, ruleset_id, house_id, kind, fetched_at, http_status,
                               outcome, last_modified, sha256, n_auctions, bytes, path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.region,
            row.ruleset_id,
            row.house_id,
            row.kind,
            row.fetched_at,
            row.http_status,
            row.outcome,
            row.last_modified,
            row.sha256,
            row.n_auctions,
            row.bytes,
            row.path,
        ),
    )


def latest_stored(
    conn: sqlite3.Connection, region: str, house_id: int, kind: str
) -> sqlite3.Row | None:
    """Most recent snapshot that carried data (stored or unchanged), for change detection."""
    return conn.execute(
        """
        SELECT * FROM snapshots
        WHERE region = ? AND house_id = ? AND kind = ? AND outcome IN ('stored', 'unchanged')
        ORDER BY id DESC LIMIT 1
        """,
        (region, house_id, kind),
    ).fetchone()


def list_houses(conn: sqlite3.Connection, region: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT h.*, GROUP_CONCAT(hr.ruleset_id) AS rulesets,
               (SELECT n_auctions FROM snapshots s
                 WHERE s.region = h.region AND s.house_id = h.house_id
                   AND s.kind = 'auctions' AND s.outcome = 'stored'
                 ORDER BY s.id DESC LIMIT 1) AS last_auctions,
               (SELECT n_auctions FROM snapshots s
                 WHERE s.region = h.region AND s.house_id = h.house_id
                   AND s.kind = 'commodities' AND s.outcome = 'stored'
                 ORDER BY s.id DESC LIMIT 1) AS last_commodities
        FROM houses h
        LEFT JOIN house_rulesets hr ON hr.region = h.region AND hr.house_id = h.house_id
        WHERE h.region = ?
        GROUP BY h.house_id
        ORDER BY h.house_id
        """,
        (region,),
    ).fetchall()
