"""Unit tests for recovering the original PR of a backported commit.

These parsers let release-note discovery credit the PR that introduced a change
rather than the backport PR that carried it onto the line. The module is
vendored inside release_notes (not shared with scripts.backport) so the backport
system stays untouched; these tests pin the behavior discovery relies on.
"""

from __future__ import annotations

from scripts.release_notes.backport_refs import (
    applied_source_prs_from_body,
    cherry_pick_source_shas,
    is_backport_title,
    source_pr_from_branch,
    source_title_from_backport_title,
    summary_source_pr_from_body,
    summary_source_title_from_body,
)


class TestIsBackportTitle:
    def test_matches_built_title(self) -> None:
        assert is_backport_title("[Backport 8.1] Fix leak") is True

    def test_case_and_whitespace_tolerant(self) -> None:
        assert is_backport_title("  [backport 9.0] port fix") is True

    def test_not_a_backport(self) -> None:
        assert is_backport_title("Fix memory leak (#10)") is False

    def test_inner_backport_word_not_matched(self) -> None:
        # Only a leading [Backport ...] tag counts, not the word mid-title.
        assert is_backport_title("Refactor the backport sweep (#12)") is False


class TestCherryPickSourceShas:
    def test_single_trailer(self) -> None:
        body = "port fix\n\n(cherry picked from commit abcdef1234567890)"
        assert cherry_pick_source_shas(body) == ["abcdef1234567890"]

    def test_multiple_hops_in_file_order(self) -> None:
        body = (
            "port fix\n\n"
            "(cherry picked from commit 1111111111111111)\n"
            "(cherry picked from commit 2222222222222222)\n"
        )
        # git cherry-pick -x appends, so file order is oldest hop first, most
        # recent hop last: 1111... was inherited from the earlier hop, 2222...
        # added by the latest.
        assert cherry_pick_source_shas(body) == [
            "1111111111111111", "2222222222222222",
        ]

    def test_case_insensitive(self) -> None:
        body = "(Cherry Picked From Commit ABCDEF1234567890)"
        assert cherry_pick_source_shas(body) == ["ABCDEF1234567890"]

    def test_no_trailer(self) -> None:
        assert cherry_pick_source_shas("plain body, no trailer") == []

    def test_ignores_prose_mention(self) -> None:
        # Only a line that is exactly the trailer counts, not prose that happens
        # to mention a cherry-pick.
        assert cherry_pick_source_shas("this was a (cherry picked from commit) note") == []


class TestAppliedSourcePrsFromBody:
    def test_reads_source_pr_column(self) -> None:
        body = (
            "## Applied\n\n"
            "| Source PR | Title | Detail |\n"
            "|---|---|---|\n"
            "| #10 | feat a | clean |\n"
            "| [#11](http://x/11) | fix b | clean |\n"
        )
        assert applied_source_prs_from_body(body) == {10, 11}

    def test_ignores_needs_attention_section(self) -> None:
        body = (
            "## Applied\n\n"
            "| Source PR | Title |\n|---|---|\n| #10 | feat |\n\n"
            "## Needs attention\n\n"
            "| Source PR | Title |\n|---|---|\n| #99 | conflict |\n"
        )
        assert applied_source_prs_from_body(body) == {10}

    def test_ignores_pr_ref_in_other_columns(self) -> None:
        body = (
            "## Applied\n\n"
            "| Source PR | Title |\n|---|---|\n"
            "| #10 | Revert \"X (#3)\" |\n"
        )
        assert applied_source_prs_from_body(body) == {10}

    def test_no_applied_section(self) -> None:
        assert applied_source_prs_from_body("just a normal PR body (#5)") == set()

    def test_first_column_when_no_header(self) -> None:
        body = "## Applied\n\n| #10 | feat |\n"
        assert applied_source_prs_from_body(body) == {10}

    def test_wrapped_cell_reassembled(self) -> None:
        # A row whose cell wraps onto a continuation line is folded back before
        # the columns are split, so the Source PR is still read.
        body = (
            "## Applied\n\n"
            "| Source PR | Title |\n|---|---|\n"
            "| #10 | a very long title that\nwraps across lines |\n"
        )
        assert applied_source_prs_from_body(body) == {10}


class TestSummarySourcePrFromBody:
    # The per-PR backport "## Backport Summary" table is transposed vs the sweep's
    # "## Applied": Source PR is a row *label* (col 0), the #N is the value (col 1).
    def test_reads_source_pr_row_linked(self) -> None:
        # The exact shape scripts.backport.pr_creator.build_pr_body emits.
        body = (
            "## Backport Summary\n\n"
            "Cherry-pick applied cleanly with no conflicts.\n\n"
            "| Field | Value |\n"
            "|---|---|\n"
            "| Source PR | [#123](https://github.com/valkey-io/valkey/pull/123) |\n"
            "| Source title | Fix a leak |\n"
            "| Target branch | `9.1` |\n"
            "| Cherry-picked commits | 1 |\n"
        )
        assert summary_source_pr_from_body(body) == 123

    def test_reads_bare_source_pr_row(self) -> None:
        body = (
            "## Backport Summary\n\n"
            "| Field | Value |\n|---|---|\n"
            "| Source PR | #77 |\n"
        )
        assert summary_source_pr_from_body(body) == 77

    def test_ignores_pr_ref_in_other_rows(self) -> None:
        # A "#N" in the Source title / other rows is never read as the source.
        body = (
            "## Backport Summary\n\n"
            "| Field | Value |\n|---|---|\n"
            "| Source PR | #77 |\n"
            "| Source title | Revert \"X (#3)\" |\n"
        )
        assert summary_source_pr_from_body(body) == 77

    def test_applied_table_is_not_a_summary(self) -> None:
        # A sweep body (## Applied, no ## Backport Summary) yields None here; that
        # path is handled by applied_source_prs_from_body instead.
        body = "## Applied\n\n| Source PR | Title |\n|---|---|\n| #10 | feat |\n"
        assert summary_source_pr_from_body(body) is None

    def test_no_summary_section(self) -> None:
        assert summary_source_pr_from_body("a normal PR body (#5)") is None
        assert summary_source_pr_from_body("") is None


class TestSourcePrFromBranch:
    def test_plain_backport_branch(self) -> None:
        assert source_pr_from_branch("backport/123-to-8.0") == 123

    def test_agent_namespaced_branch(self) -> None:
        assert source_pr_from_branch("agent/backport/456-to-9.1") == 456

    def test_sweep_branch_has_no_single_source(self) -> None:
        assert source_pr_from_branch("agent/backport/sweep-9.1-abc123") is None

    def test_unrelated_branch(self) -> None:
        assert source_pr_from_branch("feature/my-change") is None
        assert source_pr_from_branch("") is None


class TestSourceTitleFromBackportTitle:
    # build_pr_title embeds the source title verbatim after "[Backport <branch>] ".
    def test_extracts_embedded_source_title(self) -> None:
        assert source_title_from_backport_title(
            "[Backport 9.1] Fix a memory leak"
        ) == "Fix a memory leak"

    def test_case_and_whitespace_tolerant(self) -> None:
        # Same leniency as is_backport_title's anchor; extra spaces are trimmed.
        assert source_title_from_backport_title(
            "  [backport 9.0]   Port the fix  "
        ) == "Port the fix"

    def test_not_a_backport_title(self) -> None:
        assert source_title_from_backport_title("Fix a memory leak (#10)") is None

    def test_bare_prefix_no_title(self) -> None:
        # A "[Backport 9.1]" with nothing after it has no source title to give.
        assert source_title_from_backport_title("[Backport 9.1]") is None
        assert source_title_from_backport_title("[Backport 9.1] ") is None

    def test_empty(self) -> None:
        assert source_title_from_backport_title("") is None


class TestSummarySourceTitleFromBody:
    def test_reads_source_title_row(self) -> None:
        body = (
            "## Backport Summary\n\n"
            "| Field | Value |\n|---|---|\n"
            "| Source PR | [#123](https://x/123) |\n"
            "| Source title | Fix a memory leak in cluster failover |\n"
            "| Target branch | `9.1` |\n"
        )
        assert summary_source_title_from_body(body) == "Fix a memory leak in cluster failover"

    def test_no_source_title_row(self) -> None:
        # A summary that carries only the Source PR row (an older/drifted format).
        body = (
            "## Backport Summary\n\n"
            "| Field | Value |\n|---|---|\n"
            "| Source PR | #77 |\n"
        )
        assert summary_source_title_from_body(body) is None

    def test_title_with_escaped_pipe_is_unescaped(self) -> None:
        # The emitter escapes | as \| inside cells. The parser must unescape so
        # the title cross-check in discover.py sees the real title, not a truncation.
        body = (
            "## Backport Summary\n\n"
            "| Field | Value |\n|---|---|\n"
            "| Source PR | #100 |\n"
            "| Source title | Fix a\\|b parsing in config |\n"
        )
        assert summary_source_title_from_body(body) == "Fix a|b parsing in config"

    def test_title_with_multiple_escaped_pipes(self) -> None:
        body = (
            "## Backport Summary\n\n"
            "| Field | Value |\n|---|---|\n"
            "| Source title | a \\| b \\| c |\n"
        )
        assert summary_source_title_from_body(body) == "a | b | c"

    def test_no_summary_section(self) -> None:
        assert summary_source_title_from_body("a normal PR body (#5)") is None
        assert summary_source_title_from_body("") is None
