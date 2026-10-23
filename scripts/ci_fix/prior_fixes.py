"""Find merged changes that may already fix a failing test.

The first question for any CI failure is whether it is already fixed: on a
release branch (a backport PR) the fix usually exists on the
default branch and only needs porting; on the default branch itself it may
have landed after the failing run. The engine's checkout holds the full
(blobless) history of every branch, so this is a local ``git log`` search,
not a GitHub API one.

Valkey squash-merges, so a fix's commit message carries its PR title and
description. Deflake commits name the test, its file, or the issue they fix
(``Fixes #N``), and a backport keeps the original subject and usually adds
``(cherry picked from commit <sha>)``. Those are the signals used here. The
result is evidence for the fix agent, never a decision: the agent reads the
candidates' diffs and chooses whether to port one.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.ci_fix.models import FailedTest
from scripts.common.git_clone import is_full_sha
from scripts.common.proc import default_branch_ref, git_output, is_ancestor

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 5
# Diffs shown to the agent, and how much of each.
MAX_DIFFS = 3
_DIFF_CHARS = 8_000
_LOG_LIMIT = "200"
# A shorter test name matches too many unrelated commit messages.
_MIN_NAME_CHARS = 10
_PREFIX_CHARS = 40
# Valkey's squash-merge subject ends with its PR number, which makes an
# identical subject on another branch a backport of the same PR.
_PR_SUBJECT_RE = re.compile(r"\(#\d+\)\s*$")
# How much each kind of match says about a commit; a subject with a fix word
# adds 1, and a candidate needs ``_MIN_SCORE``.
_REFERENCES_ISSUE = 5
_NAMES_TEST = 4
_CHANGES_FILE = 2
_NAMES_TEST_START = 2
_MIN_SCORE = 3
_FIX_WORDS_RE = re.compile(r"\b(?:flak\w*|deflake\w*|fix\w*|race|timing|timeout|unstable test)\b", re.IGNORECASE)


@dataclass(frozen=True)
class PriorFix:
    sha: str
    subject: str
    date: str
    reasons: tuple[str, ...]
    score: int
    # Already in the commit the candidate is prepared on: it landed after the
    # failing run, or it was ported here (``ported_as``).
    on_target: bool = False
    ported_as: str = ""


def find_prior_fixes(
    repo_dir: str,
    tests: tuple[FailedTest, ...],
    *,
    tested_sha: str = "",
    issue_number: int = 0,
    paths: tuple[str, ...] = (),
) -> tuple[PriorFix, ...] | None:
    """Best candidates, strongest first; None when no search could run.

    ``paths`` are other repository files the failure names (a compile error's
    source file), for a failure that is not a test's.
    """
    if not tests and not issue_number and not paths:
        return None
    try:
        return _find(repo_dir, tests, tested_sha=tested_sha, issue_number=issue_number, paths=paths)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Searching for an existing fix failed: %s", exc)
        return None


def _find(
    repo_dir: str,
    tests: tuple[FailedTest, ...],
    *,
    tested_sha: str,
    issue_number: int,
    paths: tuple[str, ...],
) -> tuple[PriorFix, ...] | None:
    head = git_output(repo_dir, "rev-parse", "HEAD").strip()
    ranges: list[tuple[str, bool]] = []
    if is_full_sha(tested_sha) and tested_sha.lower() != head.lower() and is_ancestor(repo_dir, tested_sha, head) is True:
        # Landed between the failing run and the commit being fixed.
        ranges.append((f"{tested_sha}..{head}", True))
    default_ref = default_branch_ref(repo_dir)
    if default_ref:
        # On the default branch but not here: what a port would bring.
        ranges.append((f"{head}..{default_ref}", False))
    if not ranges:
        return None

    found: dict[str, PriorFix] = {}
    for revision_range, on_target in ranges:
        for sha, reasons in _matches(repo_dir, revision_range, tests, issue_number, paths).items():
            subject, date = _subject_and_date(repo_dir, sha)
            score = sum(weight for weight, _text in reasons) + (1 if _FIX_WORDS_RE.search(subject) else 0)
            if score < _MIN_SCORE:
                continue
            ported_as = "" if on_target else _ported_as(repo_dir, sha, subject, head)
            found.setdefault(sha, PriorFix(
                sha=sha, subject=subject, date=date, reasons=tuple(sorted(text for _weight, text in reasons)),
                score=score, on_target=on_target or bool(ported_as), ported_as=ported_as,
            ))
    # Strongest first; among equals, the newest.
    ranked = sorted(found.values(), key=lambda fix: fix.date, reverse=True)
    ranked.sort(key=lambda fix: fix.score, reverse=True)
    return tuple(ranked[:MAX_CANDIDATES])


def _matches(
    repo_dir: str,
    revision_range: str,
    tests: tuple[FailedTest, ...],
    issue_number: int,
    paths: tuple[str, ...],
) -> dict[str, set[tuple[int, str]]]:
    """Each matching commit's ``(weight, reason)`` pairs."""
    reasons: dict[str, set[tuple[int, str]]] = {}

    def add(shas: list[str], weight: int, reason: str) -> None:
        for sha in shas:
            reasons.setdefault(sha, set()).add((weight, reason))

    files = dict.fromkeys([test.file for test in tests if test.file] + list(paths))
    for path in files:
        add(_log(repo_dir, revision_range, "--", path), _CHANGES_FILE, f"changes `{path}`")
    for test in tests:
        name = test.name.strip()
        if len(name) < _MIN_NAME_CHARS:
            continue
        whole = _log(repo_dir, revision_range, "--fixed-strings", f"--grep={name}")
        add(whole, _NAMES_TEST, f"names the test `{name}`")
        prefix = _wrapped_prefix(name)
        if prefix:
            started = _log(repo_dir, revision_range, "--fixed-strings", f"--grep={prefix}")
            add([sha for sha in started if sha not in whole], _NAMES_TEST_START,
                f"names the start of the test, `{prefix}`")
    if issue_number > 0:
        add(_log(repo_dir, revision_range, "--extended-regexp", f"--grep=#{issue_number}([^0-9]|$)"),
            _REFERENCES_ISSUE, f"references #{issue_number}")
    return reasons


def _wrapped_prefix(name: str) -> str:
    """The leading words of a long test name, or "".

    A commit message wraps at ~72 columns, so a long test name quoted in it
    is often split across lines, where a whole-name grep cannot match. Many
    tests share a long prefix, so a prefix match is weaker evidence.
    """
    if len(name) <= _PREFIX_CHARS:
        return ""
    prefix = name[:_PREFIX_CHARS].rsplit(" ", 1)[0]
    return prefix if len(prefix) >= _MIN_NAME_CHARS * 2 else ""


# A source file a log names with a line number, as gcc (``replication.c:120:5:
# error``, run inside ``src/``), sanitizers (an absolute path) and valgrind
# (``(replication.c:120)``) print it.
_LOG_PATH_RE = re.compile(r"([\w./-]*[\w-]\.(?:c|h|cc|cpp|tcl)):\d+")
_REPO_ROOTS = ("src/", "tests/", "deps/", "modules/", "utils/")
_MAX_LOG_PATHS = 5


def paths_in_log(repo_dir: str, job_log: str) -> tuple[str, ...]:
    """Files of the checkout that the log names, most-mentioned first."""
    counts: dict[str, int] = {}
    for match in _LOG_PATH_RE.finditer(job_log or ""):
        path = _repo_path(repo_dir, match.group(1))
        if path:
            counts[path] = counts.get(path, 0) + 1
    ranked = sorted(counts, key=lambda path: -counts[path])
    return tuple(ranked[:_MAX_LOG_PATHS])


def _repo_path(repo_dir: str, printed: str) -> str:
    """The checkout path a printed source path means, or "" when it has none."""
    candidates = []
    for root in _REPO_ROOTS:
        at = printed.rfind(root)
        if at == 0 or (at > 0 and printed[at - 1] == "/"):
            candidates.append(printed[at:])
    if "/" not in printed:
        # The server builds inside src/, so gcc and valgrind print bare names.
        candidates.append(f"src/{printed}")
    return next((path for path in candidates if _inside(repo_dir, path)), "")


def _inside(repo_dir: str, path: str) -> bool:
    """Whether ``path`` is a regular file of the checkout, reached without ".."."""
    parts = Path(path).parts
    if Path(path).is_absolute() or ".." in parts:
        return False
    target = Path(repo_dir, path)
    return target.is_file() and not target.is_symlink()


def _log(repo_dir: str, revision_range: str, *args: str) -> list[str]:
    pathspec: tuple[str, ...] = ()
    if "--" in args:
        split = args.index("--")
        args, pathspec = args[:split], args[split:]
    output = git_output(
        repo_dir, "log", "--no-merges", "--format=%H", f"--max-count={_LOG_LIMIT}",
        *args, revision_range, *pathspec,
    )
    return [line.strip() for line in output.splitlines() if is_full_sha(line.strip())]


def _subject_and_date(repo_dir: str, sha: str) -> tuple[str, str]:
    subject, _, date = git_output(repo_dir, "show", "-s", "--format=%s%x00%cs", sha).strip().partition("\x00")
    return subject, date


def _ported_as(repo_dir: str, sha: str, subject: str, head: str) -> str:
    """The commit on ``head`` that already carries ``sha``'s change, or "".

    A ``cherry-pick -x`` trailer names the commit. Otherwise an identical
    subject counts only when it carries a PR number: a generic subject such
    as "Fix flaky test" is not evidence of anything.
    """
    searches = [("--fixed-strings", f"--grep=cherry picked from commit {sha}")]
    if _PR_SUBJECT_RE.search(subject):
        searches.append(("--fixed-strings", f"--grep={subject}"))
    for args in searches:
        output = git_output(repo_dir, "log", "--format=%H%x00%s", "--max-count=20", *args, head)
        for line in output.splitlines():
            commit, _, found_subject = line.partition("\x00")
            if args[1].startswith("--grep=cherry") or found_subject == subject:
                return commit
    return ""


def render_prior_fixes(repo_dir: str, fixes: tuple[PriorFix, ...]) -> str:
    """Markdown for the agent: every candidate, and the diffs of the best ones."""
    if not fixes:
        return ""
    lines = [
        "# Merged changes that may already fix this failure",
        "",
        "Found by searching history for the failing test's name, its file, and",
        "the issue number. They are leads, not conclusions: read each diff and",
        "decide whether it addresses the root cause in the job log.",
        "",
    ]
    for fix in fixes:
        if fix.ported_as:
            where = f"already on the working commit as `{fix.ported_as[:12]}`"
        elif fix.on_target:
            where = "already on the working commit (landed after the failing run)"
        else:
            where = "on the default branch, NOT on the working commit (a port candidate)"
        lines.append(f"- `{fix.sha}` {fix.date} {fix.subject}")
        lines.append(f"  - {where}; {'; '.join(fix.reasons)}")
    shown = 0
    for fix in fixes:
        if fix.on_target or shown >= MAX_DIFFS:
            continue
        shown += 1
        try:
            diff = git_output(repo_dir, "show", "--no-ext-diff", "--no-textconv", "--stat", "--patch", fix.sha)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            diff = f"(could not show the commit: {exc})"
        if len(diff) > _DIFF_CHARS:
            diff = diff[:_DIFF_CHARS] + "\n[... diff truncated ...]"
        lines += ["", f"## `{fix.sha[:12]}` {fix.subject}", "", "```diff", diff, "```"]
    return "\n".join(lines) + "\n"


def summarize_prior_fixes(fixes: tuple[PriorFix, ...]) -> str:
    """One line per candidate, for the prompt and the maintainer report."""
    lines = []
    for fix in fixes:
        state = (
            f"already ported as {fix.ported_as[:12]}" if fix.ported_as
            else "already on the working commit" if fix.on_target
            else "port candidate"
        )
        lines.append(f"`{fix.sha[:12]}` {fix.subject} ({state})")
    return "\n".join(lines)
