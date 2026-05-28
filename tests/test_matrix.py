from __future__ import annotations

import json

from scripts.backport.matrix import build_matrix


def _write_registry(tmp_path) -> str:
    path = tmp_path / "repos.yml"
    path.write_text(
        """
repos:
  - repo: org/core
    project_owner: org
    project_owner_type: organization
    language: c
    build_commands:
      - make test
    branches:
      - branch: "1.0"
        project_number: 1
      - branch: "2.0"
        project_number: 2
  - repo: org/module
    project_owner: org
    project_owner_type: organization
    language: c++
    branches:
      - branch: "1.0"
        project_number: 3
""",
        encoding="utf-8",
    )
    return str(path)


def test_build_matrix_emits_one_leg_per_registered_branch(tmp_path) -> None:
    matrix = build_matrix(_write_registry(tmp_path))

    assert [entry["repo"] for entry in matrix["include"]] == [
        "org/core",
        "org/core",
        "org/module",
    ]
    assert matrix["include"][0]["branch"] == "1.0"
    assert matrix["include"][0]["repo_slug"] == "org-core"
    assert matrix["include"][0]["project_number"] == 1
    assert matrix["include"][0]["push_repo"] == "org/core"
    assert matrix["include"][0]["language"] == "c"
    assert json.loads(matrix["include"][0]["build_commands_json"]) == ["make test"]
    assert json.loads(matrix["include"][0]["validation_setup_commands_json"]) == []
    assert matrix["include"][0]["validate_each_candidate"] is False


def test_build_matrix_filters_by_repo_and_project_number(tmp_path) -> None:
    matrix = build_matrix(
        _write_registry(tmp_path),
        repo_filter="org/core",
        project_number_filter=2,
    )

    assert matrix == {
        "include": [
            {
                "repo": "org/core",
                "repo_slug": "org-core",
                "project_owner": "org",
                "project_owner_type": "organization",
                "project_number": 2,
                "branch": "2.0",
                "push_repo": "org/core",
                "language": "c",
                "build_commands_json": json.dumps(["make test"]),
                "validation_setup_commands_json": json.dumps([]),
                "validate_each_candidate": False,
            }
        ]
    }
