"""Tests for carrying published note wording across release lines.

The parsing and alignment tests are pure; the index tests build a real local
"remote" with two release branches, because the whole point of the module is that
it reads the clone the cut already made and never touches the network.
"""

from __future__ import annotations

import os

import pytest

from scripts.common.proc import run_git
from scripts.release_notes import prior_notes as pn
from scripts.release_notes import render as render_mod
from scripts.release_notes.models import CategorizedBullet, MergedPR

_NOTES_FILE = "00-RELEASENOTES"


def _bullet(
    pr_number: int, text: str, *, author: str = "alice", category: str = "Bug Fixes",
    uncertain: bool = False, uncertain_reason: str = "",
) -> CategorizedBullet:
    return CategorizedBullet(
        pr_number=pr_number, author=author, category=category, text=text,
        uncertain=uncertain, uncertain_reason=uncertain_reason,
    )


def _note(
    pr_number: int, text: str, *, author: str = "alice", line: str = "9.0",
    heading: str = "Valkey 9.0.1", category: str = "Bug Fixes",
    order: tuple = (0,),
) -> pn.PriorNote:
    return pn.PriorNote(
        pr_number=pr_number, text=text, author=author, release_line=line,
        release_heading=heading, category=category, order=order,
    )


def _index(*notes: pn.PriorNote, read=("9.0",), failed=()) -> pn.PriorNoteIndex:
    grouped: dict = {}
    for note in notes:
        grouped.setdefault(note.pr_number, []).append(note)
    return pn.PriorNoteIndex(
        notes={k: tuple(v) for k, v in grouped.items()},
        lines_read=tuple(read), lines_failed=tuple(failed),
    )


def _pr(number: int, *, title: str = "Fix something", author: str = "alice") -> MergedPR:
    return MergedPR(
        number=number, title=title, body="", author=author,
        url=f"https://example.invalid/{number}", labels=(),
        merge_commit_sha="0" * 40,
    )


def _section(version: str, *categories) -> str:
    """A canonical dated section as render_version_section writes it."""
    heading = f"Valkey {version}  -  Released Tue 21 July 2026"
    out = [heading, "-" * len(heading), "", "Upgrade urgency LOW.", ""]
    for name, bullets in categories:
        out.append(f"### {name}")
        out.extend(bullets)
        out.append("")
    return "\n".join(out)


class TestReleaseLineKey:
    """Only a bare ``M.m`` names a release line."""

    def test_release_line_parsed(self) -> None:
        assert pn.release_line_key("8.1") == (8, 1)
        assert pn.release_line_key(" 10.0 ") == (10, 0)
        assert pn.is_release_line("7.2")

    def test_non_release_refs_rejected(self) -> None:
        for name in ("main", "unstable", "8", "8.1.2", "8.x", "release-8.1", "v8.1", ""):
            assert pn.release_line_key(name) is None, name
            assert not pn.is_release_line(name)


class TestReleaseLineRefs:
    """Sibling lines come from the clone's own remote-tracking refs."""

    @pytest.fixture
    def clone(self, tmp_path) -> str:
        remote = str(tmp_path / "remote")
        os.makedirs(remote)
        run_git(remote, "init", "-q", "--initial-branch", "main")
        run_git(remote, "config", "user.email", "t@e")
        run_git(remote, "config", "user.name", "t")
        (tmp_path / "remote" / "f").write_text("x", encoding="utf-8")
        run_git(remote, "add", "f")
        run_git(remote, "commit", "-q", "-m", "c")
        for branch in ("7.2", "8.0", "8.1", "9.0", "release-9.1"):
            run_git(remote, "branch", branch)
        clone = str(tmp_path / "clone")
        run_git(None, "clone", "-q", remote, clone)
        return clone

    def test_release_lines_listed_newest_first(self, clone) -> None:
        assert pn.release_line_refs(clone) == (
            ("origin/9.0", "9.0"),
            ("origin/8.1", "8.1"),
            ("origin/8.0", "8.0"),
            ("origin/7.2", "7.2"),
        )

    def test_current_line_excluded(self, clone) -> None:
        lines = [line for _ref, line in pn.release_line_refs(clone, exclude="8.1")]
        assert lines == ["9.0", "8.0", "7.2"]

    def test_git_failure_degrades_to_no_lines(self, tmp_path) -> None:
        # Not a repository: alignment must lose its input, not raise.
        assert pn.release_line_refs(str(tmp_path / "nope")) == ()


class TestReadLineNotes:
    """Reading a sibling line's changelog never raises."""

    @pytest.fixture
    def repo(self, tmp_path) -> str:
        repo = str(tmp_path / "r")
        os.makedirs(repo)
        run_git(repo, "init", "-q")
        run_git(repo, "config", "user.email", "t@e")
        run_git(repo, "config", "user.name", "t")
        (tmp_path / "r" / _NOTES_FILE).write_text("hello\n", encoding="utf-8")
        run_git(repo, "add", _NOTES_FILE)
        run_git(repo, "commit", "-q", "-m", "c")
        return repo

    def test_notes_read_at_ref(self, repo) -> None:
        text, reason = pn.read_line_notes(repo, "HEAD", _NOTES_FILE)
        assert text == "hello\n"
        assert reason == ""

    def test_missing_file_reports_a_reason(self, repo) -> None:
        text, reason = pn.read_line_notes(repo, "HEAD", "NO-SUCH-FILE")
        assert text is None
        assert reason

    def test_oversized_blob_refused(self, repo, monkeypatch) -> None:
        monkeypatch.setattr(pn, "MAX_NOTES_BYTES", 1)
        text, reason = pn.read_line_notes(repo, "HEAD", _NOTES_FILE)
        assert text is None
        assert "cap" in reason


class TestSplitBullet:
    """A published line reverses back into (text, handle, PR)."""

    def test_canonical_line(self) -> None:
        assert pn.split_bullet("* Fix a crash in RESET by @alice (#42)") == (
            "Fix a crash in RESET", "alice", 42,
        )

    def test_dash_marker_and_no_author(self) -> None:
        assert pn.split_bullet("- Fix a crash (#42)") == ("Fix a crash", "", 42)

    def test_terminal_punctuation_normalized_like_the_formatter(self) -> None:
        # format_bullet drops terminal punctuation, so the recovered text must
        # already be at that fixed point or nothing would ever round-trip.
        assert pn.split_bullet("* Fix a crash. by @alice (#42)")[0] == "Fix a crash"

    def test_multi_pr_bullet_belongs_to_no_single_pr(self) -> None:
        assert pn.split_bullet("* Assorted fixes (#47, #232)") is None

    def test_bullet_without_a_pr_reference(self) -> None:
        assert pn.split_bullet("* Assorted fixes") is None


class TestRendersBack:
    """The round trip, not a regex, decides whether prose is reusable."""

    @pytest.mark.parametrize("text", [
        "Fix a crash in RESET",
        # Both of these mention a reference mid-sentence. A guard that rejected
        # any "(#" or " by @" would refuse them; they are legitimate notes.
        "Revert the change from (#1200) that broke replicas",
        "keys deleted by @-prefixed clients are now counted",
        "Fix a crash on 32-bit builds",
        "Support `CLIENT NO-EVICT on` (see the docs)",
    ])
    def test_legitimate_prose_round_trips(self, text) -> None:
        assert pn.renders_back(text, author="alice", pr_number=42)

    @pytest.mark.parametrize("text", [
        "Fix a crash (#99)",          # formatter would strip the trailing ref
        "Fix a crash by @bob",        # formatter would strip the trailing credit
        "Fix a crash by @bob (#99)",  # both
    ])
    def test_prose_the_formatter_would_rewrite_is_refused(self, text) -> None:
        assert not pn.renders_back(text, author="alice", pr_number=42)

    def test_round_trip_is_measured_against_the_real_formatter(self) -> None:
        text = "Fix a crash in RESET"
        rendered = render_mod.format_bullet(_bullet(42, text))
        assert rendered == "* Fix a crash in RESET by @alice (#42)"
        assert pn.split_bullet(rendered) == (text, "alice", 42)


class TestUsableText:
    """Length and word checks on top of the round trip."""

    def test_reasonable_note_is_usable(self) -> None:
        assert pn.usable_text("Fix a crash in RESET", author="alice", pr_number=1)

    @pytest.mark.parametrize("text", ["", "   ", "Fixed", "...", "-"])
    def test_fragments_refused(self, text) -> None:
        assert not pn.usable_text(text, author="alice", pr_number=1)

    def test_paragraph_length_refused(self) -> None:
        assert not pn.usable_text("word " * 200, author="alice", pr_number=1)


class TestParsePublishedNotes:
    """Only canonical, agent-written sections are indexed."""

    def test_canonical_section_indexed(self) -> None:
        text = _section("9.0.1", ("Bug Fixes", ["* Fix a crash in RESET by @alice (#42)"]))
        notes = pn.parse_published_notes(text, display_name="Valkey", release_line="9.0")
        assert len(notes) == 1
        assert notes[0].pr_number == 42
        assert notes[0].text == "Fix a crash in RESET"
        assert notes[0].author == "alice"
        assert notes[0].category == "Bug Fixes"
        assert notes[0].release_line == "9.0"
        # The heading label drops the "- Released <date>" tail.
        assert notes[0].release_heading == "Valkey 9.0.1"

    def test_reserved_sections_skipped(self) -> None:
        text = _section(
            "9.0.1",
            ("Security Fixes", ["* CVE-2026-1: Fix a use-after-free by @alice (#42)"]),
            ("Contributors", ["* alice (#42)"]),
            ("Bug Fixes", ["* Fix a crash by @bob (#43)"]),
        )
        notes = pn.parse_published_notes(text, display_name="Valkey", release_line="9.0")
        assert [n.pr_number for n in notes] == [43]

    def test_hand_authored_setext_sections_not_reached(self) -> None:
        # Every pre-agent section writes its categories as setext headings. Those
        # bullets have their own conventions (no credit, several PRs per line) and
        # must stay out of the index.
        text = (
            "Valkey 9.0 release notes\n"
            "========================\n\n"
            + _section("9.0.1", ("Bug Fixes", ["* Fix a crash by @alice (#42)"]))
            + "\n"
            "Valkey 9.0.0 GA  -  Released Mon 31 March 2025\n"
            "=============================================\n\n"
            "Bug fixes\n"
            "=========\n\n"
            "* Fix something old (#7)\n"
            "* Assorted fixes (#47, #232)\n"
        )
        notes = pn.parse_published_notes(text, display_name="Valkey", release_line="9.0")
        assert [n.pr_number for n in notes] == [42]

    def test_other_heading_levels_close_the_category_scope(self) -> None:
        text = _section("9.0.1", ("Bug Fixes", ["* Fix a crash by @alice (#42)"])) + (
            "\n## Notes for packagers\n\n* Not a release note (#99)\n"
        )
        notes = pn.parse_published_notes(text, display_name="Valkey", release_line="9.0")
        assert [n.pr_number for n in notes] == [42]

    def test_bullets_outside_any_dated_section_ignored(self) -> None:
        text = "### Bug Fixes\n\n* Stray bullet by @alice (#42)\n"
        assert pn.parse_published_notes(
            text, display_name="Valkey", release_line="9.0"
        ) == ()

    def test_foreign_display_name_matches_nothing(self) -> None:
        text = _section("9.0.1", ("Bug Fixes", ["* Fix a crash by @alice (#42)"]))
        assert pn.parse_published_notes(
            text, display_name="Valkey Search", release_line="9.0"
        ) == ()

    def test_order_prefers_the_earliest_section_in_a_line(self) -> None:
        text = (
            _section("9.0.2", ("Bug Fixes", ["* Later wording by @alice (#42)"]))
            + "\n"
            + _section("9.0.1", ("Bug Fixes", ["* Original wording by @alice (#42)"]))
        )
        notes = sorted(
            pn.parse_published_notes(text, display_name="Valkey", release_line="9.0"),
            key=lambda note: note.order,
        )
        assert [n.text for n in notes] == ["Original wording", "Later wording"]

    def test_rc_sorts_before_the_ga_of_the_same_version(self) -> None:
        text = (
            _section("9.0.0 GA", ("Bug Fixes", ["* GA wording by @alice (#42)"]))
            + "\n"
            + _section("9.0.0 RC1", ("Bug Fixes", ["* RC wording by @alice (#42)"]))
        )
        notes = sorted(
            pn.parse_published_notes(text, display_name="Valkey", release_line="9.0"),
            key=lambda note: note.order,
        )
        assert [n.text for n in notes] == ["RC wording", "GA wording"]


class TestBuildIndex:
    """The index is built from the clone, with no network access."""

    @pytest.fixture
    def clone(self, tmp_path) -> str:
        remote = str(tmp_path / "remote")
        os.makedirs(remote)
        # 7.2 is the root and never gains the changelog file, standing in for a
        # release line older than the convention.
        run_git(remote, "init", "-q", "--initial-branch", "7.2")
        run_git(remote, "config", "user.email", "t@e")
        run_git(remote, "config", "user.name", "t")
        (tmp_path / "remote" / "README").write_text("x", encoding="utf-8")
        run_git(remote, "add", "README")
        run_git(remote, "commit", "-q", "-m", "root")

        notes = tmp_path / "remote" / _NOTES_FILE
        for branch, text in (
            ("8.1", _section("8.1.9", ("Bug Fixes", [
                "* Fix a leak in ZDIFF by @alice (#42)",
            ]))),
            ("9.0", _section("9.0.1", ("Bug Fixes", [
                "* Fix a memory leak in ZDIFF when the result set is empty by @alice (#42)",
                "* Fix a crash in RESET by @bob (#43)",
            ]))),
        ):
            run_git(remote, "checkout", "-q", "-b", branch, "7.2")
            notes.write_text(text, encoding="utf-8")
            run_git(remote, "add", _NOTES_FILE)
            run_git(remote, "commit", "-q", "-m", f"{branch} notes")

        clone = str(tmp_path / "clone")
        run_git(None, "clone", "-q", "--branch", "9.0", remote, clone)
        return clone

    def _index(self, clone, **kwargs) -> pn.PriorNoteIndex:
        return pn.build_index(
            clone, notes_file=_NOTES_FILE, display_name="Valkey", **kwargs
        )

    def test_every_readable_line_indexed(self, clone) -> None:
        index = self._index(clone)
        assert set(index.lines_read) == {"9.0", "8.1"}
        assert [line for line, _reason in index.lines_failed] == ["7.2"]
        assert set(index.notes) == {42, 43}

    def test_newest_line_wins_for_a_shared_pr(self, clone) -> None:
        index = self._index(clone)
        candidates = index.candidates(42)
        assert [note.release_line for note in candidates] == ["9.0", "8.1"]
        assert candidates[0].text.startswith("Fix a memory leak in ZDIFF")

    def test_current_line_excluded(self, clone) -> None:
        index = self._index(clone, current_line="9.0")
        assert index.lines_read == ("8.1",)
        assert [note.release_line for note in index.candidates(42)] == ["8.1"]

    def test_wanted_bounds_the_index(self, clone) -> None:
        index = self._index(clone, wanted={43})
        assert set(index.notes) == {43}

    def test_unreadable_line_reports_a_reason(self, clone) -> None:
        index = self._index(clone)
        reasons = dict(index.lines_failed)
        assert reasons["7.2"]

    def test_non_repository_degrades_to_an_empty_index(self, tmp_path) -> None:
        index = self._index(str(tmp_path / "nope"))
        assert index.notes == {}
        assert index.lines_read == ()

    def test_carried_wording_renders_byte_identically(self, clone) -> None:
        # The whole point of the module: after alignment, the two lines' bullets
        # differ only in the credit they render, never in the prose.
        index = self._index(clone, current_line="8.1")
        published = index.candidates(42)[0]
        result = pn.align_wording(
            [_bullet(42, "Some freshly invented wording for the leak")],
            index, prs_by_number={42: _pr(42)},
        )
        carried = render_mod.format_bullet(result.bullets[0])
        original = render_mod.format_bullet(
            _bullet(42, published.text, author=published.author)
        )
        assert carried == original
        assert carried == (
            "* Fix a memory leak in ZDIFF when the result set is empty by @alice (#42)"
        )


class TestAlignWording:
    """Substitution, and every deliberate refusal to substitute."""

    def test_published_wording_carried(self) -> None:
        index = _index(_note(42, "Fix a memory leak in ZDIFF"))
        result = pn.align_wording(
            [_bullet(42, "Fix leak", category="Bug Fixes")],
            index, prs_by_number={42: _pr(42)},
        )
        assert result.bullets[0].text == "Fix a memory leak in ZDIFF"
        assert [n.pr_number for n in result.aligned] == [42]
        assert result.aligned[0].release_line == "9.0"
        assert result.aligned[0].text == "Fix a memory leak in ZDIFF"
        assert result.aligned[0].generated_text == "Fix leak"
        assert result.declined == ()

    def test_only_the_text_is_carried(self) -> None:
        # Category, credit and PR number stay this cut's own: a change can be a
        # Bug Fix on one line and a Behavior Change on the line that shipped it.
        index = _index(_note(42, "Published wording", author="alice", category="Bug Fixes"))
        bullet = _bullet(42, "Local wording", author="alice", category="Behavior Changes")
        result = pn.align_wording([bullet], index, prs_by_number={42: _pr(42)})
        carried = result.bullets[0]
        assert carried.category == "Behavior Changes"
        assert carried.author == "alice"
        assert carried.pr_number == 42

    def test_model_uncertainty_survives_the_substitution(self) -> None:
        # The flag judges the *change* ("unclear whether user-facing"), which
        # rewording does not answer. Clearing it would hide a real signal.
        index = _index(_note(42, "Published wording"))
        bullet = _bullet(
            42, "Local wording", uncertain=True,
            uncertain_reason="unclear whether this is user-facing",
        )
        result = pn.align_wording([bullet], index, prs_by_number={42: _pr(42)})
        assert result.bullets[0].uncertain
        assert result.bullets[0].uncertain_reason == "unclear whether this is user-facing"

    def test_pr_with_no_published_note_untouched(self) -> None:
        result = pn.align_wording(
            [_bullet(99, "Local wording")], _index(), prs_by_number={99: _pr(99)},
        )
        assert result.bullets[0].text == "Local wording"
        assert result.aligned == ()
        assert result.declined == ()

    def test_matching_wording_reported_as_already_consistent(self) -> None:
        index = _index(_note(42, "Same wording"))
        result = pn.align_wording(
            [_bullet(42, "Same wording")], index, prs_by_number={42: _pr(42)},
        )
        assert result.already_consistent == (42,)
        assert result.aligned == ()
        assert result.declined == ()

    def test_credit_disagreement_holds_the_cut(self) -> None:
        index = _index(_note(42, "Published wording", author="carol"))
        result = pn.align_wording(
            [_bullet(42, "Local wording", author="alice")],
            index, prs_by_number={42: _pr(42)},
        )
        assert result.bullets[0].text == "Local wording"
        assert len(result.declined) == 1
        assert result.declined[0].needs_review
        assert "carol" in result.declined[0].reason
        assert "alice" in result.declined[0].reason

    def test_missing_credit_on_either_side_is_not_a_disagreement(self) -> None:
        index = _index(_note(42, "Published wording", author=""))
        result = pn.align_wording(
            [_bullet(42, "Local wording", author="alice")],
            index, prs_by_number={42: _pr(42)},
        )
        assert result.bullets[0].text == "Published wording"

    def test_credit_comparison_is_case_insensitive(self) -> None:
        index = _index(_note(42, "Published wording", author="Alice"))
        result = pn.align_wording(
            [_bullet(42, "Local wording", author="alice")],
            index, prs_by_number={42: _pr(42)},
        )
        assert result.declined == ()

    def test_material_scope_disagreement_holds_the_cut(self) -> None:
        # An adapted backport: the sibling limits the fix to 32-bit builds, this
        # line's note does not. No other check can see this, because the PR's
        # title and body are byte-identical on both lines.
        from scripts.release_notes.generate import material_scope_tokens

        index = _index(_note(42, "Fix a crash on 32-bit builds"))
        result = pn.align_wording(
            [_bullet(42, "Fix a crash when loading an RDB")],
            index, prs_by_number={42: _pr(42)},
            scope_tokens=material_scope_tokens,
        )
        assert result.bullets[0].text == "Fix a crash when loading an RDB"
        assert len(result.declined) == 1
        assert result.declined[0].needs_review
        assert "32-bit" in result.declined[0].reason

    def test_matching_material_scope_still_carries(self) -> None:
        from scripts.release_notes.generate import material_scope_tokens

        index = _index(_note(42, "Fix a crash on 32-bit builds during RDB load"))
        result = pn.align_wording(
            [_bullet(42, "Fix an RDB crash on 32-bit builds")],
            index, prs_by_number={42: _pr(42)},
            scope_tokens=material_scope_tokens,
        )
        assert result.bullets[0].text == "Fix a crash on 32-bit builds during RDB load"
        assert result.declined == ()

    def test_cve_wording_is_never_copied(self) -> None:
        index = _index(_note(42, "Fix a use-after-free in TLS handling"))
        result = pn.align_wording(
            [_bullet(42, "Local wording")], index,
            prs_by_number={42: _pr(42)}, cve_prs={42},
        )
        assert result.bullets[0].text == "Local wording"
        assert len(result.declined) == 1
        assert not result.declined[0].needs_review
        assert "CVE" in result.declined[0].reason

    def test_unconfirmed_credit_is_reported_without_holding(self) -> None:
        # The unresolved backport / cherry-pick / collision that made the credit
        # doubtful already holds the cut on its own; a second hold for the same
        # fact would only train reviewers to click through the banner.
        index = _index(_note(42, "Published wording"))
        result = pn.align_wording(
            [_bullet(42, "Local wording")], index,
            prs_by_number={42: _pr(42)}, unconfirmed_credit_prs={42},
        )
        assert result.bullets[0].text == "Local wording"
        assert len(result.declined) == 1
        assert not result.declined[0].needs_review
        assert "could not confirm" in result.declined[0].reason

    def test_revert_polarity_mismatch_reported_without_holding(self) -> None:
        index = _index(_note(42, "Revert the ZDIFF optimization"))
        result = pn.align_wording(
            [_bullet(42, "Speed up ZDIFF on large sets")],
            index, prs_by_number={42: _pr(42, title="Speed up ZDIFF")},
        )
        assert result.bullets[0].text == "Speed up ZDIFF on large sets"
        assert len(result.declined) == 1
        assert not result.declined[0].needs_review
        assert "revert" in result.declined[0].reason

    def test_revert_recognized_from_the_pr_title(self) -> None:
        # The note need not say "revert" if the PR does; that is the same change.
        index = _index(_note(42, "Revert the ZDIFF optimization"))
        result = pn.align_wording(
            [_bullet(42, "Undo the ZDIFF change that regressed latency")],
            index, prs_by_number={42: _pr(42, title='Revert "Speed up ZDIFF"')},
        )
        assert result.bullets[0].text == "Revert the ZDIFF optimization"
        assert result.declined == ()

    def test_revert_titled_pr_whose_notes_both_avoid_the_word_still_carries(self) -> None:
        # The PR title is the *original* source PR's, so it is byte-identical on
        # every release line and cannot tell the two notes apart. Treating it as
        # a polarity assertion on this cut's side would collapse the check to
        # "decline unless the published text literally says revert" -- declining
        # the common case where neither line uses the word, and reporting a
        # contradiction that is untrue of both texts.
        index = _index(_note(42, "Restore the previous ZDIFF behavior"))
        result = pn.align_wording(
            [_bullet(42, "Go back to the old ZDIFF behavior")],
            index, prs_by_number={42: _pr(42, title='Revert "Speed up ZDIFF"')},
        )
        assert result.bullets[0].text == "Restore the previous ZDIFF behavior"
        assert result.declined == ()

    def test_unusable_candidate_reported_as_non_canonical(self) -> None:
        index = _index(_note(42, "Fixed"))
        result = pn.align_wording(
            [_bullet(42, "Local wording")], index, prs_by_number={42: _pr(42)},
        )
        assert result.bullets[0].text == "Local wording"
        assert len(result.declined) == 1
        assert not result.declined[0].needs_review
        assert "canonical" in result.declined[0].reason

    def test_first_usable_candidate_wins(self) -> None:
        index = _index(
            _note(42, "Fixed", line="9.0", order=(0,)),
            _note(42, "Fix a memory leak in ZDIFF", line="8.1", order=(1,)),
        )
        result = pn.align_wording(
            [_bullet(42, "Local wording")], index, prs_by_number={42: _pr(42)},
        )
        assert result.bullets[0].text == "Fix a memory leak in ZDIFF"
        assert result.aligned[0].release_line == "8.1"

    def test_candidate_that_would_not_render_under_this_credit_refused(self) -> None:
        # Stage 2 catches what stage 1 cannot: the round-trip has to hold under
        # the credit the bullet will *actually* render with, not the credit the
        # other line rendered with. Here the published prose ends in a handle
        # mention. On 9.0 it renders "... by @foo_bar by @alice (#42)", where the
        # mention is safely mid-line; this cut resolved no author, so the same
        # text would render "... by @foo_bar (#42)" and the prose's own @foo_bar
        # would land in the credit slot. (format_bullet's handle-stripping charset
        # excludes "_", so it does not normalize the mention away first.)
        index = _index(_note(42, "Fix a crash reported by @foo_bar", author="alice"))
        result = pn.align_wording(
            [_bullet(42, "Local wording", author="")],
            index, prs_by_number={42: _pr(42)},
        )
        assert result.bullets[0].text == "Local wording"
        assert result.aligned == ()
        # Pinned by reason: stage 1's decline reads differently, and a fixture
        # that silently drifted to being rejected there would leave this guard
        # untested.
        assert "under this cut's credit" in result.declined[0].reason
        assert not result.declined[0].needs_review

    def test_bot_login_matching_its_published_handle_is_not_a_disagreement(self) -> None:
        # The published author was recovered from a rendered line, so it is
        # already the handle format_bullet emits; this cut holds the raw login.
        # A bot login ("dependabot[bot]" -> "@dependabotbot") differs from its own
        # printed handle, so comparing the raw strings would report the two lines
        # as disagreeing about the author and hold the release PR as a draft --
        # over a credit both lines print identically.
        index = _index(_note(42, "Bump the CI toolchain to LLVM 18", author="dependabotbot"))
        result = pn.align_wording(
            [_bullet(42, "Update LLVM", author="dependabot[bot]")],
            index, prs_by_number={42: _pr(42)},
        )
        assert result.bullets[0].text == "Bump the CI toolchain to LLVM 18"
        assert result.declined == ()
        assert render_mod.format_bullet(result.bullets[0]) == (
            "* Bump the CI toolchain to LLVM 18 by @dependabotbot (#42)"
        )

    def test_different_handles_still_hold_the_cut(self) -> None:
        # The normalization above must not soften a genuine disagreement: two
        # lines crediting different people for one PR means one of them is wrong.
        index = _index(_note(42, "Published wording", author="alice"))
        result = pn.align_wording(
            [_bullet(42, "Local wording", author="carol")],
            index, prs_by_number={42: _pr(42)},
        )
        assert result.bullets[0].text == "Local wording"
        assert result.declined[0].needs_review
        assert "disagree about whose change this is" in result.declined[0].reason

    def test_author_disagreement_outranks_the_informational_reasons(self) -> None:
        # _decline_reason documents that the two holding reasons are checked
        # first, so the strongest signal is the one the reviewer is shown. A CVE
        # note whose lines credit different people must report the disagreement
        # and hold, not report the (never-holding) CVE reason and let the PR open
        # ready.
        index = _index(_note(42, "Published wording", author="alice"))
        result = pn.align_wording(
            [_bullet(42, "Local wording", author="carol")],
            index, prs_by_number={42: _pr(42)},
            cve_prs={42}, unconfirmed_credit_prs={42},
        )
        assert len(result.declined) == 1
        assert result.declined[0].needs_review
        assert "disagree about whose change this is" in result.declined[0].reason

    def test_unknown_pr_facts_do_not_block_a_carry(self) -> None:
        index = _index(_note(42, "Published wording"))
        result = pn.align_wording([_bullet(42, "Local wording")], index, prs_by_number={})
        assert result.bullets[0].text == "Published wording"

    def test_no_bullets_is_a_no_op(self) -> None:
        result = pn.align_wording([], _index(_note(42, "Published")), prs_by_number={})
        assert result == pn.AlignResult(bullets=())
