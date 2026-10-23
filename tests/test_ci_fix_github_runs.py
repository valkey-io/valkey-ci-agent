"""Tests for failed-job listing: code owns which jobs count as failures."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock

import pytest

from scripts.ci_fix import github_runs
from scripts.ci_fix.github_runs import (
    FailedJob,
    failed_jobs_for_run,
    newest_runs_by_workflow,
    newest_runs_for_head,
)


def _gh_with_jobs(jobs, *, run_attempt=2):
    run = SimpleNamespace(
        jobs=lambda *, _filter: jobs,
        run_attempt=run_attempt,
    )
    repo = MagicMock()
    repo.get_workflow_run.return_value = run
    gh = MagicMock()
    gh.get_repo.return_value = repo
    return gh


def test_only_failure_and_timed_out_count():
    gh = _gh_with_jobs([
        SimpleNamespace(name="build", conclusion="failure", run_attempt=2),
        SimpleNamespace(name="lint", conclusion="timed_out", run_attempt=2),
        SimpleNamespace(
            name="skipped-by-failfast",
            conclusion="cancelled",
            run_attempt=2,
        ),
        SimpleNamespace(name="ok", conclusion="success", run_attempt=2),
        SimpleNamespace(name="skipped", conclusion="skipped", run_attempt=2),
    ])
    names = {
        j.name
        for j in failed_jobs_for_run(gh, "o/r", 1, run_attempt=2)
    }
    assert names == {"build", "lint"}
    # A cancelled job (fail-fast skip) is not a fix target.
    assert "skipped-by-failfast" not in names


def test_a_read_failure_raises_rather_than_reading_as_no_failed_jobs():
    gh = MagicMock()
    gh.get_repo.return_value.get_workflow_run.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        failed_jobs_for_run(gh, "o/r", 1, run_attempt=2, retries=1)


@pytest.mark.parametrize("job_attempt", [None, True, "2", 0, -1])
def test_invalid_job_attempt_fails_closed(job_attempt):
    job = SimpleNamespace(
        id=44,
        name="build",
        conclusion="failure",
    )
    if job_attempt is not None:
        job.run_attempt = job_attempt
    gh = _gh_with_jobs([job])

    with pytest.raises(ValueError, match="does not identify a valid run attempt"):
        failed_jobs_for_run(gh, "o/r", 1, run_attempt=2)


def test_jobs_endpoint_loads_all_attempts_before_selecting_the_exact_one():
    run = MagicMock()
    run.run_attempt = 2
    run.jobs.return_value = []
    gh = MagicMock()
    gh.get_repo.return_value.get_workflow_run.return_value = run

    assert failed_jobs_for_run(gh, "o/r", 1, run_attempt=2) == []
    run.jobs.assert_called_once_with(_filter="all")


def test_actionable_failures_stop_when_a_newer_rerun_starts():
    old = SimpleNamespace(
        id=44,
        name="build",
        conclusion="failure",
        run_attempt=2,
    )
    newer = SimpleNamespace(
        id=55,
        name="build",
        conclusion="success",
        run_attempt=3,
    )
    gh = _gh_with_jobs([old, newer], run_attempt=3)

    with pytest.raises(ValueError, match="currently attempt 3"):
        failed_jobs_for_run(gh, "o/r", 1, run_attempt=2)


def test_failed_jobs_are_a_thin_filter_over_the_raw_snapshot(monkeypatch):
    raw = (
        SimpleNamespace(id=1, name="build", conclusion="failure"),
        SimpleNamespace(id=2, name="ok", conclusion="success"),
    )
    loader = MagicMock(return_value=raw)
    monkeypatch.setattr(github_runs, "jobs_for_run_attempt", loader)

    assert failed_jobs_for_run(
        MagicMock(),
        "o/r",
        10,
        run_attempt=4,
        retries=7,
    ) == [FailedJob("build", 1)]
    loader.assert_called_once_with(
        ANY,
        "o/r",
        10,
        run_attempt=4,
        retries=7,
    )


def test_current_head_loader_filters_locally_and_collapses_old_runs():
    old = SimpleNamespace(
        id=10,
        workflow_id=100,
        event="push",
        head_sha="a" * 40,
        head_branch="feature",
        run_attempt=1,
    )
    newest = SimpleNamespace(
        id=11,
        workflow_id=100,
        event="push",
        head_sha="a" * 40,
        head_branch="feature",
        run_attempt=1,
    )
    stale = SimpleNamespace(
        id=12,
        workflow_id=200,
        event="push",
        head_sha="b" * 40,
        head_branch="feature",
        run_attempt=1,
    )
    wrong_branch = SimpleNamespace(
        id=13,
        workflow_id=300,
        event="push",
        head_sha="a" * 40,
        head_branch="other",
        run_attempt=1,
    )
    gh = MagicMock()
    gh.get_repo.return_value.get_workflow_runs.return_value = [
        old,
        stale,
        wrong_branch,
        newest,
    ]

    assert newest_runs_for_head(
        gh,
        "o/r",
        branch="feature",
        head_sha="a" * 40,
    ) == (newest,)
    gh.get_repo.return_value.get_workflow_runs.assert_called_once_with(
        branch="feature",
        head_sha="a" * 40,
    )


def test_a_whole_suite_schema_validator_is_not_ranked_as_a_build_break():
    from scripts.ci_fix.github_runs import job_priority

    assert job_priority("reply-schemas-linter") == 1   # a lint
    assert job_priority("build-macos-latest") == 1
    assert job_priority("reply-schemas-validator") > job_priority("build-macos-latest")


def test_a_test_job_is_tried_before_the_lint_and_build_jobs_of_the_same_run():
    """A lint or build failure's fix is often in the CI definition, which the bot never
    changes, while a test failure is reproduced and verified: the fork's follow-up spent
    two rounds on format-yaml and build-debian-old before it reached the test."""
    from scripts.ci_fix.github_runs import FailedJob, job_priority, select_job

    jobs = [FailedJob(name=name, id=index) for index, name in enumerate(
        ("format-yaml", "build-debian-old", "test-sanitizer-address", "test-ubuntu-latest"), 1)]

    assert select_job(jobs).name == "test-ubuntu-latest"
    assert job_priority("test-ubuntu-latest") < job_priority("format-yaml") < job_priority("test-sanitizer-address")
    assert job_priority("test-valgrind-test (unit)") == 2  # a valgrind job is not a plain test job


def test_newest_runs_are_collapsed_by_workflow_and_exact_attempt():
    runs = [
        SimpleNamespace(id=10, workflow_id=100, run_attempt=1),
        SimpleNamespace(id=10, workflow_id=100, run_attempt=2),
        SimpleNamespace(id=12, workflow_id=200, run_attempt=1),
    ]

    newest = newest_runs_by_workflow(runs)

    assert [(run.id, run.run_attempt) for run in newest] == [(12, 1), (10, 2)]


def test_newest_runs_keep_distinct_workflow_events():
    runs = [
        SimpleNamespace(id=10, workflow_id=100, event="pull_request", run_attempt=1),
        SimpleNamespace(id=11, workflow_id=100, event="push", run_attempt=1),
    ]

    newest = newest_runs_by_workflow(runs)

    assert [(run.id, run.event) for run in newest] == [
        (11, "push"),
        (10, "pull_request"),
    ]


def test_newest_run_uses_execution_time_before_run_id():
    runs = [
        SimpleNamespace(
            id=100,
            workflow_id=1000,
            run_attempt=2,
            run_started_at=datetime(2026, 10, 14, 12, tzinfo=timezone.utc),
        ),
        SimpleNamespace(
            id=101,
            workflow_id=1000,
            run_attempt=1,
            run_started_at="2026-10-14T11:00:00Z",
        ),
    ]

    newest = newest_runs_by_workflow(runs)

    assert [(run.id, run.run_attempt) for run in newest] == [(100, 2)]


def test_newest_run_prefers_start_time_over_late_completion():
    runs = [
        SimpleNamespace(
            id=100,
            workflow_id=1000,
            run_attempt=1,
            run_started_at="2026-10-14T10:00:00Z",
            updated_at="2026-10-14T12:00:00Z",
        ),
        SimpleNamespace(
            id=101,
            workflow_id=1000,
            run_attempt=1,
            run_started_at="2026-10-14T11:00:00Z",
            updated_at="2026-10-14T11:30:00Z",
        ),
    ]

    newest = newest_runs_by_workflow(runs)

    assert [(run.id, run.run_attempt) for run in newest] == [(101, 1)]
