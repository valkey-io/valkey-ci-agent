"""Base-image pre-check for CVE rebuild decisions.

Verifies a rebuild-fixable CVE is actually patched in the upstream base image
(advisory FixedVersion may precede a republished base tag). Reads each base's
package database via one-shot ``docker run`` and compares with native dpkg/apk
semantics (version_compare), not the pure-Python approximation. The distro
repository candidate is consulted only for packages the build demonstrably
manages: those absent from the raw base db, or present in the base but shipped
newer in the scanned image (installed_version > base_version), which proves the
build installed or upgraded them. A package the base ships at the same version
the image ships is left untouched by the build, so a rebuild cannot change it
and it downgrades without a repo query. Any comparison error or ambiguity
downgrades conservatively (fail-closed). Deterministic, no AI, stdlib only.
"""

from __future__ import annotations

import logging
import subprocess

from scripts.cve_scan.models import Classification, Finding
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
# Distro repository candidate lookup (for packages absent from the base db)
# ---------------------------------------------------------------------------

#: Timeout for a repo candidate query (includes network index refresh).
_REPO_QUERY_TIMEOUT = 180  # seconds


def _flavor_for(base_ref: str) -> str:
    """Map a base image reference to a version-comparison flavor."""
    if base_ref.startswith("alpine:"):
        return "alpine"
    if base_ref.startswith("debian:"):
        return "debian"
    return "debian"  # unknown flavor: conservative default


def _parse_apk_policy(raw: str) -> list[str]:
    """Extract candidate versions from ``apk policy <pkg>`` output.

    Format: an unindented ``<pkg> policy:`` header, then two-space-indented
    ``<version>:`` lines, each followed by deeper-indented source lines.
    Returns every listed version; [] when none parse (fail-closed).
    """
    versions: list[str] = []
    for line in raw.splitlines():
        if line.startswith("  ") and not line.startswith("    "):
            stripped = line.strip()
            if stripped.endswith(":"):
                versions.append(stripped[:-1])
    return versions


def _parse_apt_cache_policy(raw: str) -> list[str]:
    """Extract the Candidate version from ``apt-cache policy <pkg>`` output.

    Returns [] when the ``Candidate:`` line is missing, empty, or
    ``(none)`` (fail-closed).
    """
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("Candidate:"):
            candidate = stripped[len("Candidate:"):].strip()
            if not candidate or candidate == "(none)":
                return []
            return [candidate]
    return []


def get_repo_candidates(base_ref: str, package: str, platform: str = "") -> list[str]:
    """Query the distro repository candidate versions for a package inside the base container.

    Refreshes the package index (network access required) and asks the
    package manager which version(s) the repository currently supplies.
    The package name is passed as a positional shell argument (``"$1"``),
    never interpolated into the ``sh -c`` script, so it cannot inject.

    Args:
        base_ref: Base image reference (e.g. "alpine:3.23", "debian:trixie-slim").
        package: Package name to query.
        platform: Optional platform passed as ``--platform`` to docker run.

    Returns:
        Candidate version strings, or [] on any failure (unknown flavor,
        docker/network failure, timeout, nonzero exit, unparseable output).
        Callers must treat [] as fail-closed.
    """
    if base_ref.startswith("alpine:"):
        script = 'apk update -q >/dev/null 2>&1 && apk policy "$1"'
        parse = _parse_apk_policy
    elif base_ref.startswith("debian:"):
        script = 'apt-get update -qq >/dev/null 2>&1 && apt-cache policy "$1"'
        parse = _parse_apt_cache_policy
    else:
        logger.warning(
            "Repo candidate query: unknown base flavor %r; treating candidate as unavailable.",
            base_ref,
        )
        return []

    cmd = ["docker", "run"]
    if platform:
        cmd.extend(["--platform", platform])
    cmd.extend(["--rm", base_ref, "sh", "-c", script, "_", package])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_REPO_QUERY_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "Repo candidate query timed out for %s in %s (timeout=%ds).",
            package, base_ref, _REPO_QUERY_TIMEOUT,
        )
        return []
    except OSError as exc:
        logger.warning(
            "Repo candidate query failed to execute for %s in %s: %s",
            package, base_ref, exc,
        )
        return []

    if result.returncode != 0:
        logger.warning(
            "Repo candidate query exited %d for %s in %s: %s",
            result.returncode, package, base_ref, result.stderr.strip(),
        )
        return []

    return parse(result.stdout)


def _find_satisfying_candidate(
    finding: Finding,
    base_ref: str,
    flavor: str,
    candidates: list[str],
) -> str | None:
    """Return the first repo candidate at or above the fix version, else None.

    The package manager installs the highest available candidate, so "any
    candidate >= fix" is equivalent to "max(candidates) >= fix" without extra
    docker calls to order the candidates themselves. Returns None when the fix
    version is unknown, no candidate satisfies it, or every comparison is
    ambiguous (fail-closed).
    """
    if finding.fixed_version is None:
        return None
    for candidate in candidates:
        cmp = _native_compare(candidate, finding.fixed_version, flavor, base_ref)
        if cmp is not None and cmp >= 0:
            return candidate
    return None


def _repo_candidates_cached(
    base_ref: str,
    finding: Finding,
    cache: dict[tuple[str, str, str], list[str]],
) -> list[str]:
    """Query the distro repo candidates for a finding's package, memoized.

    Shared by the absent-from-base and stale-base paths. The cache is keyed by
    (base_ref, platform, package) so the same package is queried at most once
    per base image and platform. Returns [] on any failure (fail-closed).
    """
    cand_key = (base_ref, finding.platform, finding.package)
    if cand_key not in cache:
        logger.info(
            "Querying repo candidate for %s in base %s (platform=%s) ...",
            finding.package, base_ref, finding.platform or "native",
        )
        cache[cand_key] = get_repo_candidates(
            base_ref, finding.package, platform=finding.platform,
        )
    return cache[cand_key]


def _verify_via_repo_candidate(
    classification: Classification,
    base_ref: str,
    flavor: str,
    base_version: str | None,
    cache: dict[tuple[str, str, str], list[str]],
) -> Classification:
    """Confirm or downgrade a finding by consulting the distro repo candidate.

    Shared by the two paths where the base image alone does not prove the fix
    AND the build is known to manage the package: the package is absent from
    the raw base db (installed at build time, so ``base_version`` is None), or
    the base ships an older version than the fix while the scanned image ships
    newer than the base (installed_version > base_version, so the build
    upgraded it). A rebuild reinstalls such build-managed packages from the
    distro repo, so confirm only when the repo currently supplies a candidate
    at or above the fix version; any other outcome (older candidate, no
    candidate, query or comparison failure) downgrades fail-closed. The
    rationale wording distinguishes the absent and stale cases so the job
    summary explains why each finding landed where it did.
    """
    finding = classification.finding
    candidates = _repo_candidates_cached(base_ref, finding, cache)
    satisfying = _find_satisfying_candidate(finding, base_ref, flavor, candidates)
    candidate_list = ", ".join(candidates) if candidates else "none"

    if satisfying is not None:
        if base_version is None:
            detail = (
                f"package {finding.package} not in base image {base_ref} "
                f"(installed at build time); repo candidate {satisfying} >= "
                f"fix {finding.fixed_version}, so a rebuild will pick up the fix."
            )
        else:
            detail = (
                f"base {base_ref} ships {base_version} "
                f"(< {finding.fixed_version}), but repo candidate {satisfying} "
                f"satisfies the fix, so a rebuild will pick it up."
            )
        return Classification(
            finding=finding,
            fixable=True,
            rationale=f"{classification.rationale} Verified: {detail}",
        )

    if base_version is None:
        rationale = (
            f"Fix for {finding.cve_id} in {finding.package}: package not in "
            f"base image {base_ref} and repo candidate unverified/older "
            f"(candidates: {candidate_list}; fix: {finding.fixed_version}). "
            f"Downgrading conservatively (fail-closed). Re-check next scan."
        )
    else:
        rationale = (
            f"Fix for {finding.cve_id} in {finding.package} is published "
            f"upstream but base image {base_ref} still ships {base_version} "
            f"(< {finding.fixed_version}) and the repo candidate was older or "
            f"unavailable (candidates: {candidate_list}); a rebuild would not "
            f"pick it up. Re-check next scan."
        )
    return Classification(finding=finding, fixable=False, rationale=rationale)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_fixable_in_base(
    fixable: list[Classification],
    base_map: dict[str, str],
) -> tuple[list[Classification], list[Classification]]:
    """Verify fixable findings against their base images' package databases.

    Reads each distinct (base image, platform) package list once (cached per
    invocation). A finding is confirmed when the base already ships the fix.
    When the base ships an older version, the distro repository candidate is
    consulted only if the scanned image ships newer than the base
    (installed_version > base_version), which proves the build manages the
    package; if the image ships the same version as the base (or older, or the
    comparison is ambiguous) the finding downgrades without a repo query. When
    the package is absent from the base db (installed at build time) the repo
    candidate is queried directly. In every repo-candidate case the finding is
    confirmed only when that candidate is at or above the fix version;
    otherwise it is downgraded (fail-closed). Ambiguous comparisons also
    downgrade (fail-closed).

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
    # Cache: (base_ref, platform, package) -> repo candidate versions
    repo_candidate_cache: dict[tuple[str, str, str], list[str]] = {}

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

        flavor = _flavor_for(base_ref)
        base_version = base_packages.get(finding.package)

        if base_version is None:
            # Package absent from the raw base db (installed at build time).
            # Absence alone proves nothing about whether the repo currently
            # supplies the fix: verify against the repo candidate (fail-closed).
            result = _verify_via_repo_candidate(
                classification, base_ref, flavor, None, repo_candidate_cache,
            )
            (confirmed if result.fixable else downgraded).append(result)
            continue

        # Native dpkg/apk comparison; None -> fail-closed (downgrade)
        if finding.fixed_version is None:
            # Should not happen for fixable findings, but be safe
            confirmed.append(classification)
            continue

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
            # Base ships an older version than the fix. Consulting the repo
            # candidate is only legitimate when the build actually manages this
            # package; use the scanned image's installed_version as evidence.
            # (Fix: repo-candidate promotion was too broad and marked packages
            # the Dockerfile never installs/upgrades, e.g. zlib1g, as fixable.)
            installed = finding.installed_version
            build_cmp = _native_compare(installed, base_version, flavor, base_ref)
            if build_cmp is not None and build_cmp > 0:
                # Image ships newer than base: the build demonstrably upgraded
                # this package, so it is build-managed. The repo candidate is a
                # legitimate signal for what a rebuild would install.
                result = _verify_via_repo_candidate(
                    classification, base_ref, flavor, base_version, repo_candidate_cache,
                )
                (confirmed if result.fixable else downgraded).append(result)
            elif build_cmp == 0:
                # Image ships exactly the base version: the build never touched
                # this package, so a rebuild from the same base cannot upgrade
                # it. Downgrade fail-closed without querying the repo.
                rationale = (
                    f"Fix for {finding.cve_id} in {finding.package} is "
                    f"published upstream but base image {base_ref} ships "
                    f"{base_version} and the scanned image ships the same "
                    f"version, so the build does not upgrade it and a rebuild "
                    f"would not change it. Needs a base image update. "
                    f"Re-check next scan."
                )
                downgraded.append(Classification(
                    finding=finding, fixable=False, rationale=rationale,
                ))
            else:
                # Image older than base, or the comparison is ambiguous/None:
                # no evidence the build manages this package. Downgrade
                # fail-closed without querying the repo.
                if build_cmp is None:
                    reason = (
                        f"the image-vs-base comparison ({installed} vs "
                        f"{base_version}) is ambiguous"
                    )
                else:
                    reason = (
                        f"the scanned image ships {installed}, older than base "
                        f"{base_version}"
                    )
                rationale = (
                    f"Fix for {finding.cve_id} in {finding.package}: {reason}, "
                    f"so there is no evidence the build upgrades this package "
                    f"beyond the base. Downgrading conservatively "
                    f"(fail-closed). Re-check next scan."
                )
                downgraded.append(Classification(
                    finding=finding, fixable=False, rationale=rationale,
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
