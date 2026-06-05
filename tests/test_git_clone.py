"""Tests for shallow git-clone helper."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.common.git_clone import shallow_clone_at_sha


def test_rejects_invalid_repo_format(tmp_path):
    """Argument-injection defense: repo must match owner/name."""
    assert not shallow_clone_at_sha("not-a-valid-repo", tmp_path / "x")
    assert not shallow_clone_at_sha("owner/name; rm -rf /", tmp_path / "x")


def test_rejects_non_sha_value(tmp_path):
    """Argument-injection defense: sha must be a hex commit hash."""
    assert not shallow_clone_at_sha("owner/name", tmp_path / "x", sha="HEAD")
    assert not shallow_clone_at_sha("owner/name", tmp_path / "x", sha="--upload-pack=evil")


def test_default_branch_uses_depth_1(tmp_path):
    """Cloning without a SHA does a depth-1 clone of the default branch."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    with patch("scripts.common.git_clone.subprocess.run", side_effect=fake_run):
        ok = shallow_clone_at_sha("owner/name", tmp_path / "dest")
    assert ok is True
    assert "--depth" in captured["args"]
    assert "1" in captured["args"]
    assert "owner/name" in " ".join(captured["args"])


def test_sha_path_runs_clone_fetch_checkout(tmp_path):
    """With a SHA we do a blobless full clone (no --depth) + checkout."""
    sha = "deadbeef1234567"
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)

        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    with patch("scripts.common.git_clone.subprocess.run", side_effect=fake_run):
        ok = shallow_clone_at_sha("owner/name", tmp_path / "dest", sha=sha)
    assert ok is True
    assert calls[0][:2] == ["git", "clone"]
    # The SHA clone must NOT be shallow — a depth-1 clone can't reach a
    # non-tip commit since GitHub refuses fetching arbitrary SHAs.
    assert "--depth" not in calls[0]
    assert calls[1][:2] == ["git", "checkout"]
    assert len(calls) == 2


def test_clone_failure_returns_false(tmp_path):
    """A non-zero exit from git clone is logged and reported as failure."""
    def fake_run(args, **kwargs):
        class _R:
            returncode = 128
            stdout = ""
            stderr = "fatal: repository 'x' not found"
        return _R()

    with patch("scripts.common.git_clone.subprocess.run", side_effect=fake_run):
        assert shallow_clone_at_sha("owner/name", tmp_path / "dest") is False


def test_timeout_returns_false(tmp_path):
    """A timed-out clone is logged and reported as failure (not raised)."""
    import subprocess

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs.get("timeout", 0))

    with patch("scripts.common.git_clone.subprocess.run", side_effect=fake_run):
        assert shallow_clone_at_sha("owner/name", tmp_path / "dest") is False


def test_oserror_returns_false(tmp_path):
    """A missing/non-executable git binary is logged and reported as failure."""
    def fake_run(args, **kwargs):
        raise OSError("git is not executable")

    with patch("scripts.common.git_clone.subprocess.run", side_effect=fake_run):
        assert shallow_clone_at_sha("owner/name", tmp_path / "dest") is False
