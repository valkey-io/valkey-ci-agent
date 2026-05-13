"""Deterministic risk assessment for generated backport PRs."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal

from scripts.backport.models import BackportPRContext, ResolutionResult

RiskLevel = Literal["low", "medium", "high"]

# Major version of the current development line. Any target branch with a
# major < _CURRENT_DEV_MAJOR is treated as an older release branch for
# backport-risk scoring. Bump this when Valkey rolls forward a major.
_CURRENT_DEV_MAJOR = 10

_DIFF_PATH_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)
_HIGH_RISK_PREFIXES = (
    "src/cluster",
    "src/replication",
    "src/rdb",
    "src/aof",
    "src/networking",
    "src/module",
    "src/server.c",
    "src/t_",
)
_HIGH_RISK_PATH_PARTS = {"cluster", "sentinel", "replication", "module"}
_MEDIUM_RISK_PREFIXES = (
    ".github/",
    "deps/",
    "src/",
    "tests/cluster/",
    "tests/sentinel/",
)
_LOW_RISK_PREFIXES = ("docs/", "tests/", "utils/")


@dataclass(frozen=True)
class BackportRiskAssessment:
    """Maintainer-facing risk metadata for one generated backport."""

    level: RiskLevel
    reasons: list[str] = field(default_factory=list)
    touched_paths: list[str] = field(default_factory=list)
    conflicted_files: int = 0
    auto_resolved_files: int = 0
    unresolved_files: int = 0


def assess_backport_risk(
    context: BackportPRContext,
    *,
    had_conflicts: bool,
    resolution_results: list[ResolutionResult] | None,
) -> BackportRiskAssessment:
    """Classify backport risk from paths, branch age, and conflict outcome."""
    paths = _changed_paths_from_diff(context.source_pr_diff)
    results = resolution_results or []
    resolved_count = sum(result.resolved_content is not None for result in results)
    unresolved_count = len(results) - resolved_count

    score = 0
    reasons: list[str] = []
    if had_conflicts:
        score += 2
        reasons.append("cherry-pick required conflict resolution")
    if resolved_count:
        score += 1
        reasons.append(f"{resolved_count} file(s) were auto-resolved")
    if unresolved_count:
        score += 3
        reasons.append(f"{unresolved_count} file(s) still need manual resolution")

    if _is_older_release_branch(context.target_branch):
        score += 1
        reasons.append(f"target branch `{context.target_branch}` is an older release line")

    high_risk_paths = [path for path in paths if _is_high_risk_path(path)]
    if high_risk_paths:
        score += 2
        reasons.append("touches cluster, replication, persistence, module, or server core code")
    elif any(path.startswith(_MEDIUM_RISK_PREFIXES) for path in paths):
        score += 1
        reasons.append("touches source, dependency, CI, or integration-test code")

    if paths and all(path.startswith(_LOW_RISK_PREFIXES) for path in paths):
        score = max(0, score - 1)
        reasons.append("changes are limited to docs/tests/utilities")

    if score >= 3:
        level: RiskLevel = "high"
    elif score >= 1:
        level = "medium"
    else:
        level = "low"

    if not reasons:
        reasons.append("clean cherry-pick with no high-risk path signals")

    return BackportRiskAssessment(
        level=level,
        reasons=reasons,
        touched_paths=paths,
        conflicted_files=len(results) if had_conflicts else 0,
        auto_resolved_files=resolved_count,
        unresolved_files=unresolved_count,
    )


def _changed_paths_from_diff(diff_text: str) -> list[str]:
    seen: set[str] = set()
    paths: list[str] = []
    for match in _DIFF_PATH_RE.finditer(diff_text or ""):
        path = match.group(2)
        if path == "/dev/null":
            path = match.group(1)
        if path and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def _is_high_risk_path(path: str) -> bool:
    if path.startswith(_HIGH_RISK_PREFIXES):
        return True
    parts = {part.lower() for part in PurePosixPath(path).parts}
    return bool(parts & _HIGH_RISK_PATH_PARTS)


def _is_older_release_branch(target_branch: str) -> bool:
    parts = target_branch.split(".")
    if len(parts) < 2:
        return False
    try:
        major = int(parts[0])
    except ValueError:
        return False
    return major < _CURRENT_DEV_MAJOR
