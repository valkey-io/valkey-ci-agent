"""Tests for automatic backport CI follow-up selection and orchestration."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts.backport import ci_followup
from scripts.backport.ci_followup import (
    FollowupTarget,
    find_followup_target,
)
from scripts.backport.registry import BranchEntry, RepoEntry
from scripts.ci_fix import github_runs
from scripts.ci_fix.github_runs import FailedJob
from scripts.ci_fix.models import (
    CandidateAction,
    FixCandidate,
    FixPolicy,
    FixResult,
    FrontDoor,
    ResultKind,
    TargetKind,
)

_HEAD = "a" * 40
_BOT = "valkeyrie-ops[bot]"


def _entry(**overrides: Any) -> RepoEntry:
    values: dict[str, Any] = {
        "repo": "valkey-io/valkey",
        "project_owner": "valkey-io",
        "project_owner_type": "organization",
        "language": "c",
        "branches": (BranchEntry(branch="9.0", project_number=18),),
        "automatic_ci_followup": True,
        "ci_followup_ignored_jobs": ("*dco*",),
    }
    values.update(overrides)
    return RepoEntry(**values)


def _pr(
    *,
    author: str = _BOT,
    head_sha: str = _HEAD,
    comments=(),
):
    posted: list[str] = []

    def post(body: str) -> SimpleNamespace:
        posted.append(body)
        return SimpleNamespace(id=len(posted))

    pr = SimpleNamespace(
        number=4226,
        state="open",
        user=SimpleNamespace(login=author),
        base=SimpleNamespace(ref="9.0"),
        head=SimpleNamespace(
            ref="agent/backport/sweep/9.0",
            sha=head_sha,
            repo=SimpleNamespace(full_name="valkey-io/valkey"),
        ),
        get_issue_comments=lambda: list(comments),
        get_commits=lambda: [],
        create_issue_comment=post,
    )
    pr.posted = posted
    return pr


def _run(
    run_id: int = 10,
    *,
    status: str = "completed",
    conclusion: str = "failure",
    workflow_id: int = 100,
    head_sha: str = _HEAD,
    head_branch: str = "agent/backport/sweep/9.0",
    run_attempt: int = 1,
):
    return SimpleNamespace(
        id=run_id,
        workflow_id=workflow_id,
        head_sha=head_sha,
        head_branch=head_branch,
        status=status,
        conclusion=conclusion,
        run_attempt=run_attempt,
    )


def _gh(runs, pr, open_prs=()):
    repo = MagicMock()
    repo.get_workflow_runs.return_value = list(runs)
    repo.get_pull.return_value = pr
    repo.get_pulls.return_value = list(open_prs)
    gh = MagicMock()
    gh.get_repo.return_value = repo
    return gh


def _marker(
    *,
    head: str = _HEAD,
    run: int = 10,
    attempt: int | None = None,
    job: int = 2,
    key: str = "",
    author: str = _BOT,
    outcome: str = "",
    reason: str = "",
):
    attempt_text = f" attempt={attempt}" if attempt is not None else ""
    key_text = f" key={key}" if key else ""
    outcome_text = (f" outcome={outcome}" if outcome else "") + (f" reason={reason}" if reason else "")
    return SimpleNamespace(
        user=SimpleNamespace(login=author),
        body=(
            "<!-- valkey-ci-agent:auto-ci-followup "
            f"head={head} run={run}{attempt_text} job={job}"
            f"{key_text}{outcome_text} -->"
        ),
    )


def _select(
    monkeypatch,
    *,
    pr=None,
    runs=None,
    jobs=None,
    entry=None,
    prior_plans=(),
    release_run=None,
    release_jobs=(),
    ci_sweep=None,
):
    pr = pr or _pr()
    runs = runs if runs is not None else [_run()]
    jobs = jobs if jobs is not None else [
        FailedJob("unit tests", id=3),
    ]
    gh = _gh(runs, pr)
    workflow = gh.get_repo.return_value.get_workflow.return_value
    workflow.get_runs.return_value = [release_run] if release_run is not None else []
    monkeypatch.setattr(
        ci_followup,
        "find_existing_pr",
        lambda *args: ci_sweep if args[-1].startswith("agent/backport/ci-sweep/") else pr,
    )
    attempts_by_run = {
        int(run.id): int(getattr(run, "run_attempt", 1) or 1)
        for run in runs
    }

    def jobs_for_run(_gh, _repo, run_id, *, run_attempt, **_kwargs):
        if release_run is not None and run_id == release_run.id:
            return list(release_jobs)
        assert run_attempt == attempts_by_run[run_id]
        return list(jobs)

    monkeypatch.setattr(
        github_runs,
        "failed_jobs_for_run",
        jobs_for_run,
    )
    target, reason = find_followup_target(
        gh,
        repo_entry=entry or _entry(),
        target_branch="9.0",
        bot_login=_BOT,
        prior_plans=prior_plans,
    )
    return target, reason, gh


def test_finds_current_head_failure_and_ignores_configured_jobs(
    monkeypatch,
) -> None:
    target, reason, _gh_client = _select(
        monkeypatch,
        jobs=[
            FailedJob("DCO", id=1),
            FailedJob("Reply schema validator", id=2),
        ],
    )

    assert reason == "actionable"
    assert target is not None
    assert target.job == FailedJob("Reply schema validator", id=2)


def test_selects_only_the_highest_priority_failure(monkeypatch) -> None:
    target, reason, _gh_client = _select(
        monkeypatch,
        jobs=[
            FailedJob("asan tests", id=1),
            FailedJob("clang format", id=2),
            FailedJob("reply schema", id=3),
        ],
    )

    assert reason == "actionable"
    assert target is not None
    assert target.job.id == 2


def test_waits_until_every_current_head_run_finishes(monkeypatch) -> None:
    target, reason, _gh_client = _select(
        monkeypatch,
        runs=[
            _run(10),
            _run(11, status="in_progress", conclusion=""),
        ],
    )

    assert target is None
    assert reason == "current-head-ci-running"


def test_ignores_run_returned_for_a_different_head_branch(monkeypatch) -> None:
    target, reason, _gh_client = _select(
        monkeypatch,
        runs=[_run(head_branch="agent/backport/sweep/8.1")],
    )

    assert target is None
    assert reason == "no-current-head-runs"


def test_newer_success_suppresses_old_failure_for_only_that_workflow(
    monkeypatch,
) -> None:
    runs = [
        _run(10, workflow_id=100, conclusion="failure"),
        _run(11, workflow_id=100, conclusion="success"),
        _run(12, workflow_id=200, conclusion="failure", run_attempt=3),
    ]
    pr = _pr()
    gh = _gh(runs, pr)
    monkeypatch.setattr(ci_followup, "find_existing_pr", lambda *_args: pr)
    jobs_for_run = MagicMock(
        return_value=[FailedJob("build", id=22)],
    )
    monkeypatch.setattr(github_runs, "failed_jobs_for_run", jobs_for_run)

    target, reason = find_followup_target(
        gh,
        repo_entry=_entry(),
        target_branch="9.0",
        bot_login=_BOT,
    )

    assert reason == "actionable"
    assert target is not None
    assert (target.run.id, target.run.run_attempt) == (12, 3)
    jobs_for_run.assert_called_once_with(
        gh,
        "valkey-io/valkey",
        12,
        run_attempt=3,
    )


def test_newer_successful_attempt_suppresses_older_failed_attempt(
    monkeypatch,
) -> None:
    target, reason, _gh_client = _select(
        monkeypatch,
        runs=[
            _run(10, workflow_id=100, conclusion="failure", run_attempt=1),
            _run(10, workflow_id=100, conclusion="success", run_attempt=2),
        ],
    )

    assert target is None
    assert reason == "no-unhandled-actionable-failures"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        (lambda pr: setattr(pr, "state", "closed"), "pr-not-open"),
        (
            lambda pr: setattr(pr.base, "ref", "8.1"),
            "base-branch-mismatch",
        ),
        (
            lambda pr: setattr(pr.head, "ref", "not-a-bot-namespace"),
            "head-branch-not-writable",
        ),
        (
            lambda pr: setattr(
                pr.head.repo,
                "full_name",
                "attacker/valkey",
            ),
            "head-repository-mismatch",
        ),
        (
            lambda pr: setattr(pr.head, "sha", "not-a-sha"),
            "invalid-head-sha",
        ),
        (
            lambda pr: setattr(pr.user, "login", "maintainer"),
            "pr-not-bot-owned",
        ),
    ],
)
def test_rejects_a_pr_that_is_not_the_expected_bot_sweep(
    monkeypatch,
    change,
    reason,
) -> None:
    pr = _pr()
    change(pr)
    target, actual, _gh_client = _select(monkeypatch, pr=pr)

    assert target is None
    assert actual == reason


def test_terminal_marker_prevents_retry_of_the_exact_job(
    monkeypatch,
) -> None:
    pr = _pr(comments=(_marker(job=2),))
    target, reason, _gh_client = _select(
        monkeypatch,
        pr=pr,
        jobs=[
            FailedJob("Reply schema validator", id=2),
        ],
    )

    assert target is None
    assert reason == "no-unhandled-actionable-failures"


def test_logical_marker_deduplicates_twin_event_runs(
    monkeypatch,
) -> None:
    first_run = _run(10, workflow_id=100)
    key = ci_followup._job_key(first_run, "Reply schema validator")
    pr = _pr(comments=(_marker(run=10, job=2, key=key),))
    target, reason, _gh_client = _select(
        monkeypatch,
        pr=pr,
        runs=[_run(11, workflow_id=100)],
        jobs=[
            FailedJob("Reply schema validator", id=22),
        ],
    )

    assert target is None
    assert reason == "no-unhandled-actionable-failures"


def test_job_the_fixer_declined_twice_on_earlier_heads_is_left_alone(monkeypatch) -> None:
    key = ci_followup._job_key(_run(10, workflow_id=100), "asan tests")
    older = tuple(
        _marker(head=head * 40, run=run, job=run, key=key, outcome="refused", reason="declined")
        for head, run in (("c", 5), ("d", 6))
    )
    jobs = [FailedJob("asan tests", id=22)]

    target, reason, _gh_client = _select(
        monkeypatch, pr=_pr(comments=older), runs=[_run(11, workflow_id=100)], jobs=jobs,
    )
    assert target is None
    assert reason == "no-unhandled-actionable-failures"

    # One time, two pushed fixes, or two fixes refused at publication (a head that moved) do not retire the job.
    target, reason, _gh_client = _select(
        monkeypatch, pr=_pr(comments=older[:1]), runs=[_run(11, workflow_id=100)], jobs=jobs,
    )
    assert reason == "actionable"
    for outcome in ("pushed", "refused"):
        earlier = tuple(
            _marker(head=head * 40, run=run, job=run, key=key, outcome=outcome)
            for head, run in (("c", 5), ("d", 6))
        )
        target, reason, _gh_client = _select(
            monkeypatch, pr=_pr(comments=earlier), runs=[_run(11, workflow_id=100)], jobs=jobs,
        )
        assert reason == "actionable"


def test_same_job_name_in_another_workflow_remains_actionable(
    monkeypatch,
) -> None:
    key = ci_followup._job_key(_run(10, workflow_id=100), "build")
    pr = _pr(comments=(_marker(run=10, job=2, key=key),))
    target, reason, _gh_client = _select(
        monkeypatch,
        pr=pr,
        runs=[_run(11, workflow_id=200)],
        jobs=[FailedJob("build", id=22)],
    )

    assert reason == "actionable"
    assert target is not None
    assert target.job.id == 22


def test_old_head_markers_neither_handle_jobs_nor_consume_budget(
    monkeypatch,
) -> None:
    old_comments = tuple(
        _marker(
            head=digit * 40,
            run=index,
            job=index,
        )
        for index, digit in enumerate(("b", "c", "d"), start=1)
    )
    pr = _pr(comments=old_comments)
    target, reason, gh = _select(monkeypatch, pr=pr)

    assert reason == "actionable"
    assert target is not None
    gh.get_repo.return_value.get_workflow_runs.assert_called_once()


def test_attempt_budget_counts_only_terminal_comments_on_current_head(
    monkeypatch,
) -> None:
    pr = _pr(
        comments=tuple(
            _marker(run=index, job=index)
            for index in range(1, 8)
        ),
    )
    target, reason, gh = _select(monkeypatch, pr=pr)

    assert target is None
    assert reason == "attempt-budget-exhausted"
    gh.get_repo.return_value.get_workflow_runs.assert_not_called()


def test_refusals_retire_their_jobs_without_spending_the_heads_attempts(monkeypatch) -> None:
    """A refusal changed nothing on the branch, so it is not one of the head's seven attempts."""
    refused = tuple(_marker(run=index, job=index, outcome="refused") for index in range(30, 33))
    pushed = tuple(_marker(run=index, job=index, outcome="pushed") for index in range(40, 46))
    target, reason, _gh = _select(monkeypatch, pr=_pr(comments=refused + pushed))

    assert reason == "actionable" and target is not None
    _target, reason, _gh = _select(monkeypatch, pr=_pr(comments=pushed + (_marker(run=42, job=42, outcome="opened"),)))
    assert reason == "attempt-budget-exhausted"


def test_markers_from_other_authors_are_ignored(monkeypatch) -> None:
    pr = _pr(comments=(_marker(job=3, author="someone-else"),))
    target, reason, _gh_client = _select(monkeypatch, pr=pr)

    assert reason == "actionable"
    assert target is not None


def _target(
    pr,
    *,
    job: FailedJob | None = None,
    run_attempt: int = 2,
):
    return FollowupTarget(
        pr=pr,
        run=_run(run_attempt=run_attempt),
        head_sha=_HEAD,
        head_branch="agent/backport/sweep/9.0",
        job=job or FailedJob("unit tests", id=3),
    )


def _plan(monkeypatch, target, reason="actionable", *, upstream_open: int = 0):
    monkeypatch.setattr(
        ci_followup, "load_registry",
        lambda _path: SimpleNamespace(get_branch=lambda repo, branch: (_entry(), None)),
    )
    monkeypatch.setattr(ci_followup, "find_followup_target", lambda *_args, **_kwargs: (target, reason))
    client = MagicMock()
    client.get_repo.return_value.default_branch = "unstable"
    # The bot's open pull requests into the default branch.
    client.search_issues.return_value.totalCount = upstream_open
    return ci_followup.plan_request(
        client, None, {"repo": "valkey-io/valkey", "branch": "9.0"}, bot_login=_BOT,
    )


def test_a_sweep_fix_is_run_on_the_default_branch_too(monkeypatch) -> None:
    plan = _plan(monkeypatch, _target(_pr()))

    assert plan.task.target.upstream_branch == "unstable"


def test_while_the_bot_has_its_most_pull_requests_into_the_default_branch_open_a_sweep_waits(monkeypatch) -> None:
    result = _plan(monkeypatch, _target(_pr()), upstream_open=3)

    assert result == {"repo": "valkey-io/valkey", "branch": "9.0", "action": "skipped",
                      "reason": "upstream-pr-cap-reached"}


def test_a_fix_opened_into_the_default_branch_is_a_line_on_the_sweep_pr_and_its_job_waits(monkeypatch) -> None:
    sweep = _pr()
    client = MagicMock()
    client.get_repo.return_value.get_pull.side_effect = lambda number: {4226: sweep}[number]
    plan = _plan(monkeypatch, _target(sweep, job=FailedJob("test-ubuntu-tls", id=27)))
    candidate = FixCandidate(action=CandidateAction.PATCH, failing_check="t")

    ci_followup.report_result(client, plan, FixResult(
        kind=ResultKind.OPENED, summary="s", task=plan.task, candidate=candidate, commit_sha="b" * 40,
        pr_number=9, open_upstream=True,
    ))

    # Not explained on #9, whose own description says it all.
    (line,) = sweep.posted
    assert line.splitlines()[2].startswith("Opened #9 into `unstable` with a fix for `test-ubuntu-tls` ([run](")
    assert "Backport it to `9.0` once it merges." in line
    assert "<!-- valkey-ci-agent:backport-suggestion pr=9 -->" in line and " outcome=opened reason=backport -->" in line


def test_the_plan_binds_the_exact_job_and_its_markers(monkeypatch) -> None:
    target = _target(_pr(), job=FailedJob("Reply schema validator", id=27), run_attempt=4)

    plan = _plan(monkeypatch, target)

    task = plan.task
    assert plan.front_door is FrontDoor.FOLLOW_UP
    assert task.policy is FixPolicy.BACKPORT
    assert task.failure.repo_full_name == "valkey-io/valkey"
    assert task.failure.run_id == 10
    assert task.failure.run_attempt == 4
    assert task.failure.head_sha == _HEAD
    assert task.failure.job_id == 27
    assert task.failure.job_name == "Reply schema validator"
    assert task.failure.pr_number == 4226
    assert task.target.kind is TargetKind.UPDATE_PR
    assert task.target.repo_full_name == "valkey-io/valkey"
    assert task.target.branch == "agent/backport/sweep/9.0"
    assert task.target.expected_sha == _HEAD
    assert task.failure.base_branch == "9.0"
    (marker,) = plan.markers
    assert f"head={_HEAD} run=10 attempt=4 job=27" in marker and "key=" in marker


def test_a_skip_plans_nothing(monkeypatch) -> None:
    assert _plan(monkeypatch, None, "current-head-ci-running") == {
        "repo": "valkey-io/valkey",
        "branch": "9.0",
        "action": "skipped",
        "reason": "current-head-ci-running",
    }


def _report(monkeypatch, kind, **result):
    pr = _pr()
    gh = MagicMock()
    gh.get_repo.return_value.get_pull.return_value = pr
    monkeypatch.setattr(ci_followup, "render_result", lambda result: f"result: {result.summary}")
    plan = _plan(monkeypatch, _target(pr, job=FailedJob("Reply schema validator", id=27)))
    report = ci_followup.report_result(
        gh, plan, FixResult(kind=kind, summary="the summary", task=plan.task, **result),
    )
    return report, pr


def test_a_terminal_result_is_posted_with_the_marker_that_retires_the_job(monkeypatch) -> None:
    report, pr = _report(monkeypatch, ResultKind.PUSHED, commit_sha="b" * 40)

    assert report["action"] == "pushed"
    assert report["commit"] == "b" * 40
    assert report["job"] == {"id": 27, "name": "Reply schema validator"}
    (body,) = pr.posted
    assert "result: the summary" in body
    assert "<!-- valkey-ci-agent:auto-ci-followup" in body and " outcome=pushed -->" in body


def test_a_failed_result_does_not_comment(monkeypatch) -> None:
    report, pr = _report(monkeypatch, ResultKind.FAILED)

    assert report["action"] == "failed"
    assert pr.posted == []


def test_a_refusal_is_posted_with_a_terminal_marker(monkeypatch) -> None:
    report, pr = _report(monkeypatch, ResultKind.REFUSED)

    assert report["action"] == "refused"
    (body,) = pr.posted
    assert "result: the summary" in body
    assert " outcome=refused -->" in body


def test_a_refusal_says_whether_the_fixer_declined_or_a_prepared_fix_went_unpublished(monkeypatch) -> None:
    """Only the first says something about the job, and counts toward leaving it alone."""
    _report_, declined = _report(monkeypatch, ResultKind.REFUSED, candidate=FixCandidate(CandidateAction.REFUSE))
    _report_, unpublished = _report(monkeypatch, ResultKind.REFUSED, candidate=FixCandidate(CandidateAction.PATCH))

    assert " outcome=refused reason=declined -->" in declined.posted[0]
    assert " outcome=refused -->" in unpublished.posted[0]
    assert ci_followup._MARKER_RE.search(declined.posted[0]).group("reason") == "declined"


def test_failed_attempts_post_nothing_and_their_plans_bound_the_retries(monkeypatch) -> None:
    """A failure is not commented on the PR; the plans of earlier runs record it."""
    report, pr = _report(monkeypatch, ResultKind.FAILED)
    assert report["action"] == "failed" and pr.posted == []

    pr = _pr()
    first = _plan(monkeypatch, _target(pr))
    target, reason, _gh = _select(monkeypatch, pr=pr, prior_plans=(first,))
    assert reason == "actionable" and target is not None

    target, reason, _gh = _select(monkeypatch, pr=pr, prior_plans=(first, first))
    assert target is None and reason == "no-unhandled-actionable-failures"


def test_an_attempt_that_failed_before_the_engine_started_has_a_budget_of_its_own(monkeypatch) -> None:
    """A runner outage fails every run it lasts: none says anything about the job, but it is not retried forever."""
    pr = _pr()
    failed = _plan(monkeypatch, _target(pr))
    outage = replace(failed, never_started=True)

    # Neither a failure of its job, which two would retire, nor one of the head's attempts.
    target, reason, _gh = _select(monkeypatch, pr=pr, prior_plans=(outage,) * 6 + (failed,))
    assert reason == "actionable" and target is not None

    target, reason, _gh = _select(monkeypatch, pr=pr, prior_plans=(outage,) * 7)
    assert target is None and reason == "attempt-budget-exhausted"


def test_a_plan_whose_attempt_was_commented_is_not_counted_again(monkeypatch) -> None:
    """A fix or refusal is already on the PR: its plan is not also a failed attempt."""
    pr = _pr()
    first = _plan(monkeypatch, _target(pr))
    refused = tuple(
        SimpleNamespace(user=SimpleNamespace(login=_BOT), body=marker.removesuffix(" -->") + " outcome=refused -->")
        for marker in first.markers
    )
    target, reason, _gh = _select(
        monkeypatch, pr=_pr(comments=refused), prior_plans=(first, first, first),
    )
    assert reason != "attempt-budget-exhausted"


def test_a_job_that_keeps_failing_is_retried_a_bounded_number_of_times(monkeypatch) -> None:
    """Failure comments posted before failures went silent still count."""
    once = (_marker(run=10, job=3, outcome="failed"),)
    target, reason, _gh = _select(monkeypatch, pr=_pr(comments=once))
    assert reason == "actionable" and target is not None and target.job.id == 3

    twice = once + (_marker(run=11, job=3, outcome="failed"),)
    target, reason, _gh = _select(monkeypatch, pr=_pr(comments=twice))
    assert target is None and reason == "no-unhandled-actionable-failures"


def test_failed_attempts_count_against_the_heads_budget(monkeypatch) -> None:
    failed = tuple(_marker(run=index, job=index, outcome="failed") for index in range(50, 57))
    _target, reason, _gh = _select(monkeypatch, pr=_pr(comments=failed[:6]))
    assert reason != "attempt-budget-exhausted"

    _target, reason, _gh = _select(monkeypatch, pr=_pr(comments=failed))
    assert reason == "attempt-budget-exhausted"


# --- the release branch's own failures, from the run of every release branch -------

_WEEKLY = (
    FailedJob("run-daily-for-release-branches (8.0) / test-ubuntu-jemalloc", id=70),
    FailedJob("run-daily-for-release-branches (9.0) / code-coverage", id=71),
    FailedJob("run-daily-for-release-branches (9.0) / test-valgrind-misc", id=72),
    FailedJob("run-daily-for-release-branches (9.0) / test-ubuntu-tls", id=73),
    FailedJob("determine-release-branches", id=74),
)


def _weekly_run(**fields):
    run = _run(500, workflow_id=900, head_sha="f" * 40, head_branch="unstable", **fields)
    run.path, run.event = ".github/workflows/weekly.yml", "schedule"
    return run


def _weekly_entry():
    return _entry(ci_followup_release_workflow="weekly.yml", ci_followup_ignored_jobs=("code-coverage*",))


def _select_weekly(monkeypatch, **kwargs):
    """The sweep PR's own CI is green unless ``runs`` says otherwise."""
    kwargs.setdefault("runs", [_run(conclusion="success")])
    kwargs.setdefault("release_run", _weekly_run())
    return _select(monkeypatch, entry=_weekly_entry(), release_jobs=_WEEKLY, **kwargs)


def test_a_release_branchs_weekly_failure_is_fixed_on_its_sweep_pr(monkeypatch) -> None:
    """This branch's jobs only, the one most likely to have a fix first, and never a job that only reports."""
    target, reason, gh = _select_weekly(monkeypatch)

    assert reason == "actionable" and target.release_run
    assert target.job == FailedJob("run-daily-for-release-branches (9.0) / test-ubuntu-tls", id=73)
    assert target.run.id == 500 and target.head_sha == _HEAD
    gh.get_repo.return_value.get_workflow.assert_called_once_with("weekly.yml")
    gh.get_repo.return_value.get_workflow.return_value.get_runs.assert_called_once_with(event="schedule")


def test_the_plan_for_a_weekly_failure_replays_the_scheduled_job_on_the_sweep_head(monkeypatch) -> None:
    target, _reason, _gh_client = _select_weekly(monkeypatch)

    task = _plan(monkeypatch, target).task

    # The job as daily.yml names it, run as scheduled, on the head the fix is pushed to.
    assert task.failure.job_name == "test-ubuntu-tls" and task.failure.job_id == 73
    assert task.failure.scheduled and task.failure.workflow == "weekly"
    # Weekly's jobs are the default branch's daily.yml, at the commit the run was started on.
    assert task.failure.workflow_sha == "f" * 40
    assert task.failure.run_id == 500 and task.failure.head_sha == _HEAD and task.failure.pr_number == 4226
    assert "scheduled run of release branch `9.0`" in task.failure.description
    assert task.policy is FixPolicy.BACKPORT and task.target.kind is TargetKind.UPDATE_PR
    assert task.target.branch == "agent/backport/sweep/9.0" and task.target.expected_sha == _HEAD


def test_the_pull_requests_own_failure_comes_before_the_release_branchs(monkeypatch) -> None:
    target, reason, gh = _select_weekly(monkeypatch, runs=[_run()])

    assert reason == "actionable" and not target.release_run and target.job.id == 3
    gh.get_repo.return_value.get_workflow.assert_not_called()


def test_a_weekly_failure_is_handled_once_however_often_the_head_moves(monkeypatch) -> None:
    """A fix pushed for it moves the head, and the weekly run is not run again on the new one."""
    earlier_head = tuple(
        _marker(head="b" * 40, run=500, job=job, outcome=outcome)
        for job, outcome in ((73, "pushed"), (72, "refused"))
    )

    target, reason, _gh_client = _select_weekly(monkeypatch, pr=_pr(comments=earlier_head))

    assert target is None and reason == "no-unhandled-actionable-failures"

    # An attempt that failed reported nothing, and is tried again.
    failed = (_marker(head="b" * 40, run=500, job=73, outcome="failed"), earlier_head[1])
    target, reason, _gh_client = _select_weekly(monkeypatch, pr=_pr(comments=failed))
    assert reason == "actionable" and target.job.id == 73


@pytest.mark.parametrize("release_run", [
    _weekly_run(status="in_progress", conclusion=None),  # says nothing yet; the one before it is a week old
    _weekly_run(conclusion="success"),
])
def test_a_weekly_run_that_is_running_or_passed_has_nothing_to_fix(monkeypatch, release_run) -> None:
    target, reason, _gh_client = _select_weekly(monkeypatch, release_run=release_run)

    assert target is None and reason == "no-unhandled-actionable-failures"


def test_a_weekly_run_that_cannot_be_read_does_not_fail_the_follow_up(monkeypatch) -> None:
    """Its jobs are several hundred, over several requests: GitHub ended one early, and the run failed."""
    monkeypatch.setattr(ci_followup, "find_existing_pr", lambda *_args: _pr())
    gh = _gh([_run(conclusion="success")], _pr())
    gh.get_repo.return_value.get_workflow.return_value.get_runs.return_value = [_weekly_run()]

    def jobs_for_run(_gh, _repo, _run_id, **_kwargs):
        raise ConnectionError("Response ended prematurely")

    monkeypatch.setattr(github_runs, "failed_jobs_for_run", jobs_for_run)

    target, reason = find_followup_target(
        gh, repo_entry=_weekly_entry(), target_branch="9.0", bot_login=_BOT,
    )

    assert target is None and reason == "no-unhandled-actionable-failures"


def test_weekly_failures_are_followed_up_only_where_the_registry_names_the_workflow(monkeypatch) -> None:
    target, reason, gh = _select(
        monkeypatch, runs=[_run(conclusion="success")], release_run=_weekly_run(), release_jobs=_WEEKLY,
    )

    assert target is None and reason == "no-unhandled-actionable-failures"
    gh.get_repo.return_value.get_workflow.assert_not_called()


def test_a_weekly_fix_is_reported_as_the_release_branchs_failure(monkeypatch) -> None:
    target, _reason, _gh_client = _select_weekly(monkeypatch)
    plan = _plan(monkeypatch, target)
    posted = []
    pull = SimpleNamespace(create_issue_comment=posted.append)
    gh = MagicMock()
    gh.get_repo.return_value.get_pull.return_value = pull
    result = FixResult(kind=ResultKind.REFUSED, summary="A port clash on the runner.", task=plan.task,
                       candidate=FixCandidate(action=CandidateAction.REFUSE, summary="A port clash on the runner."))

    ci_followup.report_result(gh, plan, result)

    assert posted[0].startswith("Automatic follow-up on the scheduled test run of `9.0`")
    assert "run=500" in posted[0] and "job=73" in posted[0] and "outcome=refused reason=declined" in posted[0]


# --- a pushed fix or a draft PR also has CI nobody else watches --------------------

def _fix_pr(branch="agent/ci-fix/issue-9-100-1", *, author=_BOT, base="9.0", head_sha="d" * 40):
    pr = _pr(author=author, head_sha=head_sha)
    pr.number = 4300
    pr.head.ref = branch
    pr.base.ref = base
    return pr


def test_a_bot_fix_prs_red_head_is_followed_up(monkeypatch) -> None:
    fix = _fix_pr()
    runs = [_run(20, head_sha=fix.head.sha, head_branch=fix.head.ref)]
    monkeypatch.setattr(ci_followup, "find_existing_pr", lambda *_args: None)  # no sweep PR
    monkeypatch.setattr(github_runs, "failed_jobs_for_run", lambda *_a, **_k: (
        FailedJob(name="test-ubuntu", id=7),))
    target, reason = ci_followup.find_followup_target(
        _gh(runs, fix, open_prs=[fix]), repo_entry=_entry(), target_branch="9.0", bot_login=_BOT,
    )
    assert reason == "actionable"
    assert target is not None and target.pr is fix and target.head_branch == fix.head.ref


@pytest.mark.parametrize("pr, expected", [
    (_fix_pr(branch="contributor-branch"), "no-open-bot-pr"),
    (_fix_pr(author="someone"), "no-open-bot-pr"),
])
def test_only_bot_owned_namespace_prs_are_followed_up(monkeypatch, pr, expected) -> None:
    monkeypatch.setattr(ci_followup, "find_existing_pr", lambda *_args: None)
    target, reason = ci_followup.find_followup_target(
        _gh([], pr, open_prs=[pr]), repo_entry=_entry(), target_branch="9.0", bot_login=_BOT,
    )
    assert (target, reason) == (None, expected)


def test_the_sweep_pr_is_followed_up_before_a_fix_pr(monkeypatch) -> None:
    sweep, fix = _pr(), _fix_pr()
    runs = {sweep.head.sha: [_run(10)], fix.head.sha: [_run(20, head_sha=fix.head.sha, head_branch=fix.head.ref)]}
    monkeypatch.setattr(ci_followup, "find_existing_pr", lambda *_args: sweep)
    monkeypatch.setattr(ci_followup, "newest_runs_for_head",
                        lambda _gh, _repo, *, branch, head_sha: runs.get(head_sha, []))
    monkeypatch.setattr(github_runs, "failed_jobs_for_run", lambda *_a, **_k: (
        FailedJob(name="test-ubuntu", id=7),))
    target, _reason = ci_followup.find_followup_target(
        _gh([], sweep, open_prs=[fix]), repo_entry=_entry(), target_branch="9.0", bot_login=_BOT,
    )
    assert target is not None and target.pr is sweep


def test_a_sweep_fix_goes_to_the_ci_sweep_branch_and_a_fix_prs_to_its_own(monkeypatch) -> None:
    """The sweep carries only backports; a fix PR the bot opened is its own fix's place."""
    assert _plan(monkeypatch, _target(_pr())).task.target.fix_branch == "agent/backport/ci-sweep/9.0"

    fix = _fix_pr()
    target = replace(_target(fix), head_branch=fix.head.ref, head_sha=fix.head.sha)
    task = _plan(monkeypatch, target).task
    assert task.target.branch == fix.head.ref and task.target.fix_branch == ""


def _ci_sweep_with(*jobs: str):
    """An open CI-sweep PR whose commits are a backport its branch was built on and a fix for each of ``jobs``."""
    backport = SimpleNamespace(message="Backport x (#1)\n\nBackport-Source-PR: 1")
    commits = [SimpleNamespace(sha="b" * 40, commit=backport)]
    commits += [
        SimpleNamespace(sha=f"{index:040x}", commit=SimpleNamespace(message=f"Fix {job}\n\nCI-Fix-Job: {job}"))
        for index, job in enumerate(jobs, 1)
    ]
    return SimpleNamespace(number=4400, get_commits=lambda: list(commits))


def test_a_job_with_a_fix_in_the_open_ci_sweep_pr_is_not_fixed_again(monkeypatch) -> None:
    """The sweep's head is without the fix until the CI-sweep PR is merged, on every new head."""
    target, reason, _gh_client = _select(monkeypatch, ci_sweep=_ci_sweep_with("unit tests"))
    assert target is None and reason == "no-unhandled-actionable-failures"

    # A fix the sweep's rebase dropped is no longer there, and its job is fixed again.
    target, reason, _gh_client = _select(monkeypatch, ci_sweep=_ci_sweep_with("test-alpine"))
    assert reason == "actionable" and target.job.id == 3

    # With no CI-sweep PR open, nothing holds a fix.
    target, reason, _gh_client = _select(monkeypatch)
    assert reason == "actionable"


def test_a_weekly_failure_with_a_fix_in_the_ci_sweep_pr_is_not_fixed_again(monkeypatch) -> None:
    target, _reason, _gh_client = _select_weekly(monkeypatch, ci_sweep=_ci_sweep_with("test-ubuntu-tls"))

    # test-ubuntu-tls has its fix there; the branch's next failure is taken instead.
    assert target is not None and target.release_run and target.job.id == 72


def test_a_sweep_fix_is_a_line_on_the_sweep_pr_and_explained_on_the_ci_sweep_pr(monkeypatch) -> None:
    sweep, ci_sweep = _pr(), _pr()
    gh = MagicMock()
    gh.get_repo.return_value.get_pull.side_effect = lambda number: {4226: sweep, 61: ci_sweep}[number]
    plan = _plan(monkeypatch, _target(sweep, job=FailedJob("test-ubuntu-tls", id=27)))
    candidate = FixCandidate(action=CandidateAction.PATCH, failing_check="t", root_cause="The test races the save.")

    ci_followup.report_result(gh, plan, FixResult(
        kind=ResultKind.PUSHED, summary="s", task=plan.task, candidate=candidate, commit_sha="b" * 40, pr_number=61,
    ))

    (line,) = sweep.posted
    assert "Pushed a fix for `test-ubuntu-tls`" in line and f"to #61: {'b' * 40}." in line
    assert "races the save" not in line and " outcome=pushed -->" in line
    (explained,) = ci_sweep.posted
    assert explained.startswith(f"Pushed {'b' * 40} with a fix for `test-ubuntu-tls`")
    assert "#4226" in explained and "The test races the save." in explained



def _suggest(monkeypatch, sweep, job: str, job_id: int):
    gh = MagicMock()
    gh.get_repo.return_value.get_pull.return_value = sweep
    plan = _plan(monkeypatch, _target(sweep, job=FailedJob(job, id=job_id)))
    candidate = FixCandidate(action=CandidateAction.PORT, port_commit="c" * 40)
    ci_followup.report_result(gh, plan, FixResult(
        kind=ResultKind.SUGGESTED, summary="s", task=plan.task, candidate=candidate, pr_number=4726,
    ))


def test_an_upstream_fix_is_suggested_once_however_many_failures_it_fixes(monkeypatch) -> None:
    comments: list = []
    sweep = _pr(comments=comments)
    sweep.create_issue_comment = lambda body: comments.append(SimpleNamespace(
        user=SimpleNamespace(login="valkeyrie-ops[bot]"), body=body,
    ))

    _suggest(monkeypatch, sweep, "test-ubuntu-tls", 27)
    (comment,) = comments
    assert "#4726 fixes `test-ubuntu-tls` ([run](" in comment.body and "). Backport it to `9.0`." in comment.body
    assert "<!-- valkey-ci-agent:backport-suggestion pr=4726 -->" in comment.body
    assert " outcome=suggested reason=backport -->" in comment.body

    edits = []
    comment.edit = lambda body: edits.append(body)
    _suggest(monkeypatch, sweep, "test-alpine", 28)
    assert len(comments) == 1
    (edited,) = edits
    assert edited.startswith(comment.body) and "It also fixes `test-alpine`." in edited
    assert edited.count("reason=backport") == 2


def test_a_failure_whose_upstream_fix_was_suggested_waits_for_the_backport(monkeypatch) -> None:
    key = ci_followup._job_key(_run(), "unit tests")
    suggested = _marker(head="b" * 40, job=9, key=key, outcome="suggested", reason="backport")

    target, reason, _gh_client = _select(monkeypatch, pr=_pr(comments=(suggested,)))

    assert target is None and reason == "no-unhandled-actionable-failures"
