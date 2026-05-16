"""Naming conventions, conflict detection, and validation helpers."""

from __future__ import annotations

import ast
import json
import re
from pathlib import PurePosixPath

import yaml  # type: ignore[import-untyped]

_CONFLICT_MARKERS = re.compile(
    r"^(<{7} \S|={7}$|>{7} \S|<{7}$|>{7}$)",
    re.MULTILINE,
)


def build_branch_name(source_pr_number: int, target_branch: str) -> str:
    return f"backport/{source_pr_number}-to-{target_branch}"


def build_pr_title(source_pr_title: str, target_branch: str) -> str:
    return f"[Backport {target_branch}] {source_pr_title}"


def has_conflict_markers(content: str) -> bool:
    """Check whether *content* contains git conflict markers.

    Returns ``True`` if any of ``<<<<<<<``, ``=======``, or ``>>>>>>>``
    (seven characters each) appear anywhere in the string.

    """
    return bool(_CONFLICT_MARKERS.search(content))


def braces_balanced(content: str) -> bool:
    """Check whether curly braces are balanced and never go negative.

    This is a conservative signal that resolved C/C++ code isn't obviously
    broken (mid-block truncation, missing outer braces). It does NOT
    actually parse C — unbalanced braces inside string literals or
    block comments will false-positive/negative. The real validation is
    `make -j$(nproc)` run by Claude Code during resolution; this function
    is a cheap last-mile sanity check.

    Returns ``True`` when the number of ``{`` equals the number of ``}``
    and the brace depth never goes negative (i.e. no ``}`` before its
    matching ``{``).

    """
    depth = 0
    for ch in content:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0



def validate_resolved_content(path: str, content: str) -> bool:
    return validate_resolved_content_detail(path, content)[0]


def validate_resolved_content_detail(path: str, content: str) -> tuple[bool, str]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hxx"}:
        if not braces_balanced(content):
            return False, "C/C++ brace balance check failed"
        return True, ""
    if suffix == ".py":
        try:
            ast.parse(content)
        except SyntaxError as exc:
            return False, f"Python syntax error: {exc}"
        return True, ""
    if suffix == ".json":
        try:
            json.loads(content)
        except json.JSONDecodeError as exc:
            return False, f"JSON parse error: {exc}"
        return True, ""
    if suffix in {".yml", ".yaml"}:
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as exc:
            return False, f"YAML parse error: {exc}"
        return True, ""
    if suffix == ".tcl":
        return _validate_tcl(content)
    return True, ""


def _validate_tcl(content: str) -> tuple[bool, str]:
    depth = 0
    for index, ch in enumerate(content):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                line = content[:index].count("\n") + 1
                return False, f"Tcl brace error: unexpected closing brace at line {line}"
    if depth:
        return False, f"Tcl brace error: {depth} unclosed opening brace(s)"
    return True, ""


def is_whitespace_only_conflict(target_content: str, source_content: str) -> bool:
    """Return ``True`` when *target_content* and *source_content* differ only in whitespace.

    Whitespace differences include spaces, tabs, indentation, trailing
    whitespace, and line endings.  The comparison strips all whitespace
    from both strings before checking equality.

    """
    return _strip_all_whitespace(target_content) == _strip_all_whitespace(source_content)


def _strip_all_whitespace(s: str) -> str:
    return re.sub(r"\s+", "", s)
