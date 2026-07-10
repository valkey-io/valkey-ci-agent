"""Tests for release-range discovery.

Builds real local git repositories in ``tmp_path`` (commits with ``(#N)``
subjects, tags) to exercise tag resolution, range listing, and PR dedup; the
commit->PR API fallback and PR hydration are tested against MagicMock repos.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.common.proc import git_output, run_git
from scripts.release_notes import discover as discover_mod
from scripts.release_notes.discover import (
    _MAX_PR_BODY_CHARS,
    _clean_pr_body,
    _pr_from_commit_api,
    _resolve_base_ref,
    hydrate_prs,
    list_range_commits,
    resolve_commit_prs,
    resolve_last_tag,
)


def _init_repo(path) -> str:
    repo = str(path)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    return repo


def _commit(repo: str, subject: str) -> str:
    # Empty commits keep the test fast and the subject is all discovery reads.
    run_git(repo, "commit", "-q", "--allow-empty", "-m", subject)
    return git_output(repo, "rev-parse", "HEAD").strip()


class TestResolveLastTag:
    def test_returns_nearest_tag_by_graph(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "old (#1)")
        run_git(repo, "tag", "9.1.0-rc1")
        _commit(repo, "newer (#2)")
        tag, sha = resolve_last_tag(repo, "main")
        assert tag == "9.1.0-rc1"
        assert sha == git_output(repo, "rev-list", "-n", "1", "9.1.0-rc1").strip()

    def test_picks_most_recent_of_several(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.1.0-rc1")
        _commit(repo, "b (#2)")
        run_git(repo, "tag", "9.1.0-rc2")
        _commit(repo, "c (#3)")
        tag, _ = resolve_last_tag(repo, "main")
        assert tag == "9.1.0-rc2"

    def test_glob_restricts_line(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.1.0-rc1")
        _commit(repo, "b (#2)")
        run_git(repo, "tag", "8.0.0")  # different line, more recent
        tag, _ = resolve_last_tag(repo, "main", tag_glob="9.1.*")
        assert tag == "9.1.0-rc1"

    def test_no_tag_raises(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "only (#1)")
        with pytest.raises(ValueError):
            resolve_last_tag(repo, "main")

    def test_timeout_propagates_not_masked_as_no_tag(self, tmp_path, monkeypatch) -> None:
        # A hung `git describe` (TimeoutExpired) is an operational failure, not
        # "no baseline tag": it must propagate, not be disguised as a missing tag
        # that would send the caller to a wrong baseline. Only a non-zero exit
        # (CalledProcessError) maps to the "no tag reachable" ValueError.
        import subprocess

        repo = _init_repo(tmp_path)
        _commit(repo, "only (#1)")

        def _hang(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git describe", timeout=300)

        monkeypatch.setattr(discover_mod, "git_output", _hang)
        with pytest.raises(subprocess.TimeoutExpired):
            resolve_last_tag(repo, "main")


class TestListRangeCommits:
    def test_lists_range_oldest_first(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        _commit(repo, "first (#2)")
        _commit(repo, "second (#3)")
        commits = list_range_commits(repo, "base", "main")
        subjects = [s for _, s, _ in commits]
        assert subjects == ["first (#2)", "second (#3)"]

    def test_excludes_base(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        commits = list_range_commits(repo, "base", "main")
        assert commits == []

    def test_captures_multiline_body(self, tmp_path) -> None:
        # The body is carried alongside the subject so discovery can recover the
        # original PR from an ## Applied table or a -x trailer; a multi-line body
        # (the common case) must survive the record split intact.
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        run_git(
            repo, "commit", "-q", "--allow-empty",
            "-m", "squash backport (#50)",
            "-m", "## Applied\n\n| Source PR | Title |\n|---|---|\n| #10 | feat |",
        )
        commits = list_range_commits(repo, "base", "main")
        assert len(commits) == 1
        _sha, subject, body = commits[0]
        assert subject == "squash backport (#50)"
        assert "## Applied" in body and "#10" in body


class TestResolveCommitPrs:
    def test_subject_parse_and_dedup(self, tmp_path) -> None:
        # Two commits carrying the same trailing (#N) collapse to one PR.
        commits = [
            ("sha1", "feature (#10)", ""),
            ("sha2", "backport feature (#10)", ""),
            ("sha3", "fix (#11)", ""),
        ]
        repo = MagicMock()
        pr_to_sha, unresolved = resolve_commit_prs(repo, commits)
        assert set(pr_to_sha) == {10, 11}
        assert pr_to_sha[10] == "sha1"  # first occurrence wins
        assert unresolved == []
        repo.get_commit.assert_not_called()  # subject parse never hit the API

    def test_api_fallback_when_no_trailing_ref(self) -> None:
        repo = MagicMock()
        pull = MagicMock(number=77)
        repo.get_commit.return_value.get_pulls.return_value = [pull]
        pr_to_sha, unresolved = resolve_commit_prs(repo, [("shaX", "direct push, no ref", "")])
        assert pr_to_sha == {77: "shaX"}
        assert unresolved == []

    def test_commit_with_no_pr_is_unresolved_not_dropped(self) -> None:
        # A commit that resolves to no PR must surface as unresolved (a shipped
        # change that would otherwise vanish past the label-only gate), not be
        # silently dropped.
        repo = MagicMock()
        repo.get_commit.return_value.get_pulls.return_value = []
        pr_to_sha, unresolved = resolve_commit_prs(repo, [("shaX", "no ref and no pr", "")])
        assert pr_to_sha == {}
        assert [(u.sha, u.subject) for u in unresolved] == [("shaX", "no ref and no pr")]

    def test_revert_uses_trailing_not_inner_ref(self) -> None:
        # 'Revert "X (#3)" (#9)' belongs to PR 9, not 3.
        repo = MagicMock()
        pr_to_sha, _ = resolve_commit_prs(repo, [("sha", 'Revert "X (#3)" (#9)', "")])
        assert set(pr_to_sha) == {9}

    def test_api_failure_is_unresolved_without_aborting(self) -> None:
        # The riskiest branch: the commit has no trailing (#N) so the API fallback
        # runs, and the API call itself raises (rate limit / network error surviving
        # retries). A RuntimeError is non-retryable, so retry_github_call re-raises
        # at once (no sleeps). The failure must be swallowed to an unresolved commit
        # (discovery keeps going), not propagated to abort the whole run.
        repo = MagicMock()
        repo.get_commit.side_effect = RuntimeError("500 upstream error")
        commits = [
            ("shaX", "hand-applied cherry-pick, no ref", ""),
            ("sha2", "fix (#11)", ""),
        ]
        pr_to_sha, unresolved = resolve_commit_prs(repo, commits)
        # shaX unresolved; the well-formed commit is still resolved.
        assert pr_to_sha == {11: "sha2"}
        assert [u.sha for u in unresolved] == ["shaX"]

    def test_applied_table_recovers_original_over_backport_subject(self) -> None:
        # A squash-merged sweep: the subject is the backport PR (#50), but the
        # ## Applied table names the original source PRs. The originals must win;
        # the backport number must not appear, and the API is never consulted.
        body = (
            "Backport sweep\n\n"
            "## Applied\n\n"
            "| Source PR | Title | Detail |\n"
            "|---|---|---|\n"
            "| #10 | feat a | clean |\n"
            "| #11 | fix b | clean |\n"
        )
        repo = MagicMock()
        pr_to_sha, unresolved = resolve_commit_prs(repo, [("shaSquash", "sweep (#50)", body)])
        assert set(pr_to_sha) == {10, 11}
        assert 50 not in pr_to_sha
        assert unresolved == []
        repo.get_commit.assert_not_called()

    def test_cherry_pick_trailer_recovers_original(self) -> None:
        # A -x cherry-pick whose subject is the backport PR (#60): the trailer
        # names the source commit, whose PR (#12) is the original. The trailer is
        # tried before the subject, so #12 wins over #60.
        body = "port fix\n\n(cherry picked from commit abcdef1234567890)"
        repo = MagicMock()
        pull = MagicMock(number=12)
        repo.get_commit.return_value.get_pulls.return_value = [pull]
        pr_to_sha, unresolved = resolve_commit_prs(repo, [("shaPick", "port fix (#60)", body)])
        assert set(pr_to_sha) == {12}
        assert 60 not in pr_to_sha
        assert unresolved == []
        repo.get_commit.assert_called_once_with("abcdef1234567890")

    def test_cherry_pick_trailer_prefers_oldest_hop(self) -> None:
        # Picked through several branches (unstable -> 9.0 -> 8.0): git -x appends,
        # so the file lists the oldest hop first, most-recent last. The oldest hop
        # (1111...) is the original commit; the newest (2222...) is an intermediate
        # backport. We must resolve the oldest first and credit the ORIGINAL PR
        # (#13), not the intermediate backport PR (#99).
        body = (
            "port fix (#70)\n\n"
            "(cherry picked from commit 1111111111111111)\n"
            "(cherry picked from commit 2222222222222222)\n"
        )
        per_sha = {
            "1111111111111111": [MagicMock(number=13)],  # original
            "2222222222222222": [MagicMock(number=99)],  # intermediate backport
        }
        repo = MagicMock()
        repo.get_commit.side_effect = lambda sha: MagicMock(
            get_pulls=MagicMock(return_value=per_sha[sha])
        )
        pr_to_sha, _ = resolve_commit_prs(repo, [("shaPick", "port fix (#70)", body)])
        assert set(pr_to_sha) == {13}
        assert 99 not in pr_to_sha
        # Oldest hop (first in file) is resolved first and wins on the first hit,
        # so the intermediate hop is never looked up.
        repo.get_commit.assert_called_once_with("1111111111111111")

    def test_falls_back_to_subject_when_trailer_unresolvable(self) -> None:
        # A -x trailer whose source commit the API can't resolve (not in this
        # repo) must fall through to the subject (#80), not become unresolved.
        body = "port fix (#80)\n\n(cherry picked from commit deadbeefdeadbeef)"
        repo = MagicMock()
        repo.get_commit.return_value.get_pulls.return_value = []
        pr_to_sha, unresolved = resolve_commit_prs(repo, [("shaPick", "port fix (#80)", body)])
        assert set(pr_to_sha) == {80}
        assert unresolved == []


class TestPrFromCommitApi:
    def test_returns_none_on_api_error_instead_of_raising(self) -> None:
        # Isolated: a lookup failure returns None (caller drops the commit) rather
        # than letting the exception escape _pr_from_commit_api.
        repo = MagicMock()
        repo.get_commit.side_effect = RuntimeError("network down")
        assert _pr_from_commit_api(repo, "deadbeef") is None


class TestCleanPrBody:
    def test_none_and_empty_become_empty(self) -> None:
        assert _clean_pr_body(None) == ""
        assert _clean_pr_body("") == ""
        assert _clean_pr_body("   \n  ") == ""

    def test_non_string_body_becomes_empty(self) -> None:
        # PyGithub types body as str, but a mis-parsed payload could hand back a
        # non-string; it must degrade to "" rather than crash the cut.
        from unittest.mock import MagicMock
        assert _clean_pr_body(MagicMock()) == ""
        assert _clean_pr_body(123) == ""

    def test_strips_html_comments(self) -> None:
        # PR templates render guidance/checklists as HTML comments; drop them.
        body = "Real summary.\n<!-- please fill this in\nmultiline -->\nMore text."
        cleaned = _clean_pr_body(body)
        assert "please fill this in" not in cleaned
        assert "Real summary." in cleaned
        assert "More text." in cleaned

    def test_strips_dco_trailers(self) -> None:
        body = "Fixes a bug.\n\nSigned-off-by: Jane Dev <jane@example.com>\nCo-authored-by: Bob <bob@x>"
        cleaned = _clean_pr_body(body)
        assert "Fixes a bug." in cleaned
        assert "Signed-off-by" not in cleaned
        assert "Co-authored-by" not in cleaned

    def test_collapses_blank_runs_left_by_removals(self) -> None:
        # Removing a comment between paragraphs must not leave a 3-blank-line gap.
        body = "Para one.\n\n<!-- comment -->\n\nPara two."
        assert _clean_pr_body(body) == "Para one.\n\nPara two."

    def test_short_body_untouched_except_strip(self) -> None:
        assert _clean_pr_body("  Just a summary.  ") == "Just a summary."

    def test_truncates_long_body_on_word_boundary(self) -> None:
        # A long body is clipped to the cap and gets an ellipsis; the cut lands on
        # a space so the last token is whole (no "wor…" split) since whitespace is
        # frequent near the boundary. Every token is "alpha", so a correct
        # boundary cut ends in a complete "alpha…", never a fragment.
        body = ("alpha " * 1000).strip()  # ~6000 chars, spaces throughout
        cleaned = _clean_pr_body(body)
        assert len(cleaned) <= _MAX_PR_BODY_CHARS + 1  # +1 for the ellipsis char
        assert cleaned.endswith("alpha…")  # whole final token, not a split fragment
        # Body (sans ellipsis) is only whole "alpha" tokens joined by spaces.
        assert set(cleaned[:-1].split()) == {"alpha"}

    def test_truncates_hard_when_no_late_whitespace(self) -> None:
        # A single giant token with no nearby space must still be capped, not left
        # far short by chasing a word boundary that isn't there.
        body = "x" * 5000
        cleaned = _clean_pr_body(body)
        # No usable boundary near the cap, so it clips at the cap (plus ellipsis).
        assert len(cleaned) == _MAX_PR_BODY_CHARS + 1
        assert cleaned.endswith("…")


class TestHydratePrs:
    def test_builds_merged_prs(self) -> None:
        repo = MagicMock()
        pull = MagicMock()
        pull.title = "Fix the thing"
        pull.user.login = "octocat"
        pull.html_url = "https://x/10"
        pull.body = "Fixes a crash when the thing overflows."
        pull.merge_commit_sha = "deadbeef"
        pull.labels = [MagicMock(name="lbl")]
        pull.labels[0].name = "release-notes"
        repo.get_pull.return_value = pull
        prs, unresolved_backports = hydrate_prs(repo, {10: "sha"})
        assert len(prs) == 1
        assert prs[0].number == 10
        assert prs[0].author == "octocat"
        assert prs[0].body == "Fixes a crash when the thing overflows."
        assert prs[0].labels == ("release-notes",)
        assert unresolved_backports == []

    def test_ghost_author_becomes_empty(self) -> None:
        repo = MagicMock()
        pull = MagicMock()
        pull.title = "t"
        pull.user = None
        pull.html_url = "u"
        pull.body = None  # a PR with no description
        pull.merge_commit_sha = ""
        pull.labels = []
        repo.get_pull.return_value = pull
        prs, _ = hydrate_prs(repo, {5: "sha5"})
        assert prs[0].author == ""
        assert prs[0].body == ""  # None body coerces to ""
        assert prs[0].merge_commit_sha == "sha5"  # falls back to the commit sha

    def test_pr_404_is_skipped(self) -> None:
        # A 404 is a non-PR reference (an issue, or a (#N) from another
        # repo); skip it rather than aborting the run.
        from github.GithubException import UnknownObjectException

        repo = MagicMock()
        repo.get_pull.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
        assert hydrate_prs(repo, {404: "sha"}) == ([], [])

    def test_non_404_github_error_is_reraised(self) -> None:
        # A 5xx that outlasts retries must not be swallowed: dropping a real
        # release-noted PR would ship it un-noted, and the label gate won't catch it.
        from github.GithubException import GithubException

        repo = MagicMock()
        repo.get_pull.side_effect = GithubException(500, {"message": "Server Error"}, {})
        with pytest.raises(GithubException):
            hydrate_prs(repo, {7: "sha"})


def _pull(number, *, title="a change", author="dev", url=None, body="",
          labels=(), commit_subjects=None, head_ref="", merge_sha="msha"):
    """Build a MagicMock PyGithub pull. ``labels`` is a tuple of label-name strings."""
    pull = MagicMock()
    pull.number = number
    pull.title = title
    pull.user = MagicMock() if author is not None else None
    if author is not None:
        pull.user.login = author
    pull.html_url = url if url is not None else f"https://x/{number}"
    pull.body = body
    pull.merge_commit_sha = merge_sha
    label_mocks = []
    for name in labels:
        m = MagicMock()
        m.name = name
        label_mocks.append(m)
    pull.labels = label_mocks
    pull.head.ref = head_ref
    if commit_subjects is None:
        # A plain PR should never have its commits read; make it explode if it does.
        pull.get_commits.side_effect = AssertionError("get_commits should not be called")
    else:
        commits = []
        for subject in commit_subjects:
            c = MagicMock()
            c.commit.message = subject
            commits.append(c)
        pull.get_commits.return_value = commits
    return pull


class TestHydratePrsBackportRecovery:
    """Per-PR [Backport ...] PRs resolved for a range commit are walked back to
    their original source PR, so the note credits the change's author (and the
    original labels drive classification), never the backport."""

    def _repo(self, pulls):
        # pulls: {number: MagicMock pull}. get_pull raises 404 for unknown numbers.
        from github.GithubException import UnknownObjectException

        repo = MagicMock()

        def _get_pull(number):
            if number in pulls:
                return pulls[number]
            raise UnknownObjectException(404, {"message": "Not Found"}, {})

        repo.get_pull.side_effect = _get_pull
        return repo

    def test_recovers_via_backport_summary_row(self) -> None:
        # The backport PR (#500) carries a ## Backport Summary naming source #7.
        # Identity is taken from #7; the backport PR's own commits are never read.
        backport = _pull(
            500, title="[Backport 9.1] Fix a leak", labels=("backport",),
            body=(
                "## Backport Summary\n\nClean.\n\n"
                "| Field | Value |\n|---|---|\n"
                "| Source PR | [#7](https://x/7) |\n"
            ),
        )
        source = _pull(7, title="Fix a leak", author="alice", body="the real body",
                       labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        prs, unresolved_backports = hydrate_prs(repo, {500: "shaBackport"})
        assert len(prs) == 1
        assert prs[0].number == 7
        assert prs[0].title == "Fix a leak"
        assert prs[0].author == "alice"
        assert prs[0].labels == ("release-notes",)
        assert prs[0].body == "the real body"
        # merge_commit_sha is the backport's range commit on THIS line, not the
        # source's merge on unstable (which is where source.merge_commit_sha points).
        assert prs[0].merge_commit_sha == "shaBackport"
        backport.get_commits.assert_not_called()
        assert unresolved_backports == []  # source recovered -> nothing to flag

    def test_recovers_via_pr_commits_when_no_summary(self) -> None:
        # No ## Backport Summary in the body: fall back to the backport PR's own
        # commits, whose trailing (#N) still names the original.
        backport = _pull(
            500, title="[Backport 9.1] Fix a leak", labels=("backport",),
            body="no summary here", commit_subjects=["Fix a leak (#7)"],
        )
        source = _pull(7, title="Fix a leak", author="alice", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        prs, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [7]
        assert prs[0].author == "alice"

    def test_recovers_via_branch_name_last(self) -> None:
        # No summary and the PR's commits carry no resolvable (#N): the head branch
        # backport/<n>-to-<x> still names the source.
        backport = _pull(
            500, title="[Backport 9.1] Fix a leak", labels=("backport",),
            body="", commit_subjects=["Fix a leak with no ref"],
            head_ref="backport/55-to-9.1",
        )
        source = _pull(55, title="Fix a leak", author="bob", labels=("release-notes",))
        repo = self._repo({500: backport, 55: source})
        prs, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [55]
        assert prs[0].author == "bob"

    def test_unrecoverable_backport_credits_backport_not_dropped(self, caplog) -> None:
        # A backport with no summary, no resolvable PR-commit (#N), no backport
        # branch: keep crediting the backport (never drop), warn, and flag it so
        # the PR body surfaces the suspect credit (not only the CI log).
        backport = _pull(
            500, title="[Backport 9.1] Fix a leak", labels=("backport",),
            body="", commit_subjects=["Fix a leak with no ref"],
            head_ref="some/other-branch",
        )
        repo = self._repo({500: backport})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert any("itself a backport" in r.message for r in caplog.records)
        assert [(b.number, b.title) for b in unresolved_backports] == [
            (500, "[Backport 9.1] Fix a leak")
        ]

    def test_source_404_falls_back_to_backport(self, caplog) -> None:
        # The recovered source PR does not resolve (deleted / cross-repo): keep the
        # backport rather than drop the change, and flag it as unresolved.
        backport = _pull(
            500, title="[Backport 9.1] Fix a leak", labels=("backport",),
            body=(
                "## Backport Summary\n\n| Field | Value |\n|---|---|\n"
                "| Source PR | #7 |\n"
            ),
        )
        repo = self._repo({500: backport})  # #7 absent -> 404
        with caplog.at_level("WARNING"):
            prs, unresolved_backports = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert [b.number for b in unresolved_backports] == [500]

    def test_two_backports_of_one_source_dedup(self) -> None:
        # Two per-PR backports (#500, #501) both trace to source #7 -> one entry.
        b1 = _pull(500, title="[Backport 9.1] Fix", labels=("backport",),
                   body="## Backport Summary\n\n| Field | Value |\n|---|---|\n| Source PR | #7 |\n")
        b2 = _pull(501, title="[Backport 9.0] Fix", labels=("backport",),
                   body="## Backport Summary\n\n| Field | Value |\n|---|---|\n| Source PR | #7 |\n")
        source = _pull(7, title="Fix", author="alice", labels=("release-notes",))
        repo = self._repo({500: b1, 501: b2, 7: source})
        prs, unresolved_backports = hydrate_prs(repo, {500: "shaA", 501: "shaB"})
        assert [p.number for p in prs] == [7]
        # First-seen backport (#500) wins the dedup; its range sha is recorded.
        assert prs[0].merge_commit_sha == "shaA"
        assert unresolved_backports == []  # both resolved to #7

    def test_source_also_present_as_direct_pr_dedup(self) -> None:
        # #7 is in the range directly AND via backport #500 -> one entry keyed 7.
        source = _pull(7, title="Fix", author="alice", labels=("release-notes",))
        backport = _pull(
            500, title="[Backport 9.1] Fix", labels=("backport",),
            body="## Backport Summary\n\n| Field | Value |\n|---|---|\n| Source PR | #7 |\n",
        )
        repo = self._repo({7: source, 500: backport})
        prs, _ = hydrate_prs(repo, {7: "shaDirect", 500: "shaBackport"})
        assert [p.number for p in prs] == [7]
        assert prs[0].author == "alice"

    def test_plain_pr_never_reads_commits_or_remaps(self) -> None:
        # A normal (non-backport) PR: exactly one get_pull, commits never read.
        plain = _pull(10, title="A normal change", labels=("release-notes",))
        repo = self._repo({10: plain})
        prs, unresolved_backports = hydrate_prs(repo, {10: "sha"})
        assert [p.number for p in prs] == [10]
        assert repo.get_pull.call_count == 1
        plain.get_commits.assert_not_called()
        assert unresolved_backports == []

    def test_backport_titled_pr_with_no_source_not_remapped(self, caplog) -> None:
        # A PR whose title merely starts with "[Backport ..]" but has no summary,
        # no PR-commit (#N), no backport branch is not remapped: credit itself, warn,
        # and flag it (it is still credited to a backport-looking PR).
        odd = _pull(
            10, title="[Backport 9.1] but actually authored here",
            labels=(), body="", commit_subjects=["some work, no ref"],
            head_ref="feature/x",
        )
        repo = self._repo({10: odd})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports = hydrate_prs(repo, {10: "sha"})
        assert [p.number for p in prs] == [10]
        assert any("itself a backport" in r.message for r in caplog.records)
        assert [b.number for b in unresolved_backports] == [10]

    @staticmethod
    def _summary_backport(number, source, *, title=None):
        # A backport PR whose ## Backport Summary names `source`, so recovery
        # resolves via the row without ever reading its commits.
        return _pull(
            number,
            title=title or f"[Backport 9.1] chained #{number}",
            labels=("backport",),
            body=(
                "## Backport Summary\n\n| Field | Value |\n|---|---|\n"
                f"| Source PR | #{source} |\n"
            ),
        )

    def test_cyclic_backport_summary_terminates(self, caplog) -> None:
        # A malformed pair of ## Backport Summary rows points in a circle:
        # #500's source is #501, #501's source is #500. The `visited` guard must
        # stop the walk (never loop): it reaches #501, sees #501's source (#500)
        # is already visited, halts, and credits #501 as an unresolved backport
        # rather than hanging.
        b500 = self._summary_backport(500, 501)
        b501 = self._summary_backport(501, 500)
        repo = self._repo({500: b500, 501: b501})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports = hydrate_prs(repo, {500: "shaCycle"})
        assert [p.number for p in prs] == [501]  # halted at the last hop before the cycle
        assert [b.number for b in unresolved_backports] == [501]  # still a backport -> flagged
        assert any("itself a backport" in r.message for r in caplog.records)

    def test_backport_chain_bounded_by_max_depth(self, caplog) -> None:
        # A linear chain of per-PR backports #500 -> #501 -> #502 -> #503, each a
        # backport naming the next as its source. _MAX_BACKPORT_DEPTH (2) caps the
        # walk: it advances two hops to #502 and stops before resolving #503, so
        # #502 is credited (and flagged, still a backport). #503 is never fetched.
        b500 = self._summary_backport(500, 501)
        b501 = self._summary_backport(501, 502)
        b502 = self._summary_backport(502, 503)
        b503 = self._summary_backport(503, 504)  # present but must never be fetched
        repo = self._repo({500: b500, 501: b501, 502: b502, 503: b503})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports = hydrate_prs(repo, {500: "shaChain"})
        assert [p.number for p in prs] == [502]  # two hops from #500
        assert [b.number for b in unresolved_backports] == [502]
        # The depth cap stopped the walk before #503 was ever looked up.
        assert 503 not in [c.args[0] for c in repo.get_pull.call_args_list]


class TestDiscover:
    def test_end_to_end_local(self, tmp_path, monkeypatch) -> None:
        repo_dir = _init_repo(tmp_path)
        _commit(repo_dir, "base (#1)")
        run_git(repo_dir, "tag", "9.1.0-rc1")
        _commit(repo_dir, "feat (#2)")
        _commit(repo_dir, "fix (#3)")

        gh_repo = MagicMock()

        def _get_pull(n):
            p = MagicMock()
            p.title = f"PR {n}"
            p.user.login = "dev"
            p.html_url = f"https://x/{n}"
            p.body = f"Body of PR {n}"
            p.merge_commit_sha = ""
            p.labels = []
            return p

        gh_repo.get_pull.side_effect = _get_pull
        result = discover_mod.discover(gh_repo, repo_dir, "main", tag_glob="9.1.*")
        assert result.base_tag == "9.1.0-rc1"
        assert {p.number for p in result.prs} == {2, 3}

    def test_end_to_end_credits_source_of_per_pr_backport(self, tmp_path) -> None:
        # A per-PR backport squash-merged onto the line: its subject is the backport
        # PR (#500), it carries no ## Applied table / -x trailer, so resolve_commit_prs
        # keys it as #500. hydrate_prs then walks it back to source #7 via the
        # backport PR's ## Backport Summary. The rendered range must credit #7.
        repo_dir = _init_repo(tmp_path)
        _commit(repo_dir, "base (#1)")
        run_git(repo_dir, "tag", "9.1.0-rc1")
        _commit(repo_dir, "Fix a leak (#500)")  # squash subject = the backport PR

        def _get_pull(n):
            p = MagicMock()
            p.user.login = "dev" if n == 500 else "alice"
            p.html_url = f"https://x/{n}"
            p.merge_commit_sha = ""
            p.head.ref = ""
            if n == 500:
                p.title = "[Backport 9.1] Fix a leak"
                m = MagicMock()
                m.name = "backport"
                p.labels = [m]
                p.body = (
                    "## Backport Summary\n\n| Field | Value |\n|---|---|\n"
                    "| Source PR | [#7](https://x/7) |\n"
                )
                p.get_commits.side_effect = AssertionError("summary row should win")
            else:  # the original source PR
                p.title = "Fix a leak"
                m = MagicMock()
                m.name = "release-notes"
                p.labels = [m]
                p.body = "the original description"
            return p

        gh_repo = MagicMock()
        gh_repo.get_pull.side_effect = _get_pull
        result = discover_mod.discover(gh_repo, repo_dir, "main", tag_glob="9.1.*")
        assert {p.number for p in result.prs} == {7}
        (pr,) = result.prs
        assert pr.author == "alice"
        assert pr.title == "Fix a leak"
        assert pr.labels == ("release-notes",)

    def test_explicit_base_ref_overrides_tag(self, tmp_path) -> None:
        # A repo with no tags (like a fork): tag resolution would raise, but an
        # explicit base_ref makes the range base_ref..head work directly.
        repo_dir = _init_repo(tmp_path)
        _commit(repo_dir, "root (#1)")
        run_git(repo_dir, "branch", "base")
        _commit(repo_dir, "feat (#2)")

        gh_repo = MagicMock()

        def _get_pull(n):
            p = MagicMock()
            p.title = f"PR {n}"
            p.user.login = "dev"
            p.html_url = f"https://x/{n}"
            p.body = f"Body of PR {n}"
            p.merge_commit_sha = ""
            p.labels = []
            return p

        gh_repo.get_pull.side_effect = _get_pull
        result = discover_mod.discover(gh_repo, repo_dir, "main", base_ref="base")
        assert result.base_tag == "base"
        assert {p.number for p in result.prs} == {2}  # only commits after base

    def test_base_ref_resolves_via_remote_tracking_ref(self, tmp_path) -> None:
        # Mirror the real cut: `git clone --branch <src>` leaves every OTHER
        # branch reachable only as origin/<name>. A base_ref naming such a branch
        # must resolve via the remote-tracking ref, and the resolved name must
        # carry into the range so base..head still excludes the base commit.
        (tmp_path / "upstream").mkdir()
        upstream = _init_repo(tmp_path / "upstream")
        _commit(upstream, "root (#1)")
        run_git(upstream, "branch", "unstable")  # baseline lives on its own branch
        run_git(upstream, "checkout", "-q", "main")
        _commit(upstream, "feat (#2)")

        clone_dir = str(tmp_path / "clone")
        # Single-branch clone of main only; 'unstable' is now origin/unstable.
        run_git(None, "clone", "-q", "--branch", "main", upstream, clone_dir)
        with pytest.raises(Exception):  # noqa: B017 - bare name does not resolve locally
            git_output(clone_dir, "rev-parse", "--verify", "unstable")

        gh_repo = MagicMock()

        def _get_pull(n):
            p = MagicMock()
            p.title = f"PR {n}"
            p.user.login = "dev"
            p.html_url = f"https://x/{n}"
            p.body = f"Body of PR {n}"
            p.merge_commit_sha = ""
            p.labels = []
            return p

        gh_repo.get_pull.side_effect = _get_pull
        result = discover_mod.discover(gh_repo, clone_dir, "main", base_ref="unstable")
        assert result.base_tag == "origin/unstable"  # fell back to remote-tracking ref
        assert {p.number for p in result.prs} == {2}  # only commits after the baseline

    def test_unresolvable_base_ref_raises_valueerror_naming_ref(self, tmp_path) -> None:
        # A --base-ref that resolves neither as given nor as origin/<name> (a
        # typo'd branch/tag) must raise a ValueError naming the ref, mirroring
        # resolve_last_tag, not leak a raw CalledProcessError from the fallback
        # rev-parse.
        repo = _init_repo(tmp_path)
        _commit(repo, "root (#1)")
        with pytest.raises(ValueError, match="no-such-ref"):
            _resolve_base_ref(repo, "no-such-ref")
