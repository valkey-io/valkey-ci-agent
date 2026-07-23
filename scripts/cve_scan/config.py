"""Settings loader for the CVE scan workflow (env-var house style).

Configuration is driven by prefixed environment variables with sensible
defaults, consistent with the ci_fix and release_notes workflows in this
repo. A dedicated config file is unnecessary: only one container repo
(valkey-container) exists.

Env vars:
    CVE_SCAN_VERSIONS_URL     - versions.json manifest URL for dynamic resolution
    CVE_SCAN_REPOSITORY       - Docker Hub repository prefix for derived tags
    CVE_SCAN_INCLUDE_UNSTABLE - include the unstable version line (default false)
    CVE_SCAN_SCANNER          - vulnerability scanner (trivy only)
    CVE_SCAN_SEVERITY_THRESHOLD - ignore findings below this severity
    CVE_SCAN_IMAGES           - optional static image list (overrides dynamic)
    CVE_SCAN_PLATFORMS        - comma-separated platforms to scan per image
                                (default: verified published platforms for valkey)

Invalid values raise immediately: an env typo must not silently scan nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from scripts.cve_scan.models import Severity

_VALID_SCANNERS = frozenset({"trivy"})
_TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSY_STRINGS = frozenset({"0", "false", "no", "off", ""})

_DEFAULT_VERSIONS_URL = (
    "https://raw.githubusercontent.com/valkey-io/valkey-container"
    "/mainline/versions.json"
)
_DEFAULT_REPOSITORY = "valkey/valkey"
_DEFAULT_SCANNER = "trivy"
_DEFAULT_SEVERITY_THRESHOLD = "HIGH"

# Verified published platforms for valkey images (inspected via
# `docker buildx imagetools inspect public.ecr.aws/valkey/valkey:8.0`
# and `public.ecr.aws/valkey/valkey:8.0-alpine`; both publish the same set).
# Note: linux/386 is NOT published; linux/ppc64le IS.
DEFAULT_PLATFORMS: list[str] = [
    "linux/amd64",
    "linux/arm64",
    "linux/arm/v7",
    "linux/ppc64le",
]

_DEFAULT_PLATFORMS_STR = ",".join(DEFAULT_PLATFORMS)


class CveScanConfigError(Exception):
    """Raised when CVE scan settings are missing or invalid."""


@dataclass(frozen=True)
class CveScanSettings:
    """Typed, immutable settings for the CVE scan workflow."""

    versions_url: str
    repository: str
    include_unstable: bool
    scanner: str
    severity_threshold: Severity
    images: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)


def load_settings() -> CveScanSettings:
    """Build CveScanSettings from CVE_SCAN_* env vars with defaults.

    Raises :class:`CveScanConfigError` on invalid values.
    """
    versions_url = os.environ.get("CVE_SCAN_VERSIONS_URL", _DEFAULT_VERSIONS_URL)
    repository = os.environ.get("CVE_SCAN_REPOSITORY", _DEFAULT_REPOSITORY)

    include_unstable_raw = os.environ.get("CVE_SCAN_INCLUDE_UNSTABLE", "false").strip().lower()
    if include_unstable_raw in _TRUTHY_STRINGS:
        include_unstable = True
    elif include_unstable_raw in _FALSY_STRINGS:
        include_unstable = False
    else:
        raise CveScanConfigError(
            f"Invalid CVE_SCAN_INCLUDE_UNSTABLE: {include_unstable_raw!r}. "
            f"Must be one of (case-insensitive): "
            f"truthy {sorted(_TRUTHY_STRINGS)}, falsy {sorted(_FALSY_STRINGS)}"
        )

    scanner = os.environ.get("CVE_SCAN_SCANNER", _DEFAULT_SCANNER).strip().lower()
    if scanner not in _VALID_SCANNERS:
        raise CveScanConfigError(
            f"Invalid CVE_SCAN_SCANNER: {scanner!r}. Must be 'trivy'."
        )

    severity_raw = os.environ.get(
        "CVE_SCAN_SEVERITY_THRESHOLD", _DEFAULT_SEVERITY_THRESHOLD
    ).strip()
    try:
        severity_threshold = Severity.from_str(severity_raw)
    except ValueError as exc:
        raise CveScanConfigError(
            f"Invalid CVE_SCAN_SEVERITY_THRESHOLD: {exc}"
        ) from exc

    images_raw = os.environ.get("CVE_SCAN_IMAGES", "")
    images = [img.strip() for img in images_raw.split(",") if img.strip()]

    platforms_raw = os.environ.get("CVE_SCAN_PLATFORMS", _DEFAULT_PLATFORMS_STR)
    platforms = [p.strip() for p in platforms_raw.split(",") if p.strip()]
    if not platforms:
        raise CveScanConfigError(
            "CVE_SCAN_PLATFORMS must contain at least one non-empty platform"
        )

    return CveScanSettings(
        versions_url=versions_url,
        repository=repository,
        include_unstable=include_unstable,
        scanner=scanner,
        severity_threshold=severity_threshold,
        images=images,
        platforms=platforms,
    )
