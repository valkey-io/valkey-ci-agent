"""Dynamic image matrix resolver for the CVE scan workflow.

Resolves the list of container images to scan, supporting two modes:

1. Static override: settings.images is non-empty (returned as-is).
2. Dynamic (default): fetches the versions-json manifest from settings.versions_url
   and derives image tags from its structure.

The resolver is fail-closed: any network, parsing, or derivation failure raises
an exception rather than silently falling back to a stale or incomplete list.
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

    Raises:
        MatrixResolutionError: On network failure, non-200 status, or invalid JSON.
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
    """Derive the sorted image tag list from a versions.json structure.

    Single source of truth: delegates to :func:`_derive_base_map` (which
    applies the version-iteration, include_unstable, and variant rules) and
    returns its keys, so the derivation logic is not duplicated.

    Raises:
        MatrixResolutionError: If derivation produces an empty list.
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
    """Derive a mapping from each derived image tag to its base image reference.

    Base image conventions (from valkey-container Dockerfiles):
    - Alpine variant: FROM alpine:<alpine.version>
    - Debian variant: FROM debian:<debian.version>-slim

    Returns:
        dict mapping image_ref -> base_ref (e.g.
        'valkey/valkey:9.1-alpine' -> 'alpine:3.23',
        'valkey/valkey:9.1' -> 'debian:trixie-slim').
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

    Static override: settings.images is non-empty, returned with an empty base_map.
    Dynamic (default): fetches the manifest from settings.versions_url once and
    derives both the image list and the image-to-base mapping.

    Args:
        settings: Loaded CveScanSettings instance.

    Returns:
        Tuple of (images, base_map):
        - images: sorted list of image references to scan.
        - base_map: mapping of image_ref -> base_ref (empty in static mode).

    Raises:
        MatrixResolutionError: On any dynamic resolution failure.
    """
    if settings.images:
        # Static override mode: no base image info available
        logger.info(
            "Using static image override (%d image(s)): %s",
            len(settings.images),
            ", ".join(settings.images),
        )
        return settings.images, {}

    # Dynamic mode: single fetch, derive both images and base_map
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
