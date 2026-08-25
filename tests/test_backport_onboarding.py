"""Tests for post-first-GA backport onboarding."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from github.GithubException import GithubException

from scripts.backport.matrix import build_matrix
from scripts.backport.onboarding import (
    ProjectMatch,
    _issue_body,
    _pr_body,
    _upsert_issue,
    _validate_existing_pr,
    _validate_existing_registry_branch,
    add_registry_branch,
    discover_project,
    first_ga_branch,
    onboard_first_ga,
    registry_project_number,
)
from scripts.backport.onboarding import (
    main as onboarding_main,
)

_REGISTRY = """repos:
  - repo: valkey-io/valkey
    project_owner: valkey-io
    project_owner_type: organization
    language: c
    branches:
      - branch: "9.1"
        project_number: 41

  - repo: valkey-io/other
    project_owner: valkey-io
    language: c
    branches:
      - branch: "1.0"
        project_number: 7
"""

_BOT = "valkeyrie[bot]"


def _project(
    number: int,
    title: str,
    *,
    description: str = "",
    closed: bool = False,
    configured: bool = True,
) -> dict[str, object]:
    fields: list[dict[str, object]] = []
    if configured:
        fields.append({
            "__typename": "ProjectV2SingleSelectField",
            "name": "Status",
            "options": [{"name": "To be backported"}],
        })
    return {
        "number": number,
        "title": title,
        "shortDescription": description,
        "url": f"https://github.com/orgs/valkey-io/projects/{number}",
        "closed": closed,
        "fields": {
            "nodes": fields,
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }


def _gql_with(*projects: dict[str, object]) -> MagicMock:
    gql = MagicMock()
    gql.execute.return_value = {
        "organization": {
            "projectsV2": {
                "nodes": list(projects),
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            }
        }
    }
    return gql


class _Content:
    def __init__(self, text: str, sha: str = "file-sha") -> None:
        self.decoded_content = text.encode()
        self.sha = sha


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("9.2.0", "9.2"),
        ("10.0.0", "10.0"),
        ("9.2.0-rc1", None),
        ("9.2.1", None),
        ("v9.2.0", None),
        ("9.2", None),
    ],
)
def test_first_ga_branch(tag: str, expected: str | None) -> None:
    assert first_ga_branch(tag) == expected


def test_discovery_requires_exact_branch_token_and_backport_status() -> None:
    gql = _gql_with(
        _project(20, "Valkey 9.20 backports"),
        _project(21, "Valkey release planning", description="Branch 9.2", configured=False),
        _project(22, "Valkey 9.2 backports", closed=True),
        _project(23, "Valkey 9.2 backports"),
    )

    matches, detail = discover_project(gql, owner="valkey-io", branch="9.2")

    assert matches == [ProjectMatch(
        number=23,
        title="Valkey 9.2 backports",
        url="https://github.com/orgs/valkey-io/projects/23",
    )]
    assert detail == ""


def test_discovery_zero_and_ambiguous_never_choose_a_project() -> None:
    no_matches, no_detail = discover_project(
        _gql_with(_project(20, "Valkey 9.20 backports")),
        owner="valkey-io",
        branch="9.2",
    )
    assert no_matches == []
    assert "No matching Project" in no_detail

    ambiguous, ambiguous_detail = discover_project(
        _gql_with(
            _project(23, "Valkey 9.2 backports"),
            _project(24, "9.2 maintenance and backports"),
        ),
        owner="valkey-io",
        branch="9.2",
    )
    assert [project.number for project in ambiguous] == [23, 24]
    assert "Found 2 matching Projects" in ambiguous_detail


def test_discovery_paginates_projects() -> None:
    gql = MagicMock()
    gql.execute.side_effect = [
        {
            "organization": {
                "projectsV2": {
                    "nodes": [_project(20, "Valkey 9.20 backports")],
                    "pageInfo": {"hasNextPage": True, "endCursor": "next"},
                }
            }
        },
        {
            "organization": {
                "projectsV2": {
                    "nodes": [_project(23, "Valkey 9.2 backports")],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        },
    ]

    matches, _ = discover_project(gql, owner="valkey-io", branch="9.2")

    assert [project.number for project in matches] == [23]
    assert gql.execute.call_args_list[1].args[1]["cursor"] == "next"




def test_discovery_paginates_fields_before_deciding_cardinality() -> None:
    later_status = _project(24, "Valkey 9.2 maintenance", configured=False)
    later_status["fields"] = {
        "nodes": [],
        "pageInfo": {"hasNextPage": True, "endCursor": "fields-next"},
    }
    gql = MagicMock()
    gql.execute.side_effect = [
        {
            "organization": {
                "projectsV2": {
                    "nodes": [_project(23, "Valkey 9.2 backports"), later_status],
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        },
        {
            "organization": {
                "projectV2": {
                    "fields": {
                        "nodes": [{
                            "__typename": "ProjectV2SingleSelectField",
                            "name": "Status",
                            "options": [{"name": "To be backported"}],
                        }],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            }
        },
    ]

    matches, detail = discover_project(gql, owner="valkey-io", branch="9.2")

    assert [project.number for project in matches] == [23, 24]
    assert "Found 2 matching Projects" in detail
    assert gql.execute.call_args_list[1].args[1] == {
        "owner": "valkey-io", "number": 24, "cursor": "fields-next",
    }
def test_registry_edit_is_surgical_and_generates_matrix_entry(tmp_path) -> None:
    updated = add_registry_branch(_REGISTRY, "valkey-io/valkey", "9.2", 23)

    assert updated == _REGISTRY.replace(
        '\n\n  - repo: valkey-io/other',
        '\n      - branch: "9.2"\n        project_number: 23\n\n  - repo: valkey-io/other',
    )
    assert registry_project_number(updated, "valkey-io/valkey", "9.2") == 23
    path = tmp_path / "repos.yml"
    path.write_text(updated, encoding="utf-8")
    matrix = build_matrix(str(path))
    assert any(
        item["repo"] == "valkey-io/valkey"
        and item["branch"] == "9.2"
        and item["project_number"] == 23
        for item in matrix["include"]
    )


def test_registry_edit_refuses_conflicting_existing_project() -> None:
    registered = add_registry_branch(_REGISTRY, "valkey-io/valkey", "9.2", 23)
    with pytest.raises(RuntimeError, match="Project 23"):
        add_registry_branch(registered, "valkey-io/valkey", "9.2", 24)


def test_issue_is_unlabeled_and_deduplicated_by_marker() -> None:
    repo = MagicMock()
    created = SimpleNamespace(
        number=17,
        html_url="https://github.com/valkey-io/valkey-ci-agent/issues/17",
        title="Enable backport automation for Valkey 9.2",
        body="",
        pull_request=None,
    )
    repo.get_issues.return_value = []
    repo.create_issue.return_value = created
    project = ProjectMatch(23, "Valkey 9.2 backports", "https://example/projects/23")

    number, _ = _upsert_issue(
        repo,
        source_repo="valkey-io/valkey",
        branch="9.2",
        tag="9.2.0",
        project=project,
        blocker="",
        expected_author=_BOT,
    )

    assert number == 17
    kwargs = repo.create_issue.call_args.kwargs
    assert "labels" not in kwargs
    assert "<!-- valkey-ci-agent:backport-onboarding:valkey-io/valkey:9.2 -->" in kwargs["body"]

    existing = SimpleNamespace(
        number=17,
        html_url=created.html_url,
        title=kwargs["title"],
        body=kwargs["body"],
        pull_request=None,
        user=SimpleNamespace(login=_BOT),
        edit=MagicMock(),
    )
    repo.get_issues.return_value = [existing]
    repo.create_issue.reset_mock()
    _upsert_issue(
        repo,
        source_repo="valkey-io/valkey",
        branch="9.2",
        tag="9.2.0",
        project=project,
        blocker="",
        expected_author=_BOT,
    )
    repo.create_issue.assert_not_called()
    existing.edit.assert_not_called()



def test_issue_create_recovers_ambiguous_server_error_by_marker() -> None:
    repo = MagicMock()
    marker = "<!-- valkey-ci-agent:backport-onboarding:valkey-io/valkey:9.2 -->"
    recovered = SimpleNamespace(
        number=17,
        html_url="https://github.com/valkey-io/valkey-ci-agent/issues/17",
        title="Enable backport automation for Valkey 9.2",
        body=_issue_body(
            marker,
            "9.2.0",
            "valkey-io/valkey",
            "9.2",
            None,
            "Project discovery failed",
        ),
        pull_request=None,
        user=SimpleNamespace(login=_BOT),
    )
    repo.get_issues.side_effect = [[], [recovered]]
    repo.create_issue.side_effect = GithubException(500, "response lost")

    number, url = _upsert_issue(
        repo,
        source_repo="valkey-io/valkey",
        branch="9.2",
        tag="9.2.0",
        project=None,
        blocker="Project discovery failed",
        expected_author=_BOT,
    )

    assert (number, url) == (17, recovered.html_url)
    repo.create_issue.assert_called_once()



def test_issue_marker_from_unexpected_author_is_refused() -> None:
    marker = "<!-- valkey-ci-agent:backport-onboarding:valkey-io/valkey:9.2 -->"
    repo = MagicMock()
    repo.get_issues.return_value = [SimpleNamespace(
        title="Enable backport automation for Valkey 9.2",
        body=marker,
        pull_request=None,
        user=SimpleNamespace(login="untrusted-user"),
    )]

    with pytest.raises(RuntimeError, match="pre-seeded by an unexpected author"):
        _upsert_issue(
            repo,
            source_repo="valkey-io/valkey",
            branch="9.2",
            tag="9.2.0",
            project=None,
            blocker="No Project",
            expected_author=_BOT,
        )

def test_issue_update_preserves_human_notes_and_refuses_title_only_adoption() -> None:
    repo = MagicMock()
    project = ProjectMatch(23, "Valkey 9.2 backports", "https://example/projects/23")
    created = SimpleNamespace(number=17, html_url="https://example/issues/17")
    repo.get_issues.return_value = []
    repo.create_issue.return_value = created
    _upsert_issue(
        repo,
        source_repo="valkey-io/valkey",
        branch="9.2",
        tag="9.2.0",
        project=None,
        blocker="No Project",
        expected_author=_BOT,
    )
    generated = repo.create_issue.call_args.kwargs["body"]
    existing = SimpleNamespace(
        number=17,
        html_url=created.html_url,
        title="Enable backport automation for Valkey 9.2",
        body=f"{generated}\n\nHuman note: coordinate with maintainers.",
        pull_request=None,
        user=SimpleNamespace(login=_BOT),
        edit=MagicMock(),
    )
    repo.get_issues.return_value = [existing]

    _upsert_issue(
        repo,
        source_repo="valkey-io/valkey",
        branch="9.2",
        tag="9.2.0",
        project=project,
        blocker="",
        expected_author=_BOT,
    )

    updated = existing.edit.call_args.kwargs["body"]
    assert "GitHub Project discovered: [Valkey 9.2 backports]" in updated
    assert updated.endswith("Human note: coordinate with maintainers.")

    unowned = SimpleNamespace(
        title="Enable backport automation for Valkey 9.2",
        body="Human-created issue without marker",
        pull_request=None,
    )
    repo.get_issues.return_value = [unowned]
    with pytest.raises(RuntimeError, match="without the ownership marker"):
        _upsert_issue(
            repo,
            source_repo="valkey-io/valkey",
            branch="9.2",
            tag="9.2.0",
            project=project,
            blocker="",
            expected_author=_BOT,
        )


def _agent_repo() -> tuple[MagicMock, MagicMock]:
    repo = MagicMock()
    repo.default_branch = "main"
    repo.get_contents.return_value = _Content(_REGISTRY)
    repo.get_branch.return_value = SimpleNamespace(commit=SimpleNamespace(sha="base-sha"))
    repo.get_issues.return_value = []
    repo.create_issue.return_value = SimpleNamespace(
        number=17,
        html_url="https://github.com/valkey-io/valkey-ci-agent/issues/17",
    )
    repo.get_pulls.return_value = []
    repo.create_pull.return_value = SimpleNamespace(
        html_url="https://github.com/valkey-io/valkey-ci-agent/pull/18",
    )
    gh = MagicMock()
    gh.get_repo.return_value = repo
    return gh, repo



def _configure_generated_branch(repo: MagicMock, expected: str) -> None:
    repo.get_branch.side_effect = lambda name: SimpleNamespace(
        commit=SimpleNamespace(
            sha="generated-sha" if name == "agent/backport/onboard/9.2" else "base-sha"
        )
    )
    repo.get_commit.return_value = SimpleNamespace(
        parents=[SimpleNamespace(sha="base-sha")],
        files=[SimpleNamespace(filename="repos.yml")],
    )
    repo.compare.return_value = SimpleNamespace(status="identical")
    repo.get_contents.side_effect = lambda _path, ref: _Content(
        expected if ref == "generated-sha" else _REGISTRY
    )
    repo.update_file.return_value = {
        "commit": SimpleNamespace(sha="generated-sha"),
    }

def test_zero_project_creates_blocker_issue_but_no_pr() -> None:
    gh, repo = _agent_repo()

    result = onboard_first_ga(
        _gql_with(), gh,
        agent_repo="valkey-io/valkey-ci-agent",
        tag="9.2.0",
        issue_author=_BOT,
    )

    assert result.action == "blocked-project-discovery"
    assert result.issue_url.endswith("/issues/17")
    issue_body = repo.create_issue.call_args.kwargs["body"]
    assert "No Project number was guessed and no pull request was opened" in issue_body
    repo.create_git_ref.assert_not_called()
    repo.create_pull.assert_not_called()


def test_project_api_failure_still_creates_blocker_issue() -> None:
    gh, repo = _agent_repo()
    gql = MagicMock()
    gql.execute.side_effect = RuntimeError("Projects permission denied")

    result = onboard_first_ga(
        gql, gh,
        agent_repo="valkey-io/valkey-ci-agent",
        tag="9.2.0",
        issue_author=_BOT,
    )

    assert result.action == "blocked-project-discovery"
    assert "Projects permission denied" in result.detail
    assert "Project discovery failed" in repo.create_issue.call_args.kwargs["body"]
    repo.create_pull.assert_not_called()


def test_unique_project_creates_exact_registry_pr_linked_to_issue(tmp_path) -> None:
    gh, repo = _agent_repo()
    expected = add_registry_branch(_REGISTRY, "valkey-io/valkey", "9.2", 23)
    _configure_generated_branch(repo, expected)
    project = ProjectMatch(
        23,
        "Valkey 9.2 backports",
        "https://github.com/orgs/valkey-io/projects/23",
    )
    repo.create_pull.return_value = SimpleNamespace(
        html_url="https://github.com/valkey-io/valkey-ci-agent/pull/18",
        title="Enable backports for Valkey 9.2",
        body=_pr_body("valkey-io/valkey", "9.2", project, 17),
        base=SimpleNamespace(ref="main"),
        head=SimpleNamespace(
            repo=SimpleNamespace(full_name="valkey-io/valkey-ci-agent"),
            ref="agent/backport/onboard/9.2",
            sha="generated-sha",
        ),
    )

    result = onboard_first_ga(
        _gql_with(_project(23, "Valkey 9.2 backports")),
        gh,
        agent_repo="valkey-io/valkey-ci-agent",
        tag="9.2.0",
        issue_author=_BOT,
    )

    assert result.action == "pr-created"
    repo.create_git_ref.assert_called_once_with(
        ref="refs/heads/agent/backport/onboard/9.2", sha="base-sha",
    )
    update = repo.update_file.call_args
    assert update.args[0] == "repos.yml"
    assert update.kwargs["branch"] == "agent/backport/onboard/9.2"
    assert "Signed-off-by: github-actions[bot]" in update.args[1]
    assert update.kwargs["author"] == update.kwargs["committer"]
    updated_text = update.args[2]
    assert registry_project_number(updated_text, "valkey-io/valkey", "9.2") == 23
    path = tmp_path / "repos.yml"
    path.write_text(updated_text, encoding="utf-8")
    assert any(
        item["repo"] == "valkey-io/valkey" and item["branch"] == "9.2"
        for item in build_matrix(str(path))["include"]
    )
    pr_kwargs = repo.create_pull.call_args.kwargs
    assert pr_kwargs["title"] == "Enable backports for Valkey 9.2"
    assert pr_kwargs["head"] == "agent/backport/onboard/9.2"
    assert pr_kwargs["base"] == "main"
    assert "Closes #17" in pr_kwargs["body"]


def test_already_registered_skips_project_and_issue_discovery() -> None:
    gh, repo = _agent_repo()
    repo.get_contents.return_value = _Content(
        add_registry_branch(_REGISTRY, "valkey-io/valkey", "9.2", 23)
    )
    gql = MagicMock()

    result = onboard_first_ga(
        gql, gh,
        agent_repo="valkey-io/valkey-ci-agent",
        tag="9.2.0",
        issue_author=_BOT,
    )

    assert result.action == "already-registered"
    gql.execute.assert_not_called()
    repo.get_issues.assert_not_called()
    repo.create_pull.assert_not_called()


def test_branch_movement_after_update_is_refused() -> None:
    repo = MagicMock()
    repo.get_branch.return_value = SimpleNamespace(commit=SimpleNamespace(sha="moved-sha"))

    with pytest.raises(RuntimeError, match="moved after the generated commit"):
        _validate_existing_registry_branch(
            repo,
            "agent/backport/onboard/9.2",
            "base-sha",
            "valkey-io/valkey",
            "9.2",
            23,
            expected_head_sha="generated-sha",
        )


def test_pr_create_recovers_ambiguous_server_error() -> None:
    gh, repo = _agent_repo()
    expected = add_registry_branch(_REGISTRY, "valkey-io/valkey", "9.2", 23)
    _configure_generated_branch(repo, expected)
    project = ProjectMatch(
        23,
        "Valkey 9.2 backports",
        "https://github.com/orgs/valkey-io/projects/23",
    )
    recovered = SimpleNamespace(
        html_url="https://github.com/valkey-io/valkey-ci-agent/pull/18",
        title="Enable backports for Valkey 9.2",
        body=_pr_body("valkey-io/valkey", "9.2", project, 17),
        base=SimpleNamespace(ref="main"),
        head=SimpleNamespace(
            repo=SimpleNamespace(full_name="valkey-io/valkey-ci-agent"),
            ref="agent/backport/onboard/9.2",
            sha="generated-sha",
        ),
    )
    repo.get_pulls.side_effect = [[], [recovered]]
    repo.create_pull.side_effect = GithubException(500, "response lost")

    result = onboard_first_ga(
        _gql_with(_project(23, "Valkey 9.2 backports")),
        gh,
        agent_repo="valkey-io/valkey-ci-agent",
        tag="9.2.0",
        issue_author=_BOT,
    )

    assert result.action == "pr-already-open"
    assert result.pr_url == recovered.html_url
    repo.create_pull.assert_called_once()


def test_existing_deterministic_branch_with_unrelated_changes_is_refused() -> None:
    repo = MagicMock()
    repo.get_commit.return_value = SimpleNamespace(
        parents=[SimpleNamespace(sha="parent")],
        files=[SimpleNamespace(filename="repos.yml"), SimpleNamespace(filename="README.md")],
    )

    with pytest.raises(RuntimeError, match="changes other than"):
        _validate_existing_registry_branch(
            repo,
            "agent/backport/onboard/9.2",
            "base-sha",
            "valkey-io/valkey",
            "9.2",
            23,
        )




def test_existing_generated_commit_on_diverged_history_is_refused() -> None:
    repo = MagicMock()
    repo.get_commit.return_value = SimpleNamespace(
        parents=[SimpleNamespace(sha="unrelated-parent")],
        files=[SimpleNamespace(filename="repos.yml")],
    )
    repo.compare.return_value = SimpleNamespace(status="diverged")

    with pytest.raises(RuntimeError, match="not the current default head"):
        _validate_existing_registry_branch(
            repo,
            "agent/backport/onboard/9.2",
            "base-sha",
            "valkey-io/valkey",
            "9.2",
            23,
        )


@pytest.mark.parametrize(
    ("base", "body"),
    [
        ("maintenance", "owned body with Closes #17"),
        ("main", "owned body without a closing reference"),
    ],
)
def test_existing_pr_must_have_exact_base_and_owned_body(base: str, body: str) -> None:
    pr = SimpleNamespace(
        html_url="https://example/pull/18",
        title="Enable backports for Valkey 9.2",
        body=body,
        base=SimpleNamespace(ref=base),
        head=SimpleNamespace(
            repo=SimpleNamespace(full_name="valkey-io/valkey-ci-agent"),
            ref="agent/backport/onboard/9.2",
            sha="generated-sha",
        ),
    )

    with pytest.raises(RuntimeError, match="not the exact agent-owned"):
        _validate_existing_pr(
            pr,
            default_branch="main",
            head_repo="valkey-io/valkey-ci-agent",
            head_branch="agent/backport/onboard/9.2",
            head_sha="generated-sha",
            expected_title="Enable backports for Valkey 9.2",
            expected_body="owned body with Closes #17",
        )


def test_existing_exact_branch_and_pr_are_reused() -> None:
    gh, repo = _agent_repo()
    expected = add_registry_branch(_REGISTRY, "valkey-io/valkey", "9.2", 23)
    _configure_generated_branch(repo, expected)
    repo.create_git_ref.side_effect = GithubException(422, "Reference already exists")
    project = ProjectMatch(
        23,
        "Valkey 9.2 backports",
        "https://github.com/orgs/valkey-io/projects/23",
    )
    existing_pr = SimpleNamespace(
        html_url="https://github.com/valkey-io/valkey-ci-agent/pull/18",
        title="Enable backports for Valkey 9.2",
        body=_pr_body("valkey-io/valkey", "9.2", project, 17),
        base=SimpleNamespace(ref="main"),
        head=SimpleNamespace(
            repo=SimpleNamespace(full_name="valkey-io/valkey-ci-agent"),
            ref="agent/backport/onboard/9.2",
            sha="generated-sha",
        ),
    )
    repo.get_pulls.return_value = [existing_pr]

    result = onboard_first_ga(
        _gql_with(_project(23, "Valkey 9.2 backports")),
        gh,
        agent_repo="valkey-io/valkey-ci-agent",
        tag="9.2.0",
        issue_author=_BOT,
    )

    assert result.action == "pr-already-open"
    assert result.pr_url.endswith("/pull/18")
    repo.update_file.assert_not_called()
    repo.create_pull.assert_not_called()


def test_cli_non_first_ga_needs_no_credentials(capsys: pytest.CaptureFixture[str]) -> None:
    assert onboarding_main(["--tag", "9.2.1", "--agent-repo", "o/agent"]) == 0
    assert "::warning" not in capsys.readouterr().out


def test_cli_missing_token_and_runtime_failure_are_fail_soft(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert onboarding_main(["--tag", "9.2.0", "--agent-repo", "o/agent"]) == 0
    assert "remains published" in capsys.readouterr().out

    with patch(
        "scripts.backport.onboarding.onboard_first_ga",
        side_effect=RuntimeError("unexpected failure"),
    ):
        assert onboarding_main([
            "--tag", "9.2.0",
            "--agent-repo", "o/agent",
            "--token", "token",
            "--issue-author", _BOT,
        ]) == 0
    output = capsys.readouterr().out
    assert "unexpected failure" in output
    assert "remains published" in output
