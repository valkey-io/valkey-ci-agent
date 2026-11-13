"""A sweep PR's CI-sweep PR: the bot's fixes for its CI, kept on its head.

The sweep PR carries only backports. Each CI fix the follow-up makes for it
goes to ``agent/backport/ci-sweep/<release>``, a pull request into the sweep
branch, as a commit with ``CI-Fix-Job`` and ``CI-Fix-Run`` trailers. The
sweep rebases its branch onto the release branch as it adds backports, so
the CI-sweep branch is rebuilt on each new sweep head: its fixes are
cherry-picked onto it, and a fix that comes out empty (the sweep has it now)
or conflicts with the sweep is dropped. A fix whose files a new backport
changes is also reviewed, and dropped when the backport made it unneeded.

The rebuild is planned while the sweep is prepared, with no write token and
with the agent available, and replayed with git when the sweep is published.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Callable

from scripts.backport.sweep_git import BRANCH_PREFIX as SWEEP_PREFIX
from scripts.backport.sweep_reporting import _esc
from scripts.common.proc import git_output, run_git

logger = logging.getLogger(__name__)

BRANCH_PREFIX = "agent/backport/ci-sweep"
JOB_TRAILER = "CI-Fix-Job"
RUN_TRAILER = "CI-Fix-Run"
ADAPTS_TRAILER = "CI-Fix-Adapts"
# In the CI-sweep PR's description: the sweep PR its fixes are for.
_SWEEP_MARKER = "<!-- valkey-ci-agent:ci-sweep sweep={number} -->"
_SWEEP_MARKER_RE = re.compile(r"<!-- valkey-ci-agent:ci-sweep sweep=(\d+) -->")
# Said once on a CI-sweep PR someone else pushed to.
FOREIGN_MARKER = "<!-- valkey-ci-agent:ci-sweep-foreign -->"

# Where the sweep's preparation fetches the open CI-sweep branch, for its
# publication to rebuild from.
PLANNED_REF = "refs/ci-sweep/planned"

# At most this much of the new backports' diffs goes to the still-needed review.
_REVIEW_DIFF_CHARS = 20_000

DROPPED_ON_SWEEP = "the sweep has the same change"
DROPPED_CONFLICT = "it conflicts with the sweep"

# Whether a fix is still needed after new backports changed its files:
# (fix, the new backports' diffs to those files) -> (needed, why).
Review = Callable[["Fix", str], tuple[bool, str]]


def ci_sweep_branch(release: str) -> str:
    """The branch of the PR that collects the CI fixes of ``release``'s sweep PR."""
    return f"{BRANCH_PREFIX}/{release}"


def fix_branch_for(branch: str, release: str) -> str:
    """Where a fix for ``branch``, a PR into ``release``, goes instead of onto it: the CI-sweep branch of a sweep."""
    return ci_sweep_branch(release) if branch == f"{SWEEP_PREFIX}/{release}" else ""


def sweep_marker(number: int) -> str:
    return _SWEEP_MARKER.format(number=number)


def sweep_of(body: str) -> int:
    """The sweep PR a CI-sweep PR's description says its fixes are for, or 0."""
    match = _SWEEP_MARKER_RE.search(body or "")
    return int(match.group(1)) if match else 0


def trailers(job: str, run_url: str, adapts: str = "") -> tuple[str, ...]:
    """The trailers a fix's commit carries, by which the rebuild and the follow-up know it."""
    pairs = ((JOB_TRAILER, job), (RUN_TRAILER, run_url), (ADAPTS_TRAILER, adapts))
    return tuple(f"{key}: {value}" for key, value in pairs if value)


@dataclass(frozen=True)
class Fix:
    sha: str
    subject: str
    job: str
    run_url: str = ""
    adapts: str = ""


def as_fix(sha: str, message: str) -> Fix | None:
    """The fix a commit is, from its trailers, or None for any other commit."""
    found = {}
    for line in message.splitlines():
        key, _, value = line.partition(":")
        if key.strip() in (JOB_TRAILER, RUN_TRAILER, ADAPTS_TRAILER) and value.strip():
            found[key.strip()] = value.strip()
    if JOB_TRAILER not in found:
        return None
    subject = message.splitlines()[0] if message.strip() else sha[:12]
    return Fix(sha, subject, found[JOB_TRAILER], found.get(RUN_TRAILER, ""), found.get(ADAPTS_TRAILER, ""))


def commit_fixes(commits: Any) -> list[Fix]:
    """The fixes among GitHub ``commits``, in order."""
    fixes = (as_fix(str(commit.sha), str(commit.commit.message)) for commit in commits)
    return [fix for fix in fixes if fix is not None]


def pr_fixes(pull: Any) -> list[Fix]:
    """The fixes on an open CI-sweep PR, from its commits, in order."""
    return commit_fixes(pull.get_commits())


@dataclass(frozen=True)
class Plan:
    """What the CI-sweep PR becomes on the sweep's new head."""

    pr_number: int
    head: str
    # The fixes kept, in order, by their commits on the old head.
    keep: tuple[str, ...] = ()
    # (commit, subject, job, why) of each fix dropped.
    dropped: tuple[tuple[str, str, str, str], ...] = ()
    # Why the PR is to be closed instead, when it is.
    close: str = ""
    # The branch has commits that are not the bot's fixes: it is left alone.
    foreign: bool = False


def fixes_on(repo_dir: str, sweep: str, ci_sweep: str) -> tuple[list[Fix], list[Fix], bool]:
    """The fixes ``ci_sweep`` adds to ``sweep``, those ``sweep`` already has, and whether it has others.

    A CI-sweep branch is an old sweep head with the fixes on top. The commits
    below its first fix are that old sweep's, however the rebase changed their
    patches (a moved context line, a resolved conflict); a commit above it
    that is not a fix is someone else's. ``git cherry`` says which fixes the
    sweep now has, by their patch.
    """
    adds: list[Fix] = []
    has: list[Fix] = []
    foreign = False
    for line in git_output(repo_dir, "cherry", sweep, ci_sweep).splitlines():
        sign, _, sha = line.partition(" ")
        sha = sha.strip()
        if len(git_output(repo_dir, "show", "-s", "--format=%P", sha).split()) > 1:
            continue
        fix = as_fix(sha, git_output(repo_dir, "show", "-s", "--format=%B", sha))
        if fix is None:
            foreign = foreign or bool(adds or has)
            continue
        (adds if sign == "+" else has).append(fix)
    return adds, has, foreign


def rebuild(repo_dir: str, onto: str, fixes: list[Fix]) -> tuple[str, list[Fix], list[tuple[Fix, str]]]:
    """Cherry-pick ``fixes`` onto ``onto``, detached: the new head, the fixes kept, and those dropped, with why."""
    run_git(repo_dir, "checkout", "-q", "--detach", onto)
    kept: list[Fix] = []
    dropped: list[tuple[Fix, str]] = []
    for fix in fixes:
        parent = git_output(repo_dir, "rev-parse", f"{fix.sha}~1").strip()
        if parent == git_output(repo_dir, "rev-parse", "HEAD").strip():
            # Nothing under it changed: the commit stays, and so does what its comments call it.
            run_git(repo_dir, "checkout", "-q", "--detach", fix.sha)
            kept.append(fix)
            continue
        try:
            run_git(repo_dir, "cherry-pick", "--allow-empty", "--keep-redundant-commits", fix.sha)
        except subprocess.CalledProcessError:
            run_git(repo_dir, "cherry-pick", "--abort")
            dropped.append((fix, DROPPED_CONFLICT))
            continue
        if not git_output(repo_dir, "diff", "--name-only", "HEAD~1", "HEAD").strip():
            run_git(repo_dir, "reset", "-q", "--hard", "HEAD~1")
            dropped.append((fix, DROPPED_ON_SWEEP))
            continue
        kept.append(fix)
    return git_output(repo_dir, "rev-parse", "HEAD").strip(), kept, dropped


def plan(
    repo_dir: str,
    *,
    pull: Any,
    ci_sweep: str,
    sweep_head: str,
    sweep_pr: int | None,
    review: Review | None = None,
) -> Plan:
    """How the open CI-sweep PR ``pull``, at ``ci_sweep`` in ``repo_dir``, goes onto ``sweep_head``.

    Run where the agent may run and no write token is held. ``sweep_pr`` is
    the sweep's open pull request, if it has one: a CI-sweep PR for an
    earlier one, since merged or closed, is closed.
    """
    number, head = int(pull.number), str(pull.head.sha)
    opened_for = sweep_of(str(getattr(pull, "body", "") or ""))
    if not sweep_pr or opened_for != sweep_pr:
        return Plan(number, head, close=f"#{opened_for} was merged or closed without it" if opened_for
                    else "its sweep PR was merged or closed without it")
    adds, has, foreign = fixes_on(repo_dir, sweep_head, ci_sweep)
    if foreign:
        return Plan(number, head, foreign=True)
    dropped = [(fix, DROPPED_ON_SWEEP) for fix in has]
    with tempfile.TemporaryDirectory(prefix="ci-sweep-") as worktree:
        run_git(repo_dir, "worktree", "add", "-q", "--detach", worktree, sweep_head)
        try:
            _head, kept, lost = rebuild(worktree, sweep_head, adds)
            dropped += lost
            if review is not None and kept:
                kept, unneeded = _review(worktree, ci_sweep, sweep_head, kept, review)
                dropped += unneeded
        finally:
            run_git(repo_dir, "worktree", "remove", "--force", worktree)
    return Plan(
        number, head,
        keep=tuple(fix.sha for fix in kept),
        dropped=tuple((fix.sha, fix.subject, fix.job, why) for fix, why in dropped),
    )


def _review(
    repo_dir: str, ci_sweep: str, sweep_head: str, kept: list[Fix], review: Review,
) -> tuple[list[Fix], list[tuple[Fix, str]]]:
    """Ask, of each fix whose files the sweep's new backports change, whether it is still needed.

    A sweep commit is new unless the CI-sweep branch already has the pull
    request it backports: the sweep's rebase gives the old ones new commits,
    whose patches may differ.
    """
    had = set(_source_prs(repo_dir, f"{sweep_head}..{ci_sweep}").values()) - {""}
    backports = [
        sha for sha, pr in _source_prs(repo_dir, f"{ci_sweep}..{sweep_head}").items() if not pr or pr not in had
    ]
    still, unneeded = [], []
    for fix in kept:
        files = git_output(repo_dir, "show", "--name-only", "--format=", fix.sha).split()
        new = "".join(
            git_output(repo_dir, "show", "--no-ext-diff", "--format=%n### %h %s%n", sha, "--", *files)
            for sha in backports
            if files and git_output(repo_dir, "diff", "--name-only", f"{sha}~1", sha, "--", *files).strip()
        )
        if len(new) > _REVIEW_DIFF_CHARS:
            new = new[:_REVIEW_DIFF_CHARS] + "\n[... diff truncated ...]"
        if not new.strip():
            still.append(fix)
            continue
        try:
            needed, why = review(fix, new)
        except Exception as exc:  # noqa: BLE001 - a fix the review could not judge is kept
            logger.warning("Could not review %s against the new backports: %s", fix.sha[:12], exc)
            needed, why = True, ""
        if needed:
            still.append(fix)
        else:
            unneeded.append((fix, why or "a new backport made it unneeded"))
    return still, unneeded


def _source_prs(repo_dir: str, revisions: str) -> dict[str, str]:
    """Each commit in ``revisions``, oldest first, and the pull request its Backport-Source-PR trailer names."""
    output = git_output(
        repo_dir, "log", "--reverse", "--no-merges",
        "--format=%H %(trailers:key=Backport-Source-PR,valueonly,separator=%x2C)", revisions,
    )
    return {sha: pr.strip() for sha, _, pr in (line.partition(" ") for line in output.splitlines() if line)}


def replay(repo_dir: str, sweep_head: str, ci_sweep: str, keep: tuple[str, ...]) -> str:
    """The planned CI-sweep head: ``keep``, from ``ci_sweep``, cherry-picked onto ``sweep_head``."""
    adds, _has, _foreign = fixes_on(repo_dir, sweep_head, ci_sweep)
    by_sha = {fix.sha: fix for fix in adds}
    head, kept, dropped = rebuild(repo_dir, sweep_head, [by_sha[sha] for sha in keep if sha in by_sha])
    if dropped or len(kept) != len(keep):
        raise RuntimeError("the CI-sweep branch no longer rebuilds as planned")
    return head


def body(release: str, sweep_pr: int, fixes: list[Fix]) -> str:
    """The CI-sweep PR's description, laid out like the sweep PR's, with a row a fix."""
    rows = []
    for fix in fixes:
        job = f"`{_esc(fix.job)}`" + (f" ([run]({fix.run_url}))" if fix.run_url else "")
        detail = f"adapts upstream `{fix.adapts[:12]}`" if fix.adapts else ""
        # A bare SHA, which GitHub links to the commit.
        rows.append(f"| {fix.sha} | {job} | {_esc(fix.subject)} | {detail} |")
    return "\n".join([
        f"# CI Fix Sweep for {release}",
        "",
        f"Automated fixes for CI failures on #{sweep_pr}.",
        "",
        "## Applied",
        "",
        "| Commit | Failing job | Title | Detail |",
        "|---|---|---|---|",
        *rows,
        "",
        "Fix details are posted as comments on this PR.",
        "",
        "---",
        "*Generated by valkey-ci-agent using Claude Code.*",
        sweep_marker(sweep_pr),
    ])


_SWEEP_LINE_RE = re.compile(r"\n*CI fixes for this sweep are in #\d+\.\n*")


def sweep_line(number: int) -> str:
    """The sweep PR's line, at the top of its description, naming its CI-sweep PR."""
    return f"CI fixes for this sweep are in #{number}."


def with_sweep_line(text: str, number: int) -> str:
    """``text``, a sweep PR's description, with :func:`sweep_line` above its first section, once."""
    text = _SWEEP_LINE_RE.sub("\n\n", text).strip("\n")
    head, found, tail = text.partition("\n## ")
    if not found:
        return f"{text}\n\n{sweep_line(number)}\n"
    return f"{head.rstrip()}\n\n{sweep_line(number)}\n\n## {tail}"


def dropped_comment(dropped: tuple[tuple[str, str, str, str], ...]) -> str:
    lines = ["Dropped from this PR on the sweep's new head:", ""]
    lines += [f"- {sha} {_esc(subject)} (`{_esc(job)}`): {why}." for sha, subject, job, why in dropped]
    return "\n".join(lines)


def apply(
    gh: Any,
    repo_dir: str,
    *,
    repo_full_name: str,
    release: str,
    plan: Plan,
    sweep_head: str,
    sweep_pr: int | None,
    push: Callable[[str, str, str], None],
) -> None:
    """Carry out ``plan`` once the sweep is pushed: rebuild, push, describe, or close.

    ``push(worktree, head, expected_remote_head)`` force-pushes the CI-sweep
    branch with a lease. Only git runs here, with the write token.
    """
    repo = gh.get_repo(repo_full_name)
    pull = repo.get_pull(plan.pr_number)
    if str(getattr(pull, "state", "") or "") != "open" or str(pull.head.sha) != plan.head:
        logger.info("CI-sweep PR #%s changed since it was planned; it is rebuilt on the next sweep", plan.pr_number)
        return
    if plan.close:
        pull.create_issue_comment(f"Closing: {plan.close}. Failures that remain are fixed again for the sweep.")
        pull.edit(state="closed")
        return
    if plan.foreign:
        if not any(FOREIGN_MARKER in str(getattr(c, "body", "") or "") for c in pull.get_issue_comments()):
            pull.create_issue_comment(
                "This branch has commits the bot did not make, so the bot no longer rebases it onto the sweep.\n"
                + FOREIGN_MARKER
            )
        return
    if not plan.keep:
        pull.create_issue_comment(dropped_comment(plan.dropped) + "\n\nClosing: no fix is left.")
        pull.edit(state="closed")
        return
    with tempfile.TemporaryDirectory(prefix="ci-sweep-") as worktree:
        run_git(repo_dir, "worktree", "add", "-q", "--detach", worktree, sweep_head)
        try:
            head = replay(worktree, sweep_head, PLANNED_REF, plan.keep)
            push(worktree, head, plan.head)
            fixes = [
                fix for sha in git_output(worktree, "rev-list", "--reverse", f"{sweep_head}..{head}").split()
                if (fix := as_fix(sha, git_output(worktree, "show", "-s", "--format=%B", sha))) is not None
            ]
        finally:
            run_git(repo_dir, "worktree", "remove", "--force", worktree)
    if plan.dropped:
        pull.create_issue_comment(dropped_comment(plan.dropped))
    if sweep_pr:
        pull.edit(body=body(release, sweep_pr, fixes))
