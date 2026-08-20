"""Integration coverage for scan settings, reporting, and plan emission."""

from __future__ import annotations

import json
import os
from io import BytesIO
from pathlib import Path

import pytest

from scripts.cve_scan.config import load_settings
from scripts.cve_scan.models import Finding, Severity
from scripts.cve_scan.scanner import ScanError
from scripts.cve_scan.sweep import run_sweep

SAMPLE_VERSIONS = {
    line: {
        "version": version,
        "debian": {"version": "trixie"},
        "alpine": {"version": "3.23"},
    }
    for line, version in {
        "7.2": "7.2.13",
        "8.0": "8.0.9",
        "8.1": "8.1.8",
        "9.0": "9.0.4",
        "9.1": "9.1.0",
        "unstable": "unstable",
    }.items()
}


@pytest.fixture(autouse=True)
def _clean_cve_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("CVE_SCAN_"):
            monkeypatch.delenv(key, raising=False)


def _response(data: dict) -> BytesIO:
    response = BytesIO(json.dumps(data).encode())
    response.status = 200  # type: ignore[attr-defined]
    return response


def _outputs(path: Path) -> dict[str, str]:
    return dict(line.partition("=")[::2] for line in path.read_text().splitlines())


def _finding(
    *,
    image: str = "valkey/valkey:8.0-alpine",
    cve: str = "CVE-2024-1234",
    fixed: str | None = "2",
    platform: str = "linux/amd64",
) -> Finding:
    return Finding(image, "openssl", "1", cve, Severity.HIGH, fixed, platform)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    findings: list[Finding],
    *,
    dry_run: bool = True,
    capture: dict[str, object] | None = None,
) -> tuple[dict[str, str], str]:
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(
        "scripts.cve_scan.image_matrix.urllib.request.urlopen",
        lambda *_args, **_kwargs: _response(SAMPLE_VERSIONS),
    )

    def scan(images: list[str], threshold: Severity, **kwargs: object) -> list[Finding]:
        if capture is not None:
            capture.update(images=images, threshold=threshold, **kwargs)
        return findings

    monkeypatch.setattr("scripts.cve_scan.sweep.scan_images", scan)
    run_sweep(settings=load_settings(), dry_run=dry_run)
    return _outputs(output), summary.read_text()


@pytest.mark.parametrize(
    ("findings", "expected"),
    [
        ([], []),
        ([_finding(fixed=None)], []),
        (
            [_finding()],
            [{
                "line": "8.0",
                "variant": "alpine",
                "platform": "linux/amd64",
                "cves": ["CVE-2024-1234"],
            }],
        ),
    ],
)
def test_emits_only_the_verification_plan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    findings: list[Finding],
    expected: list[dict],
) -> None:
    outputs, _summary = _run(monkeypatch, tmp_path, findings)
    assert set(outputs) == {"plan"}
    assert json.loads(outputs["plan"]) == expected


def test_plan_covers_multiple_version_lines(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    findings = [
        _finding(image="valkey/valkey:9.0-alpine", cve="CVE-9"),
        _finding(image="valkey/valkey:7.2", cve="CVE-7", platform="linux/arm64"),
    ]
    outputs, _summary = _run(monkeypatch, tmp_path, findings)
    plan = json.loads(outputs["plan"])
    assert {(leg["line"], leg["variant"], leg["platform"]) for leg in plan} == {
        ("9.0", "alpine", "linux/amd64"),
        ("7.2", "debian", "linux/arm64"),
    }


def test_dynamic_matrix_and_scan_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    capture: dict[str, object] = {}
    _run(monkeypatch, tmp_path, [_finding()], capture=capture)
    images = capture["images"]
    assert isinstance(images, list)
    assert len(images) == 10
    assert "valkey/valkey:8.0" in images
    assert "valkey/valkey:8.0-alpine" in images
    assert all("unstable" not in image for image in images)
    assert capture["threshold"] == Severity.HIGH
    assert capture["platforms"] == [
        "linux/amd64",
        "linux/arm64",
        "linux/arm/v7",
        "linux/ppc64le",
    ]


def test_dry_run_prints_plan_and_candidate_language(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setattr(
        "scripts.cve_scan.image_matrix.urllib.request.urlopen",
        lambda *_args, **_kwargs: _response(SAMPLE_VERSIONS),
    )
    monkeypatch.setattr(
        "scripts.cve_scan.sweep.scan_images",
        lambda *_args, **_kwargs: [_finding()],
    )
    run_sweep(settings=load_settings(), dry_run=True)
    output = capsys.readouterr().out
    assert "plan=[" in output
    assert "VERIFICATION TARGETS" in output
    assert "pending artifact verification" in output
    assert "CVE-2024-1234" in output


@pytest.mark.parametrize(
    ("findings", "expected"),
    [
        ([_finding()], "Fixable candidates (pending artifact verification"),
        ([_finding(fixed=None)], "Unresolved findings (no rebuild)"),
        ([], "No findings at or above the severity threshold"),
    ],
)
def test_job_summary_reports_each_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    findings: list[Finding],
    expected: str,
) -> None:
    _outputs_map, summary = _run(monkeypatch, tmp_path, findings)
    assert expected in summary
    assert "Confirmed fixable" not in summary


def test_scan_failure_is_summarized_and_reraised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("CVE_SCAN_IMAGES", "valkey/valkey:8.0")

    def fail(*_args: object, **_kwargs: object) -> list[Finding]:
        raise ScanError("Trivy failed for linux/amd64")

    monkeypatch.setattr("scripts.cve_scan.sweep.scan_images", fail)
    with pytest.raises(ScanError, match="linux/amd64"):
        run_sweep(settings=load_settings())
    text = summary.read_text()
    assert "### Scan failed" in text
    assert "no rebuild will be dispatched" in text
