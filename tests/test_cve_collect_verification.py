"""Behavior tests for per-architecture verification aggregation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.cve_scan.collect_verification import (
    CollectError,
    Status,
    load_markers,
    main,
    parse_plan,
    reconcile,
)

_Leg = tuple[str, str, str]


def _plan(*legs: _Leg) -> str:
    return json.dumps(
        [
            {
                "line": line,
                "variant": variant,
                "platform": platform,
                "cves": ["CVE-1"],
            }
            for line, variant, platform in legs
        ]
    )


def _marker(
    directory: Path,
    line: str,
    platform: str,
    outcome: str,
    variant: str = "alpine",
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    slug = platform.replace("/", "-")
    (directory / f"{line}-{variant}-{slug}.json").write_text(
        json.dumps(
            {
                "line": line,
                "variant": variant,
                "platform": platform,
                "outcome": outcome,
            }
        ),
        encoding="utf-8",
    )


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan: str,
) -> tuple[int, dict[str, str], str]:
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    code = main(["--markers-dir", str(tmp_path / "markers"), "--plan", plan])
    values = dict(line.partition("=")[::2] for line in output.read_text(encoding="utf-8").splitlines())
    return code, values, summary.read_text(encoding="utf-8")


def test_plan_parses_unique_architecture_legs() -> None:
    assert parse_plan(
        _plan(
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
        )
    ) == {
        ("8.0", "alpine", "linux/amd64"),
        ("8.0", "alpine", "linux/arm64"),
    }


@pytest.mark.parametrize(
    "plan",
    [
        "not json",
        "[]",
        json.dumps({"line": "8.0"}),
        json.dumps([{"line": "8.0"}]),
        json.dumps(
            [
                {
                    "line": "8.0",
                    "variant": "alpine",
                    "platform": "linux/amd64",
                    "cves": [],
                }
            ]
        ),
        _plan(
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/amd64"),
        ),
    ],
)
def test_malformed_plan_fails_closed(plan: str) -> None:
    with pytest.raises(CollectError):
        parse_plan(plan)


def test_unique_marker_files_are_loaded(tmp_path: Path) -> None:
    _marker(tmp_path, "8.0", "linux/amd64", "verified")
    _marker(tmp_path, "8.0", "linux/arm64", "survivors")
    assert {marker.platform for marker in load_markers(tmp_path)} == {
        "linux/amd64",
        "linux/arm64",
    }


def test_duplicate_or_malformed_markers_fail_closed(tmp_path: Path) -> None:
    _marker(tmp_path, "8.0", "linux/amd64", "verified")
    (tmp_path / "duplicate.json").write_text(
        (tmp_path / "8.0-alpine-linux-amd64.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with pytest.raises(CollectError, match="duplicate marker"):
        load_markers(tmp_path)


def test_missing_and_unexpected_legs_are_explicit() -> None:
    expected = {
        ("8.0", "alpine", "linux/amd64"),
        ("8.0", "alpine", "linux/arm64"),
    }
    statuses = reconcile([Status("8.0", "alpine", "linux/amd64", "verified")], expected)
    assert [(item.platform, item.outcome) for item in statuses] == [
        ("linux/amd64", "verified"),
        ("linux/arm64", "missing"),
    ]
    with pytest.raises(CollectError, match="unexpected marker"):
        reconcile([Status("9.1", "alpine", "linux/amd64", "verified")], expected)


def test_any_verified_architecture_dispatches_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    markers = tmp_path / "markers"
    _marker(markers, "8.0", "linux/amd64", "verified")
    _marker(markers, "8.0", "linux/arm64", "survivors")
    code, output, summary = _run(
        tmp_path,
        monkeypatch,
        _plan(
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
        ),
    )
    assert code == 0
    assert output["verified_versions"] == "8.0"
    assert "linux/arm64" in summary
    report = json.loads(output["arch_report"])
    assert report["lines"][0]["dispatched"] is True


def test_lines_are_decided_independently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    markers = tmp_path / "markers"
    _marker(markers, "8.0", "linux/amd64", "verified")
    _marker(markers, "9.1", "linux/amd64", "survivors", "debian")
    code, output, _ = _run(
        tmp_path,
        monkeypatch,
        _plan(
            ("8.0", "alpine", "linux/amd64"),
            ("9.1", "debian", "linux/amd64"),
        ),
    )
    assert code == 0
    assert output["verified_versions"] == "8.0"


def test_all_survivors_is_clean_no_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _marker(tmp_path / "markers", "8.0", "linux/amd64", "survivors")
    code, output, _ = _run(
        tmp_path,
        monkeypatch,
        _plan(("8.0", "alpine", "linux/amd64")),
    )
    assert code == 0
    assert output["verified_versions"] == ""


@pytest.mark.parametrize("outcome", ["error", None])
def test_no_positive_proof_with_unresolved_leg_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str | None,
) -> None:
    markers = tmp_path / "markers"
    _marker(markers, "8.0", "linux/amd64", "survivors")
    if outcome:
        _marker(markers, "8.0", "linux/arm64", outcome)
    code, output, _ = _run(
        tmp_path,
        monkeypatch,
        _plan(
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
        ),
    )
    assert code == 1


def test_bad_input_returns_two_and_empty_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "markers").mkdir()
    code, output, summary = _run(tmp_path, monkeypatch, "[]")
    assert code == 2
    assert output["verified_versions"] == ""
    assert json.loads(output["arch_report"]) == {"lines": []}
    assert "FAIL" in summary
