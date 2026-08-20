"""Resolve published Valkey image tags from versions.json."""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import TYPE_CHECKING
from urllib.error import URLError

if TYPE_CHECKING:
    from scripts.cve_scan.config import CveScanSettings

logger = logging.getLogger(__name__)
_FETCH_TIMEOUT_SECONDS = 15


class MatrixResolutionError(Exception):
    """Image resolution failed or the manifest was malformed."""


def _fetch_versions_json(url: str) -> dict:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "valkey-ci-agent/cve-scan"})
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise MatrixResolutionError(f"HTTP {response.status} from {url}")
            body = response.read().decode("utf-8")
    except (URLError, OSError, TimeoutError) as exc:
        raise MatrixResolutionError(f"Failed to fetch versions manifest: {exc}") from exc
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        raise MatrixResolutionError(f"Invalid versions manifest JSON: {exc}") from exc
    if not isinstance(value, dict) or not value:
        raise MatrixResolutionError("Versions manifest must be a non-empty JSON object")
    return value


def _derive_images(versions: dict, repository: str, include_unstable: bool) -> list[str]:
    images: list[str] = []
    for line, entry in versions.items():
        if line == "unstable" and not include_unstable:
            continue
        if not isinstance(entry, dict):
            logger.warning("Skipping non-object manifest entry %r", line)
            continue
        for variant, suffix in (("debian", ""), ("alpine", "-alpine")):
            if variant not in entry:
                continue
            if not isinstance(entry[variant], dict):
                raise MatrixResolutionError(f"Manifest entry {line!r} variant {variant!r} must be an object")
            images.append(f"{repository}:{line}{suffix}")
    return sorted(images)


def resolve_matrix(settings: CveScanSettings) -> list[str]:
    """Return static image overrides or tags derived from one manifest fetch."""
    if settings.images:
        return settings.images
    versions = _fetch_versions_json(settings.versions_url)
    images = _derive_images(versions, settings.repository, settings.include_unstable)
    if not images:
        raise MatrixResolutionError("Dynamic resolution produced zero images")
    logger.info("Resolved %d image(s): %s", len(images), ", ".join(images))
    return images
