"""Deterministic per-branch evidence for backport eligibility.

Blame says a release branch carries *some* line a PR touches. That is not the
same as the branch carrying the defect: a fix to a recent feature can touch an
old line, and a test fix can touch an old harness line while the test itself
never reached the branch. These checks ask each candidate branch directly,
without a model:

* do the files the PR modifies exist there;
* does the PR's patch apply there as is; and
* do the identifiers the PR is about (functions it edits, configs and test
  names it or its linked issues name) exist there.

A branch carrying none of the identifiers the PR is about is dropped before
the model sees it. The rest is evidence for the model to weigh.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Iterable, Sequence

from scripts.common.proc import PROCESS_BASICS, filter_env, git_output, run_git

# Docs paths. A PR touching only these is never suggested for backport.
DOC_PATTERNS = (
    "*.md",
    "docs/*",
)
# Paths whose changes never ship to users: tests, test harness, CI, and docs.
# A release branch can still need them to keep its own CI working, so such a
# PR is judged, but never commented on.
NON_PRODUCT_PATTERNS = (
    "tests/*",
    "src/unit/*",
    ".github/*",
    "ci/*",
    *DOC_PATTERNS,
)

# Bounds on what is extracted and asked of each branch.
_MAX_IDENTIFIERS = 25
_MIN_IDENTIFIER_LEN = 5
_MAX_QUOTED_LEN = 80

# "@@ -12,7 +12,9 @@ static int foo(...)" - the enclosing function.
_HUNK_FUNCTION_RE = re.compile(r"^@@ [^@]* @@.*?\b([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_BACKTICK_RE = re.compile(r"`([^`\n]{3,%d})`" % _MAX_QUOTED_LEN)
_QUOTED_RE = re.compile(r'"([^"\n]{6,%d})"' % _MAX_QUOTED_LEN)
# snake_case, kebab-case (configs), and camelCase/PascalCase identifiers.
_TOKEN_RE = re.compile(
    r"\b(?:[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+|[a-z]+[A-Z]\w+|[A-Z][a-z0-9]+[A-Z]\w*)\b"
)
# Names too generic to say anything about a branch.
_GENERIC = frozenset({
    "valkey-server", "valkey-cli", "redis-server", "redis-cli", "io-threads",
    "unstable", "backport", "release-notes",
})


def is_non_product_change(paths: Iterable[str]) -> bool:
    """True when every changed path is a test, harness, CI, or doc file."""
    return _all_match(paths, NON_PRODUCT_PATTERNS)


def is_docs_only_change(paths: Iterable[str]) -> bool:
    """True when every changed path is a doc file."""
    return _all_match(paths, DOC_PATTERNS)


def _all_match(paths: Iterable[str], patterns: Sequence[str]) -> bool:
    paths = list(paths)
    return bool(paths) and all(
        any(fnmatch.fnmatch(path, pattern) for pattern in patterns) for path in paths
    )


def changed_paths(repo_dir: str, base: str, sha: str) -> list[str]:
    """Every path ``base..sha`` adds, modifies, or deletes (renames as both)."""
    out = git_output(repo_dir, "diff", "--name-only", "--no-renames", base, sha, "--")
    return [line for line in out.splitlines() if line]


@dataclass(frozen=True)
class BranchEvidence:
    """What one release branch carries of a PR's change."""

    branch: str
    files_present: tuple[str, ...]
    files_missing: tuple[str, ...]
    # None when the patch could not be checked (for example a binary diff).
    applies_cleanly: bool | None
    identifiers_present: tuple[str, ...]
    identifiers_missing: tuple[str, ...]

    @property
    def carries_subject(self) -> bool:
        """False only on positive evidence the branch lacks everything the PR is about."""
        return bool(self.identifiers_present) or not self.identifiers_missing

    def as_payload(self) -> dict[str, object]:
        return {
            "modified_files_present": list(self.files_present),
            "modified_files_missing": list(self.files_missing),
            "patch_applies_cleanly": self.applies_cleanly,
            "identifiers_present": list(self.identifiers_present),
            "identifiers_missing": list(self.identifiers_missing),
        }


def subject_identifiers(
    repo_dir: str, base: str, sha: str, texts: Sequence[str],
) -> tuple[str, ...]:
    """Return the identifiers a PR is about, as they exist on the development branch.

    Functions the diff edits come from its hunk headers. Configs, test names,
    and code identifiers come from the PR's title, body, and linked issues,
    kept only when the development branch at *base* (or the PR itself) has
    them, which filters out prose.
    """
    diff = git_output(repo_dir, "diff", "--no-ext-diff", "--unified=0", base, sha, "--")
    functions = _ordered_unique(_HUNK_FUNCTION_RE.findall(diff))
    candidates = _ordered_unique(
        item
        for text in texts
        for item in (
            *_BACKTICK_RE.findall(text or ""),
            *_QUOTED_RE.findall(text or ""),
            *_TOKEN_RE.findall(text or ""),
        )
    )
    candidates = [
        c.strip() for c in candidates
        if len(c.strip()) >= _MIN_IDENTIFIER_LEN and c.strip().lower() not in _GENERIC and "\n" not in c
    ]
    known = _present(repo_dir, base, candidates) | _present(repo_dir, sha, candidates)
    named = [c for c in candidates if c in known]
    return tuple(_ordered_unique([*named, *functions]))[:_MAX_IDENTIFIERS]


def branch_evidence(
    repo_dir: str,
    branch_ref: str,
    branch: str,
    *,
    base: str,
    sha: str,
    identifiers: Sequence[str],
) -> BranchEvidence:
    """Ask *branch_ref* whether it carries the files, patch, and identifiers of ``base..sha``."""
    modified = git_output(
        repo_dir, "diff", "--name-only", "--no-renames", "--diff-filter=MD", base, sha, "--",
    ).split("\n")
    modified = [path for path in modified if path]
    present_files = set()
    if modified:
        listed = git_output(repo_dir, "ls-tree", "-r", "--name-only", branch_ref, "--", *modified)
        present_files = set(listed.splitlines())
    found = _present(repo_dir, branch_ref, identifiers)
    return BranchEvidence(
        branch=branch,
        files_present=tuple(p for p in modified if p in present_files),
        files_missing=tuple(p for p in modified if p not in present_files),
        applies_cleanly=_applies(repo_dir, branch_ref, base, sha),
        identifiers_present=tuple(i for i in identifiers if i in found),
        identifiers_missing=tuple(i for i in identifiers if i not in found),
    )


def _applies(repo_dir: str, branch_ref: str, base: str, sha: str) -> bool | None:
    """Whether the patch applies to *branch_ref*'s tree, checked in a scratch index."""
    try:
        patch = git_output(repo_dir, "diff", "--no-ext-diff", "--binary", "--full-index", base, sha, "--")
    except subprocess.SubprocessError:
        return None
    if not patch.strip():
        return None
    with tempfile.TemporaryDirectory(prefix="eligibility-index-") as tmp:
        env = {**filter_env(PROCESS_BASICS), "GIT_INDEX_FILE": os.path.join(tmp, "index")}
        try:
            run_git(repo_dir, "read-tree", branch_ref, env=env)
        except subprocess.SubprocessError:
            return None
        try:
            run_git(repo_dir, "apply", "--cached", "--check", "-", env=env, input=patch)
        except subprocess.CalledProcessError:
            return False
        except subprocess.SubprocessError:
            return None
    return True


def _present(repo_dir: str, rev: str, needles: Sequence[str]) -> set[str]:
    """The *needles* that occur anywhere in *rev*'s tree, from one ``git grep``."""
    needles = [n for n in needles if n]
    if not needles:
        return set()
    args = [arg for needle in needles for arg in ("-e", needle)]
    try:
        out = git_output(repo_dir, "grep", "-I", "-F", "-o", "-h", *args, rev, "--")
    except subprocess.CalledProcessError as exc:
        if exc.returncode == 1:
            return set()  # git grep exits 1 when nothing matches
        raise
    return set(out.splitlines()) & set(needles)


def _ordered_unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


_MAX_SITE_CHARS = 120
# A site shorter than this, or with no identifier, matches nearly any tree
# ("}", "0", "->"), so it proves nothing about a branch.
_MIN_SITE_CHARS = 4
# Files compared whitespace-insensitively when the site is searched tree-wide.
_MAX_SITE_FILES = 20
_SITE_WORD_RE = re.compile(r"[A-Za-z_]\w{2,}")


def site_present(repo_dir: str, branch_ref: str, path: str, symbol: str) -> bool | None:
    """Whether *symbol* occurs on *branch_ref*, in *path* when the branch has that file.

    Whitespace is ignored: older branches predate the clang-format reformat,
    so 7.2 spells ``strchr(c->querybuf + c->qb_pos, '\\n')`` without spaces.
    *symbol* is model output read from untrusted PR text, so it is only ever
    a fixed-string ``git grep`` pattern or a plain substring. Returns None
    when the site cannot be checked: empty, too short to mean anything,
    without an identifier, multi-line, or overlong, or git could not answer.
    """
    symbol = symbol.strip()
    if (
        "\n" in symbol
        or len(symbol) > _MAX_SITE_CHARS
        or len("".join(symbol.split())) < _MIN_SITE_CHARS
        or not _SITE_WORD_RE.search(symbol)
    ):
        return None
    paths: list[str] = []
    try:
        if path and git_output(repo_dir, "ls-tree", "--name-only", branch_ref, "--", path).strip():
            paths = [path]  # a renamed or moved file falls back to the whole tree
        try:
            git_output(repo_dir, "grep", "-q", "-I", "-F", "-e", symbol, branch_ref, "--", *paths)
            return True
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 1:
                return None
        return _present_ignoring_whitespace(repo_dir, branch_ref, symbol, paths)
    except subprocess.CalledProcessError as exc:
        return False if exc.returncode == 1 else None
    except subprocess.SubprocessError:
        return None


def _present_ignoring_whitespace(repo_dir: str, branch_ref: str, symbol: str, paths: list[str]) -> bool:
    """Whether *symbol*, whitespace removed, occurs in *paths* with theirs removed.

    Without *paths*, the files to compare are those holding the site's longest
    word. Raises ``CalledProcessError`` with returncode 1 when none does.
    """
    squashed = "".join(symbol.split())
    if not paths:
        words = _SITE_WORD_RE.findall(symbol)
        if not words:
            return False
        out = git_output(repo_dir, "grep", "-l", "-I", "-F", "-e", max(words, key=len), branch_ref, "--")
        paths = [line.partition(":")[2] for line in out.splitlines()][:_MAX_SITE_FILES]
    return any(
        squashed in "".join(git_output(repo_dir, "show", f"{branch_ref}:{p}").split())
        for p in paths
    )
