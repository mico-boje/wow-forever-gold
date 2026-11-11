"""The character the plan is made for."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from ..methods.models import CLASSES, PROFESSIONS, Faction, GearTier

# Gathering/farming throughput by travel speed. Mounts at 40 (60% speed), epic at 60.
MOUNT_LEVEL = 40


class CharacterProfile(BaseModel):
    cls: str = Field(alias="class")
    level: int = Field(ge=1, le=60)
    faction: Faction
    professions: dict[str, int] = Field(default_factory=dict)
    gear: GearTier = GearTier.FRESH
    epic_mount: bool = False

    model_config = {"populate_by_name": True}

    @field_validator("cls")
    @classmethod
    def _cls(cls, v: str) -> str:
        v = v.lower()
        if v not in CLASSES:
            raise ValueError(f"unknown class {v!r}; one of {', '.join(CLASSES)}")
        return v

    @field_validator("professions")
    @classmethod
    def _profs(cls, v: dict[str, int]) -> dict[str, int]:
        v = {k.lower().replace(" ", "_").replace("-", "_"): s for k, s in v.items()}
        if unknown := set(v) - set(PROFESSIONS):
            raise ValueError(f"unknown profession(s): {sorted(unknown)}")
        return v

    @property
    def travel_multiplier(self) -> float:
        """Rough effect of travel speed on route-based income."""
        if self.level < MOUNT_LEVEL:
            return 0.75
        return 1.15 if self.epic_mount else 1.0


def parse_professions(spec: list[str]) -> dict[str, int]:
    """['herbalism:300', 'alchemy:275'] or ['herbalism:300,alchemy:275'] -> dict."""
    out: dict[str, int] = {}
    for chunk in spec:
        for part in chunk.split(","):
            part = part.strip()
            if not part:
                continue
            name, _, skill = part.partition(":")
            if not skill.isdigit():
                raise ValueError(f"expected profession:skill, got {part!r}")
            out[name.strip().lower().replace(" ", "_").replace("-", "_")] = int(skill)
    return out
