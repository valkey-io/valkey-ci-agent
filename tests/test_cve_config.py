"""Tests for scripts/cve_scan/config.py -- env-var settings validation.

Invalid settings must not silently allow unintended rebuilds. The loader
raises CveScanConfigError on invalid env-var values.
"""

from __future__ import annotations

import pytest

from scripts.cve_scan.config import (
    CveScanConfigError,
    CveScanSettings,
    load_settings,
)
from scripts.cve_scan.models import Severity


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all CVE_SCAN_* env vars before each test."""
    import os

    for key in list(os.environ):
        if key.startswith("CVE_SCAN_"):
            monkeypatch.delenv(key, raising=False)


class TestDefaults:
    def test_defaults_when_no_env_set(self) -> None:
        settings = load_settings()
        assert isinstance(settings, CveScanSettings)
        assert settings.versions_url == (
            "https://raw.githubusercontent.com/valkey-io/valkey-container"
            "/mainline/versions.json"
        )
        assert settings.repository == "valkey/valkey"
        assert settings.include_unstable is False
        assert settings.scanner == "trivy"
        assert settings.severity_threshold == Severity.HIGH
        assert settings.images == []

    def test_settings_is_frozen(self) -> None:
        settings = load_settings()
        with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
            settings.scanner = "invalid"  # type: ignore[misc]


class TestEnvOverrides:
    def test_versions_url_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_VERSIONS_URL", "https://example.com/v.json")
        settings = load_settings()
        assert settings.versions_url == "https://example.com/v.json"

    def test_repository_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_REPOSITORY", "ghcr.io/valkey-io/valkey")
        settings = load_settings()
        assert settings.repository == "ghcr.io/valkey-io/valkey"

    def test_include_unstable_true_variants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for truthy in ("1", "true", "yes", "on", "True", "YES", "TRUE", "ON"):
            monkeypatch.setenv("CVE_SCAN_INCLUDE_UNSTABLE", truthy)
            settings = load_settings()
            assert settings.include_unstable is True, f"Failed for {truthy!r}"

    def test_include_unstable_false_variants(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for falsy in ("0", "false", "no", "off", "False", "NO", "OFF", ""):
            monkeypatch.setenv("CVE_SCAN_INCLUDE_UNSTABLE", falsy)
            settings = load_settings()
            assert settings.include_unstable is False, f"Failed for {falsy!r}"

    def test_scanner_grype_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_SCANNER", "grype")
        with pytest.raises(CveScanConfigError, match="Invalid CVE_SCAN_SCANNER"):
            load_settings()

    def test_scanner_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_SCANNER", "TRIVY")
        settings = load_settings()
        assert settings.scanner == "trivy"

    def test_severity_threshold_all_levels(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for level in ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"):
            monkeypatch.setenv("CVE_SCAN_SEVERITY_THRESHOLD", level)
            settings = load_settings()
            assert settings.severity_threshold == Severity[level]

    def test_severity_threshold_case_insensitive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_SEVERITY_THRESHOLD", "critical")
        settings = load_settings()
        assert settings.severity_threshold == Severity.CRITICAL

    def test_images_static_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_IMAGES", "img:1, img:2 ,img:3")
        settings = load_settings()
        assert settings.images == ["img:1", "img:2", "img:3"]

    def test_images_empty_means_dynamic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_IMAGES", "")
        settings = load_settings()
        assert settings.images == []

    def test_images_whitespace_only_means_dynamic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_IMAGES", "  ,  , ")
        settings = load_settings()
        assert settings.images == []


class TestStrictRejection:
    def test_invalid_scanner_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_SCANNER", "nessus")
        with pytest.raises(CveScanConfigError, match="Invalid CVE_SCAN_SCANNER"):
            load_settings()

    def test_grype_scanner_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_SCANNER", "grype")
        with pytest.raises(CveScanConfigError, match="Invalid CVE_SCAN_SCANNER"):
            load_settings()

    def test_invalid_severity_threshold_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_SEVERITY_THRESHOLD", "APOCALYPTIC")
        with pytest.raises(CveScanConfigError, match="Invalid CVE_SCAN_SEVERITY_THRESHOLD"):
            load_settings()

    def test_garbage_include_unstable_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_INCLUDE_UNSTABLE", "maybe")
        with pytest.raises(CveScanConfigError, match="Invalid CVE_SCAN_INCLUDE_UNSTABLE"):
            load_settings()

    def test_garbage_include_unstable_typo_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CVE_SCAN_INCLUDE_UNSTABLE", "treu")
        with pytest.raises(CveScanConfigError, match="Invalid CVE_SCAN_INCLUDE_UNSTABLE"):
            load_settings()

    def test_empty_platforms_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An empty CVE_SCAN_PLATFORMS must fail, not silently scan zero platforms."""
        monkeypatch.setenv("CVE_SCAN_PLATFORMS", "")
        with pytest.raises(CveScanConfigError, match="at least one non-empty platform"):
            load_settings()

    def test_whitespace_only_platforms_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A platforms value of only separators/whitespace parses empty and must fail."""
        monkeypatch.setenv("CVE_SCAN_PLATFORMS", "  ,  , ")
        with pytest.raises(CveScanConfigError, match="at least one non-empty platform"):
            load_settings()
