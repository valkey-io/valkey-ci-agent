"""Prepare one CI fix.

Preparation runs in this order, every step on one checkout: a disposable
clone, or the checkout of the failed CI job the fix is prepared in, after
that job's own setup and build (``scripts/ci_fix/job.sh``):

1. **Find existing fixes**: search history for a merged change that names the
   failing test, changes its file, or references its issue
   (:mod:`scripts.ci_fix.prior_fixes`).
2. **Reproduce**: run the failing test on the unfixed code the way its CI job
   did, looped, then under load (:mod:`scripts.ci_fix.reproduce`).
3. **Fix**: one agent session, told what steps 1 and 2 found and the
   repository's test conventions, ports an existing fix or writes a patch.
4. **Verify and review**: the candidate (a port is cherry-picked into the
   checkout first) must pass the reproduction many times, and a separate
   read-only review must approve it. Otherwise the result goes back to the
   agent, up to ``MAX_ROUNDS`` times.
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

from scripts.ci_fix.agent import review_candidate, run_fix_agent
from scripts.ci_fix.models import (
    CandidateAction,
    FailedTest,
    FailureType,
    FixCandidate,
    FixContext,
    FixTask,
    TargetKind,
)
from scripts.ci_fix.policy import target_capabilities
from scripts.ci_fix.prior_fixes import (
    find_prior_fixes,
    paths_in_log,
    render_prior_fixes,
    summarize_prior_fixes,
)
from scripts.ci_fix.push import PushFailed, PushRefused, check_port, cherry_pick_refusal
from scripts.ci_fix.reproduce import (
    JobCommands,
    Reproduction,
    ReproPlan,
    ReproStatus,
    TestRun,
    failure_report,
    plan_for,
    ran_tests,
    reproduce,
    shows_failure_of,
)
from scripts.common.git_clone import is_full_sha, shallow_clone_at_sha
from scripts.common.isolation import as_user, handed_to, isolated_user, kill_all
from scripts.common.proc import (
    PROCESS_BASICS,
    EmptyPatch,
    build_approved_patch,
    filter_env,
    git_output,
    git_paths,
    run_git,
    worktree_changed_paths,
)
from scripts.common.text_utils import strip_ansi
from scripts.common.valkey_ci import FAILURE_MARKERS, failed_tests_in_log

logger = logging.getLogger(__name__)

MAX_JOB_LOG_CHARS = 300_000
JOB_LOG_HEAD_CHARS = 40_000

# A candidate that fails its test or its review goes back to the agent with
# the result, this many times in all.
MAX_ROUNDS = 3
# How many times the agent's own test command must pass with the patch, when
# code has no reproduction of its own: once is luck for a flaky failure.
TEST_RUNS = 2
FLAKY_TEST_RUNS = 10
TEST_TIMEOUT_S = 30 * 60
# The model's AWS session (ci-fix-run.yml's role-duration-seconds). The whole
# preparation, review included, must end inside it, so rounds stop when the
# budget runs low. Reproduction may use part of it.
AWS_SESSION_S = 75 * 60
PREPARE_BUDGET_S = 60 * 60
REPRO_BUDGET_S = 20 * 60
_MIN_ROUND_S = 8 * 60
# Of each round's time, what the agent may not use: the candidate's
# verification (looped tests, possibly under valgrind) and review come after it.
_VERIFY_RESERVE_S = 10 * 60
_TEST_OUTPUT_CHARS = 6_000
# At most this many of a job's failing tests are searched for and reproduced.
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
    job_log = bounded_job_log(job_log)

    with checkout(task, repo_dir) as (repo_dir, original_head):
        deadline = time.monotonic() + PREPARE_BUDGET_S
        runs_tests = task.target.kind is not TargetKind.SUGGEST
        run_test = test_func or run_test_command
        tests = locate_unit_tests(repo_dir, failed_tests(task, job_log))
        context, plan, reproduction = _establish(
            task, repo_dir, tests, paths_in_log(repo_dir, job_log),
            run_test if runs_tests else None,
            repro_deadline=min(deadline, time.monotonic() + REPRO_BUDGET_S), job=job,
        )
        return _fix_loop(
            task, repo_dir, original_head, job_log, context, plan, reproduction,
            agent_func=agent_func, review=review_func or review_candidate,
            run_test=run_test if runs_tests else None, deadline=deadline, job=job, tests=tests,
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
    """The tests to search fixes for and reproduce: the front door's, then the log's.

    A detector issue is about its own test; other tests the same job failed
    have issues of their own, and a fix for this one must not wait on them.
    """
    tests = list(task.failure.failed_tests)
    if not (tests and task.failure.issue_number):
        tests += [
            test for test in (FailedTest.from_report(name, file) for name, file in failed_tests_in_log(job_log))
            if test not in tests
        ]
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
    run_test: TestFunc | None,
    *,
    repro_deadline: float,
    job: JobCommands | None = None,
) -> tuple[FixContext, ReproPlan | None, Reproduction | None]:
    """Reproduce the failure and search for existing fixes, before any edit.

    What both found, and the repository's test guide, is what the agent is
    told before it starts.
    """
    plan = reproduction = None
    described = ""
    if run_test is not None:
        plan = plan_for(repo_dir, tests, scheduled=task.failure.scheduled, job=job)
    if plan is not None:
        assert run_test is not None  # a plan is only made to be run
        logger.info("Reproducing with: %s", plan.command(1))
        reproduction = reproduce(
            plan,
            lambda command, timeout: run_test(repo_dir, command, timeout),
            deadline=repro_deadline,
        )
        logger.info("Reproduction: %s", reproduction.describe())
        described = " ".join([reproduction.describe(), *plan.notes])
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
        reproduction=described,
        reproduction_output=reproduction.output if reproduction else "",
        verified_by_code=reproduction is not None and reproduction.status is not ReproStatus.UNRUNNABLE,
        guide=guide,
        guide_paths=tuple(path for path in _REPO_DOCS if guide and (Path(repo_dir) / path).is_file()),
    )
    return context, plan, reproduction


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
    plan: ReproPlan | None,
    reproduction: Reproduction | None,
    *,
    agent_func: AgentFunc,
    review: ReviewFunc,
    run_test: TestFunc | None,
    deadline: float,
    job: JobCommands | None = None,
    tests: tuple[FailedTest, ...] = (),
) -> FixCandidate:
    established = _established_checks(context, reproduction)
    feedback = ""
    last = "no attempt was made"
    for attempt in range(1, MAX_ROUNDS + 1):
        remaining = int(deadline - time.monotonic())
        # Running out of time, or an agent crash, says nothing about the
        # failure: it is a failed attempt, which may be retried, not a refusal.
        if attempt > 1 and remaining < _MIN_ROUND_S:
            raise RuntimeError(f"Ran out of time after {attempt - 1} attempt(s). Last: {last}")
        agent_timeout = remaining - _VERIFY_RESERVE_S if run_test is not None else remaining
        try:
            outcome = agent_round(
                task, repo_dir, original_head, job_log, context, feedback,
                attempt=attempt, timeout=max(60, agent_timeout), agent_func=agent_func,
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
        reviewed = replace(candidate, patch=outcome.applied)
        # What the tree holds now is what the candidate is: the build and the
        # test commands run as the account that owns the tree, and could
        # rewrite the tracked files they run on. Only this exact change is
        # accepted; a changed tree is a changed candidate, and goes back.
        checked_tree = tree_patch(repo_dir)
        fixes: tuple[int, ...] = ()
        touches: tuple[int, ...] = ()
        try:
            failed = ""
            if run_test is not None:
                failed = _verify(reviewed, repo_dir, plan, reproduction, run_test, deadline, checks, tests)
            if not failed:
                approved, reasoning = review(
                    reviewed, repo_dir, job_log, context=context, timeout=_timeout(deadline),
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
                fixes, touches = other_issues_fixed(task, reviewed, repo_dir, run_test, deadline, checks, job=job)
            if not failed and tree_patch(repo_dir) != checked_tree:
                failed = _TREE_CHANGED
                logger.info("CI-fix round %d: the tree changed during verification", attempt)
        finally:
            if candidate.action is CandidateAction.PORT:
                restore(repo_dir)
        if failed:
            last = failed
            tree = _RESET_TREE if candidate.action is CandidateAction.PORT else _KEPT_TREE
            feedback = f"{failed}\n\n{tree}"
            logger.info("CI-fix round %d: %s", attempt, failed.splitlines()[0])
            continue
        return replace(
            candidate, verification="\n".join(checks), also_fixes=fixes, also_touches=touches,
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
        candidate = _checked_candidate(task, repo_dir, original_head, candidate)
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
                f"\n\n{_RESET_TREE}"
            ))
    return RoundOutcome(candidate, applied=candidate.patch)


_KEPT_TREE = "The working tree still holds your previous edits; revise them."
_RESET_TREE = "The working tree was reset to the working commit; it holds no earlier edits."
_TREE_CHANGED = (
    "The build or your test command changed tracked files while the candidate was being "
    "verified, so the tree no longer matches the patch that was checked. A test command "
    "must not edit the repository: make the fix itself, and give a command that only runs it."
)


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


def _established_checks(context: FixContext, reproduction: Reproduction | None) -> list[str]:
    checks = []
    if context.prior_fixes:
        checks.append("Existing fixes found: " + "; ".join(context.prior_fixes.splitlines()))
    elif context.prior_fixes_searched:
        checks.append("No merged change that looks like a fix was found.")
    if context.reproduction:
        checks.append(context.reproduction)
    return checks


def other_issues_fixed(
    task: FixTask,
    candidate: FixCandidate,
    repo_dir: str,
    run_test: TestFunc | None,
    deadline: float,
    checks: list[str],
    *,
    job: JobCommands | None = None,
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Which other detector issues this patch was shown to fix, and which it only touched.

    One file often holds several open failures, so a patch for one issue can
    change another's test. Each such issue is verified on its own, exactly as
    the target was: its test must fail without the patch and pass with it.
    Only then may the draft PR say it fixes it.
    """
    changed = changed_lines(candidate.patch)
    others = [issue for issue in task.other_issues if _changes_test(repo_dir, changed, issue.test)]
    if not others:
        return (), ()
    fixed: list[int] = []
    touched: list[int] = []
    for issue in others:
        # In code: GitHub links a bare #N, and a linked open PR keeps that
        # issue from a fix of its own (issue_state.has_open_linked_pr), which
        # a PR that has not shown it fixes the issue must not do.
        name, ref = f"`{issue.test.name or issue.test.file}`", f"`#{issue.number}`"
        if run_test is None or time.monotonic() > deadline:
            touched.append(issue.number)
            checks.append(f"The patch changes {name}, issue {ref}'s test; "
                          "there was no time left to verify that it fixes it.")
            continue
        plan = plan_for(repo_dir, (issue.test,), scheduled=task.failure.scheduled, job=job)
        if plan is None:
            touched.append(issue.number)
            checks.append(f"The patch changes {name}, issue {ref}'s test, which cannot be run on this runner.")
            continue
        shown = _patch_fixes(plan, candidate, repo_dir, run_test, deadline)
        (fixed if shown else touched).append(issue.number)
        checks.append(
            f"Also fixes issue #{issue.number}: {name} failed without the patch and passed with it."
            if shown else
            f"The patch changes {name}, issue {ref}'s test, but it was not shown to fix it."
        )
    return tuple(fixed), tuple(touched)


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


def _patch_fixes(
    plan: ReproPlan,
    candidate: FixCandidate,
    repo_dir: str,
    run_test: TestFunc,
    deadline: float,
) -> bool:
    """Whether ``plan``'s test fails without ``candidate``'s patch and passes with it."""
    def run(command: str) -> TestRun:
        return run_test(repo_dir, command, _timeout(deadline))

    with _without_patch(repo_dir, candidate.patch):
        without = reproduce(plan, lambda command, _timeout_s: run(command), deadline=deadline)
    if not without.reproduced:
        return False
    loops = plan.verify_loops(without.deterministic)
    with_patch = run(plan.command(loops, under_load=without.under_load))
    return with_patch.passed and plan.ran(with_patch)


def _verify(
    candidate: FixCandidate,
    repo_dir: str,
    plan: ReproPlan | None,
    reproduction: Reproduction | None,
    run_test: TestFunc,
    deadline: float,
    checks: list[str],
    tests: tuple[FailedTest, ...] = (),
) -> str:
    """Run the candidate's tests; "" when they hold, else feedback for the agent.

    The agent's commands could leave ignored files the patch does not show (a
    prebuilt server binary that keeps ``make`` from rebuilding), so those go
    first: the build and tests run from source.
    """
    _remove_ignored(repo_dir)
    if plan is not None and reproduction is not None and reproduction.status is not ReproStatus.UNRUNNABLE:
        flaky = candidate.failure_type is FailureType.FLAKY
        failed = _verify_with_plan(plan, reproduction, repo_dir, run_test, deadline, checks, flaky=flaky)
        if failed or reproduction.reproduced or not candidate.test_command:
            return failed
        # Code's reproduction never failed; the agent's own command may show
        # the failure, and so whether the fix works.
        return _test_feedback(candidate, repo_dir, run_test, deadline, checks, tests)
    if candidate.test_command:
        return _test_feedback(candidate, repo_dir, run_test, deadline, checks, tests)
    checks.append("No test command could run this check on the bot's runner.")
    return ""


def _verify_with_plan(
    plan: ReproPlan,
    reproduction: Reproduction,
    repo_dir: str,
    run_test: TestFunc,
    deadline: float,
    checks: list[str],
    *,
    flaky: bool = False,
) -> str:
    # Two failing runs can be a frequent flake's luck: when the agent judged
    # the failure flaky, the fix must pass a flake's full loops.
    loops = plan.verify_loops(reproduction.deterministic and not flaky)
    command = plan.command(loops, under_load=reproduction.under_load)
    result = run_test(repo_dir, command, _timeout(deadline))
    if result.passed and not plan.ran(result):
        return (
            f"With your change, `{command}` passed without running the failing test. "
            "Do not rename, skip, or tag out the test: fix why it fails.\n\n"
            f"Its output:\n{result.output}"
        )
    if result.passed:
        if reproduction.reproduced:
            checks.append(f"With the fix, the reproduction passed {loops} loop(s): `{command}`.")
        else:
            checks.append(
                f"With the fix, `{command}` passed {loops} loop(s), but the failure never "
                "reproduced without it, so this does not show the fix works."
            )
        return ""
    if plan.failed(result):
        return (
            f"With your change, the reproduction still fails within {loops} loop(s): `{command}`.\n\n"
            f"Its output:\n{result.output}"
        )
    if result.timed_out:
        # Nothing is known about the change, and nothing the agent can do
        # about it: the attempt failed, and is retried.
        raise RuntimeError(f"`{command}` did not finish in the time left ({result.output}).")
    return (
        f"With your change, `{command}` could not build or run the tests (a build "
        "break, or a server or test client failing outside the test).\n\n"
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


@contextmanager
def _without_patch(repo_dir: str, patch: str) -> Iterator[None]:
    """The working tree with ``patch`` taken out, and put back afterwards."""
    run_git(repo_dir, "apply", "-R", "--whitespace=nowarn", "-", input=patch)
    try:
        yield
    finally:
        run_git(repo_dir, "apply", "--whitespace=nowarn", "-", input=patch)


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
    return max(60, min(TEST_TIMEOUT_S, int(deadline - time.monotonic())))


def _checked_candidate(task: FixTask, repo_dir: str, original_head: str, candidate: FixCandidate) -> FixCandidate:
    """``candidate`` with its patch built from the checkout, or a refusal.

    A commit is a refusal: the agent broke the one rule its checkout depends
    on. Anything else wrong with what it handed back raises
    :class:`_Rejected`, which goes back to it like a failing test.
    """
    current_head = git_output(repo_dir, "rev-parse", "HEAD").strip()
    if current_head != original_head:
        return _refusal(
            "The agent created a commit. CI-fix agents may edit files but may not commit or push."
        )

    changed_paths = worktree_changed_paths(repo_dir, strict=True)
    declared_paths = tuple(sorted(candidate.changed_paths))
    if candidate.action is CandidateAction.REFUSE:
        return replace(candidate, patch="", changed_paths=(), port_commit="", validation="", test_command="")

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
        if not _has_complete_diagnosis(candidate):
            raise _Rejected(_INCOMPLETE_DIAGNOSIS)
        if not is_full_sha(candidate.port_commit):
            raise _Rejected("The proposed upstream port is not a full commit SHA.")
        return replace(candidate, patch="", changed_paths=(), validation="", test_command="")

    if not changed_paths:
        raise _Rejected("The agent selected a patch but made no working-tree changes.")
    if not declared_paths:
        raise _Rejected(
            "The agent selected a patch but did not declare its changed paths."
        )
    if declared_paths != tuple(sorted(changed_paths)):
        raise _Rejected(
            "The working tree did not match the paths declared by the agent "
            f"(declared {declared_paths!r}, found {tuple(sorted(changed_paths))!r})."
        )
    if not _has_complete_diagnosis(candidate):
        raise _Rejected(_INCOMPLETE_DIAGNOSIS)
    try:
        patch = build_approved_patch(repo_dir, changed_paths, strict=True)
    except EmptyPatch:
        raise _Rejected("The agent selected a patch but produced no patch content.")
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
    )


def _test_feedback(
    candidate: FixCandidate, repo_dir: str, run_test: TestFunc, deadline: float, checks: list[str],
    tests: tuple[FailedTest, ...] = (),
) -> str:
    """Run the agent's test without and with its patch; "" when both hold.

    Otherwise returns what went wrong, for the agent's next round. On success
    ``checks`` gains a line for the maintainer report. A run killed for time
    shows nothing either way; when the command fails one of the job's
    ``tests`` without the patch, it must run them with it, not just exit 0.
    """
    command = candidate.test_command
    runs = FLAKY_TEST_RUNS if candidate.failure_type is FailureType.FLAKY else TEST_RUNS
    # Without the patch, the test must fail: one that passes does not
    # exercise the failure, and a pass with the patch would mean nothing. A
    # flaky failure may need several runs to show.
    baseline_runs = 0
    with _without_patch(repo_dir, candidate.patch):
        for baseline_runs in range(1, runs + 1):
            baseline = run_test(repo_dir, command, _timeout(deadline))
            if not baseline.passed:
                break
    if baseline.timed_out:
        return (
            f"`{command}` did not finish in the time left without your change ({baseline.output}), "
            "so it shows nothing. Give a test command that fails quickly on the unpatched code."
        )
    if baseline.passed:
        return (
            f"`{command}` passes {runs} time(s) without your change, so it does not reproduce "
            "the failure. Give a test command that fails on the unpatched code the way the job did.\n\n"
            f"Its output without your change:\n{baseline.output}"
        )
    named = tests if shows_failure_of(baseline.report, tests) else ()
    for run in range(1, runs + 1):
        result = run_test(repo_dir, command, _timeout(deadline))
        if not result.passed:
            return (
                f"With your change, `{command}` failed on run {run} of {runs}; "
                "it must pass every time.\n\n"
                f"Its output:\n{result.output}"
            )
        if named and not ran_tests(result.report, named):
            return (
                f"With your change, `{command}` passed without running the failing test. "
                "Do not rename, skip, or tag out the test: fix why it fails.\n\n"
                f"Its output:\n{result.output}"
            )
    checks.append(
        f"`{command}` failed without the patch (on run {baseline_runs}) and passed {runs} time(s) with it."
    )
    return ""


def run_test_command(repo_dir: str, command: str, timeout: int = TEST_TIMEOUT_S) -> TestRun:
    """Run ``command`` in ``repo_dir`` as the isolated account."""
    env = filter_env(PROCESS_BASICS)
    argv = ["bash", "-c", command]
    user = isolated_user()
    started = time.monotonic()
    try:
        if user:
            with handed_to(user, Path(repo_dir)) as home:
                result = _run_in_own_group(as_user(user, env, argv, home=home), repo_dir, None, timeout, user=user)
        else:
            result = _run_in_own_group(argv, repo_dir, env, timeout)
    except subprocess.TimeoutExpired:
        return TestRun(passed=False, output=f"timed out after {timeout}s", timed_out=True)
    output = strip_ansi((result.stdout + result.stderr).decode("utf-8", errors="replace"))
    logger.info("Test command exited %d after %.0fs", result.returncode, time.monotonic() - started)
    return TestRun(
        passed=result.returncode == 0,
        output=_excerpt(output),
        report=failure_report(output),
    )


def _run_in_own_group(
    argv: list[str], repo_dir: str, env: dict[str, str] | None, timeout: int, *, user: str = "",
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
    found = [at for at in (output.find(marker) for marker in FAILURE_MARKERS) if at >= 0]
    if not found:
        return output[-_TEST_OUTPUT_CHARS:]
    at = min(found)
    start = max(0, output.rfind("\n", 0, max(0, at - 500)) + 1)
    head = output[start:start + _TEST_OUTPUT_CHARS // 2]
    return f"{head}\n[... output trimmed ...]\n{tail}"


def _refusal(summary: str, verification: str = "") -> FixCandidate:
    return FixCandidate(action=CandidateAction.REFUSE, summary=summary, verification=verification)


def _has_complete_diagnosis(candidate: FixCandidate) -> bool:
    return all((candidate.failing_check, candidate.root_cause, candidate.reasoning))


_INCOMPLETE_DIAGNOSIS = (
    "The agent did not provide a complete failing check, root cause, and change rationale."
)


class _Rejected(Exception):
    """What the agent handed back cannot be used as it is; it may revise it."""


def bounded_job_log(job_log: str) -> str:
    if len(job_log) <= MAX_JOB_LOG_CHARS:
        return job_log
    marker = "\n\n[... job log truncated; keeping the final failure output ...]\n\n"
    tail_chars = MAX_JOB_LOG_CHARS - JOB_LOG_HEAD_CHARS - len(marker)
    return job_log[:JOB_LOG_HEAD_CHARS] + marker + job_log[-tail_chars:]
