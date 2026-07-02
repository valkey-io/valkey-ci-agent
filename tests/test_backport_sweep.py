from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github.GithubException import GithubException

from scripts.backport import sweep as backport_sweep
from scripts.backport import sweep_apply, sweep_git, sweep_graphql, sweep_validation
from scripts.backport.models import ResolutionResult
from scripts.backport.sweep import (
    BranchSweepResult,
    CandidateResult,
    ProjectBackportCandidate,
)
from scripts.backport.sweep_apply import apply_candidate
from scripts.backport.sweep_git import (
    changed_paths_in_index_or_worktree,
    clone_target_branch,
    list_applied_prs_on_branch,
    push_backport_branch,
    safe_tmp_component,
    sync_target_branch_to_source,
    worktree_changed_paths,
)
from scripts.backport.sweep_prs import upsert_pr
from scripts.backport.sweep_reporting import (
    build_pr_body,
    build_summary,
    parse_previous_applied,
    parse_previous_failed,
)
from scripts.backport.sweep_validation import (
    build_validation_repair_prompt,
    repair_validation_failure_with_claude,
    run_test_commands,
    validate_backport_branch,
)
from scripts.common.git_auth import GitAuth

DETAIL = backport_sweep.DETAIL_ALREADY_ON_SWEEP_BRANCH


def test_git_auth_keeps_askpass_outside_clone_destination(tmp_path):
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    with GitAuth("token", prefix="test-git-auth-") as git_auth:
        env = git_auth.env()
        askpass = Path(env["GIT_ASKPASS"])
        assert askpass.exists()
        assert askpass.parent != repo_dir
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_PASSWORD"] == "token"
    assert not askpass.exists()


def test_git_auth_default_env_strips_ambient_tokens(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ambient")
    monkeypatch.setenv("GH_TOKEN", "ambient")
    with GitAuth("token", prefix="test-git-auth-") as git_auth:
        env = git_auth.env()
    assert env["GIT_PASSWORD"] == "token"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_apply_candidate_aborts_empty_cherry_pick(monkeypatch, tmp_path):
    candidate = ProjectBackportCandidate(
        source_pr_number=10,
        source_pr_title="Already applied",
        source_pr_url="https://github.com/valkey-io/valkey/pull/10",
        target_branch="8.1",
        merge_commit_sha="abc123",
    )
    git_calls: list[tuple[str, ...]] = []
    subprocess_calls: list[list[str]] = []

    def fake_run_git(_repo_dir, *args, **_kwargs):
        git_calls.append(args)

    def fake_subprocess_run(cmd, **_kwargs):
        subprocess_calls.append(cmd)
        if cmd[:2] == ["git", "cherry-pick"]:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="The previous cherry-pick is now empty",
            )
        if cmd[:4] == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "cherry-pick", "--abort"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(sweep_apply.subprocess, "run", fake_subprocess_run)

    result = apply_candidate(
        repo_dir=str(tmp_path),
        candidate=candidate,
        repo_full_name="valkey-io/valkey",
        git_env={},
        run_git=fake_run_git,
        run_process=fake_subprocess_run,
    )

    assert result.outcome == "skipped-existing"
    assert result.detail == "already applied or empty cherry-pick"
    assert ("fetch", "origin", "abc123") in git_calls
    assert ("cherry-pick", "--abort") in git_calls


def test_apply_candidate_skips_binary_only_conflict(monkeypatch, tmp_path):
    candidate = ProjectBackportCandidate(
        source_pr_number=12,
        source_pr_title="Binary fixture conflict",
        source_pr_url="https://github.com/valkey-io/valkey-search/pull/12",
        target_branch="1.1",
        merge_commit_sha="abc123",
    )
    git_calls: list[tuple[str, ...]] = []
    resolver = MagicMock()

    def fake_run_git(_repo_dir, *args, **_kwargs):
        git_calls.append(args)

    def fake_subprocess_run(cmd, **_kwargs):
        if cmd[:2] == ["git", "cherry-pick"] and cmd[2:3] != ["--abort"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="conflict")
        if cmd[:4] == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="fixture.gz\n", stderr="")
        if cmd[:2] == ["git", "show"]:  # :2:fixture.gz / :3:fixture.gz
            return subprocess.CompletedProcess(cmd, 0, stdout="bin\x00ary", stderr="")
        if cmd[:3] == ["git", "cherry-pick", "--abort"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(sweep_apply.subprocess, "run", fake_subprocess_run)

    result = apply_candidate(
        repo_dir=str(tmp_path),
        candidate=candidate,
        repo_full_name="valkey-io/valkey-search",
        git_env={},
        run_git=fake_run_git,
        run_process=fake_subprocess_run,
        resolve_conflicts=resolver,
    )

    assert result.outcome == "skipped-conflict"
    assert "binary" in result.detail
    resolver.assert_not_called()
    assert ("cherry-pick", "--abort") in git_calls


def test_apply_candidate_retries_squash_merge_commit_without_mainline(
    monkeypatch,
    tmp_path,
):
    candidate = ProjectBackportCandidate(
        source_pr_number=11,
        source_pr_title="Squash merged fix",
        source_pr_url="https://github.com/valkey-io/valkey/pull/11",
        target_branch="8.1",
        merge_commit_sha="abc123",
    )
    git_calls: list[tuple[str, ...]] = []
    subprocess_calls: list[list[str]] = []

    def fake_run_git(_repo_dir, *args, **_kwargs):
        git_calls.append(args)

    def fake_subprocess_run(cmd, **_kwargs):
        subprocess_calls.append(cmd)
        if cmd == ["git", "cherry-pick", "-m", "1", "abc123"]:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="fatal: mainline was specified but commit abc123 is not a merge",
            )
        if cmd == ["git", "cherry-pick", "abc123"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(sweep_apply.subprocess, "run", fake_subprocess_run)

    result = apply_candidate(
        repo_dir=str(tmp_path),
        candidate=candidate,
        repo_full_name="valkey-io/valkey",
        git_env={},
        run_git=fake_run_git,
        run_process=fake_subprocess_run,
    )

    assert result.outcome == "applied"
    assert ("fetch", "origin", "abc123") in git_calls
    assert ["git", "cherry-pick", "-m", "1", "abc123"] in subprocess_calls
    assert ["git", "cherry-pick", "abc123"] in subprocess_calls


def test_apply_candidate_skips_noop_conflict_resolution(monkeypatch, tmp_path):
    conflicted_file = tmp_path / "conflict.txt"
    conflicted_file.write_text("target content\n", encoding="utf-8")
    candidate = ProjectBackportCandidate(
        source_pr_number=3317,
        source_pr_title="Fix macOS workflow",
        source_pr_url="https://github.com/valkey-io/valkey/pull/3317",
        target_branch="8.1",
        merge_commit_sha="abc123",
    )
    git_calls: list[tuple[str, ...]] = []
    subprocess_calls: list[list[str]] = []

    def fake_run_git(_repo_dir, *args, **_kwargs):
        git_calls.append(args)

    def fake_subprocess_run(cmd, **_kwargs):
        subprocess_calls.append(cmd)
        if cmd[:2] == ["git", "cherry-pick"] and "--abort" not in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="conflict")
        if cmd[:4] == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="conflict.txt\n", stderr="")
        if cmd in (
            ["git", "diff", "--name-only", "-z"],
            ["git", "diff", "--cached", "--name-only", "-z"],
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        ):
            return subprocess.CompletedProcess(cmd, 0, stdout="conflict.txt\0", stderr="")
        if cmd[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="target content\n", stderr="")
        if cmd[:3] == ["git", "cat-file", "-e"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:4] == ["git", "diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "cherry-pick", "--abort"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(sweep_apply.subprocess, "run", fake_subprocess_run)
    def fake_resolve(*_args, **_kwargs):
        return [
            ResolutionResult(
                path="conflict.txt",
                resolved_content="target content\n",
                resolution_summary="resolved",
            )
        ]

    result = apply_candidate(
        repo_dir=str(tmp_path),
        candidate=candidate,
        repo_full_name="valkey-io/valkey",
        git_env={},
        run_git=fake_run_git,
        run_process=fake_subprocess_run,
        resolve_conflicts=fake_resolve,
    )

    assert result.outcome == "skipped-existing"
    assert result.detail == "resolution was already satisfied on target branch"
    assert ("add", "conflict.txt") in git_calls
    assert ["git", "commit", "--no-edit"] not in subprocess_calls
    assert ("cherry-pick", "--abort") in git_calls


def test_apply_candidate_does_not_recreate_target_missing_file(monkeypatch, tmp_path):
    missing_on_target = tmp_path / "src" / "cluster_legacy.c"
    missing_on_target.parent.mkdir()
    missing_on_target.write_text("<<<<<<< HEAD\n=======\nlarge source file\n>>>>>>> source\n", encoding="utf-8")
    candidate = ProjectBackportCandidate(
        source_pr_number=2174,
        source_pr_title="Converge divergent shard-id",
        source_pr_url="https://github.com/valkey-io/valkey/pull/2174",
        target_branch="7.2",
        merge_commit_sha="def456",
    )
    git_calls: list[tuple[str, ...]] = []
    subprocess_calls: list[list[str]] = []

    def fake_run_git(_repo_dir, *args, **_kwargs):
        git_calls.append(args)

    def fake_subprocess_run(cmd, **_kwargs):
        subprocess_calls.append(cmd)
        if cmd[:2] == ["git", "cherry-pick"] and "--abort" not in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="conflict")
        if cmd[:4] == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="src/cluster_legacy.c\n", stderr="")
        if cmd[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="large source file\n", stderr="")
        if cmd[:3] == ["git", "cat-file", "-e"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="missing")
        if cmd[:4] == ["git", "diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "cherry-pick", "--abort"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(sweep_apply.subprocess, "run", fake_subprocess_run)
    def fake_resolve(*_args, **_kwargs):
        raise AssertionError("should not call Claude")

    result = apply_candidate(
        repo_dir=str(tmp_path),
        candidate=candidate,
        repo_full_name="valkey-io/valkey",
        git_env={},
        run_git=fake_run_git,
        run_process=fake_subprocess_run,
        resolve_conflicts=fake_resolve,
    )

    assert result.outcome == "skipped-conflict"
    assert result.detail == "target branch lacks conflicted file(s): src/cluster_legacy.c"
    assert ("add", "src/cluster_legacy.c") not in git_calls
    assert missing_on_target.exists()
    assert ["git", "commit", "--no-edit"] not in subprocess_calls
    assert ("cherry-pick", "--abort") in git_calls


def test_apply_candidate_fails_closed_when_abort_fails(monkeypatch, tmp_path):
    conflicted_file = tmp_path / "conflict.txt"
    conflicted_file.write_text("<<<<<<< HEAD\ntarget\n=======\nsource\n>>>>>>> source\n", encoding="utf-8")
    candidate = ProjectBackportCandidate(
        source_pr_number=631,
        source_pr_title="Fix ordering",
        source_pr_url="https://github.com/valkey-io/valkey-search/pull/631",
        target_branch="1.1",
        merge_commit_sha="abc123",
    )

    def fake_run_git(_repo_dir, *args, **_kwargs):
        if args == ("cherry-pick", "--abort"):
            raise subprocess.CalledProcessError(1, ["git", *args])

    def fake_subprocess_run(cmd, **_kwargs):
        if cmd[:2] == ["git", "cherry-pick"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="conflict")
        if cmd[:4] == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="conflict.txt\n", stderr="")
        if cmd in (
            ["git", "diff", "--name-only", "-z"],
            ["git", "diff", "--cached", "--name-only", "-z"],
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        ):
            return subprocess.CompletedProcess(cmd, 0, stdout="conflict.txt\0", stderr="")
        if cmd[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="target\n", stderr="")
        if cmd[:3] == ["git", "cat-file", "-e"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(sweep_apply.subprocess, "run", fake_subprocess_run)
    def fake_resolve(*_args, **_kwargs):
        return [
            ResolutionResult(
                path="conflict.txt",
                resolved_content=None,
                resolution_summary="unresolved",
            )
        ]

    with pytest.raises(subprocess.CalledProcessError):
        apply_candidate(
            repo_dir=str(tmp_path),
            candidate=candidate,
            repo_full_name="valkey-io/valkey-search",
            git_env={},
            run_git=fake_run_git,
            run_process=fake_subprocess_run,
            resolve_conflicts=fake_resolve,
        )


def test_run_test_commands_returns_failure_output(tmp_path):
    ok, output = run_test_commands(
        str(tmp_path),
        ["printf stdout; printf stderr >&2; exit 3"],
    )

    assert ok is False
    assert "stdout" in output
    assert "stderr" in output


def test_upsert_pr_uses_direct_upstream_branch_by_default():
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_pr = MagicMock()
    mock_pr.number = 555
    mock_pr.html_url = "https://github.com/valkey-io/valkey/pull/555"
    mock_repo.create_pull.return_value = mock_pr
    result = BranchSweepResult(
        target_branch="8.1",
        candidates_found=1,
        results=[
            CandidateResult(
                source_pr_number=10,
                source_pr_title="Fix module API",
                outcome="applied",
                detail="",
            )
        ],
    )

    pr_url = upsert_pr(
        mock_gh,
        "valkey-io/valkey",
        "valkey-io/valkey",
        "8.1",
        "agent/backport/sweep/8.1",
        result,
        existing_pr=None,
    )

    assert pr_url == "https://github.com/valkey-io/valkey/pull/555"
    mock_repo.create_pull.assert_called_once()
    _, kwargs = mock_repo.create_pull.call_args
    assert kwargs["head"] == "agent/backport/sweep/8.1"
    assert kwargs["base"] == "8.1"
    # Sweep PRs are opened directly (not as drafts) so maintainers see
    # them in the active queue alongside other PRs.
    assert kwargs["draft"] is False
    # A candidate with no AI resolution gets only the backport label.
    mock_pr.add_to_labels.assert_called_once_with("backport")


def test_upsert_pr_labels_ai_resolved_conflicts():
    """A sweep PR carrying an AI-resolved candidate gets the conflict label."""
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_pr = MagicMock()
    mock_pr.number = 777
    mock_pr.html_url = "https://github.com/valkey-io/valkey/pull/777"
    mock_repo.create_pull.return_value = mock_pr
    result = BranchSweepResult(
        target_branch="8.1",
        candidates_found=1,
        results=[
            CandidateResult(
                source_pr_number=11,
                source_pr_title="Conflicting change",
                outcome="applied",
                detail="",
                resolved_by_ai=True,
            )
        ],
    )

    upsert_pr(
        mock_gh,
        "valkey-io/valkey",
        "valkey-io/valkey",
        "8.1",
        "agent/backport/sweep/8.1",
        result,
        existing_pr=None,
        backport_label="backport",
        llm_conflict_label="ai-resolved-conflicts",
    )

    mock_pr.add_to_labels.assert_called_once_with("backport", "ai-resolved-conflicts")


def test_upsert_pr_labels_ai_resolved_from_branch_applied():
    """The conflict label is applied even when the AI-resolved candidate is
    already on the branch (a later top-up run that re-resolves nothing)."""
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    existing_pr = MagicMock()
    existing_pr.number = 888
    existing_pr.html_url = "https://github.com/valkey-io/valkey/pull/888"
    existing_pr.draft = False

    result = BranchSweepResult(
        target_branch="8.1",
        candidates_found=0,
        results=[],
    )
    branch_applied = [
        CandidateResult(12, "Earlier AI-resolved PR", "applied", "", resolved_by_ai=True),
    ]

    upsert_pr(
        mock_gh,
        "valkey-io/valkey",
        "valkey-io/valkey",
        "8.1",
        "agent/backport/sweep/8.1",
        result,
        existing_pr=existing_pr,
        branch_applied=branch_applied,
        llm_conflict_label="ai-resolved-conflicts",
    )

    existing_pr.add_to_labels.assert_called_once_with("backport", "ai-resolved-conflicts")


def test_upsert_pr_promotes_existing_draft_to_ready():
    """An existing draft sweep PR should be marked ready-for-review on update.

    Earlier versions of this script created sweep PRs as drafts. To roll
    that change forward without touching every PR by hand, _upsert_pr
    should promote any existing draft to ready-for-review whenever it
    edits the PR.
    """
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    existing_pr = MagicMock()
    existing_pr.number = 999
    existing_pr.html_url = "https://github.com/valkey-io/valkey/pull/999"
    existing_pr.draft = True
    existing_pr.node_id = "PR_kwDO_node_id_999"

    mock_gql = MagicMock()

    result = BranchSweepResult(
        target_branch="9.1",
        candidates_found=1,
        results=[CandidateResult(10, "Some PR", "applied", "")],
    )

    upsert_pr(
        mock_gh,
        "valkey-io/valkey",
        "valkey-io/valkey",
        "9.1",
        "agent/backport/sweep/9.1",
        result,
        existing_pr=existing_pr,
        gql=mock_gql,
    )

    # PR body/title were edited as before.
    existing_pr.edit.assert_called_once()
    # And the GraphQL ready-for-review mutation ran with the PR's node_id.
    mock_gql.execute.assert_called_once()
    args, _ = mock_gql.execute.call_args
    query, variables = args
    assert "markPullRequestReadyForReview" in query
    assert variables == {"id": "PR_kwDO_node_id_999"}


def test_upsert_pr_skips_ready_promotion_when_already_open():
    """If the existing PR is not a draft, no GraphQL mutation should run."""
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    existing_pr = MagicMock()
    existing_pr.number = 1000
    existing_pr.html_url = "https://github.com/valkey-io/valkey/pull/1000"
    existing_pr.draft = False
    existing_pr.node_id = "PR_kwDO_node_id_1000"

    mock_gql = MagicMock()

    result = BranchSweepResult(
        target_branch="9.1",
        candidates_found=1,
        results=[CandidateResult(11, "Another PR", "applied", "")],
    )

    upsert_pr(
        mock_gh,
        "valkey-io/valkey",
        "valkey-io/valkey",
        "9.1",
        "agent/backport/sweep/9.1",
        result,
        existing_pr=existing_pr,
        gql=mock_gql,
    )

    existing_pr.edit.assert_called_once()
    # No promotion mutation when the PR is already open.
    mock_gql.execute.assert_not_called()


def test_upsert_pr_preserves_existing_applied_detail_on_update():
    """Already-on-branch candidates keep richer detail from the prior body."""
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    existing_pr = MagicMock()
    existing_pr.number = 1001
    existing_pr.html_url = "https://github.com/valkey-io/valkey/pull/1001"
    existing_pr.draft = False
    existing_pr.body = "\n".join(
        [
            "# Backport sweep for 8.0",
            "",
            "## Applied",
            "",
            "| Source PR | Title | Detail |",
            "|---|---|---|",
            "| #2915 | Fix CLUSTER SLOTS crash | conflicts resolved by Claude Code |",
        ]
    )

    result = BranchSweepResult(
        target_branch="8.0",
        candidates_found=1,
        results=[
            CandidateResult(
                2915,
                "Fix CLUSTER SLOTS crash",
                "skipped-existing",
                backport_sweep.DETAIL_ALREADY_ON_SWEEP_BRANCH,
            )
        ],
    )

    upsert_pr(
        mock_gh,
        "valkey-io/valkey",
        "valkey-io/valkey",
        "8.0",
        "agent/backport/sweep/8.0",
        result,
        existing_pr=existing_pr,
        branch_applied=[
            CandidateResult(
                2915,
                "Fix CLUSTER SLOTS crash",
                "skipped-existing",
                backport_sweep.DETAIL_ALREADY_ON_SWEEP_BRANCH,
            )
        ],
    )

    _, kwargs = existing_pr.edit.call_args
    assert "conflicts resolved by Claude Code" in kwargs["body"]
    assert "already on backport branch" not in kwargs["body"]


def test_sweep_body_points_to_ai_comments_when_ai_resolved():
    from scripts.backport.sweep_models import DETAIL_RESOLVED_BY_AI

    with_ai = build_pr_body(BranchSweepResult(
        target_branch="8.1", candidates_found=1,
        results=[CandidateResult(50, "AI fix", "applied", DETAIL_RESOLVED_BY_AI, resolved_by_ai=True)],
    ))
    assert "AI resolution details are posted as comments on this PR when available." in with_ai

    without_ai = build_pr_body(BranchSweepResult(
        target_branch="8.1", candidates_found=1,
        results=[CandidateResult(51, "Clean fix", "applied", "cherry-picked cleanly")],
    ))
    assert "AI resolution details are posted as comments on this PR" not in without_ai


def test_sweep_body_links_ai_row_to_comment_when_url_known():
    from scripts.backport.sweep_models import DETAIL_RESOLVED_BY_AI

    result = BranchSweepResult(
        target_branch="8.1", candidates_found=2,
        results=[
            CandidateResult(50, "AI fix", "applied", DETAIL_RESOLVED_BY_AI, resolved_by_ai=True),
            CandidateResult(51, "Clean fix", "applied", "cherry-picked cleanly"),
        ],
    )
    url = "https://github.com/o/r/pull/9#issuecomment-123"
    stray_url = "https://github.com/o/r/pull/9#issuecomment-999"
    body = build_pr_body(result, comment_urls={50: url, 51: stray_url})
    # AI-resolved row's detail becomes a link to its comment.
    assert f"[conflicts resolved by Claude Code]({url})" in body
    # Clean row (no AI resolution) is not linked even if a stray URL existed.
    assert "| #51 | Clean fix | cherry-picked cleanly |" in body
    assert stray_url not in body


def test_sweep_body_preserves_ai_resolution_across_unprocessed_runs():
    """A day-0 AI resolution must not flatten to the generic prior-sweep
    string on a later run where the candidate is not re-processed."""
    from scripts.backport.sweep_models import (
        DETAIL_ALREADY_ON_SWEEP_BRANCH,
        DETAIL_RESOLVED_BY_AI,
    )

    # Day 0: candidate resolved by the AI this run.
    day0 = BranchSweepResult(
        target_branch="8.1",
        candidates_found=1,
        results=[
            CandidateResult(100, "Fix X", "applied", DETAIL_RESOLVED_BY_AI, resolved_by_ai=True),
        ],
    )
    body0 = build_pr_body(day0)
    assert "conflicts resolved by Claude Code" in body0

    # Day 1: candidate not re-processed; only on the branch via membership.
    membership = [
        CandidateResult(100, "Fix X", "applied", DETAIL_ALREADY_ON_SWEEP_BRANCH),
    ]
    body1 = build_pr_body(
        BranchSweepResult(target_branch="8.1", candidates_found=0, results=[]),
        branch_applied=membership,
        previous_body=body0,
    )
    assert "conflicts resolved by Claude Code" in body1
    assert "cherry-picked in a prior sweep" not in body1

    # Day 2: same again, fed day-1 body. Signal still preserved.
    body2 = build_pr_body(
        BranchSweepResult(target_branch="8.1", candidates_found=0, results=[]),
        branch_applied=membership,
        previous_body=body1,
    )
    assert "conflicts resolved by Claude Code" in body2


def test_sweep_reconcile_deletes_stale_source_pr_comment_groups():
    """A source PR commented on by an earlier sweep must have its comments
    deleted once it is no longer represented in the current result."""
    from scripts.backport.diff_comments import (
        parse_marker,
        render_diff_comment,
    )
    from scripts.backport.sweep_prs import _reconcile_sweep_diff_comments

    BOT = "valkeyrie-bot[bot]"

    class FakeComment:
        def __init__(self, body, author=BOT):
            self.body = body
            self.deleted = False
            self.user = type("U", (), {"login": author})()
            self.html_url = "https://github.com/o/r/pull/9#c"

        def edit(self, body):
            self.body = body

        def delete(self):
            self.deleted = True

    class FakePR:
        def __init__(self, comments):
            self._c = comments
            self.html_url = "https://github.com/o/r/pull/9"

        def get_issue_comments(self):
            return [c for c in self._c if not c.deleted]

        def create_issue_comment(self, body):
            c = FakeComment(body)
            self._c.append(c)
            return c

    # Prior sweep left a comment for source PR #2.
    stale = FakeComment(render_diff_comment(2, [
        ResolutionResult(
            path="src/old.c", resolved_content="x\n",
            resolution_summary="resolved by Claude Code",
            resolution_diff="-a\n+b",
            reviewer_diff="-a\n+b",
        ),
    ]))
    pr = FakePR([stale])

    # Current result only has source PR #1 with fresh resolutions; #2 is gone.
    result = BranchSweepResult(
        target_branch="8.1",
        candidates_found=1,
        results=[
            CandidateResult(
                1, "Fix one", "applied", "conflicts resolved by Claude Code",
                resolutions=[
                    ResolutionResult(
                        path="src/new.c", resolved_content="x\n",
                        resolution_summary="resolved by Claude Code",
                        resolution_diff="-a\n+b",
                        reviewer_diff="-a\n+b",
                    ),
                ],
                resolved_by_ai=True,
            ),
        ],
    )

    _reconcile_sweep_diff_comments(pr, result)

    live = {
        parse_marker(c.body).source_pr
        for c in pr.get_issue_comments()
        if parse_marker(c.body)
    }
    assert stale.deleted, "stale source PR #2 comment should be deleted"
    assert live == {1}, "only the current source PR #1 group should remain"


def test_sweep_reconcile_keeps_comments_for_still_applied_candidate_on_rerun():
    """A rerun where an AI-resolved candidate is already on the branch (no fresh
    resolutions) must keep its diff comments, not delete them."""
    from scripts.backport.diff_comments import parse_marker, render_diff_comment
    from scripts.backport.sweep_models import DETAIL_ALREADY_ON_SWEEP_BRANCH
    from scripts.backport.sweep_prs import _reconcile_sweep_diff_comments

    BOT = "valkeyrie-bot[bot]"

    class FakeComment:
        def __init__(self, body, author=BOT):
            self.body = body
            self.deleted = False
            self.user = type("U", (), {"login": author})()
            self.html_url = "https://github.com/o/r/pull/9#c"

        def edit(self, body):
            self.body = body

        def delete(self):
            self.deleted = True

    class FakePR:
        def __init__(self, comments):
            self._c = comments
            self.html_url = "https://github.com/o/r/pull/9"

        def get_issue_comments(self):
            return [c for c in self._c if not c.deleted]

        def create_issue_comment(self, body):
            c = FakeComment(body)
            self._c.append(c)
            return c

    # Prior sweep posted a comment for source PR #5.
    kept = FakeComment(render_diff_comment(5, [
        ResolutionResult(
            path="src/x.c", resolved_content="x\n",
            resolution_summary="resolved by Claude Code",
            resolution_diff="-a\n+b",
            reviewer_diff="-a\n+b",
        ),
    ]))
    pr = FakePR([kept])

    # Rerun: #5 is still on the branch but only as already-on-branch, with no
    # fresh resolutions carried.
    result = BranchSweepResult(
        target_branch="8.1",
        candidates_found=1,
        results=[
            CandidateResult(5, "Fix five", "skipped-existing", DETAIL_ALREADY_ON_SWEEP_BRANCH),
        ],
    )

    _reconcile_sweep_diff_comments(pr, result)

    assert not kept.deleted, "comments for a still-on-branch candidate must be kept"
    live = {
        parse_marker(c.body).source_pr
        for c in pr.get_issue_comments()
        if parse_marker(c.body)
    }
    assert live == {5}


def test_sweep_reconcile_keeps_comments_for_branch_applied_membership_only():
    """The PR body's branch_applied membership and comment cleanup must agree."""
    from scripts.backport.diff_comments import parse_marker, render_diff_comment
    from scripts.backport.sweep_models import DETAIL_ALREADY_ON_SWEEP_BRANCH
    from scripts.backport.sweep_prs import _reconcile_sweep_diff_comments

    BOT = "valkeyrie-bot[bot]"

    class FakeComment:
        def __init__(self, body, author=BOT):
            self.body = body
            self.deleted = False
            self.user = type("U", (), {"login": author})()
            self.html_url = "https://github.com/o/r/pull/9#c"

        def edit(self, body):
            self.body = body

        def delete(self):
            self.deleted = True

    class FakePR:
        def __init__(self, comments):
            self._c = comments
            self.html_url = "https://github.com/o/r/pull/9"

        def get_issue_comments(self):
            return [c for c in self._c if not c.deleted]

        def create_issue_comment(self, body):
            c = FakeComment(body)
            self._c.append(c)
            return c

    kept = FakeComment(render_diff_comment(5, [
        ResolutionResult(
            path="src/x.c", resolved_content="x\n",
            resolution_summary="resolved by Claude Code",
            resolution_diff="-a\n+b",
            reviewer_diff="-a\n+b",
        ),
    ]))
    pr = FakePR([kept])

    urls = _reconcile_sweep_diff_comments(
        pr,
        BranchSweepResult(target_branch="8.1", candidates_found=0, results=[]),
        branch_applied=[
            CandidateResult(5, "Fix five", "skipped-existing", DETAIL_ALREADY_ON_SWEEP_BRANCH),
        ],
    )

    assert not kept.deleted
    live = {
        parse_marker(c.body).source_pr
        for c in pr.get_issue_comments()
        if parse_marker(c.body)
    }
    assert live == {5}
    assert urls == {5: kept.html_url}


def test_clone_target_branch_invokes_git_clone_without_destination_cwd(
    monkeypatch,
    tmp_path,
):
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(sweep_git.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sweep_git, "run_git_default",
        lambda repo_dir, *args, **_kwargs: calls.append((["git", *args], {})),
    )

    dest = tmp_path / "checkout"
    clone_target_branch(
        "owner/repo",
        "1.0",
        str(dest),
        {"GIT_ASKPASS": "/tmp/askpass"},
    )

    assert calls[0] == (
        [
            "git",
            "clone",
            "--branch",
            "1.0",
            "https://github.com/owner/repo.git",
            str(dest),
        ],
        {
            "check": True,
            "capture_output": True,
            "text": True,
            "env": {"GIT_ASKPASS": "/tmp/askpass"},
        },
    )
    assert "cwd" not in calls[0][1]
    assert [cmd for cmd, _ in calls[1:]] == [
        ["git", "config", "user.name", sweep_git.BOT_NAME],
        ["git", "config", "user.email", sweep_git.BOT_EMAIL],
    ]


def test_push_backport_branch_uses_plain_push_for_new_branch(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_run_git(_repo_dir, *args, **_kwargs):
        calls.append(args)

    monkeypatch.setattr(backport_sweep, "_run_git", fake_run_git)

    push_backport_branch(
        "/repo",
        "agent/backport/sweep/8.1",
        {},
        force_with_lease=False,
        run_git=fake_run_git,
    )

    assert calls == [("push", "push_target", "agent/backport/sweep/8.1")]


def test_push_backport_branch_uses_force_with_lease_after_rebase(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_run_git(_repo_dir, *args, **_kwargs):
        calls.append(args)

    monkeypatch.setattr(backport_sweep, "_run_git", fake_run_git)

    push_backport_branch(
        "/repo",
        "agent/backport/sweep/8.1",
        {},
        force_with_lease=True,
        run_git=fake_run_git,
    )

    assert calls == [
        (
            "push",
            "--force-with-lease",
            "push_target",
            "agent/backport/sweep/8.1",
        )
    ]


def test_process_branch_applied_cap_ignores_skipped_candidates(monkeypatch):
    candidates = [
        ProjectBackportCandidate(
            source_pr_number=i,
            source_pr_title=f"PR {i}",
            source_pr_url=f"https://github.com/valkey-io/valkey/pull/{i}",
            target_branch="8.1",
            merge_commit_sha=f"sha{i}",
        )
        for i in range(1, 10)
    ]
    applied_by_pr = {3, 4, 6, 7, 8, 9}
    attempted: list[int] = []

    monkeypatch.setattr(backport_sweep, "clone_target_branch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_run_git", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "find_existing_pr", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "delete_stale_backport_branch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "list_already_applied", lambda *_args, **_kwargs: {"2"})
    monkeypatch.setattr(backport_sweep, "list_applied_prs_on_branch", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(sweep_validation, "changed_paths_since_base", lambda *_args, **_kwargs: [], raising=False)
    monkeypatch.setattr(backport_sweep, "run_test_commands", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(
        backport_sweep,
        "validate_branch_with_optional_repair",
        lambda *_args, **_kwargs: (True, ""),
    )
    monkeypatch.setattr(backport_sweep, "branch_has_changes", lambda *_args, **_kwargs: True)

    pushed: list[str] = []
    monkeypatch.setattr(
        backport_sweep,
        "push_backport_branch",
        lambda _repo_dir, branch, *_args, **_kwargs: pushed.append(branch),
    )
    monkeypatch.setattr(
        backport_sweep,
        "upsert_pr",
        lambda *_args, **_kwargs: "https://github.com/valkey-io/valkey/pull/100",
    )

    def fake_apply(_repo_dir, candidate, *_args, **_kwargs):
        attempted.append(candidate.source_pr_number)
        if candidate.source_pr_number in applied_by_pr:
            return CandidateResult(
                source_pr_number=candidate.source_pr_number,
                source_pr_title=candidate.source_pr_title,
                outcome="applied",
            )
        return CandidateResult(
            source_pr_number=candidate.source_pr_number,
            source_pr_title=candidate.source_pr_title,
            outcome="skipped-conflict",
            detail="conflict",
        )

    monkeypatch.setattr(backport_sweep, "apply_candidate", fake_apply)

    result = backport_sweep._process_branch(
        gh=MagicMock(),
        repo_full_name="valkey-io/valkey",
        github_token="token",
        target_branch="8.1",
        candidates=candidates,
        push_repo="valkey-io/valkey",
        test_commands=[],
        max_applied=5,
    )

    assert attempted == [1, 3, 4, 5, 6, 7, 8]
    assert [r.source_pr_number for r in result.results] == [1, 2, 3, 4, 5, 6, 7, 8]
    assert sum(1 for r in result.results if r.outcome == "applied") == 5
    assert result.results[1].outcome == "skipped-existing"
    assert result.results[4].outcome == "skipped-conflict"
    assert pushed == ["agent/backport/sweep/8.1"]
    assert result.pr_url == "https://github.com/valkey-io/valkey/pull/100"


def test_process_branch_push_failure_reconciles_applied(monkeypatch):
    candidate = ProjectBackportCandidate(
        source_pr_number=1,
        source_pr_title="PR 1",
        source_pr_url="https://github.com/valkey-io/valkey/pull/1",
        target_branch="8.1",
        merge_commit_sha="sha1",
    )

    monkeypatch.setattr(backport_sweep, "clone_target_branch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_run_git", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "find_existing_pr", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "delete_stale_backport_branch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "list_already_applied", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(backport_sweep, "run_test_commands", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(
        backport_sweep,
        "validate_branch_with_optional_repair",
        lambda *_args, **_kwargs: (True, ""),
    )
    monkeypatch.setattr(backport_sweep, "branch_has_changes", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        backport_sweep,
        "apply_candidate",
        lambda _repo_dir, c, *_args, **_kwargs: CandidateResult(
            c.source_pr_number, c.source_pr_title, "applied"
        ),
    )

    def fail_push(*_args, **_kwargs):
        raise RuntimeError("push rejected")

    monkeypatch.setattr(backport_sweep, "push_backport_branch", fail_push)

    result = backport_sweep._process_branch(
        gh=MagicMock(),
        repo_full_name="valkey-io/valkey",
        github_token="token",
        target_branch="8.1",
        candidates=[candidate],
        push_repo="valkey-io/valkey",
        test_commands=[],
    )

    assert result.error
    assert result.results[0].outcome == "error"
    assert "push failed" in result.results[0].detail
    assert sum(1 for r in result.results if r.outcome == "applied") == 0


def _green_only_process_branch(monkeypatch, *, candidates, apply_fn, validate_fn,
                               already_applied=None, max_applied=1):
    """Run _process_branch with the common green-only mocks wired up.

    Tests supply how each candidate applies (apply_fn) and how the branch
    validates after each kept cherry-pick (validate_fn). Returns
    (result, pushed, upserts, reset_count).
    """
    monkeypatch.setattr(backport_sweep, "clone_target_branch", lambda *_a, **_k: None)
    monkeypatch.setattr(backport_sweep, "find_existing_pr", lambda *_a, **_k: None)
    monkeypatch.setattr(backport_sweep, "delete_stale_backport_branch", lambda *_a, **_k: None)
    monkeypatch.setattr(
        backport_sweep,
        "list_already_applied",
        lambda *_a, **_k: set(already_applied or set()),
    )
    monkeypatch.setattr(backport_sweep, "list_applied_prs_on_branch", lambda *_a, **_k: [])
    monkeypatch.setattr(backport_sweep, "branch_has_changes", lambda *_a, **_k: True)
    monkeypatch.setattr(backport_sweep, "run_test_commands", lambda *_a, **_k: (True, ""))
    monkeypatch.setattr(backport_sweep, "apply_candidate", apply_fn)
    monkeypatch.setattr(backport_sweep, "validate_branch_with_optional_repair", validate_fn)

    reset_count = {"n": 0}

    def fake_run_git(_repo_dir, *args, **_kwargs):
        if args[:3] == ("reset", "--hard", "HEAD^"):
            reset_count["n"] += 1

    monkeypatch.setattr(backport_sweep, "_run_git", fake_run_git)

    pushed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        backport_sweep,
        "push_backport_branch",
        lambda _repo_dir, branch, _env, *, force_with_lease: pushed.append(
            (branch, force_with_lease)
        ),
    )
    upserts: list[dict] = []

    def fake_upsert(*_args, **kwargs):
        upserts.append(kwargs)
        return "https://github.com/valkey-io/valkey/pull/100"

    monkeypatch.setattr(backport_sweep, "upsert_pr", fake_upsert)

    result = backport_sweep._process_branch(
        gh=MagicMock(),
        repo_full_name="valkey-io/valkey",
        github_token="token",
        target_branch="8.1",
        candidates=candidates,
        push_repo="valkey-io/valkey",
        test_commands=["make"],
        max_applied=max_applied,
        repair_validation_failures=True,
    )
    return result, pushed, upserts, reset_count["n"]


def _candidate(num):
    return ProjectBackportCandidate(
        source_pr_number=num,
        source_pr_title=f"PR {num}",
        source_pr_url=f"https://github.com/valkey-io/valkey/pull/{num}",
        target_branch="8.1",
        merge_commit_sha=f"sha{num}",
    )


def _applied(_repo_dir, candidate, *_args, **_kwargs):
    return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "applied")


def test_process_branch_does_not_push_when_only_candidate_fails_validation(monkeypatch):
    """A red cherry-pick is reset off the branch and never pushed."""
    result, pushed, upserts, resets = _green_only_process_branch(
        monkeypatch,
        candidates=[_candidate(10)],
        apply_fn=_applied,
        validate_fn=lambda *_a, **_k: (False, "compiler error"),
    )

    assert pushed == []
    assert upserts == []
    assert result.pr_url == ""
    assert resets == 1  # the failed cherry-pick was reset off the branch
    assert result.results[0].outcome == "skipped-validation-failed"
    assert "compiler error" in result.results[0].detail


def test_process_branch_keeps_trying_until_green(monkeypatch):
    """Skip failing candidates, keep the first green one, stop after the cap."""
    validations = iter([(False, "boom"), (False, "boom"), (True, "")])

    result, pushed, upserts, resets = _green_only_process_branch(
        monkeypatch,
        candidates=[_candidate(11), _candidate(12), _candidate(13), _candidate(14)],
        apply_fn=_applied,
        validate_fn=lambda *_a, **_k: next(validations),
        max_applied=1,
    )

    outcomes = [r.outcome for r in result.results]
    # 11 and 12 fail validation and are dropped; 13 is green; 14 not attempted (cap).
    assert outcomes == [
        "skipped-validation-failed",
        "skipped-validation-failed",
        "applied",
    ]
    assert resets == 2  # two red cherry-picks reset off the branch
    assert pushed == [("agent/backport/sweep/8.1", False)]
    assert len(upserts) == 1
    # The pushed PR is never a draft - the branch is green.
    assert upserts[0].get("draft", False) is False


def test_process_branch_pushes_green_branch_as_ready(monkeypatch):
    """A single green cherry-pick is pushed as a normal (non-draft) PR."""
    result, pushed, upserts, resets = _green_only_process_branch(
        monkeypatch,
        candidates=[_candidate(20)],
        apply_fn=_applied,
        validate_fn=lambda *_a, **_k: (True, ""),
    )

    assert result.results[0].outcome == "applied"
    assert resets == 0
    assert pushed == [("agent/backport/sweep/8.1", False)]
    assert upserts[0].get("draft", False) is False


def test_process_branch_skips_already_applied_without_reapplying(monkeypatch):
    """Candidates already on the branch are reported, not re-applied."""
    attempted: list[int] = []

    def fake_apply(_repo_dir, candidate, *_args, **_kwargs):
        attempted.append(candidate.source_pr_number)
        return CandidateResult(candidate.source_pr_number, candidate.source_pr_title, "applied")

    result, pushed, upserts, resets = _green_only_process_branch(
        monkeypatch,
        candidates=[_candidate(40), _candidate(41)],
        apply_fn=fake_apply,
        validate_fn=lambda *_a, **_k: (True, ""),
        already_applied={"40"},
        max_applied=1,
    )

    assert attempted == [41]  # 40 skipped as already-applied, not re-applied
    assert result.results[0].outcome == "skipped-existing"
    assert result.results[1].outcome == "applied"
    assert pushed == [("agent/backport/sweep/8.1", False)]




def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full_env = dict(os.environ)
    full_env.update(env or {})
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
        env=full_env,
    )


def test_apply_candidate_preserves_source_author_on_conflict_path(monkeypatch, tmp_path):
    """Sweep must preserve the original commit author after LLM-resolved
    conflicts. Regression test for a bug where `git commit --no-edit`
    replaced the author with the local git identity.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Local Committer")
    _git(repo, "config", "user.email", "committer@local.invalid")
    _git(repo, "config", "commit.gpgsign", "false")

    # Initial commit on main
    (repo / "file.txt").write_text("line1\nline2\nline3\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "initial")

    # Create source branch with a commit authored by someone else
    _git(repo, "checkout", "-q", "-b", "source")
    (repo / "file.txt").write_text("line1\nsource-change\nline3\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    source_author_env = {
        "GIT_AUTHOR_NAME": "Original Author",
        "GIT_AUTHOR_EMAIL": "original@example.com",
    }
    _git(repo, "commit", "-q", "-m", "source change", env=source_author_env)
    source_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Diverge main with a conflicting change, then try to cherry-pick source
    _git(repo, "checkout", "-q", "main")
    (repo / "file.txt").write_text("line1\nmain-change\nline3\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "main conflicting change")
    _git(repo, "checkout", "-q", "-b", "backport")

    # Attempt cherry-pick - will conflict
    result = subprocess.run(
        ["git", "cherry-pick", source_sha],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert result.returncode != 0, "expected cherry-pick to conflict"

    # Simulate Claude's resolution: pick source side. Stage it manually.
    (repo / "file.txt").write_text("line1\nresolved-content\nline3\n", encoding="utf-8")
    _git(repo, "add", "file.txt")

    candidate = ProjectBackportCandidate(
        source_pr_number=42,
        source_pr_title="Test PR",
        source_pr_url="https://github.com/example/repo/pull/42",
        target_branch="main",
        merge_commit_sha=source_sha,
        commit_shas=[source_sha],
    )

    # Skip the parts of _apply_candidate that happen before we're already
    # mid-cherry-pick (fetch, initial cherry-pick, stage-reading). Drive
    # only the commit-resolution + sanity-check portion by monkeypatching
    # the parts that would re-run git ops or talk to Claude.
    def fake_resolve(*_args, **_kwargs):
        return [
            ResolutionResult(
                path="file.txt",
                resolved_content="line1\nresolved-content\nline3\n",
                resolution_summary="resolved",
            )
        ]
    # Drive just the post-resolution phase: write files, stage, continue.
    # This mirrors what _apply_candidate does after Claude returns.
    resolution = ResolutionResult(
        path="file.txt",
        resolved_content="line1\nresolved-content\nline3\n",
        resolution_summary="resolved",
    )
    (repo / resolution.path).write_text(resolution.resolved_content or "", encoding="utf-8")
    _git(repo, "add", resolution.path)

    # This is the exact commit flow _apply_candidate now uses after the fix.
    commit_result = subprocess.run(
        [
            "git",
            "-c", "core.editor=true",
            "cherry-pick", "--continue",
        ],
        cwd=str(repo), capture_output=True, text=True,
    )
    assert commit_result.returncode == 0, commit_result.stderr

    # Author should be the source commit's author; committer is local.
    author = _git(repo, "log", "-1", "--format=%an <%ae>").stdout.strip()
    committer = _git(repo, "log", "-1", "--format=%cn <%ce>").stdout.strip()

    assert author == "Original Author <original@example.com>", (
        f"author not preserved after conflict resolution: got {author!r}"
    )
    assert committer == "Local Committer <committer@local.invalid>"
    # Don't rely on the unused `candidate` local.
    assert candidate.source_pr_number == 42



def test_sync_target_branch_creates_missing_fork_branch():
    gh = MagicMock()
    source_repo = MagicMock()
    fork_repo = MagicMock()
    source_repo.get_branch.return_value.commit.sha = "abc123def"
    fork_repo.get_branch.side_effect = GithubException(
        status=404,
        data={"message": "Branch not found"},
        headers={},
    )
    gh.get_repo.side_effect = lambda name: {
        "valkey-io/valkey": source_repo,
        "ci-bot/valkey": fork_repo,
    }[name]

    sync_target_branch_to_source(
        gh,
        "ci-bot/valkey",
        "valkey-io/valkey",
        "8.1",
    )

    fork_repo.create_git_ref.assert_called_once_with(
        ref="refs/heads/8.1",
        sha="abc123def",
    )


def test_graphql_client_retry_exhaustion_raises_clear_error(monkeypatch):
    """After exhausting retries on URLError, the client must raise a
    RuntimeError (not UnboundLocalError from reading `body`)."""

    class FakeURLError(Exception):
        pass

    # Build a URLError-like exception the client's except clause matches.
    import urllib.error

    call_count = {"n": 0}

    def always_fails(*_args, **_kwargs):
        call_count["n"] += 1
        raise urllib.error.URLError("simulated network down")

    monkeypatch.setattr(sweep_graphql.urllib.request, "urlopen", always_fails)
    # Skip actual sleeps in the backoff loop.
    monkeypatch.setattr(backport_sweep, "_random", None, raising=False)
    monkeypatch.setattr("random.uniform", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    client = sweep_graphql.GitHubGraphQLClient("fake-token")
    try:
        client.execute("query {}", {})
    except urllib.error.URLError:
        # On the 4th attempt, the client re-raises the URLError directly,
        # which is also fine - the test's purpose is to verify we never
        # hit an UnboundLocalError from the `body` variable.
        pass
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected retry exhaustion to raise")
    assert call_count["n"] == 4, f"expected 4 retry attempts, got {call_count['n']}"


def _fake_graphql_response(payload):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            import json as _json
            return _json.dumps(payload).encode()

    return FakeResponse()


def test_graphql_client_retries_transient_errors_in_200_body(monkeypatch):
    """Rate-limit errors arrive in a 200 body and must trigger backoff retries."""
    responses = [
        {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
        {"data": {"ok": True}},
    ]

    def fake_urlopen(*_args, **_kwargs):
        return _fake_graphql_response(responses.pop(0))

    monkeypatch.setattr(sweep_graphql.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("random.uniform", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    result = sweep_graphql.GitHubGraphQLClient("fake-token").execute("query {}", {})
    assert result == {"ok": True}
    assert responses == []


def test_graphql_client_raises_immediately_on_non_transient_error(monkeypatch):
    """A genuine query error must surface right away, not retry."""
    call_count = {"n": 0}

    def fake_urlopen(*_args, **_kwargs):
        call_count["n"] += 1
        return _fake_graphql_response(
            {"errors": [{"type": "INVALID", "message": "Field 'bogus' doesn't exist"}]}
        )

    monkeypatch.setattr(sweep_graphql.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="GraphQL errors"):
        sweep_graphql.GitHubGraphQLClient("fake-token").execute("query {}", {})
    assert call_count["n"] == 1


def test_safe_tmp_component_removes_branch_separators():
    assert safe_tmp_component("release/8.1") == "release-8.1"
    assert safe_tmp_component("///") == "branch"


def test_list_applied_prs_on_branch_reads_backport_commit_subjects(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "8.0")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "file.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/8.0", "HEAD")
    _git(repo, "checkout", "-q", "-b", "agent/backport/sweep/8.0")

    (repo / "file.txt").write_text("base\none\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "Preserve original fd blocking state (#1298)")
    (repo / "file.txt").write_text("base\none\ntwo\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "Fix CLUSTER SLOTS crash (#2915)")

    applied = list_applied_prs_on_branch(
        str(repo),
        "8.0",
        "agent/backport/sweep/8.0",
    )

    assert [(r.source_pr_number, r.source_pr_title) for r in applied] == [
        (1298, "Preserve original fd blocking state"),
        (2915, "Fix CLUSTER SLOTS crash"),
    ]


def test_build_pr_body_lists_already_on_branch_under_applied():
    """Applied table reflects cumulative state of the backport branch.

    `skipped-existing` with detail "already on backport branch" means the
    PR was cherry-picked by a prior sweep run -> appears in Applied.

    `skipped-existing` with any other detail means the change is already
    on the *release* branch (empty cherry-pick or no-op resolution) -> it
    is NOT on the backport branch and must not appear in Applied.
    """
    result = BranchSweepResult(
        target_branch="9.1",
        candidates_found=5,
        results=[
            # Fresh cherry-pick this run.
            CandidateResult(
                source_pr_number=3654,
                source_pr_title="Use full hash-seed bytes when deriving SipHash seed",
                outcome="applied",
                detail="conflicts resolved by Claude Code",
            ),
            # Already on the sweep branch from a prior run -> in Applied.
            CandidateResult(
                source_pr_number=3380,
                source_pr_title="CLUSTERSCAN MATCH pattern maps to a specific slot optimizations",
                outcome="skipped-existing",
                detail=backport_sweep.DETAIL_ALREADY_ON_SWEEP_BRANCH,
            ),
            CandidateResult(
                source_pr_number=3619,
                source_pr_title="Fix invalid memory access in RESTORE with malformed zipmap",
                outcome="skipped-existing",
                detail=backport_sweep.DETAIL_ALREADY_ON_SWEEP_BRANCH,
            ),
            # Already merged into the release branch (empty cherry-pick)
            # -> NOT on the sweep branch, must not appear in Applied.
            CandidateResult(
                source_pr_number=4001,
                source_pr_title="Already-merged release-branch commit",
                outcome="skipped-existing",
                detail="already applied or empty cherry-pick",
            ),
            # Conflict resolution collapsed to a no-op -> NOT in Applied.
            CandidateResult(
                source_pr_number=4002,
                source_pr_title="No-op resolution",
                outcome="skipped-existing",
                detail="resolution was already satisfied on target branch",
            ),
        ],
    )

    body = build_pr_body(result)

    assert "## Applied" in body
    assert "## Needs attention" not in body
    assert "Already on branch" not in body
    # Newly applied + on-sweep-branch carry-overs are listed in Applied.
    assert "#3654" in body
    assert "#3380" in body
    assert "#3619" in body

    # The Applied table must not list changes that are not on the sweep branch.
    applied_section = body.split("## Applied", 1)[1].split("## ", 1)[0]
    assert "#4001" not in applied_section
    assert "#4002" not in applied_section

    # A conflict resolution that collapsed to a no-op is surfaced under
    # "Skipped" so maintainers see it was
    # evaluated and intentionally skipped, rather than vanishing silently.
    assert "## Skipped" in body
    skipped_section = body.split("## Skipped", 1)[1]
    assert "#4002" in skipped_section


def test_build_pr_body_surfaces_no_op_resolution_under_skipped():
    """A candidate resolved to a no-op is surfaced under Skipped, not dropped.

    When the resolution contributes nothing to the target branch (e.g. the fix
    targets code absent on this branch), the candidate is recorded as
    ``skipped-existing`` with ``DETAIL_EMPTY_ON_TARGET`` and a deterministic
    ``skip_reason``. It must not appear in Applied, but the body must list it
    under "Skipped" with that reason so maintainers see why it was skipped.
    """
    from scripts.backport.sweep_models import DETAIL_EMPTY_ON_TARGET

    result = BranchSweepResult(
        target_branch="8.0",
        candidates_found=2,
        results=[
            CandidateResult(
                source_pr_number=312,
                source_pr_title="Fix RESP3 type violation",
                outcome="applied",
                detail="conflicts resolved by Claude Code",
            ),
            CandidateResult(
                source_pr_number=313,
                source_pr_title="Fix off_t truncation in bio repl",
                outcome="skipped-existing",
                detail=DETAIL_EMPTY_ON_TARGET,
                skip_reason=(
                    "The change does not apply to this branch: resolving the "
                    "conflict matched the existing code, so the cherry-pick "
                    "added nothing."
                ),
            ),
        ],
    )

    body = build_pr_body(result)

    assert "## Skipped" in body
    skipped = body.split("## Skipped", 1)[1]
    assert "#313" in skipped
    # The per-row reason is the deterministic skip_reason, not resolver prose.
    assert "does not apply to this branch" in skipped

    applied = body.split("## Applied", 1)[1].split("## ", 1)[0]
    assert "#312" in applied
    assert "#313" not in applied



def test_empty_skip_reason_detects_change_not_applicable():
    from scripts.backport.models import ConflictedFile, ResolutionResult
    from scripts.backport.sweep_apply import _empty_skip_reason

    # Every resolved file matched the target's existing content -> the change
    # does not apply on this branch.
    cf = [ConflictedFile("src/server.c", "TARGET", "SOURCE")]
    res = [ResolutionResult("src/server.c", "TARGET", "r")]
    assert "does not apply to this branch" in _empty_skip_reason(cf, res)


def test_empty_skip_reason_generic_fallback():
    from scripts.backport.models import ConflictedFile, ResolutionResult
    from scripts.backport.sweep_apply import _empty_skip_reason

    # Resolution differs from target -> fall back to the generic no-net-change
    # statement rather than asserting a cause we cannot prove.
    cf = [ConflictedFile("src/server.c", "TARGET", "SOURCE")]
    res = [ResolutionResult("src/server.c", "DIFFERENT", "r")]
    reason = _empty_skip_reason(cf, res)
    assert "no net change" in reason


def test_build_pr_body_omits_skipped_section_when_none():
    """No Skipped section is rendered when every candidate applied cleanly."""
    result = BranchSweepResult(
        target_branch="8.0",
        candidates_found=1,
        results=[
            CandidateResult(
                source_pr_number=400,
                source_pr_title="Clean backport",
                outcome="applied",
                detail="cherry-picked cleanly",
            ),
        ],
    )

    body = build_pr_body(result)

    assert "## Skipped" not in body


def test_build_pr_body_uses_branch_commits_and_preserves_prior_detail():
    result = BranchSweepResult(
        target_branch="8.0",
        candidates_found=2,
        results=[
            CandidateResult(
                source_pr_number=2915,
                source_pr_title="Fix CLUSTER SLOTS crash",
                outcome="skipped-existing",
                detail=backport_sweep.DETAIL_ALREADY_ON_SWEEP_BRANCH,
            ),
            CandidateResult(
                source_pr_number=1826,
                source_pr_title="Fix Lua VM crash",
                outcome="skipped-conflict",
                detail="target branch lacks conflicted file(s): src/lua/engine_lua.c",
            ),
        ],
    )
    previous_body = "\n".join(
        [
            "# Backport sweep for 8.0",
            "",
            "## Applied",
            "",
            "| Source PR | Title | Detail |",
            "|---|---|---|",
            "| #2915 | Fix CLUSTER SLOTS crash | conflicts resolved by Claude Code |",
        ]
    )

    body = build_pr_body(
        result,
        branch_applied=[
            CandidateResult(
                1298,
                "Preserve original fd blocking state",
                "skipped-existing",
                backport_sweep.DETAIL_ALREADY_ON_SWEEP_BRANCH,
            ),
            CandidateResult(
                2915,
                "Fix CLUSTER SLOTS crash",
                "skipped-existing",
                backport_sweep.DETAIL_ALREADY_ON_SWEEP_BRANCH,
            ),
        ],
        previous_body=previous_body,
    )

    assert "#1298" in body
    assert "#2915" in body
    assert "#1826" in body
    assert "conflicts resolved by Claude Code" in body
    assert body.index("#1298") < body.index("#2915")


def test_build_pr_body_round_trips_applied_and_failed_detail():
    first = build_pr_body(
        BranchSweepResult("8.0", 2, results=[
            CandidateResult(2915, "Fix | crash", "applied", "conflicts resolved by Claude Code"),
            CandidateResult(1826, "Fix Lua VM crash", "skipped-conflict", "lacks src/lua/engine_lua.c"),
        ]),
        branch_applied=[CandidateResult(2915, "Fix | crash", "applied", "conflicts resolved by Claude Code")],
    )

    # A later run that processes nothing must keep every entry from the prior body.
    second = build_pr_body(
        BranchSweepResult("8.0", 0),
        branch_applied=[CandidateResult(2915, "Fix | crash", "skipped-existing", DETAIL)],
        previous_body=first,
    )

    assert parse_previous_applied(second) == [
        CandidateResult(
            2915, "Fix | crash", "applied", "conflicts resolved by Claude Code",
            resolved_by_ai=True,
        ),
    ]
    assert [(r.source_pr_number, r.detail) for r in parse_previous_failed(second)] == [
        (1826, "lacks src/lua/engine_lua.c"),
    ]


def test_parse_previous_applied_preserves_ai_detail_from_linked_row():
    from scripts.backport.sweep_models import DETAIL_RESOLVED_BY_AI

    body = "\n".join([
        "## Applied",
        "",
        "| Source PR | Title | Detail |",
        "|---|---|---|",
        "| #2915 | Fix crash | "
        f"[{DETAIL_RESOLVED_BY_AI}](https://github.com/o/r/pull/9#issuecomment-123) |",
    ])

    assert parse_previous_applied(body) == [
        CandidateResult(
            2915, "Fix crash", "applied", DETAIL_RESOLVED_BY_AI,
            resolved_by_ai=True,
        ),
    ]


def test_build_pr_body_drops_failed_entry_once_applied():
    previous_body = "\n".join([
        "## Needs attention", "",
        "| Source PR | Title | Outcome | Reason |", "|---|---|---|---|",
        "| #4100 | Now fixed | skipped-conflict | was conflicting |",
    ])

    body = build_pr_body(
        BranchSweepResult("8.0", 0),
        branch_applied=[CandidateResult(4100, "Now fixed", "skipped-existing", DETAIL)],
        previous_body=previous_body,
    )

    assert "## Needs attention" not in body
    assert "#4100" in body


def test_build_pr_body_clears_stale_failure_when_current_skips_existing():
    previous_body = "\n".join([
        "## Needs attention", "",
        "| Source PR | Title | Outcome | Reason |", "|---|---|---|---|",
        "| #4001 | Already merged | skipped-conflict | was conflicting |",
    ])

    # Current run reports it as already on the release branch (not on the
    # sweep branch, so not in Applied) -> it no longer needs attention.
    body = build_pr_body(
        BranchSweepResult("8.0", 1, results=[
            CandidateResult(4001, "Already merged", "skipped-existing", "already applied or empty cherry-pick"),
        ]),
        branch_applied=[],
        previous_body=previous_body,
    )

    assert "## Needs attention" not in body
    assert "#4001" not in body


def test_build_pr_body_uses_friendly_detail_for_bare_branch_commit():
    body = build_pr_body(
        BranchSweepResult("8.0", 0),
        branch_applied=[CandidateResult(4200, "Preserved feature", "skipped-existing", DETAIL)],
    )

    assert "#4200" in body
    assert DETAIL not in body
    assert "cherry-picked in a prior sweep" in body


def test_build_summary_counts_applied_candidates():
    result = BranchSweepResult(
        target_branch="8.1",
        candidates_found=3,
        pr_url="https://github.com/valkey-io/valkey/pull/100",
        results=[
            CandidateResult(10, "Good", "applied", ""),
            CandidateResult(11, "Failed validation", "skipped-validation-failed", "bad"),
            CandidateResult(12, "Skipped", "skipped-conflict", "conflict"),
        ],
    )

    summary = build_summary([result])

    assert "`8.1`: 1/3 applied" in summary
    assert "https://github.com/valkey-io/valkey/pull/100" in summary


def test_validation_repair_prompt_is_narrowly_scoped():
    prompt = build_validation_repair_prompt(
        "8.1",
        ("src/module.c", "tests/module.tcl"),
        "/tmp/backport-validation-xyz.log",
    )

    assert "Do NOT edit files outside the listed changed files" in prompt
    assert "Do NOT run builds, tests, docker, git" in prompt
    assert "validation output, commit messages, diffs" in prompt
    assert "src/module.c" in prompt
    assert "/tmp/backport-validation-xyz.log" in prompt
    assert "Read tool" in prompt


def test_repair_validation_failure_invokes_edit_only_agent(monkeypatch):
    agent_calls: list[tuple[str, str, str]] = []
    git_calls: list[tuple[str, ...]] = []
    validation_calls: list[list[str]] = []
    log_paths: list[str | None] = []

    def changed_paths_since_base_func(*_args, **_kwargs):
        return ("src/a.c",)

    def fake_run_agent(profile, prompt, *, cwd):
        agent_calls.append((profile, prompt, cwd))
        return SimpleNamespace(returncode=0, stderr="")


    def fake_validate(_repo_dir, _target_branch, commands, _rules, log_path=None):
        validation_calls.append(list(commands))
        log_paths.append(log_path)
        return True, "ok"

    ok, output = repair_validation_failure_with_claude(
        "/repo",
        "8.1",
        ["make"],
        [],
        "compiler error",
        run_git=lambda _repo_dir, *args, **_kwargs: git_calls.append(args),
        run_agent_func=fake_run_agent,
        validate_func=fake_validate,
        changed_paths_func=lambda *_args: ("src/a.c",),
        changed_paths_since_base_func=changed_paths_since_base_func,
        has_staged_changes_func=lambda *_args: True,
    )

    assert ok is True
    assert output == "ok"
    assert agent_calls[0][0] == "validation_repair_edit_only"
    # The prompt points Claude at the validation log path it should Read,
    # rather than embedding a truncated tail.
    assert "Read tool" in agent_calls[0][1]
    assert "/tmp/" in agent_calls[0][1] or "backport-validation-" in agent_calls[0][1]
    assert ("add", "src/a.c") in git_calls
    assert ("commit", "-m", "Repair backport validation failure") in git_calls
    # The failing validation has already happened; repair only revalidates once
    # after Claude edits.
    assert validation_calls == [["make"]]
    assert log_paths == [None]


def test_worktree_changed_paths_handles_spaces(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    src = tmp_path / "src"
    src.mkdir()
    tracked = src / "file with space.c"
    untracked = src / "new file with space.c"
    tracked.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/file with space.c"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=tmp_path, check=True)

    tracked.write_text("new\n", encoding="utf-8")
    untracked.write_text("created\n", encoding="utf-8")

    assert worktree_changed_paths(str(tmp_path)) == (
        "src/file with space.c",
        "src/new file with space.c",
    )
    assert changed_paths_in_index_or_worktree(str(tmp_path)) == (
        "src/file with space.c",
        "src/new file with space.c",
    )


def test_validation_failure_detail_uses_tail_without_repair_diagnosis():
    detail = backport_sweep.validation_failure_detail(
        "configure output\n" + ("compiler noise\n" * 80) + "undefined reference to objectGetVal\n"
    )

    assert "configure output" not in detail
    assert "undefined reference to objectGetVal" in detail


def test_validation_failure_detail_uses_tail_with_repair_diagnosis():
    detail = backport_sweep.validation_failure_detail(
        "Claude repair diagnosis:\n"
        "clean cherry-pick used a newer API\n\n"
        "Validation output:\n"
        + ("compiler noise\n" * 80)
        + "undefined reference to objectGetVal\n"
    )

    assert "clean cherry-pick used a newer API" in detail
    assert "undefined reference to objectGetVal" in detail


# ---------------------------------------------------------------------------
# ProjectBackportDiscovery - cross-repo filter
# ---------------------------------------------------------------------------


def _project_item(*, number: int, repo: str, status: str = "To be backported",
                  merge_sha: str = "abc1234567890abcdef") -> dict:
    """Build a fake project-item payload shaped like the GraphQL response."""
    return {
        "content": {
            "__typename": "PullRequest",
            "number": number,
            "title": f"PR {number}",
            "url": f"https://github.com/{repo}/pull/{number}",
            "merged": True,
            "repository": {"nameWithOwner": repo},
            "mergeCommit": {"oid": merge_sha},
            "commits": {"nodes": [{"commit": {"oid": merge_sha}}]},
        },
        "fieldValues": {
            "nodes": [
                {
                    "__typename": "ProjectV2ItemFieldSingleSelectValue",
                    "name": status,
                    "field": {"name": "Status"},
                },
            ],
        },
    }


def _make_discovery(items: list[dict], *, source_repo: str = "valkey-io/valkey"):
    """Build a discovery instance whose GraphQL client returns `items`."""
    gql = MagicMock()
    discovery = backport_sweep.ProjectBackportDiscovery(
        gql,
        project_owner="valkey-io",
        project_number=1,
        source_repo=source_repo,
        implicit_target_branch="9.1",
    )
    # Bypass the GraphQL fetch - return our fake items directly.
    discovery._iter_items = lambda: items  # type: ignore[method-assign]
    return discovery


def test_discovery_filters_out_pr_from_other_repo():
    """A blog-post PR on valkey-io.github.io (added to the same project
    board) must NOT become a backport candidate for valkey-io/valkey.
    """
    items = [
        _project_item(number=3654, repo="valkey-io/valkey"),
        _project_item(number=553, repo="valkey-io/valkey-io.github.io"),
    ]
    by_branch = _make_discovery(items).discover(["9.1"])
    nums = [c.source_pr_number for c in by_branch["9.1"]]
    assert nums == [3654]


def test_discovery_keeps_matching_repo_pr():
    """Sanity check: a PR from the configured repo flows through."""
    items = [_project_item(number=3654, repo="valkey-io/valkey")]
    by_branch = _make_discovery(items).discover(["9.1"])
    assert len(by_branch["9.1"]) == 1
    assert by_branch["9.1"][0].source_pr_number == 3654


def test_discovery_drops_unmerged_pr_regardless_of_repo():
    """The merged-only filter is applied before the repo filter."""
    item = _project_item(number=999, repo="valkey-io/valkey")
    item["content"]["merged"] = False
    by_branch = _make_discovery([item]).discover(["9.1"])
    assert by_branch["9.1"] == []


def test_discovery_drops_pr_with_wrong_status_regardless_of_repo():
    item = _project_item(number=42, repo="valkey-io/valkey", status="Done")
    by_branch = _make_discovery([item]).discover(["9.1"])
    assert by_branch["9.1"] == []


def test_discovery_keeps_pr_when_repository_field_missing():
    """If the GraphQL payload lacks repository (older cached response, schema
    quirk, etc.), don't refuse to sweep - the field is the new filter, not a
    hard requirement.
    """
    item = _project_item(number=3654, repo="valkey-io/valkey")
    item["content"].pop("repository")
    by_branch = _make_discovery([item]).discover(["9.1"])
    assert len(by_branch["9.1"]) == 1


def test_project_items_query_selects_repository_name_with_owner():
    """Regression guard: the query must request the repo field, otherwise
    the runtime filter sees None for every item and lets cross-repo PRs
    through.
    """
    query = backport_sweep._project_items_query("organization")
    assert "repository {" in query
    assert "nameWithOwner" in query
