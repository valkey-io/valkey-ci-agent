from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.release.checks import evaluate_candidate_ci, require_green_checks
from scripts.release.models import ReleasePolicy

POLICY = ReleasePolicy(
    repo="valkey-io/valkey",
    authorized_team="valkey-io/core-team",
    branches=("9.1",),
    checks_workflow="ci.yml",
    required_checks=("linux", "macos"),
)


def _run(name: str, conclusion: str, suite: int = 7, run_id: int = 1):
    return SimpleNamespace(
        name=name,
        status="completed",
        conclusion=conclusion,
        started_at=None,
        id=run_id,
        html_url=f"https://example/checks/{run_id}",
        _rawData={"check_suite": {"id": suite}},
    )


def _workflow(path: str, suite: int, *, run_id: int = 1, status: str = "completed"):
    return SimpleNamespace(
        path=f".github/workflows/{path}",
        check_suite_id=suite,
        status=status,
        conclusion="success" if status == "completed" else None,
        created_at=None,
        id=run_id,
        html_url=f"https://example/actions/runs/{run_id}",
    )


def _repo(checks, workflows=None):
    repo = MagicMock()
    repo.get_workflow_runs.return_value = workflows or [
        _workflow("ci.yml", 7),
        _workflow("daily.yml", 8),
    ]
    repo.get_commit.return_value.get_check_runs.return_value = checks
    return repo


def test_all_required_checks_pass() -> None:
    require_green_checks(_repo([_run("linux", "success"), _run("macos", "success")]), POLICY, "a" * 40)


def test_missing_or_failed_check_refuses() -> None:
    with pytest.raises(ValueError, match=r"macos \(failure\)"):
        require_green_checks(_repo([_run("linux", "success"), _run("macos", "failure")]), POLICY, "a" * 40)


def test_same_named_check_from_other_workflow_cannot_satisfy() -> None:
    with pytest.raises(ValueError, match=r"macos \(missing\)"):
        require_green_checks(
            _repo([_run("linux", "success"), _run("macos", "success", suite=8)]),
            POLICY,
            "a" * 40,
        )


def test_newest_ci_rerun_cannot_borrow_green_checks_from_older_suite() -> None:
    checks = [
        _run("linux", "success", suite=7),
        _run("macos", "success", suite=7),
        _run("linux", "success", suite=9),
    ]
    workflows = [_workflow("ci.yml", 7, run_id=1), _workflow("ci.yml", 9, run_id=2)]
    with pytest.raises(ValueError, match=r"macos \(missing\)"):
        require_green_checks(_repo(checks, workflows), POLICY, "a" * 40)


def test_latest_ci_workflow_must_finish_before_checks_are_accepted() -> None:
    workflows = [_workflow("ci.yml", 9, run_id=2, status="in_progress")]
    with pytest.raises(ValueError, match="wait for it to complete"):
        require_green_checks(_repo([], workflows), POLICY, "a" * 40)


def test_candidate_ci_snapshot_exposes_exact_run_and_required_check_evidence() -> None:
    snapshot = evaluate_candidate_ci(
        _repo([_run("linux", "success", run_id=11), _run("macos", "failure", run_id=12)]),
        POLICY,
        "a" * 40,
    )

    assert snapshot.workflow_url == "https://example/actions/runs/1"
    assert snapshot.state == "failed"
    assert snapshot.passed_count == 1
    assert [(check.name, check.conclusion, check.url) for check in snapshot.checks] == [
        ("linux", "success", "https://example/checks/11"),
        ("macos", "failure", "https://example/checks/12"),
    ]
