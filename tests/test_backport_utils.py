"""Unit tests for backport utility functions."""

from __future__ import annotations

from scripts.backport.utils import (
    braces_balanced,
    build_branch_name,
    build_pr_title,
    has_conflict_markers,
    is_whitespace_only_conflict,
    validate_resolved_content,
)


class TestParseBackportLabels:
    def test_basic(self) -> None:
        assert build_branch_name(123, "8.1") == "backport/123-to-8.1"

    def test_large_pr_number(self) -> None:
        assert build_branch_name(99999, "7.2") == "backport/99999-to-7.2"




class TestBuildPrTitle:
    def test_basic(self) -> None:
        assert build_pr_title("Fix memory leak", "8.1") == "[Backport 8.1] Fix memory leak"

    def test_preserves_original_title(self) -> None:
        title = "[BUG] Segfault on startup"
        assert build_pr_title(title, "7.2") == "[Backport 7.2] [BUG] Segfault on startup"




class TestHasConflictMarkers:
    def test_no_markers(self) -> None:
        assert has_conflict_markers("clean code\nno conflicts\n") is False

    def test_opening_marker(self) -> None:
        assert has_conflict_markers("<<<<<<< HEAD\ncode\n") is True

    def test_separator_marker(self) -> None:
        assert has_conflict_markers("code\n=======\nother\n") is True

    def test_closing_marker(self) -> None:
        assert has_conflict_markers("code\n>>>>>>> branch\n") is True

    def test_full_conflict_block(self) -> None:
        content = "<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> branch\n"
        assert has_conflict_markers(content) is True

    def test_fewer_than_seven_chars(self) -> None:
        assert has_conflict_markers("<<<<<< not enough") is False
        assert has_conflict_markers("====== not enough") is False
        assert has_conflict_markers(">>>>>> not enough") is False

    def test_empty_string(self) -> None:
        assert has_conflict_markers("") is False




class TestBracesBalanced:
    def test_balanced(self) -> None:
        assert braces_balanced("int main() { return 0; }") is True

    def test_nested_balanced(self) -> None:
        assert braces_balanced("void f() { if (x) { y(); } }") is True

    def test_unbalanced_extra_open(self) -> None:
        assert braces_balanced("void f() { if (x) {") is False

    def test_unbalanced_extra_close(self) -> None:
        assert braces_balanced("void f() }") is False

    def test_empty_string(self) -> None:
        assert braces_balanced("") is True

    def test_no_braces(self) -> None:
        assert braces_balanced("// just a comment") is True

    def test_closing_before_opening(self) -> None:
        assert braces_balanced("} {") is False




class TestIsWhitespaceOnlyConflict:
    def test_identical(self) -> None:
        assert is_whitespace_only_conflict("int x = 1;", "int x = 1;") is True

    def test_different_indentation(self) -> None:
        assert is_whitespace_only_conflict("  int x = 1;", "    int x = 1;") is True

    def test_trailing_whitespace(self) -> None:
        assert is_whitespace_only_conflict("int x = 1;  ", "int x = 1;") is True

    def test_different_line_endings(self) -> None:
        assert is_whitespace_only_conflict("a\nb\n", "a\r\nb\r\n") is True

    def test_tabs_vs_spaces(self) -> None:
        assert is_whitespace_only_conflict("\tint x;", "    int x;") is True

    def test_actual_content_difference(self) -> None:
        assert is_whitespace_only_conflict("int x = 1;", "int x = 2;") is False

    def test_both_empty(self) -> None:
        assert is_whitespace_only_conflict("", "") is True

    def test_whitespace_vs_empty(self) -> None:
        assert is_whitespace_only_conflict("   ", "") is True

    def test_different_code(self) -> None:
        assert is_whitespace_only_conflict("foo()", "bar()") is False



from hypothesis import given, settings
from hypothesis import strategies as st


class TestBuildBranchNameProperty:


    @given(
        pr_number=st.integers(min_value=1, max_value=10**9),
        target_branch=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "P")),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_branch_name_matches_convention(
        self, pr_number: int, target_branch: str
    ) -> None:
        """For any positive PR number and non-empty branch name,
        build_branch_name returns 'backport/<pr_number>-to-<target_branch>'."""
        result = build_branch_name(pr_number, target_branch)
        assert result == f"backport/{pr_number}-to-{target_branch}"
        assert result.startswith("backport/")
        assert f"-to-{target_branch}" in result


class TestBuildPrTitleProperty:


    @given(
        source_title=st.text(min_size=1, max_size=200),
        target_branch=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "P")),
            min_size=1,
            max_size=50,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_pr_title_matches_convention(
        self, source_title: str, target_branch: str
    ) -> None:
        """For any non-empty PR title and branch name,
        build_pr_title returns '[Backport <target_branch>] <source_title>'."""
        result = build_pr_title(source_title, target_branch)
        assert result == f"[Backport {target_branch}] {source_title}"
        assert result.startswith(f"[Backport {target_branch}] ")
        assert result.endswith(source_title)


class TestHasConflictMarkersProperty:


    # Real-world git conflict markers are line-anchored: <<<<<<< and >>>>>>> are
    # followed by a ref name (or appear alone), and ======= is on its own line.
    MARKERS = ["<<<<<<< HEAD", "=======", ">>>>>>> source"]

    @given(
        base=st.text(max_size=200),
        marker=st.sampled_from(MARKERS),
        suffix=st.text(max_size=50),
    )
    @settings(max_examples=100, deadline=None)
    def test_detects_injected_markers(
        self, base: str, marker: str, suffix: str
    ) -> None:
        """Strings with an injected line-anchored conflict marker are detected."""
        from hypothesis import assume

        # Filter out base strings that already contain a marker
        assume(not any(m.split()[0] in base for m in self.MARKERS))
        # Inject the marker on its own line as it appears in real conflicts
        content = base + "\n" + marker + "\n" + suffix
        assert has_conflict_markers(content) is True

    @given(
        content=st.text(
            alphabet=st.characters(
                blacklist_characters="<=>",
            ),
            max_size=300,
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_no_false_positives_without_marker_chars(self, content: str) -> None:
        """Strings without '<', '=', '>' characters never trigger detection."""
        assert has_conflict_markers(content) is False


class TestBracesBalancedProperty:


    @given(data=st.data())
    @settings(max_examples=100, deadline=None)
    def test_balanced_braces_accepted(self, data: st.DataObject) -> None:
        """Strings with balanced curly braces (depth never negative) pass validation."""
        # Build a string with guaranteed balanced braces
        depth = data.draw(st.integers(min_value=1, max_value=10))
        filler = st.text(
            alphabet=st.characters(blacklist_characters="{}"), max_size=20
        )
        parts: list[str] = []
        for _ in range(depth):
            parts.append(data.draw(filler))
            parts.append("{")
        for _ in range(depth):
            parts.append(data.draw(filler))
            parts.append("}")
        parts.append(data.draw(filler))
        content = "".join(parts)
        assert braces_balanced(content) is True

    @given(
        content=st.text(
            alphabet=st.characters(blacklist_characters="{}"), max_size=100
        ),
        extra_opens=st.integers(min_value=1, max_value=5),
    )
    @settings(max_examples=100, deadline=None)
    def test_extra_open_braces_rejected(
        self, content: str, extra_opens: int
    ) -> None:
        """Strings with more '{' than '}' are rejected."""
        unbalanced = content + "{" * extra_opens
        assert braces_balanced(unbalanced) is False

    @given(
        content=st.text(
            alphabet=st.characters(blacklist_characters="{}"), max_size=100
        ),
    )
    @settings(max_examples=100, deadline=None)
    def test_closing_before_opening_rejected(self, content: str) -> None:
        """A '}' appearing before any '{' is rejected."""
        unbalanced = "}" + content + "{"
        assert braces_balanced(unbalanced) is False


class TestIsWhitespaceOnlyConflictProperty:


    @given(
        base=st.text(min_size=0, max_size=200),
    )
    @settings(max_examples=100, deadline=None)
    def test_whitespace_variations_detected(self, base: str) -> None:
        """Adding whitespace-only changes to a string is detected as whitespace-only."""
        import re

        # Create a version with whitespace modifications:
        # replace each whitespace char with a different whitespace sequence
        ws_map = {" ": "\t", "\t": "  ", "\n": "\r\n", "\r": "\n"}
        modified = []
        for ch in base:
            if ch in ws_map:
                modified.append(ws_map[ch])
            else:
                modified.append(ch)
        modified_str = "".join(modified)
        # Both should have the same non-whitespace content
        assert is_whitespace_only_conflict(base, modified_str) is True

    @given(
        base=st.text(
            alphabet=st.characters(whitelist_categories=("L", "N", "P")),
            min_size=1,
            max_size=100,
        ),
        insert_char=st.characters(whitelist_categories=("L", "N", "P")),
        insert_pos=st.integers(min_value=0),
    )
    @settings(max_examples=100, deadline=None)
    def test_non_whitespace_differences_detected(
        self, base: str, insert_char: str, insert_pos: int
    ) -> None:
        """Strings with non-whitespace differences return False."""
        from hypothesis import assume

        pos = insert_pos % (len(base) + 1)
        modified = base[:pos] + insert_char + base[pos:]
        # Only test when the non-whitespace content actually differs
        import re

        base_stripped = re.sub(r"\s+", "", base)
        modified_stripped = re.sub(r"\s+", "", modified)
        assume(base_stripped != modified_stripped)
        assert is_whitespace_only_conflict(base, modified) is False


class TestValidateResolvedContent:
    def test_valid_c_file(self):
        assert validate_resolved_content("src/server.c", "int main() { return 0; }") is True

    def test_invalid_c_file(self):
        assert validate_resolved_content("src/server.c", "int main() {") is False

    def test_valid_python_file(self):
        assert validate_resolved_content("scripts/main.py", "x = 1\n") is True

    def test_invalid_python_file(self):
        assert validate_resolved_content("scripts/main.py", "def f(\n") is False

    def test_valid_json_file(self):
        assert validate_resolved_content("config.json", '{"key": "value"}') is True

    def test_invalid_json_file(self):
        assert validate_resolved_content("config.json", "{broken") is False

    def test_valid_yaml_file(self):
        assert validate_resolved_content("ci.yml", "name: CI\non: push\n") is True

    def test_invalid_yaml_file(self):
        assert validate_resolved_content("ci.yml", ":\n  - :\n  [invalid") is False

    def test_unknown_extension_always_valid(self):
        assert validate_resolved_content("README.md", "anything") is True

    def test_header_file_uses_c_validation(self):
        assert validate_resolved_content("src/server.h", "void f() { }") is True
        assert validate_resolved_content("src/server.h", "void f() {") is False

    def test_tcl_brace_validation(self):
        assert validate_resolved_content("tests/unit/foo.tcl", "proc f {} { return ok }\n") is True
        assert validate_resolved_content("tests/unit/foo.tcl", "proc f {} { return ok\n") is False
