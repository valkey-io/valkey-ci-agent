from __future__ import annotations

from scripts.ci_fix.models import CandidateAction, TargetKind
from scripts.ci_fix.policy import (
    TARGET_CAPABILITIES,
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
