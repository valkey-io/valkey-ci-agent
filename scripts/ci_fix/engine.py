"""Prepare one CI fix.

Preparation runs in this order, every step on one checkout: a disposable
clone, or the checkout of the failed CI job the fix is prepared in, after
that job's own setup and build (``scripts/ci_fix/job.sh``):

1. **Find existing fixes**: search history for a merged change that names the
   failing test, changes its file, or references its issue
   (:mod:`scripts.ci_fix.prior_fixes`).
2. **Fix**: the CI job's log is the evidence of what fails. One agent session, told what step 1 found, the job log and the
   repository's test conventions, ports an existing fix or writes a patch.
3. **Verify and review**: the candidate (a port is cherry-picked into the
   checkout first) must build and pass the failing test, once and then
   looped, the way its CI job runs it (:mod:`scripts.ci_fix.verification`
   plans the command; when the test cannot run alone, its own block, then
   its whole file), and a separate read-only review must approve it.
   Otherwise the result goes back to the agent, up to ``MAX_ROUNDS`` times.

On a backport branch, and for a failure no command here can run, a change
already merged upstream comes first: a port, or a patch that adapts one
(``FixContext.upstream_first``). Where none fixes the failure, the fix is
written on the branch, and reported as one the default branch does not have.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

from scripts.ci_fix.agent import AgentTimeout, review_candidate, run_fix_agent
from scripts.ci_fix.models import (
    CandidateAction,
    FailedTest,
    FixCandidate,
    FixContext,
    FixPolicy,
    FixTask,
    TargetKind,
)
from scripts.ci_fix.policy import target_capabilities
from scripts.ci_fix.prior_fixes import (
    failing_test_history,
    find_prior_fixes,
    paths_in_log,
    render_prior_fixes,
    summarize_prior_fixes,
    unported_changes,
    unported_index,
)
from scripts.ci_fix.push import PushFailed, PushRefused, check_port, cherry_pick_refusal
from scripts.ci_fix.verification import (
    ISOLATED_COPY_PATTERN,
    JobCommands,
    TestRun,
    VerifyPlan,
    build_plan,
    failure_report,
    plan_for,
    upstream_plan,
)
from scripts.common.git_clone import is_full_sha, shallow_clone_at_sha
from scripts.common.isolation import as_user, handed_to, isolated_user, kill_all
from scripts.common.logging_utils import log_group
from scripts.common.proc import (
    PROCESS_BASICS,
    EmptyPatch,
    build_approved_patch,
    default_branch_ref,
    filter_env,
    git_output,
    git_paths,
    is_ancestor,
    run_git,
    worktree_changed_paths,
)
from scripts.common.text_utils import strip_ansi
from scripts.common.valkey_ci import FAILURE_MARKERS, failed_instance_tests_in_log, failed_tests_in_log

logger = logging.getLogger(__name__)

# A candidate that fails its test or its review goes back to the agent with
# the result, this many times in all.
MAX_ROUNDS = 3
TEST_TIMEOUT_S = 30 * 60
# The model's AWS session (ci-fix-run.yml's role-duration-seconds). The whole
# preparation, review included, must end inside it, so rounds stop when the
# budget runs low.
AWS_SESSION_S = 5 * 60 * 60
PREPARE_BUDGET_S = 4 * 60 * 60 + 30 * 60
_MIN_ROUND_S = 8 * 60
# Of each round's time, what the agent may not use: the candidate's
# verification (looped tests, possibly under valgrind) and review come after it.
_VERIFY_RESERVE_S = 60 * 60
# The same for a fix that is reviewed but not run.
_REVIEW_RESERVE_S = 15 * 60
# One agent session's share of the budget, so that a first round cannot use
# all of it: a candidate that fails its test still gets a second round.
_MAX_AGENT_ROUND_S = 2 * 60 * 60
# What the looped run of a verification may take. A slow test gets as many
# loops as fit (a 79s test cannot loop 40 times in the time kept back).
_LOOP_BUDGET_S = 45 * 60
_TEST_OUTPUT_CHARS = 40_000
# A make that stopped on an error; ``make[1]: [...] Error 2 (ignored)`` is not one.
_BUILD_BROKE_RE = re.compile(r"make(?:\[\d+\])?: \*\*\* .*Error \d+")
# At most this many of a job's failing tests are searched for and verified.
_MAX_TESTS = 3

# Where a repository's test conventions for the agent live, and the
# repository's own docs that the agent should read when present.
_GUIDES_DIR = Path(__file__).resolve().parent / "guides"
_REPO_DOCS = (
    "tests/README.md",
    ".github/instructions/integration-tests.instructions.md",
    "AGENTS.md",
    "DEVELOPMENT_GUIDE.md",
    "CONTRIBUTING.md",
)

AgentFunc = Callable[..., FixCandidate]


class ReviewFunc(Protocol):
    def __call__(
        self, candidate: FixCandidate, repo_dir: str, job_log: str, *, context: FixContext, timeout: int,
    ) -> tuple[bool, str]: ...


TestFunc = Callable[[str, str, int], TestRun]


def prepare_fix(
    task: FixTask,
    job_log: str,
    agent_func: AgentFunc = run_fix_agent,
    review_func: ReviewFunc | None = None,
    test_func: TestFunc | None = None,
    *,
    repo_dir: str = "",
    job: JobCommands | None = None,
) -> FixCandidate:
    """Build a patch or port candidate from the exact failed job and its log.

    A candidate for a branch the bot pushes to is verified and reviewed in a
    feedback loop (see the module docstring). A suggestion is reviewed but
    never run: it is for a PR whose code the bot does not execute. With
    ``repo_dir``, the fix is prepared in that checkout (the failed CI job's),
    built and tested with that ``job``'s commands; otherwise in a clone.
    """
    failure = task.failure
    if failure.job_id <= 0:
        return _refusal("The failed job has no valid job id.")

    if not job_log:
        return _refusal("The exact failed-job log is unavailable; no fix was prepared.")

    with checkout(task, repo_dir) as (repo_dir, original_head):
        deadline = time.monotonic() + PREPARE_BUDGET_S
        runs_tests = task.target.kind is not TargetKind.SUGGEST
        run_test = test_func or run_test_command
        tests = locate_unit_tests(repo_dir, failed_tests(task, job_log))
        context, plan = _establish(
            task, repo_dir, tests, paths_in_log(repo_dir, job_log),
            runs_tests=runs_tests, job=job, job_log=job_log,
        )
        if runs_tests and not context.reviewed_only:
            refusal = _unverifiable_refusal(context, plan)
            if refusal is not None:
                return refusal
        return _fix_loop(
            task, repo_dir, original_head, job_log, context, plan,
            agent_func=agent_func, review=review_func or review_candidate,
            run_test=run_test if runs_tests and plan is not None else None, deadline=deadline, job=job, tests=tests,
        )


def _unverifiable_refusal(context: FixContext, plan: VerifyPlan | None) -> FixCandidate | None:
    """Refuse when no command can run the failing test, where a fix may not be published on review alone."""
    if plan is not None:
        return None
    return _refusal(
        "No command on this runner can run the failing test, so a fix could not be checked. "
        "No edit was attempted.",
        verification="\n".join(_established_checks(context)),
    )


@contextmanager
def checkout(task: FixTask, repo_dir: str = "") -> Iterator[tuple[str, str]]:
    """The checkout at the commit the candidate is prepared on: (path, HEAD).

    ``repo_dir`` is an existing one (the failed CI job's); otherwise a
    disposable clone is made.
    """
    failure = task.failure
    checkout_sha = task.target.expected_sha or failure.head_sha
    if repo_dir:
        head = git_output(repo_dir, "rev-parse", "HEAD").strip()
        if head.lower() != checkout_sha.lower():
            raise RuntimeError(f"the job checked out {head[:12]} instead of {checkout_sha[:12]}")
        _keep_existing(repo_dir)
        yield repo_dir, head
        return
    with tempfile.TemporaryDirectory(prefix="ci-fix-") as workdir:
        # Traversable but not listable, so the isolated account can reach the
        # checkout inside it (see scripts/common/isolation.py).
        os.chmod(workdir, 0o711)
        clone = Path(workdir) / "repo"
        if not shallow_clone_at_sha(failure.repo_full_name, clone, checkout_sha, pull_number=failure.pr_number):
            raise RuntimeError(f"could not clone {failure.repo_full_name} at {checkout_sha[:12]}")
        original_head = git_output(str(clone), "rev-parse", "HEAD").strip()
        if original_head.lower() != checkout_sha.lower():
            raise RuntimeError(f"clone resolved {original_head[:12]} instead of {checkout_sha[:12]}")
        yield str(clone), original_head


# What a job's checkout held before the agent started: its ignored files
# (build output, generated test certificates) by content hash. Kept in .git,
# which the agent's account never owns.
_KEPT_FILES = "ci-fix-kept.json"


def _keep_existing(repo_dir: str) -> None:
    """Treat what the job's setup left in ``repo_dir`` as the job's, not the fix's.

    Untracked files (a dependency checked out into the tree) are ignored from
    now on, so they are not part of the patch; the ignored files are recorded,
    so verification removes only what changed after the job's setup.
    """
    untracked = git_paths(repo_dir, "ls-files", "--others", "--exclude-standard", "--directory", "-z")
    if untracked:
        exclude = Path(repo_dir, ".git", "info", "exclude")
        exclude.parent.mkdir(exist_ok=True)
        with exclude.open("a", encoding="utf-8", errors="surrogateescape") as out:
            out.write("".join("/" + _glob_escaped(path) + "\n" for path in untracked))
    kept = {path: _digest(Path(repo_dir, path)) for path in _ignored_files(repo_dir)}
    Path(repo_dir, ".git", _KEPT_FILES).write_text(json.dumps(kept), encoding="utf-8", errors="surrogateescape")


def _glob_escaped(path: str) -> str:
    return "".join("\\" + char if char in "*?[\\!#" else char for char in path)


def _ignored_files(repo_dir: str) -> tuple[str, ...]:
    """The ignored files in ``repo_dir``, never a directory.

    A job's setup may leave a whole directory the fix then ignores (a
    dependency checked out into the tree, as daily.yml's libbacktrace is);
    git names the directory itself, which has no content to hash or remove.
    """
    return tuple(
        path for path in git_paths(repo_dir, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
        if not path.endswith("/") and not Path(repo_dir, path).is_dir()
    )


def _digest(path: Path) -> str:
    """The content of ``path`` as a hash; "" for one that cannot be read as a file."""
    if path.is_symlink():
        return "link:" + os.readlink(path)
    if path.is_dir():
        return ""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
    except PermissionError:
        return ""
    return digest.hexdigest()


def failed_tests(task: FixTask, job_log: str) -> tuple[FailedTest, ...]:
    """The tests to search fixes for and verify: the front door's, then the log's.

    A detector issue is about its own test; other tests the same job failed
    have issues of their own, and a fix for this one must not wait on them.
    """
    tests = list(task.failure.failed_tests)
    if not (tests and task.failure.issue_number):
        tests += [
            test for test in (FailedTest.from_report(name, file) for name, file in failed_tests_in_log(job_log))
            if test not in tests
        ]
    if not tests:
        # A sentinel or legacy-cluster test names no file and no command here
        # runs it, but its name still finds the upstream change that fixes it.
        tests = [FailedTest(name, "") for name in failed_instance_tests_in_log(job_log)]
    # A log or issue names the file; one that leaves tests/ is not a test.
    return tuple(test for test in tests if ".." not in Path(test.file).parts)[:_MAX_TESTS]


def locate_unit_tests(repo_dir: str, tests: tuple[FailedTest, ...]) -> tuple[FailedTest, ...]:
    """``tests`` with each C unit test's file as its path in the checkout.

    A log or issue names the file by base name alone (``test_crc64.cpp``);
    the search for an existing fix and the agent need ``src/unit/test_crc64.cpp``.
    """
    src = Path(repo_dir, "src")
    located = []
    for test in tests:
        if test.is_unit_test and "/" not in test.file and src.is_dir():
            found = sorted(path for path in src.rglob(test.file) if path.is_file())
            if found:
                test = FailedTest(test.name, found[0].relative_to(repo_dir).as_posix())
        located.append(test)
    return tuple(located)


def _establish(
    task: FixTask,
    repo_dir: str,
    tests: tuple[FailedTest, ...],
    paths: tuple[str, ...],
    *,
    runs_tests: bool,
    job: JobCommands | None = None,
    job_log: str = "",
) -> tuple[FixContext, VerifyPlan | None]:
    """What the agent is told before it starts, and how code will check its patch.

    The CI job's log shows the failure; the plan is the command that
    verifies a candidate. What the history
    search found, and the repository's test guide, go to the agent too.
    """
    plan = None
    upstream_first = False
    described = ""
    if runs_tests:
        plan = plan_for(repo_dir, tests, scheduled=task.failure.scheduled, job=job)
        if plan is None and not tests and job is not None and job.build and _BUILD_BROKE_RE.search(job_log):
            plan = build_plan(job)
        elif plan is None and _can_port(task, repo_dir):
            # A sanitizer or valgrind report, a sentinel or legacy-cluster
            # test, a lint: nothing here runs it, but the default branch may
            # already hold its fix, which the pull request's own CI checks.
            plan = upstream_plan(job)
            upstream_first = True
        elif plan is not None and task.policy is FixPolicy.BACKPORT:
            # A release branch takes the upstream change where there is one.
            upstream_first = True
    # A job that failed outside any test the bot can run (a validator, an
    # exception between tests), with nothing upstream to port: the fixes
    # people write for these are small and read from the log, so one is
    # proposed, reviewed, and published as not run. Not on a backport
    # branch: its unrunnable failures take the plan above, which builds.
    reviewed_only = runs_tests and plan is None and task.policy is FixPolicy.GENERAL
    if reviewed_only:
        described = _NOT_RUN
    if plan is not None:
        _ignore_generated_copies(repo_dir)
        loops = plan.loops
        logger.info("Verification plan: %s", plan.command(loops))
        described = " ".join([
            f"Code checks your change with `{plan.command(loops)}`: the build must pass." if plan.build_only else
            f"Code checks your change with `{plan.command(loops)}`: the build must pass and the failing test "
            f"must run and pass, once and then looped (up to {loops} loop(s), fewer when one loop is slow).",
            *plan.notes,
        ])
    history = failing_test_history(repo_dir, tests)
    found = find_prior_fixes(
        repo_dir, tests, tested_sha=task.failure.head_sha, issue_number=task.failure.issue_number,
        paths=paths,
    )
    fixes = found or ()
    if fixes:
        logger.info("Existing-fix candidates: %s", "; ".join(f"{f.sha[:12]} {f.subject}" for f in fixes))
    guide = guide_for(task.failure.repo_full_name)
    context = FixContext(
        prior_fixes=summarize_prior_fixes(fixes),
        prior_fixes_detail=render_prior_fixes(repo_dir, fixes),
        prior_fixes_searched=found is not None,
        test_history=history,
        unported_changes=(
            unported_changes(repo_dir, tuple(dict.fromkeys([*(test.file for test in tests if test.file), *paths])))
            if upstream_first else ""
        ),
        unported_index=unported_index(repo_dir) if upstream_first else "",
        verification=described,
        verified_by_code=plan is not None,
        upstream_first=upstream_first,
        reviewed_only=reviewed_only,
        guide=guide,
        guide_paths=tuple(path for path in _REPO_DOCS if guide and (Path(repo_dir) / path).is_file()),
    )
    return context, plan


def _can_port(task: FixTask, repo_dir: str) -> bool:
    """Whether a port could fix ``task``: its target takes one, and the default branch has commits this checkout lacks."""
    if not target_capabilities(task.target.kind).allows(CandidateAction.PORT):
        return False
    default_ref = default_branch_ref(repo_dir)
    return bool(default_ref) and is_ancestor(repo_dir, default_ref, "HEAD") is False


def _ignore_generated_copies(repo_dir: str) -> None:
    """Keep a fallback's copy of a test file out of every patch, even a killed run's.

    The run removes its copy when it ends; one killed by its timeout cannot,
    and an untracked file would be part of the tree the patch is built from.
    Ignored, it is not, and is removed before each verification instead.
    """
    exclude = Path(repo_dir) / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if ISOLATED_COPY_PATTERN not in existing.splitlines():
            exclude.write_text(existing + ("" if existing.endswith("\n") or not existing else "\n")
                               + ISOLATED_COPY_PATTERN + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not ignore generated test copies in %s: %s", repo_dir, exc)


def guide_for(repo_full_name: str) -> str:
    path = _GUIDES_DIR / f"{repo_full_name.replace('/', '__')}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _fix_loop(
    task: FixTask,
    repo_dir: str,
    original_head: str,
    job_log: str,
    context: FixContext,
    plan: VerifyPlan | None,
    *,
    agent_func: AgentFunc,
    review: ReviewFunc,
    run_test: TestFunc | None,
    deadline: float,
    job: JobCommands | None = None,
    tests: tuple[FailedTest, ...] = (),
) -> FixCandidate:
    established = _established_checks(context)
    feedback = ""
    last = "no attempt was made"
    for attempt in range(1, MAX_ROUNDS + 1):
        remaining = int(deadline - time.monotonic())
        # Running out of time, or an agent crash, says nothing about the
        # failure: it is a failed attempt, which may be retried, not a refusal.
        if attempt > 1 and remaining < _MIN_ROUND_S:
            raise RuntimeError(f"Ran out of time after {attempt - 1} attempt(s). Last: {last}")
        # What comes after the agent: verification and review, or review
        # alone. In a short budget, at most half of what is left.
        reserve = min(_VERIFY_RESERVE_S if run_test is not None else _REVIEW_RESERVE_S, remaining // 2)
        agent_timeout = remaining - reserve
        agent_timeout = min(agent_timeout, _MAX_AGENT_ROUND_S)
        try:
            outcome = agent_round(
                task, repo_dir, original_head, job_log, context, feedback,
                attempt=attempt, timeout=max(60, agent_timeout), agent_func=agent_func,
            )
        except AgentTimeout:
            # A session that used its whole budget investigating found no
            # evidence-backed fix; a fresh one repeats the same reading.
            # Report that as a refusal, which ends this occurrence.
            logger.info("CI-fix round %d: the agent found no fix within its session", attempt)
            return _refusal(
                "The fix agent investigated for its whole session without finding an "
                "evidence-backed fix. No change was made.",
                verification="\n".join([*established, *([f"Earlier round: {last}"] if attempt > 1 else [])]),
            )
        except Exception as exc:
            if attempt == 1:
                raise
            raise RuntimeError(f"The agent failed on attempt {attempt}: {exc}. Last: {last}") from exc
        candidate = outcome.candidate
        if candidate.action is CandidateAction.REFUSE:
            return replace(candidate, verification="\n".join(established))
        if outcome.feedback:
            feedback = last = outcome.feedback
            logger.info("CI-fix round %d: %s", attempt, last)
            continue

        checks = list(established)
        if context.upstream_first and candidate.action is CandidateAction.PATCH and not candidate.adapts:
            checks.append(_OWN_FIX)
        if outcome.undeclared:
            checks.append(
                "The patch also changes " + ", ".join(f"`{path}`" for path in outcome.undeclared)
                + ", which its author did not list: check that it belongs to the fix."
            )
        reviewed = replace(candidate, patch=outcome.applied)
        # What the tree holds now is what the candidate is: the build and the
        # test commands run as the account that owns the tree, and could
        # rewrite the tracked files they run on. Only this exact change is
        # accepted; a changed tree is a changed candidate, and goes back.
        checked_tree = tree_patch(repo_dir)
        touches: tuple[int, ...] = ()
        try:
            failed = ""
            if run_test is not None and plan is not None:
                failed = _verify(reviewed, repo_dir, plan, run_test, deadline, checks)
            if not failed:
                # The reviewer sees what the checks showed, not only the plan.
                approved, reasoning = review(
                    replace(reviewed, verification="\n".join(checks)), repo_dir, job_log,
                    context=context, timeout=_timeout(deadline),
                )
                if not approved:
                    failed = f"A reviewer rejected the {_noun(candidate)}: {reasoning}"
                    logger.info("CI-fix round %d: review rejected the %s", attempt, _noun(candidate))
                else:
                    checks.append(f"Review: {reasoning}" if reasoning else f"Review approved the {_noun(candidate)}.")
            if not failed:
                if attempt > 1:
                    checks.append(f"Accepted on attempt {attempt} of {MAX_ROUNDS}.")
                # While the change is still in the tree: a port's cherry-pick
                # is undone below, and its patch is the one it applied.
                touches = other_issues_touched(task, reviewed, repo_dir, run_test, deadline, checks, job=job)
            if not failed and tree_patch(repo_dir) != checked_tree:
                failed = _TREE_CHANGED
                logger.info("CI-fix round %d: the tree changed during verification", attempt)
        finally:
            if candidate.action is CandidateAction.PORT:
                restore(repo_dir)
        if failed:
            last = failed
            tree = _RESET_TREE if candidate.action is CandidateAction.PORT else _KEPT_TREE
            feedback = "\n\n".join(part for part in (failed, _earlier_diagnosis(candidate), tree) if part)
            logger.info("CI-fix round %d: %s", attempt, failed.splitlines()[0])
            continue
        return replace(
            candidate, verification="\n".join(checks), also_touches=touches, not_run=context.reviewed_only,
        )
    return _refusal(
        f"No candidate passed its test and review in {MAX_ROUNDS} attempts. Last: {last}",
        verification="\n".join(established),
    )


@dataclass(frozen=True)
class RoundOutcome:
    """One agent round: the checked candidate, applied in the tree as ``applied``.

    ``feedback`` is set, and nothing is applied, when a port did not apply.
    """

    candidate: FixCandidate
    applied: str = ""
    feedback: str = ""
    # Files the patch changes that the agent did not list: published with
    # the rest, and pointed out to the reviewer and in the report.
    undeclared: tuple[str, ...] = ()


def agent_round(
    task: FixTask,
    repo_dir: str,
    original_head: str,
    job_log: str,
    context: FixContext,
    feedback: str,
    *,
    attempt: int,
    timeout: int,
    agent_func: AgentFunc = run_fix_agent,
) -> RoundOutcome:
    """Run the agent once and apply what it chose to the working tree.

    A patch is the agent's own edits; a port is cherry-picked into the tree.
    The caller undoes a port with :func:`restore` once it is done with it.
    """
    candidate = agent_func(task, repo_dir, job_log, feedback=feedback, timeout=timeout, context=context)
    if attempt > 1 and candidate.action is CandidateAction.PORT:
        # An earlier round's patch is still in the tree; the agent has moved
        # on to porting instead.
        restore(repo_dir)
    try:
        declared = set(candidate.changed_paths)
        candidate = _checked_candidate(
            task, repo_dir, original_head, candidate, upstream_first=context.upstream_first,
        )
    except _Rejected as exc:
        tree = _KEPT_TREE if worktree_changed_paths(repo_dir, strict=True) else _RESET_TREE
        return RoundOutcome(candidate, feedback=f"{exc}\n\n{tree}")
    if candidate.action is CandidateAction.REFUSE:
        return RoundOutcome(candidate)
    if candidate.action is CandidateAction.PORT:
        try:
            return RoundOutcome(candidate, applied=_apply_port(repo_dir, candidate.port_commit))
        except PortNotApplied as exc:
            return RoundOutcome(candidate, feedback=(
                f"{exc} Adapt it as a `patch` if it is the right fix, or choose another action."
                f"{_commit_diff(repo_dir, candidate.port_commit)}\n\n{_RESET_TREE}"
            ))
    return RoundOutcome(
        candidate, applied=candidate.patch, undeclared=tuple(sorted(set(candidate.changed_paths) - declared)),
    )


def _commit_diff(repo_dir: str, commit: str) -> str:
    """``commit``'s diff, for an agent that has to adapt it and cannot run git; "" when it cannot be read."""
    try:
        diff = git_output(repo_dir, "show", "--no-ext-diff", "--no-textconv", "--format=", commit)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    if len(diff) > _TEST_OUTPUT_CHARS:
        diff = diff[:_TEST_OUTPUT_CHARS] + "\n[... diff truncated ...]"
    return f"\n\nIts diff:\n```diff\n{diff.rstrip()}\n```" if diff.strip() else ""


_KEPT_TREE = "The working tree still holds your previous edits; revise them."
_RESET_TREE = "The working tree was reset to the working commit; it holds no earlier edits."
_TREE_CHANGED = (
    "The build or the tests changed tracked files while the candidate was being "
    "verified, so the tree no longer matches the patch that was checked. Make the fix "
    "itself: nothing the build or a test writes can be part of it."
)
# Of the hand-written fixes people pushed to backport branches, eleven of
# thirteen were for a flake the release branch already had, in a file the
# backport never touched.
_OWN_FIX = (
    "No change on the default branch was found to fix this failure, so this fix was written "
    "for this branch: the default branch does not have it."
)
_NOT_RUN = "No command here can run the failing check, so this fix was reviewed, not run."


def _earlier_diagnosis(candidate: FixCandidate) -> str:
    """What the last session concluded, for the next one, which starts without its reading."""
    found = [
        f"- {label}: {text}" for label, text in (
            ("Root cause", candidate.root_cause), ("Change", candidate.reasoning), ("Notes", candidate.validation),
        ) if text
    ]
    if not found:
        return ""
    return "\n".join(["Your previous session concluded (the result above may show it wrong):", *found])


def tree_patch(repo_dir: str) -> str:
    """The exact patch of every change in ``repo_dir``'s working tree; "" when it is clean."""
    paths = worktree_changed_paths(repo_dir, strict=True)
    if not paths:
        return ""
    try:
        return build_approved_patch(repo_dir, paths, strict=True)
    except EmptyPatch:
        return ""


def _noun(candidate: FixCandidate) -> str:
    return "port" if candidate.action is CandidateAction.PORT else "patch"


def _established_checks(context: FixContext) -> list[str]:
    checks = []
    if context.prior_fixes:
        checks.append("Existing fixes found: " + "; ".join(context.prior_fixes.splitlines()))
    elif context.prior_fixes_searched:
        checks.append("No merged change that looks like a fix was found.")
    if context.verification:
        checks.append(context.verification)
    return checks


def other_issues_touched(
    task: FixTask,
    candidate: FixCandidate,
    repo_dir: str,
    run_test: TestFunc | None,
    deadline: float,
    checks: list[str],
    *,
    job: JobCommands | None = None,
) -> tuple[int, ...]:
    """The other detector issues whose test this patch changes.

    One file often holds several open failures, so a patch for one issue can
    change another's test. Each such issue's test is run with the patch, the
    way the target's is verified, and the draft PR names it as touched.
    Passing with the patch does not show the patch fixes it, so no other
    issue is claimed as fixed (and closed by the PR).
    """
    changed = changed_lines(candidate.patch)
    others = [issue for issue in task.other_issues if _changes_test(repo_dir, changed, issue.test)]
    touched: list[int] = []
    for issue in others:
        # In code, so that GitHub does not link the PR from that issue: it
        # has not shown that it fixes it.
        name, ref = f"`{issue.test.name or issue.test.file}`", f"`#{issue.number}`"
        touched.append(issue.number)
        plan = plan_for(repo_dir, (issue.test,), scheduled=task.failure.scheduled, job=job)
        if run_test is None or plan is None or time.monotonic() > deadline:
            checks.append(f"The patch changes {name}, issue {ref}'s test, which could not be run.")
            continue
        result = run_test(repo_dir, plan.command(plan.loops), _timeout(deadline))
        checks.append(
            f"The patch changes {name}, issue {ref}'s test, which passed {plan.loops} loop(s) with it."
            if result.passed and plan.ran(result) else
            f"The patch changes {name}, issue {ref}'s test, which did not pass with it."
        )
    return tuple(touched)


def changed_lines(patch: str) -> dict[str, set[int]]:
    """For each file ``patch`` changes, the lines of the patched file it changes.

    An added line is its own line; a removed one counts as the line now in
    its place, so a deletion still marks where it was.
    """
    changed: dict[str, set[int]] = {}
    lines: set[int] = set()
    line = 0
    for text in patch.splitlines():
        if text.startswith("+++ "):
            lines = changed.setdefault(text[6:] if text.startswith("+++ b/") else text[4:], set())
        elif text.startswith("@@"):
            match = _HUNK_RE.match(text)
            line = int(match.group(1)) if match else 0
        elif text.startswith("+") and not text.startswith("+++"):
            lines.add(line)
            line += 1
        elif text.startswith("-") and not text.startswith("---"):
            lines.add(line)
        elif text.startswith(" "):
            line += 1
    return changed


_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
# A Tcl test's start: ``test {name} {`` or ``test "name" {``.
_TEST_START_RE = re.compile(r"^\s*test\s+[{\"]")


def _changes_test(repo_dir: str, changed: dict[str, set[int]], test: FailedTest) -> bool:
    """Whether the ``changed`` lines fall inside ``test``'s own block.

    The block runs from the line that names the test to the next test. A
    test whose name is built at run time (``"$cmd operation ..."``) cannot be
    found by its name; a change to its file is then taken to touch it.
    """
    lines = changed.get(test.file)
    if not lines:
        return False
    if not test.name:
        return True  # the whole file is what failed
    try:
        source = Path(repo_dir, test.file).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return True
    starts = [number for number, text in enumerate(source, 1) if _TEST_START_RE.match(text)]
    named = [number for number in starts
             if f"{{{test.name}}}" in source[number - 1] or f'"{test.name}"' in source[number - 1]]
    if not named:
        return True
    for start in named:
        end = next((later for later in starts if later > start), len(source) + 1)
        if any(start <= line < end for line in lines):
            return True
    return False


def _verify(
    candidate: FixCandidate,
    repo_dir: str,
    plan: VerifyPlan,
    run_test: TestFunc,
    deadline: float,
    checks: list[str],
) -> str:
    """Run the candidate's tests; "" when they hold, else feedback for the agent.

    The build or an earlier run could leave ignored files the patch does not
    show (a prebuilt server binary that keeps ``make`` from rebuilding), so
    those go first: the build and tests run from source.
    """
    _remove_ignored(repo_dir)
    if any(path.startswith("deps/") for path in changed_lines(candidate.patch)):
        _rebuild_dependencies(repo_dir)
    return _verify_with_plan(plan, repo_dir, run_test, deadline, checks)


def _rebuild_dependencies(repo_dir: str) -> None:
    """Make the next build rebuild ``deps/``.

    Valkey's src/Makefile builds its dependencies only when its saved build
    settings change, and a job's own build output is kept, so a fix under
    ``deps/`` would be verified against the library built without it.
    Without the saved settings, make cleans and rebuilds everything.
    """
    Path(repo_dir, "src", ".make-settings").unlink(missing_ok=True)


def _verify_with_plan(
    plan: VerifyPlan,
    repo_dir: str,
    run_test: TestFunc,
    deadline: float,
    checks: list[str],
) -> str:
    """The patched code building and its test passing, the way the CI job runs it; feedback when not.

    The build runs first, alone, so a build break is reported as one and the
    test's time is its own. Then the test runs once: with ``--only`` it may
    not run at all, or fail for want of what a skipped test set up
    (integration/replication.tcl, cluster/slave-selection.tcl), so a plan
    whose first run does not pass is followed by its fallbacks, the test's own
    block and then its whole file. The plan that passed once is then looped,
    as many times as fit, and has to pass every loop.
    """
    built = _run(run_test, repo_dir, plan.build, deadline)
    if not built.passed:
        return f"With your change, `{plan.build}` does not build.\n\nIts output:\n{built.output}"
    if plan.build_only:
        checks.append(f"With the fix, `{plan.build}` builds.")
        return ""
    current = plan
    while True:
        first = _run(run_test, repo_dir, current.command(1), deadline)
        if first.passed and current.ran(first):
            break
        if current.fallback is None:
            return _not_passing(current, current.quoted(1), 1, first)
        current = current.fallback
    loops = _loops_that_fit(current, first.seconds, deadline)
    if loops > 1:
        looped = _run(run_test, repo_dir, current.command(loops), deadline)
        if not (looped.passed and current.ran(looped)):
            return _not_passing(current, current.quoted(loops), loops, looped)
    checks.append(f"With the fix, `{current.quoted(loops)}` passed {loops} loop(s).")
    if loops < current.loops:
        checks.append(f"One loop takes {first.seconds:.0f}s, so {loops} loop(s) ran, not {current.loops}.")
    return ""


def _run(run_test: TestFunc, repo_dir: str, command: str, deadline: float) -> TestRun:
    result = run_test(repo_dir, command, _timeout(deadline))
    if result.timed_out:
        # Nothing is known about the change, and nothing the agent can do
        # about it: the attempt failed, and is retried.
        raise RuntimeError(f"`{command}` did not finish in the time left ({result.output}).")
    return result


def _loops_that_fit(plan: VerifyPlan, seconds: float, deadline: float) -> int:
    """The plan's loops, or as many as fit in the time for them when one loop took ``seconds``."""
    available = min(_LOOP_BUDGET_S, deadline - time.monotonic())
    return max(1, min(plan.loops, int(available // max(seconds, 1.0))))


def _not_passing(plan: VerifyPlan, command: str, loops: int, result: TestRun) -> str:
    """The agent's feedback for a run that did not pass the failing test."""
    if result.passed:
        return (
            f"With your change, `{command}` passed without running the failing test. "
            "Do not rename, skip, or tag out the test: fix why it fails.\n\n"
            f"Its output:\n{result.output}"
        )
    if plan.failed(result):
        return (
            f"With your change, the failing test still fails within {loops} loop(s): `{command}`.\n\n"
            f"Its output:\n{result.output}"
        )
    return (
        f"With your change, `{command}` could not run the tests (a server or test client "
        "failing outside the test).\n\n"
        f"Its output:\n{result.output}"
    )


def _apply_port(repo_dir: str, commit: str) -> str:
    """Cherry-pick ``commit`` into the working tree and return its patch.

    The publisher's own checks (merged on the default branch, not already
    here, not a merge) run first. Raises ``PortNotApplied`` with the reason,
    after restoring the tree.
    """
    try:
        check_port(Path(repo_dir), commit, "HEAD")
    except (PushRefused, PushFailed) as exc:
        raise PortNotApplied(str(exc)) from exc
    try:
        run_git(repo_dir, "cherry-pick", "--no-commit", commit)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        reason = cherry_pick_refusal(Path(repo_dir), commit) if isinstance(exc, subprocess.CalledProcessError) else ""
        restore(repo_dir)
        detail = " ".join(str(getattr(exc, "stderr", "") or exc).split())[:500]
        raise PortNotApplied(reason or f"Refusing to port {commit[:12]}: {detail}") from exc
    patch = git_output(repo_dir, "diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD")
    if not patch.strip():
        restore(repo_dir)
        raise PortNotApplied(f"Refusing to port {commit[:12]}: it changes nothing on the working commit.")
    return patch


class PortNotApplied(Exception):
    """A port that cannot be cherry-picked onto the working commit."""


def restore(repo_dir: str) -> None:
    """Undo a cherry-pick into the working tree, keeping ignored build output."""
    for args in (("cherry-pick", "--quit"), ("reset", "--hard", "--quiet", "HEAD"), ("clean", "-fdq")):
        try:
            run_git(repo_dir, *args)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            logger.warning("Could not run git %s after a port", " ".join(args))


def _remove_ignored(repo_dir: str) -> None:
    """Remove every ignored file, except a job's own that are still as its setup left them."""
    try:
        kept = json.loads(Path(repo_dir, ".git", _KEPT_FILES).read_text(encoding="utf-8", errors="surrogateescape"))
    except FileNotFoundError:
        run_git(repo_dir, "clean", "-fdXq")
        return
    for path in _ignored_files(repo_dir):
        target = Path(repo_dir, path)
        if kept.get(path) != _digest(target):
            target.unlink(missing_ok=True)


def _timeout(deadline: float) -> int:
    """What a verification or review may use: the rest of the deadline.

    A looped verification (40 loops of a flaky test, or valgrind) can need
    more than one test command's default; the deadline already bounds it.
    """
    return max(60, int(deadline - time.monotonic()))


def _checked_candidate(
    task: FixTask, repo_dir: str, original_head: str, candidate: FixCandidate, *,
    upstream_first: bool = False,
) -> FixCandidate:
    """``candidate`` with its patch built from the checkout, or a refusal.

    A commit is a refusal: the agent broke the one rule its checkout depends
    on. Anything else wrong with what it handed back raises
    :class:`_Rejected`, which goes back to it like a failing test. With
    ``upstream_first``, a patch's claim to adapt a merged upstream commit is
    kept only when code finds it does; otherwise the patch is the bot's own.

    The patch is what the working tree holds, not what the agent listed: a
    file it changed and did not list is published with the rest and pointed
    out to the reviewer, rather than costing a round.
    """
    current_head = git_output(repo_dir, "rev-parse", "HEAD").strip()
    if current_head != original_head:
        return _refusal(
            "The agent created a commit. CI-fix agents may edit files but may not commit or push."
        )

    changed_paths = worktree_changed_paths(repo_dir, strict=True)
    if candidate.action is CandidateAction.REFUSE:
        return replace(candidate, patch="", changed_paths=(), port_commit="", adapts="", validation="")
    if not candidate.failing_check:
        # The job is what failed, whatever else the agent left out.
        candidate = replace(candidate, failing_check=task.failure.job_name)

    capabilities = target_capabilities(task.target.kind)
    if not capabilities.allows(candidate.action):
        raise _Rejected(
            f"The {candidate.action.value} action is not supported for "
            f"a {task.target.kind.value} target."
        )
    if candidate.action is CandidateAction.PORT:
        if changed_paths:
            raise _Rejected(
                "The agent mixed an upstream port with working-tree edits; no candidate was published."
            )
        if not is_full_sha(candidate.port_commit):
            raise _Rejected("The proposed upstream port is not a full commit SHA.")
        return replace(candidate, patch="", changed_paths=(), adapts="", validation="")

    if not changed_paths:
        raise _Rejected("The agent selected a patch but made no working-tree changes.")
    try:
        patch = build_approved_patch(repo_dir, changed_paths, strict=True)
    except EmptyPatch:
        raise _Rejected("The agent selected a patch but produced no patch content.")
    adapts = ""
    if upstream_first and _adapts(repo_dir, candidate.adapts, changed_paths, patch):
        adapts = candidate.adapts
    patch_limit = capabilities.patch_limit
    if len(patch) > patch_limit:
        raise _Rejected(
            f"The generated patch is too large to publish safely "
            f"({len(patch)} > {patch_limit} characters)."
        )
    return replace(
        candidate,
        patch=patch,
        changed_paths=changed_paths,
        port_commit="",
        # Only a checked claim is kept, and reported.
        adapts=adapts,
    )


# How much of an adapting patch must be its upstream commit's change: of the
# lines it adds, or of the lines it removes. Of the fixes pushed to backport
# branches, the one real port shared all of its added lines with an upstream
# commit and no hand-written one more than a sixth, though each changed a
# file that 3 to 175 unported upstream commits change. An adaptation may
# share few added lines (another encoding to assert, a wider tolerance, its
# own comment: 12% in one that a reviewer accepted) but it replaces the lines
# its commit replaces.
_MIN_ADAPTED_SHARE = 0.5


def _adapts(repo_dir: str, commit: str, changed_paths: tuple[str, ...], patch: str) -> bool:
    """Whether ``patch`` adapts ``commit``, a merged upstream change.

    The commit has to pass what a port must (merged on the default branch,
    not on this one), and the patch has to be mostly that commit's change to
    the same files: naming a commit that only touches the file adapts nothing.
    """
    if not is_full_sha(commit):
        return False
    try:
        check_port(Path(repo_dir), commit, "HEAD")
        upstream = git_output(
            repo_dir, "show", "--no-ext-diff", "--no-textconv", "--format=", commit, "--", *changed_paths,
        )
    except (PushRefused, PushFailed, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        logger.info("The patch does not adapt %s: %s", commit[:12], exc)
        return False

    def shared(sign: str) -> float:
        ours = changed_text(patch, sign)
        return len(ours & changed_text(upstream, sign)) / len(ours) if ours else 0.0

    return max(shared("+"), shared("-")) >= _MIN_ADAPTED_SHARE


def changed_text(diff: str, sign: str) -> set[str]:
    """The lines a diff adds (``+``) or removes (``-``), without their indentation; a lone brace or blank says nothing."""
    return {
        text for text in (line[1:].strip() for line in diff.splitlines()
                          if line.startswith(sign) and not line.startswith(sign * 3))
        if len(text) > 3
    }


def run_test_command(repo_dir: str, command: str, timeout: int = TEST_TIMEOUT_S) -> TestRun:
    """Run ``command`` in ``repo_dir`` as the isolated account."""
    env = filter_env(PROCESS_BASICS)
    argv = ["bash", "-c", command]
    user = isolated_user()
    started = time.monotonic()
    try:
        if user:
            with handed_to(user, Path(repo_dir)) as home:
                invocation = as_user(user, env, argv, home=home, cwd=repo_dir)
                result = _run_in_own_group(
                    list(invocation.argv),
                    None,
                    invocation.env,
                    timeout,
                    user=user,
                )
        else:
            result = _run_in_own_group(argv, repo_dir, env, timeout)
    except subprocess.TimeoutExpired:
        logger.info("Test command timed out after %ds: %s", timeout, command)
        return TestRun(passed=False, output=f"timed out after {timeout}s", timed_out=True)
    seconds = time.monotonic() - started
    output = strip_ansi((result.stdout + result.stderr).decode("utf-8", errors="replace"))
    logger.info("Test command exited %d after %.0fs: %s", result.returncode, seconds, command)
    excerpt = _excerpt(output)
    if result.returncode != 0:
        # The only record of why a command failed, once the run is over.
        with log_group("Failed test command output"):
            logger.info("%s", excerpt)
    return TestRun(
        passed=result.returncode == 0,
        output=excerpt,
        report=failure_report(output),
        seconds=seconds,
    )


def _run_in_own_group(
    argv: list[str], repo_dir: str | None, env: dict[str, str] | None, timeout: int, *, user: str = "",
) -> subprocess.CompletedProcess[bytes]:
    """Run ``argv``; on timeout kill its whole process group, not just the shell.

    A command run as ``user`` sits under root's sudo or runuser, which this
    process may not signal: everything that account runs is killed instead.
    """
    process = subprocess.Popen(
        argv, cwd=repo_dir, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if user:
            kill_all(user)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            # A process that left the group may still hold the pipes.
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _excerpt(output: str) -> str:
    """The first failure report and the end of ``output``, within the size limit.

    ``--dump-logs`` prints every server's log after a failure, so the tail
    alone would hide the ``[err]`` line that says what failed.
    """
    if len(output) <= _TEST_OUTPUT_CHARS:
        return output
    tail = output[-_TEST_OUTPUT_CHARS // 2:]
    # A build break has no test report: its first compiler error says what broke.
    found = [at for at in (output.find(marker) for marker in (*FAILURE_MARKERS, ": error: ")) if at >= 0]
    if not found:
        return output[-_TEST_OUTPUT_CHARS:]
    at = min(found)
    start = max(0, output.rfind("\n", 0, max(0, at - 500)) + 1)
    head = output[start:start + _TEST_OUTPUT_CHARS // 2]
    return f"{head}\n[... output trimmed ...]\n{tail}"


def _refusal(summary: str, verification: str = "") -> FixCandidate:
    return FixCandidate(action=CandidateAction.REFUSE, summary=summary, verification=verification)


class _Rejected(Exception):
    """What the agent handed back cannot be used as it is; it may revise it."""
