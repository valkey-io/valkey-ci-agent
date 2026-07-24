"""Native package-manager version comparison for the CVE safety gate.

Delegates to dpkg (Debian) or apk (Alpine) via ``docker run --rm`` for
semantically correct ordering. Any subprocess failure or unparseable output
returns None (fail-closed: caller must treat as not fixable). Deterministic,
no AI, no shell interpolation.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

#: Docker run timeout in seconds for a single comparison command.
_COMPARE_TIMEOUT = 60

# Canonical public images hosting the comparison tool (dpkg / apk) when no
# specific base image is given.
# Pinned by digest for the deterministic safety gate; refresh via
# `docker buildx imagetools inspect <image:tag>`.
_DEBIAN_COMPARATOR_IMAGE = (
    "public.ecr.aws/docker/library/debian:stable-slim"
    "@sha256:328d16499860ae6cb9b345e2e4cebca08c2a36e4f7278482c7bd1f39d71e5bfd"
)
_ALPINE_COMPARATOR_IMAGE = (
    "public.ecr.aws/docker/library/alpine:3.21"
    "@sha256:48b0309ca019d89d40f670aa1bc06e426dc0931948452e8491e3d65087abc07d"
)


def compare_versions(
    a: str,
    b: str,
    flavor: str,
    base_image: Optional[str] = None,
) -> Optional[int]:
    """Compare two package version strings using the native package manager.

    Args:
        a: First version string (e.g. installed version).
        b: Second version string (e.g. fixed version).
        flavor: Package manager flavor: ``'debian'`` or ``'alpine'``.
        base_image: Optional override for the container hosting the comparison tool.

    Returns:
        -1/0/1 ordering of a vs b, or None on any error (fail-closed
        sentinel: callers must treat it as not-fixable).
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
    """Compare via dpkg --compare-versions (lt then eq).

    dpkg legitimately exits 0 (true) or 1 (false); any other exit code is a
    docker-level failure and returns None (fail-closed).
    """
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

    return 1  # neither lt nor eq -> a > b


def _compare_alpine(a: str, b: str, image: str) -> Optional[int]:
    """Compare via ``apk version -t`` (prints '<', '=', or '>'). Argv list, no shell."""
    rc, stdout, stderr = _run_docker([
        "docker", "run", "--rm", image,
        "apk", "version", "-t", a, b,
    ])
    if rc == -1:
        # rc == -1 signals timeout/OSError from _run_docker
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
