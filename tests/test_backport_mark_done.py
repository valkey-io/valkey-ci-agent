from __future__ import annotations

import json
import os
import sys

import pytest

import scripts.backport.mark_done as mark_done
from scripts.backport.mark_done import (
    BackportStatusUpdateResult,
    mark_backport_items_done,
    reconcile_project_board,
)
from scripts.common.polling import PollLoopError


def test_mark_backport_items_done_updates_matching_to_be_backported_items() -> None:
    gql = FakeGraphQLClient(
        project_items=[
            _project_item(101, "valkey-io/valkey", "item-101", "To be backported"),
            _project_item(102, "valkey-io/valkey", "item-102", "Done"),
            _project_item(103, "valkey-io/valkey", "item-103", "Needs review"),
            _project_item(104, "valkey-io/valkey-bloom", "item-104", "To be backported"),
        ]
    )

    result = mark_backport_items_done(
        gql,
        project_owner="valkey-io",
        project_number=14,
        source_repo="valkey-io/valkey",
        source_pr_numbers=[101, 102, 103, 104, 105],
        verified_pr_numbers={101, 102, 103, 104, 105},
    )

    assert result.updated == [101]
    assert result.already_done == [102]
    assert result.missing == [104, 105]
    assert result.skipped == {103: "Status is 'Needs review', not 'To be backported'"}
    assert gql.mutations == [
        {
            "projectId": "project-1",
            "itemId": "item-101",
            "fieldId": "status-field",
            "optionId": "done-option",
        }
    ]


def test_mark_backport_items_done_gates_on_verified_set() -> None:
    gql = FakeGraphQLClient(
        project_items=[
            _project_item(101, "valkey-io/valkey", "item-101", "To be backported"),
            _project_item(102, "valkey-io/valkey", "item-102", "To be backported"),
        ]
    )

    result = mark_backport_items_done(
        gql,
        project_owner="valkey-io",
        project_number=14,
        source_repo="valkey-io/valkey",
        source_pr_numbers=[101, 102],
        verified_pr_numbers={101},
    )

    assert result.updated == [101]
    assert result.unverified == [102]
    assert [m["itemId"] for m in gql.mutations] == ["item-101"]


def test_reconcile_marks_only_branch_present_items(monkeypatch) -> None:
    gql = FakeGraphQLClient(
        project_items=[
            _project_item(201, "valkey-io/valkey", "item-201", "To be backported"),
            _project_item(202, "valkey-io/valkey", "item-202", "To be backported"),
            _project_item(203, "valkey-io/valkey", "item-203", "Done"),
            _project_item(204, "valkey-io/valkey-bloom", "item-204", "To be backported"),
        ]
    )

    captured: dict = {}

    def fake_verify(repo, branch, pr_numbers, *, token="", git_env=None):
        captured["repo"] = repo
        captured["branch"] = branch
        captured["pr_numbers"] = set(pr_numbers)
        return {201}  # only 201 actually landed on the branch

    monkeypatch.setattr(mark_done, "verify_prs_on_branch", fake_verify)

    result = reconcile_project_board(
        gql,
        project_owner="valkey-io",
        project_number=14,
        source_repo="valkey-io/valkey",
        target_branch="9.1",
    )

    # Only valkey-io/valkey items still "To be backported" are candidates.
    assert captured["pr_numbers"] == {201, 202}
    assert captured["repo"] == "valkey-io/valkey"
    assert captured["branch"] == "9.1"
    assert result.updated == [201]
    assert result.unverified == [202]
    assert [m["itemId"] for m in gql.mutations] == ["item-201"]


def test_reconcile_no_candidates_is_noop(monkeypatch) -> None:
    gql = FakeGraphQLClient(
        project_items=[_project_item(301, "valkey-io/valkey", "item-301", "Done")]
    )
    monkeypatch.setattr(
        mark_done, "verify_prs_on_branch",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not verify")),
    )

    result = reconcile_project_board(
        gql,
        project_owner="valkey-io",
        project_number=14,
        source_repo="valkey-io/valkey",
        target_branch="9.1",
    )

    assert result == BackportStatusUpdateResult(requested=[])
    assert gql.mutations == []


def test_pr_numbers_from_subjects_ignores_body_only_mentions() -> None:
    from scripts.backport.utils import pr_numbers_from_commit_subjects

    # Each element is a commit *subject*. A (#N) here means that commit is PR N.
    subjects = [
        "Fix a thing (#3801)",
        "Unrelated work without a ref",
        "Another fix (#3920)",
    ]
    assert pr_numbers_from_commit_subjects(subjects) == {3801, 3920}


def test_pr_numbers_from_subjects_uses_trailing_pr_only() -> None:
    from scripts.backport.utils import pr_numbers_from_commit_subjects

    # A revert names the reverted PR mid-subject; only the trailing (#N) is the
    # commit's own PR. Must be 3756 (the revert), not 3544 (what it reverts).
    subjects = ['Revert "IO-Threads redesign cleanup work (#3544)" (#3756)']
    assert pr_numbers_from_commit_subjects(subjects) == {3756}


def test_verify_counts_subject_but_not_body_mention(tmp_path, monkeypatch) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, env=env,
            capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-q")
    (repo / "f").write_text("1")
    git("add", "f")
    git("commit", "-qm", "Cherry-picked fix (#3801)")

    (repo / "f").write_text("2")
    git(
        "commit", "-aqm",
        "Some later work\n\nThis follows up on (#3920) but does not apply it.",
    )

    # Clone is the local repo (skip network). verify operates on the checked-out tree.
    def fake_clone(repo_full_name, target_branch, dest_dir, git_env):
        subprocess.run(["git", "clone", "-q", str(repo), dest_dir], check=True, env=env)

    monkeypatch.setattr(mark_done, "_shallow_clone", fake_clone)

    present = mark_done.verify_prs_on_branch(
        "valkey-io/valkey",
        "9.1",
        {
            3801,  # present via subject (#3801)
            3920,  # only mentioned in a body -> NOT present
            4242,  # never referenced -> absent
        },
    )

    assert present == {3801}


def test_verify_detects_squash_merged_applied_table(tmp_path, monkeypatch) -> None:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, env=env, capture_output=True, text=True)

    # A squash-merged backport sweep: subject names the backport PR (#3774);
    # the source PRs it applied live only in the ## Applied table.
    body = (
        "[backport] Backport sweep for 9.1 (#3774)\n\n"
        "## Applied\n\n"
        "| Source PR | Title | Detail |\n"
        "|---|---|---|\n"
        "| #3801 | Validate DB clause | |\n"
        "| #3847 | Revert work (#7777) | depends on #8888 |\n\n"
        "## Needs attention\n\n"
        "| Source PR | Title | Outcome | Reason |\n"
        "|---|---|---|---|\n"
        "| #9999 | Failed one | skipped-conflict | conflict |\n"
    )
    git("init", "-q")
    (repo / "f").write_text("1")
    git("add", "f")
    git("commit", "-qm", body)

    def fake_clone(repo_full_name, target_branch, dest_dir, git_env):
        subprocess.run(["git", "clone", "-q", str(repo), dest_dir], check=True, env=env)

    monkeypatch.setattr(mark_done, "_shallow_clone", fake_clone)

    present = mark_done.verify_prs_on_branch(
        "valkey-io/valkey",
        "9.1",
        {3801, 3847, 7777, 8888, 9999},  # the ## Applied table is the only signal
    )

    # Only Source-PR-column entries of the Applied table count: the #7777 in a
    # Title cell, the #8888 in a Detail cell, and the Needs-attention #9999 are
    # all excluded.
    assert present == {3801, 3847}


def test_dry_run_reports_without_mutating() -> None:
    gql = FakeGraphQLClient(
        project_items=[
            _project_item(101, "valkey-io/valkey", "item-101", "To be backported"),
        ]
    )

    result = mark_backport_items_done(
        gql,
        project_owner="valkey-io",
        project_number=14,
        source_repo="valkey-io/valkey",
        source_pr_numbers=[101],
        verified_pr_numbers={101},
        dry_run=True,
    )

    assert result.updated == [101]
    assert gql.mutations == []


def test_main_surfaces_sustained_poll_failure(monkeypatch, capsys) -> None:
    class FakeRegistry:
        pass

    monkeypatch.setattr(
        "scripts.backport.registry.load_registry",
        lambda _path: FakeRegistry(),
    )
    monkeypatch.setattr(mark_done, "GitHubGraphQLClient", lambda _token: object())

    def fail_after_success(*_args, **_kwargs):
        raise PollLoopError(
            results=[{"repo": "valkey-io/valkey", "action": "checked"}],
            last_error=RuntimeError("transient graphql failure"),
        )

    monkeypatch.setattr(mark_done, "run_poll_loop_from_args", fail_after_success)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mark_done",
            "--repo", "valkey-io/valkey",
            "--target-branch", "9.1",
            "--target-token", "tok",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        mark_done.main()

    assert raised.value.code == 1
    output = json.loads(capsys.readouterr().out)
    assert output["runs"] == [
        {"repo": "valkey-io/valkey", "action": "checked"},
        {
            "repo": "valkey-io/valkey",
            "target_branch": "9.1",
            "action": "error",
            "error": "transient graphql failure",
        },
    ]


class FakeGraphQLClient:
    def __init__(self, *, project_items: list[dict]) -> None:
        self._project_items = project_items
        self.mutations: list[dict] = []

    def execute(self, query: str, variables: dict) -> dict:
        if "updateProjectV2ItemFieldValue" in query:
            self.mutations.append(dict(variables))
            return {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": variables["itemId"]}}}

        return {
            "organization": {
                "projectV2": {
                    "id": "project-1",
                    "fields": {
                        "nodes": [
                            {
                                "__typename": "ProjectV2SingleSelectField",
                                "id": "status-field",
                                "name": "Status",
                                "options": [
                                    {"id": "todo-option", "name": "To be backported"},
                                    {"id": "done-option", "name": "Done"},
                                ],
                            }
                        ]
                    },
                    "items": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": self._project_items,
                    },
                }
            }
        }


def _project_item(
    number: int, repo: str, item_id: str, status: str
) -> dict:
    return {
        "id": item_id,
        "content": {
            "__typename": "PullRequest",
            "number": number,
            "repository": {"nameWithOwner": repo},
        },
        "fieldValues": {
            "nodes": [
                {
                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "name": status,
                    "field": {"name": "Status"},
                }
            ]
        },
    }
