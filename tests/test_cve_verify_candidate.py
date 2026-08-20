"""Behavior tests for candidate artifact verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.cve_scan import scanner, verify_candidate

_CVES = '["CVE-2024-1234"]'


def _trivy(*vulnerabilities: dict) -> str:
    return json.dumps(
        {
            "SchemaVersion": 2,
            "ArtifactName": "candidate",
            "ArtifactType": "container_image",
            "Metadata": {"OS": {"Family": "alpine", "Name": "3.23"}},
            "Results": [
                {
                    "Target": "candidate (alpine 3.23)",
                    "Class": "os-pkgs",
                    "Type": "alpine",
                    "Vulnerabilities": list(vulnerabilities),
                }
            ],
        }
    )


def _vulnerability(cve: str, package: str = "openssl") -> dict:
    return {
        "VulnerabilityID": cve,
        "PkgID": f"{package}@1",
        "PkgName": package,
        "InstalledVersion": "1",
        "FixedVersion": "2",
        "Severity": "HIGH",
    }


class _Result:
    def __init__(self, code: int, stdout: str, stderr: str = "") -> None:
        self.returncode = code
        self.stdout = stdout
        self.stderr = stderr


def _patch(monkeypatch: pytest.MonkeyPatch, result: _Result) -> None:
    monkeypatch.setattr(scanner.subprocess, "run", lambda *a, **k: result)


def _args(cves: str = _CVES) -> list[str]:
    return [
        "--image-ref",
        "candidate:8.0-alpine",
        "--cves-json",
        cves,
        "--line",
        "8.0",
        "--variant",
        "alpine",
        "--platform",
        "linux/amd64",
    ]


def test_passes_when_targeted_cve_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch(monkeypatch, _Result(0, _trivy(_vulnerability("CVE-OTHER"))))
    assert verify_candidate.main(_args()) == 0


def test_detects_targeted_cve_after_package_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch(monkeypatch, _Result(0, _trivy(_vulnerability("CVE-2024-1234", "libssl3"))))
    survivors = verify_candidate.verify(
        image_ref="candidate:8.0-alpine",
        cves_json=_CVES,
        platform="linux/amd64",
    )
    assert survivors == [("CVE-2024-1234", "libssl3", "1")]
    assert verify_candidate.main(_args()) == 1


def test_uses_shared_scanner_with_verification_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    call: dict[str, object] = {}

    def scan(image: str, platform: str, **kwargs: object) -> list:
        call.update(image=image, platform=platform, **kwargs)
        return []

    monkeypatch.setattr(verify_candidate, "scan_image", scan)
    assert verify_candidate.verify(
        image_ref="candidate:8.0-alpine",
        cves_json=_CVES,
        platform="linux/amd64",
        trivy_bin="custom-trivy",
    ) == []
    assert call == {
        "image": "candidate:8.0-alpine",
        "platform": "linux/amd64",
        "trivy_bin": "custom-trivy",
        "timeout": 300,
    }


@pytest.mark.parametrize("cves", ["not json", "[]", '["CVE-1", "CVE-1"]', "[1]"])
def test_bad_cve_contract_fails_closed(cves: str) -> None:
    assert verify_candidate.main(_args(cves)) == 2


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (_Result(1, "", "boom"), "exited with code 1"),
        (_Result(0, ""), "empty output"),
        (_Result(0, "not json"), "not valid JSON"),
        (_Result(0, json.dumps({"Results": []})), "schema validation"),
    ],
)
def test_scan_failures_fail_closed(monkeypatch: pytest.MonkeyPatch, result: _Result, message: str) -> None:
    _patch(monkeypatch, result)
    with pytest.raises(verify_candidate.VerifyError, match=message):
        verify_candidate.verify(
            image_ref="candidate:8.0-alpine",
            cves_json=_CVES,
            platform="linux/amd64",
        )


def test_summary_reports_survivor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    _patch(monkeypatch, _Result(0, _trivy(_vulnerability("CVE-2024-1234"))))
    assert verify_candidate.main(_args()) == 1
    text = summary.read_text(encoding="utf-8")
    assert "CVE-2024-1234" in text
    assert "openssl" in text
