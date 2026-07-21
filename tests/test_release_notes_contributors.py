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


def _commit(repo: str, subject: str, *, name: str, email: str, body: str = "") -> None:
    args = ["-c", f"user.name={name}", "-c", f"user.email={email}",
            "commit", "-q", "--allow-empty", "-m", subject]
    if body:
        args += ["-m", body]
    run_git(repo, *args)


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
        logins, truncated, _git_names = contrib._compare_logins("r", "base", "head", "tok")
        assert calls["n"] == 3               # walked all three pages
        assert len(logins) == 250            # nobody past the first page dropped
        assert "u3_49" in logins             # a third-page author is present
        assert truncated is False            # returned every commit it was told of

    def test_single_short_page_stops_immediately(self, monkeypatch) -> None:
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 2,
                                "commits": [{"author": {"login": "a"}},
                                            {"author": {"login": "b"}}]},
        )
        assert contrib._compare_logins("r", "base", "head", None)[:2] == (["a", "b"], False)

    def test_bot_and_duplicate_logins_skipped(self, monkeypatch) -> None:
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 3, "commits": [
                {"author": {"login": "a"}},
                {"author": {"login": "a"}},          # duplicate
                {"author": {"login": "dependabot[bot]"}},  # bot
            ]},
        )
        assert contrib._compare_logins("r", "base", "head", None)[:2] == (["a"], False)

    def test_login_dedup_is_case_insensitive(self, monkeypatch) -> None:
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 2, "commits": [
                {"author": {"login": "Alice"}},
                {"author": {"login": "alice"}},
            ]},
        )
        assert contrib._compare_logins("r", "base", "head", None)[:2] == (
            ["Alice"], False,
        )

    def test_cap_hit_flags_truncated_and_warns(self, monkeypatch, caplog) -> None:
        # total_commits exceeds what the endpoint returns: warn and flag truncated
        # so the caller supplements from git shortlog rather than shipping short.
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 400,
                                "commits": [{"author": {"login": "a"}}]},
        )
        with caplog.at_level("WARNING"):
            logins, truncated, _git_names = contrib._compare_logins("r", "base", "head", None)
        assert logins == ["a"]
        assert truncated is True
        assert _git_names == []
        assert any("compare API returned only" in r.message for r in caplog.records)

    def test_git_names_extracted_from_inner_commit(self, monkeypatch) -> None:
        # The compare payload carries both a top-level author (GitHub user) and an
        # inner commit.commit.author (git-level name). git_names collects the latter
        # for shortlog dedup.
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 2, "commits": [
                {"author": {"login": "bob"},
                 "commit": {"author": {"name": "Bob Dev"}}},
                {"author": {"login": "ann"},
                 "commit": {"author": {"name": "Ann Coder"}}},
            ]},
        )
        logins, truncated, git_names = contrib._compare_logins("r", "base", "head", None)
        assert logins == ["bob", "ann"]
        assert truncated is False
        assert set(git_names) == {"Bob Dev", "Ann Coder"}

    def test_git_names_deduped_and_bots_excluded(self, monkeypatch) -> None:
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 3, "commits": [
                {"author": {"login": "bob"},
                 "commit": {"author": {"name": "Bob Dev"}}},
                {"author": {"login": "bob2"},
                 "commit": {"author": {"name": "Bob Dev"}}},  # same git name, deduped
                {"author": {"login": "ci"},
                 "commit": {"author": {"name": "github-actions[bot]"}}},  # bot
            ]},
        )
        _logins, _truncated, git_names = contrib._compare_logins("r", "base", "head", None)
        assert git_names == ["Bob Dev"]

    def test_non_list_commits_does_not_raise(self, monkeypatch) -> None:
        # A malformed 200 (commits as a scalar/dict) must not be iterated into an
        # AttributeError that aborts the cut; it yields no logins.
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 1, "commits": 5},
        )
        assert contrib._compare_logins("r", "base", "head", None)[:2] == ([], True)

    def test_non_dict_commit_entry_skipped(self, monkeypatch) -> None:
        # A junk (non-dict) commit entry alongside a real one: the real login is
        # still collected, the junk entry is skipped rather than crashing.
        monkeypatch.setattr(
            contrib, "_api_get",
            lambda url, token: {"total_commits": 2,
                                "commits": ["junk", {"author": {"login": "a"}}]},
        )
        assert contrib._compare_logins("r", "base", "head", None)[:2] == (["a"], False)

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
        assert contrib._compare_logins("r", "base", "head", None)[:2] == (["a"], False)

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
                            lambda *a, **k: (["zoe", "amy"], False, []))
        names = {"zoe": "Zoe Q", "amy": "Amy P"}
        monkeypatch.setattr(contrib, "_display_name",
                            lambda login, token: names.get(login))
        result = contrib.list_contributors("r", "base", "head", token="t")
        # Sorted by display name: Amy before Zoe.
        assert result == ["Amy P @amy", "Zoe Q @zoe"]

    def test_login_used_when_display_name_missing(self, monkeypatch) -> None:
        monkeypatch.setattr(contrib, "_compare_logins", lambda *a, **k: (["ghost"], False, []))
        monkeypatch.setattr(contrib, "_display_name", lambda login, token: None)
        assert contrib.list_contributors("r", "base", "head") == ["ghost @ghost"]

    def test_truncated_compare_supplemented_from_shortlog(self, monkeypatch, tmp_path) -> None:
        # The compare API caps at 250 commits and reports truncated=True with a
        # partial login set. The tail is supplemented from git shortlog: an author
        # the API window missed (Zed) is added name-only, while one already
        # credited with a handle (Bob Dev @bob) is not re-added name-only.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "feat", name="Bob Dev", email="bob@x")
        _commit(repo, "late", name="Zed", email="zed@x")

        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: (["bob"], True, ["Bob Dev"]))
        monkeypatch.setattr(contrib, "_display_name",
                            lambda login, token: {"bob": "Bob Dev"}.get(login))
        result = contrib.list_contributors("r", "base", "main", repo_dir=repo)
        # Bob credited once (with handle, not doubled name-only); Zed added from
        # shortlog; alpha-sorted by display name.
        assert result == ["Bob Dev @bob", "Zed"]

    def test_display_name_differs_from_git_name_not_doubled(self, monkeypatch, tmp_path) -> None:
        # The bug: profile display name ("Bob Developer") differs from git author
        # name ("Bob Dev"). Without git_names dedup, shortlog's "Bob Dev" would not
        # match the API entry "Bob Developer @bob" and the person appears twice.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "feat", name="Bob Dev", email="bob@x")
        _commit(repo, "late", name="Zed", email="zed@x")

        # API sees Bob's login and resolves to "Bob Developer" (profile name).
        # git_names carries "Bob Dev" (the commit-level author name the API saw).
        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: (["bob"], True, ["Bob Dev"]))
        monkeypatch.setattr(contrib, "_display_name",
                            lambda login, token: {"bob": "Bob Developer"}.get(login))
        result = contrib.list_contributors("r", "base", "main", repo_dir=repo)
        # Bob credited once (with handle under display name); shortlog's "Bob Dev"
        # matches git_names and is not re-added. Zed (not in API window) is added.
        assert result == ["Bob Developer @bob", "Zed"]

    def test_untruncated_compare_not_supplemented_from_shortlog(self, monkeypatch, tmp_path) -> None:
        # When the compare API returned every commit (truncated=False), the
        # shortlog supplement is NOT run, so a shortlog-only author is not added.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "late", name="Zed", email="zed@x")

        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: (["bob"], False, ["Bob Dev"]))
        monkeypatch.setattr(contrib, "_display_name",
                            lambda login, token: {"bob": "Bob Dev"}.get(login))
        result = contrib.list_contributors("r", "base", "main", repo_dir=repo)
        assert result == ["Bob Dev @bob"]

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

    def test_shortlog_fallback_skips_bot_author(self, monkeypatch, tmp_path) -> None:
        # The offline, backport-heavy case: the only commit author is the bot.
        # It must not be credited (matching the compare-API path), so the list is
        # empty rather than listing the bot.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "sweep", name="valkey-ci-agent[bot]",
                email="bot@users.noreply.github.com")

        def _boom(*a, **k):
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(contrib, "_compare_logins", _boom)
        assert contrib.list_contributors("r", "base", "main", repo_dir=repo) == []


class TestCoauthorsInRange:
    def test_reads_trailers_deduped_first_seen(self, tmp_path) -> None:
        # Two commits, three distinct co-authors (one repeated): each name once,
        # in first-seen order.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "sweep 1", name="bot", email="bot@x",
                body="Co-authored-by: Binbin <bb@qq.com>\n"
                     "Co-authored-by: Ran S <ran@amazon.com>")
        _commit(repo, "sweep 2", name="bot", email="bot@x",
                body="Co-authored-by: Ran S <ran@amazon.com>\n"  # dup across commits
                     "Co-authored-by: Viktor <vik@x.io>")
        names = contrib._coauthors_in_range("base", "main", repo)
        assert names == ["Binbin", "Ran S", "Viktor"]

    def test_case_insensitive_dedup(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "c", name="bot", email="bot@x",
                body="Co-authored-by: Alex Kim <a@x>\n"
                     "Co-authored-by: alex kim <a2@x>")  # same person, different case
        assert contrib._coauthors_in_range("base", "main", repo) == ["Alex Kim"]

    def test_no_trailers_is_empty(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "plain", name="Dev", email="dev@x")
        assert contrib._coauthors_in_range("base", "main", repo) == []

    def test_bad_range_degrades_to_empty(self, tmp_path) -> None:
        # git log over a non-existent ref fails; the helper returns [] rather than
        # raising, matching the shortlog fallback's stance.
        repo = _init_repo(tmp_path)
        _commit(repo, "only", name="Dev", email="dev@x")
        assert contrib._coauthors_in_range("nope", "alsonope", repo) == []

    def test_bot_coauthor_skipped(self, tmp_path) -> None:
        # A tool listing itself as a co-author is not credited; humans alongside
        # it still are.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "sweep", name="bot", email="bot@x",
                body="Co-authored-by: github-actions[bot] <ga@users.noreply.github.com>\n"
                     "Co-authored-by: Real Human <rh@x>")
        assert contrib._coauthors_in_range("base", "main", repo) == ["Real Human"]

    def test_prose_coauthored_by_line_is_not_credited(self, tmp_path) -> None:
        # Regression: a Co-authored-by:-shaped line quoted in the body prose (with
        # more text after it, so it is NOT the terminal trailer block) must not be
        # read as a trailer and publish a phantom credit. Only the real terminal
        # trailer is credited. A plain body-wide regex would wrongly credit both.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(
            repo, "sweep", name="bot", email="bot@x",
            body=(
                "Discussion: we tried\n"
                "Co-authored-by: Prose Ghost <ghost@x>\n"  # buried in prose, not a trailer
                "but reverted it.\n\n"
                "Co-authored-by: Real Human <rh@x>"  # the real terminal trailer
            ),
        )
        assert contrib._coauthors_in_range("base", "main", repo) == ["Real Human"]

    def test_transitive_email_aliases_prefer_display_name(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(
            repo,
            "sweep",
            name="valkeyrie-ops[bot]",
            email="bot@x",
            body=(
                "Co-authored-by: jjuleslasarte <jules.lasarte@gmail.com>\n"
                "Co-authored-by: Jules Lasarte <lasartej@amazon.com>"
            ),
        )

        assert contrib._coauthors_in_range(
            "base", "main", repo
        ) == ["Jules Lasarte"]


class TestIsBot:
    def test_bot_login_suffix(self) -> None:
        assert contrib._is_bot("dependabot[bot]") is True
        assert contrib._is_bot("valkey-ci-agent[bot]") is True

    def test_case_and_whitespace_tolerant(self) -> None:
        assert contrib._is_bot("  GitHub-Actions[BOT] ") is True

    def test_human_not_matched(self) -> None:
        assert contrib._is_bot("Robert Botsworth") is False
        assert contrib._is_bot("botanist") is False  # 'bot' as a substring, no [bot]
        assert contrib._is_bot("Amy P") is False


class TestCoauthorUnion:
    def test_coauthors_unioned_into_api_path(self, monkeypatch, tmp_path) -> None:
        # The compare API credits one author; the sweep's co-authors (in commit
        # bodies) are added name-only, and the whole list is alpha-sorted.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "sweep", name="bot", email="bot@x",
                body="Co-authored-by: Zed Author <z@x>\n"
                     "Co-authored-by: Ann Coder <ann@x>")

        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: (["mid"], False, []))
        monkeypatch.setattr(contrib, "_display_name",
                            lambda login, token: "Mid Dev")
        result = contrib.list_contributors("r", "base", "main", token="t", repo_dir=repo)
        # API author keeps its @handle; co-authors are name-only; all sorted.
        assert result == ["Ann Coder", "Mid Dev @mid", "Zed Author"]

    def test_coauthor_already_credited_by_handle_not_duplicated(self, monkeypatch, tmp_path) -> None:
        # A co-author whose display name matches an API-credited author is not
        # re-added: the "Name @handle" entry already covers that person.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "sweep", name="bot", email="bot@x",
                body="Co-authored-by: Amy P <amy@x>")

        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: (["amy"], False, []))
        monkeypatch.setattr(contrib, "_display_name", lambda login, token: "Amy P")
        result = contrib.list_contributors("r", "base", "main", token="t", repo_dir=repo)
        assert result == ["Amy P @amy"]  # not also a bare "Amy P"

    def test_coauthors_unioned_into_shortlog_fallback(self, monkeypatch, tmp_path) -> None:
        # API fails -> shortlog names; co-authors from bodies are unioned on top.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "feat", name="Bob Dev", email="bob@x",
                body="Co-authored-by: Cara Fixer <cara@x>")

        def _boom(*a, **k):
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(contrib, "_compare_logins", _boom)
        result = contrib.list_contributors("r", "base", "main", repo_dir=repo)
        # Bob (shortlog author) and Cara (co-author trailer), both name-only, sorted.
        assert result == ["Bob Dev", "Cara Fixer"]

    def test_email_aliases_collapse_login_and_display_name(self, monkeypatch, tmp_path) -> None:
        # A sweep can carry two trailers for one person under a login-shaped name
        # and a display name. The first trailer links the known GitHub login to an
        # email local-part; the second name matches that local-part, so it must not
        # create an obvious duplicate contributor.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(
            repo,
            "sweep",
            name="valkeyrie-ops[bot]",
            email="bot@x",
            body=(
                "Co-authored-by: jjuleslasarte <jules.lasarte@gmail.com>\n"
                "Co-authored-by: Jules Lasarte <lasartej@amazon.com>"
            ),
        )

        monkeypatch.setattr(
            contrib,
            "_compare_logins",
            lambda *a, **k: (["jjuleslasarte"], False, []),
        )
        monkeypatch.setattr(contrib, "_display_name", lambda login, token: None)

        assert contrib.list_contributors(
            "r", "base", "main", repo_dir=repo
        ) == ["jjuleslasarte @jjuleslasarte"]

    def test_email_aliases_use_display_name_without_api_identity(
        self, monkeypatch, tmp_path
    ) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(
            repo,
            "sweep",
            name="valkeyrie-ops[bot]",
            email="bot@x",
            body=(
                "Co-authored-by: jjuleslasarte <jules.lasarte@gmail.com>\n"
                "Co-authored-by: Jules Lasarte <lasartej@amazon.com>"
            ),
        )
        monkeypatch.setattr(
            contrib,
            "_compare_logins",
            lambda *a, **k: ([], False, []),
        )

        assert contrib.list_contributors(
            "r", "base", "main", repo_dir=repo
        ) == ["Jules Lasarte"]


class TestPRLogins:
    """PR-resolved logins give squash-merged contributors proper @handles."""

    def test_pr_logins_produce_handle_entries(self, monkeypatch, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "sweep", name="valkeyrie-ops[bot]", email="bot@x",
                body="Co-authored-by: devnick <devnick@example.com>")

        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: ([], False, []))
        monkeypatch.setattr(contrib, "_display_name", lambda login, token: None)

        result = contrib.list_contributors(
            "r", "base", "main", token="t", repo_dir=repo,
            pr_logins=["devnick"],
        )
        assert result == ["devnick @devnick"]

    def test_pr_logins_with_display_name(self, monkeypatch, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "sweep", name="valkeyrie-ops[bot]", email="bot@x",
                body="Co-authored-by: Pat Rivera <privera@example.com>")

        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: ([], False, []))
        monkeypatch.setattr(contrib, "_display_name",
                            lambda login, token: "Pat Rivera" if login == "privera" else None)

        result = contrib.list_contributors(
            "r", "base", "main", token="t", repo_dir=repo,
            pr_logins=["privera"],
        )
        assert result == ["Pat Rivera @privera"]

    def test_pr_logins_dedup_against_compare_api(self, monkeypatch, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "feat", name="Amy", email="amy@x")

        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: (["amy"], False, []))
        monkeypatch.setattr(contrib, "_display_name", lambda login, token: "Amy P")

        result = contrib.list_contributors(
            "r", "base", "main", token="t", repo_dir=repo,
            pr_logins=["amy"],
        )
        assert result == ["Amy P @amy"]

    def test_pr_logins_suppress_bare_coauthor_name(self, monkeypatch, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "sweep", name="valkeyrie-ops[bot]", email="bot@x",
                body="Co-authored-by: devnick <devnick@example.com>")

        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: ([], False, []))
        monkeypatch.setattr(contrib, "_display_name", lambda login, token: None)

        result = contrib.list_contributors(
            "r", "base", "main", token="t", repo_dir=repo,
            pr_logins=["devnick"],
        )
        assert result == ["devnick @devnick"]

    def test_pr_logins_bot_filtered(self, monkeypatch, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "feat", name="human", email="h@x")

        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: ([], False, []))
        monkeypatch.setattr(contrib, "_display_name", lambda login, token: None)

        result = contrib.list_contributors(
            "r", "base", "main", token="t", repo_dir=repo,
            pr_logins=["valkeyrie-ops[bot]", "human"],
        )
        assert result == ["human @human"]

    def test_pr_logins_mixed_with_compare_api(self, monkeypatch, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "direct", name="Alice", email="alice@x")
        _commit(repo, "sweep", name="bot", email="bot@x",
                body="Co-authored-by: devnick <devnick@example.com>\n"
                     "Co-authored-by: Unknown Person <unknown@x>")

        monkeypatch.setattr(contrib, "_compare_logins",
                            lambda *a, **k: (["alice123"], False, ["Alice"]))

        def _mock_display(login, token):
            return {"alice123": "Alice", "devnick": None}.get(login)

        monkeypatch.setattr(contrib, "_display_name", _mock_display)

        result = contrib.list_contributors(
            "r", "base", "main", token="t", repo_dir=repo,
            pr_logins=["devnick"],
        )
        assert result == ["Alice @alice123", "devnick @devnick", "Unknown Person"]

    def test_pr_logins_upgrade_shortlog_by_display_name(self, monkeypatch, tmp_path) -> None:
        # Login aliases don't overlap with the shortlog entry, but the resolved
        # display name does. The bare entry should be upgraded, not duplicated.
        repo = _init_repo(tmp_path)
        _commit(repo, "base", name="Root", email="root@x")
        run_git(repo, "tag", "base")
        _commit(repo, "feat", name="Alice", email="alice@x")

        def _boom(*a, **k):
            raise urllib.error.URLError("offline")

        monkeypatch.setattr(contrib, "_compare_logins", _boom)
        monkeypatch.setattr(contrib, "_display_name",
                            lambda login, token: "Alice" if login == "alice123" else None)

        result = contrib.list_contributors(
            "r", "base", "main", token="t", repo_dir=repo,
            pr_logins=["alice123"],
        )
        assert result == ["Alice @alice123"]
