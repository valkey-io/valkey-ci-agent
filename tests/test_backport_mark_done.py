from __future__ import annotations

import os

import scripts.backport.mark_done as mark_done
from scripts.backport.mark_done import (
    BackportStatusUpdateResult,
    mark_backport_items_done,
    parse_backport_source_pr_numbers,
    reconcile_project_board,
)


def test_parse_sweep_body_uses_only_applied_section() -> None:
    body = """# Backport sweep for 8.1

Automated cherry-picks from PRs marked "To be backported".

## Applied

| Source PR | Title | Detail |
|---|---|---|
| #101 | Good fix |  |
| #102 | Other fix | conflicts resolved |

## Needs attention

| Source PR | Title | Outcome | Reason |
|---|---|---|---|
| #999 | Bad fix | skipped-conflict | conflict |
"""

    assert parse_backport_source_pr_numbers(body) == [101, 102]


def test_parse_manual_body_source_pr_row() -> None:
    body = """## Backport Summary

| Field | Value |
|---|---|
| Source PR | [#123](https://github.com/valkey-io/valkey/pull/123) |
| Target branch | `8.1` |
"""

    assert parse_backport_source_pr_numbers(body) == [123]


def test_parse_head_ref_for_single_pr_backport() -> None:
    assert parse_backport_source_pr_numbers("", head_ref="backport/456-to-8.1") == [456]


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
            _project_item(201, "valkey-io/valkey", "item-201", "To be backported", merge_sha="aaa"),
            _project_item(202, "valkey-io/valkey", "item-202", "To be backported", merge_sha="bbb"),
            _project_item(203, "valkey-io/valkey", "item-203", "Done", merge_sha="ccc"),
            _project_item(204, "valkey-io/valkey-bloom", "item-204", "To be backported", merge_sha="ddd"),
        ]
    )

    captured: dict = {}

    def fake_verify(repo, branch, pr_merge_shas, *, git_env=None):
        captured["repo"] = repo
        captured["branch"] = branch
        captured["pr_merge_shas"] = dict(pr_merge_shas)
        return {201}  # only 201 actually landed on the branch

    monkeypatch.setattr(mark_done, "verify_prs_on_branch", fake_verify)

    result = reconcile_project_board(
        gql,
        project_owner="valkey-io",
        project_number=14,
        source_repo="valkey-io/valkey",
        target_branch="9.1",
    )

    # Only valkey-io/valkey items still "To be backported" are candidates,
    # each paired with its development-branch merge SHA.
    assert captured["pr_merge_shas"] == {201: "aaa", 202: "bbb"}
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


def test_verify_counts_subject_and_sha_but_not_body_mention(tmp_path, monkeypatch) -> None:
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
    sha_3801 = git("rev-parse", "HEAD")  # this commit's own SHA = the "merge sha" case

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
            3801: "irrelevant",   # present via subject (#3801)
            3920: "irrelevant",   # only mentioned in a body -> NOT present
            3801_000 + 1: sha_3801,  # present via merge-SHA ancestry
            4242: "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",  # unknown sha -> absent
        },
    )

    assert 3801 in present
    assert 3801_000 + 1 in present
    assert 3920 not in present
    assert 4242 not in present


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
    number: int, repo: str, item_id: str, status: str, *, merge_sha: str = ""
) -> dict:
    return {
        "id": item_id,
        "content": {
            "__typename": "PullRequest",
            "number": number,
            "repository": {"nameWithOwner": repo},
            "mergeCommit": {"oid": merge_sha} if merge_sha else None,
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
