"""Real-Git tests for the shared candidate-application engine."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.backport import candidate_apply
from scripts.backport.candidate_apply import apply_candidate
from scripts.backport.models import (
    BackportCandidate,
    ResolutionResult,
)
from scripts.backport.source_plan import SourceChangePlan
from scripts.backport.sweep_git import list_applied_prs_on_branch
from scripts.common.logging_utils import LOG_HIGHLIGHT_RULE


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "commit.gpgsign", "false")


def _commit(repo: Path, path: str, content: str, message: str) -> str:
    destination = repo / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", path)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _candidate(
    *,
    merge_commit_sha: str | None,
    commit_shas: list[str],
) -> BackportCandidate:
    return BackportCandidate(
        source_pr_number=42,
        source_pr_title="Apply the complete source change",
        source_pr_url="https://github.com/example/repo/pull/42",
        target_branch="release",
        merge_commit_sha=merge_commit_sha,
        commit_shas=commit_shas,
    )


def test_apply_candidate_highlights_number_title_and_plan(
    caplog,
    monkeypatch,
    tmp_path: Path,
) -> None:
    candidate = BackportCandidate(
        source_pr_number=4714,
        source_pr_title="\x1b[31mDeflake\nBgIterationTest.createAndCleanup\x1b[0m",
        source_pr_url="https://github.com/example/repo/pull/4714",
        target_branch="9.2",
    )
    plan = SourceChangePlan(
        strategy="single",
        commits=("source-sha",),
        merge_commit_sha="source-sha",
        source_commits=("source-sha",),
        aggregate_patch_id="patch-id",
    )
    monkeypatch.setattr(
        candidate_apply,
        "head_sha",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("stop after logging")
        ),
    )
    caplog.set_level(logging.INFO, logger=candidate_apply.__name__)

    result = apply_candidate(
        str(tmp_path),
        candidate,
        "valkey-io/valkey",
        {},
        source_plan=plan,
    )

    assert result.outcome == "error"
    assert caplog.messages == [
        LOG_HIGHLIGHT_RULE,
        (
            "BACKPORT ATTEMPT: PR #4714 | "
            "Deflake BgIterationTest.createAndCleanup | "
            "source=valkey-io/valkey | plan=single | commits=1"
        ),
        LOG_HIGHLIGHT_RULE,
    ]


def test_rebase_merged_pull_request_fails_closed_end_to_end(
    tmp_path: Path,
) -> None:
    """The merge SHA of a rebase merge is only the final rewritten commit.
    The old engine cherry-picked just that SHA, silently losing 'source
    one'. Replaying rebased series is not supported, so the candidate must
    error loudly with a clean worktree."""
    source = tmp_path / "source"
    _init_repo(source)
    _commit(source, "base.txt", "base\n", "base")
    base = _git(source, "rev-parse", "HEAD")
    _git(source, "branch", "release", base)

    _git(source, "checkout", "-q", "-b", "pull-request")
    first = _commit(source, "one.txt", "one\n", "source one")
    second = _commit(source, "two.txt", "two\n", "source two")

    _git(source, "checkout", "-q", "main")
    _commit(source, "main.txt", "main advanced\n", "advance main")
    _git(source, "checkout", "-q", "-b", "rebased")
    _git(source, "cherry-pick", first, second)
    rebased_tip = _git(source, "rev-parse", "HEAD")

    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", str(remote)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(source, "remote", "add", "origin", str(remote))
    _git(source, "push", "-q", "origin", "release")
    _git(source, "push", "-q", "origin", "rebased:main")
    _git(
        source,
        "push",
        "-q",
        "origin",
        "pull-request:refs/pull/42/head",
    )

    worktree = tmp_path / "worktree"
    subprocess.run(
        [
            "git",
            "clone",
            "-q",
            "--branch",
            "release",
            str(remote),
            str(worktree),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(worktree, "config", "user.name", "Test")
    _git(worktree, "config", "user.email", "test@example.com")

    result = apply_candidate(
        str(worktree),
        _candidate(
            merge_commit_sha=rebased_tip,
            commit_shas=[first, second],
        ),
        "example/repo",
        dict(os.environ),
    )

    assert result.outcome == "error"
    assert "rebase-merged" in result.detail
    assert result.applied_commits == []
    assert not (worktree / "one.txt").exists()
    assert not (worktree / "two.txt").exists()
    assert _git(worktree, "rev-list", "--count", "origin/release..HEAD") == "0"
    assert _git(worktree, "status", "--porcelain") == ""


def test_conflict_limit_rolls_back_without_invoking_resolver(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "one.txt", "base one\n", "base one")
    _commit(repo, "two.txt", "base two\n", "base two")
    base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "source")
    source_sha = _commit(repo, "one.txt", "source one\n", "source one")
    (repo / "two.txt").write_text("source two\n", encoding="utf-8")
    _git(repo, "add", "two.txt")
    _git(repo, "commit", "--amend", "-q", "--no-edit")
    source_sha = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "release", base)
    _commit(repo, "one.txt", "release one\n", "release one")
    (repo / "two.txt").write_text("release two\n", encoding="utf-8")
    _git(repo, "add", "two.txt")
    _git(repo, "commit", "--amend", "-q", "--no-edit")
    starting_head = _git(repo, "rev-parse", "HEAD")
    resolver = MagicMock()

    result = apply_candidate(
        str(repo),
        _candidate(merge_commit_sha=None, commit_shas=[source_sha]),
        "example/repo",
        dict(os.environ),
        max_conflicting_files=1,
        resolve_conflicts=resolver,
        source_plan=SourceChangePlan(
            strategy="single",
            commits=(source_sha,),
            merge_commit_sha=None,
            source_commits=(source_sha,),
            aggregate_patch_id="test-patch",
        ),
    )

    assert result.outcome == "skipped-conflict"
    assert "Too many conflicting files (2 >" in result.detail
    assert _git(repo, "rev-parse", "HEAD") == starting_head
    assert _git(repo, "status", "--porcelain") == ""
    resolver.assert_not_called()


def test_hard_cherry_pick_failure_is_not_reported_as_empty(
    tmp_path: Path,
) -> None:
    """A cherry-pick that fails without conflicts and is NOT empty must be an
    error, never 'already applied' — misclassifying it would silently drop
    the commit while the sweep reports the PR as applied."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "base.txt", "base\n", "base")
    base = _git(repo, "rev-parse", "HEAD")

    _git(repo, "checkout", "-q", "-b", "source")
    source_sha = _commit(repo, "data.txt", "source\n", "source change")

    _git(repo, "checkout", "-q", "-b", "release", base)
    starting_head = _git(repo, "rev-parse", "HEAD")
    (repo / "data.txt").write_text("untracked\n", encoding="utf-8")

    result = apply_candidate(
        str(repo),
        _candidate(merge_commit_sha=None, commit_shas=[source_sha]),
        "example/repo",
        dict(os.environ),
        source_plan=SourceChangePlan(
            strategy="single",
            commits=(source_sha,),
            merge_commit_sha=None,
            source_commits=(source_sha,),
            aggregate_patch_id="test-patch",
        ),
    )

    assert result.outcome == "error"
    assert "cherry-pick failed" in result.detail
    assert result.applied_commits == []
    assert _git(repo, "rev-parse", "HEAD") == starting_head


def test_failed_abort_falls_back_to_hard_reset(tmp_path: Path) -> None:
    """When ``cherry-pick --abort`` itself fails, the helper must reset the
    tree before any caller continues — an empty/no-op path returning success
    on a dirty tree would poison every later commit in the run."""
    calls: list[list[str]] = []

    def run_process(cmd, **_kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "cherry-pick", "--abort"]:
            return subprocess.CompletedProcess(
                cmd, 128, stdout="",
                stderr="fatal: no cherry-pick or revert in progress",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    assert candidate_apply._abort_cherry_pick(str(tmp_path), run_process) is True

    assert ["git", "cherry-pick", "--abort"] in calls
    assert ["git", "reset", "--hard", "HEAD"] in calls


def test_abort_and_reset_both_failing_reports_unclean(tmp_path: Path) -> None:
    """If the fallback reset fails too, the tree state is unknown; callers
    must see False and turn the candidate into an error rather than
    continuing or reporting skipped-existing."""

    def run_process(cmd, **_kwargs):
        return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="boom")

    assert candidate_apply._abort_cherry_pick(str(tmp_path), run_process) is False


def test_successful_abort_does_not_reset(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run_process(cmd, **_kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    assert candidate_apply._abort_cherry_pick(str(tmp_path), run_process) is True

    assert calls == [["git", "cherry-pick", "--abort"]]


def test_read_index_stage_propagates_unexpected_git_failure(
    tmp_path: Path,
) -> None:
    def run_process(cmd, **_kwargs):
        if cmd == ["git", "show", ":2:shared.txt"]:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="I/O error")
        if cmd == ["git", "cat-file", "-e", ":2:shared.txt"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)

    with pytest.raises(RuntimeError, match="could not read index stage"):
        candidate_apply.read_index_stage(
            str(tmp_path),
            "shared.txt",
            2,
            run_process=run_process,
        )


def test_cleanup_failure_is_contained_as_an_error(tmp_path: Path) -> None:
    def run_process(cmd, **_kwargs):
        if cmd == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="start\n", stderr=""
            )
        if cmd == ["git", "cherry-pick", "deadbeef"]:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="The previous cherry-pick is now empty",
            )
        if cmd == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd in (
            ["git", "diff", "--name-only", "-z"],
            ["git", "diff", "--cached", "--name-only", "-z"],
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        ):
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")
        if cmd in (
            ["git", "cherry-pick", "--abort"],
            ["git", "reset", "--hard", "HEAD"],
        ):
            return subprocess.CompletedProcess(
                cmd, 128, stdout="", stderr="cleanup failed"
            )
        raise AssertionError(cmd)

    def run_git(*_args, **_kwargs):
        raise RuntimeError("reset to candidate start failed")

    result = apply_candidate(
        str(tmp_path),
        _candidate(merge_commit_sha="deadbeef", commit_shas=["source"]),
        "example/repo",
        {},
        run_process=run_process,
        run_git=run_git,
        source_plan=SourceChangePlan(
            strategy="single",
            commits=("deadbeef",),
            merge_commit_sha="deadbeef",
            source_commits=("source",),
            aggregate_patch_id="patch",
        ),
    )

    assert result.outcome == "error"
    assert "cleanup failed" in result.detail
    assert result.worktree_restored is False


def test_automatic_resolution_is_not_reported_as_ai(tmp_path: Path) -> None:
    (tmp_path / "shared.txt").write_text("conflict\n", encoding="utf-8")
    head_shas = iter(("starting-head", "resolved-head", "amended-head"))

    def run_process(cmd, **_kwargs):
        if cmd == ["git", "rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=next(head_shas) + "\n", stderr=""
            )
        if cmd == ["git", "cherry-pick", "deadbeef"]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="conflict"
            )
        if cmd == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="shared.txt\n", stderr=""
            )
        if cmd == ["git", "show", ":2:shared.txt"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="target\n", stderr=""
            )
        if cmd == ["git", "show", ":3:shared.txt"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="source\n", stderr=""
            )
        if cmd == ["git", "cat-file", "-e", ":2:shared.txt"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd in (
            ["git", "diff", "--name-only", "-z"],
            ["git", "diff", "--cached", "--name-only", "-z"],
            ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        ):
            return subprocess.CompletedProcess(
                cmd, 0, stdout=b"", stderr=b""
            )
        if cmd == ["git", "diff", "--cached", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if cmd == [
            "git",
            "-c",
            "core.editor=true",
            "cherry-pick",
            "--continue",
        ]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:6] == [
            "git",
            "-c",
            "core.editor=true",
            "commit",
            "--amend",
            "--no-edit",
        ]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)

    def resolve(*_args, **_kwargs):
        return [
            ResolutionResult(
                path="shared.txt",
                resolved_content="source\n",
                resolution_summary="whitespace-only (no LLM needed)",
                source="automatic",
            )
        ]

    result = apply_candidate(
        str(tmp_path),
        _candidate(merge_commit_sha="deadbeef", commit_shas=["source"]),
        "example/repo",
        {},
        run_process=run_process,
        run_git=lambda *_args, **_kwargs: None,
        resolve_conflicts=resolve,
        source_plan=SourceChangePlan(
            strategy="single",
            commits=("deadbeef",),
            merge_commit_sha="deadbeef",
            source_commits=("source",),
            aggregate_patch_id="patch",
        ),
    )

    assert result.outcome == "applied"
    assert result.resolved_by_ai is False
    assert result.resolved_commit_sha == "amended-head"
    assert "Claude Code" not in result.detail


@pytest.mark.parametrize("has_staged_changes", [False, True])
def test_dirty_no_change_errors_restore_untracked_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_staged_changes: bool,
) -> None:
    rollback_calls: list[dict[str, bytes]] = []

    def run_process(cmd, **_kwargs):
        if cmd == ["git", "cherry-pick", "deadbeef"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="conflict")
        if cmd == ["git", "diff", "--name-only", "--diff-filter=U"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="shared.txt\n", stderr=""
            )
        if cmd[:4] == ["git", "-c", "core.editor=true", "cherry-pick"]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="nothing to commit"
            )
        raise AssertionError(cmd)

    monkeypatch.setattr(
        candidate_apply,
        "read_index_stage",
        lambda _repo, _path, stage, **_kwargs: (
            "target\n" if stage == 2 else "source\n"
        ),
    )
    monkeypatch.setattr(candidate_apply, "index_stage_exists", lambda *_a, **_k: True)
    monkeypatch.setattr(
        candidate_apply,
        "changed_paths_in_index_or_worktree",
        lambda *_a, **_k: (),
    )
    monkeypatch.setattr(
        candidate_apply,
        "has_staged_changes",
        lambda *_a, **_k: has_staged_changes,
    )
    monkeypatch.setattr(candidate_apply, "_abort_cherry_pick", lambda *_a: False)
    monkeypatch.setattr(
        candidate_apply,
        "_abort_and_rollback",
        lambda _repo, _head, _git, _process, untracked: rollback_calls.append(
            untracked
        ),
    )

    starting_untracked = {"validation-output.txt": b"keep\n"}
    result = candidate_apply._apply_plan(
        str(tmp_path),
        _candidate(merge_commit_sha="deadbeef", commit_shas=["source"]),
        SourceChangePlan(
            strategy="single",
            commits=("deadbeef",),
            merge_commit_sha="deadbeef",
            source_commits=("source",),
            aggregate_patch_id="patch",
        ),
        candidate_apply._ApplyState(
            "starting-head",
            starting_untracked_files=starting_untracked,
        ),
        language="c",
        build_commands=None,
        validation_rules=None,
        test_path_patterns=None,
        max_conflicting_files=100,
        run_git=lambda *_a, **_k: None,
        resolve_conflicts=lambda *_a, **_k: [
            ResolutionResult(
                path="shared.txt",
                resolved_content="source\n",
                resolution_summary="resolved",
                source="automatic",
            )
        ],
        adapt_missing_tests=lambda *_a, **_k: None,
        run_process=run_process,
    )

    assert result.outcome == "error"
    assert candidate_apply._DIRTY_TREE_ERROR in result.detail
    assert rollback_calls == [starting_untracked]


def test_preexisting_untracked_file_is_not_allowed_or_committed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "shared.txt", "base\n", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "source")
    source_sha = _commit(repo, "shared.txt", "source\n", "source change")
    _git(repo, "checkout", "-q", "-b", "release", base)
    _commit(repo, "shared.txt", "release\n", "release change")
    artifact = repo / "validation-output.txt"
    artifact.write_text("keep me\n", encoding="utf-8")
    allowed: list[str] = []

    def resolve(_repo_dir, _files, _context, **kwargs):
        allowed.extend(kwargs["allowed_paths"])
        return [
            ResolutionResult(
                path="shared.txt",
                resolved_content="resolved\n",
                resolution_summary="resolved",
            )
        ]

    result = apply_candidate(
        str(repo),
        _candidate(merge_commit_sha=None, commit_shas=[source_sha]),
        "example/repo",
        dict(os.environ),
        resolve_conflicts=resolve,
        source_plan=SourceChangePlan(
            strategy="single",
            commits=(source_sha,),
            merge_commit_sha=None,
            source_commits=(source_sha,),
            aggregate_patch_id="patch",
        ),
    )

    assert result.outcome == "applied"
    assert allowed == ["shared.txt"]
    assert artifact.read_text(encoding="utf-8") == "keep me\n"
    assert "validation-output.txt" not in _git(
        repo, "show", "--pretty=", "--name-only", "HEAD"
    ).splitlines()
    assert "Backport-Source-PR: 42" in _git(repo, "log", "-1", "--format=%B")


def test_failed_resolution_removes_only_candidate_created_untracked_files(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "shared.txt", "base\n", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "source")
    source_sha = _commit(repo, "shared.txt", "source\n", "source change")
    _git(repo, "checkout", "-q", "-b", "release", base)
    _commit(repo, "shared.txt", "release\n", "release change")
    preserved = repo / "validation-output" / "cache.txt"
    preserved.parent.mkdir()
    preserved.write_text("original\n", encoding="utf-8")

    def resolve(repo_dir, _files, _context, **_kwargs):
        Path(repo_dir, "scratch.txt").write_text("temporary\n", encoding="utf-8")
        shutil.rmtree(preserved.parent)
        return [
            ResolutionResult(
                path="shared.txt",
                resolved_content=None,
                resolution_summary="unresolved",
            )
        ]

    result = apply_candidate(
        str(repo),
        _candidate(merge_commit_sha=None, commit_shas=[source_sha]),
        "example/repo",
        dict(os.environ),
        resolve_conflicts=resolve,
        source_plan=SourceChangePlan(
            strategy="single",
            commits=(source_sha,),
            merge_commit_sha=None,
            source_commits=(source_sha,),
            aggregate_patch_id="patch",
        ),
    )

    assert result.outcome == "skipped-conflict"
    assert result.worktree_restored is True
    assert not (repo / "scratch.txt").exists()
    assert preserved.read_text(encoding="utf-8") == "original\n"
    assert _git(repo, "status", "--porcelain") == "?? validation-output/"


def test_merge_commit_subject_is_detected_as_already_applied(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "base.txt", "base\n", "base")
    _git(repo, "branch", "release")
    _git(repo, "update-ref", "refs/remotes/origin/release", "release")
    _commit(
        repo,
        "merged.txt",
        "merged\n",
        "Merge pull request #42 from owner/feature",
    )

    applied = list_applied_prs_on_branch(str(repo), "release", "main")

    assert [item.source_pr_number for item in applied] == [42]
