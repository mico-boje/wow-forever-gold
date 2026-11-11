"""Validate method item IDs against item data (catches typos in hand-written YAML)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .models import Method


@dataclass(frozen=True)
class ItemIssue:
    method_id: str
    item_id: int
    expected: str
    actual: str | None  # None = unknown item

    def __str__(self) -> str:
        got = "unknown item" if self.actual is None else f"is {self.actual!r}"
        return f"{self.method_id}: item {self.item_id} ({self.expected!r}) {got}"


def _norm(name: str) -> str:
    return name.lower().replace("’", "'").strip()


def missing_item_ids(conn: sqlite3.Connection, methods: list[Method]) -> list[int]:
    ids = sorted({i for m in methods for i in m.item_ids})
    known = {r[0] for r in conn.execute("SELECT item_id FROM items WHERE name IS NOT NULL")}
    return [i for i in ids if i not in known]


def check_items(conn: sqlite3.Connection, methods: list[Method]) -> list[ItemIssue]:
    names = dict(conn.execute("SELECT item_id, name FROM items WHERE name IS NOT NULL"))
    issues = []
    for m in methods:
        for iq in (*m.yields, *m.reagents, *m.outputs):
            actual = names.get(iq.item)
            if actual is None or _norm(actual) != _norm(iq.name):
                issues.append(ItemIssue(m.id, iq.item, iq.name, actual))
    return issues
