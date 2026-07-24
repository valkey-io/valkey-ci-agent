"""Dynamic image matrix resolver for the CVE scan workflow.

Static override (settings.images) or dynamic derivation from versions.json.
Fail-closed: any fetch, parse, or derivation failure raises rather than
silently falling back to a stale or incomplete list.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import TYPE_CHECKING
from urllib.error import URLError

if TYPE_CHECKING:
    from scripts.cve_scan.config import CveScanSettings

logger = logging.getLogger(__name__)

#: Default HTTP timeout for fetching the versions manifest (seconds).
_FETCH_TIMEOUT_SECONDS = 15


class MatrixResolutionError(Exception):
    """Raised when dynamic image matrix resolution fails."""


def _fetch_versions_json(url: str) -> dict:
    """Fetch and parse versions.json from the given URL.

    Raises MatrixResolutionError on network failure, non-200 status, or invalid JSON.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "valkey-ci-agent/cve-scan"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_SECONDS) as resp:
            if resp.status != 200:
                raise MatrixResolutionError(
                    f"Failed to fetch versions manifest: HTTP {resp.status} from {url}"
                )
            body = resp.read().decode("utf-8")
    except (URLError, OSError, TimeoutError) as exc:
        raise MatrixResolutionError(
            f"Failed to fetch versions manifest from {url}: {exc}"
        ) from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise MatrixResolutionError(
            f"Invalid JSON in versions manifest from {url}: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise MatrixResolutionError(
            f"Versions manifest must be a JSON object, got {type(data).__name__}"
        )
    return data


def _derive_images(
    versions: dict,
    repository: str,
    include_unstable: bool,
) -> list[str]:
    """Derive the sorted image tag list (keys of _derive_base_map, single source of truth).

    Raises MatrixResolutionError if derivation produces an empty list.
    """
    images = sorted(_derive_base_map(versions, repository, include_unstable))
    if not images:
        raise MatrixResolutionError(
            "Dynamic resolution produced zero images from versions manifest"
        )
    return images


def _derive_base_map(
    versions: dict,
    repository: str,
    include_unstable: bool,
) -> dict[str, str]:
    """Map each derived image tag to its base image reference.

    Base conventions (valkey-container Dockerfiles): alpine:<version> for
    -alpine tags, debian:<version>-slim otherwise.
    """
    base_map: dict[str, str] = {}
    for version_key, value in versions.items():
        if version_key == "unstable" and not include_unstable:
            continue
        if not isinstance(value, dict):
            continue
        if "alpine" in value:
            alpine_ver = value["alpine"].get("version", "")
            image_ref = f"{repository}:{version_key}-alpine"
            base_map[image_ref] = f"alpine:{alpine_ver}"
        if "debian" in value:
            debian_ver = value["debian"].get("version", "")
            image_ref = f"{repository}:{version_key}"
            base_map[image_ref] = f"debian:{debian_ver}-slim"
    return base_map


def resolve_matrix(settings: CveScanSettings) -> tuple[list[str], dict[str, str]]:
    """Resolve the image list and base-image mapping from a single fetch.

    Args:
        settings: Loaded CveScanSettings instance.

    Returns:
        (images, base_map): sorted image refs and image_ref -> base_ref
        mapping (base_map is empty in static override mode).

    Raises:
        MatrixResolutionError: On any dynamic resolution failure.
    """
    if settings.images:
        logger.info(
            "Using static image override (%d image(s)): %s",
            len(settings.images),
            ", ".join(settings.images),
        )
        return settings.images, {}

    logger.info(
        "Resolving dynamic image matrix from %s (repository=%s, include_unstable=%s)",
        settings.versions_url,
        settings.repository,
        settings.include_unstable,
    )
    versions = _fetch_versions_json(settings.versions_url)
    base_map = _derive_base_map(versions, settings.repository, settings.include_unstable)
    images = sorted(base_map.keys())

    if not images:
        raise MatrixResolutionError(
            "Dynamic resolution produced zero images from versions manifest"
        )

    logger.info("Resolved %d image(s): %s", len(images), ", ".join(images))
    return images, base_map
