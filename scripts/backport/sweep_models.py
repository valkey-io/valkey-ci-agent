"""Typed data passed through the scheduled backport sweep."""

from __future__ import annotations

from dataclasses import dataclass, field

from scripts.backport.models import ResolutionResult


@dataclass(frozen=True)
class ProjectBackportCandidate:
    source_pr_number: int
    source_pr_title: str
    source_pr_url: str
    target_branch: str
    merge_commit_sha: str | None = None
    commit_shas: list[str] = field(default_factory=list)
    merged_at: str = ""


@dataclass
class CandidateResult:
    source_pr_number: int
    source_pr_title: str
    # One of: applied, skipped-existing, skipped-conflict,
    # skipped-validation-failed, error.
    outcome: str
    detail: str = ""
    # Per-file AI resolutions produced for this candidate (empty when the
    # cherry-pick applied cleanly). Used to post diff comments on the sweep PR.
    resolutions: list[ResolutionResult] = field(default_factory=list)
    # Durable signal that this candidate's conflicts were resolved by the AI.
    # Unlike `detail`, this survives the sweep-PR-body round-trip so later
    # sweeps don't lose the "resolved by Claude" record once the candidate is
    # already on the branch and no longer re-resolved.
    resolved_by_ai: bool = False
    # Human-facing reason a candidate was skipped, derived deterministically
    # from the resolution outcome (e.g. the resolved content matched the target
    # branch, so the cherry-pick added nothing). Surfaced in the Skipped table.
    skip_reason: str = ""
    # SHA of the resolution commit created on the sweep branch by
    # `cherry-pick --continue`. Lets diff comments link each resolved file to
    # its native diff in the commit view instead of inlining it.
    resolved_commit_sha: str | None = None


# Detail string used when a candidate PR is already cherry-picked onto the
# backport sweep branch. Reporting treats this as "on the branch", unlike
# empty cherry-picks that mean "already on the release branch".
DETAIL_ALREADY_ON_SWEEP_BRANCH = "already on backport branch"

# Stable detail recorded for a candidate whose conflicts were resolved by the
# AI. Kept as a constant so the sweep-PR-body round-trip can recognize and
# preserve the signal across runs.
DETAIL_RESOLVED_BY_AI = "conflicts resolved by Claude Code"

# Detail recorded for a candidate whose cherry-pick (or post-resolution result)
# contributes no net change to the target branch, e.g. the fix targets code
# that does not exist on this release branch. Reporting surfaces these as
# intentionally skipped rather than dropping them silently.
DETAIL_EMPTY_ON_TARGET = "resolution was already satisfied on target branch"


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
