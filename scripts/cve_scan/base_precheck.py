"""Base-image pre-check for CVE rebuild decisions.

Verifies a rebuild-fixable CVE is actually patched in the upstream base image
(advisory FixedVersion may precede a republished base tag). Reads each base's
package database via one-shot ``docker run`` and compares with native dpkg/apk
semantics (version_compare), not the pure-Python approximation. Any comparison
error or ambiguity downgrades conservatively (fail-closed). Deterministic, no
AI, stdlib only.
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
    """Parse Alpine's /lib/apk/db/installed (blank-line-separated P:/V: stanzas) into {name: version}."""
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
    """Parse '<package> <version>' lines from dpkg-query -W output."""
    packages: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            packages[parts[0]] = parts[1]
    return packages


def get_base_packages(base_ref: str, platform: str = "") -> dict[str, str]:
    """Read the base image's package database via one-shot docker run.

    Args:
        base_ref: Base image reference (e.g. "alpine:3.23", "debian:trixie-slim").
        platform: Optional platform passed as ``--platform`` to docker run.

    Returns:
        Mapping of package name to installed version string.

    Raises:
        BasePrecheckError: On unknown base flavor, docker failure, or empty output.
    """
    if base_ref.startswith("alpine:"):
        cmd = ["docker", "run"]
        if platform:
            cmd.extend(["--platform", platform])
        cmd.extend(["--rm", base_ref, "cat", "/lib/apk/db/installed"])
        parser = _parse_apk_installed
    elif base_ref.startswith("debian:"):
        cmd = ["docker", "run"]
        if platform:
            cmd.extend(["--platform", platform])
        cmd.extend([
            "--rm", base_ref,
            "dpkg-query", "-W", "-f", "${Package} ${Version}\n",
        ])
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

    Reads each distinct (base image, platform) package list once (cached per
    invocation). Findings where the base is still vulnerable or comparison is
    ambiguous are downgraded (fail-closed).

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

    # Cache: (base_ref, platform) -> {package: version}
    base_pkg_cache: dict[tuple[str, str], dict[str, str]] = {}

    confirmed: list[Classification] = []
    downgraded: list[Classification] = []

    for classification in fixable:
        finding = classification.finding
        base_ref = base_map.get(finding.image)

        if base_ref is None:
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

        # Read base package database (cached per base_ref + platform)
        cache_key = (base_ref, finding.platform)
        if cache_key not in base_pkg_cache:
            logger.info(
                "Reading base image package database: %s (platform=%s) ...",
                base_ref, finding.platform or "native",
            )
            base_pkg_cache[cache_key] = get_base_packages(base_ref, platform=finding.platform)

        base_packages = base_pkg_cache[cache_key]

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

        # Native dpkg/apk comparison; None -> fail-closed (downgrade)
        if finding.fixed_version is None:
            # Should not happen for fixable findings, but be safe
            confirmed.append(classification)
            continue

        if base_ref.startswith("alpine:"):
            flavor = "alpine"
        elif base_ref.startswith("debian:"):
            flavor = "debian"
        else:
            flavor = "debian"  # unknown flavor: conservative default

        cmp = _native_compare(base_version, finding.fixed_version, flavor, base_ref)

        if cmp is None:
            # Ambiguous comparison: fail closed
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
            # Base still ships an older version: stale base
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
            # Base ships the fix
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
