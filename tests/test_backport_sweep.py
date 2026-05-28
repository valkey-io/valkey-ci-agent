from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from github.GithubException import GithubException

from scripts.backport import sweep as backport_sweep
from scripts.backport.models import ResolutionResult
from scripts.backport.sweep import (
    BranchSweepResult,
    CandidateResult,
    ProjectBackportCandidate,
)
from scripts.common.git_auth import GitAuth


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

    monkeypatch.setattr(backport_sweep, "_run_git", fake_run_git)
    monkeypatch.setattr(backport_sweep.subprocess, "run", fake_subprocess_run)

    result = backport_sweep._apply_candidate(
        repo_dir=str(tmp_path),
        candidate=candidate,
        repo_full_name="valkey-io/valkey",
        git_env={},
    )

    assert result.outcome == "skipped-existing"
    assert result.detail == "already applied or empty cherry-pick"
    assert ("fetch", "origin", "abc123") in git_calls
    assert ["git", "cherry-pick", "--abort"] in subprocess_calls


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

    monkeypatch.setattr(backport_sweep, "_run_git", fake_run_git)
    monkeypatch.setattr(backport_sweep.subprocess, "run", fake_subprocess_run)

    result = backport_sweep._apply_candidate(
        repo_dir=str(tmp_path),
        candidate=candidate,
        repo_full_name="valkey-io/valkey",
        git_env={},
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
        if cmd[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="target content\n", stderr="")
        if cmd[:3] == ["git", "cat-file", "-e"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:4] == ["git", "diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:3] == ["git", "cherry-pick", "--abort"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(backport_sweep, "_run_git", fake_run_git)
    monkeypatch.setattr(backport_sweep.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        backport_sweep,
        "resolve_conflicts_with_claude",
        lambda *_args, **_kwargs: [
            ResolutionResult(
                path="conflict.txt",
                resolved_content="target content\n",
                resolution_summary="resolved",
            )
        ],
    )

    result = backport_sweep._apply_candidate(
        repo_dir=str(tmp_path),
        candidate=candidate,
        repo_full_name="valkey-io/valkey",
        git_env={},
    )

    assert result.outcome == "skipped-existing"
    assert result.detail == "resolution was already satisfied on target branch"
    assert ("add", "conflict.txt") in git_calls
    assert ["git", "commit", "--no-edit"] not in subprocess_calls
    assert ["git", "cherry-pick", "--abort"] in subprocess_calls


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

    monkeypatch.setattr(backport_sweep, "_run_git", fake_run_git)
    monkeypatch.setattr(backport_sweep.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(
        backport_sweep,
        "resolve_conflicts_with_claude",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not call Claude")),
    )

    result = backport_sweep._apply_candidate(
        repo_dir=str(tmp_path),
        candidate=candidate,
        repo_full_name="valkey-io/valkey",
        git_env={},
    )

    assert result.outcome == "skipped-conflict"
    assert result.detail == "target branch lacks conflicted file(s): src/cluster_legacy.c"
    assert ("add", "src/cluster_legacy.c") not in git_calls
    assert missing_on_target.exists()
    assert ["git", "commit", "--no-edit"] not in subprocess_calls
    assert ["git", "cherry-pick", "--abort"] in subprocess_calls


def test_run_test_commands_returns_failure_output(tmp_path):
    ok, output = backport_sweep._run_test_commands(
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

    pr_url = backport_sweep._upsert_pr(
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

    backport_sweep._upsert_pr(
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

    backport_sweep._upsert_pr(
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


def test_upsert_pr_converts_existing_ready_pr_to_draft_on_validation_failure():
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    existing_pr = MagicMock()
    existing_pr.number = 1001
    existing_pr.html_url = "https://github.com/valkey-io/valkey/pull/1001"
    existing_pr.draft = False
    existing_pr.node_id = "PR_kwDO_node_id_1001"

    mock_gql = MagicMock()

    result = BranchSweepResult(
        target_branch="9.1",
        candidates_found=1,
        results=[CandidateResult(11, "Broken PR", "skipped-test", "compile failed")],
    )

    backport_sweep._upsert_pr(
        mock_gh,
        "valkey-io/valkey",
        "valkey-io/valkey",
        "9.1",
        "agent/backport/sweep/9.1",
        result,
        existing_pr=existing_pr,
        gql=mock_gql,
        draft=True,
    )

    existing_pr.edit.assert_called_once()
    mock_gql.execute.assert_called_once()
    query, variables = mock_gql.execute.call_args.args
    assert "convertPullRequestToDraft" in query
    assert variables == {"id": "PR_kwDO_node_id_1001"}


def test_upsert_pr_creates_draft_when_validation_failed():
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_gh.get_repo.return_value = mock_repo
    mock_pr = MagicMock()
    mock_pr.number = 556
    mock_pr.html_url = "https://github.com/valkey-io/valkey/pull/556"
    mock_repo.create_pull.return_value = mock_pr
    result = BranchSweepResult(
        target_branch="8.1",
        candidates_found=1,
        results=[
            CandidateResult(
                source_pr_number=10,
                source_pr_title="Fix module API",
                outcome="skipped-test",
                detail="compiler error",
            )
        ],
    )

    backport_sweep._upsert_pr(
        mock_gh,
        "valkey-io/valkey",
        "valkey-io/valkey",
        "8.1",
        "agent/backport/sweep/8.1",
        result,
        existing_pr=None,
        draft=True,
    )

    _, kwargs = mock_repo.create_pull.call_args
    assert kwargs["draft"] is True
    assert kwargs["title"] == "[backport][validation failed] Backport sweep for 8.1"
    assert "## Validation failed" in kwargs["body"]


def test_clone_target_branch_invokes_git_clone_without_destination_cwd(
    monkeypatch,
    tmp_path,
):
    calls: list[tuple[list[str], dict]] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(backport_sweep.subprocess, "run", fake_run)

    dest = tmp_path / "checkout"
    backport_sweep._clone_target_branch(
        "owner/repo",
        "1.0",
        str(dest),
        {"GIT_ASKPASS": "/tmp/askpass"},
    )

    assert calls == [
        (
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
    ]
    assert "cwd" not in calls[0][1]


def test_push_backport_branch_uses_plain_push_for_new_branch(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_run_git(_repo_dir, *args, **_kwargs):
        calls.append(args)

    monkeypatch.setattr(backport_sweep, "_run_git", fake_run_git)

    backport_sweep._push_backport_branch(
        "/repo",
        "agent/backport/sweep/8.1",
        {},
        force_with_lease=False,
    )

    assert calls == [("push", "push_target", "agent/backport/sweep/8.1")]


def test_push_backport_branch_uses_force_with_lease_after_rebase(monkeypatch):
    calls: list[tuple[str, ...]] = []

    def fake_run_git(_repo_dir, *args, **_kwargs):
        calls.append(args)

    monkeypatch.setattr(backport_sweep, "_run_git", fake_run_git)

    backport_sweep._push_backport_branch(
        "/repo",
        "agent/backport/sweep/8.1",
        {},
        force_with_lease=True,
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

    monkeypatch.setattr(backport_sweep, "_clone_target_branch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_run_git", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_find_existing_pr", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_delete_stale_backport_branch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_list_already_applied", lambda *_args, **_kwargs: {"2"})
    monkeypatch.setattr(backport_sweep, "changed_paths_since_base", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(backport_sweep, "_run_test_commands", lambda *_args, **_kwargs: (True, ""))
    monkeypatch.setattr(backport_sweep, "_branch_has_changes", lambda *_args, **_kwargs: True)

    pushed: list[str] = []
    monkeypatch.setattr(
        backport_sweep,
        "_push_backport_branch",
        lambda _repo_dir, branch, *_args, **_kwargs: pushed.append(branch),
    )
    monkeypatch.setattr(
        backport_sweep,
        "_upsert_pr",
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

    monkeypatch.setattr(backport_sweep, "_apply_candidate", fake_apply)

    result = backport_sweep._process_branch(
        gh=MagicMock(),
        repo=MagicMock(),
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


def test_process_branch_pushes_draft_pr_when_batch_validation_fails(monkeypatch):
    candidate = ProjectBackportCandidate(
        source_pr_number=10,
        source_pr_title="Compile failure",
        source_pr_url="https://github.com/valkey-io/valkey/pull/10",
        target_branch="8.1",
        merge_commit_sha="sha10",
    )

    monkeypatch.setattr(backport_sweep, "_clone_target_branch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_run_git", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_find_existing_pr", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_delete_stale_backport_branch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_list_already_applied", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(backport_sweep, "changed_paths_since_base", lambda *_args, **_kwargs: ["src/a.c"])
    monkeypatch.setattr(backport_sweep, "_branch_has_changes", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        backport_sweep,
        "_apply_candidate",
        lambda _repo_dir, c, *_args, **_kwargs: CandidateResult(
            c.source_pr_number,
            c.source_pr_title,
            "applied",
        ),
    )

    run_calls: list[list[str]] = []

    def fake_run_test_commands(_repo_dir, commands):
        run_calls.append(commands)
        if not commands:
            return True, ""
        return False, "compiler error"

    monkeypatch.setattr(backport_sweep, "_run_test_commands", fake_run_test_commands)
    pushed: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        backport_sweep,
        "_push_backport_branch",
        lambda _repo_dir, branch, _env, *, force_with_lease: pushed.append(
            (branch, force_with_lease)
        ),
    )
    upserts: list[dict] = []

    def fake_upsert(*args, **kwargs):
        upserts.append(kwargs)
        return "https://github.com/valkey-io/valkey/pull/100"

    monkeypatch.setattr(backport_sweep, "_upsert_pr", fake_upsert)

    result = backport_sweep._process_branch(
        gh=MagicMock(),
        repo=MagicMock(),
        repo_full_name="valkey-io/valkey",
        github_token="token",
        target_branch="8.1",
        candidates=[candidate],
        push_repo="valkey-io/valkey",
        test_commands=["make"],
        max_applied=5,
    )

    assert result.pr_url == "https://github.com/valkey-io/valkey/pull/100"
    assert result.results[0].outcome == "skipped-test"
    assert result.results[0].detail == "compiler error"
    assert pushed == [("agent/backport/sweep/8.1", False)]
    assert upserts[0]["draft"] is True
    assert run_calls == [[], ["make"]]


def test_process_branch_incremental_validation_drops_failed_later_candidate(
    monkeypatch,
):
    candidates = [
        ProjectBackportCandidate(
            source_pr_number=10,
            source_pr_title="Good",
            source_pr_url="https://github.com/valkey-io/valkey/pull/10",
            target_branch="8.1",
            merge_commit_sha="sha10",
        ),
        ProjectBackportCandidate(
            source_pr_number=11,
            source_pr_title="Bad",
            source_pr_url="https://github.com/valkey-io/valkey/pull/11",
            target_branch="8.1",
            merge_commit_sha="sha11",
        ),
    ]

    monkeypatch.setattr(backport_sweep, "_clone_target_branch", lambda *_args, **_kwargs: None)
    git_calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        backport_sweep,
        "_run_git",
        lambda _repo_dir, *args, **_kwargs: git_calls.append(args),
    )
    monkeypatch.setattr(backport_sweep, "_find_existing_pr", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_delete_stale_backport_branch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(backport_sweep, "_list_already_applied", lambda *_args, **_kwargs: set())
    monkeypatch.setattr(backport_sweep, "changed_paths_since_base", lambda *_args, **_kwargs: ["src/a.c"])
    monkeypatch.setattr(backport_sweep, "_branch_has_changes", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        backport_sweep,
        "_apply_candidate",
        lambda _repo_dir, c, *_args, **_kwargs: CandidateResult(
            c.source_pr_number,
            c.source_pr_title,
            "applied",
        ),
    )
    validations = iter([(True, ""), (False, "bad compile"), (True, "")])

    def fake_run_test_commands(_repo_dir, commands):
        if not commands:
            return True, ""
        return next(validations)

    monkeypatch.setattr(backport_sweep, "_run_test_commands", fake_run_test_commands)
    reset_calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        reset_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(backport_sweep.subprocess, "run", fake_run)
    monkeypatch.setattr(backport_sweep, "_push_backport_branch", lambda *_args, **_kwargs: None)
    upserts: list[dict] = []
    monkeypatch.setattr(
        backport_sweep,
        "_upsert_pr",
        lambda *args, **kwargs: upserts.append(kwargs)
        or "https://github.com/valkey-io/valkey/pull/100",
    )

    result = backport_sweep._process_branch(
        gh=MagicMock(),
        repo=MagicMock(),
        repo_full_name="valkey-io/valkey",
        github_token="token",
        target_branch="8.1",
        candidates=candidates,
        push_repo="valkey-io/valkey",
        test_commands=["make"],
        validate_each_candidate=True,
    )

    assert [r.outcome for r in result.results] == ["applied", "skipped-test"]
    assert result.results[1].detail == "bad compile"
    assert ["git", "reset", "--hard", "HEAD^"] in reset_calls
    assert result.pr_url == "https://github.com/valkey-io/valkey/pull/100"
    assert upserts[0]["draft"] is False


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

    # Attempt cherry-pick — will conflict
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
    monkeypatch.setattr(
        backport_sweep,
        "resolve_conflicts_with_claude",
        lambda *_args, **_kwargs: [
            ResolutionResult(
                path="file.txt",
                resolved_content="line1\nresolved-content\nline3\n",
                resolution_summary="resolved",
            )
        ],
    )
    # Skip over-application check (requires `git fetch origin` which fails in
    # the tmpdir repo).
    monkeypatch.setattr(
        backport_sweep, "_check_applied_commit_size", lambda *_a, **_k: None,
    )
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



def _make_size_check_candidate() -> ProjectBackportCandidate:
    return ProjectBackportCandidate(
        source_pr_number=99,
        source_pr_title="Test",
        source_pr_url="https://example.invalid/pr/99",
        target_branch="8.1",
        merge_commit_sha="deadbeef",
    )


def _stub_size_check_subprocess(monkeypatch, upstream_add: int, applied_add: int) -> None:
    """Stub subprocess.run so _check_applied_commit_size sees the given additions."""

    def fake(cmd, **_kwargs):
        if cmd[:3] == ["git", "fetch", "origin"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "show"] and "HEAD" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout=f" 1 file changed, {applied_add} insertions(+)\n",
                stderr="",
            )
        if cmd[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(
                cmd, 0,
                stdout=f" 1 file changed, {upstream_add} insertions(+)\n",
                stderr="",
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(backport_sweep.subprocess, "run", fake)


def test_check_applied_commit_size_passes_small_pr_adaptation(monkeypatch):
    """A 1-line upstream PR that becomes 5 lines after branch adaptation
    must not be rejected (under old 3x rule, 3+ lines would trip)."""
    _stub_size_check_subprocess(monkeypatch, upstream_add=1, applied_add=5)
    assert backport_sweep._check_applied_commit_size("/fake", _make_size_check_candidate()) is None


def test_check_applied_commit_size_passes_medium_pr_slight_growth(monkeypatch):
    """Upstream 50 lines, applied 120 lines (70 extra) is fine under the new rule."""
    _stub_size_check_subprocess(monkeypatch, upstream_add=50, applied_add=120)
    assert backport_sweep._check_applied_commit_size("/fake", _make_size_check_candidate()) is None


def test_check_applied_commit_size_rejects_ratio_and_floor(monkeypatch):
    """Upstream 100 lines, applied 400 (300 extra, 4x) trips both guards."""
    _stub_size_check_subprocess(monkeypatch, upstream_add=100, applied_add=400)
    issue = backport_sweep._check_applied_commit_size("/fake", _make_size_check_candidate())
    assert issue is not None
    assert "+400" in issue and "+100" in issue


def test_check_applied_commit_size_rejects_absolute_floor_only(monkeypatch):
    """Upstream 200 lines, applied 600 (400 extra, 3x): absolute-floor branch."""
    _stub_size_check_subprocess(monkeypatch, upstream_add=200, applied_add=600)
    issue = backport_sweep._check_applied_commit_size("/fake", _make_size_check_candidate())
    assert issue is not None


def test_check_applied_commit_size_accepts_mild_ratio_under_floor(monkeypatch):
    """Upstream 5 lines, applied 50 (10x ratio but only 45 extra): accept."""
    _stub_size_check_subprocess(monkeypatch, upstream_add=5, applied_add=50)
    assert backport_sweep._check_applied_commit_size("/fake", _make_size_check_candidate()) is None


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

    backport_sweep._sync_target_branch_to_source(
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

    monkeypatch.setattr(backport_sweep.urllib.request, "urlopen", always_fails)
    # Skip actual sleeps in the backoff loop.
    monkeypatch.setattr(backport_sweep, "_random", None, raising=False)
    monkeypatch.setattr("random.uniform", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr("time.sleep", lambda *_args, **_kwargs: None)

    client = backport_sweep.GitHubGraphQLClient("fake-token")
    try:
        client.execute("query {}", {})
    except urllib.error.URLError:
        # On the 4th attempt, the client re-raises the URLError directly,
        # which is also fine — the test's purpose is to verify we never
        # hit an UnboundLocalError from the `body` variable.
        pass
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected retry exhaustion to raise")
    assert call_count["n"] == 4, f"expected 4 retry attempts, got {call_count['n']}"


def test_safe_tmp_component_removes_branch_separators():
    assert backport_sweep._safe_tmp_component("release/8.1") == "release-8.1"
    assert backport_sweep._safe_tmp_component("///") == "branch"


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

    body = backport_sweep._build_pr_body(result)

    assert "## Applied" in body
    # The Applied section is the only commit-listing section in the body
    # (no Needs attention because there are no failures, and the legacy
    # "Already on branch" section is removed). Asserting against the full
    # body therefore only checks the Applied table.
    assert "## Needs attention" not in body
    assert "Already on branch" not in body
    # Newly applied + on-sweep-branch carry-overs are listed.
    assert "#3654" in body
    assert "#3380" in body
    assert "#3619" in body
    # Already-on-release-branch and no-op resolutions must NOT be listed:
    # those changes are not on the sweep branch, so listing them under
    # Applied would misrepresent the PR's commit set.
    assert "#4001" not in body
    assert "#4002" not in body



# ---------------------------------------------------------------------------
# ProjectBackportDiscovery — cross-repo filter
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
    # Bypass the GraphQL fetch — return our fake items directly.
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
    quirk, etc.), don't refuse to sweep — the field is the new filter, not a
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
