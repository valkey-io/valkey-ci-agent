"""Find existing default-branch fixes that may be missing from a backport.

This is deliberately heuristic but code-owned: before the diagnosis agent
concludes that a prerequisite is missing, we gather nearby commits from the
default branch and put them in front of the model as concrete candidates. The
model still decides whether a candidate actually matches the root cause; code
later verifies a PORT by requiring a clean cherry-pick.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.common.proc import git_output, run_git

logger = logging.getLogger(__name__)

DEFAULT_BRANCH = "unstable"
_MAX_LOG_BYTES = 512 * 1024
_MAX_TERMS = 16
_MAX_CANDIDATES = 8

_PATH_RE = re.compile(r"(?:[A-Za-z0-9_.@+-]+/)+[A-Za-z0-9_.@+-]+")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{4,}")
_NOISY_TOKENS = frozenset({
    "error", "failed", "failure", "fatal", "warning", "github", "actions",
    "runner", "command", "output", "verbose", "install", "python", "branch",
})


@dataclass(frozen=True)
class PortCandidate:
    """A default-branch commit that may be the missing backport fix."""

    sha: str
    subject: str
    paths: tuple[str, ...] = ()


def discover_port_candidates(
    repo_dir: str,
    logs_dir: str,
    *,
    default_branch: str = DEFAULT_BRANCH,
    max_candidates: int = _MAX_CANDIDATES,
) -> tuple[PortCandidate, ...]:
    """Return likely upstream-fix candidates from ``default_branch``.

    Candidates come from two deterministic signals extracted from the failing
    logs: repository paths mentioned in the output, and distinctive words in
    the failure text. Path history is preferred, then subject/body grep fills in
    cases where a validator failure names a subsystem rather than a source file.
    Any git failure yields no candidates; diagnosis can still proceed normally.
    """
    try:
        ref = _ensure_default_ref(repo_dir, default_branch)
        base = git_output(repo_dir, "merge-base", "HEAD", ref).strip()
    except (subprocess.CalledProcessError, OSError) as exc:
        logger.info("Could not prepare default-branch candidate search: %s", exc)
        return ()

    log_text = _read_logs(logs_dir)
    paths = _extract_repo_paths(repo_dir, log_text)
    terms = _distinctive_terms(log_text)

    candidates: dict[str, PortCandidate] = {}
    for candidate in _path_candidates(repo_dir, base, ref, paths):
        candidates.setdefault(candidate.sha, candidate)
        if len(candidates) >= max_candidates:
            return tuple(candidates.values())
    for candidate in _grep_candidates(repo_dir, base, ref, terms):
        candidates.setdefault(candidate.sha, candidate)
        if len(candidates) >= max_candidates:
            break
    return tuple(candidates.values())


def format_port_candidates(candidates: tuple[PortCandidate, ...]) -> str:
    """Render candidates for the diagnosis prompt."""
    if not candidates:
        return ""
    lines = [
        "## Default-branch candidate fixes (code-discovered)",
        "These commits are from the default branch after this release branch "
        "diverged. Prefer `path: \"port\"` only if one clearly fixes the "
        "same root cause and can be cherry-picked cleanly.",
    ]
    for c in candidates:
        paths = f" [{', '.join(c.paths[:4])}]" if c.paths else ""
        lines.append(f"- {c.sha[:12]} {c.subject}{paths}")
    return "\n".join(lines) + "\n"


def _ensure_default_ref(repo_dir: str, default_branch: str) -> str:
    ref = f"origin/{default_branch}"
    try:
        git_output(repo_dir, "rev-parse", "--verify", ref)
        return ref
    except subprocess.CalledProcessError:
        run_git(
            repo_dir, "fetch", "origin",
            f"refs/heads/{default_branch}:refs/remotes/origin/{default_branch}",
        )
        return ref


def _read_logs(logs_dir: str) -> str:
    root = Path(logs_dir)
    chunks: list[str] = []
    remaining = _MAX_LOG_BYTES
    for path in sorted(p for p in root.iterdir() if p.is_file()):
        if remaining <= 0:
            break
        data = path.read_bytes()[:remaining]
        chunks.append(data.decode("utf-8", errors="replace"))
        remaining -= len(data)
    return "\n".join(chunks)


def _extract_repo_paths(repo_dir: str, text: str) -> tuple[str, ...]:
    root = Path(repo_dir)
    found: set[str] = set()
    for match in _PATH_RE.findall(text):
        path = match.strip("`'\".,:;()[]{}")
        if path and (root / path).exists():
            found.add(path)
    return tuple(sorted(found))


def _distinctive_terms(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    terms: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        term = raw.lower().replace("-", "")
        if term in _NOISY_TOKENS or term in seen:
            continue
        seen.add(term)
        terms.append(raw)
        if len(terms) >= _MAX_TERMS:
            break
    return tuple(terms)


def _path_candidates(
    repo_dir: str, base: str, ref: str, paths: tuple[str, ...],
) -> tuple[PortCandidate, ...]:
    if not paths:
        return ()
    try:
        out = git_output(
            repo_dir, "log", "--format=%H%x00%s", f"{base}..{ref}", "--", *paths,
        )
    except subprocess.CalledProcessError:
        return ()
    return tuple(_candidate_from_line(repo_dir, line) for line in out.splitlines() if line)


def _grep_candidates(
    repo_dir: str, base: str, ref: str, terms: tuple[str, ...],
) -> tuple[PortCandidate, ...]:
    candidates: list[PortCandidate] = []
    for term in terms:
        try:
            out = git_output(
                repo_dir, "log", "--format=%H%x00%s", "--regexp-ignore-case",
                f"--grep={term}", f"{base}..{ref}",
            )
        except subprocess.CalledProcessError:
            continue
        candidates.extend(_candidate_from_line(repo_dir, line) for line in out.splitlines() if line)
    return tuple(candidates)


def _candidate_from_line(repo_dir: str, line: str) -> PortCandidate:
    sha, _, subject = line.partition("\0")
    return PortCandidate(sha=sha, subject=subject, paths=_changed_paths(repo_dir, sha))


def _changed_paths(repo_dir: str, sha: str) -> tuple[str, ...]:
    try:
        out = git_output(repo_dir, "diff-tree", "--no-commit-id", "--name-only", "-r", sha)
    except subprocess.CalledProcessError:
        return ()
    return tuple(path for path in out.splitlines() if path)
