"""Battle.net namespace helpers for WoW: Forever.

Forever namespaces are undocumented; these are the forms observed by the community
around launch (Nov 2026). See PLAN.md section 2.
"""

from __future__ import annotations

from enum import StrEnum


class NamespaceKind(StrEnum):
    DYNAMIC = "dynamic"
    STATIC = "static"
    STATIC_VERSIONED = "static_versioned"
    PROFILE = "profile"


def dynamic(region: str) -> str:
    return f"dynamic-forever-{region}"


def static(region: str, version: str | None = None) -> str:
    if version:
        return f"static-{version}-forever-{region}"
    return f"static-forever-{region}"


def classic_era(region: str) -> str:
    """Classic Era static data: fallback for vanilla items missing from Forever's static data."""
    return f"static-classic1x-{region}"


def profile(region: str) -> str:
    return f"profile-forever-{region}"


def resolve(kind: NamespaceKind, region: str, static_version: str | None = None) -> str:
    match kind:
        case NamespaceKind.DYNAMIC:
            return dynamic(region)
        case NamespaceKind.STATIC:
            return static(region)
        case NamespaceKind.STATIC_VERSIONED:
            return static(region, static_version)
        case NamespaceKind.PROFILE:
            return profile(region)
