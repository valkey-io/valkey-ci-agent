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
        subjects = [s for _, s in commits]
        assert subjects == ["first (#2)", "second (#3)"]

    def test_excludes_base(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        commits = list_range_commits(repo, "base", "main")
        assert commits == []


class TestResolveCommitPrs:
    def test_subject_parse_and_dedup(self, tmp_path) -> None:
        # Two commits carrying the same trailing (#N) collapse to one PR.
        commits = [("sha1", "feature (#10)"), ("sha2", "backport feature (#10)"), ("sha3", "fix (#11)")]
        repo = MagicMock()
        pr_to_sha = resolve_commit_prs(repo, commits)
        assert set(pr_to_sha) == {10, 11}
        assert pr_to_sha[10] == "sha1"  # first occurrence wins
        repo.get_commit.assert_not_called()  # subject parse never hit the API

    def test_api_fallback_when_no_trailing_ref(self) -> None:
        repo = MagicMock()
        pull = MagicMock(number=77)
        repo.get_commit.return_value.get_pulls.return_value = [pull]
        pr_to_sha = resolve_commit_prs(repo, [("shaX", "direct push, no ref")])
        assert pr_to_sha == {77: "shaX"}

    def test_commit_with_no_pr_dropped(self) -> None:
        repo = MagicMock()
        repo.get_commit.return_value.get_pulls.return_value = []
        pr_to_sha = resolve_commit_prs(repo, [("shaX", "no ref and no pr")])
        assert pr_to_sha == {}

    def test_revert_uses_trailing_not_inner_ref(self) -> None:
        # 'Revert "X (#3)" (#9)' belongs to PR 9, not 3.
        repo = MagicMock()
        pr_to_sha = resolve_commit_prs(repo, [("sha", 'Revert "X (#3)" (#9)')])
        assert set(pr_to_sha) == {9}

    def test_api_failure_drops_commit_without_aborting(self) -> None:
        # The riskiest branch: the commit has no trailing (#N) so the API fallback
        # runs, and the API call itself raises (rate limit / network error surviving
        # retries). A RuntimeError is non-retryable, so retry_github_call re-raises
        # at once (no sleeps). The failure must be swallowed to a dropped commit
        # (discovery keeps going), not propagated to abort the whole run.
        repo = MagicMock()
        repo.get_commit.side_effect = RuntimeError("500 upstream error")
        commits = [("shaX", "hand-applied cherry-pick, no ref"), ("sha2", "fix (#11)")]
        pr_to_sha = resolve_commit_prs(repo, commits)
        # shaX dropped; the well-formed commit is still resolved.
        assert pr_to_sha == {11: "sha2"}


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
        prs = hydrate_prs(repo, {10: "sha"})
        assert len(prs) == 1
        assert prs[0].number == 10
        assert prs[0].author == "octocat"
        assert prs[0].body == "Fixes a crash when the thing overflows."
        assert prs[0].labels == ("release-notes",)

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
        prs = hydrate_prs(repo, {5: "sha5"})
        assert prs[0].author == ""
        assert prs[0].body == ""  # None body coerces to ""
        assert prs[0].merge_commit_sha == "sha5"  # falls back to the commit sha

    def test_pr_404_is_skipped(self) -> None:
        # A 404 is a genuine non-PR reference (an issue, or a (#N) from another
        # repo); skip it rather than aborting the run.
        from github.GithubException import UnknownObjectException

        repo = MagicMock()
        repo.get_pull.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
        assert hydrate_prs(repo, {404: "sha"}) == []

    def test_non_404_github_error_is_reraised(self) -> None:
        # A 5xx that outlasts retries must not be swallowed: dropping a real
        # release-noted PR would ship it un-noted, and the label gate won't catch it.
        from github.GithubException import GithubException

        repo = MagicMock()
        repo.get_pull.side_effect = GithubException(500, {"message": "Server Error"}, {})
        with pytest.raises(GithubException):
            hydrate_prs(repo, {7: "sha"})


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
