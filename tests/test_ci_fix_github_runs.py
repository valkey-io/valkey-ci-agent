"""Tests for failed-job listing: code owns which jobs count as failures."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts.ci_fix.verify.github_runs import failed_jobs_for_run


def _gh_with_jobs(jobs):
    run = SimpleNamespace(jobs=lambda: jobs)
    repo = MagicMock()
    repo.get_workflow_run.return_value = run
    gh = MagicMock()
    gh.get_repo.return_value = repo
    return gh


def test_only_failure_and_timed_out_count():
    gh = _gh_with_jobs([
        SimpleNamespace(name="build", conclusion="failure"),
        SimpleNamespace(name="lint", conclusion="timed_out"),
        SimpleNamespace(name="skipped-by-failfast", conclusion="cancelled"),
        SimpleNamespace(name="ok", conclusion="success"),
        SimpleNamespace(name="skipped", conclusion="skipped"),
    ])
    names = {j.name for j in failed_jobs_for_run(gh, "o/r", 1)}
    assert names == {"build", "lint"}
    # A cancelled job (fail-fast skip) is not a fix target.
    assert "skipped-by-failfast" not in names


def test_read_failure_yields_empty():
    gh = MagicMock()
    gh.get_repo.return_value.get_workflow_run.side_effect = RuntimeError("boom")
    assert failed_jobs_for_run(gh, "o/r", 1) == []
