"""Push-path tests, focused on the PORT path preserving upstream authorship.

A port carries an already-merged upstream commit, so the pushed commit must keep
the original author and sign-off rather than being re-authored as the bot.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.ci_fix import push as push_mod
from scripts.ci_fix.push import PushRefused, commit_and_push_port


def _git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True, **kw).stdout


def _make_origin(tmp_path):
    """A repo with a release branch and an unstable fix commit by a distinct
    author, plus a bare 'remote' to push to. Returns (work, bare, fix_sha, head_sha)."""
    work = tmp_path / "origin"
    work.mkdir()
    _git(work, "init", "-q", "-b", "agent/backport/sweep/9.0")
    _git(work, "config", "user.email", "base@t")
    _git(work, "config", "user.name", "Base")
    (work / "f.txt").write_text("base\n")
    _git(work, "add", "f.txt")
    _git(work, "commit", "-qm", "base")
    head_sha = _git(work, "rev-parse", "HEAD").strip()

    # The upstream fix on unstable, authored by a different human.
    _git(work, "checkout", "-qb", "unstable")
    (work / "g.txt").write_text("the fix\n")
    _git(work, "add", "g.txt")
    _git(work, "-c", "user.email=author@example.com", "-c", "user.name=Real Author",
         "commit", "-qm", "the upstream fix\n\nSigned-off-by: Real Author <author@example.com>")
    fix_sha = _git(work, "rev-parse", "HEAD").strip()
    _git(work, "checkout", "-q", "agent/backport/sweep/9.0")

    bare = tmp_path / "remote.git"
    _git(work, "clone", "-q", "--bare", str(work), str(bare))
    return work, bare, fix_sha, head_sha


def test_port_push_preserves_original_authorship(tmp_path, monkeypatch):
    work, bare, fix_sha, head_sha = _make_origin(tmp_path)

    # Clone from the local origin (which has both the head and the fix commit),
    # and push to the local bare remote, so no network or real GitHub is used.
    def fake_clone(_full_name, dest: Path):
        _git(dest.parent, "clone", "-q", str(work), str(dest))

    monkeypatch.setattr(push_mod, "_clone_clean", fake_clone)
    monkeypatch.setattr(push_mod, "github_https_url", lambda _n: str(bare))

    pushed_sha = commit_and_push_port(
        str(work),
        head_repo_full_name="valkey-io/valkey",
        head_branch="agent/backport/sweep/9.0",
        head_sha=head_sha,
        unstable_fix_commit=fix_sha,
        git_env={},
    )

    # The pushed commit on the bare remote keeps the original author, not the bot.
    author = _git(bare, "log", "-1", "--format=%an <%ae>", pushed_sha).strip()
    assert author == "Real Author <author@example.com>"
    body = _git(bare, "log", "-1", "--format=%B", pushed_sha)
    assert "Signed-off-by: Real Author <author@example.com>" in body
    assert "cherry picked from commit" in body  # the -x trailer


def test_port_push_refuses_non_namespaced_branch(tmp_path):
    with pytest.raises(PushRefused):
        commit_and_push_port(
            str(tmp_path), head_repo_full_name="valkey-io/valkey",
            head_branch="main", head_sha="a" * 40, unstable_fix_commit="b" * 40, git_env={},
        )


def test_port_push_refuses_malformed_commit(tmp_path):
    with pytest.raises(PushRefused):
        commit_and_push_port(
            str(tmp_path), head_repo_full_name="valkey-io/valkey",
            head_branch="agent/backport/sweep/9.0", head_sha="a" * 40,
            unstable_fix_commit="not-a-sha", git_env={},
        )
