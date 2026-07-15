"""Tests for canonical release-notes rendering.

Uses the real format module (:mod:`scripts.release_notes.release_format`) so the
rendered output is validated against the real parser, not a re-implementation.
The fixture clone under ``tests/fixtures/valkey_clone`` supplies only data files
(``00-RELEASENOTES``).
"""

from __future__ import annotations

import os
import re

from scripts.release_notes import release_format
from scripts.release_notes.models import CategorizedBullet
from scripts.release_notes.render import (
    format_bullet,
    group_bullets,
)

_FIXTURE_CLONE = os.path.join(os.path.dirname(__file__), "fixtures", "valkey_clone")

# The trailing-PR-ref and author regexes of valkey's hand-written release-note
# convention, replicated to assert each rendered bullet matches that form.
_TRAILING_PR_RE = re.compile(r"\(#([^)]*)\)\s*$")
_AUTHOR_RE = re.compile(r"by @([\w-]+)")


def _fmt():
    return release_format


def _bullet(pr, author, category, text):
    return CategorizedBullet(pr_number=pr, author=author, category=category, text=text)


class TestFormatBullet:
    def test_canonical_form(self) -> None:
        line = format_bullet(_bullet(40, "BChan-0", "Bug Fixes", "fix a crash"))
        assert line == "* fix a crash by @BChan-0 (#40)"

    def test_trailing_pr_and_author_present(self) -> None:
        line = format_bullet(_bullet(7, "jdoe", "Bug Fixes", "x"))
        assert _TRAILING_PR_RE.search(line)
        assert _AUTHOR_RE.search(line)

    def test_ghost_author_omits_attribution_but_keeps_pr(self) -> None:
        line = format_bullet(_bullet(7, "", "Bug Fixes", "x"))
        assert line == "* x (#7)"
        assert _TRAILING_PR_RE.search(line)
        assert not _AUTHOR_RE.search(line)

    def test_newline_in_text_collapsed_to_single_line(self) -> None:
        line = format_bullet(_bullet(7, "a", "Bug Fixes", "line one\nline two"))
        assert "\n" not in line
        assert line == "* line one line two by @a (#7)"

    def test_text_with_h2_does_not_survive_as_its_own_line(self) -> None:
        # A '## ...' on its own line would read as a new section in the changelog.
        line = format_bullet(_bullet(7, "a", "Bug Fixes", "fixed\n## Injected"))
        assert "\n" not in line
        assert _TRAILING_PR_RE.search(line)

    def test_trailing_ref_in_text_not_duplicated(self) -> None:
        line = format_bullet(_bullet(40, "", "Bug Fixes", "see (#99)"))
        # The stray trailing ref is stripped; only the real (#40) remains at end.
        assert line == "* see (#40)"
        assert line.count("(#") == 1

    def test_author_handle_sanitized_to_login_charset(self) -> None:
        line = format_bullet(_bullet(7, "alice smith", "Bug Fixes", "x"))
        # The space would otherwise truncate _AUTHOR_RE's capture.
        assert "by @alicesmith" in line
        assert _AUTHOR_RE.search(line).group(1) == "alicesmith"


class TestGroupBullets:
    def test_canonical_order_preserved(self) -> None:
        fmt = _fmt()
        bullets = [
            _bullet(3, "a", "Bug Fixes", "b"),
            _bullet(1, "a", "Behavior Changes", "c"),
        ]
        grouped = group_bullets(bullets)
        keys = list(grouped.keys())
        # Behavior Changes precedes Bug Fixes (canonical order).
        assert keys.index("Behavior Changes") < keys.index("Bug Fixes")

    def test_noncanonical_category_coerced_to_catch_all(self) -> None:
        # The model never creates a new header: an off-list category is a
        # suggestion, so the bullet lands in the catch-all, not under "Networking".
        fmt = _fmt()
        grouped = group_bullets([_bullet(9, "a", "Networking", "n")])
        assert "Networking" not in grouped
        assert grouped == {fmt.CATCH_ALL_CATEGORY: ["* n by @a (#9)"]}

    def test_all_keys_are_canonical(self) -> None:
        # No matter what categories the model returns, every rendered key is one of
        # the canonical categories; an invented header can never be emitted.
        fmt = _fmt()
        bullets = [
            _bullet(1, "a", "Bug Fixes", "x"),
            _bullet(2, "a", "Networking", "y"),
            _bullet(3, "a", "## Injected", "z"),
        ]
        grouped = group_bullets(bullets)
        assert set(grouped).issubset(set(fmt.CATEGORIES))

    def test_reserved_sections_refused(self) -> None:
        fmt = _fmt()
        grouped = group_bullets(
            [_bullet(1, "a", "Security Fixes", "x"), _bullet(2, "a", "Contributors", "y")]
        )
        assert grouped == {}

    def test_reserved_section_refused_case_insensitively(self) -> None:
        # A lowercase "security fixes" must be refused too, not coerced into the
        # catch-all where it would ship next to the real auto-generated section.
        fmt = _fmt()
        grouped = group_bullets(
            [_bullet(1, "a", "security fixes", "x"), _bullet(2, "a", "CONTRIBUTORS", "y")]
        )
        assert grouped == {}

    def test_multiple_bullets_same_category(self) -> None:
        fmt = _fmt()
        grouped = group_bullets(
            [_bullet(1, "a", "Bug Fixes", "one"), _bullet(2, "b", "Bug Fixes", "two")]
        )
        assert len(grouped["Bug Fixes"]) == 2

    def test_injected_heading_category_cannot_emit_header(self) -> None:
        # A category like "## Injected" must not survive as a block-terminating
        # header; it is coerced into the catch-all like any other off-list value.
        fmt = _fmt()
        grouped = group_bullets([_bullet(1, "a", "## Injected", "x")])
        assert all(not k.startswith("#") for k in grouped)
        assert grouped == {fmt.CATCH_ALL_CATEGORY: ["* x by @a (#1)"]}


class TestMaliciousBulletCannotBreakSection:
    def test_injected_heading_in_text_does_not_split_the_section(self) -> None:
        # A bullet whose text embeds a fake "## " / "### " heading must render as a
        # single bullet line, not spawn a spurious category or truncate the section.
        fmt = _fmt()
        bullets = [
            _bullet(40, "a", "Bug Fixes", "fixed\n## Injected\n### Bug Fixes\n* fake (#1)"),
            _bullet(41, "b", "Build and Tooling", "later category still rendered"),
        ]
        grouped = group_bullets(bullets)
        assert len(grouped["Bug Fixes"]) == 1
        assert "\n" not in grouped["Bug Fixes"][0]
        section = fmt.render_version_section("9.1.0", "rc1", "LOW", "2026-06-25", grouped)
        # Both categories render; the injected heading (now inside a bullet line)
        # did not create an extra category HEADER. Count header lines, not the
        # substring; the sanitized bullet text legitimately contains "### Bug Fixes".
        header_lines = [ln for ln in section.splitlines() if ln.strip() == "### Bug Fixes"]
        assert len(header_lines) == 1
        assert any(ln.strip() == "### Build and Tooling" for ln in section.splitlines())


class TestRenderVersionSection:
    def test_bullets_render_under_their_categories(self) -> None:
        fmt = _fmt()
        bullets = [
            _bullet(40, "BChan-0", "Bug Fixes", "fix crash"),
            _bullet(41, "jdoe", "New Features and Enhanced Behavior", "new opt"),
        ]
        section = fmt.render_version_section(
            "9.1.0", "rc1", "LOW", "2026-06-25", group_bullets(bullets)
        )
        assert "* fix crash by @BChan-0 (#40)" in section
        assert "* new opt by @jdoe (#41)" in section
        # Empty categories are omitted from the dated section.
        assert "### Module API Changes" not in section

    def test_bullets_satisfy_check_release_notes_rules(self) -> None:
        # Every rendered bullet keeps its trailing (#N), the form the label gate wants.
        fmt = _fmt()
        grouped = group_bullets(
            [_bullet(40, "BChan-0", "Bug Fixes", "a"), _bullet(41, "", "Bug Fixes", "b")]
        )
        section = fmt.render_version_section("9.1.0", "rc1", "LOW", "2026-06-25", grouped)
        for line in section.splitlines():
            if line.startswith("* "):
                assert _TRAILING_PR_RE.search(line), line
