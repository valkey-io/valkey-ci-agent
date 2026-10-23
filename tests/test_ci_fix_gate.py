"""Authorization, run binding, and publication selection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github.GithubException import GithubException

from scripts.ci_fix import gate as gate_mod
from scripts.ci_fix import github_runs
from scripts.ci_fix.gate import (
    GateError,
    GateRejection,
    ParsedCommand,
    build_fix_task,
    is_authorized,
    parse_command,
)
from scripts.ci_fix.github_runs import FailedJob
from scripts.ci_fix.models import FixPolicy, FixTask, TargetKind

_SHA = "a" * 40
_RUN_URL = "https://github.com/valkey-io/valkey/actions/runs/27559908167"


def _patch_failed_jobs(monkeypatch, failed_jobs):
    """The gate reads a linked run's jobs itself, and the most actionable run's through github_runs."""
    monkeypatch.setattr(gate_mod, "failed_jobs_for_run", failed_jobs)
    monkeypatch.setattr(github_runs, "failed_jobs_for_run", failed_jobs)


def _membership_gh(state: str = "active") -> MagicMock:
    team = MagicMock()
    team.get_team_membership.return_value = SimpleNamespace(state=state)
    org = MagicMock()
    org.get_team_by_slug.return_value = team
    gh = MagicMock()
    gh.get_organization.return_value = org
    return gh


def _gh(
    *,
    head_sha: str = _SHA,
    run_sha: str = _SHA,
    branch: str = "agent/backport/sweep/8.0",
    run_branch: str | None = None,
    head_repo: str = "valkey-io/valkey",
    author: str = "valkeyrie-ops[bot]",
    state: str = "open",
) -> tuple[MagicMock, MagicMock]:
    gh = _membership_gh()
    pr = SimpleNamespace(
        state=state,
        user=SimpleNamespace(login=author),
        head=SimpleNamespace(
            sha=head_sha,
            ref=branch,
            repo=SimpleNamespace(full_name=head_repo),
        ),
        base=SimpleNamespace(ref="8.0"),
    )
    run = SimpleNamespace(
        id=27559908167,
        run_attempt=2,
        head_sha=run_sha,
        head_branch=run_branch if run_branch is not None else branch,
        status="completed",
        conclusion="failure",
    )
    repo = MagicMock()
    repo.get_pull.return_value = pr
    repo.get_workflow_run.return_value = run
    gh.get_repo.return_value = repo
    return gh, repo


@pytest.mark.parametrize(
    ("body", "run_id", "job_id", "run_attempt", "hint"),
    [
        (f"@valkeyrie-bot fix {_RUN_URL}", 27559908167, 0, 0, ""),
        (
            f"@valkeyrie-ops fix {_RUN_URL}/job/77 look at tcl",
            27559908167,
            77,
            0,
            "look at tcl",
        ),
        (
            f"@valkeyrie-ops fix {_RUN_URL}/attempts/2",
            27559908167,
            0,
            2,
            "",
        ),
        (f"@valkeyrie-ops fix {_RUN_URL}/job/77.", 27559908167, 77, 0, ""),
        (f"@valkeyrie-ops fix {_RUN_URL}, please", 27559908167, 0, 0, "please"),
        ("@valkeyrie-ops fix investigate valgrind", 0, 0, 0, "investigate valgrind"),
    ],
)
def test_parse_command(
    body: str,
    run_id: int,
    job_id: int,
    run_attempt: int,
    hint: str,
) -> None:
    command = parse_command(body)
    assert command is not None
    assert (
        command.run_id,
        command.job_id,
        command.run_attempt,
        command.hint,
    ) == (run_id, job_id, run_attempt, hint)


@pytest.mark.parametrize(
    "body",
    [
        "thanks",
        "@valkeyrie-ops fixed it",
        "@valkeyrie-ops fix https://example.com/actions/runs/1",
        f"quoted `@valkeyrie-ops fix {_RUN_URL}`",
        f"@valkeyrie-ops fix {_RUN_URL} {_RUN_URL}",
    ],
)
def test_parse_command_rejects_non_commands(body: str) -> None:
    assert parse_command(body) is None


def test_authorization_is_active_membership_or_explicit_allowlist(monkeypatch) -> None:
    assert is_authorized(_membership_gh("active"), "valkey-io", "contributors", "alice")
    assert not is_authorized(_membership_gh("pending"), "valkey-io", "contributors", "alice")
    missing = _membership_gh()
    missing.get_organization.return_value.get_team_by_slug.return_value.get_team_membership.side_effect = (
        GithubException(404, "Not Found", None)
    )
    assert not is_authorized(missing, "valkey-io", "contributors", "mallory", retries=1)
    monkeypatch.setenv("CI_FIX_AUTH_ALLOWLIST", "alice")
    assert is_authorized(MagicMock(), "valkey-io", "contributors", "alice")


def test_a_membership_read_error_fails_closed_unless_the_caller_asks_to_see_it() -> None:
    broken = _membership_gh()
    broken.get_organization.return_value.get_team_by_slug.side_effect = GithubException(502, "Bad Gateway", None)

    # Shared callers (the release-notes review bots) keep failing closed.
    assert not is_authorized(broken, "valkey-io", "contributors", "alice", retries=1)
    with pytest.raises(GithubException):
        is_authorized(broken, "valkey-io", "contributors", "alice", retries=1, raise_on_error=True)


def _build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gh: MagicMock | None = None,
    command: ParsedCommand | None = None,
    jobs: list[FailedJob] | None = None,
) -> FixTask | GateRejection | GateError:
    gh = gh or _gh()[0]
    monkeypatch.setattr(gate_mod, "is_authorized", lambda *args, **kwargs: True)

    def jobs_for_run(*args, **kwargs):
        assert kwargs["run_attempt"] == 2
        return (
            jobs
            if jobs is not None
            else [FailedJob("test-ubuntu", 11)]
        )

    _patch_failed_jobs(monkeypatch, jobs_for_run)
    return build_fix_task(
        gh,
        command=command or ParsedCommand("valkey-io", "valkey", 27559908167, ""),
        pr_repo_full_name="valkey-io/valkey",
        pr_number=42,
        commenter="alice",
    )


def test_an_unauthorized_commenter_is_refused_before_anything_is_read(monkeypatch) -> None:
    gh = MagicMock()
    gh.get_repo.side_effect = AssertionError("nothing is read for an unauthorized commenter")
    monkeypatch.setattr(gate_mod, "is_authorized", lambda *args, **kwargs: False)
    _patch_failed_jobs(monkeypatch, lambda *a, **k: pytest.fail("no run or job is read"))

    result = build_fix_task(
        gh,
        command=ParsedCommand("valkey-io", "valkey", 27559908167, ""),
        pr_repo_full_name="valkey-io/valkey",
        pr_number=42,
        commenter="mallory",
    )

    assert isinstance(result, GateRejection)
    assert result.reason == "@mallory is not an active member of valkey-io/contributors; refusing."


def test_bot_backport_branch_uses_backport_update_policy(monkeypatch) -> None:
    result = _build(monkeypatch)
    assert isinstance(result, FixTask)
    assert result.target.kind is TargetKind.UPDATE_PR
    assert result.target.base_repo_full_name == "valkey-io/valkey"
    assert result.target.expected_sha == _SHA
    assert result.failure.job_id == 11
    assert result.failure.run_attempt == 2
    assert result.policy is FixPolicy.BACKPORT


@pytest.mark.parametrize(
    ("head_repo", "branch", "author"),
    [
        ("someone/valkey", "feature", "alice"),
        ("valkey-io/valkey", "feature", "alice"),
        ("valkey-io/valkey", "agent/ci-fix/x", "alice"),
    ],
)
def test_contributor_or_unowned_branch_gets_suggestion(
    monkeypatch,
    head_repo: str,
    branch: str,
    author: str,
) -> None:
    gh, _repo = _gh(
        head_repo=head_repo,
        branch=branch,
        run_branch=branch,
        author=author,
    )
    result = _build(monkeypatch, gh=gh)
    assert isinstance(result, FixTask)
    assert result.target.kind is TargetKind.SUGGEST
    assert result.policy is FixPolicy.GENERAL


def test_exact_failed_job_link_is_preserved(monkeypatch) -> None:
    result = _build(
        monkeypatch,
        command=ParsedCommand("valkey-io", "valkey", 27559908167, "hint", 22),
        jobs=[
            FailedJob("build", 11),
            FailedJob("test-sanitizer", 22),
        ],
    )
    assert isinstance(result, FixTask)
    assert (result.failure.job_id, result.failure.job_name, result.failure.hint) == (
        22,
        "test-sanitizer",
        "hint",
    )


def test_stale_run_attempt_link_is_refused(monkeypatch) -> None:
    result = _build(
        monkeypatch,
        command=ParsedCommand(
            "valkey-io",
            "valkey",
            27559908167,
            "",
            run_attempt=1,
        ),
    )

    assert isinstance(result, GateRejection)
    assert "currently attempt 2" in result.reason


@pytest.mark.parametrize(
    ("gh", "reason"),
    [
        (_gh(run_sha="b" * 40)[0], "moved"),
        (_gh(run_branch="different")[0], "not the current PR branch"),
        (_gh(state="closed")[0], "not open"),
    ],
)
def test_integrity_mismatch_is_refused(monkeypatch, gh: MagicMock, reason: str) -> None:
    result = _build(monkeypatch, gh=gh)
    assert isinstance(result, GateRejection)
    assert reason in result.reason


def test_cross_repo_run_is_refused_before_lookup(monkeypatch) -> None:
    result = _build(
        monkeypatch,
        command=ParsedCommand("other", "repo", 1, ""),
    )
    assert isinstance(result, GateRejection)
    assert "belongs to other/repo" in result.reason


@pytest.mark.parametrize("failure", ["authorization", "pr", "run", "jobs", "runs"])
def test_github_read_failures_remain_retryable(monkeypatch, failure: str) -> None:
    gh, repo = _gh()
    monkeypatch.setattr(gate_mod, "is_authorized", lambda *args, **kwargs: True)
    command = ParsedCommand("valkey-io", "valkey", 27559908167, "")

    if failure == "authorization":
        monkeypatch.setattr(
            gate_mod,
            "is_authorized",
            MagicMock(side_effect=RuntimeError("temporary auth failure")),
        )
    elif failure == "pr":
        repo.get_pull.side_effect = RuntimeError("temporary PR failure")
    elif failure == "run":
        repo.get_workflow_run.side_effect = RuntimeError("temporary run failure")
    elif failure == "jobs":
        _patch_failed_jobs(monkeypatch, MagicMock(side_effect=RuntimeError("temporary jobs failure")),
        )
    else:
        command = ParsedCommand("", "", 0, "")
        repo.get_workflow_runs.side_effect = RuntimeError("temporary runs failure")

    result = build_fix_task(
        gh,
        command=command,
        pr_repo_full_name="valkey-io/valkey",
        pr_number=42,
        commenter="alice",
        retries=1,
    )

    assert isinstance(result, GateError)
    assert "temporary" in result.reason


def test_without_link_selects_most_actionable_current_head_failure(monkeypatch) -> None:
    gh, repo = _gh()
    repo.get_workflow_runs.return_value = [
        SimpleNamespace(
            id=1,
            head_sha=_SHA,
            head_branch="agent/backport/sweep/8.0",
            status="completed",
            conclusion="failure",
            run_attempt=1,
        ),
        SimpleNamespace(
            id=2,
            head_sha=_SHA,
            head_branch="agent/backport/sweep/8.0",
            status="completed",
            conclusion="failure",
            run_attempt=1,
        ),
    ]
    jobs = {
        1: [FailedJob("test-sanitizer", 10)],
        2: [FailedJob("build-linux", 20)],
    }
    monkeypatch.setattr(gate_mod, "is_authorized", lambda *args, **kwargs: True)
    _patch_failed_jobs(monkeypatch, lambda _gh, _repo, run_id, **kwargs: jobs[run_id])

    result = build_fix_task(
        gh,
        command=ParsedCommand("", "", 0, ""),
        pr_repo_full_name="valkey-io/valkey",
        pr_number=42,
        commenter="alice",
    )

    assert isinstance(result, FixTask)
    assert (result.failure.run_id, result.failure.job_name) == (2, "build-linux")


def test_without_link_ignores_failure_superseded_by_success(monkeypatch) -> None:
    gh, repo = _gh()
    repo.get_workflow_runs.return_value = [
        SimpleNamespace(
            id=10,
            workflow_id=100,
            head_sha=_SHA,
            head_branch="agent/backport/sweep/8.0",
            status="completed",
            conclusion="failure",
            run_attempt=1,
        ),
        SimpleNamespace(
            id=11,
            workflow_id=100,
            head_sha=_SHA,
            head_branch="agent/backport/sweep/8.0",
            status="completed",
            conclusion="success",
            run_attempt=1,
        ),
        SimpleNamespace(
            id=12,
            workflow_id=200,
            head_sha=_SHA,
            head_branch="agent/backport/sweep/8.0",
            status="completed",
            conclusion="failure",
            run_attempt=3,
        ),
    ]
    jobs_for_run = MagicMock(
        return_value=[FailedJob("build-linux", 20)],
    )
    monkeypatch.setattr(gate_mod, "is_authorized", lambda *args, **kwargs: True)
    _patch_failed_jobs(monkeypatch, jobs_for_run)

    result = build_fix_task(
        gh,
        command=ParsedCommand("", "", 0, ""),
        pr_repo_full_name="valkey-io/valkey",
        pr_number=42,
        commenter="alice",
    )

    assert isinstance(result, FixTask)
    assert (result.failure.run_id, result.failure.run_attempt) == (12, 3)
    jobs_for_run.assert_called_once_with(
        gh,
        "valkey-io/valkey",
        12,
        run_attempt=3,
    )


def test_without_link_scopes_newest_run_selection_to_pr_branch(monkeypatch) -> None:
    gh, repo = _gh()
    repo.get_workflow_runs.return_value = [
        SimpleNamespace(
            id=20,
            workflow_id=100,
            head_sha=_SHA,
            head_branch="agent/backport/sweep/8.0",
            status="completed",
            conclusion="failure",
            run_attempt=1,
        ),
        SimpleNamespace(
            id=21,
            workflow_id=100,
            head_sha=_SHA,
            head_branch="another-branch",
            status="completed",
            conclusion="success",
            run_attempt=1,
        ),
    ]
    jobs_for_run = MagicMock(
        return_value=[FailedJob("build-linux", 20)],
    )
    monkeypatch.setattr(gate_mod, "is_authorized", lambda *args, **kwargs: True)
    _patch_failed_jobs(monkeypatch, jobs_for_run)

    result = build_fix_task(
        gh,
        command=ParsedCommand("", "", 0, ""),
        pr_repo_full_name="valkey-io/valkey",
        pr_number=42,
        commenter="alice",
    )

    assert isinstance(result, FixTask)
    assert result.failure.run_id == 20
    repo.get_workflow_runs.assert_called_once_with(
        head_sha=_SHA,
        branch="agent/backport/sweep/8.0",
    )
    jobs_for_run.assert_called_once_with(
        gh,
        "valkey-io/valkey",
        20,
        run_attempt=1,
    )
