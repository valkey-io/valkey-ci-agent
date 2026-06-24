"""Tests for the release contributor list.

The single network seam is ``_api_get`` (a urllib wrapper); patch it to feed
compare/user payloads without a live GitHub. The git-shortlog fallback runs
against a real local repo built in ``tmp_path``.
"""

from __future__ import annotations

import urllib.error

from scripts.common.proc import run_git
from scripts.release_notes import contributors as contrib


def _init_repo(path) -> str:
    repo = str(path)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    return repo


def _commit(repo: str, subject: str, *, name: str, email: str) -> None:
    run_git(repo, "-c", f"user.name={name}", "-c", f"user.email={email}",
            "commit", "-q", "--allow-empty", "-m", subject)


def _compare_url(page: int) -> str:
    return f"compare/base...head?per_page=100&page={page}"


class TestComparePagination:
    def test_walks_all_pages_until_short_page(self, monkeypatch) -> None:
        # Two full pages of 100 then a short third page: every page's authors are
        # collected. per_page=100 makes "short page" a real end signal (a larger
        # value would end after page 1 and drop everyone past commit 100).
        def _page(n):
            if n <= 2:
                return {"total_commits": 250,
                        "commits": [{"author": {"login": f"u{n}_{i}"}} for i in range(100)]}
            return {"total_commits": 250,
                    "commits": [{"author": {"login": f"u3_{i}"}} for i in range(50)]}

        calls = {"n": 0}

        def _fake_api_get(url, token):
            calls["n"] += 1
            return _page(calls["n"])

        monkeypatch.setattr(contrib, "_api_get", _fake_api_get)
        logins = contrib._compare_logins("r", "base", "head", "tok")
        assert calls["n"] == 3               # walked all three pages
        assert len(logins) == 250            # nobody past the first page dropped
        assert "u3_49" in logins             # a third-page author is present

    def test_single_short_page_stops_immediately(self, monkeypatch) -> None:
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 2,
                                "commits": [{"author": {"login": "a"}},
                                            {"author": {"login": "b"}}]},
        )
        assert contrib._compare_logins("r", "base", "head", None) == ["a", "b"]

    def test_bot_and_duplicate_logins_skipped(self, monkeypatch) -> None:
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 3, "commits": [
                {"author": {"login": "a"}},
                {"author": {"login": "a"}},          # duplicate
                {"author": {"login": "dependabot[bot]"}},  # bot
            ]},
        )
        assert contrib._compare_logins("r", "base", "head", None) == ["a"]

    def test_cap_hit_logs_warning(self, monkeypatch, caplog) -> None:
        # total_commits exceeds what the endpoint returns: warn rather than
        # truncate silently, so a very wide GA cut is not quietly short.
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 400,
                                "commits": [{"author": {"login": "a"}}]},
        )
        with caplog.at_level("WARNING"):
            contrib._compare_logins("r", "base", "head", None)
        assert any("compare API returned only" in r.message for r in caplog.records)

    def test_non_list_commits_does_not_raise(self, monkeypatch) -> None:
        # A malformed 200 (commits as a scalar/dict) must not be iterated into an
        # AttributeError that aborts the cut; it yields no logins.
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 1, "commits": 5},
        )
        assert contrib._compare_logins("r", "base", "head", None) == []

    def test_non_dict_commit_entry_skipped(self, monkeypatch) -> None:
        # A junk (non-dict) commit entry alongside a real one: the real login is
        # still collected, the junk entry is skipped rather than crashing.
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 2,
                                "commits": ["junk", {"author": {"login": "a"}}]},
        )
        assert contrib._compare_logins("r", "base", "head", None) == ["a"]

    def test_non_string_login_skipped(self, monkeypatch) -> None:
        # A non-string login (malformed payload) must not raise on .endswith and
        # abort the cut; it is skipped, and a real login alongside is still kept.
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 2, "commits": [
                {"author": {"login": 123}},
                {"author": {"login": "a"}},
            ]},
        )
        assert contrib._compare_logins("r", "base", "head", None) == ["a"]

    def test_page_cap_stops_runaway_pagination(self, monkeypatch) -> None:
        # An endpoint that ignores `page` and returns a full page forever must not
        # loop unbounded; the max-page cap stops it.
        calls = {"n": 0}

        def _always_full(url, token):
            calls["n"] += 1
            return {"total_commits": 9999,
                    "commits": [{"author": {"login": f"u{calls['n']}_{i}"}} for i in range(100)]}

        monkeypatch.setattr(contrib, "_api_get", _always_full)
        contrib._compare_logins("r", "base", "head", None)
        assert calls["n"] <= 5  # bounded by max_pages


class TestListContributors:
    def test_resolves_display_names_and_sorts(self, monkeypatch) -> None:
        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: ["zoe", "amy"])
        names = {"zoe": "Zoe Q", "amy": "Amy P"}
        monkeypatch.setattr(contrib, "_display_name",
                            lambda login, token: names.get(login))
        result = contrib.list_contributors("r", "base", "head", token="t")
        # Sorted by display name: Amy before Zoe.
        assert result == ["Amy P @amy", "Zoe Q @zoe"]

    def test_login_used_when_display_name_missing(self, monkeypatch) -> None:
        monkeypatch.setattr(contrib, "_compare_logins", lambda *a, **k: ["ghost"])
        monkeypatch.setattr(contrib, "_display_name", lambda login, token: None)
        assert contrib.list_contributors("r", "base", "head") == ["ghost @ghost"]

    def test_falls_back_to_shortlog_on_api_failure(self, monkeypatch, tmp_path) -> None:
        # The compare API raises; the list degrades to git-shortlog names (no
        # handles), deduped and alpha-sorted, from the real range.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "feat", name="Bob Dev", email="bob@x")
        _commit(repo, "fix", name="Ann Coder", email="ann@x")

        def _boom(*a, **k):
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(contrib, "_compare_logins", _boom)
        result = contrib.list_contributors("r", "base", "main", repo_dir=repo)
        assert result == ["Ann Coder", "Bob Dev"]

    def test_socket_timeout_degrades_to_fallback(self, monkeypatch, tmp_path) -> None:
        # A socket read timeout is a TimeoutError (OSError), not a URLError; it
        # must still degrade to the fallback rather than abort the cut.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "feat", name="Sam Dev", email="sam@x")

        def _timeout(*a, **k):
            raise TimeoutError("read timed out")

        monkeypatch.setattr(contrib, "_compare_logins", _timeout)
        result = contrib.list_contributors("r", "base", "main", repo_dir=repo)
        assert result == ["Sam Dev"]
