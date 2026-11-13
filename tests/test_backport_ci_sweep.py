"""The CI-sweep PR kept on its sweep's head as the sweep rebases and takes new backports."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from scripts.backport import ci_sweep
from tests.git_helpers import commit, git

_FIX_A = "Fix test-a\n\nCI-Fix-Job: test-a\nCI-Fix-Run: https://x/runs/1"
_FIX_B = "Fix test-b\n\nCI-Fix-Job: test-b"
_FIX_C = "Fix test-c\n\nCI-Fix-Job: test-c"


def _repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A release branch, its sweep with one backport, and a CI-sweep branch with three fixes on it.

    Then the release branch moves and the sweep is rebuilt on it, as the sweep
    does, with a new backport that makes fix A's change and conflicts with
    fix B's. Returns the repository, the new sweep head, and the CI-sweep head.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "9.0")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.com")
    commit(repo, "base.txt", "base\n", "Base")
    git(repo, "checkout", "-q", "-b", "sweep")
    backport = commit(repo, "backport.txt", "a backport\n", "Backport x (#1)\n\nBackport-Source-PR: 1")
    git(repo, "checkout", "-q", "-b", "ci")
    commit(repo, "a.txt", "fixed a\n", _FIX_A)
    commit(repo, "b.txt", "fixed b one way\n", _FIX_B)
    commit(repo, "c.txt", "fixed c\n", _FIX_C)
    ci = git(repo, "rev-parse", "HEAD")
    # The release branch moves; the sweep is rebased onto it, and takes a new backport.
    git(repo, "checkout", "-q", "9.0")
    commit(repo, "release.txt", "a release commit\n", "Release commit")
    git(repo, "checkout", "-q", "-B", "sweep")
    git(repo, "cherry-pick", backport)
    git(repo, "checkout", "-q", "-b", "new-backport")
    (repo / "a.txt").write_text("fixed a\n", encoding="utf-8")
    (repo / "b.txt").write_text("fixed b another way\n", encoding="utf-8")
    git(repo, "add", "a.txt", "b.txt")
    git(repo, "commit", "-q", "-m", "Backport y (#2)\n\nBackport-Source-PR: 2")
    git(repo, "checkout", "-q", "sweep")
    git(repo, "merge", "-q", "--ff-only", "new-backport")
    return repo, git(repo, "rev-parse", "HEAD"), ci


def _pull(ci: str, *, sweep: int = 7) -> SimpleNamespace:
    return SimpleNamespace(number=61, head=SimpleNamespace(sha=ci), body=ci_sweep.body("9.0", sweep, []))


def test_a_rebased_sweep_keeps_the_fixes_it_still_needs(tmp_path) -> None:
    repo, sweep_head, ci = _repo(tmp_path)

    plan = ci_sweep.plan(str(repo), pull=_pull(ci), ci_sweep=ci, sweep_head=sweep_head, sweep_pr=7)

    subjects = {fix.subject: fix.sha for fix in ci_sweep.fixes_on(str(repo), sweep_head, ci)[0]}
    assert plan.keep == (subjects["Fix test-c"],)
    assert {(subject, job, why) for _sha, subject, job, why in plan.dropped} == {
        ("Fix test-a", "test-a", ci_sweep.DROPPED_ON_SWEEP),
        ("Fix test-b", "test-b", ci_sweep.DROPPED_CONFLICT),
    }
    assert not plan.close and not plan.foreign

    # Publication replays it: the sweep's head, then the fixes kept, nothing else.
    head = ci_sweep.replay(str(repo), sweep_head, ci, plan.keep)
    assert git(repo, "rev-parse", f"{head}~1") == sweep_head
    assert git(repo, "diff", "--name-only", sweep_head, head) == "c.txt"


def test_someone_elses_commit_on_the_ci_sweep_branch_stops_the_rebuild(tmp_path) -> None:
    repo, sweep_head, ci = _repo(tmp_path)
    git(repo, "checkout", "-q", ci)
    theirs = commit(repo, "manual.txt", "a maintainer's change\n", "Adjust the fix")

    plan = ci_sweep.plan(str(repo), pull=_pull(theirs), ci_sweep=theirs, sweep_head=sweep_head, sweep_pr=7)

    assert plan.foreign and not plan.keep and not plan.dropped


def test_a_ci_sweep_pr_for_an_earlier_sweep_pr_is_closed(tmp_path) -> None:
    """Its sweep PR was merged or closed without it; the next sweep's failures are fixed again."""
    repo, sweep_head, ci = _repo(tmp_path)

    plan = ci_sweep.plan(str(repo), pull=_pull(ci, sweep=6), ci_sweep=ci, sweep_head=sweep_head, sweep_pr=7)
    assert plan.close == "#6 was merged or closed without it"

    plan = ci_sweep.plan(str(repo), pull=_pull(ci, sweep=6), ci_sweep=ci, sweep_head=sweep_head, sweep_pr=None)
    assert plan.close == "#6 was merged or closed without it"


def test_a_fix_whose_file_a_new_backport_changed_elsewhere_is_reviewed(tmp_path) -> None:
    """A backport can fix the same test another way without touching the fix's lines."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "9.0")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.com")
    commit(repo, "test.tcl", "one\ntwo\nthree\nfour\nfive\n", "Base")
    git(repo, "checkout", "-q", "-b", "ci")
    commit(repo, "test.tcl", "one fixed\ntwo\nthree\nfour\nfive\n", "Fix test-d\n\nCI-Fix-Job: test-d")
    commit(repo, "other.tcl", "fixed e\n", "Fix test-e\n\nCI-Fix-Job: test-e")
    ci = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "sweep", "9.0")
    sweep_head = commit(
        repo, "test.tcl", "one\ntwo\nthree\nfour\nfive fixed\n", "Backport z (#3)\n\nBackport-Source-PR: 3",
    )
    reviewed = []

    def review(fix, backports):
        reviewed.append(fix.job)
        assert "Backport z (#3)" in backports and "+five fixed" in backports
        return False, "Backport z (#3) removes the race the fix waits out"

    plan = ci_sweep.plan(str(repo), pull=_pull(ci), ci_sweep=ci, sweep_head=sweep_head, sweep_pr=7, review=review)

    # Only the fix whose file the backport changed is reviewed.
    assert reviewed == ["test-d"]
    assert [job for _sha, _subject, job, _why in plan.dropped] == ["test-d"]
    assert plan.dropped[0][3] == "Backport z (#3) removes the race the fix waits out"
    assert len(plan.keep) == 1


def _gh(pull, sweep):
    repo = MagicMock()
    repo.get_pull.side_effect = lambda number: {61: pull, 7: sweep}[number]
    gh = MagicMock()
    gh.get_repo.return_value = repo
    return gh


def test_publication_pushes_the_rebuilt_branch_and_rewrites_its_table(tmp_path) -> None:
    repo, sweep_head, ci = _repo(tmp_path)
    git(repo, "update-ref", ci_sweep.PLANNED_REF, ci)
    plan = ci_sweep.plan(str(repo), pull=_pull(ci), ci_sweep=ci, sweep_head=sweep_head, sweep_pr=7)
    pull = SimpleNamespace(
        number=61, state="open", head=SimpleNamespace(sha=ci), create_issue_comment=MagicMock(), edit=MagicMock(),
    )
    sweep = SimpleNamespace(
        number=7, edit=MagicMock(),
        body="# Backport sweep for 9.0\n\nAutomated.\n\n## Applied\n\n| a |\n\n---\n*Generated*",
    )
    pushed = []

    ci_sweep.apply(
        _gh(pull, sweep), str(repo), repo_full_name="o/r", release="9.0", plan=plan,
        sweep_head=sweep_head, sweep_pr=7, push=lambda _worktree, head, lease: pushed.append((head, lease)),
    )

    ((head, lease),) = pushed
    assert lease == ci and git(repo, "rev-parse", f"{head}~1") == sweep_head
    (comment,), _ = pull.create_issue_comment.call_args
    assert comment.startswith("Dropped from this PR on the sweep's new head:\n\n")
    assert "Fix test-a (`test-a`): the sweep has the same change." in comment
    text = pull.edit.call_args.kwargs["body"]
    assert f"| {head} | `test-c` | Fix test-c |  |" in text and "test-a" not in text
    # The sweep PR is pointed at it by the sweep's publication (sweep._apply_ci_sweep), plan or not.
    sweep.edit.assert_not_called()


def test_publication_closes_a_ci_sweep_pr_with_no_fix_left_or_for_an_earlier_sweep(tmp_path) -> None:
    pull = SimpleNamespace(
        number=61, state="open", head=SimpleNamespace(sha="c" * 40), create_issue_comment=MagicMock(), edit=MagicMock(),
    )
    for plan in (
        ci_sweep.Plan(61, "c" * 40, close="#6 was merged or closed without it"),
        ci_sweep.Plan(61, "c" * 40, dropped=(("a" * 40, "Fix test-a", "test-a", ci_sweep.DROPPED_ON_SWEEP),)),
    ):
        pull.edit.reset_mock()
        ci_sweep.apply(
            _gh(pull, None), "unused", repo_full_name="o/r", release="9.0", plan=plan,
            sweep_head="d" * 40, sweep_pr=7, push=lambda *_a: (_ for _ in ()).throw(AssertionError("pushed")),
        )
        pull.edit.assert_called_once_with(state="closed")
    first, second = (call.args[0] for call in pull.create_issue_comment.call_args_list)
    assert first == "Closing: #6 was merged or closed without it. Failures that remain are fixed again for the sweep."
    assert second.endswith("Closing: no fix is left.")


def test_a_ci_sweep_pr_that_moved_since_it_was_planned_is_left_for_the_next_sweep() -> None:
    pull = SimpleNamespace(number=61, state="open", head=SimpleNamespace(sha="e" * 40), edit=MagicMock())

    ci_sweep.apply(
        _gh(pull, None), "unused", repo_full_name="o/r", release="9.0",
        plan=ci_sweep.Plan(61, "c" * 40, close="x"), sweep_head="d" * 40, sweep_pr=7, push=lambda *_a: None,
    )

    pull.edit.assert_not_called()


def test_the_sweep_line_goes_at_the_top_once() -> None:
    text = (
        '# Backport sweep for 9.0\n\nAutomated cherry-picks from PRs marked "To be backported".\n\n'
        "## Applied\n\n| a |\n\n---\n*Generated by valkey-ci-agent using Claude Code.*"
    )

    once = ci_sweep.with_sweep_line(text, 61)

    assert once.startswith(
        '# Backport sweep for 9.0\n\nAutomated cherry-picks from PRs marked "To be backported".\n\n'
        "CI fixes for this sweep are in #61.\n\n## Applied\n\n| a |\n"
    )
    assert ci_sweep.with_sweep_line(once, 61) == once
    # One left lower down by an earlier version, or naming an earlier CI-sweep PR, is replaced.
    lower = text.replace("\n---\n", "\nCI fixes for this sweep are in #50.\n\n---\n")
    assert ci_sweep.with_sweep_line(lower, 61) == once


def test_a_backport_the_rebase_changed_is_the_old_sweep_not_someone_elses(tmp_path) -> None:
    """A moved context line, or a conflict resolved in the rebase, gives a backport a new patch."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "9.0")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.com")
    commit(repo, "src.c", "a\nb\nc\nd\ne\n", "Base")
    git(repo, "checkout", "-q", "-b", "sweep")
    commit(repo, "src.c", "a\nb\nc changed\nd\ne\n", "Backport x (#1)\n\nBackport-Source-PR: 1")
    git(repo, "checkout", "-q", "-b", "ci")
    commit(repo, "test.tcl", "fixed\n", "Wait for it\n\nCI-Fix-Job: test-a")
    ci = git(repo, "rev-parse", "HEAD")
    # The release branch changes the line next to the backport's, and the sweep is rebuilt on it.
    git(repo, "checkout", "-q", "-B", "sweep", "9.0")
    commit(repo, "src.c", "a\nb changed upstream\nc\nd\ne\n", "Release commit")
    commit(repo, "src.c", "a\nb changed upstream\nc changed\nd\ne\n", "Backport x (#1)\n\nBackport-Source-PR: 1")
    sweep_head = git(repo, "rev-parse", "HEAD")
    assert git(repo, "cherry", sweep_head, ci).splitlines()[0].startswith("+")  # git cherry alone says: not the sweep's
    reviewed = []

    plan = ci_sweep.plan(
        str(repo), pull=_pull(ci), ci_sweep=ci, sweep_head=sweep_head, sweep_pr=7,
        review=lambda fix, backports: reviewed.append(backports) or (True, ""),
    )

    assert not plan.foreign and len(plan.keep) == 1 and not plan.dropped
    # The rebased backport is not a new one, so nothing asks whether the fix is still needed.
    assert reviewed == []


def test_a_commit_above_the_fixes_that_is_not_a_fix_is_someone_elses(tmp_path) -> None:
    repo, sweep_head, ci = _repo(tmp_path)
    git(repo, "checkout", "-q", ci)
    theirs = commit(repo, "manual.txt", "a maintainer's change\n", "Adjust the fix")

    adds, _has, foreign = ci_sweep.fixes_on(str(repo), sweep_head, theirs)

    assert foreign and [fix.job for fix in adds] == ["test-a", "test-b", "test-c"]


def test_the_new_backports_given_to_the_review_are_capped(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "9.0")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.com")
    commit(repo, "test.tcl", "one\n" + "x\n" * 50 + "two\n", "Base")
    git(repo, "checkout", "-q", "-b", "ci")
    commit(repo, "test.tcl", "one fixed\n" + "x\n" * 50 + "two\n", "Fix test-d\n\nCI-Fix-Job: test-d")
    ci = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", "sweep", "9.0")
    # Far from the fix's lines: no conflict, but a large diff to the same file.
    sweep_head = commit(
        repo, "test.tcl", "one\n" + "x\n" * 50 + "two\n" + "y\n" * 200, "Backport z (#3)\n\nBackport-Source-PR: 3",
    )
    monkeypatch.setattr(ci_sweep, "_REVIEW_DIFF_CHARS", 200)
    seen = []

    ci_sweep.plan(
        str(repo), pull=_pull(ci), ci_sweep=ci, sweep_head=sweep_head, sweep_pr=7,
        review=lambda fix, backports: seen.append(backports) or (True, ""),
    )

    (backports,) = seen
    assert len(backports) <= 200 + len("\n[... diff truncated ...]") and backports.endswith("[... diff truncated ...]")
