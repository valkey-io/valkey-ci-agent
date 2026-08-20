"""Base-image pre-check for CVE rebuild decisions.

Answers one question per fixable finding: if we rebuild this image, will the
affected package land at or above the CVE's fixed version? Version numbers
alone cannot tell whether a rebuild upgrades a package (e.g. Debian's
``libssl-dev`` drags a newer ``libssl3t64``; Alpine's ``openssl`` drags newer
``libcrypto3``/``libssl3``) or leaves it untouched (e.g. ``zlib1g``, referenced
by no Dockerfile install). So instead of guessing, we simulate the Dockerfile's
own install transaction inside the base image and read the version the rebuild
would land on.

Decision, per finding:
  * base already ships the fix -> confirmed (fast path, no simulation).
  * package is in the simulated install plan at or above the fix -> confirmed.
  * package is in the plan but below the fix -> downgraded.
  * package is not in the plan -> the rebuild leaves the base version, so
    confirm only when the base already satisfies the fix, else downgrade
    (needs a base image update).
  * any fetch/parse/simulation/comparison failure -> downgraded (fail-closed).

Comparisons use native dpkg/apk semantics (version_compare), not a pure-Python
approximation. Deterministic, no AI, stdlib only.
"""

from __future__ import annotations

import logging
import re
import subprocess
import urllib.request
from urllib.error import URLError

from scripts.cve_scan.models import Classification, Finding
from scripts.cve_scan.version_compare import compare_versions as _native_compare

logger = logging.getLogger(__name__)


class BasePrecheckError(Exception):
    """Raised when a base image package database or rebuild plan cannot be read."""


#: Timeout for reading a base image's package database.
_DOCKER_TIMEOUT = 300  # seconds
#: Timeout for a rebuild install simulation (includes network index refresh).
_SIMULATE_TIMEOUT = 180  # seconds
#: Timeout for fetching a Dockerfile.
_FETCH_TIMEOUT = 15  # seconds

#: valkey-container Dockerfiles live at deterministic paths on branch mainline.
_DOCKERFILE_URL_TEMPLATE = (
    "https://raw.githubusercontent.com/valkey-io/valkey-container"
    "/mainline/{line}/{variant}/Dockerfile"
)


# ---------------------------------------------------------------------------
# Package database readers
# ---------------------------------------------------------------------------


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
        BasePrecheckError: On unknown base flavor, docker failure (including
            OSError launching docker), timeout, empty output, or nonempty
            output that parses to zero packages.
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
    except OSError as exc:
        raise BasePrecheckError(
            f"Failed to run docker to read package database from {base_ref}: {exc}"
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

    packages = parser(result.stdout)
    if not packages:
        # Nonempty but unparseable: treating this as an empty db would make
        # every package look absent and confirmable with no base evidence.
        raise BasePrecheckError(
            f"Read {len(result.stdout)} bytes from {base_ref} package database "
            f"but parsed zero packages (unrecognized format)."
        )
    return packages


def _flavor_for(base_ref: str) -> str:
    """Map a base image reference to a version-comparison flavor."""
    if base_ref.startswith("alpine:"):
        return "alpine"
    if base_ref.startswith("debian:"):
        return "debian"
    return "debian"  # unknown flavor: conservative default


# ---------------------------------------------------------------------------
# Dockerfile fetch and install-list parsing
# ---------------------------------------------------------------------------


def _dockerfile_target(image: str) -> tuple[str, str]:
    """Derive (line, variant) from a valkey image ref.

    ``valkey/valkey:9.0`` -> ``("9.0", "debian")``;
    ``valkey/valkey:9.0-alpine`` -> ``("9.0", "alpine")``. Strips the
    ``-alpine`` suffix only, not any other occurrence.
    """
    tag = image.rsplit(":", 1)[-1] if ":" in image else image
    if tag.endswith("-alpine"):
        return tag[: -len("-alpine")], "alpine"
    return tag, "debian"


def _fetch_dockerfile(line: str, variant: str) -> str:
    """Fetch a valkey-container Dockerfile for the given line and variant.

    Reuses the urllib pattern (and timeout style) from image_matrix; no new
    HTTP dependency. Raises BasePrecheckError on any network or status error.
    """
    url = _DOCKERFILE_URL_TEMPLATE.format(line=line, variant=variant)
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "valkey-ci-agent/cve-scan"}
        )
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            if resp.status != 200:
                raise BasePrecheckError(
                    f"Failed to fetch Dockerfile: HTTP {resp.status} from {url}"
                )
            return resp.read().decode("utf-8")
    except (URLError, OSError, TimeoutError) as exc:
        raise BasePrecheckError(
            f"Failed to fetch Dockerfile from {url}: {exc}"
        ) from exc


def _packages_from_segment(tokens: list[str]) -> list[str]:
    """Extract package tokens from a single ``apk add`` / ``apt-get install`` command.

    Skips flags (``-*``), the ``-t``/``--virtual`` argument, apk virtual names
    (``.build-deps``), and shell variables (``$...``). Returns [] when the
    segment is not an install command.
    """
    start: int | None = None
    for i in range(len(tokens) - 1):
        if tokens[i] == "apk" and tokens[i + 1] == "add":
            start = i + 2
            break
        if tokens[i] == "apt-get" and tokens[i + 1] == "install":
            start = i + 2
            break
    if start is None:
        return []

    packages: list[str] = []
    skip_next = False
    for tok in tokens[start:]:
        if skip_next:
            skip_next = False  # consume the --virtual/-t name argument
            continue
        if tok in ("-t", "--virtual"):
            skip_next = True
            continue
        if tok.startswith(("-", ".", "$")):
            continue  # flag, apk virtual name, or shell variable
        packages.append(tok)
    return packages


def _parse_install_list(dockerfile: str) -> list[str]:
    """Collect package tokens from every ``apk add`` / ``apt-get install`` block.

    Strips comment lines FIRST: the real valkey-container Dockerfiles put a
    ``#`` comment inside the install block, and it carries no trailing
    backslash, so merging continuations first would both absorb the comment
    prose as packages and orphan the real package names onto a segment with no
    install verb. Then merges backslash continuations, splits into command
    segments on ``;``, ``&&``, and newlines, and unions the packages from every
    install segment (build-stage packages are harmless: Trivy only scans the
    final image). Order-preserving and de-duplicated.
    """
    without_comments = "\n".join(
        line for line in dockerfile.splitlines()
        if not line.lstrip().startswith("#")
    )
    merged = re.sub(r"\\\r?\n", " ", without_comments)
    packages: list[str] = []
    seen: set[str] = set()
    for segment in re.split(r";|&&|\n", merged):
        for pkg in _packages_from_segment(segment.split()):
            if pkg not in seen:
                seen.add(pkg)
                packages.append(pkg)
    return packages


# ---------------------------------------------------------------------------
# Rebuild install simulation
# ---------------------------------------------------------------------------

#: apt --dry-run: ``Inst <pkg> [old] (<ver> ...`` or ``Conf <pkg> (<ver> ...``.
_APT_PLAN_RE = re.compile(r"^(Inst|Conf)\s+(\S+)\s+(?:\[[^\]]*\]\s+)?\(([^\s)]+)")
#: apk --simulate: ``(1/5) Installing <pkg> (<ver>)`` / ``Upgrading <pkg> (<old> -> <new>)``.
_APK_PLAN_RE = re.compile(
    r"^\(\d+/\d+\)\s+(?:Installing|Upgrading|Reinstalling)\s+(\S+)\s+\((.+?)\)\s*$"
)


def _parse_apt_plan(raw: str) -> dict[str, str]:
    """Parse ``apt-get install --dry-run`` output into {package: planned_version}.

    ``Inst`` lines (the version being installed/upgraded) take precedence over
    ``Conf`` lines.
    """
    plan: dict[str, str] = {}
    conf: dict[str, str] = {}
    for line in raw.splitlines():
        m = _APT_PLAN_RE.match(line.strip())
        if not m:
            continue
        kind, pkg, ver = m.group(1), m.group(2), m.group(3)
        if kind == "Inst":
            plan[pkg] = ver
        else:
            conf.setdefault(pkg, ver)
    for pkg, ver in conf.items():
        plan.setdefault(pkg, ver)
    return plan


def _parse_apk_plan(raw: str) -> dict[str, str]:
    """Parse ``apk add --simulate`` output into {package: planned_version}.

    For ``Upgrading <pkg> (<old> -> <new>)`` the post-arrow version is kept.
    """
    plan: dict[str, str] = {}
    for line in raw.splitlines():
        m = _APK_PLAN_RE.match(line.strip())
        if not m:
            continue
        pkg, ver = m.group(1), m.group(2)
        if "->" in ver:
            ver = ver.split("->")[-1].strip()
        plan[pkg] = ver
    return plan


def simulate_install(
    base_ref: str, packages: list[str], platform: str = "",
) -> dict[str, str]:
    """Simulate the Dockerfile install transaction inside the base image.

    Runs the package manager in dry-run/simulate mode via one-shot docker run
    and returns the versions the rebuild would land on. Package names are
    passed as positional shell arguments (``"$@"``), never interpolated into
    the script, so they cannot inject.

    Args:
        base_ref: Base image reference (e.g. "alpine:3.23", "debian:trixie-slim").
        packages: Install list parsed from the Dockerfile.
        platform: Optional platform passed as ``--platform`` to docker run.

    Returns:
        Mapping of package name to the version a rebuild would install.

    Raises:
        BasePrecheckError: On empty install list, unknown base flavor, docker
            failure (including OSError), timeout, nonzero exit, or a plan that
            parses to zero packages.
    """
    if not packages:
        raise BasePrecheckError(
            f"No install packages parsed for {base_ref}; cannot simulate rebuild."
        )

    if base_ref.startswith("alpine:"):
        script = 'apk update -q && apk add --simulate "$@"'
        parse = _parse_apk_plan
    elif base_ref.startswith("debian:"):
        script = (
            'apt-get update -qq && '
            'apt-get install --dry-run --no-install-recommends "$@"'
        )
        parse = _parse_apt_plan
    else:
        raise BasePrecheckError(
            f"Unknown base image flavor: {base_ref!r}. "
            f"Expected prefix 'alpine:' or 'debian:'."
        )

    cmd = ["docker", "run"]
    if platform:
        cmd.extend(["--platform", platform])
    cmd.extend(["--rm", base_ref, "sh", "-c", script, "_", *packages])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SIMULATE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BasePrecheckError(
            f"Timed out simulating install in {base_ref} "
            f"(timeout={_SIMULATE_TIMEOUT}s)."
        ) from exc
    except OSError as exc:
        raise BasePrecheckError(
            f"Failed to run docker to simulate install in {base_ref}: {exc}"
        ) from exc

    if result.returncode != 0:
        raise BasePrecheckError(
            f"Install simulation failed for {base_ref} (exit {result.returncode}): "
            f"{result.stderr.strip()}"
        )

    # apt prints the plan to stdout; apk emits progress to stdout or stderr.
    plan = parse(result.stdout + "\n" + result.stderr)
    if not plan:
        raise BasePrecheckError(
            f"Parsed an empty install plan from the {base_ref} simulation "
            f"({len(result.stdout)} stdout bytes)."
        )
    return plan


def _rebuild_plan(
    finding: Finding,
    base_ref: str,
    install_list_cache: dict[tuple[str, str], list[str]],
    plan_cache: dict[tuple[str, str, tuple[str, ...]], dict[str, str]],
) -> dict[str, str]:
    """Fetch + parse the Dockerfile and simulate its install, with caching.

    The install list is cached per (line, variant); the simulated plan is
    cached per (base_ref, platform, install-list). Raises BasePrecheckError on
    any fetch, parse, or simulation failure (caller downgrades fail-closed).
    """
    line, variant = _dockerfile_target(finding.image)
    df_key = (line, variant)
    if df_key not in install_list_cache:
        dockerfile = _fetch_dockerfile(line, variant)
        install_list_cache[df_key] = _parse_install_list(dockerfile)
    install_list = install_list_cache[df_key]

    plan_key = (base_ref, finding.platform, tuple(install_list))
    if plan_key not in plan_cache:
        logger.info(
            "Simulating rebuild install for %s in base %s (platform=%s) ...",
            finding.image, base_ref, finding.platform or "native",
        )
        plan_cache[plan_key] = simulate_install(
            base_ref, install_list, platform=finding.platform,
        )
    return plan_cache[plan_key]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _confirmed(finding: Finding, rationale: str) -> Classification:
    """Build a confirmed (fixable) classification."""
    return Classification(finding=finding, fixable=True, rationale=rationale)


def _downgraded(finding: Finding, rationale: str) -> Classification:
    """Build a downgraded (not fixable) classification."""
    return Classification(finding=finding, fixable=False, rationale=rationale)


def verify_fixable_in_base(
    fixable: list[Classification],
    base_map: dict[str, str],
) -> tuple[list[Classification], list[Classification]]:
    """Verify fixable findings against a simulated rebuild of their base images.

    Each distinct (base image, platform) package list is read once, and each
    distinct (base, platform, install-list) rebuild is simulated once (both
    cached per invocation). A finding is confirmed when the base already ships
    the fix (fast path, no simulation) or when the simulated rebuild install
    plan lands the package at or above the fix. A package absent from the plan
    stays at the base version, so it confirms only when the base already
    satisfies the fix. Any fetch, parse, simulation, or comparison failure
    downgrades fail-closed.

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

    base_pkg_cache: dict[tuple[str, str], dict[str, str]] = {}
    install_list_cache: dict[tuple[str, str], list[str]] = {}
    plan_cache: dict[tuple[str, str, tuple[str, ...]], dict[str, str]] = {}

    confirmed: list[Classification] = []
    downgraded: list[Classification] = []

    for classification in fixable:
        finding = classification.finding
        base_ref = base_map.get(finding.image)

        if base_ref is None:
            logger.warning(
                "No base image mapping for %s; downgrading %s/%s conservatively.",
                finding.image, finding.cve_id, finding.package,
            )
            downgraded.append(_downgraded(
                finding,
                f"No base image mapping for {finding.image}; cannot verify fix "
                f"presence. Downgrading conservatively (fail-closed).",
            ))
            continue

        # Read base package database (cached per base_ref + platform).
        # BasePrecheckError here propagates (unreadable base db is fatal).
        cache_key = (base_ref, finding.platform)
        if cache_key not in base_pkg_cache:
            logger.info(
                "Reading base image package database: %s (platform=%s) ...",
                base_ref, finding.platform or "native",
            )
            base_pkg_cache[cache_key] = get_base_packages(
                base_ref, platform=finding.platform,
            )
        base_packages = base_pkg_cache[cache_key]

        flavor = _flavor_for(base_ref)
        base_version = base_packages.get(finding.package)
        fixed = finding.fixed_version

        # Item B: no known fixed version -> fail-closed (unreachable via
        # classify today; kept as an explicit downgrade, not a silent confirm).
        if fixed is None:
            downgraded.append(_downgraded(
                finding,
                f"Fix for {finding.cve_id} in {finding.package} has no known "
                f"fixed version; cannot verify a rebuild resolves it. "
                f"Downgrading conservatively (fail-closed).",
            ))
            continue

        # Fast path: base already ships the fix (no simulation needed).
        if base_version is not None:
            cmp = _native_compare(base_version, fixed, flavor, base_ref)
            if cmp is None:
                downgraded.append(_downgraded(
                    finding,
                    f"Fix for {finding.cve_id} in {finding.package}: comparison "
                    f"between base version {base_version} and fix {fixed} is "
                    f"ambiguous. Downgrading conservatively (fail-closed). "
                    f"Re-check next scan.",
                ))
                continue
            if cmp >= 0:
                confirmed.append(_confirmed(
                    finding,
                    f"{classification.rationale} Verified: base {base_ref} "
                    f"ships {base_version} (>= fix {fixed}).",
                ))
                continue

        # Base is absent or older than the fix: simulate the rebuild install.
        try:
            plan = _rebuild_plan(finding, base_ref, install_list_cache, plan_cache)
        except BasePrecheckError as exc:
            downgraded.append(_downgraded(
                finding,
                f"Fix for {finding.cve_id} in {finding.package}: could not "
                f"simulate the rebuild ({exc}). Downgrading conservatively "
                f"(fail-closed). Re-check next scan.",
            ))
            continue

        planned = plan.get(finding.package)

        if planned is None:
            # The rebuild neither installs nor upgrades this package, so it
            # stays at the base version (which is < fix here, since the fast
            # path already confirmed base >= fix). This is the zlib1g case.
            shipped = base_version if base_version is not None else "absent"
            downgraded.append(_downgraded(
                finding,
                f"Fix for {finding.cve_id} in {finding.package}: a rebuild does "
                f"not install or upgrade it (not in the Dockerfile install "
                f"plan), so it stays at the base version {shipped} (< fix "
                f"{fixed}) and needs a base image update. Re-check next scan.",
            ))
            continue

        cmp_plan = _native_compare(planned, fixed, flavor, base_ref)
        if cmp_plan is None:
            downgraded.append(_downgraded(
                finding,
                f"Fix for {finding.cve_id} in {finding.package}: comparison "
                f"between planned rebuild version {planned} and fix {fixed} is "
                f"ambiguous. Downgrading conservatively (fail-closed). "
                f"Re-check next scan.",
            ))
        elif cmp_plan >= 0:
            confirmed.append(_confirmed(
                finding,
                f"{classification.rationale} Verified: a rebuild installs "
                f"{finding.package} {planned} (>= fix {fixed}).",
            ))
        else:
            downgraded.append(_downgraded(
                finding,
                f"Fix for {finding.cve_id} in {finding.package}: a rebuild "
                f"installs {planned}, below fix {fixed}. Downgrading "
                f"conservatively (fail-closed). Re-check next scan.",
            ))

    return confirmed, downgraded
