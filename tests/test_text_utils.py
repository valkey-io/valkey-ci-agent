from __future__ import annotations

from scripts.common.text_utils import compact_text, strip_ansi


def test_strip_ansi_removes_color_codes():
    assert strip_ansi("\x1b[31mERROR\x1b[0m") == "ERROR"


def test_strip_ansi_noop_on_clean_text():
    assert strip_ansi("hello world") == "hello world"


def test_strip_ansi_empty_string():
    assert strip_ansi("") == ""


def test_compact_text_normalizes_and_truncates():
    assert compact_text("  one \n two  ", 20) == "one two"
    assert compact_text("one two three", 8) == "one two…"
