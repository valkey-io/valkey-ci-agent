"""Tests for the 00-RELEASENOTES format primitives.

These pure helpers (no I/O, no network) were previously exercised only
indirectly through the render / release_cut / pipeline tests. The prior-text
roll-up in :func:`render_release_notes` is load-bearing for idempotency across
rc1 -> rcN -> GA (it carries forward earlier dated sections and merges the
cumulative contributor footer), so it is worth testing directly.
"""

from __future__ import annotations

from functools import partial

import pytest

from scripts.release_notes import projects
from scripts.release_notes import release_format as rf

_CORE = projects.VALKEY_PROFILE
_SEARCH = projects.profile_for("valkey-search")
render_version_section = partial(
    rf.render_version_section,
    display_name=_CORE.display_name,
    categories=_CORE.categories,
)
render_release_notes = partial(
    rf.render_release_notes,
    display_name=_CORE.display_name,
    categories=_CORE.categories,
)


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

    def test_bare_handles_sort_by_login(self) -> None:
        out = rf.render_contributors_footer(["@zoe", "Amy @amy", "@bob"])
        assert out == "### Contributors\n* Amy @amy\n* @bob\n* @zoe"

    def test_dedup_latest_handled_identity_wins(self) -> None:
        # Later entries come from the current compare API after the prior footer,
        # so their profile spelling replaces stale carried-forward text.
        out = rf.render_contributors_footer(["Jane Doe @jane", "jane doe @jane"])
        assert out == "### Contributors\n* jane doe @jane"

    def test_strips_existing_bullet_markers(self) -> None:
        # Entries carried from a prior footer already start with "* "; not doubled.
        out = rf.render_contributors_footer(["* Amy @amy", "- Bob @bob"])
        assert out == "### Contributors\n* Amy @amy\n* Bob @bob"

    def test_blank_entries_dropped(self) -> None:
        assert rf.render_contributors_footer(["", "  ", "\t"]) == ""

    def test_same_handle_with_changed_display_name_is_one_identity(self) -> None:
        out = rf.render_contributors_footer([
            "Old Profile Name @same-login",
            "Current Profile Name @same-login",
        ])
        assert out == "### Contributors\n* Current Profile Name @same-login"

    def test_same_display_name_with_old_and_new_handles_is_one_identity(self) -> None:
        out = rf.render_contributors_footer([
            "Quanye Yang @Ada-Church-Closure",
            "Quanye Yang @quanyeyang",
        ])
        assert out == "### Contributors\n* Quanye Yang @quanyeyang"

    def test_handled_entry_replaces_name_only_fallback(self) -> None:
        out = rf.render_contributors_footer([
            "Amy P",
            "Amy P @amy",
        ])
        assert out == "### Contributors\n* Amy P @amy"

    def test_bare_handle_merges_with_name_only_fallback(self) -> None:
        out = rf.render_contributors_footer(
            ["@KarthikSubbarao", "KarthikSubbarao", "Karthik Subbarao"]
        )
        assert out == "### Contributors\n* @KarthikSubbarao"


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
            render_version_section("9.1.0", "ga", "SOON", "2026-06-02", {})

    def test_ga_heading_and_urgency_sentence(self) -> None:
        out = render_version_section(
            "9.1.0", "ga", "LOW", "2026-06-02", {"Bug Fixes": ["* fix (#1)"]}
        )
        assert "Valkey 9.1.0 GA  -  Released Tue 02 June 2026" in out
        assert "This is the first stable release of Valkey 9.1." in out
        assert "### Bug Fixes\n* fix (#1)" in out

    def test_patch_heading_omits_ga_suffix(self) -> None:
        out = render_version_section(
            "9.1.1", "ga", "LOW", "2026-06-02", {"Bug Fixes": ["fix"]}
        )
        assert "Valkey 9.1.1  -  Released Tue 02 June 2026" in out
        assert "Valkey 9.1.1 GA" not in out

    def test_security_uses_canonical_rationale_not_release_ordinal(self) -> None:
        out = render_version_section(
            "9.1.1", "ga", "SECURITY", "2026-06-02", {"Bug Fixes": ["fix"]}
        )
        assert (
            "Upgrade urgency SECURITY: This release includes security fixes we "
            "recommend you apply as soon as possible."
        ) in out
        assert "second stable release" not in out

    def test_nonsecurity_patch_uses_standard_urgency_rationale(self) -> None:
        out = render_version_section(
            "9.1.2", "ga", "MODERATE", "2026-06-02", {"Bug Fixes": ["fix"]}
        )
        assert "Program an upgrade of the server, but it's not urgent." in out
        assert "stable release" not in out

    def test_security_fixes_render_first_from_argument(self) -> None:
        out = render_version_section(
            "9.1.0", "rc1", "SECURITY", "2026-06-02",
            {"Bug Fixes": ["* fix (#1)"]},
            security_fixes=["(CVE-2026-1) a hole"],
        )
        assert out.index("### Security Fixes") < out.index("### Bug Fixes")

    def test_non_canonical_category_rendered_last_not_dropped(self) -> None:
        out = render_version_section(
            "9.1.0", "rc1", "LOW", "2026-06-02",
            {"Bug Fixes": ["* fix (#1)"], "Networking": ["* net (#2)"]},
        )
        assert "### Networking\n* net (#2)" in out
        assert out.index("### Bug Fixes") < out.index("### Networking")


class TestRenderReleaseNotes:
    def test_first_cut_no_prior_text(self) -> None:
        out = render_release_notes(
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
        out = render_release_notes(
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
        out = render_release_notes(
            {"Bug Fixes": ["* new fix (#2)"]},
            version="9.1.0", stage="rc2", urgency="LOW", date="2026-06-08",
            prior_text=prior, contributors=["Amy @amy", "Bob @bob"],
        )
        assert out.count("### Contributors") == 1
        assert out.count("* Amy @amy") == 1
        # Footer is the tail of the file, alpha-sorted.
        assert out.rstrip().endswith("### Contributors\n* Amy @amy\n* Bob @bob")

    def test_canonical_patch_footer_remains_single_and_preserves_preface(self) -> None:
        prior = (
            "Valkey 8.1 release notes\n========================\n\n"
            "Valkey 8.1.9  -  Released Tue 21 July 2026\n"
            "------------------------------------------\n\n"
            "### Bug Fixes\n* earlier fix (#1)\n\n"
            "We appreciate the efforts of all who contributed code to this release!\n\n"
            "### Contributors\n* Amy @amy\n* Bob @bob\n"
        )
        out = render_release_notes(
            {"Bug Fixes": ["* new fix (#2)"]},
            version="8.1.10", stage="ga", urgency="LOW", date="2026-08-01",
            prior_text=prior, contributors=["Amy @amy", "Cara @cara"],
        )
        assert out.count("### Contributors") == 1
        assert out.count("* Amy @amy") == 1
        assert "We appreciate the efforts" in out
        assert out.rstrip().endswith(
            "### Contributors\n* Amy @amy\n* Bob @bob\n* Cara @cara"
        )


class TestModuleRepoRendering:
    """Display-name-parameterized rendering for the module repos."""

    def test_module_heading_and_urgency_sentence(self) -> None:
        out = render_version_section(
            "1.2.0", "ga", "LOW", "2026-06-02", {"Bug Fixes": ["* fix (#1)"]},
            display_name="Valkey Search",
            categories=_SEARCH.categories,
        )
        assert "Valkey Search 1.2.0 GA  -  Released Tue 02 June 2026" in out
        assert "This is the first stable release of Valkey Search 1.2." in out

    def test_module_rc_heading(self) -> None:
        out = render_version_section(
            "1.2.0", "rc1", "LOW", "2026-06-02", {"Bug Fixes": ["* fix (#1)"]},
            display_name="Valkey Search",
            categories=_SEARCH.categories,
        )
        assert "Valkey Search 1.2.0-rc1  -  Released Tue 02 June 2026" in out
        assert "This is the first release candidate of Valkey Search 1.2.0." in out

    def test_profile_categories_control_section_order(self) -> None:
        out = render_version_section(
            "1.2.0", "ga", "LOW", "2026-06-02",
            {"Bug Fixes": ["* fix (#1)"], "Behavior Changes": ["* change (#2)"]},
            display_name="Valkey Search",
            categories=("Behavior Changes", "Bug Fixes", "Other Changes"),
        )
        assert out.index("### Behavior Changes") < out.index("### Bug Fixes")

    def test_prior_module_history_preserved(self) -> None:
        # Regression: with the fixed "Valkey M.m.p" splitter, a module heading
        # like "Valkey Search 1.2.1" never matched and the ENTIRE prior
        # changelog was silently dropped from the render.
        prior = (
            "Valkey Search 1.2 release notes\n"
            "===============================\n\n"
            "Valkey Search 1.2.1 GA - Released Tue 07 July 2026\n"
            "--------------------------------------------------\n\n"
            "* Fixed a race condition (#1079)\n"
        )
        out = render_release_notes(
            {"Bug Fixes": ["* new fix (#1300)"]},
            version="1.2.2", stage="ga", urgency="LOW", date="2026-08-06",
            prior_text=prior, contributors=[],
            display_name="Valkey Search",
            categories=_SEARCH.categories,
        )
        assert "Valkey Search 1.2.1 GA - Released Tue 07 July 2026" in out
        assert "* Fixed a race condition (#1079)" in out
        assert out.index("Valkey Search 1.2.2") < out.index("Valkey Search 1.2.1 GA")

    def test_core_display_name_drops_foreign_headings(self) -> None:
        # Explicit core profile rendering ignores prior text without a
        # "Valkey M.m.p" heading.
        out = render_release_notes(
            {"Bug Fixes": ["* fix (#1)"]},
            version="9.1.1", stage="ga", urgency="LOW", date="2026-08-06",
            prior_text="Some unrelated placeholder text\n", contributors=[],
        )
        assert "placeholder" not in out


class TestTrailingPrNumbers:
    """The trailing ``(#N)`` group is the branch-invariant join key.

    ``prior_notes`` and the release-cut dedup both read it, so the grammar lives
    here once rather than being re-derived per caller.
    """

    @pytest.mark.parametrize("line,expected", [
        ("* Fix a crash by @alice (#42)", {42}),
        ("* Fix a crash (#42)", {42}),
        ("* Assorted fixes (#47, #232)", {47, 232}),
        ("* Assorted fixes (#47,#232, #9)", {47, 232, 9}),
        ("* Fix a crash (#42).", {42}),
        ("* Fix a crash (#42) ", {42}),
        # Only the *final* group counts; a mid-sentence reference is prose.
        ("* Revert the change from (#1200) that broke replicas (#42)", {42}),
        ("* Fix a crash", set()),
        # A reference that is not at the end credits nothing: the group must be
        # trailing, or prose quoting a PR would be read as this note's credit.
        ("* Revert the change from (#1200) that broke replicas", set()),
    ])
    def test_trailing_group_parsed(self, line, expected) -> None:
        assert rf.trailing_pr_numbers(line) == expected

    def test_only_bullets_are_credited(self) -> None:
        text = (
            "Valkey 9.0.1  -  Released Tue 21 July 2026\n"
            "### Bug Fixes\n"
            "* Fix a crash by @alice (#42)\n"
            "Some prose mentioning (#99)\n"
            "- Fix a leak by @bob (#43)\n"
        )
        assert rf.credited_pr_numbers(text) == {42, 43}


class TestDatedReleaseKey:
    """Dated headings sort deterministically, RC before the GA of one version."""

    def test_ga_and_rc_keys(self) -> None:
        assert rf.dated_release_key("Valkey 9.0.1  -  Released Tue 21 July 2026", "Valkey") == (
            9, 0, 1, 1, 0,
        )
        assert rf.dated_release_key("Valkey 9.0.0 GA  -  Released Mon 31 March 2025", "Valkey") == (
            9, 0, 0, 1, 0,
        )
        assert rf.dated_release_key("Valkey 9.0.0 RC2  -  Released Thu 20 March 2025", "Valkey") == (
            9, 0, 0, 0, 2,
        )

    def test_rc_sorts_before_ga_of_the_same_version(self) -> None:
        rc1 = rf.dated_release_key("Valkey 9.0.0 RC1", "Valkey")
        rc2 = rf.dated_release_key("Valkey 9.0.0 RC2", "Valkey")
        ga = rf.dated_release_key("Valkey 9.0.0 GA", "Valkey")
        patch = rf.dated_release_key("Valkey 9.0.1", "Valkey")
        assert rc1 < rc2 < ga < patch

    @pytest.mark.parametrize("line", [
        "Valkey 9.0 release notes",
        "Valkey Search 1.2.1 GA",
        "### Bug Fixes",
        "Valkeyish 9.0.1",
        "",
    ])
    def test_non_headings_rejected(self, line) -> None:
        assert rf.dated_release_key(line, "Valkey") is None

    def test_display_name_is_matched_exactly(self) -> None:
        assert rf.dated_release_key("Valkey Search 1.2.1 GA", "Valkey Search") == (
            1, 2, 1, 1, 0,
        )
