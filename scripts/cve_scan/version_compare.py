"""Native package-manager version comparison for the CVE safety gate.

Provides compare_versions(), which delegates to the native comparison tool
of the relevant package manager (dpkg for Debian, apk for Alpine). This is
used in base_precheck.py as the actual gate before dispatch: it has correct
Debian/Alpine semantics by design, rather than a hand-rolled approximation.

Both tools are invoked via ``docker run --rm <image> <cmd>`` so the runner
needs Docker available. Any subprocess failure or unparseable output
returns None (fail-closed: caller must treat as "not fixable").

Security model: deterministic, no AI, no network beyond the docker pull
that already happened for the trivy scan.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

#: Docker run timeout in seconds for a single comparison command.
_COMPARE_TIMEOUT = 60

# Canonical public images used when comparing versions independent of a
# specific base image. Kept small: these are just used to host the comparison
# tool (dpkg / apk), not to read installed package databases.
_DEBIAN_COMPARATOR_IMAGE = "public.ecr.aws/docker/library/debian:stable-slim"
_ALPINE_COMPARATOR_IMAGE = "public.ecr.aws/docker/library/alpine:latest"


def compare_versions(
    a: str,
    b: str,
    flavor: str,
    base_image: Optional[str] = None,
) -> Optional[int]:
    """Compare two package version strings using the native package manager.

    Runs ``dpkg --compare-versions a lt b`` (Debian) or
    ``apk version -t a b`` (Alpine) inside a minimal container to obtain
    semantically correct ordering.

    Args:
        a: First version string (e.g. installed version).
        b: Second version string (e.g. fixed version).
        flavor: Package manager flavor: ``'debian'`` or ``'alpine'``.
        base_image: Override the container image used to host the comparison
            tool. Defaults to a canonical public image for the flavor.

    Returns:
        -1 if a < b
         0 if a == b
         1 if a > b
        None on any error (subprocess failure, timeout, unparseable output).
        None is the fail-closed sentinel: callers must treat it as not-fixable.
    """
    if flavor == "debian":
        return _compare_debian(a, b, base_image or _DEBIAN_COMPARATOR_IMAGE)
    elif flavor == "alpine":
        return _compare_alpine(a, b, base_image or _ALPINE_COMPARATOR_IMAGE)
    else:
        logger.warning("compare_versions: unknown flavor %r; returning None (fail-closed)", flavor)
        return None


def _run_docker(cmd: list[str]) -> "tuple[int, str, str]":
    """Run a docker command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_COMPARE_TIMEOUT,
            check=False,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        logger.warning("compare_versions: docker command timed out: %s", " ".join(cmd))
        return -1, "", "timeout"
    except OSError as exc:
        logger.warning("compare_versions: failed to run docker: %s", exc)
        return -1, "", str(exc)


def _compare_debian(a: str, b: str, image: str) -> Optional[int]:
    """Compare versions using dpkg --compare-versions.

    Runs two comparisons (lt and eq) to determine the full ordering.
    ``dpkg --compare-versions`` legitimately exits 0 (true) or 1 (false).
    Any other exit code indicates an unexpected failure (e.g. docker error
    125/126/127) and we return None (fail-closed).
    """
    # Check a < b
    rc_lt, _, stderr_lt = _run_docker([
        "docker", "run", "--rm", image,
        "dpkg", "--compare-versions", a, "lt", b,
    ])
    if rc_lt not in (0, 1):
        logger.warning(
            "compare_versions(debian): unexpected exit code %d for lt check; stderr=%r",
            rc_lt, stderr_lt,
        )
        return None

    if rc_lt == 0:
        return -1  # a < b

    # Check a == b
    rc_eq, _, stderr_eq = _run_docker([
        "docker", "run", "--rm", image,
        "dpkg", "--compare-versions", a, "eq", b,
    ])
    if rc_eq not in (0, 1):
        logger.warning(
            "compare_versions(debian): unexpected exit code %d for eq check; stderr=%r",
            rc_eq, stderr_eq,
        )
        return None

    if rc_eq == 0:
        return 0  # a == b

    # Neither lt nor eq -> a > b
    return 1


def _compare_alpine(a: str, b: str, image: str) -> Optional[int]:
    """Compare versions using ``apk version -t``.

    ``apk version -t a b`` prints one of '<', '=', '>'.
    Invoked as an argv list (no shell) to avoid quoting hazards.
    """
    rc, stdout, stderr = _run_docker([
        "docker", "run", "--rm", image,
        "apk", "version", "-t", a, b,
    ])
    if rc == -1:
        # _run_docker signals timeout/OSError with rc == -1
        return None
    if rc != 0:
        logger.warning(
            "compare_versions(alpine): apk version -t exited %d stderr=%r",
            rc, stderr,
        )
        return None

    symbol = stdout.strip()
    if symbol == "<":
        return -1
    elif symbol == "=":
        return 0
    elif symbol == ">":
        return 1
    else:
        logger.warning(
            "compare_versions(alpine): unexpected apk output %r for %r vs %r",
            symbol, a, b,
        )
        return None
