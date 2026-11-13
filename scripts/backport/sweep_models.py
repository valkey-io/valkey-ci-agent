"""Typed data passed through the scheduled backport sweep."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from scripts.backport import models as _models

if TYPE_CHECKING:
    from scripts.backport.ci_sweep import Plan

ProjectBackportCandidate = _models.BackportCandidate
CandidateResult = _models.CandidateResult
DETAIL_DROPPED_TARGET_MISSING_TEST_PREFIX = (
    _models.DETAIL_DROPPED_TARGET_MISSING_TEST_PREFIX
)
DETAIL_EMPTY_ON_TARGET = _models.DETAIL_EMPTY_ON_TARGET
DETAIL_PORTED_TARGET_MISSING_TEST_PREFIX = (
    _models.DETAIL_PORTED_TARGET_MISSING_TEST_PREFIX
)
DETAIL_RESOLVED_BY_AI = _models.DETAIL_RESOLVED_BY_AI


# Detail string used when a candidate PR is already cherry-picked onto the
# backport sweep branch. Reporting treats this as "on the branch", unlike
# empty cherry-picks that mean "already on the release branch".
DETAIL_ALREADY_ON_SWEEP_BRANCH = "already on backport branch"

# Detail string used when the release branch history already records the
# source PR (subject, Backport-Source-PR trailer, or a merged sweep's Applied
# table). Such candidates are skipped before any cherry-pick is attempted.
DETAIL_ALREADY_ON_TARGET = "already on release branch"

@dataclass
class BranchSweepResult:
    target_branch: str
    candidates_found: int = 0
    results: list[CandidateResult] = field(default_factory=list)
    pr_url: str = ""
    error: str = ""

    @property
    def applied_count(self) -> int:
        """Number of candidates that were cherry-picked onto the branch."""
        return sum(1 for item in self.results if item.outcome == "applied")


@dataclass
class PreparedBranchSweep:
    """Validated local branch awaiting publication with a fresh token."""

    repo_full_name: str
    push_repo: str
    target_branch: str
    backport_branch: str
    repo_dir: str
    target_head: str
    prepared_head: str
    expected_remote_head: str | None
    expected_pr_number: int | None
    result: BranchSweepResult
    backport_label: str = "backport"
    llm_conflict_label: str = "ai-resolved-conflicts"
    # How the open CI-sweep PR goes onto this head, if there is one.
    ci_sweep: Plan | None = None
