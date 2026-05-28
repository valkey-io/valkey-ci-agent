"""Registry loader for multi-repo backport configuration."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_VALID_OWNER_TYPES = {"organization", "user"}


@dataclass(frozen=True)
class BranchEntry:
    branch: str
    project_number: int


@dataclass(frozen=True)
class ValidationRule:
    paths: tuple[str, ...]
    commands: tuple[str, ...]


@dataclass(frozen=True)
class RepoEntry:
    repo: str
    project_owner: str
    project_owner_type: str
    language: str
    branches: tuple[BranchEntry, ...]
    push_repo: str | None = None
    build_commands: tuple[str, ...] = ()
    validation_setup_commands: tuple[str, ...] = ()
    validation_rules: tuple[ValidationRule, ...] = ()
    validate_each_candidate: bool = False
    backport_label: str = "backport"
    llm_conflict_label: str = "ai-resolved-conflicts"
    max_conflicting_files: int = 100

    @property
    def effective_push_repo(self) -> str:
        return self.push_repo or self.repo


@dataclass(frozen=True)
class Registry:
    repos: tuple[RepoEntry, ...]

    def get_repo(self, repo_full_name: str) -> RepoEntry:
        for entry in self.repos:
            if entry.repo == repo_full_name:
                return entry
        raise KeyError(f"Repository '{repo_full_name}' not found in registry")

    def get_branch(self, repo_full_name: str, branch: str) -> tuple[RepoEntry, BranchEntry]:
        repo_entry = self.get_repo(repo_full_name)
        for b in repo_entry.branches:
            if b.branch == branch:
                return repo_entry, b
        raise KeyError(
            f"Branch '{branch}' not found for '{repo_full_name}' in registry"
        )


def load_registry(path: str) -> Registry:
    """Load and validate the registry from a YAML file."""
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, dict):
        raise ValueError(f"Registry file must be a YAML mapping, got {type(raw).__name__}")
    return _parse_registry(raw)


def _parse_registry(raw: dict[str, Any]) -> Registry:
    # repos
    repos_raw = raw.get("repos", [])
    if not isinstance(repos_raw, list) or not repos_raw:
        raise ValueError("repos must be a non-empty list")

    seen_repos: set[str] = set()
    entries: list[RepoEntry] = []
    for i, repo_raw in enumerate(repos_raw):
        entries.append(_parse_repo_entry(repo_raw, i, seen_repos))

    return Registry(
        repos=tuple(entries),
    )


def _parse_repo_entry(raw: Any, index: int, seen_repos: set[str]) -> RepoEntry:
    if not isinstance(raw, dict):
        raise ValueError(f"repos[{index}] must be a mapping")

    repo = raw.get("repo")
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        raise ValueError(f"repos[{index}].repo must be a valid 'owner/name' string, got {repo!r}")
    if repo in seen_repos:
        raise ValueError(f"Duplicate repo in registry: {repo!r}")
    seen_repos.add(repo)

    project_owner = raw.get("project_owner")
    if not isinstance(project_owner, str) or not project_owner:
        raise ValueError(f"repos[{index}].project_owner is required")

    project_owner_type = raw.get("project_owner_type", "organization")
    if project_owner_type not in _VALID_OWNER_TYPES:
        raise ValueError(
            f"repos[{index}].project_owner_type must be one of {_VALID_OWNER_TYPES}, "
            f"got {project_owner_type!r}"
        )

    language = raw.get("language")
    if not isinstance(language, str) or not language:
        raise ValueError(f"repos[{index}].language is required")

    push_repo = raw.get("push_repo")
    if push_repo is not None:
        if not isinstance(push_repo, str) or not _REPO_RE.match(push_repo):
            raise ValueError(
                f"repos[{index}].push_repo must be a valid 'owner/name' string"
            )
        if push_repo.split("/", 1)[0] == repo.split("/", 1)[0]:
            raise ValueError(
                f"repos[{index}].push_repo must be a different-owner fork; "
                "omit push_repo for direct upstream pushes"
            )

    build_commands = raw.get("build_commands", [])
    if not isinstance(build_commands, list):
        raise ValueError(f"repos[{index}].build_commands must be a list")
    for j, cmd in enumerate(build_commands):
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError(
                f"repos[{index}].build_commands[{j}] must be a non-empty string"
            )

    validation_setup_commands = raw.get("validation_setup_commands", [])
    if not isinstance(validation_setup_commands, list):
        raise ValueError(f"repos[{index}].validation_setup_commands must be a list")
    for j, cmd in enumerate(validation_setup_commands):
        if not isinstance(cmd, str) or not cmd.strip():
            raise ValueError(
                f"repos[{index}].validation_setup_commands[{j}] "
                "must be a non-empty string"
            )

    validation_rules = _parse_validation_rules(raw.get("validation_rules", []), index)
    validate_each_candidate = raw.get("validate_each_candidate", False)
    if not isinstance(validate_each_candidate, bool):
        raise ValueError(f"repos[{index}].validate_each_candidate must be a boolean")

    backport_label = raw.get("backport_label", "backport")
    if not isinstance(backport_label, str) or not backport_label.strip():
        raise ValueError(f"repos[{index}].backport_label must be a non-empty string")
    llm_conflict_label = raw.get("llm_conflict_label", "ai-resolved-conflicts")
    if not isinstance(llm_conflict_label, str) or not llm_conflict_label.strip():
        raise ValueError(f"repos[{index}].llm_conflict_label must be a non-empty string")
    max_conflicting_files = raw.get("max_conflicting_files", 100)
    if not isinstance(max_conflicting_files, int) or max_conflicting_files < 1:
        raise ValueError(f"repos[{index}].max_conflicting_files must be a positive integer")

    branches_raw = raw.get("branches", [])
    if not isinstance(branches_raw, list) or not branches_raw:
        raise ValueError(f"repos[{index}].branches must be a non-empty list")

    seen_branches: set[str] = set()
    seen_projects: set[int] = set()
    branches: list[BranchEntry] = []
    for j, b_raw in enumerate(branches_raw):
        branches.append(_parse_branch_entry(b_raw, index, j, seen_branches, seen_projects))

    return RepoEntry(
        repo=repo,
        project_owner=project_owner,
        project_owner_type=project_owner_type,
        language=language,
        push_repo=push_repo,
        build_commands=tuple(build_commands),
        validation_setup_commands=tuple(validation_setup_commands),
        validation_rules=tuple(validation_rules),
        validate_each_candidate=validate_each_candidate,
        backport_label=backport_label,
        llm_conflict_label=llm_conflict_label,
        max_conflicting_files=max_conflicting_files,
        branches=tuple(branches),
    )


def _parse_validation_rules(raw: Any, repo_idx: int) -> list[ValidationRule]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError(f"repos[{repo_idx}].validation_rules must be a list")

    rules: list[ValidationRule] = []
    for rule_idx, rule_raw in enumerate(raw):
        if not isinstance(rule_raw, dict):
            raise ValueError(
                f"repos[{repo_idx}].validation_rules[{rule_idx}] must be a mapping"
            )
        paths = rule_raw.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError(
                f"repos[{repo_idx}].validation_rules[{rule_idx}].paths "
                "must be a non-empty list"
            )
        for path_idx, pattern in enumerate(paths):
            if not isinstance(pattern, str) or not pattern.strip():
                raise ValueError(
                    f"repos[{repo_idx}].validation_rules[{rule_idx}]"
                    f".paths[{path_idx}] must be a non-empty string"
                )

        commands = rule_raw.get("commands")
        if not isinstance(commands, list) or not commands:
            raise ValueError(
                f"repos[{repo_idx}].validation_rules[{rule_idx}].commands "
                "must be a non-empty list"
            )
        for cmd_idx, command in enumerate(commands):
            if not isinstance(command, str) or not command.strip():
                raise ValueError(
                    f"repos[{repo_idx}].validation_rules[{rule_idx}]"
                    f".commands[{cmd_idx}] must be a non-empty string"
                )

        rules.append(
            ValidationRule(
                paths=tuple(str(pattern) for pattern in paths),
                commands=tuple(str(command) for command in commands),
            )
        )
    return rules


def _parse_branch_entry(
    raw: Any, repo_idx: int, branch_idx: int,
    seen_branches: set[str], seen_projects: set[int],
) -> BranchEntry:
    if not isinstance(raw, dict):
        raise ValueError(f"repos[{repo_idx}].branches[{branch_idx}] must be a mapping")

    branch = raw.get("branch")
    if not isinstance(branch, str) or not branch:
        raise ValueError(f"repos[{repo_idx}].branches[{branch_idx}].branch is required")
    if branch in seen_branches:
        raise ValueError(f"Duplicate branch '{branch}' in repos[{repo_idx}]")
    seen_branches.add(branch)

    project_number = raw.get("project_number")
    if not isinstance(project_number, int) or project_number < 0:
        raise ValueError(
            f"repos[{repo_idx}].branches[{branch_idx}].project_number must be a non-negative integer"
        )
    if project_number in seen_projects:
        raise ValueError(
            f"Duplicate project_number {project_number} in repos[{repo_idx}]"
        )
    seen_projects.add(project_number)

    return BranchEntry(branch=str(branch), project_number=project_number)
