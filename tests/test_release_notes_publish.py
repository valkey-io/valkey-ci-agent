"""Tests for the PR-side publish primitives (mocked GitHub)."""

from __future__ import annotations

from unittest.mock import MagicMock

from scripts.release_notes.publish import escape_cell, find_existing_pr, open_or_update_pr


class TestFindExistingPr:
    def test_returns_first_open_pr(self) -> None:
        repo = MagicMock()
        existing = MagicMock(number=5)
        repo.get_pulls.return_value = [existing]
        found = find_existing_pr(repo, base_repo="valkey-io/valkey", push_repo=None,
                                 branch="agent/release-cut/9.1.0-rc1")
        assert found is existing

    def test_returns_none_when_no_open_pr(self) -> None:
        repo = MagicMock()
        repo.get_pulls.return_value = []
        assert find_existing_pr(repo, base_repo="valkey-io/valkey", push_repo=None,
                                branch="agent/release-cut/9.1.0-rc1") is None

    def test_queries_open_state_and_owner_qualified_head(self) -> None:
        # The search must be scoped to open PRs whose head is the OWNER-qualified
        # branch ("owner:branch"). A bare branch head would match nothing, and
        # state != "open" would resurface a stale closed/merged cut as reusable.
        # Assert the exact get_pulls kwargs so a wrong filter can't pass silently.
        repo = MagicMock()
        repo.get_pulls.return_value = []
        find_existing_pr(repo, base_repo="valkey-io/valkey", push_repo=None,
                         branch="agent/release-cut/9.1.0-rc1")
        repo.get_pulls.assert_called_once_with(
            state="open", head="valkey-io:agent/release-cut/9.1.0-rc1"
        )

    def test_head_uses_push_repo_owner_for_cross_repo(self) -> None:
        # When the prep branch lives on a fork, the head filter must carry the
        # fork's owner, not the base repo's, or the cross-repo PR is never found.
        repo = MagicMock()
        repo.get_pulls.return_value = []
        find_existing_pr(repo, base_repo="valkey-io/valkey", push_repo="fork/valkey",
                         branch="agent/release-cut/9.1.0-rc1")
        repo.get_pulls.assert_called_once_with(
            state="open", head="fork:agent/release-cut/9.1.0-rc1"
        )


class TestOpenOrUpdatePr:
    def test_updates_existing(self) -> None:
        repo = MagicMock()
        existing = MagicMock(number=5, html_url="https://x/5")
        url = open_or_update_pr(repo, base_repo="o/r", push_repo=None,
                                branch="agent/release-cut/9.1.0-rc1", base_branch="pre-release-9.1.0",
                                title="t", body="b", existing=existing)
        assert url == "https://x/5"
        existing.edit.assert_called_once()
        repo.create_pull.assert_not_called()

    def test_creates_when_absent(self) -> None:
        repo = MagicMock()
        repo.create_pull.return_value = MagicMock(number=9, html_url="https://x/9")
        url = open_or_update_pr(repo, base_repo="o/r", push_repo=None,
                                branch="agent/release-cut/9.1.0-rc1", base_branch="pre-release-9.1.0",
                                title="t", body="b", existing=None)
        assert url == "https://x/9"
        repo.create_pull.assert_called_once()


class TestEscapeCell:
    def test_escapes_pipe_and_newline(self) -> None:
        assert escape_cell("a | b\nc") == "a \\| b c"

    def test_plain_text_unchanged(self) -> None:
        assert escape_cell("normal title") == "normal title"

    def test_collapses_cr_and_crlf(self) -> None:
        # A raw \r (CRLF or lone CR) in a contributor PR title must not survive
        # into the cell, or it breaks the Markdown table row. Any run of CR/LF
        # collapses to one space.
        assert escape_cell("fix\r\nbug") == "fix bug"      # CRLF
        assert escape_cell("fix\rbug") == "fix bug"        # lone CR
        assert escape_cell("a\r\n\r\nb") == "a b"          # consecutive breaks
        assert "\r" not in escape_cell("fix\r\nbug")

    def test_backslash_before_pipe_stays_escaped(self) -> None:
        # A pre-existing backslash right before a pipe must not consume the pipe's
        # escape. "a\|b" -> "a\\\|b" renders as literal "a\|b" (no cell break),
        # not "a\\|b" (literal backslash + a live delimiter that splits the row).
        assert escape_cell("a\\|b") == "a\\\\\\|b"

    def test_lone_backslash_escaped(self) -> None:
        assert escape_cell("C:\\path") == "C:\\\\path"
