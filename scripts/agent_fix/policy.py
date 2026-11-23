"""Shared publication capabilities and ownership policy for fixes."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from scripts.agent_fix.models import CandidateAction, FixPolicy, FixTask, TargetKind
from scripts.common.identity import APP_LOGIN

# Where a fix that opens a pull request is pushed: ``issue-<issue>-<labelling>``
# for a labelled issue's change (labelled.py), ``ci-issue-<issue>-<run>`` for a
# detector issue's fix (issues.py), and ``upstream/<test>`` for a fix proposed
# to the default branch (upstream.py).
FIX_BRANCH_PREFIX = "agent/fix/"
# ``agent/ci-fix/`` is where the last two were pushed before the package was
# renamed: the pull requests opened from it are still the bot's to update.
WRITABLE_BRANCH_PREFIXES = ("backport/", "agent/backport/", FIX_BRANCH_PREFIX, "agent/ci-fix/")
# The bot's own fork of the target repository, ``owner/name``, for those
# branches: a pull request from it runs CI with an outside contributor's
# permissions. Unset, they are pushed to the target repository.
PUSH_REPO_ENV = "AGENT_FIX_PUSH_REPO"
BACKPORT_BRANCH_PREFIXES = ("backport/", "agent/backport/")
AgentFixProfile = Literal["agent_fix_edit", "agent_fix_suggest"]


@dataclass(frozen=True)
class TargetCapabilities:
    """Behavior that every consumer must agree on for one target kind."""

    agent_profile: AgentFixProfile
    publication_guidance: str
    allowed_actions: frozenset[CandidateAction]
    patch_limit: int

    def allows(self, action: CandidateAction) -> bool:
        return action in self.allowed_actions


TARGET_CAPABILITIES: dict[TargetKind, TargetCapabilities] = {
    TargetKind.UPDATE_PR: TargetCapabilities(
        agent_profile="agent_fix_edit",
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
        agent_profile="agent_fix_suggest",
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
        agent_profile="agent_fix_edit",
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


def push_repo() -> str:
    """The bot's own fork (``PUSH_REPO_ENV``), or ""."""
    return os.environ.get(PUSH_REPO_ENV, "").strip()


def allows_ci_changes(task: FixTask) -> bool:
    """Whether ``task``'s fix may change ``.github/``.

    Only on a bot-owned backport branch: a release branch often fails because
    it lacks an upstream CI change, and the bot owns the branch it pushes to.
    Everywhere else a workflow change runs with the repository's privileges
    once pushed, so a person applies it.
    """
    target = task.target
    return (
        task.policy is FixPolicy.BACKPORT
        and target.kind is TargetKind.UPDATE_PR
        and is_backport_branch(target.branch)
    )


def can_update_branch(*, same_repo: bool, branch: str, author: str) -> bool:
    """Whether a validated PR branch is eligible for an automated update."""
    return same_repo and is_writable_branch(branch) and is_bot_author(author)
