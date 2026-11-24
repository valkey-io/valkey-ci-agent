"""Data models for the Backport Agent pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ResolutionSource = Literal["llm", "automatic"]
CandidateOutcome = Literal[
    "applied",
    "skipped-existing",
    "skipped-conflict",
    "skipped-validation-failed",
    "error",
]
BackportOutcome = Literal[
    "success",
    "conflicts-unresolved",
    "duplicate",
    "branch-missing",
    "pr-not-merged",
    "already-applied",
    "error",
]

DETAIL_RESOLVED_BY_AI = "conflicts resolved by Claude Code"
DETAIL_DROPPED_TARGET_MISSING_TEST_PREFIX = "dropped target-missing test file(s):"
DETAIL_PORTED_TARGET_MISSING_TEST_PREFIX = "ported target-missing test coverage to:"
DETAIL_EMPTY_ON_TARGET = "resolution was already satisfied on target branch"


@dataclass
class ConflictedFile:
    """A file with merge conflict markers after cherry-pick."""

    path: str
    target_branch_content: str
    source_branch_content: str


@dataclass
class ResolutionResult:
    """Outcome of conflict resolution for a single file."""

    path: str
    resolved_content: str | None  # None = resolution failed
    resolution_summary: str
    source: ResolutionSource = "llm"
    resolution_diff: str | None = None
    reviewer_diff: str | None = None
    llm_summary: str | None = None


@dataclass
class CherryPickResult:
    """Outcome of the cherry-pick operation."""

    success: bool  # True if no conflicts
    conflicting_files: list[ConflictedFile] = field(default_factory=list)
    applied_commits: list[str] = field(default_factory=list)
    conflicting_commit_sha: str | None = None


@dataclass
class BackportPRContext:
    """Context about the source PR needed throughout the pipeline."""

    source_pr_number: int
    source_pr_title: str
    source_pr_url: str
    source_pr_diff: str
    target_branch: str
    commits: list[str]


@dataclass(frozen=True)
class BackportCandidate:
    """A merged source pull request selected for one target branch."""

    source_pr_number: int
    source_pr_title: str
    source_pr_url: str
    target_branch: str
    merge_commit_sha: str | None = None
    commit_shas: list[str] = field(default_factory=list)
    merged_at: str = ""
    source_pr_diff: str = ""
    source_commits_complete: bool = True

    def to_pr_context(self) -> BackportPRContext:
        """Return the resolver and PR-publication view of this candidate."""

        return BackportPRContext(
            source_pr_number=self.source_pr_number,
            source_pr_title=self.source_pr_title,
            source_pr_url=self.source_pr_url,
            source_pr_diff=self.source_pr_diff,
            target_branch=self.target_branch,
            commits=list(self.commit_shas),
        )


@dataclass
class CandidateResult:
    """Result of applying one source pull request to a local branch."""

    source_pr_number: int
    source_pr_title: str
    outcome: CandidateOutcome
    detail: str = ""
    resolutions: list[ResolutionResult] = field(default_factory=list)
    resolved_by_ai: bool = False
    skip_reason: str = ""
    resolved_commit_sha: str | None = None
    applied_commits: list[str] = field(default_factory=list)
    conflicting_files: list[ConflictedFile] = field(default_factory=list)
    conflicting_commit_sha: str | None = None
    ai_summary: str = ""
    worktree_restored: bool = True


@dataclass
class BackportResult:
    """Final outcome of a backport run."""

    outcome: BackportOutcome
    backport_pr_url: str | None = None
    commits_cherry_picked: int = 0
    files_conflicted: int = 0
    files_resolved: int = 0
    files_unresolved: int = 0
    error_message: str | None = None


@dataclass
class BackportConfig:
    """Configuration for the backport agent, derived from the registry."""

    backport_label: str = "backport"
    llm_conflict_label: str = "ai-resolved-conflicts"
    benchmark_label: str = ""
    max_conflicting_files: int = 100
