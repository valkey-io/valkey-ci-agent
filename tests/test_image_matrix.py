"""Tests for scripts/cve_scan/image_matrix.py -- dynamic image matrix resolution.

Covers:
  - Static passthrough: settings.images is non-empty, returned as-is with empty base_map.
  - Dynamic derivation: mocked versions.json -> correct sorted tags + base_map.
  - Unstable skipped by default, included when include_unstable=True.
  - Single-variant versions (only alpine or only debian) handled correctly.
  - Fetch failure (network error) raises MatrixResolutionError.
  - Invalid JSON raises MatrixResolutionError.
  - Empty derivation (no valid versions) raises MatrixResolutionError.
  - Single fetch (no double fetch).
"""

from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from unittest.mock import patch

import pytest

from scripts.cve_scan.config import CveScanSettings
from scripts.cve_scan.image_matrix import (
    MatrixResolutionError,
    _derive_images,
    _fetch_versions_json,
    resolve_matrix,
)
from scripts.cve_scan.models import Severity

# ---------------------------------------------------------------------------
# Sample versions.json payloads
# ---------------------------------------------------------------------------

SAMPLE_VERSIONS: dict[str, Any] = {
    "7.2": {
        "version": "7.2.13",
        "debian": {"version": "trixie"},
        "alpine": {"version": "3.23"},
    },
    "8.0": {
        "version": "8.0.9",
        "debian": {"version": "trixie"},
        "alpine": {"version": "3.23"},
    },
    "8.1": {
        "version": "8.1.8",
        "debian": {"version": "trixie"},
        "alpine": {"version": "3.23"},
    },
    "9.0": {
        "version": "9.0.4",
        "debian": {"version": "trixie"},
        "alpine": {"version": "3.23"},
    },
    "9.1": {
        "version": "9.1.0",
        "debian": {"version": "trixie"},
        "alpine": {"version": "3.23"},
    },
    "unstable": {
        "version": "unstable",
        "debian": {"version": "trixie"},
        "alpine": {"version": "3.23"},
    },
}

#: A version with only alpine (no debian)
SAMPLE_ALPINE_ONLY: dict[str, Any] = {
    "10.0": {
        "version": "10.0.0",
        "alpine": {"version": "3.23"},
    },
}

#: A version with only debian (no alpine)
SAMPLE_DEBIAN_ONLY: dict[str, Any] = {
    "10.1": {
        "version": "10.1.0",
        "debian": {"version": "trixie"},
    },
}


def _make_settings(
    *,
    images: list[str] | None = None,
    versions_url: str = "https://example.com/versions.json",
    repository: str = "valkey/valkey",
    include_unstable: bool = False,
) -> CveScanSettings:
    """Helper to build a CveScanSettings with test defaults."""
    return CveScanSettings(
        versions_url=versions_url,
        repository=repository,
        include_unstable=include_unstable,
        scanner="trivy",
        severity_threshold=Severity.HIGH,
        images=images or [],
    )


def _mock_urlopen(data: Any, status: int = 200):
    """Create a mock context manager for urllib.request.urlopen."""
    body = json.dumps(data).encode("utf-8") if not isinstance(data, bytes) else data
    resp = BytesIO(body)
    resp.status = status  # type: ignore[attr-defined]
    resp.__enter__ = lambda self: self  # type: ignore[attr-defined]
    resp.__exit__ = lambda self, *a: None  # type: ignore[attr-defined]
    return resp


# ---------------------------------------------------------------------------
# Static passthrough
# ---------------------------------------------------------------------------


class TestStaticPassthrough:
    """Static override mode: settings.images is non-empty, return as-is."""

    def test_returns_static_list_with_empty_base_map(self) -> None:
        settings = _make_settings(images=["img:1", "img:2"])
        images, base_map = resolve_matrix(settings)
        assert images == ["img:1", "img:2"]
        assert base_map == {}

    def test_single_image_static(self) -> None:
        settings = _make_settings(images=["valkey/valkey:8.0-alpine"])
        images, base_map = resolve_matrix(settings)
        assert images == ["valkey/valkey:8.0-alpine"]
        assert base_map == {}


# ---------------------------------------------------------------------------
# Dynamic derivation (mocked fetch)
# ---------------------------------------------------------------------------


class TestDynamicDerivation:
    """Dynamic mode: fetch versions.json and derive images + base_map."""

    def test_derives_correct_sorted_tags(self) -> None:
        """All stable versions produce both alpine and bare tags, sorted."""
        settings = _make_settings(include_unstable=False)

        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(SAMPLE_VERSIONS)
            images, base_map = resolve_matrix(settings)

        expected = sorted([
            "valkey/valkey:7.2-alpine",
            "valkey/valkey:7.2",
            "valkey/valkey:8.0-alpine",
            "valkey/valkey:8.0",
            "valkey/valkey:8.1-alpine",
            "valkey/valkey:8.1",
            "valkey/valkey:9.0-alpine",
            "valkey/valkey:9.0",
            "valkey/valkey:9.1-alpine",
            "valkey/valkey:9.1",
        ])
        assert images == expected

    def test_base_map_correct_for_dynamic(self) -> None:
        """Dynamic settings returns alpine -> alpine:X.Y, debian -> debian:Z-slim."""
        settings = _make_settings(include_unstable=False)

        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(SAMPLE_VERSIONS)
            images, base_map = resolve_matrix(settings)

        # Check alpine variant mapping
        assert base_map["valkey/valkey:9.1-alpine"] == "alpine:3.23"
        assert base_map["valkey/valkey:7.2-alpine"] == "alpine:3.23"
        # Check debian variant mapping (bare tag)
        assert base_map["valkey/valkey:9.1"] == "debian:trixie-slim"
        assert base_map["valkey/valkey:7.2"] == "debian:trixie-slim"
        # Should have 10 entries (5 versions x 2 variants, unstable excluded)
        assert len(base_map) == 10

    def test_unstable_skipped_by_default(self) -> None:
        """Unstable version is excluded when include_unstable=False."""
        settings = _make_settings(include_unstable=False)

        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(SAMPLE_VERSIONS)
            images, base_map = resolve_matrix(settings)

        assert "valkey/valkey:unstable-alpine" not in images
        assert "valkey/valkey:unstable" not in images
        assert "valkey/valkey:unstable-alpine" not in base_map
        assert "valkey/valkey:unstable" not in base_map

    def test_unstable_included_when_requested(self) -> None:
        """Unstable version is included when include_unstable=True."""
        settings = _make_settings(include_unstable=True)

        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(SAMPLE_VERSIONS)
            images, base_map = resolve_matrix(settings)

        assert "valkey/valkey:unstable-alpine" in images
        assert "valkey/valkey:unstable" in images
        assert base_map["valkey/valkey:unstable-alpine"] == "alpine:3.23"
        assert base_map["valkey/valkey:unstable"] == "debian:trixie-slim"

    def test_single_variant_alpine_only(self) -> None:
        """Version with only alpine variant produces only alpine tag."""
        settings = _make_settings(repository="myrepo/myimg")

        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(SAMPLE_ALPINE_ONLY)
            images, base_map = resolve_matrix(settings)

        assert images == ["myrepo/myimg:10.0-alpine"]
        assert "myrepo/myimg:10.0-alpine" in base_map

    def test_single_variant_debian_only(self) -> None:
        """Version with only debian variant produces only bare tag."""
        settings = _make_settings(repository="myrepo/myimg")

        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(SAMPLE_DEBIAN_ONLY)
            images, base_map = resolve_matrix(settings)

        assert images == ["myrepo/myimg:10.1"]
        assert "myrepo/myimg:10.1" in base_map

    def test_custom_repository(self) -> None:
        """Custom repository prefix is applied to all derived tags."""
        settings = _make_settings(repository="ghcr.io/valkey-io/valkey")

        payload = {"1.0": {"debian": {"version": "bookworm"}, "alpine": {"version": "3.20"}}}
        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(payload)
            images, base_map = resolve_matrix(settings)

        assert "ghcr.io/valkey-io/valkey:1.0-alpine" in images
        assert "ghcr.io/valkey-io/valkey:1.0" in images

    def test_single_fetch_for_both_images_and_base_map(self) -> None:
        """resolve_matrix makes exactly one HTTP fetch."""
        settings = _make_settings(include_unstable=False)

        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(SAMPLE_VERSIONS)
            resolve_matrix(settings)

        assert mock_open.call_count == 1


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestDynamicErrors:
    """Dynamic mode failure cases raise MatrixResolutionError."""

    def test_network_error_raises(self) -> None:
        """URLError from fetch raises MatrixResolutionError."""
        from urllib.error import URLError

        settings = _make_settings()

        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = URLError("connection refused")
            with pytest.raises(MatrixResolutionError, match="Failed to fetch"):
                resolve_matrix(settings)

    def test_invalid_json_raises(self) -> None:
        """Malformed JSON raises MatrixResolutionError."""
        settings = _make_settings()

        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(b"not json {{{{")
            with pytest.raises(MatrixResolutionError, match="Invalid JSON"):
                resolve_matrix(settings)

    def test_empty_derivation_raises(self) -> None:
        """Manifest with no valid versions raises MatrixResolutionError."""
        settings = _make_settings(include_unstable=False)

        # Only unstable, and include_unstable=False -> zero images
        payload = {"unstable": {"debian": {"version": "trixie"}, "alpine": {"version": "3.23"}}}
        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen(payload)
            with pytest.raises(MatrixResolutionError, match="zero images"):
                resolve_matrix(settings)

    def test_non_dict_manifest_raises(self) -> None:
        """Manifest that is not a JSON object raises MatrixResolutionError."""
        settings = _make_settings()

        with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as mock_open:
            mock_open.return_value = _mock_urlopen([1, 2, 3])
            with pytest.raises(MatrixResolutionError, match="must be a JSON object"):
                resolve_matrix(settings)


# ---------------------------------------------------------------------------
# _derive_images unit tests
# ---------------------------------------------------------------------------


class TestDeriveImages:
    """Direct tests for the _derive_images helper."""

    def test_deterministic_sort(self) -> None:
        """Output is always sorted regardless of input ordering."""
        versions = {
            "9.0": {"debian": {}, "alpine": {}},
            "7.2": {"debian": {}, "alpine": {}},
        }
        result = _derive_images(versions, "r", include_unstable=False)
        assert result == ["r:7.2", "r:7.2-alpine", "r:9.0", "r:9.0-alpine"]

    def test_non_dict_version_value_skipped(self) -> None:
        """Non-dict values in the manifest are silently skipped."""
        versions = {"7.2": {"debian": {}}, "meta": "not a dict"}
        result = _derive_images(versions, "r", include_unstable=False)
        assert result == ["r:7.2"]


# ---------------------------------------------------------------------------
# RC-era regression: derivation must key off version-line keys, not versions
# ---------------------------------------------------------------------------

RC_ERA_VERSIONS: dict[str, Any] = {
    "7.2": {"version": "7.2.12", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "8.0": {"version": "8.0.7", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "8.1": {"version": "8.1.6", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "9.0": {"version": "9.0.3", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "9.1": {"version": "9.1.0-rc2", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
    "unstable": {"version": "unstable", "debian": {"version": "trixie"}, "alpine": {"version": "3.23"}},
}


class TestRcEraDerivation:
    """During an RC window, derivation must still yield the version-line tags."""

    def test_rc_line_derives_bare_and_alpine_line_tags(self) -> None:
        images = _derive_images(RC_ERA_VERSIONS, "valkey/valkey", include_unstable=False)
        assert "valkey/valkey:9.1" in images
        assert "valkey/valkey:9.1-alpine" in images

    def test_rc_full_version_never_used_in_tags(self) -> None:
        images = _derive_images(RC_ERA_VERSIONS, "valkey/valkey", include_unstable=False)
        assert not any("rc" in img for img in images)

    def test_rc_era_matrix_is_complete(self) -> None:
        images = _derive_images(RC_ERA_VERSIONS, "valkey/valkey", include_unstable=False)
        assert images == sorted(
            [
                "valkey/valkey:7.2",
                "valkey/valkey:7.2-alpine",
                "valkey/valkey:8.0",
                "valkey/valkey:8.0-alpine",
                "valkey/valkey:8.1",
                "valkey/valkey:8.1-alpine",
                "valkey/valkey:9.0",
                "valkey/valkey:9.0-alpine",
                "valkey/valkey:9.1",
                "valkey/valkey:9.1-alpine",
            ]
        )
