from __future__ import annotations

from scripts.common.text_utils import compact_text, defused, strip_ansi


def test_strip_ansi_removes_color_codes():
    assert strip_ansi("\x1b[31mERROR\x1b[0m") == "ERROR"


def test_strip_ansi_noop_on_clean_text():
    assert strip_ansi("hello world") == "hello world"


def test_strip_ansi_empty_string():
    assert strip_ansi("") == ""


def test_compact_text_normalizes_and_truncates():
    assert compact_text("  one \n two  ", 20) == "one two"
    assert compact_text("one two three", 8) == "one two…"


def test_defused_text_holds_nothing_github_acts_on():
    text = (
        "Fixes #12 and closes valkey-io/valkey#3. See https://evil.example/x and [the docs](http://a.b/c). "
        "cc @someone and @org/team."
    )

    assert defused(text) == (
        "Fixes #\u200b12 and closes valkey-io/valkey#\u200b3. See https:\u200b//evil.example/x and the docs "
        "(http:\u200b//a.b/c). cc @\u200bsomeone and @\u200borg/team."
    )


def test_defusing_adds_no_markup_that_the_text_could_turn_against_it():
    """A stray backtick, emphasis or raw HTML around it changes nothing: the break is in the token itself."""
    assert defused("see ` @octocat and _@octocat_") == "see ` @\u200boctocat and _@\u200boctocat_"
    assert defused('<a href="//evil.example">x</a> www.evil.example') == (
        '<\u200ba href="//evil.example">x<\u200b/a> www\u200b.evil.example'
    )
    assert defused("`build @valkey-io/core-team`") == "`build @\u200bvalkey-io/core-team`"


def test_a_closing_keyword_is_broken_before_either_way_of_writing_an_issue():
    assert defused("Fixes #12, closes o/r#3 and resolves GH-7.") == (
        "Fixes #\u200b12, closes o/r#\u200b3 and resolves GH-\u200b7."
    )


def test_defusing_leaves_plain_references_addresses_and_comparisons_alone():
    text = "#44 is only a reference, a@b.com an address, prefixes #5 no keyword, and a < b no tag."

    assert defused(text) == text
