"""Tests for default-branch port candidate discovery."""

from __future__ import annotations

import subprocess

from scripts.ci_fix.port_discovery import discover_port_candidates, format_port_candidates


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _commit(repo, message):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def test_discovers_default_branch_fix_by_logged_path(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "release", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "src").mkdir()
    (repo / "src" / "logreqres.c").write_text("base\n")
    _commit(repo, "release base")

    _git(repo, "checkout", "-qb", "unstable")
    (repo / "src" / "logreqres.c").write_text("skip internal clients\n")
    upstream = _commit(repo, "Skips the internal clients from logreqres checks (#3154)")
    _git(repo, "update-ref", "refs/remotes/origin/unstable", "unstable")
    _git(repo, "checkout", "-q", "release")

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "validator.txt").write_text(
        "validator failed while checking src/logreqres.c for missing replies\n"
    )

    candidates = discover_port_candidates(str(repo), str(logs))

    assert candidates
    assert candidates[0].sha == upstream
    assert candidates[0].paths == ("src/logreqres.c",)
    rendered = format_port_candidates(candidates)
    assert upstream[:12] in rendered
    assert "logreqres" in rendered


def test_discovers_default_branch_fix_by_message_term_when_no_path(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "release", str(repo)], check=True)
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README").write_text("base\n")
    _commit(repo, "release base")

    _git(repo, "checkout", "-qb", "unstable")
    (repo / "README").write_text("fixed\n")
    upstream = _commit(repo, "Fix reply schema validator for internal clients")
    _git(repo, "update-ref", "refs/remotes/origin/unstable", "unstable")
    _git(repo, "checkout", "-q", "release")

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "validator.txt").write_text("reply schema validator failed\n")

    candidates = discover_port_candidates(str(repo), str(logs))

    assert [c.sha for c in candidates] == [upstream]
