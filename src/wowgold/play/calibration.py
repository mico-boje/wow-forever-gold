"""Turn recorded sessions into corrections for farm discovery.

- **Kill speed:** each session's median "model time per kill ÷ your time per kill", pooled
  over sessions weighted by how many kill gaps they had, shrunk towards 1.0 (the model) by
  `PRIOR_GAPS` gaps' worth: a handful of kills barely moves it, an evening of farming does.
- **Respawn times:** re-kills at the same spot, pooled per mob over all sessions; the lower
  quartile of the gaps once there are `RESPAWN_MIN_PAIRS`.

Your own loot isn't used: drop rates stay Wowhead's and the vanilla database's.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field

from .digest import RESPAWN_MIN_PAIRS, digest, lower_quartile, respawn_gaps
from .recordings import load

PRIOR_GAPS = 30
MIN_FACTOR, MAX_FACTOR = 0.3, 3.0


@dataclass(frozen=True)
class Respawn:
    seconds: float
    pairs: int


@dataclass(frozen=True)
class KillSpeed:
    factor: float  # 1.0 = the model; 0.8 = you kill 20% slower than it assumes
    gaps: int  # kill-to-kill gaps behind it
    sessions: int


@dataclass
class Calibration:
    kill_speed: dict[str, KillSpeed] = field(default_factory=dict)  # per class
    respawns: dict[int, Respawn] = field(default_factory=dict)
    sessions: int = 0  # finished, not hidden

    def speed(self, cls: str | None) -> float:
        k = self.kill_speed.get((cls or "").lower())
        return k.factor if k else 1.0


def build(conn: sqlite3.Connection, data=None, names: dict[int, str] | None = None) -> Calibration:
    """`data` (VanillaData) is needed for kill speed: the model's rate per mob."""
    out = Calibration()
    pooled: dict[str, list[float]] = defaultdict(lambda: [0.0, 0, 0])  # weighted, gaps, sessions
    gaps_by_npc: dict[int, list[int]] = defaultdict(list)
    for rec in load(conn, include_hidden=False):
        if rec.stopped is None:
            continue  # still recording
        out.sessions += 1
        d = digest(rec, data=data, names=names)
        if d.kill_speed is not None and rec.cls:
            n = len(d.kill_gaps)
            acc = pooled[rec.cls]
            acc[0] += d.kill_speed * n
            acc[1] += n
            acc[2] += 1
        for npc, gaps in respawn_gaps(d.kill_log).items():
            gaps_by_npc[npc].extend(gaps)
    for cls, (weighted, n, sessions) in pooled.items():
        f = (weighted + PRIOR_GAPS) / (n + PRIOR_GAPS)
        out.kill_speed[cls] = KillSpeed(min(MAX_FACTOR, max(MIN_FACTOR, f)), int(n), int(sessions))
    for npc, gaps in gaps_by_npc.items():
        if len(gaps) >= RESPAWN_MIN_PAIRS:
            out.respawns[npc] = Respawn(lower_quartile(gaps), len(gaps))
    return out
