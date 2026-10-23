from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github.GithubException import GithubException

from scripts.ci_fix import publish as publish_mod
from scripts.ci_fix.models import (
    CandidateAction,
    Failure,
    FixCandidate,
    FixPolicy,
    FixTarget,
    FixTask,
    IssuePublication,
    ResultKind,
    TargetKind,
)
from scripts.ci_fix.publish import publish_candidate
from scripts.ci_fix.push import PushFailed, PushRefused, PushStale
from scripts.common.identity import APP_LOGIN

_HEAD = "a" * 40
_NEW = "b" * 40


def _candidate(action: CandidateAction = CandidateAction.PATCH) -> FixCandidate:
    return FixCandidate(
        action=action,
        failing_check="unit/x",
        root_cause="The test waits on a fixed sleep.",
        reasoning="Wait for the condition.",
        patch="diff --git a/x b/x\n",
        changed_paths=("x",),
        port_commit="c" * 40 if action is CandidateAction.PORT else "",
    )


def _task(kind: TargetKind, *, issue: int = 0) -> FixTask:
    failure = Failure(
        repo_full_name="valkey-io/valkey",
        run_id=100,
        head_sha=_HEAD,
        job_id=55,
        job_name="unit tests",
        base_branch="unstable" if issue else "9.0",
        pr_number=0 if issue else 7,
        issue_number=issue,
    )
    return FixTask(
        failure=failure,
        target=FixTarget(
            kind=kind,
            repo_full_name="valkey-io/valkey",
            base_repo_full_name="valkey-io/valkey",
            branch=(
                f"agent/ci-fix/issue-{issue}-100-1"
                if issue
                else "agent/backport/sweep/9.0"
            ),
            expected_sha=_HEAD,
        ),
        policy=FixPolicy.GENERAL if issue else FixPolicy.BACKPORT,
    )


def _pr(
    *,
    author: str | None = None,
    head_sha: str = _HEAD,
    head_repo: str = "valkey-io/valkey",
) -> SimpleNamespace:
    return SimpleNamespace(
        number=7,
        state="open",
        user=SimpleNamespace(login=author or f"{APP_LOGIN}[bot]"),
        base=SimpleNamespace(ref="9.0"),
        head=SimpleNamespace(
            sha=head_sha,
            ref="agent/backport/sweep/9.0",
            repo=SimpleNamespace(full_name=head_repo),
        ),
    )


def _draft_pr(*, head_sha: str = _NEW) -> SimpleNamespace:
    return SimpleNamespace(
        number=44,
        state="open",
        html_url="https://example/pr/44",
        base=SimpleNamespace(ref="unstable"),
        head=SimpleNamespace(
            sha=head_sha,
            ref="agent/ci-fix/issue-9-100-1",
            repo=SimpleNamespace(full_name="valkey-io/valkey"),
        ),
    )


def _issue(*, body: str | None = None, state: str = "open", assignees=()) -> SimpleNamespace:
    return SimpleNamespace(
        number=9,
        state=state,
        body=body or "<!-- valkey-ci-agent:test-failure:last-key:100 -->",
        assignees=list(assignees),
        get_timeline=lambda: [],
    )


def _gh(pr=None, issue=None):
    repo = MagicMock()
    repo.get_pull.return_value = pr or _pr()
    repo.get_issue.return_value = issue or _issue()

    def get_branch(name: str):
        if name in {"9.0", "unstable"}:
            return SimpleNamespace(commit=SimpleNamespace(sha=_HEAD))
        raise GithubException(404, {"message": "Not Found"})

    repo.get_branch.side_effect = get_branch
    gh = MagicMock()
    gh.get_repo.return_value = repo
    return gh, repo


def test_contributor_target_returns_the_complete_patch_without_pushing() -> None:
    task = _task(TargetKind.SUGGEST)
    gh, _repo = _gh()

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.SUGGESTED
    assert result.candidate is not None and result.candidate.patch.startswith("diff --git")


def test_suggestion_is_discarded_if_the_pr_head_moved() -> None:
    task = _task(TargetKind.SUGGEST)
    gh, _repo = _gh(pr=_pr(head_sha="f" * 40))

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.REFUSED
    assert "head moved" in result.summary


def test_direct_port_suggestion_is_refused_by_target_capabilities() -> None:
    task = _task(TargetKind.SUGGEST)
    gh, _repo = _gh()

    result = publish_candidate(
        gh,
        task,
        _candidate(CandidateAction.PORT),
        git_env={},
    )

    assert result.kind is ResultKind.REFUSED
    assert "port action is not supported" in result.summary


def test_bot_pr_is_revalidated_immediately_before_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.UPDATE_PR)
    gh, _repo = _gh()
    seen = {}

    def push(candidate, target, *, git_env, pre_push_check):
        seen.update(candidate=candidate, target=target, git_env=git_env)
        assert pre_push_check() == ""
        return _NEW

    monkeypatch.setattr(publish_mod, "push_candidate", push)
    result = publish_candidate(gh, task, _candidate(), git_env={"AUTH": "yes"})

    assert result.kind is ResultKind.PUSHED
    assert result.commit_sha == _NEW
    assert seen["target"] is task.target


def test_non_bot_pr_is_never_updated(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(TargetKind.UPDATE_PR)
    gh, _repo = _gh(pr=_pr(author="alice"))
    def pushed(*_args, pre_push_check, **_kwargs):
        reason = pre_push_check()
        if reason:
            raise PushRefused(reason)
        return _NEW

    monkeypatch.setattr(publish_mod, "push_candidate", pushed)

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.REFUSED


def test_bot_named_branch_in_a_fork_is_never_updated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.UPDATE_PR)
    task = replace(
        task,
        target=replace(
            task.target,
            repo_full_name="alice/valkey",
            base_repo_full_name="valkey-io/valkey",
        ),
    )
    gh, _repo = _gh(pr=_pr(head_repo="alice/valkey"))

    def pushed(*_args, pre_push_check, **_kwargs):
        reason = pre_push_check()
        if reason:
            raise PushRefused(reason)
        return _NEW

    monkeypatch.setattr(publish_mod, "push_candidate", pushed)

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.REFUSED
    assert "not owned by the CI agent" in result.summary


def test_an_issue_fix_published_as_a_comment_pushes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, _repo = _gh()
    monkeypatch.setattr(publish_mod, "push_candidate", lambda *_a, **_k: pytest.fail("nothing is pushed"))
    monkeypatch.setattr(publish_mod, "find_existing_pr", lambda *_a, **_k: pytest.fail("no PR is looked for"))

    result = publish_candidate(gh, task, _candidate(), git_env={}, issue_publication=IssuePublication.COMMENT)

    assert (result.kind, result.commit_sha, result.pr_number) == (ResultKind.SUGGESTED, "", 0)
    assert result.candidate == _candidate()


@pytest.mark.parametrize(("issue", "why"), [
    (_issue(state="closed"), "no longer open"),
    (_issue(assignees=[SimpleNamespace(login="alice")]), "now assigned"),
    (_issue(body="<!-- valkey-ci-agent:test-failure:last-key:101 -->"), "now points at run 101"),
])
def test_a_comment_is_not_posted_on_an_issue_that_changed_during_the_fix(issue, why) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, _repo = _gh(issue=issue)

    result = publish_candidate(gh, task, _candidate(), git_env={}, issue_publication=IssuePublication.COMMENT)

    assert result.kind is ResultKind.REFUSED and why in result.summary


def test_a_patch_too_large_for_a_comment_is_refused_and_says_so() -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, _repo = _gh()
    big = replace(_candidate(), patch="+" * 50_000)

    result = publish_candidate(gh, task, big, git_env={}, issue_publication=IssuePublication.COMMENT)

    assert result.kind is ResultKind.REFUSED
    assert "50,000-character patch" in result.summary and "CI_FIX_ISSUES_PUBLISH=draft-pr" in result.summary


def test_ready_issue_fix_opens_a_draft_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, _repo = _gh()
    monkeypatch.setattr(
        publish_mod,
        "push_candidate",
        lambda *_a, pre_push_check, **_k: (
            _NEW if pre_push_check() == "" else pytest.fail("issue should be valid")
        ),
    )
    monkeypatch.setattr(publish_mod, "find_existing_pr", lambda *_a, **_k: None)
    created = SimpleNamespace(number=44, html_url="https://github.com/valkey-io/valkey/pull/44")
    create = MagicMock(return_value=created)
    monkeypatch.setattr(publish_mod, "create_pull_from_push_repo", create)

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.OPENED
    assert (result.pr_number, result.commit_sha) == (44, _NEW)
    assert create.call_args.kwargs["draft"] is True
    assert create.call_args.kwargs["base_branch"] == "unstable"
    assert "Fixes #9" in create.call_args.kwargs["body"]
    assert "This PR's CI and a maintainer's review decide." in create.call_args.kwargs["body"]


def test_existing_issue_fix_pr_is_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, _repo = _gh()
    push = MagicMock()
    monkeypatch.setattr(publish_mod, "push_candidate", push)
    existing = _draft_pr()
    monkeypatch.setattr(publish_mod, "find_existing_pr", lambda *_a, **_k: existing)
    create = MagicMock()
    monkeypatch.setattr(publish_mod, "create_pull_from_push_repo", create)

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.OPENED
    assert result.pr_number == 44
    push.assert_not_called()
    create.assert_not_called()


def test_existing_occurrence_branch_is_turned_into_the_missing_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, repo = _gh()

    def get_branch(name: str):
        sha = _HEAD if name == "unstable" else _NEW
        return SimpleNamespace(commit=SimpleNamespace(sha=sha))

    repo.get_branch.side_effect = get_branch
    monkeypatch.setattr(publish_mod, "find_existing_pr", lambda *_a, **_k: None)
    push = MagicMock()
    monkeypatch.setattr(publish_mod, "push_candidate", push)
    created = SimpleNamespace(number=44, html_url="https://example/pr/44")
    create = MagicMock(return_value=created)
    monkeypatch.setattr(publish_mod, "create_pull_from_push_repo", create)

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.OPENED
    assert result.commit_sha == _NEW
    push.assert_not_called()
    assert "An earlier attempt prepared this branch" in create.call_args.kwargs["body"]


def test_existing_occurrence_branch_can_be_recovered_after_source_moves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, repo = _gh()

    def get_branch(name: str):
        sha = "f" * 40 if name == "unstable" else _NEW
        return SimpleNamespace(commit=SimpleNamespace(sha=sha))

    repo.get_branch.side_effect = get_branch
    monkeypatch.setattr(publish_mod, "find_existing_pr", lambda *_a, **_k: None)
    monkeypatch.setattr(publish_mod, "push_candidate", MagicMock())
    create = MagicMock(return_value=_draft_pr())
    monkeypatch.setattr(publish_mod, "create_pull_from_push_repo", create)

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.OPENED
    assert result.commit_sha == _NEW
    assert "An earlier attempt prepared this branch" in create.call_args.kwargs["body"]


def test_create_lease_race_reconciles_the_occurrence_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, repo = _gh()
    branch_exists = False

    def get_branch(name: str):
        if name == "unstable":
            return SimpleNamespace(commit=SimpleNamespace(sha=_HEAD))
        if branch_exists:
            return SimpleNamespace(commit=SimpleNamespace(sha=_NEW))
        raise GithubException(404, {"message": "Not Found"})

    repo.get_branch.side_effect = get_branch
    monkeypatch.setattr(publish_mod, "find_existing_pr", lambda *_a, **_k: None)

    def stale_push(*_args, **_kwargs):
        nonlocal branch_exists
        branch_exists = True
        raise PushStale("target branch changed")

    push = MagicMock(side_effect=stale_push)
    monkeypatch.setattr(publish_mod, "push_candidate", push)
    create = MagicMock(return_value=_draft_pr())
    monkeypatch.setattr(publish_mod, "create_pull_from_push_repo", create)

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.OPENED
    assert result.commit_sha == _NEW
    assert push.call_count == 1
    assert "An earlier attempt prepared this branch" in create.call_args.kwargs["body"]


def test_retry_finishes_pr_creation_without_pushing_a_duplicate_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, repo = _gh()
    branch_exists = False

    def get_branch(name: str):
        if name == "unstable":
            return SimpleNamespace(commit=SimpleNamespace(sha=_HEAD))
        if branch_exists:
            return SimpleNamespace(commit=SimpleNamespace(sha=_NEW))
        raise GithubException(404, {"message": "Not Found"})

    repo.get_branch.side_effect = get_branch
    monkeypatch.setattr(publish_mod, "find_existing_pr", lambda *_a, **_k: None)

    push = MagicMock()

    def pushed(*_args, **_kwargs):
        nonlocal branch_exists
        branch_exists = True
        return _NEW

    push.side_effect = pushed
    monkeypatch.setattr(publish_mod, "push_candidate", push)
    created = SimpleNamespace(number=44, html_url="https://example/pr/44")
    create = MagicMock(side_effect=[RuntimeError("GitHub unavailable"), created])
    monkeypatch.setattr(publish_mod, "create_pull_from_push_repo", create)

    first = publish_candidate(gh, task, _candidate(), git_env={})
    second = publish_candidate(gh, task, _candidate(), git_env={})

    assert first.kind is ResultKind.FAILED
    assert "branch" in first.summary
    assert second.kind is ResultKind.OPENED
    assert (second.pr_number, second.commit_sha) == (44, _NEW)
    assert push.call_count == 1


def test_wrong_base_existing_occurrence_pr_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, _repo = _gh()
    existing = _draft_pr()
    existing.base.ref = "9.0"
    monkeypatch.setattr(publish_mod, "find_existing_pr", lambda *_a, **_k: existing)
    push = MagicMock()
    monkeypatch.setattr(publish_mod, "push_candidate", push)

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.REFUSED
    assert "does not target unstable" in result.summary
    push.assert_not_called()


@pytest.mark.parametrize(
    "issue, expected",
    [
        (_issue(state="closed"), "no longer open"),
        (_issue(assignees=[SimpleNamespace(login="alice")]), "now assigned"),
        (
            _issue(body="<!-- valkey-ci-agent:test-failure:last-key:101 -->"),
            "now points at run 101",
        ),
        (_issue(body="detector marker removed"), "no longer contains"),
    ],
)
def test_issue_is_revalidated_before_any_push(
    monkeypatch: pytest.MonkeyPatch,
    issue: SimpleNamespace,
    expected: str,
) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, _repo = _gh(issue=issue)
    pushed = MagicMock()
    monkeypatch.setattr(publish_mod, "push_candidate", pushed)

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.REFUSED
    assert expected in result.summary
    pushed.assert_not_called()


def test_a_draft_is_opened_even_if_the_base_branch_moved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh, repo = _gh()

    def get_branch(name: str):
        if name == "unstable":
            return SimpleNamespace(commit=SimpleNamespace(sha="f" * 40))
        raise GithubException(404, {"message": "Not Found"})

    repo.get_branch.side_effect = get_branch
    monkeypatch.setattr(publish_mod, "find_existing_pr", lambda *_a, **_k: None)
    pushed = MagicMock(side_effect=PushFailed("stop after the checks"))
    monkeypatch.setattr(publish_mod, "push_candidate", pushed)

    publish_candidate(gh, task, _candidate(), git_env={})

    pushed.assert_called_once()


def test_permanent_push_refusal_is_a_refused_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.UPDATE_PR)
    gh, _repo = _gh()
    monkeypatch.setattr(
        publish_mod,
        "push_candidate",
        MagicMock(side_effect=PushRefused("lease failed")),
    )

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.REFUSED
    assert result.summary == "lease failed"


def test_operational_push_failure_is_a_failed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task(TargetKind.UPDATE_PR)
    gh, _repo = _gh()
    monkeypatch.setattr(
        publish_mod,
        "push_candidate",
        MagicMock(side_effect=PushFailed("network unavailable")),
    )

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.FAILED
    assert result.summary == "network unavailable"


def test_pr_revalidation_api_failure_is_a_failed_result() -> None:
    task = _task(TargetKind.SUGGEST)
    gh = MagicMock()
    gh.get_repo.side_effect = RuntimeError("rate limited")

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.FAILED
    assert "could not be revalidated" in result.summary


def test_issue_revalidation_api_failure_is_a_failed_result() -> None:
    task = _task(TargetKind.DRAFT_PR, issue=9)
    gh = MagicMock()
    gh.get_repo.side_effect = RuntimeError("rate limited")

    result = publish_candidate(gh, task, _candidate(), git_env={})

    assert result.kind is ResultKind.FAILED
    assert "could not be revalidated" in result.summary


def test_a_daily_draft_closes_its_issue():
    from scripts.ci_fix.models import Failure, FixPolicy, FixTarget, FixTask, TargetKind
    from scripts.common.valkey_ci import Source

    failure = Failure("o/r", 1, "a" * 40, 2, "job", base_branch="unstable", issue_number=9, source=Source.DAILY)
    target = FixTarget(TargetKind.DRAFT_PR, "o/r", branch="agent/ci-fix/x")
    body = publish_mod._draft_body(FixTask(failure, target, FixPolicy.GENERAL), None)
    assert body.startswith("Fixes #9\n\n") and "[See the run](https://github.com/o/r/actions/runs/1)" in body


def test_a_draft_closes_only_the_extra_issues_whose_tests_were_verified():
    from dataclasses import replace

    from scripts.ci_fix.models import Failure, FixPolicy, FixTarget, FixTask, TargetKind
    from scripts.common.valkey_ci import Source

    failure = Failure("o/r", 1, "a" * 40, 2, "job", base_branch="unstable", issue_number=9, source=Source.DAILY)
    target = FixTarget(TargetKind.DRAFT_PR, "o/r", branch="agent/ci-fix/x")
    candidate = replace(_candidate(), also_fixes=(10, 11), also_touches=(12,))

    body = publish_mod._draft_body(FixTask(failure, target, FixPolicy.GENERAL), candidate)

    assert "Fixes #9 Fixes #10 Fixes #11" in body
    assert "Fixes #12" not in body
    assert "It also changes the test of issue `#12`, without showing that it fixes it." in body
    # In code, so GitHub does not link it: a linked open PR would keep #12 from its own fix.
    assert "#12" not in body.replace("`#12`", "")
