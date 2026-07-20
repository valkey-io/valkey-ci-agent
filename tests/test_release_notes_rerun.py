"""Integration coverage for refreshing an open release-notes PR."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts.common.proc import git_output, run_git
from scripts.release_notes import pipeline as pipeline_mod
from scripts.release_notes import release_cut as cut_mod
from scripts.release_notes.models import CategorizedBullet, GenerationResult

_FIXTURE_CLONE = Path(__file__).parent / "fixtures" / "valkey_clone"


def _pull(number: int, merge_sha: str) -> MagicMock:
    pull = MagicMock()
    pull.number = number
    pull.title = f"Release change {number}"
    pull.body = f"User-facing change from PR {number}."
    pull.html_url = f"https://example.test/pull/{number}"
    pull.user.login = "developer"
    pull.labels = [SimpleNamespace(name="release-notes")]
    pull.merge_commit_sha = merge_sha
    pull.head.ref = f"feature/{number}"
    return pull


def _clone(remote: Path, destination: Path) -> str:
    run_git(None, "clone", "-q", "--branch", "9.1", str(remote), str(destination))
    return str(destination)


def _cut(repo: MagicMock, clone_dir: str) -> int:
    return cut_mod.cut(
        repo,
        repo_full_name="valkey-io/valkey",
        source_clone_dir=clone_dir,
        valkey_clone_dir=clone_dir,
        version="9.1.0",
        stage="rc1",
        urgency="LOW",
        date="2026-07-20",
        tag_glob=None,
        base_ref=None,
        contrib_base_ref=None,
        security_fixes=None,
        token="token",
        git_env={},
        dry_run=False,
    )


def test_rerun_regenerates_latest_tip_and_updates_same_open_pr(
    monkeypatch, tmp_path
) -> None:
    """A second dispatch replaces the prep branch and edits, not duplicates, its PR."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    shutil.copytree(_FIXTURE_CLONE, work)
    run_git(str(work), "init", "-q", "-b", "9.1")
    run_git(str(work), "config", "user.name", "Test User")
    run_git(str(work), "config", "user.email", "test@example.test")
    run_git(str(work), "add", ".")
    run_git(str(work), "commit", "-q", "-m", "Release line baseline")
    run_git(str(work), "tag", "9.0.0")
    run_git(str(work), "commit", "-q", "--allow-empty", "-m", "First change (#40)")
    first_change_sha = git_output(str(work), "rev-parse", "HEAD").strip()

    run_git(None, "init", "-q", "--bare", str(remote))
    run_git(str(work), "remote", "add", "origin", str(remote))
    run_git(str(work), "push", "-q", "-u", "origin", "9.1")
    run_git(str(work), "push", "-q", "origin", "9.0.0")

    pulls = {40: _pull(40, first_change_sha)}
    repo = MagicMock()
    repo.get_pull.side_effect = lambda number: pulls[number]

    existing = MagicMock(number=77, html_url="https://example.test/pull/77", draft=False)

    def _convert_to_draft() -> None:
        existing.draft = True

    def _mark_ready() -> None:
        existing.draft = False

    existing.convert_to_draft.side_effect = _convert_to_draft
    existing.mark_ready_for_review.side_effect = _mark_ready
    repo.get_pulls.side_effect = [[], [existing]]
    repo.create_pull.return_value = existing

    def _generate(prs, **_kwargs):
        return GenerationResult(
            bullets=tuple(
                CategorizedBullet(
                    pr_number=pr.number,
                    author=pr.author,
                    category="Bug Fixes",
                    text=f"Process release entry {pr.number}",
                )
                for pr in prs
            )
        )

    monkeypatch.setattr(pipeline_mod.generate_mod, "generate", _generate)
    monkeypatch.setattr(cut_mod, "_assert_origin_url", lambda *_args: None)
    monkeypatch.setattr(cut_mod, "_contrib_base", lambda *_args, **_kwargs: None)

    prep_branch = "agent/release-cut/9.1.0-rc1"
    first_clone = _clone(remote, tmp_path / "first-clone")
    assert _cut(repo, first_clone) == 0
    first_prep_sha = git_output(str(remote), "rev-parse", prep_branch).strip()
    first_notes = git_output(str(remote), "show", f"{prep_branch}:00-RELEASENOTES")
    assert "(#40)" in first_notes
    assert "(#41)" not in first_notes

    run_git(str(work), "commit", "-q", "--allow-empty", "-m", "Later change (#41)")
    second_change_sha = git_output(str(work), "rev-parse", "HEAD").strip()
    pulls[41] = _pull(41, second_change_sha)
    run_git(str(work), "push", "-q", "origin", "9.1")

    second_clone = _clone(remote, tmp_path / "second-clone")
    assert _cut(repo, second_clone) == 0

    second_prep_sha = git_output(str(remote), "rev-parse", prep_branch).strip()
    second_prep_parent = git_output(str(remote), "rev-parse", f"{prep_branch}^").strip()
    second_notes = git_output(str(remote), "show", f"{prep_branch}:00-RELEASENOTES")

    assert second_prep_sha != first_prep_sha
    assert second_prep_parent == second_change_sha
    assert second_notes.count("(#40)") == 1
    assert second_notes.count("(#41)") == 1

    repo.create_pull.assert_called_once()
    assert repo.create_pull.call_args.kwargs["head"] == prep_branch
    existing.edit.assert_called_once()
    existing.convert_to_draft.assert_called_once()
    existing.mark_ready_for_review.assert_called_once()
    assert existing.draft is False
