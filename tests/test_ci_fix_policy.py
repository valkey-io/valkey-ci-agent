from __future__ import annotations

import pytest

from scripts.ci_fix.models import CandidateAction, Failure, FixPolicy, FixTarget, FixTask, TargetKind
from scripts.ci_fix.policy import (
    TARGET_CAPABILITIES,
    allows_ci_changes,
    can_update_branch,
    is_backport_branch,
    target_capabilities,
)
from scripts.common.identity import APP_LOGIN


def test_every_target_kind_has_capabilities() -> None:
    assert set(TARGET_CAPABILITIES) == set(TargetKind)


def test_target_capabilities_capture_current_publication_modes() -> None:
    assert not target_capabilities(TargetKind.SUGGEST).allows(
        CandidateAction.PORT
    )
    assert target_capabilities(TargetKind.UPDATE_PR).allows(
        CandidateAction.PORT
    )


def test_bot_branch_ownership_and_backport_policy() -> None:

    assert can_update_branch(
        same_repo=True,
        branch="agent/ci-fix/issue-1",
        author=f"{APP_LOGIN}[bot]",
    )
    assert not can_update_branch(
        same_repo=False,
        branch="agent/ci-fix/issue-1",
        author=f"{APP_LOGIN}[bot]",
    )
    assert is_backport_branch("agent/backport/sweep/8.0")
    assert not is_backport_branch("agent/ci-fix/issue-1")


@pytest.mark.parametrize(("policy", "kind", "branch", "allowed"), [
    (FixPolicy.BACKPORT, TargetKind.UPDATE_PR, "agent/backport/sweep/9.0", True),
    (FixPolicy.BACKPORT, TargetKind.UPDATE_PR, "backport/1234-to-9.0", True),
    # A CI-fix branch, a draft PR for an issue, and a contributor's PR never take one.
    (FixPolicy.GENERAL, TargetKind.UPDATE_PR, "agent/ci-fix/x", False),
    (FixPolicy.BACKPORT, TargetKind.UPDATE_PR, "agent/ci-fix/x", False),
    (FixPolicy.GENERAL, TargetKind.DRAFT_PR, "agent/ci-fix/issue-1-2", False),
    (FixPolicy.BACKPORT, TargetKind.SUGGEST, "agent/backport/sweep/9.0", False),
])
def test_only_a_bot_owned_backport_branch_takes_a_ci_change(
    policy: FixPolicy, kind: TargetKind, branch: str, allowed: bool,
) -> None:
    task = FixTask(
        Failure(repo_full_name="valkey-io/valkey", run_id=1, head_sha="b" * 40, job_id=2, job_name="j"),
        FixTarget(kind=kind, repo_full_name="valkey-io/valkey", branch=branch, expected_sha="b" * 40),
        policy,
    )
    assert allows_ci_changes(task) is allowed


def test_a_labelled_issues_branch_is_writable_and_may_go_to_the_bots_fork(monkeypatch):
    from scripts.ci_fix import policy

    assert policy.is_writable_branch("agent/fix/issue-7-1")
    monkeypatch.delenv(policy.PUSH_REPO_ENV, raising=False)
    assert policy.push_repo() == ""
    monkeypatch.setenv(policy.PUSH_REPO_ENV, " valkeyrie/valkey ")
    assert policy.push_repo() == "valkeyrie/valkey"
