"""Data models for the Backport Agent pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ResolutionSource = Literal["llm", "automatic"]
BackportOutcome = Literal[
    "success",
    "conflicts-unresolved",
    "duplicate",
    "branch-missing",
    "pr-not-merged",
    "already-applied",
    "error",
]


@dataclass
class ConflictedFile:
    """A file with merge conflict markers after cherry-pick."""

    path: str
    target_branch_content: str
    source_branch_content: str


@dataclass
class ResolutionResult:
    """Outcome of LLM conflict resolution for a single file."""

    path: str
    resolved_content: str | None  # None = resolution failed
    resolution_summary: str
    source: ResolutionSource = "llm"


@dataclass
class CherryPickResult:
    """Outcome of the cherry-pick operation."""

    success: bool  # True if no conflicts
    conflicting_files: list[ConflictedFile] = field(default_factory=list)
    applied_commits: list[str] = field(default_factory=list)


@dataclass
class BackportPRContext:
    """Context about the source PR needed throughout the pipeline."""

    source_pr_number: int
    source_pr_title: str
    source_pr_url: str
    source_pr_diff: str
    target_branch: str
    commits: list[str]


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
    max_conflicting_files: int = 100
