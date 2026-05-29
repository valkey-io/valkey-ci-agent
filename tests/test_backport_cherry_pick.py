"""Unit tests for cherry_pick().

"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

from scripts.backport.cherry_pick import cherry_pick


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Return a successful CompletedProcess."""
    return subprocess.CompletedProcess(
        args=["git"], returncode=0, stdout=stdout, stderr=stderr,
    )


def _fail(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    """Return a failed CompletedProcess."""
    return subprocess.CompletedProcess(
        args=["git"], returncode=1, stdout=stdout, stderr=stderr,
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_empty_cherry_pick_does_not_create_empty_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "file.txt").write_text("already present\n", encoding="utf-8")
    _git(repo, "add", "file.txt")
    _git(repo, "commit", "-q", "-m", "already applied")
    sha = _git(repo, "rev-parse", "HEAD")
    before_count = _git(repo, "rev-list", "--count", "HEAD")

    result = cherry_pick(str(repo), "main", sha, [])

    assert result.success is True
    assert result.applied_commits == []
    assert _git(repo, "rev-list", "--count", "HEAD") == before_count
    assert _git(repo, "status", "--porcelain") == ""


class TestCleanCherryPickWithMergeCommit:
    """Scenario 1: Clean cherry-pick using merge commit SHA."""

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_returns_success(self, mock_run: MagicMock) -> None:
        # checkout succeeds, cherry-pick -m 1 succeeds
        mock_run.side_effect = [_ok(), _ok()]

        result = cherry_pick("/repo", "8.1", "abc123merge", ["sha1", "sha2"])

        assert result.success is True
        assert result.applied_commits == ["abc123merge"]
        assert result.conflicting_files == []

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_calls_checkout_then_cherry_pick(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [_ok(), _ok()]

        cherry_pick("/repo", "8.1", "abc123merge", ["sha1"])

        calls = mock_run.call_args_list
        # First call: git checkout 8.1
        assert calls[0][0][0] == ["git", "checkout", "8.1"]
        # Second call: git cherry-pick -m 1 <merge_sha>
        assert calls[1][0][0] == ["git", "cherry-pick", "-m", "1", "abc123merge"]

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_retries_without_mainline_for_squash_merge_commit(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),
            _fail(stderr="error: commit abc123 is not a merge but no -m option was given?\nfatal: mainline was specified but commit abc123 is not a merge."),
            _ok(),
        ]

        result = cherry_pick("/repo", "8.1", "abc123", ["sha1"])

        assert result.success is True
        assert result.applied_commits == ["abc123"]
        calls = [call_args[0][0] for call_args in mock_run.call_args_list]
        assert ["git", "cherry-pick", "-m", "1", "abc123"] in calls
        assert ["git", "cherry-pick", "abc123"] in calls


class TestCleanCherryPickSequential:
    """Scenario 2: Clean cherry-pick with sequential commits."""

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_returns_success_all_commits(self, mock_run: MagicMock) -> None:
        # checkout + 3 cherry-picks all succeed
        mock_run.side_effect = [_ok(), _ok(), _ok(), _ok()]

        result = cherry_pick("/repo", "7.2", None, ["sha1", "sha2", "sha3"])

        assert result.success is True
        assert result.applied_commits == ["sha1", "sha2", "sha3"]
        assert result.conflicting_files == []

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_calls_cherry_pick_per_commit(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [_ok(), _ok(), _ok()]

        cherry_pick("/repo", "8.1", None, ["sha1", "sha2"])

        calls = mock_run.call_args_list
        assert calls[0][0][0] == ["git", "checkout", "8.1"]
        assert calls[1][0][0] == ["git", "cherry-pick", "sha1"]
        assert calls[2][0][0] == ["git", "cherry-pick", "sha2"]

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_empty_sequential_cherry_pick_is_skipped(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),
            _fail(stderr="The previous cherry-pick is now empty"),
            _ok(stdout=""),
            _ok(),
            _ok(),
        ]

        result = cherry_pick("/repo", "8.1", None, ["sha1", "sha2"])

        assert result.success is True
        assert result.applied_commits == ["sha2"]
        assert result.conflicting_files == []
        calls = [call_args[0][0] for call_args in mock_run.call_args_list]
        assert ["git", "cherry-pick", "--abort"] in calls
        assert ["git", "cherry-pick", "--allow-empty", "sha1"] not in calls

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_empty_merge_cherry_pick_is_skipped(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            _ok(),
            _fail(stderr="The previous cherry-pick is now empty"),
            _ok(stdout=""),
            _ok(),
        ]

        result = cherry_pick("/repo", "8.1", "merge_sha", ["sha1"])

        assert result.success is True
        assert result.applied_commits == []
        assert result.conflicting_files == []
        calls = [call_args[0][0] for call_args in mock_run.call_args_list]
        assert ["git", "cherry-pick", "--abort"] in calls
        assert ["git", "cherry-pick", "-m", "1", "--allow-empty", "merge_sha"] not in calls

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_merge_failure_without_conflicts_is_not_counted_as_applied(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),
            _fail(stderr="fatal: bad revision"),
            _ok(stdout=""),
        ]

        result = cherry_pick("/repo", "8.1", "missing_sha", ["sha1"])

        assert result.success is False
        assert result.conflicting_files == []
        assert result.applied_commits == []


class TestConflictDetection:
    """Scenario 3: Cherry-pick with conflicts — conflict detection and file parsing."""

    @patch("builtins.open", mock_open(read_data="<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc123\n"))
    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_merge_commit_conflict_returns_conflicted_files(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),                                      # checkout
            _fail(stderr="conflict"),                    # cherry-pick -m 1 fails
            _ok(stdout="src/server.c\nsrc/config.c\n"), # git diff --name-only --diff-filter=U
            _ok(stdout="target content"),                # git show 8.1:src/server.c
            _ok(stdout="source content"),                # git show CHERRY_PICK_HEAD:src/server.c
            _ok(stdout="target content 2"),              # git show 8.1:src/config.c
            _ok(stdout="source content 2"),              # git show CHERRY_PICK_HEAD:src/config.c
        ]

        result = cherry_pick("/repo", "8.1", "mergesha", ["sha1"])

        assert result.success is False
        assert len(result.conflicting_files) == 2
        assert result.conflicting_files[0].path == "src/server.c"
        assert result.conflicting_files[1].path == "src/config.c"
        assert result.applied_commits == []

    @patch("builtins.open", mock_open(read_data="conflict content"))
    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_sequential_conflict_stops_at_failing_commit(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),                              # checkout
            _ok(),                              # cherry-pick sha1 succeeds
            _fail(stderr="conflict"),           # cherry-pick sha2 fails
            _ok(stdout="file.c\n"),             # git diff --name-only --diff-filter=U
            _ok(stdout="target ver"),           # git show 8.1:file.c
            _ok(stdout="source ver"),           # git show CHERRY_PICK_HEAD:file.c
        ]

        result = cherry_pick("/repo", "8.1", None, ["sha1", "sha2", "sha3"])

        assert result.success is False
        assert result.applied_commits == ["sha1"]
        assert len(result.conflicting_files) == 1
        assert result.conflicting_files[0].path == "file.c"

    @patch("builtins.open", mock_open(read_data="markers here"))
    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_conflicted_file_reads_all_versions(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),                                  # checkout
            _fail(),                                # cherry-pick fails
            _ok(stdout="src/main.c\n"),             # git diff
            _ok(stdout="target branch content"),    # git show 8.1:src/main.c
            _ok(stdout="source branch content"),    # git show CHERRY_PICK_HEAD:src/main.c
        ]

        result = cherry_pick("/repo", "8.1", "mergesha", [])

        cf = result.conflicting_files[0]
        assert cf.path == "src/main.c"
        assert cf.target_branch_content == "target branch content"
        assert cf.source_branch_content == "source branch content"

    @patch("builtins.open", mock_open(read_data="content"))
    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_git_show_failure_returns_empty_string(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [
            _ok(),                          # checkout
            _fail(),                        # cherry-pick fails
            _ok(stdout="new_file.c\n"),     # git diff
            _fail(stderr="not found"),      # git show target branch fails
            _fail(stderr="not found"),      # git show CHERRY_PICK_HEAD fails
        ]

        result = cherry_pick("/repo", "8.1", "mergesha", [])

        cf = result.conflicting_files[0]
        assert cf.target_branch_content == ""
        assert cf.source_branch_content == ""

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_binary_conflict_is_skipped(self, mock_run: MagicMock) -> None:
        # One text conflict and one binary conflict (NUL byte in content).
        mock_run.side_effect = [
            _ok(),                                  # checkout
            _fail(stderr="conflict"),                # cherry-pick fails
            _ok(stdout="src/main.c\nfixture.gz\n"),  # git diff --name-only
            _ok(stdout="target text"),               # git show 8.1:src/main.c
            _ok(stdout="source text"),               # git show CHERRY_PICK_HEAD:src/main.c
            _ok(stdout="binary\x00blob"),            # git show 8.1:fixture.gz
            _ok(stdout="binary\x00other"),           # git show CHERRY_PICK_HEAD:fixture.gz
        ]

        result = cherry_pick("/repo", "8.1", "mergesha", [])

        assert result.success is False
        # Only the text file survives; the binary one is skipped.
        assert [cf.path for cf in result.conflicting_files] == ["src/main.c"]

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_only_binary_conflicts_yields_empty_set(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [
            _ok(),                          # checkout
            _fail(stderr="conflict"),        # cherry-pick fails
            _ok(stdout="fixture.gz\n"),      # git diff --name-only
            _ok(stdout="bin\x00a"),          # git show 8.1:fixture.gz
            _ok(stdout="bin\x00b"),          # git show CHERRY_PICK_HEAD:fixture.gz
        ]

        result = cherry_pick("/repo", "8.1", "mergesha", [])

        # No resolvable conflicts — caller skips the candidate.
        assert result.success is False
        assert result.conflicting_files == []


class TestMergeCommitPreference:
    """Scenario 4 & 5: Merge commit SHA is preferred; sequential fallback."""

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_uses_m1_flag_when_merge_sha_provided(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [_ok(), _ok()]

        cherry_pick("/repo", "8.1", "merge_sha_abc", ["sha1", "sha2"])

        cherry_pick_call = mock_run.call_args_list[1]
        cmd = cherry_pick_call[0][0]
        assert cmd == ["git", "cherry-pick", "-m", "1", "merge_sha_abc"]

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_ignores_individual_commits_when_merge_sha_provided(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [_ok(), _ok()]

        result = cherry_pick("/repo", "8.1", "merge_sha", ["sha1", "sha2", "sha3"])

        # Only 2 subprocess calls: checkout + single cherry-pick
        assert mock_run.call_count == 2
        assert result.applied_commits == ["merge_sha"]

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_falls_back_to_sequential_when_no_merge_sha(
        self, mock_run: MagicMock,
    ) -> None:
        mock_run.side_effect = [_ok(), _ok(), _ok()]

        result = cherry_pick("/repo", "8.1", None, ["sha1", "sha2"])

        # checkout + 2 individual cherry-picks
        assert mock_run.call_count == 3
        calls = mock_run.call_args_list
        assert calls[1][0][0] == ["git", "cherry-pick", "sha1"]
        assert calls[2][0][0] == ["git", "cherry-pick", "sha2"]
        assert result.applied_commits == ["sha1", "sha2"]

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_empty_merge_sha_string_treated_as_none(
        self, mock_run: MagicMock,
    ) -> None:
        """An empty string for merge_commit_sha is falsy, so sequential path is used."""
        mock_run.side_effect = [_ok(), _ok()]

        result = cherry_pick("/repo", "8.1", "", ["sha1"])

        calls = mock_run.call_args_list
        # Should use sequential path (no -m 1)
        assert calls[1][0][0] == ["git", "cherry-pick", "sha1"]
        assert result.applied_commits == ["sha1"]


class TestSubprocessCwd:
    """Verify that all git commands use the configured repo_dir as cwd."""

    @patch("scripts.backport.cherry_pick.subprocess.run")
    def test_all_calls_use_repo_dir(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = [_ok(), _ok()]

        cherry_pick("/my/repo/path", "8.1", "sha", [])

        for c in mock_run.call_args_list:
            assert c[1]["cwd"] == "/my/repo/path"
