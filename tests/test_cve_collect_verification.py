"""Tests for scripts/cve_scan/collect_verification.py.

Covers reconciliation of actual markers against the expected verify matrix under
the ANY-architecture gate: a line dispatches when AT LEAST ONE expected leg
verified; errored and missing legs are surfaced but do NOT block a line that has
positive evidence on another architecture; only a run that proves nothing (no
verified leg anywhere AND at least one errored/missing leg) fails closed with a
nonzero exit; and the pre-existing strict-decode fail-closed paths (malformed
marker, unexpected marker, empty set, malformed matrix) still exit 2. Also
covers the CLI outputs (verified_versions / fixable / arch_report), exit codes,
and the per-architecture summary.

These assert BEHAVIOUR, not wiring. The gate changed because
valkey-container's ci.yml rebuilds every platform from a single `version` input
and cannot target one architecture, so a partial fix is a strict improvement:
withholding the dispatch would leave the already-fixed architectures unfixed for
another week while helping the lagging one not at all.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.cve_scan.collect_verification import (
    EntryStatus,
    ExpectedMatrixError,
    Marker,
    MarkerError,
    load_markers,
    main,
    parse_expected_matrix,
    reconcile,
)

_LegKey = tuple[str, str, str]


def _write_marker(
    markers_dir: Path,
    *,
    line: str,
    variant: str = "alpine",
    platform: str = "linux/amd64",
    outcome: str = "verified",
) -> Path:
    """Write one marker JSON file with a per-entry unique name."""
    markers_dir.mkdir(parents=True, exist_ok=True)
    slug = platform.replace("/", "-")
    path = markers_dir / f"{line}-{variant}-{slug}.json"
    path.write_text(
        json.dumps(
            {"line": line, "variant": variant, "platform": platform, "outcome": outcome}
        ),
        encoding="utf-8",
    )
    return path


def _matrix_json(legs: list[_LegKey]) -> str:
    """Build the scan job's verify-matrix JSON for the given expected legs."""
    return json.dumps(
        [
            {
                "line": line,
                "variant": variant,
                "platform": platform,
                "image": f"valkey/valkey:{line}"
                + ("" if variant == "debian" else f"-{variant}"),
            }
            for line, variant, platform in legs
        ]
    )


def _run_main(
    markers_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_matrix: str,
) -> tuple[int, dict[str, str], str]:
    """Run the CLI with GITHUB_OUTPUT/GITHUB_STEP_SUMMARY captured to tmp files."""
    out_path = tmp_path / "github_output.txt"
    summary_path = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))

    rc = main(
        [
            "--markers-dir",
            str(markers_dir),
            "--expected-matrix",
            expected_matrix,
        ]
    )

    outputs: dict[str, str] = {}
    if out_path.exists():
        for entry in out_path.read_text(encoding="utf-8").splitlines():
            key, _, value = entry.partition("=")
            outputs[key] = value
    summary = summary_path.read_text(encoding="utf-8") if summary_path.exists() else ""
    return rc, outputs, summary


# --------------------------------------------------------------------------- #
# parse_expected_matrix(): strict decode, fail closed (unchanged helper)
# --------------------------------------------------------------------------- #


class TestParseExpectedMatrix:
    def test_parses_legs(self) -> None:
        raw = _matrix_json([("8.0", "alpine", "linux/amd64"), ("9.1", "debian", "linux/arm64")])
        assert parse_expected_matrix(raw) == {
            ("8.0", "alpine", "linux/amd64"),
            ("9.1", "debian", "linux/arm64"),
        }

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ExpectedMatrixError, match="not valid JSON"):
            parse_expected_matrix("{not json")

    def test_non_list_raises(self) -> None:
        with pytest.raises(ExpectedMatrixError, match="must be a JSON list"):
            parse_expected_matrix(json.dumps({"line": "8.0"}))

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ExpectedMatrixError, match="empty"):
            parse_expected_matrix("[]")

    def test_bad_entry_key_set_raises(self) -> None:
        raw = json.dumps([{"line": "8.0", "variant": "alpine", "platform": "linux/amd64"}])
        with pytest.raises(ExpectedMatrixError, match="key mismatch"):
            parse_expected_matrix(raw)

    def test_non_string_value_raises(self) -> None:
        raw = json.dumps(
            [{"line": "8.0", "variant": "alpine", "platform": "linux/amd64", "image": 1}]
        )
        with pytest.raises(ExpectedMatrixError, match="must be a string"):
            parse_expected_matrix(raw)

    def test_duplicate_leg_raises(self) -> None:
        raw = _matrix_json(
            [("8.0", "alpine", "linux/amd64"), ("8.0", "alpine", "linux/amd64")]
        )
        with pytest.raises(ExpectedMatrixError, match="duplicate leg"):
            parse_expected_matrix(raw)


# --------------------------------------------------------------------------- #
# reconcile(): any-architecture gate, errors/missing surfaced not blocking
# --------------------------------------------------------------------------- #


class TestReconcile:
    def test_all_expected_verified_dispatches_all_lines(self) -> None:
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("9.1", "debian", "linux/amd64"),
        }
        markers = [
            Marker("8.0", "alpine", "linux/amd64", "verified"),
            Marker("9.1", "debian", "linux/amd64", "verified"),
        ]
        agg = reconcile(markers, expected)
        assert agg.dispatched_lines == ["8.0", "9.1"]
        assert agg.skipped_lines == []
        assert agg.unresolved_legs == []
        assert agg.fixable is True
        assert agg.learned_nothing is False

    def test_one_of_four_verified_dispatches_line(self) -> None:
        """1 verified + 3 survivors: the line dispatches on the any-architecture gate."""
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
            ("8.0", "alpine", "linux/arm/v7"),
            ("8.0", "alpine", "linux/ppc64le"),
        }
        markers = [
            Marker("8.0", "alpine", "linux/amd64", "verified"),
            Marker("8.0", "alpine", "linux/arm64", "survivors"),
            Marker("8.0", "alpine", "linux/arm/v7", "survivors"),
            Marker("8.0", "alpine", "linux/ppc64le", "survivors"),
        ]
        agg = reconcile(markers, expected)
        assert agg.dispatched_lines == ["8.0"]
        assert agg.fixable is True
        assert agg.learned_nothing is False

    def test_three_of_four_verified_dispatches_line(self) -> None:
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
            ("8.0", "alpine", "linux/arm/v7"),
            ("8.0", "alpine", "linux/ppc64le"),
        }
        markers = [
            Marker("8.0", "alpine", "linux/amd64", "verified"),
            Marker("8.0", "alpine", "linux/arm64", "verified"),
            Marker("8.0", "alpine", "linux/arm/v7", "verified"),
            Marker("8.0", "alpine", "linux/ppc64le", "survivors"),
        ]
        agg = reconcile(markers, expected)
        assert agg.dispatched_lines == ["8.0"]
        assert agg.fixable is True

    def test_zero_verified_all_survivors_not_dispatched(self) -> None:
        """No verified leg and no error/missing: legitimate not-fixable, not dispatched."""
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
        }
        markers = [
            Marker("8.0", "alpine", "linux/amd64", "survivors"),
            Marker("8.0", "alpine", "linux/arm64", "survivors"),
        ]
        agg = reconcile(markers, expected)
        assert agg.dispatched_lines == []
        assert agg.skipped_lines == ["8.0"]
        assert agg.fixable is False
        assert agg.learned_nothing is False

    def test_error_leg_does_not_block_line_with_another_verified(self) -> None:
        """One leg errored but another verified: dispatch, and the error is surfaced."""
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
        }
        markers = [
            Marker("8.0", "alpine", "linux/amd64", "verified"),
            Marker("8.0", "alpine", "linux/arm64", "error"),
        ]
        agg = reconcile(markers, expected)
        assert agg.dispatched_lines == ["8.0"]
        assert agg.fixable is True
        assert agg.learned_nothing is False
        # The error is still surfaced, not silently dropped.
        assert [(e.platform, e.status) for e in agg.unresolved_legs] == [
            ("linux/arm64", "error")
        ]

    def test_missing_leg_does_not_block_line_with_another_verified(self) -> None:
        """A leg that died without reporting is missing, but a verified leg still dispatches."""
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
            ("8.0", "alpine", "linux/arm/v7"),
            ("8.0", "alpine", "linux/ppc64le"),
        }
        markers = [Marker("8.0", "alpine", "linux/amd64", "verified")]
        agg = reconcile(markers, expected)
        assert agg.dispatched_lines == ["8.0"]
        assert agg.fixable is True
        assert agg.learned_nothing is False
        assert {e.platform for e in agg.unresolved_legs} == {
            "linux/arm64",
            "linux/arm/v7",
            "linux/ppc64le",
        }
        assert all(e.status == "missing" for e in agg.unresolved_legs)

    def test_no_verified_with_error_learns_nothing(self) -> None:
        """0 verified + an errored leg: nothing dispatched AND we learned nothing usable."""
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
        }
        markers = [
            Marker("8.0", "alpine", "linux/amd64", "error"),
            Marker("8.0", "alpine", "linux/arm64", "survivors"),
        ]
        agg = reconcile(markers, expected)
        assert agg.dispatched_lines == []
        assert agg.fixable is False
        assert agg.learned_nothing is True

    def test_no_verified_all_missing_learns_nothing(self) -> None:
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
        }
        markers = [Marker("8.0", "alpine", "linux/amd64", "survivors")]
        agg = reconcile(markers, expected)
        assert agg.dispatched_lines == []
        assert agg.learned_nothing is True

    def test_lines_partition_independently(self) -> None:
        """One line dispatches on its verified leg; another with only survivors is skipped."""
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
            ("9.1", "debian", "linux/amd64"),
        }
        markers = [
            Marker("8.0", "alpine", "linux/amd64", "verified"),
            Marker("8.0", "alpine", "linux/arm64", "survivors"),
            Marker("9.1", "debian", "linux/amd64", "survivors"),
        ]
        agg = reconcile(markers, expected)
        assert agg.dispatched_lines == ["8.0"]
        assert agg.skipped_lines == ["9.1"]
        assert agg.fixable is True
        assert agg.learned_nothing is False

    def test_one_line_dispatches_even_when_other_line_only_errored(self) -> None:
        """A verified line still dispatches (exit 0 territory) even if another line only errored."""
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("9.1", "debian", "linux/amd64"),
        }
        markers = [
            Marker("8.0", "alpine", "linux/amd64", "verified"),
            Marker("9.1", "debian", "linux/amd64", "error"),
        ]
        agg = reconcile(markers, expected)
        assert agg.dispatched_lines == ["8.0"]
        assert agg.skipped_lines == ["9.1"]
        assert agg.fixable is True
        # There IS positive evidence, so this is not the learned-nothing path.
        assert agg.learned_nothing is False
        assert [(e.line, e.status) for e in agg.unresolved_legs] == [("9.1", "error")]

    def test_unexpected_marker_raises(self) -> None:
        expected = {("8.0", "alpine", "linux/amd64")}
        markers = [
            Marker("8.0", "alpine", "linux/amd64", "verified"),
            Marker("8.0", "alpine", "linux/arm64", "verified"),  # not expected
        ]
        with pytest.raises(MarkerError, match="unexpected marker"):
            reconcile(markers, expected)

    def test_entries_cover_every_expected_leg(self) -> None:
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
        }
        markers = [Marker("8.0", "alpine", "linux/amd64", "verified")]
        agg = reconcile(markers, expected)
        assert {(e.line, e.variant, e.platform) for e in agg.entries} == expected
        assert isinstance(agg.entries[0], EntryStatus)

    def test_line_report_exposes_per_status_legs(self) -> None:
        expected = {
            ("8.0", "alpine", "linux/amd64"),
            ("8.0", "alpine", "linux/arm64"),
        }
        markers = [
            Marker("8.0", "alpine", "linux/amd64", "verified"),
            Marker("8.0", "alpine", "linux/arm64", "survivors"),
        ]
        agg = reconcile(markers, expected)
        (lr,) = agg.lines
        assert lr.dispatched is True
        assert [leg.platform for leg in lr.legs_with("verified")] == ["linux/amd64"]
        assert [leg.platform for leg in lr.legs_with("survivors")] == ["linux/arm64"]


# --------------------------------------------------------------------------- #
# load_markers(): strict parsing, fail closed (unchanged helper)
# --------------------------------------------------------------------------- #


class TestLoadMarkers:
    def test_loads_all_markers(self, tmp_path: Path) -> None:
        _write_marker(tmp_path, line="8.0")
        _write_marker(tmp_path, line="9.1", variant="debian")
        markers = load_markers(tmp_path)
        assert {m.line for m in markers} == {"8.0", "9.1"}

    def test_empty_set_raises(self, tmp_path: Path) -> None:
        with pytest.raises(MarkerError, match="no marker files found"):
            load_markers(tmp_path)

    def test_invalid_json_raises(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(MarkerError, match="not valid JSON"):
            load_markers(tmp_path)

    def test_non_object_raises(self, tmp_path: Path) -> None:
        (tmp_path / "x.json").write_text(json.dumps(["a"]), encoding="utf-8")
        with pytest.raises(MarkerError, match="must be a JSON object"):
            load_markers(tmp_path)

    def test_missing_key_raises(self, tmp_path: Path) -> None:
        (tmp_path / "x.json").write_text(
            json.dumps({"line": "8.0", "variant": "alpine", "platform": "linux/amd64"}),
            encoding="utf-8",
        )
        with pytest.raises(MarkerError, match=r"missing=\['outcome'\]"):
            load_markers(tmp_path)

    def test_extra_key_raises(self, tmp_path: Path) -> None:
        (tmp_path / "x.json").write_text(
            json.dumps(
                {
                    "line": "8.0",
                    "variant": "alpine",
                    "platform": "linux/amd64",
                    "outcome": "verified",
                    "extra": "nope",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(MarkerError, match=r"extra=\['extra'\]"):
            load_markers(tmp_path)

    def test_non_string_value_raises(self, tmp_path: Path) -> None:
        (tmp_path / "x.json").write_text(
            json.dumps(
                {
                    "line": "8.0",
                    "variant": "alpine",
                    "platform": "linux/amd64",
                    "outcome": 1,
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(MarkerError, match="must be a string"):
            load_markers(tmp_path)

    def test_invalid_outcome_raises(self, tmp_path: Path) -> None:
        _write_marker(tmp_path, line="8.0", outcome="maybe")
        with pytest.raises(MarkerError, match="invalid outcome"):
            load_markers(tmp_path)


# --------------------------------------------------------------------------- #
# main(): CLI outputs, summary, exit codes under the any-architecture gate
# --------------------------------------------------------------------------- #


class TestMainCli:
    def test_all_verified_dispatches_all_lines(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0")
        _write_marker(markers, line="9.1", variant="debian")
        expected = _matrix_json(
            [("8.0", "alpine", "linux/amd64"), ("9.1", "debian", "linux/amd64")]
        )
        rc, outputs, summary = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 0
        assert outputs["verified_versions"] == "8.0 9.1"
        assert outputs["fixable"] == "true"

    def test_one_of_four_verified_dispatches_exit_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 verified + 3 survivors: the line is dispatched and the exit is 0."""
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64", outcome="verified")
        _write_marker(markers, line="8.0", platform="linux/arm64", outcome="survivors")
        _write_marker(markers, line="8.0", platform="linux/arm/v7", outcome="survivors")
        _write_marker(markers, line="8.0", platform="linux/ppc64le", outcome="survivors")
        expected = _matrix_json(
            [
                ("8.0", "alpine", "linux/amd64"),
                ("8.0", "alpine", "linux/arm64"),
                ("8.0", "alpine", "linux/arm/v7"),
                ("8.0", "alpine", "linux/ppc64le"),
            ]
        )
        rc, outputs, summary = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 0
        assert outputs["verified_versions"] == "8.0"
        assert outputs["fixable"] == "true"
        # The still-vulnerable architectures are named, never hidden.
        assert "linux/arm64" in summary
        assert "linux/ppc64le" in summary

    def test_three_of_four_verified_dispatches_exit_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64", outcome="verified")
        _write_marker(markers, line="8.0", platform="linux/arm64", outcome="verified")
        _write_marker(markers, line="8.0", platform="linux/arm/v7", outcome="verified")
        _write_marker(markers, line="8.0", platform="linux/ppc64le", outcome="survivors")
        expected = _matrix_json(
            [
                ("8.0", "alpine", "linux/amd64"),
                ("8.0", "alpine", "linux/arm64"),
                ("8.0", "alpine", "linux/arm/v7"),
                ("8.0", "alpine", "linux/ppc64le"),
            ]
        )
        rc, outputs, _ = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 0
        assert outputs["verified_versions"] == "8.0"
        assert outputs["fixable"] == "true"

    def test_all_survivors_is_zero_exit_no_dispatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0 verified (all survivors) is a legitimate no-dispatch, exit 0."""
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64", outcome="survivors")
        _write_marker(markers, line="8.0", platform="linux/arm64", outcome="survivors")
        expected = _matrix_json(
            [("8.0", "alpine", "linux/amd64"), ("8.0", "alpine", "linux/arm64")]
        )
        rc, outputs, _ = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 0
        assert outputs["verified_versions"] == ""
        assert outputs["fixable"] == "false"

    def test_error_leg_with_another_verified_dispatches_exit_zero_error_surfaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One leg errored but another verified: dispatch, exit 0, error still in the summary."""
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64", outcome="verified")
        _write_marker(markers, line="8.0", platform="linux/arm64", outcome="error")
        expected = _matrix_json(
            [("8.0", "alpine", "linux/amd64"), ("8.0", "alpine", "linux/arm64")]
        )
        rc, outputs, summary = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 0
        assert outputs["verified_versions"] == "8.0"
        assert outputs["fixable"] == "true"
        # The error is present in the summary even though we dispatched.
        assert "error" in summary
        assert "linux/arm64" in summary

    def test_missing_leg_with_another_verified_dispatches_exit_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """3 legs died, 1 verified: dispatch, exit 0, missing legs surfaced (not blocking)."""
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64", outcome="verified")
        # arm64 / arm/v7 / ppc64le legs died: no marker.
        expected = _matrix_json(
            [
                ("8.0", "alpine", "linux/amd64"),
                ("8.0", "alpine", "linux/arm64"),
                ("8.0", "alpine", "linux/arm/v7"),
                ("8.0", "alpine", "linux/ppc64le"),
            ]
        )
        rc, outputs, summary = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 0
        assert outputs["verified_versions"] == "8.0"
        assert "missing" in summary
        assert "ppc64le" in summary

    def test_nothing_verified_with_error_is_nonzero_no_dispatch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """0 verified + an errored leg: nonzero exit, no dispatch, fails closed loudly."""
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64", outcome="error")
        _write_marker(markers, line="8.0", platform="linux/arm64", outcome="survivors")
        expected = _matrix_json(
            [("8.0", "alpine", "linux/amd64"), ("8.0", "alpine", "linux/arm64")]
        )
        rc, outputs, summary = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 1
        assert outputs["verified_versions"] == ""
        assert outputs["fixable"] == "false"
        assert "closed" in summary.lower()

    def test_nothing_verified_all_missing_is_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64", outcome="survivors")
        # arm64 leg died: no marker, and nothing verified.
        expected = _matrix_json(
            [("8.0", "alpine", "linux/amd64"), ("8.0", "alpine", "linux/arm64")]
        )
        rc, outputs, summary = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 1
        assert outputs["verified_versions"] == ""
        assert "closed" in summary.lower()

    def test_unexpected_marker_fails_closed_exit_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64")
        _write_marker(markers, line="8.0", platform="linux/arm64")  # not expected
        expected = _matrix_json([("8.0", "alpine", "linux/amd64")])
        rc, outputs, summary = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 2
        assert outputs["verified_versions"] == ""
        assert outputs["fixable"] == "false"
        assert "closed" in summary.lower()

    def test_malformed_marker_is_exit_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        markers = tmp_path / "markers"
        markers.mkdir(parents=True, exist_ok=True)
        (markers / "broken.json").write_text("{ not valid", encoding="utf-8")
        expected = _matrix_json([("8.0", "alpine", "linux/amd64")])
        rc, outputs, _ = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 2
        assert outputs["verified_versions"] == ""
        assert outputs["fixable"] == "false"

    def test_empty_marker_set_fails_closed_exit_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty marker set is fail-closed (exit 2), never an empty green pass."""
        markers = tmp_path / "markers"
        markers.mkdir(parents=True, exist_ok=True)
        expected = _matrix_json([("8.0", "alpine", "linux/amd64")])
        rc, outputs, _ = _run_main(markers, tmp_path, monkeypatch, expected)
        assert rc == 2
        assert outputs["verified_versions"] == ""
        assert outputs["fixable"] == "false"

    def test_malformed_expected_matrix_fails_closed_exit_two(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0")
        rc, outputs, summary = _run_main(markers, tmp_path, monkeypatch, "[")
        assert rc == 2
        assert outputs["verified_versions"] == ""
        assert outputs["fixable"] == "false"
        assert "closed" in summary.lower()


# --------------------------------------------------------------------------- #
# Summary honesty: names every architecture, never a blanket fix claim
# --------------------------------------------------------------------------- #


class TestSummaryHonesty:
    def test_summary_names_every_architecture_with_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64", outcome="verified")
        _write_marker(markers, line="8.0", platform="linux/arm64", outcome="survivors")
        _write_marker(markers, line="8.0", platform="linux/ppc64le", outcome="error")
        expected = _matrix_json(
            [
                ("8.0", "alpine", "linux/amd64"),
                ("8.0", "alpine", "linux/arm64"),
                ("8.0", "alpine", "linux/ppc64le"),
            ]
        )
        _, _, summary = _run_main(markers, tmp_path, monkeypatch, expected)
        # Every affected architecture appears with its reconciled status.
        assert "linux/amd64" in summary and "verified" in summary
        assert "linux/arm64" in summary and "survivors" in summary
        assert "linux/ppc64le" in summary and "error" in summary

    def test_partial_line_is_never_presented_as_fully_fixed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dispatched-but-partial line must not be reported as fixed on every arch."""
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64", outcome="verified")
        _write_marker(markers, line="8.0", platform="linux/arm64", outcome="survivors")
        expected = _matrix_json(
            [("8.0", "alpine", "linux/amd64"), ("8.0", "alpine", "linux/arm64")]
        )
        _, outputs, summary = _run_main(markers, tmp_path, monkeypatch, expected)
        assert outputs["verified_versions"] == "8.0"
        lowered = summary.lower()
        # No blanket-fix wording.
        assert "verified on every affected arch" not in lowered
        assert "fully fixed" not in lowered
        # The still-vulnerable architecture is called out.
        assert "still vulnerable" in lowered
        assert "linux/arm64" in summary


# --------------------------------------------------------------------------- #
# arch_report: stable, parseable machine-readable per-architecture output
# --------------------------------------------------------------------------- #


class TestArchReport:
    def test_arch_report_shape_is_stable_and_parseable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", platform="linux/amd64", outcome="verified")
        _write_marker(markers, line="8.0", platform="linux/arm64", outcome="survivors")
        _write_marker(markers, line="9.1", variant="debian", outcome="survivors")
        expected = _matrix_json(
            [
                ("8.0", "alpine", "linux/amd64"),
                ("8.0", "alpine", "linux/arm64"),
                ("9.1", "debian", "linux/amd64"),
            ]
        )
        _, outputs, _ = _run_main(markers, tmp_path, monkeypatch, expected)
        report = json.loads(outputs["arch_report"])
        assert set(report) == {"lines"}
        by_line = {entry["line"]: entry for entry in report["lines"]}
        assert set(by_line) == {"8.0", "9.1"}

        eight = by_line["8.0"]
        assert eight["dispatched"] is True
        assert set(eight) == {"line", "dispatched", "legs"}
        for leg in eight["legs"]:
            assert set(leg) == {"variant", "platform", "status"}
        statuses = {(leg["platform"], leg["status"]) for leg in eight["legs"]}
        assert statuses == {
            ("linux/amd64", "verified"),
            ("linux/arm64", "survivors"),
        }
        # A line with no verified leg is present but marked not dispatched.
        assert by_line["9.1"]["dispatched"] is False

    def test_arch_report_is_single_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The report is compact so it survives GITHUB_OUTPUT as one key=value line."""
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0", outcome="verified")
        expected = _matrix_json([("8.0", "alpine", "linux/amd64")])
        _, outputs, _ = _run_main(markers, tmp_path, monkeypatch, expected)
        assert "\n" not in outputs["arch_report"]
        assert " " not in outputs["arch_report"]

    def test_arch_report_emitted_even_on_fail_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A fail-closed (exit 2) run still emits a parseable, empty arch_report."""
        markers = tmp_path / "markers"
        _write_marker(markers, line="8.0")
        rc, outputs, _ = _run_main(markers, tmp_path, monkeypatch, "[")
        assert rc == 2
        assert json.loads(outputs["arch_report"]) == {"lines": []}
