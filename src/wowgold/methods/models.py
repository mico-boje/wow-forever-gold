"""Gold-making method definitions (loaded from data/methods/*.yaml).

A method is one repeatable activity: a gathering route, a mob farm, a fishing spot or a
craft. Yields are *estimates* until calibrated with measured sessions (Phase 4), which is
why every yield carries a standard deviation and every method a confidence level.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


class Kind(StrEnum):
    GATHER = "gather"
    FARM = "farm"
    FISHING = "fishing"
    CRAFT = "craft"


class Activity(StrEnum):
    """What you're physically doing; used to match recorded play to methods."""

    HERBALISM = "herbalism"
    MINING = "mining"
    SKINNING = "skinning"
    FISHING = "fishing"
    KILL = "kill"


GATHER_PROFESSIONS = ("herbalism", "mining", "skinning")


class Confidence(StrEnum):
    ESTIMATE = "estimate"  # educated guess (usually from Classic Era experience)
    COMMUNITY = "community"  # reported by Forever players
    MEASURED = "measured"  # calibrated from our own session logs


class Faction(StrEnum):
    ALLIANCE = "alliance"
    HORDE = "horde"
    ANY = "any"


class GearTier(StrEnum):
    LEVELING = "leveling"
    FRESH = "fresh"  # fresh 60 / quest greens and blues
    PRERAID = "preraid"  # dungeon blues
    RAID = "raid"

    @property
    def rank(self) -> int:
        return list(GearTier).index(self)


CLASSES = (
    "warrior",
    "paladin",
    "hunter",
    "rogue",
    "priest",
    "shaman",
    "mage",
    "warlock",
    "druid",
)
PROFESSIONS = (
    "herbalism",
    "mining",
    "skinning",
    "fishing",
    "alchemy",
    "blacksmithing",
    "enchanting",
    "engineering",
    "leatherworking",
    "tailoring",
    "cooking",
    "first_aid",
)


class ItemQty(BaseModel):
    item: int
    name: str  # human-readable, checked against item data by `wowgold methods check`
    qty: float = Field(gt=0)  # per hour for yields; per craft for reagents/outputs
    sd: float = Field(default=0.0, ge=0)  # standard deviation of qty
    vendor: bool = False  # reagent bought from a vendor (use vendor price, not AH)


class Requirements(BaseModel):
    level: int = Field(default=1, ge=1, le=60)
    professions: dict[str, int] = Field(default_factory=dict)
    classes: list[str] | None = None  # None = any class
    faction: Faction = Faction.ANY
    gear: GearTier = GearTier.LEVELING  # minimum gear tier
    group_size: int = Field(default=1, ge=1, le=40)

    @field_validator("professions")
    @classmethod
    def _known_professions(cls, v: dict[str, int]) -> dict[str, int]:
        unknown = set(v) - set(PROFESSIONS)
        if unknown:
            raise ValueError(f"unknown profession(s): {sorted(unknown)}")
        return v

    @field_validator("classes")
    @classmethod
    def _known_classes(cls, v: list[str] | None) -> list[str] | None:
        if v and (unknown := set(v) - set(CLASSES)):
            raise ValueError(f"unknown class(es): {sorted(unknown)}")
        return v


class Method(BaseModel):
    id: str
    name: str
    kind: Kind
    zone: str = ""
    # Every zone the method covers (English zone names, as GetZoneText returns them).
    # Defaults to [zone]; add the alternatives for routes like "Tanaris / Badlands".
    zones: list[str] = Field(default_factory=list)
    description: str = ""
    requirements: Requirements = Field(default_factory=Requirements)
    # gather / farm / fishing
    yields: list[ItemQty] = Field(default_factory=list)
    vendor_copper_per_hour: int = 0  # coins + grey vendor trash
    # craft
    reagents: list[ItemQty] = Field(default_factory=list)
    outputs: list[ItemQty] = Field(default_factory=list)
    crafts_per_hour: int = 600  # cast time + mailbox/AH overhead; rarely the bottleneck
    # adjustments
    class_modifiers: dict[str, float] = Field(default_factory=dict)  # yield multipliers
    confidence: Confidence = Confidence.ESTIMATE
    # Set at runtime by calibration (not in YAML): base-equivalent hours of measured sessions.
    calibration_hours: float = 0.0
    calibration_sessions: int = 0
    risk: list[str] = Field(default_factory=list)  # e.g. pvp, competition, elite_mobs
    notes: str = ""

    @model_validator(mode="after")
    def _shape(self) -> Method:
        if self.kind == Kind.CRAFT:
            if not self.reagents or not self.outputs:
                raise ValueError(f"{self.id}: craft methods need reagents and outputs")
        elif not self.yields and not self.vendor_copper_per_hour:
            raise ValueError(f"{self.id}: needs yields or vendor_copper_per_hour")
        if unknown := set(self.class_modifiers) - set(CLASSES):
            raise ValueError(f"{self.id}: unknown class(es) in class_modifiers: {sorted(unknown)}")
        return self

    @property
    def all_zones(self) -> list[str]:
        return self.zones or ([self.zone] if self.zone else [])

    @property
    def activity(self) -> Activity | None:
        if self.kind == Kind.CRAFT:
            return None
        if self.kind == Kind.FISHING:
            return Activity.FISHING
        if self.kind == Kind.GATHER:
            for prof in GATHER_PROFESSIONS:
                if prof in self.requirements.professions:
                    return Activity(prof)
        return Activity.KILL

    @property
    def item_ids(self) -> set[int]:
        return {i.item for i in (*self.yields, *self.reagents, *self.outputs)}


def load_methods(directory: Path) -> list[Method]:
    """Load every *.yaml file; each holds a list of methods."""
    methods: list[Method] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text()) or []
        if not isinstance(raw, list):
            raise ValueError(f"{path}: expected a list of methods")
        for entry in raw:
            try:
                methods.append(Method.model_validate(entry))
            except Exception as exc:
                raise ValueError(f"{path.name}: {exc}") from exc
    ids = [m.id for m in methods]
    if dupes := {i for i in ids if ids.count(i) > 1}:
        raise ValueError(f"duplicate method ids: {sorted(dupes)}")
    return methods
