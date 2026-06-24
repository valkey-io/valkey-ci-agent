"""Tests for the 00-RELEASENOTES format primitives.

These pure helpers (no I/O, no network) were previously exercised only
indirectly through the render / release_cut / pipeline tests. The prior-text
roll-up in :func:`render_release_notes` is load-bearing for idempotency across
rc1 -> rcN -> GA (it carries forward earlier dated sections and merges the
cumulative contributor footer), so it is worth testing directly.
"""

from __future__ import annotations

import pytest

from scripts.release_notes import release_format as rf


class TestParseVersion:
    def test_splits_into_ints(self) -> None:
        assert rf.parse_version("9.1.0") == (9, 1, 0)

    def test_strips_whitespace(self) -> None:
        assert rf.parse_version("  8.0.4 ") == (8, 0, 4)

    def test_accepts_component_boundaries(self) -> None:
        assert rf.parse_version("0.0.0") == (0, 0, 0)
        assert rf.parse_version("255.255.255") == (255, 255, 255)

    @pytest.mark.parametrize("bad", ["9.1", "9.1.0.5", "v9.1.0", "9.1.x", "", "9..0"])
    def test_rejects_malformed(self, bad) -> None:
        with pytest.raises(ValueError):
            rf.parse_version(bad)

    @pytest.mark.parametrize("bad", ["256.0.0", "9.256.0", "9.1.256", "300.1.0"])
    def test_rejects_out_of_byte_range(self, bad) -> None:
        # Each component must fit one byte of VALKEY_VERSION_NUM (0-255).
        with pytest.raises(ValueError):
            rf.parse_version(bad)


class TestOrdinal:
    def test_small_words(self) -> None:
        assert rf.ordinal(1) == "first"
        assert rf.ordinal(2) == "second"
        assert rf.ordinal(0) == "zeroth"

    def test_last_in_table(self) -> None:
        # The table runs through "twelfth" (index 12).
        assert rf.ordinal(12) == "twelfth"

    def test_falls_back_to_nth_beyond_table(self) -> None:
        assert rf.ordinal(13) == "13th"
        assert rf.ordinal(99) == "99th"

    def test_negative_uses_fallback(self) -> None:
        # Out of the 0 <= n < len bound -> the "Nth" fallback, not an index error.
        assert rf.ordinal(-1) == "-1th"


class TestFormatDate:
    def test_iso_reformatted(self) -> None:
        assert rf._format_date("2026-06-02") == "Tue 02 June 2026"

    def test_iso_with_surrounding_whitespace(self) -> None:
        assert rf._format_date("  2026-06-02 ") == "Tue 02 June 2026"

    def test_non_iso_returned_unchanged(self) -> None:
        # A pre-formatted display date passes through untouched.
        assert rf._format_date("Tue 02 June 2026") == "Tue 02 June 2026"

    def test_impossible_date_returned_unchanged(self) -> None:
        # date.fromisoformat rejects it, so it is returned as-is (stripped).
        assert rf._format_date("2026-13-45") == "2026-13-45"


class TestRenderContributorsFooter:
    def test_empty_returns_empty_string(self) -> None:
        assert rf.render_contributors_footer([]) == ""

    def test_sorted_by_display_name_case_insensitive(self) -> None:
        out = rf.render_contributors_footer(["zoe Q @zoe", "Amy P @amy", "bob @bob"])
        assert out == "### Contributors\n* Amy P @amy\n* bob @bob\n* zoe Q @zoe"

    def test_dedup_first_spelling_wins(self) -> None:
        # Same person, two spellings differing only by case: the first is kept.
        out = rf.render_contributors_footer(["Jane Doe @jane", "jane doe @jane"])
        assert out == "### Contributors\n* Jane Doe @jane"

    def test_strips_existing_bullet_markers(self) -> None:
        # Entries carried from a prior footer already start with "* "; not doubled.
        out = rf.render_contributors_footer(["* Amy @amy", "- Bob @bob"])
        assert out == "### Contributors\n* Amy @amy\n* Bob @bob"

    def test_blank_entries_dropped(self) -> None:
        assert rf.render_contributors_footer(["", "  ", "\t"]) == ""


class TestSplitContributorsFooter:
    def test_no_footer_returns_text_and_empty(self) -> None:
        # No ### Contributors header: text is returned unchanged (not stripped).
        text = "Valkey 9.1.0 GA\n\n### Bug Fixes\n* fix (#1)\n"
        body, names = rf._split_contributors_footer(text)
        assert body == text
        assert names == []

    def test_splits_names_until_next_header(self) -> None:
        text = (
            "Valkey 9.1.0 GA\n\n### Bug Fixes\n* fix (#1)\n\n"
            "### Contributors\n* Amy @amy\n* Bob @bob\n"
        )
        body, names = rf._split_contributors_footer(text)
        assert "### Contributors" not in body
        assert names == ["Amy @amy", "Bob @bob"]

    def test_uses_last_header_folding_legacy_per_section_footers(self) -> None:
        # A legacy file with a per-section footer AND a cumulative one: split at
        # the LAST header so the older per-section footer stays in the body region
        # and only the final footer's names are peeled off.
        text = (
            "Valkey 9.1.0-rc1\n\n### Contributors\n* Old @old\n\n"
            "Valkey 9.1.0 GA\n\n### Bug Fixes\n* fix (#1)\n\n"
            "### Contributors\n* Amy @amy\n"
        )
        body, names = rf._split_contributors_footer(text)
        assert names == ["Amy @amy"]
        # The earlier (per-section) footer is still inside the body region.
        assert "* Old @old" in body


class TestRenderVersionSection:
    def test_rejects_bad_urgency(self) -> None:
        with pytest.raises(ValueError):
            rf.render_version_section("9.1.0", "ga", "SOON", "2026-06-02", {})

    def test_ga_heading_and_urgency_sentence(self) -> None:
        out = rf.render_version_section(
            "9.1.0", "ga", "LOW", "2026-06-02", {"Bug Fixes": ["* fix (#1)"]}
        )
        assert "Valkey 9.1.0 GA  -  Released Tue 02 June 2026" in out
        assert "This is the first stable release of Valkey 9.1." in out
        assert "### Bug Fixes\n* fix (#1)" in out

    def test_security_fixes_render_first_from_argument(self) -> None:
        out = rf.render_version_section(
            "9.1.0", "rc1", "SECURITY", "2026-06-02",
            {"Bug Fixes": ["* fix (#1)"]},
            security_fixes=["(CVE-2026-1) a hole"],
        )
        assert out.index("### Security Fixes") < out.index("### Bug Fixes")

    def test_non_canonical_category_rendered_last_not_dropped(self) -> None:
        out = rf.render_version_section(
            "9.1.0", "rc1", "LOW", "2026-06-02",
            {"Bug Fixes": ["* fix (#1)"], "Networking": ["* net (#2)"]},
        )
        assert "### Networking\n* net (#2)" in out
        assert out.index("### Bug Fixes") < out.index("### Networking")


class TestRenderReleaseNotes:
    def test_first_cut_no_prior_text(self) -> None:
        out = rf.render_release_notes(
            {"Bug Fixes": ["* fix (#1)"]},
            version="9.1.0", stage="rc1", urgency="LOW", date="2026-06-02",
            prior_text="", contributors=["Amy @amy"],
        )
        assert out.startswith("Valkey 9.1 release notes")
        assert "Valkey 9.1.0-rc1" in out
        assert out.rstrip().endswith("### Contributors\n* Amy @amy")

    def test_prior_dated_section_carried_below_new_one(self) -> None:
        prior = (
            "Valkey 9.1 release notes\n=======================\n\n"
            "Valkey 9.1.0-rc1  -  Released Mon 01 June 2026\n"
            "---------------------------------------------\n\n"
            "### Bug Fixes\n* earlier fix (#1)\n\n"
            "### Contributors\n* Amy @amy\n"
        )
        out = rf.render_release_notes(
            {"Bug Fixes": ["* new fix (#2)"]},
            version="9.1.0", stage="rc2", urgency="LOW", date="2026-06-08",
            prior_text=prior, contributors=["Bob @bob"],
        )
        # New rc2 section sits above the carried rc1 section.
        assert out.index("Valkey 9.1.0-rc2") < out.index("Valkey 9.1.0-rc1")
        assert "* earlier fix (#1)" in out
        assert "* new fix (#2)" in out

    def test_contributor_footer_rolled_up_and_deduped(self) -> None:
        # Prior footer had Amy; this cut adds Amy again (dup) and Bob. The single
        # cumulative footer lists each once, alpha-sorted, with no stale per-cut
        # footer left in the dated region.
        prior = (
            "Valkey 9.1 release notes\n=======================\n\n"
            "Valkey 9.1.0-rc1  -  Released Mon 01 June 2026\n"
            "---------------------------------------------\n\n"
            "### Bug Fixes\n* earlier fix (#1)\n\n"
            "### Contributors\n* Amy @amy\n"
        )
        out = rf.render_release_notes(
            {"Bug Fixes": ["* new fix (#2)"]},
            version="9.1.0", stage="rc2", urgency="LOW", date="2026-06-08",
            prior_text=prior, contributors=["Amy @amy", "Bob @bob"],
        )
        assert out.count("### Contributors") == 1
        assert out.count("* Amy @amy") == 1
        # Footer is the tail of the file, alpha-sorted.
        assert out.rstrip().endswith("### Contributors\n* Amy @amy\n* Bob @bob")
