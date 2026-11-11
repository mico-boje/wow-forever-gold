"""Probe which WoW: Forever API endpoints currently work.

The Forever API is undocumented and has changed daily since launch. This module hits
a list of candidate endpoints and records status, size and shape, so we can see what
is usable before building on it. Reports are saved as JSON under data/probes/.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .blizzard import BlizzardClient, ForeverApi
from .blizzard.forever import AUCTION_INDEX_PATHS, parse_auction_index, parse_rulesets
from .blizzard.namespaces import NamespaceKind, resolve

# Linen Cloth: an item that exists in every Classic-derived version.
SAMPLE_ITEM_ID = 2589


@dataclass(frozen=True)
class ProbeCase:
    name: str
    path: str
    ns: NamespaceKind


STATIC_CASES: tuple[ProbeCase, ...] = (
    ProbeCase("region index", "/data/wow/region/index", NamespaceKind.DYNAMIC),
    ProbeCase("realm index", "/data/wow/realm/index", NamespaceKind.DYNAMIC),
    ProbeCase("connected realm index", "/data/wow/connected-realm/index", NamespaceKind.DYNAMIC),
    ProbeCase("legacy commodities", "/data/wow/auctions/commodities", NamespaceKind.DYNAMIC),
    ProbeCase("wow token", "/data/wow/token/index", NamespaceKind.DYNAMIC),
    ProbeCase("item (static)", f"/data/wow/item/{SAMPLE_ITEM_ID}", NamespaceKind.STATIC),
    ProbeCase(
        "item (static versioned)",
        f"/data/wow/item/{SAMPLE_ITEM_ID}",
        NamespaceKind.STATIC_VERSIONED,
    ),
    ProbeCase("item media", f"/data/wow/media/item/{SAMPLE_ITEM_ID}", NamespaceKind.STATIC),
    ProbeCase("item class index", "/data/wow/item-class/index", NamespaceKind.STATIC),
    ProbeCase("profession index", "/data/wow/profession/index", NamespaceKind.STATIC),
    ProbeCase(
        "profession index (versioned)", "/data/wow/profession/index", NamespaceKind.STATIC_VERSIONED
    ),
    ProbeCase("creature family index", "/data/wow/creature-family/index", NamespaceKind.STATIC),
    ProbeCase("creature type index", "/data/wow/creature-type/index", NamespaceKind.STATIC),
    ProbeCase("playable class index", "/data/wow/playable-class/index", NamespaceKind.STATIC),
    ProbeCase("playable race index", "/data/wow/playable-race/index", NamespaceKind.STATIC),
)


@dataclass
class ProbeResult:
    name: str
    url: str
    namespace: str
    status: int
    bytes: int
    elapsed_s: float
    shape: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass
class ProbeReport:
    region: str
    started_at: str
    results: list[ProbeResult] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def summarize_shape(payload: Any) -> dict[str, Any]:
    """Top-level keys plus list lengths: enough to see what came back without the body."""
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    shape: dict[str, Any] = {"keys": sorted(payload.keys())}
    for key, value in payload.items():
        if isinstance(value, list):
            shape[f"len({key})"] = len(value)
    return shape


class Prober:
    def __init__(self, client: BlizzardClient, static_version: str) -> None:
        self.client = client
        self.api = ForeverApi(client)
        self.static_version = static_version

    def _call(
        self, name: str, path: str, namespace: str, note: str = ""
    ) -> tuple[ProbeResult, Any]:
        resp = self.client.get(path, namespace)
        payload = None
        if resp.ok and resp.content:
            try:
                payload = resp.json()
            except ValueError:
                note = (note + " non-JSON body").strip()
        result = ProbeResult(
            name=name,
            url=resp.url.split("?")[0],
            namespace=namespace,
            status=resp.status,
            bytes=len(resp.content),
            elapsed_s=round(resp.elapsed_s, 3),
            shape=summarize_shape(payload) if payload is not None else {},
            note=note,
        )
        return result, payload

    def run(self, *, include_house_bodies: bool = True) -> ProbeReport:
        region = self.client.region
        report = ProbeReport(region=region, started_at=datetime.now(UTC).isoformat())

        for case in STATIC_CASES:
            ns = resolve(case.ns, region, self.static_version)
            result, _ = self._call(case.name, case.path, ns)
            report.results.append(result)

        # Dynamic chain: rulesets -> auction index -> one house.
        dyn = resolve(NamespaceKind.DYNAMIC, region)
        result, payload = self._call("ruleset index", "/data/wow/ruleset/index", dyn)
        report.results.append(result)
        rulesets = parse_rulesets(payload) if payload else []
        if not rulesets:
            return report
        result.note = ", ".join(f"{r.id}={r.name}" for r in rulesets)

        rid = rulesets[0].id
        houses = []
        for template in AUCTION_INDEX_PATHS:
            path = template.format(rid=rid)
            result, payload = self._call(
                f"auction index {path.rsplit('auctions', 1)[1] or '/'}", path, dyn
            )
            if payload and not houses:
                houses = parse_auction_index(payload)
                result.note = f"houses: {[h.id for h in houses]}"
            report.results.append(result)

        if include_house_bodies and houses:
            house = houses[0]
            for name, path in (
                (f"house {house.id} auctions", f"/data/wow/ruleset/{rid}/auctions/{house.id}"),
                (
                    f"house {house.id} commodities",
                    f"/data/wow/ruleset/{rid}/auctions/{house.id}/commodities",
                ),
            ):
                result, _ = self._call(name, path, dyn, note=house.name or "")
                report.results.append(result)
        return report


def save_report(report: ProbeReport, data_dir: Path) -> Path:
    out_dir = data_dir / "probes"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.started_at.replace(":", "").replace("-", "").split(".")[0]
    path = out_dir / f"probe-{report.region}-{stamp}.json"
    path.write_text(report.to_json())
    return path
