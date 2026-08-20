"""Tests for strict CVE scan environment configuration."""

from __future__ import annotations

import os
from dataclasses import FrozenInstanceError

import pytest

from scripts.cve_scan.config import CveScanConfigError, CveScanSettings, load_settings
from scripts.cve_scan.models import Severity


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("CVE_SCAN_"):
            monkeypatch.delenv(key, raising=False)


def test_defaults_are_safe_and_settings_are_frozen() -> None:
    settings = load_settings()
    assert isinstance(settings, CveScanSettings)
    assert settings.versions_url.endswith("/valkey-container/mainline/versions.json")
    assert settings.repository == "valkey/valkey"
    assert settings.include_unstable is False
    assert settings.severity_threshold == Severity.HIGH
    assert settings.images == []
    assert settings.platforms == [
        "linux/amd64",
        "linux/arm64",
        "linux/arm/v7",
        "linux/ppc64le",
    ]
    with pytest.raises(FrozenInstanceError):
        settings.repository = "invalid"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("name", "raw", "field", "expected"),
    [
        ("CVE_SCAN_VERSIONS_URL", "https://example.com/v.json", "versions_url", "https://example.com/v.json"),
        ("CVE_SCAN_REPOSITORY", "ghcr.io/valkey-io/valkey", "repository", "ghcr.io/valkey-io/valkey"),
        ("CVE_SCAN_IMAGES", "img:1, img:2 ,img:3", "images", ["img:1", "img:2", "img:3"]),
        ("CVE_SCAN_IMAGES", "  ,  , ", "images", []),
    ],
)
def test_overrides(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    raw: str,
    field: str,
    expected: object,
) -> None:
    monkeypatch.setenv(name, raw)
    assert getattr(load_settings(), field) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(value, True) for value in ("1", "true", "yes", "on", "True", "YES", "TRUE", "ON")]
    + [(value, False) for value in ("0", "false", "no", "off", "False", "NO", "OFF", "")],
)
def test_include_unstable_aliases(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("CVE_SCAN_INCLUDE_UNSTABLE", raw)
    assert load_settings().include_unstable is expected


@pytest.mark.parametrize("raw", ["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL", "critical"])
def test_severity_levels(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("CVE_SCAN_SEVERITY_THRESHOLD", raw)
    assert load_settings().severity_threshold == Severity[raw.upper()]


@pytest.mark.parametrize(
    ("name", "raw", "message"),
    [
        ("CVE_SCAN_SEVERITY_THRESHOLD", "APOCALYPTIC", "Invalid CVE_SCAN_SEVERITY_THRESHOLD"),
        ("CVE_SCAN_INCLUDE_UNSTABLE", "treu", "Invalid CVE_SCAN_INCLUDE_UNSTABLE"),
        ("CVE_SCAN_PLATFORMS", "", "at least one non-empty platform"),
        ("CVE_SCAN_PLATFORMS", "  ,  , ", "at least one non-empty platform"),
    ],
)
def test_invalid_values_fail_closed(
    monkeypatch: pytest.MonkeyPatch, name: str, raw: str, message: str
) -> None:
    monkeypatch.setenv(name, raw)
    with pytest.raises(CveScanConfigError, match=message):
        load_settings()
