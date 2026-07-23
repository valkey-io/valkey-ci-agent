"""Data models for the CVE scan workflow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Severity(IntEnum):
    """CVE severity levels, ordered low to high for threshold comparison.

    Usage: ``finding_severity >= Severity[config.severity_threshold]``
    """

    UNKNOWN = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4

    @classmethod
    def from_str(cls, value: str) -> Severity:
        """Parse a case-insensitive severity string, raising ValueError on unknown input."""
        try:
            return cls[value.upper()]
        except KeyError:
            raise ValueError(f"Unknown severity: {value!r}. Valid values: {[s.name for s in cls]}")


@dataclass
class Finding:
    """A single CVE finding from a scanner."""

    image: str
    package: str
    installed_version: str
    cve_id: str
    severity: Severity
    fixed_version: str | None
    platform: str = ""


@dataclass
class Classification:
    """Rebuild-fixability classification for a finding."""

    finding: Finding
    fixable: bool
    rationale: str



