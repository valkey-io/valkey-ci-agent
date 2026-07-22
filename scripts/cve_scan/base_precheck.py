"""Base-image pre-check for CVE rebuild decisions.

Verifies that a rebuild-fixable CVE has actually been patched in the upstream
base image (alpine:X.Y / debian:Z-slim) before requesting a container rebuild.
This closes the gap where a distro advisory publishes a FixedVersion but the
base image tag has not yet been republished with the patched package.

Algorithm:
    For each fixable Classification, read the corresponding base image's
    package database (apk for Alpine, dpkg for Debian) via a one-shot
    ``docker run`` and compare installed package versions against the
    finding's fixed_version. If the base ships an older version than the
    fix, the base is stale and a rebuild would not help: downgrade the
    classification. Otherwise the fix is confirmed present in the base.

Version comparison at the safety gate uses native dpkg/apk semantics via
version_compare.compare_versions(), not the pure-Python approximation in
rebuild_decider. Any comparison error or None result downgrades conservatively
(fail-closed).

Security model: deterministic, no AI, stdlib only.
"""

from __future__ import annotations

import logging
import subprocess

from scripts.cve_scan.models import Classification
from scripts.cve_scan.version_compare import compare_versions as _native_compare

logger = logging.getLogger(__name__)


class BasePrecheckError(Exception):
    """Raised when the base image package database cannot be read."""


# ---------------------------------------------------------------------------
# Package database readers
# ---------------------------------------------------------------------------

_DOCKER_TIMEOUT = 300  # seconds


def _parse_apk_installed(raw: str) -> dict[str, str]:
    """Parse Alpine's /lib/apk/db/installed format.

    The file contains blank-line-separated stanzas. Within each stanza:
      P:<package_name>
      V:<version>

    Returns a dict mapping package name to version string.
    """
    packages: dict[str, str] = {}
    name: str | None = None
    version: str | None = None

    for line in raw.splitlines():
        if line.startswith("P:"):
            name = line[2:]
        elif line.startswith("V:"):
            version = line[2:]
        elif line == "":
            # End of stanza
            if name is not None and version is not None:
                packages[name] = version
            name = None
            version = None

    # Handle last stanza if file does not end with blank line
    if name is not None and version is not None:
        packages[name] = version

    return packages


def _parse_dpkg_query(raw: str) -> dict[str, str]:
    """Parse dpkg-query -W -f '${Package} ${Version}\\n' output.

    Each line is: <package_name> <version>
    """
    packages: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            packages[parts[0]] = parts[1]
    return packages


def get_base_packages(base_ref: str) -> dict[str, str]:
    """Read the base image's package database.

    Runs the base image once via docker to extract the installed package list.
    Alpine uses /lib/apk/db/installed; Debian uses dpkg-query.

    Args:
        base_ref: Base image reference (e.g. "alpine:3.23", "debian:trixie-slim").

    Returns:
        Mapping of package name to installed version string.

    Raises:
        BasePrecheckError: On unknown base flavor, docker failure, or empty output.
    """
    if base_ref.startswith("alpine:"):
        cmd = ["docker", "run", "--rm", base_ref, "cat", "/lib/apk/db/installed"]
        parser = _parse_apk_installed
    elif base_ref.startswith("debian:"):
        cmd = [
            "docker", "run", "--rm", base_ref,
            "dpkg-query", "-W", "-f", "${Package} ${Version}\n",
        ]
        parser = _parse_dpkg_query
    else:
        raise BasePrecheckError(
            f"Unknown base image flavor: {base_ref!r}. "
            f"Expected prefix 'alpine:' or 'debian:'."
        )

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BasePrecheckError(
            f"Timed out reading package database from {base_ref} "
            f"(timeout={_DOCKER_TIMEOUT}s)."
        ) from exc

    if result.returncode != 0:
        raise BasePrecheckError(
            f"docker run failed for {base_ref} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )

    if not result.stdout.strip():
        raise BasePrecheckError(
            f"Empty package database output from {base_ref}."
        )

    return parser(result.stdout)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_fixable_in_base(
    fixable: list[Classification],
    base_map: dict[str, str],
) -> tuple[list[Classification], list[Classification]]:
    """Verify fixable findings against their base images' package databases.

    Reads each distinct base image's package list once (cached per invocation)
    and compares installed versions against the finding's fixed_version.
    Findings confirmed fixable stay in the confirmed list; findings where the
    base is still vulnerable (or comparison is ambiguous) are downgraded.

    Args:
        fixable: Classifications previously marked as rebuild-fixable.
        base_map: Mapping of derived image -> base image reference.

    Returns:
        Tuple of (confirmed, downgraded) classification lists.

    Raises:
        BasePrecheckError: If a base image package database cannot be read.
    """
    if not fixable:
        return [], []

    # Cache: base_ref -> {package: version}
    base_pkg_cache: dict[str, dict[str, str]] = {}

    confirmed: list[Classification] = []
    downgraded: list[Classification] = []

    for classification in fixable:
        finding = classification.finding
        base_ref = base_map.get(finding.image)

        if base_ref is None:
            # No base mapping available: fail-closed (conservative).
            # Unreachable in dynamic mode after the resolve_matrix merge
            # (base_map keys == images). Reachable only in static mode
            # where base_map is empty and pre-check should not be called.
            logger.warning(
                "No base image mapping for %s; downgrading %s/%s conservatively.",
                finding.image,
                finding.cve_id,
                finding.package,
            )
            downgraded.append(Classification(
                finding=finding,
                fixable=False,
                rationale=(
                    f"No base image mapping for {finding.image}; cannot verify "
                    f"fix presence. Downgrading conservatively (fail-closed)."
                ),
            ))
            continue

        # Read base package database (cached)
        if base_ref not in base_pkg_cache:
            logger.info(
                "Reading base image package database: %s ...", base_ref
            )
            base_pkg_cache[base_ref] = get_base_packages(base_ref)

        base_packages = base_pkg_cache[base_ref]

        # Look up the finding's package in the base
        base_version = base_packages.get(finding.package)

        if base_version is None:
            # Package not in base image (installed at build time)
            rationale = (
                f"{classification.rationale} "
                f"Verified: package {finding.package} not in base image "
                f"{base_ref} (installed at build time from the package "
                f"repository); rebuild will fetch the latest version."
            )
            confirmed.append(Classification(
                finding=finding,
                fixable=True,
                rationale=rationale,
            ))
            continue

        # Compare base version against the fix version using native semantics.
        # _native_compare uses dpkg/apk inside docker for correct ordering.
        # Any error returns None -> fail-closed (downgrade).
        if finding.fixed_version is None:
            # Should not happen for fixable findings, but be safe
            confirmed.append(classification)
            continue

        # Determine flavor from base_ref prefix
        if base_ref.startswith("alpine:"):
            flavor = "alpine"
        elif base_ref.startswith("debian:"):
            flavor = "debian"
        else:
            flavor = "debian"  # unknown flavor: conservative default

        cmp = _native_compare(base_version, finding.fixed_version, flavor, base_ref)

        if cmp is None:
            # Ambiguous version comparison: downgrade conservatively
            rationale = (
                f"Fix for {finding.cve_id} in {finding.package}: version "
                f"comparison between base version {base_version} and fix "
                f"version {finding.fixed_version} is ambiguous. "
                f"Downgrading conservatively. Re-check next scan."
            )
            downgraded.append(Classification(
                finding=finding,
                fixable=False,
                rationale=rationale,
            ))
        elif cmp < 0:
            # Base version older than fix: base is stale
            rationale = (
                f"Fix for {finding.cve_id} in {finding.package} is published "
                f"upstream but base image {base_ref} still ships "
                f"{base_version} (< {finding.fixed_version}); a rebuild "
                f"would not pick it up. Re-check next scan."
            )
            downgraded.append(Classification(
                finding=finding,
                fixable=False,
                rationale=rationale,
            ))
        else:
            # Base version >= fix: fix is present in base
            rationale = (
                f"{classification.rationale} "
                f"Verified: base {base_ref} ships {base_version}."
            )
            confirmed.append(Classification(
                finding=finding,
                fixable=True,
                rationale=rationale,
            ))

    return confirmed, downgraded
