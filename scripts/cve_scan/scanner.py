"""Invoke Trivy as a subprocess and parse findings.

Each image is scanned per platform (``--platform``); findings are merged and
deduplicated by (image, package, cve_id, installed_version, platform) so
cross-platform findings stay distinct for per-platform base verification.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any

from scripts.cve_scan.config import DEFAULT_PLATFORMS
from scripts.cve_scan.models import Finding, Severity
from scripts.parsers.cve_findings_parser import ParseError, filter_by_threshold, parse_trivy

logger = logging.getLogger(__name__)

#: Per-scan subprocess timeout in seconds (cached-DB scans take tens of seconds).
_SCAN_TIMEOUT_SECONDS = 180


class ScanError(Exception):
    """Raised when a scanner subprocess fails or produces unparseable output."""


def _build_command(
    image: str,
    platform: str | None = None,
    *,
    trivy_bin: str = "trivy",
) -> list[str]:
    """Build the Trivy command as an argument list (no shell interpolation)."""
    cmd = [
        trivy_bin, "image", "--format", "json", "--quiet",
        "--scanners", "vuln", "--pkg-types", "os",
    ]
    if platform:
        cmd.extend(["--platform", platform])
    cmd.append(image)
    return cmd


def _run_scanner(command: list[str], timeout: int = _SCAN_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run the scanner subprocess and return parsed JSON.

    Raises ScanError on non-zero exit, timeout, or invalid JSON.
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
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ScanError(
            f"Scanner output is not valid JSON: {' '.join(command)}: {exc}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ScanError(
            f"Scanner output is not a JSON object: {' '.join(command)}: "
            f"got {type(parsed).__name__}"
        )
    return parsed


def scan_image(
    image: str,
    platform: str | None = None,
    *,
    trivy_bin: str = "trivy",
    timeout: int = _SCAN_TIMEOUT_SECONDS,
) -> list[Finding]:
    """Scan one image with Trivy and return all findings.

    Args:
        image: Container image reference (e.g. "valkey/valkey:8.0-alpine").
        platform: Optional platform (e.g. "linux/amd64") passed as ``--platform``.

    Returns:
        List of Finding objects from the scan.

    Raises:
        ScanError: If the scanner fails, produces invalid output, or its
            JSON does not match the expected schema.
    """
    command = _build_command(image, platform, trivy_bin=trivy_bin)
    json_obj = _run_scanner(command, timeout)
    try:
        return parse_trivy(json_obj, image, platform=platform or "")
    except ParseError as exc:
        raise ScanError(
            f"Scanner output failed schema validation for {image}: {exc}"
        ) from exc


def _dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Collapse same-platform duplicates; keep cross-platform findings distinct.

    First occurrence wins. Cross-platform findings stay separate so base
    verification can check each platform's base image independently.
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
    threshold: Severity,
    platforms: list[str] | None = None,
) -> list[Finding]:
    """Scan multiple images per platform; return deduplicated findings at or above threshold.

    Args:
        images: List of container image references to scan.
        threshold: Minimum severity level; findings below this are excluded.
        platforms: Platforms to scan per image. Defaults to DEFAULT_PLATFORMS.

    Returns:
        Combined deduplicated findings from all images and platforms.

    Raises:
        ScanError: If any scanner invocation fails.
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
            platform_findings = scan_image(image, platform)
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
