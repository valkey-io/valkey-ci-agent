"""Load the deliberately small release allow-list."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from scripts.release.models import ReleasePolicy


def load_policy(path: str | Path) -> ReleasePolicy:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("release policy must be a schema_version: 1 mapping")

    allowed = {
        "schema_version",
        "repo",
        "authorized_team",
        "branches",
        "checks_workflow",
        "required_checks",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"unknown release policy key(s): {', '.join(sorted(unknown))}")

    repo = _nonempty(raw.get("repo"), "repo")
    team = _nonempty(raw.get("authorized_team"), "authorized_team")
    if team.count("/") != 1 or any(not part for part in team.split("/")):
        raise ValueError("authorized_team must be org/team-slug")
    workflow = _nonempty(raw.get("checks_workflow"), "checks_workflow")
    if "/" in workflow or not workflow.endswith((".yml", ".yaml")):
        raise ValueError("checks_workflow must be a workflow filename")

    branches = _strings(raw.get("branches"), "branches")
    checks = _strings(raw.get("required_checks"), "required_checks")
    if len(set(branches)) != len(branches):
        raise ValueError("branches contains duplicates")
    if len(set(checks)) != len(checks):
        raise ValueError("required_checks contains duplicates")

    return ReleasePolicy(
        repo=repo,
        authorized_team=team,
        branches=branches,
        checks_workflow=workflow,
        required_checks=checks,
    )


def validate_branch(policy: ReleasePolicy, branch: str) -> str:
    branch = branch.strip()
    if branch not in policy.branches:
        raise ValueError(f"branch {branch!r} is not releasable; allowed: {', '.join(policy.branches)}")
    return branch


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a non-empty list")
    result = tuple(_nonempty(item, name) for item in value)
    return result
