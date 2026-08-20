"""Tests for image-tag resolution from versions.json."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch
from urllib.error import URLError

import pytest

from scripts.cve_scan.config import CveScanSettings
from scripts.cve_scan.image_matrix import (
    MatrixResolutionError,
    _derive_images,
    resolve_matrix,
)
from scripts.cve_scan.models import Severity


def _settings(**changes: object) -> CveScanSettings:
    values = {
        "versions_url": "https://example.com/versions.json",
        "repository": "valkey/valkey",
        "include_unstable": False,
        "severity_threshold": Severity.HIGH,
        "images": [],
        "platforms": ["linux/amd64"],
    }
    values.update(changes)
    return CveScanSettings(**values)  # type: ignore[arg-type]


def _response(value: object, status: int = 200) -> BytesIO:
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    response = BytesIO(body)
    response.status = status  # type: ignore[attr-defined]
    return response


def _resolve(payload: object, **settings: object) -> list[str]:
    with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as urlopen:
        urlopen.return_value = _response(payload)
        return resolve_matrix(_settings(**settings))


def test_static_override_needs_no_manifest_fetch() -> None:
    with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as urlopen:
        assert resolve_matrix(_settings(images=["image:one", "image:two"])) == [
            "image:one",
            "image:two",
        ]
    urlopen.assert_not_called()


def test_derives_sorted_variants_and_skips_unstable() -> None:
    payload = {
        "9.1": {"version": "9.1.0-rc2", "debian": {}, "alpine": {}},
        "7.2": {"debian": {}, "alpine": {}},
        "unstable": {"debian": {}, "alpine": {}},
    }
    assert _resolve(payload) == [
        "valkey/valkey:7.2",
        "valkey/valkey:7.2-alpine",
        "valkey/valkey:9.1",
        "valkey/valkey:9.1-alpine",
    ]


def test_unstable_single_variants_and_custom_repository() -> None:
    payload = {
        "10.0": {"alpine": {}},
        "10.1": {"debian": {}},
        "unstable": {"debian": {}},
    }
    assert _resolve(payload, repository="example/valkey", include_unstable=True) == [
        "example/valkey:10.0-alpine",
        "example/valkey:10.1",
        "example/valkey:unstable",
    ]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "non-empty JSON object"),
        ({}, "non-empty JSON object"),
        ({"unstable": {"debian": {}}}, "zero images"),
        ({"9.1": {"alpine": "3.23"}}, "must be an object"),
    ],
)
def test_malformed_or_empty_derivation_fails_closed(payload: object, message: str) -> None:
    with pytest.raises(MatrixResolutionError, match=message):
        _resolve(payload)


def test_non_object_metadata_is_ignored() -> None:
    assert _derive_images(
        {"metadata": "value", "8.0": {"debian": {}}},
        "valkey/valkey",
        False,
    ) == ["valkey/valkey:8.0"]


def test_network_http_and_json_errors_fail_closed() -> None:
    with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as urlopen:
        urlopen.side_effect = URLError("offline")
        with pytest.raises(MatrixResolutionError, match="Failed to fetch"):
            resolve_matrix(_settings())

        urlopen.side_effect = None
        urlopen.return_value = _response({}, status=500)
        with pytest.raises(MatrixResolutionError, match="HTTP 500"):
            resolve_matrix(_settings())

        urlopen.return_value = _response(b"not json")
        with pytest.raises(MatrixResolutionError, match="Invalid"):
            resolve_matrix(_settings())


def test_dynamic_resolution_fetches_once() -> None:
    with patch("scripts.cve_scan.image_matrix.urllib.request.urlopen") as urlopen:
        urlopen.return_value = _response({"8.0": {"debian": {}}})
        assert resolve_matrix(_settings()) == ["valkey/valkey:8.0"]
        assert urlopen.call_count == 1
