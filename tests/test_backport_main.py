"""Tests for backport pipeline (main.py)."""

from __future__ import annotations

import sys
from unittest.mock import ANY, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from scripts.backport.main import (
    _reconcile_diff_comments_best_effort,
    _skipped_existing_message,
    build_summary,
    run_backport,
)
from scripts.backport.main import main as backport_main
from scripts.backport.models import (
    DETAIL_EMPTY_ON_TARGET,
    BackportConfig,
    BackportPRContext,
    BackportResult,
    CandidateResult,
    CherryPickResult,
    ConflictedFile,
    ResolutionResult,
)
from scripts.backport.registry import ValidationRule
from scripts.backport.sweep_validation import ValidationOutcome

# ======================================================================
# ======================================================================


class TestBuildSummaryProperty:
    """build_summary includes all key metrics from BackportResult."""

    @given(
        commits=st.integers(min_value=0, max_value=10_000),
        conflicted=st.integers(min_value=0, max_value=10_000),
        resolved=st.integers(min_value=0, max_value=10_000),
        unresolved=st.integers(min_value=0, max_value=10_000),
        tokens=st.integers(min_value=0, max_value=10_000_000),
        outcome=st.sampled_from([
            "success", "conflicts-unresolved", "duplicate",
        ]),
    )
    @settings(max_examples=100, deadline=None)
    def test_summary_contains_all_metrics(
        self,
        commits: int,
        conflicted: int,
        resolved: int,
        unresolved: int,
        tokens: int,
        outcome: str,
    ) -> None:
        result = BackportResult(
            outcome=outcome,
            commits_cherry_picked=commits,
            files_conflicted=conflicted,
            files_resolved=resolved,
            files_unresolved=unresolved,
        )
        summary = build_summary(result)

        assert str(commits) in summary, f"commits {commits} not in summary"
        assert str(conflicted) in summary, f"conflicted {conflicted} not in summary"
        assert str(resolved) in summary, f"resolved {resolved} not in summary"
        assert str(unresolved) in summary, f"unresolved {unresolved} not in summary"



# ======================================================================
# ======================================================================



# ======================================================================
# Unit tests for run_backport pipeline flow (Task 8.4)
# ======================================================================


def _default_config() -> BackportConfig:
    return BackportConfig()


def test_cli_rejects_target_branch_missing_from_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    registry = tmp_path / "repos.yml"
    registry.write_text(
        """
repos:
  - repo: valkey-io/valkey
    project_owner: valkey-io
    project_owner_type: organization
    language: c
    branches:
      - branch: "8.1"
        project_number: 14
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BACKPORT_GITHUB_TOKEN", "token")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "backport",
            "--registry",
            str(registry),
            "--repo",
            "valkey-io/valkey",
            "--pr-number",
            "100",
            "--target-branch",
            "9.9",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        backport_main()

    assert exc.value.code == 2
    assert "Branch '9.9' not found" in capsys.readouterr().err


def _make_mock_pr(
    title: str = "Fix bug",
    body: str = "Fixes a bug",
    html_url: str = "https://github.com/valkey-io/valkey/pull/100",
    merge_commit_sha: str = "merge_sha_abc",
    merged: bool = True,
    commits: list | None = None,
) -> MagicMock:
    """Create a mock source PR object."""
    pr = MagicMock()
    pr.title = title
    pr.body = body
    pr.html_url = html_url
    pr.merge_commit_sha = merge_commit_sha
    pr.merged = merged

    if commits is None:
        commit1 = MagicMock()
        commit1.sha = "commit_sha_1"
        commits = [commit1]

    pr.get_commits.return_value = commits
    return pr


# Shared patch targets
_PATCH_PREFIX = "scripts.backport.main"
_DEFAULT_PUSH_REPO = "ci-bot/valkey"


@patch(f"{_PATCH_PREFIX}.Github")
def test_run_backport_allows_different_owner_push_repo(mock_github) -> None:
    mock_repo = MagicMock()
    mock_repo.get_branch.return_value = MagicMock()
    mock_pr = _make_mock_pr()
    mock_repo.get_pull.return_value = mock_pr
    mock_github.return_value.get_repo.return_value = mock_repo

    with (
        patch(f"{_PATCH_PREFIX}._clone_repo"),
        patch(f"{_PATCH_PREFIX}._run_git"),
        patch(f"{_PATCH_PREFIX}.BackportPRCreator") as mock_creator_cls,
        patch(f"{_PATCH_PREFIX}.apply_candidate") as mock_apply_candidate,
    ):
        mock_apply_candidate.return_value = CandidateResult(
            source_pr_number=100,
            source_pr_title="Fix bug",
            outcome="applied",
            applied_commits=["abc123"],
        )
        mock_creator_cls.return_value.check_duplicate.return_value = None
        mock_creator_cls.return_value.create_backport_pr.return_value = (
            "https://github.com/valkey-io/valkey/pull/1"
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
        )

    assert result.outcome == "success"
    mock_creator_cls.assert_called_once()
    _, kwargs = mock_creator_cls.call_args
    assert kwargs["base_repo"] == "valkey-io/valkey"
    assert kwargs["push_repo"] == _DEFAULT_PUSH_REPO


def test_run_backport_rejects_redundant_same_repo_push_repo() -> None:
    result = run_backport(
        repo_full_name="valkey-io/valkey",
        source_pr_number=100,
        target_branch="8.1",
        config=_default_config(),
        github_token="fake-token",
        push_repo="valkey-io/valkey",
    )

    assert result.outcome == "error"
    assert "different-owner fork" in (result.error_message or "")


def test_run_backport_rejects_same_owner_push_repo() -> None:
    result = run_backport(
        repo_full_name="valkey-io/valkey",
        source_pr_number=100,
        target_branch="8.1",
        config=_default_config(),
        github_token="fake-token",
        push_repo="valkey-io/valkey-backport-staging",
    )

    assert result.outcome == "error"
    assert "different-owner fork" in (result.error_message or "")


@patch(f"{_PATCH_PREFIX}.Github")
def test_run_backport_defaults_to_direct_upstream_push(mock_github) -> None:
    mock_repo = MagicMock()
    mock_repo.get_branch.return_value = MagicMock()
    mock_repo.get_pull.return_value = _make_mock_pr()
    mock_github.return_value.get_repo.return_value = mock_repo

    with (
        patch(f"{_PATCH_PREFIX}._clone_repo"),
        patch(f"{_PATCH_PREFIX}._run_git") as mock_run_git,
        patch(f"{_PATCH_PREFIX}.BackportPRCreator") as mock_creator_cls,
        patch(f"{_PATCH_PREFIX}.apply_candidate") as mock_apply_candidate,
    ):
        mock_apply_candidate.return_value = CandidateResult(
            source_pr_number=100,
            source_pr_title="Fix bug",
            outcome="applied",
            applied_commits=["abc123"],
        )
        mock_creator_cls.return_value.check_duplicate.return_value = None
        mock_creator_cls.return_value.create_backport_pr.return_value = (
            "https://github.com/valkey-io/valkey/pull/1"
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
        )

    assert result.outcome == "success"
    mock_creator_cls.assert_called_once()
    _, kwargs = mock_creator_cls.call_args
    assert kwargs["push_repo"] == "valkey-io/valkey"
    assert any(
        call.args[1:4] == ("push", "--force-with-lease", "origin")
        for call in mock_run_git.call_args_list
    )
    assert not any(call.args[1:3] == ("remote", "add") for call in mock_run_git.call_args_list)


@patch(f"{_PATCH_PREFIX}.Github")
def test_validation_setup_failure_blocks_manual_application(mock_github) -> None:
    mock_repo = MagicMock()
    mock_repo.get_branch.return_value = MagicMock()
    mock_repo.get_pull.return_value = _make_mock_pr()
    mock_github.return_value.get_repo.return_value = mock_repo

    with (
        patch(f"{_PATCH_PREFIX}._clone_repo"),
        patch(f"{_PATCH_PREFIX}._run_git") as mock_run_git,
        patch(f"{_PATCH_PREFIX}.BackportPRCreator") as mock_creator_cls,
        patch(f"{_PATCH_PREFIX}.apply_candidate") as mock_apply_candidate,
        patch(f"{_PATCH_PREFIX}.run_build_commands") as mock_run_commands,
    ):
        mock_creator_cls.return_value.check_duplicate.return_value = None
        mock_run_commands.return_value = (False, "configure failed")

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
            validation_setup_commands=["./configure"],
        )

    assert result.outcome == "error"
    assert "Validation setup failed" in (result.error_message or "")
    mock_run_commands.assert_called_once_with(ANY, ["./configure"])
    mock_apply_candidate.assert_not_called()
    assert not any(
        len(call.args) > 1 and call.args[1] == "push"
        for call in mock_run_git.call_args_list
    )


class TestRunBackportCleanCherryPick:
    """Test clean cherry-pick flow — no conflicts, PR created successfully."""

    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_clean_cherry_pick_returns_success(
        self,
        mock_gh_cls: MagicMock,
        mock_apply_candidate: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_run_git: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        # Setup GitHub mock
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        # Branch exists
        mock_repo.get_branch.return_value = MagicMock()

        # Source PR
        source_pr = _make_mock_pr()
        mock_repo.get_pull.return_value = source_pr

        # Merge commit message
        mock_git_commit = MagicMock()
        mock_git_commit.raw_data = {"message": "merge commit msg"}
        mock_repo.get_git_commit.return_value = mock_git_commit

        # No duplicate
        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None
        mock_pr_creator.create_backport_pr.return_value = "https://github.com/valkey-io/valkey/pull/200"


        # Clean cherry-pick
        mock_apply_candidate.return_value = CandidateResult(
            source_pr_number=100,
            source_pr_title="Fix bug",
            outcome="applied",
            applied_commits=["commit_sha_1"],
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
        )

        assert result.outcome == "success"
        assert result.backport_pr_url == "https://github.com/valkey-io/valkey/pull/200"
        assert result.commits_cherry_picked == 1
        assert result.files_conflicted == 0
        assert result.files_resolved == 0
        assert result.files_unresolved == 0
        mock_pr_creator.create_backport_pr.assert_called_once()
        mock_pr_creator_cls.assert_called_once_with(
            mock_gh,
            base_repo="valkey-io/valkey",
            push_repo=_DEFAULT_PUSH_REPO,
            backport_label="backport",
            llm_conflict_label="ai-resolved-conflicts",
            benchmark_label="",
        )

    @patch(f"{_PATCH_PREFIX}.validate_branch_with_optional_repair")
    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_repair_enabled_uses_optional_repair_without_profile_or_generators(
        self,
        mock_gh_cls: MagicMock,
        mock_apply_candidate: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_run_git: MagicMock,
        mock_clone: MagicMock,
        mock_validate: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()
        mock_repo.get_pull.return_value = _make_mock_pr()

        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None
        mock_pr_creator.create_backport_pr.return_value = (
            "https://github.com/valkey-io/valkey/pull/200"
        )
        mock_apply_candidate.return_value = CandidateResult(
            source_pr_number=100,
            source_pr_title="Fix bug",
            outcome="applied",
            applied_commits=["commit_sha_1"],
        )
        mock_validate.return_value = ValidationOutcome(True, "validation passed")

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
            build_commands=["make"],
            repair_validation_failures=True,
        )

        assert result.outcome == "success"
        mock_validate.assert_called_once()
        args, kwargs = mock_validate.call_args
        assert args[:4] == (ANY, "8.1", ["make"], [])
        assert kwargs["repair"] is True
        assert kwargs["validation_profile"] == ""
        assert kwargs["generated_file_rules"] is None
        assert kwargs["run_git"] is mock_run_git

    @patch(f"{_PATCH_PREFIX}.run_build_commands")
    @patch(f"{_PATCH_PREFIX}.changed_paths_since_base", return_value=("src/server.c",))
    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_build_validation_failure_blocks_push_and_pr(
        self,
        mock_gh_cls: MagicMock,
        mock_apply_candidate: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_run_git: MagicMock,
        mock_clone: MagicMock,
        mock_changed_paths: MagicMock,
        mock_run_build_commands: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()
        mock_repo.get_pull.return_value = _make_mock_pr()

        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None

        mock_apply_candidate.return_value = CandidateResult(
            source_pr_number=100,
            source_pr_title="Fix bug",
            outcome="applied",
            applied_commits=["commit_sha_1"],
        )
        mock_run_build_commands.return_value = (False, "compile failed")

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            build_commands=["make"],
            validation_rules=[
                ValidationRule(
                    paths=("src/server.c",),
                    commands=("./runtest --single unit/cluster/slot-migration",),
                )
            ],
            push_repo=_DEFAULT_PUSH_REPO,
        )

        assert result.outcome == "error"
        assert "Build validation failed" in (result.error_message or "")
        mock_changed_paths.assert_called_once()
        mock_run_build_commands.assert_called_once_with(
            ANY,
            ["make", "./runtest --single unit/cluster/slot-migration"],
        )
        mock_pr_creator.create_backport_pr.assert_not_called()
        assert not any(
            len(call_args.args) > 1 and call_args.args[1] == "push"
            for call_args in mock_run_git.call_args_list
        )


class TestRunBackportConflictedCherryPick:
    """Test conflicted cherry-pick flow with LLM resolution."""

    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_conflicted_cherry_pick_with_resolution(
        self,
        mock_gh_cls: MagicMock,
        mock_apply_candidate: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_run_git: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        # Setup GitHub mock
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()

        source_pr = _make_mock_pr()
        mock_repo.get_pull.return_value = source_pr
        mock_git_commit = MagicMock()
        mock_git_commit.raw_data = {"message": ""}
        mock_repo.get_git_commit.return_value = mock_git_commit

        # No duplicate
        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None
        mock_pr_creator.create_backport_pr.return_value = "https://github.com/valkey-io/valkey/pull/201"


        # Cherry-pick with conflicts
        conflicted_file = ConflictedFile(
            path="src/server.c",
            target_branch_content="old",
            source_branch_content="new",
        )
        resolution = ResolutionResult(
            path="src/server.c",
            resolved_content="resolved content",
            resolution_summary="Applied fix",
        )
        mock_apply_candidate.return_value = CandidateResult(
            source_pr_number=100,
            source_pr_title="Fix bug",
            outcome="applied",
            conflicting_files=[conflicted_file],
            applied_commits=["merge_sha_abc"],
            resolutions=[resolution],
            resolved_by_ai=True,
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
        )

        assert result.outcome == "success"
        assert result.files_conflicted == 1
        assert result.files_resolved == 1
        assert result.files_unresolved == 0
        mock_apply_candidate.assert_called_once()

    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_ai_test_adaptation_is_disclosed_to_pr_creator(
        self,
        mock_gh_cls: MagicMock,
        mock_apply_candidate: MagicMock,
        mock_pr_creator_cls: MagicMock,
        _mock_run_git: MagicMock,
        _mock_clone: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()
        mock_repo.get_pull.return_value = _make_mock_pr()

        mock_pr_creator = mock_pr_creator_cls.return_value
        mock_pr_creator.check_duplicate.return_value = None
        mock_pr_creator.create_backport_pr.return_value = (
            "https://github.com/valkey-io/valkey/pull/201"
        )

        ai_summary = (
            "ported target-missing test coverage to: "
            "tests/unit/networking.tcl"
        )
        mock_apply_candidate.return_value = CandidateResult(
            source_pr_number=100,
            source_pr_title="Fix bug",
            outcome="applied",
            detail=(
                "dropped target-missing test file(s): "
                "src/unit/test_networking.cpp; "
                + ai_summary
            ),
            conflicting_files=[
                ConflictedFile(
                    path="src/unit/test_networking.cpp",
                    target_branch_content="",
                    source_branch_content="TEST(...)\n",
                )
            ],
            applied_commits=["merge_sha_abc"],
            resolved_by_ai=True,
            ai_summary=ai_summary,
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
        )

        assert result.outcome == "success"
        call = mock_pr_creator.create_backport_pr.call_args
        assert call.kwargs["ai_involved"] is True
        assert call.kwargs["ai_summary"] == ai_summary
        assert call.args[2] is None


@patch(f"{_PATCH_PREFIX}.reconcile_diff_comments")
def test_pr_body_rebuild_preserves_ai_adaptation_disclosure(
    mock_reconcile: MagicMock,
) -> None:
    mock_reconcile.return_value = {
        "src/server.c": "https://github.com/example/repo/pull/201#issuecomment-1"
    }
    repo = MagicMock()
    backport_pr = MagicMock()
    repo.get_pull.return_value = backport_pr
    context = BackportPRContext(
        source_pr_number=100,
        source_pr_title="Fix bug",
        source_pr_url="https://github.com/example/repo/pull/100",
        source_pr_diff="",
        target_branch="8.1",
        commits=["source"],
    )
    cherry_result = CherryPickResult(
        success=False,
        applied_commits=["squash"],
        conflicting_commit_sha="squash",
    )
    resolutions = [
        ResolutionResult(
            path="src/server.c",
            resolved_content="resolved\n",
            resolution_summary="whitespace-only (no LLM needed)",
            source="automatic",
        )
    ]
    ai_summary = (
        "ported target-missing test coverage to: tests/unit/networking.tcl"
    )

    _reconcile_diff_comments_best_effort(
        repo,
        "https://github.com/example/repo/pull/201",
        context,
        cherry_result,
        resolutions,
        ai_involved=True,
        ai_summary=ai_summary,
    )

    rebuilt_body = backport_pr.edit.call_args.kwargs["body"]
    assert "### AI Adaptation" in rebuilt_body
    assert ai_summary in rebuilt_body
    assert "### Human Review Required" in rebuilt_body


class TestRunBackportDuplicateDetection:
    """Test duplicate detection skip."""

    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_duplicate_pr_skips_processing(
        self,
        mock_gh_cls: MagicMock,
        mock_executor_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()

        # Duplicate exists
        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = "https://github.com/valkey-io/valkey/pull/99"

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
        )

        assert result.outcome == "duplicate"
        assert result.backport_pr_url == "https://github.com/valkey-io/valkey/pull/99"
        # Cherry-pick should NOT have been called
        mock_executor_cls.assert_not_called()



class TestRunBackportMergedPrValidation:
    """Test unmerged source PR skip."""

    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_unmerged_pr_skips_processing(
        self,
        mock_gh_cls: MagicMock,
        mock_executor_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()
        mock_repo.get_pull.return_value = _make_mock_pr(merged=False)

        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None


        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
        )

        assert result.outcome == "pr-not-merged"
        assert "not merged" in (result.error_message or "")
        mock_executor_cls.assert_not_called()
        mock_clone.assert_not_called()



class TestRunBackportMissingBranch:
    """Test missing branch skip."""

    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_missing_branch_skips_processing(
        self,
        mock_gh_cls: MagicMock,
        mock_executor_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        from github.GithubException import GithubException

        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        # Branch does not exist — 404
        mock_repo.get_branch.side_effect = GithubException(
            status=404, data={"message": "Branch not found"}, headers={},
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="nonexistent",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
        )

        assert result.outcome == "branch-missing"
        assert "nonexistent" in (result.error_message or "")
        mock_executor_cls.assert_not_called()


class TestRunBackportGitHubAPIError:
    """Test GitHub API error handling."""

    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_pr_creation_failure_returns_error(
        self,
        mock_gh_cls: MagicMock,
        mock_executor_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_run_git: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()

        source_pr = _make_mock_pr()
        mock_repo.get_pull.return_value = source_pr
        mock_git_commit = MagicMock()
        mock_git_commit.raw_data = {"message": ""}
        mock_repo.get_git_commit.return_value = mock_git_commit

        # No duplicate
        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None
        mock_pr_creator.create_backport_pr.side_effect = Exception("GitHub API error")


        # Clean cherry-pick
        mock_executor_cls.return_value = CandidateResult(
            source_pr_number=100,
            source_pr_title="Fix bug",
            outcome="applied",
            applied_commits=["sha1"],
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
        )

        assert result.outcome == "error"
        assert "GitHub API error" in (result.error_message or "")


class TestRunBackportCherryPickFailure:
    """Test cherry-pick failures that do not expose conflicts."""

    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_cherry_pick_failure_without_conflicts_does_not_push_or_create_pr(
        self,
        mock_gh_cls: MagicMock,
        mock_executor_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_run_git: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()
        mock_repo.get_pull.return_value = _make_mock_pr()

        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None


        mock_executor_cls.return_value = CandidateResult(
            source_pr_number=100,
            source_pr_title="Fix bug",
            outcome="error",
            detail=(
                "Cherry-pick failed without conflicted files; refusing to "
                "push an unresolved or unchanged backport branch."
            ),
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
        )

        assert result.outcome == "error"
        assert "without conflicted files" in (result.error_message or "")
        mock_pr_creator.create_backport_pr.assert_not_called()
        assert not any(
            len(call_args.args) > 1 and call_args.args[1] == "push"
            for call_args in mock_run_git.call_args_list
        )


class TestRunBackportAlreadyApplied:
    """Test no-op cherry-picks that are already present on target."""

    @patch(f"{_PATCH_PREFIX}._clone_repo")
    @patch(f"{_PATCH_PREFIX}._run_git")
    @patch(f"{_PATCH_PREFIX}.BackportPRCreator")
    @patch(f"{_PATCH_PREFIX}.apply_candidate")
    @patch(f"{_PATCH_PREFIX}.Github")
    def test_already_applied_does_not_push_or_create_pr(
        self,
        mock_gh_cls: MagicMock,
        mock_executor_cls: MagicMock,
        mock_pr_creator_cls: MagicMock,
        mock_run_git: MagicMock,
        mock_clone: MagicMock,
    ) -> None:
        mock_gh = MagicMock()
        mock_gh_cls.return_value = mock_gh
        mock_repo = MagicMock()
        mock_gh.get_repo.return_value = mock_repo
        mock_repo.get_branch.return_value = MagicMock()
        mock_repo.get_pull.return_value = _make_mock_pr()

        mock_pr_creator = MagicMock()
        mock_pr_creator_cls.return_value = mock_pr_creator
        mock_pr_creator.check_duplicate.return_value = None

        mock_executor_cls.return_value = CandidateResult(
            source_pr_number=100,
            source_pr_title="Fix bug",
            outcome="skipped-existing",
            detail="already applied or empty cherry-pick",
        )

        result = run_backport(
            repo_full_name="valkey-io/valkey",
            source_pr_number=100,
            target_branch="8.1",
            config=_default_config(),
            github_token="fake-token",
            push_repo=_DEFAULT_PUSH_REPO,
        )

        assert result.outcome == "already-applied"
        assert "already applied" in (result.error_message or "")
        mock_pr_creator.create_backport_pr.assert_not_called()
        assert not any(
            len(call_args.args) > 1 and call_args.args[1] == "push"
            for call_args in mock_run_git.call_args_list
        )

    def test_no_op_candidate_reports_accurate_reason(
        self,
    ) -> None:
        message = _skipped_existing_message(
            CandidateResult(
                source_pr_number=100,
                source_pr_title="Fix code absent from this branch",
                outcome="skipped-existing",
                detail=DETAIL_EMPTY_ON_TARGET,
                skip_reason="The affected code is absent from this release line.",
            ),
            "8.1",
        )

        assert "does not require a backport" in message
        assert "affected code is absent" in message
        assert "already applied" not in message
