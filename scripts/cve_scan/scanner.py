"""Invoke a CVE scanner subprocess and parse findings.

Runs Trivy against container images (optionally per platform for multi-arch
images), captures JSON output, and converts it into structured Finding objects
via the findings parser.

Multi-arch scanning: each image is scanned per platform by passing
``--platform <p>`` to trivy. Findings from all platforms are merged and
deduplicated by (image, package, cve_id, installed_version, platform) so
exact duplicates on the same platform are collapsed, but cross-platform
findings remain distinct for per-platform base verification.

Expected scan count: len(images) * len(platforms). For the default 4-platform
configuration (amd64, arm64, arm/v7, ppc64le) and a typical 10-image matrix
this is ~40 trivy invocations. At ~30 seconds each the job-level 30 min
timeout is sufficient.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from scripts.cve_scan.config import DEFAULT_PLATFORMS
from scripts.cve_scan.models import Finding, Severity
from scripts.parsers.cve_findings_parser import filter_by_threshold, parse_findings

logger = logging.getLogger(__name__)

#: Per-scan subprocess timeout in seconds.
#: Cached-DB scans complete in tens of seconds; the job-level timeout is 30 min.
_SCAN_TIMEOUT_SECONDS = 180


class ScanError(Exception):
    """Raised when a scanner subprocess fails or produces unparseable output."""


def _build_command(scanner: str, image: str, platform: str | None = None) -> list[str]:
    """Build the scanner command as an argument list (no shell interpolation)."""
    if scanner == "trivy":
        cmd = [
            "trivy", "image", "--format", "json", "--quiet",
            "--scanners", "vuln", "--pkg-types", "os",
        ]
        if platform:
            cmd.extend(["--platform", platform])
        cmd.append(image)
        return cmd
    raise ValueError(f"Unsupported scanner: {scanner!r}. Must be 'trivy'.")


def _run_scanner(command: list[str], timeout: int = _SCAN_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run the scanner subprocess and return parsed JSON output.

    Raises:
        ScanError: On non-zero exit, timeout, or invalid JSON output.
    """
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScanError(
            f"Scanner timed out after {timeout}s: {' '.join(command)}"
        ) from exc
    except OSError as exc:
        raise ScanError(
            f"Failed to execute scanner: {' '.join(command)}: {exc}"
        ) from exc

    if result.returncode != 0:
        stderr_snippet = result.stderr[:500] if result.stderr else "(no stderr)"
        raise ScanError(
            f"Scanner exited with code {result.returncode}: {' '.join(command)}\n"
            f"stderr: {stderr_snippet}"
        )

    if not result.stdout.strip():
        raise ScanError(f"Scanner produced empty output: {' '.join(command)}")

    try:
        return json.loads(result.stdout)  # type: ignore[no-any-return]
    except json.JSONDecodeError as exc:
        raise ScanError(
            f"Scanner output is not valid JSON: {' '.join(command)}: {exc}"
        ) from exc


def scan_image(image: str, scanner: str, platform: str | None = None) -> list[Finding]:
    """Scan a single image (optionally for a specific platform) and return all findings.

    Args:
        image: Container image reference (e.g. "valkey/valkey:8.0-alpine").
        scanner: Scanner to use ("trivy").
        platform: Optional platform string (e.g. "linux/amd64"). When provided,
            passed as ``--platform`` to trivy to scan the specific arch manifest.

    Returns:
        List of Finding objects from the scan.

    Raises:
        ScanError: If the scanner fails or produces invalid output.
        ValueError: If the scanner name is not recognized.
    """
    command = _build_command(scanner, image, platform=platform)
    json_obj = _run_scanner(command)
    return parse_findings(scanner, json_obj, image, platform=platform or "")


def _dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Deduplicate findings by (image, package, cve_id, installed_version, platform).

    When scanning multiple platforms, identical CVEs on the SAME platform can
    appear if trivy reports duplicates. This collapses them to one per platform.
    Findings on DIFFERENT platforms are kept distinct so base verification can
    check each platform's base image independently. The first occurrence is kept.
    """
    seen: set[tuple[str, str, str, str, str]] = set()
    deduped: list[Finding] = []
    for f in findings:
        key = (f.image, f.package, f.cve_id, f.installed_version, f.platform)
        if key not in seen:
            seen.add(key)
            deduped.append(f)
    return deduped


def scan_images(
    images: list[str],
    scanner: str,
    threshold: Severity,
    platforms: list[str] | None = None,
) -> list[Finding]:
    """Scan multiple images (per platform) and return findings at or above the severity threshold.

    Each image is scanned once per platform in ``platforms``. Findings are
    merged across platforms and deduplicated by (image, package, cve_id,
    installed_version, platform) so exact duplicates on the same platform are
    collapsed but cross-platform findings remain distinct for per-platform
    base verification.

    Args:
        images: List of container image references to scan.
        scanner: Scanner to use ("trivy").
        threshold: Minimum severity level; findings below this are excluded.
        platforms: Platforms to scan per image. Defaults to DEFAULT_PLATFORMS.

    Returns:
        Combined deduplicated list of findings from all images and platforms,
        filtered by threshold.

    Raises:
        ScanError: If any scanner invocation fails.
        ValueError: If the scanner name is not recognized.
    """
    if platforms is None:
        platforms = DEFAULT_PLATFORMS

    all_findings: list[Finding] = []
    total = len(images)
    for idx, image in enumerate(images, start=1):
        image_findings: list[Finding] = []
        for platform in platforms:
            logger.info(
                "Scanning image %d/%d: %s (platform=%s)",
                idx, total, image, platform,
            )
            platform_findings = scan_image(image, scanner, platform=platform)
            logger.info(
                "  %s [%s]: %d finding(s) total",
                image, platform, len(platform_findings),
            )
            image_findings.extend(platform_findings)

        deduped = _dedup_findings(image_findings)
        above = [f for f in deduped if f.severity >= threshold]
        logger.info(
            "  %s: %d unique finding(s) across %d platform(s), %d at or above %s",
            image, len(deduped), len(platforms), len(above), threshold.name,
        )
        all_findings.extend(deduped)

    return filter_by_threshold(all_findings, threshold)
