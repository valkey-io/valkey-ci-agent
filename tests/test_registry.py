"""Tests for the backport registry loader."""

from __future__ import annotations

import pytest
import yaml

from scripts.backport.registry import (
    BranchEntry,
    Registry,
    RepoEntry,
    ValidationRule,
    load_registry,
)


def _write_registry(tmp_path, data):
    path = tmp_path / "repos.yml"
    path.write_text(yaml.dump(data), encoding="utf-8")
    return str(path)


def _minimal_repo(**overrides):
    base = {
        "repo": "org/repo",
        "project_owner": "org",
        "project_owner_type": "organization",
        "language": "c",
        "branches": [{"branch": "1.0", "project_number": 1}],
    }
    base.update(overrides)
    return base


def _minimal_registry(**overrides):
    base = {
        "repos": [_minimal_repo()],
    }
    base.update(overrides)
    return base


class TestLoadRegistry:
    def test_valid_minimal(self, tmp_path):
        path = _write_registry(tmp_path, _minimal_registry())
        reg = load_registry(path)
        assert isinstance(reg, Registry)
        assert len(reg.repos) == 1
        entry = reg.repos[0]
        assert entry.repo == "org/repo"
        assert entry.project_owner == "org"
        assert entry.language == "c"
        assert entry.branches == (BranchEntry("1.0", 1),)
        assert entry.push_repo is None
        assert entry.effective_push_repo == "org/repo"
        assert entry.build_commands == ()
        assert entry.validation_rules == ()
        assert entry.backport_label == "backport"
        assert entry.llm_conflict_label == "ai-resolved-conflicts"
        assert entry.max_conflicting_files == 100

    def test_full_entry(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(
            push_repo="fork/repo",
            build_commands=["make -j4"],
            validation_rules=[
                {
                    "paths": ["src/cluster_legacy.c", "tests/unit/cluster/*.tcl"],
                    "commands": ["./runtest --single unit/cluster/slot-migration"],
                }
            ],
            backport_label="bp",
            llm_conflict_label="ai",
            max_conflicting_files=50,
            branches=[
                {"branch": "1.0", "project_number": 1},
                {"branch": "2.0", "project_number": 2},
            ],
        )])
        path = _write_registry(tmp_path, data)
        reg = load_registry(path)
        entry = reg.repos[0]
        assert entry.push_repo == "fork/repo"
        assert entry.effective_push_repo == "fork/repo"
        assert entry.build_commands == ("make -j4",)
        assert entry.validation_rules == (
            ValidationRule(
                paths=("src/cluster_legacy.c", "tests/unit/cluster/*.tcl"),
                commands=("./runtest --single unit/cluster/slot-migration",),
            ),
        )
        assert entry.backport_label == "bp"
        assert entry.llm_conflict_label == "ai"
        assert entry.max_conflicting_files == 50
        assert len(entry.branches) == 2

    def test_get_repo(self, tmp_path):
        path = _write_registry(tmp_path, _minimal_registry())
        reg = load_registry(path)
        assert reg.get_repo("org/repo").repo == "org/repo"

    def test_get_repo_missing(self, tmp_path):
        path = _write_registry(tmp_path, _minimal_registry())
        reg = load_registry(path)
        with pytest.raises(KeyError, match="not-here"):
            reg.get_repo("not-here")

    def test_get_branch(self, tmp_path):
        path = _write_registry(tmp_path, _minimal_registry())
        reg = load_registry(path)
        repo_entry, branch_entry = reg.get_branch("org/repo", "1.0")
        assert repo_entry.repo == "org/repo"
        assert branch_entry.project_number == 1

    def test_get_branch_missing(self, tmp_path):
        path = _write_registry(tmp_path, _minimal_registry())
        reg = load_registry(path)
        with pytest.raises(KeyError, match="9.9"):
            reg.get_branch("org/repo", "9.9")


class TestValidation:
    def test_not_a_mapping(self, tmp_path):
        path = tmp_path / "repos.yml"
        path.write_text("- list item\n")
        with pytest.raises(ValueError, match="YAML mapping"):
            load_registry(str(path))

    def test_repos_empty(self, tmp_path):
        path = _write_registry(tmp_path, {"repos": []})
        with pytest.raises(ValueError, match="non-empty list"):
            load_registry(path)

    def test_missing_repo_field(self, tmp_path):
        data = _minimal_registry(repos=[{"project_owner": "x", "language": "c", "branches": [{"branch": "1.0", "project_number": 1}]}])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="repo"):
            load_registry(path)

    def test_invalid_repo_format(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(repo="noslash")])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="owner/name"):
            load_registry(path)

    def test_missing_language(self, tmp_path):
        repo = _minimal_repo()
        del repo["language"]
        data = _minimal_registry(repos=[repo])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="language"):
            load_registry(path)

    def test_duplicate_repo(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(), _minimal_repo()])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="Duplicate repo"):
            load_registry(path)

    def test_duplicate_branch(self, tmp_path):
        repo = _minimal_repo(branches=[
            {"branch": "1.0", "project_number": 1},
            {"branch": "1.0", "project_number": 2},
        ])
        data = _minimal_registry(repos=[repo])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="Duplicate branch"):
            load_registry(path)

    def test_duplicate_project_number(self, tmp_path):
        repo = _minimal_repo(branches=[
            {"branch": "1.0", "project_number": 1},
            {"branch": "2.0", "project_number": 1},
        ])
        data = _minimal_registry(repos=[repo])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="Duplicate project_number"):
            load_registry(path)

    def test_invalid_owner_type(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(project_owner_type="bot")])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="project_owner_type"):
            load_registry(path)

    def test_invalid_push_repo(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(push_repo="noslash")])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="push_repo"):
            load_registry(path)

    def test_same_owner_push_repo_rejected(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(push_repo="org/other-repo")])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="different-owner fork"):
            load_registry(path)

    def test_same_repo_push_repo_rejected(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(push_repo="org/repo")])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="different-owner fork"):
            load_registry(path)

    def test_missing_push_repo_defaults_to_direct_upstream(self, tmp_path):
        repo = _minimal_repo()
        data = _minimal_registry(repos=[repo])
        path = _write_registry(tmp_path, data)
        reg = load_registry(path)
        assert reg.get_repo("org/repo").push_repo is None
        assert reg.get_repo("org/repo").effective_push_repo == "org/repo"

    def test_build_commands_not_list(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(build_commands="make")])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="build_commands must be a list"):
            load_registry(path)

    def test_build_commands_rejects_empty_command(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(build_commands=["make", "  "])])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match=r"build_commands\[1\] must be a non-empty string"):
            load_registry(path)

    def test_backport_label_must_be_non_empty_string(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(backport_label=None)])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="backport_label must be a non-empty string"):
            load_registry(path)

    def test_llm_conflict_label_must_be_non_empty_string(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(llm_conflict_label="")])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="llm_conflict_label must be a non-empty string"):
            load_registry(path)

    def test_validation_rules_not_list(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(validation_rules="rules")])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="validation_rules must be a list"):
            load_registry(path)

    def test_validation_rule_requires_paths(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(validation_rules=[{"commands": ["make test"]}])])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="paths must be a non-empty list"):
            load_registry(path)

    def test_validation_rule_rejects_whitespace_only_path(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(validation_rules=[{
            "paths": ["   "],
            "commands": ["make test"],
        }])])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match=r"paths\[0\] must be a non-empty string"):
            load_registry(path)

    def test_validation_rule_requires_commands(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(validation_rules=[{"paths": ["src/*.c"]}])])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="commands must be a non-empty list"):
            load_registry(path)

    def test_validation_rule_rejects_whitespace_only_command(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(validation_rules=[{
            "paths": ["src/*.c"],
            "commands": ["   "],
        }])])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match=r"commands\[0\] must be a non-empty string"):
            load_registry(path)

    def test_max_conflicting_files_invalid(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(max_conflicting_files=0)])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="positive integer"):
            load_registry(path)

    def test_branches_empty(self, tmp_path):
        data = _minimal_registry(repos=[_minimal_repo(branches=[])])
        path = _write_registry(tmp_path, data)
        with pytest.raises(ValueError, match="non-empty list"):
            load_registry(path)
