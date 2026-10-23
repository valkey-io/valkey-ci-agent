"""Shared publication capabilities and ownership policy for CI fixes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from scripts.ci_fix.models import CandidateAction, TargetKind
from scripts.common.identity import APP_LOGIN

WRITABLE_BRANCH_PREFIXES = ("backport/", "agent/backport/", "agent/ci-fix/")
BACKPORT_BRANCH_PREFIXES = ("backport/", "agent/backport/")
CiFixAgentProfile = Literal["ci_fix_edit", "ci_fix_suggest"]


@dataclass(frozen=True)
class TargetCapabilities:
    """Behavior that every consumer must agree on for one target kind."""

    agent_profile: CiFixAgentProfile
    publication_guidance: str
    allowed_actions: frozenset[CandidateAction]
    patch_limit: int

    def allows(self, action: CandidateAction) -> bool:
        return action in self.allowed_actions


TARGET_CAPABILITIES: dict[TargetKind, TargetCapabilities] = {
    TargetKind.UPDATE_PR: TargetCapabilities(
        agent_profile="ci_fix_edit",
        publication_guidance=(
            "A successful candidate will be committed to the existing "
            "bot-owned PR branch. Its normal repository CI and a human review "
            "decide whether it merges."
        ),
        allowed_actions=frozenset(
            {
                CandidateAction.PATCH,
                CandidateAction.PORT,
                CandidateAction.REFUSE,
            }
        ),
        patch_limit=1_000_000,
    ),
    TargetKind.SUGGEST: TargetCapabilities(
        agent_profile="ci_fix_suggest",
        publication_guidance=(
            "This is not a bot-owned branch. Prepare a patch for a PR comment "
            "only; nothing will be pushed."
        ),
        allowed_actions=frozenset(
            {
                CandidateAction.PATCH,
                CandidateAction.REFUSE,
            }
        ),
        patch_limit=45_000,
    ),
    TargetKind.DRAFT_PR: TargetCapabilities(
        agent_profile="ci_fix_edit",
        publication_guidance=(
            "A successful candidate will be committed to a new bot-owned "
            "branch and opened as a draft PR. Its normal repository CI and a "
            "human review decide whether it merges."
        ),
        allowed_actions=frozenset(
            {
                CandidateAction.PATCH,
                CandidateAction.PORT,
                CandidateAction.REFUSE,
            }
        ),
        patch_limit=1_000_000,
    ),
}


def target_capabilities(kind: TargetKind) -> TargetCapabilities:
    """Return the exhaustive capability record for ``kind``."""
    return TARGET_CAPABILITIES[kind]


def is_bot_author(author: str) -> bool:
    """Whether ``author`` is the App, whose namespaced PR branches the agent may update."""
    return author == f"{APP_LOGIN}[bot]"


def is_writable_branch(branch: str) -> bool:
    return branch.startswith(WRITABLE_BRANCH_PREFIXES)


def is_backport_branch(branch: str) -> bool:
    return branch.startswith(BACKPORT_BRANCH_PREFIXES)


def can_update_branch(*, same_repo: bool, branch: str, author: str) -> bool:
    """Whether a validated PR branch is eligible for an automated update."""
    return same_repo and is_writable_branch(branch) and is_bot_author(author)
