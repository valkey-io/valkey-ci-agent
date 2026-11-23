"""Prepare one CI fix.

Preparation runs in this order, every step on one checkout: a disposable
clone, or the checkout of the failed CI job the fix is prepared in, after
that job's own setup and build (``scripts/ci_fix/job.sh``):

1. **Find existing fixes**: search history for a merged change that names the
   failing test, changes its file, or references its issue
   (:mod:`scripts.ci_fix.prior_fixes`).
2. **Fix**: the CI job's log is the evidence of what fails. One agent
   session, told what step 1 found, the job log and the repository's test
   conventions, ports an existing fix or writes a patch.
3. **Verify and review**: the candidate (a port is cherry-picked into the
   checkout first) must build and pass the failing test, once and then
   looped, the way its CI job runs it (:mod:`scripts.ci_fix.verification`
   plans the command; when the test cannot run alone, its own block, then
   its whole file), and a separate read-only review must approve it.
   Otherwise the result goes back to the agent, up to ``MAX_ROUNDS`` times.

On a backport branch, and for a failure no command here can run, a change
already merged upstream comes first: a port, or a patch that adapts one
(``FixContext.upstream_first``). Where none fixes the failure, a fix to the
test or the test harness is written on the branch, and reported as one the
default branch does not have; product source changes there only by a commit
merged upstream.

An issue a committer labelled has no failed job (:func:`prepare_change`). Its
evidence is made first: the agent writes a test, and code has to see it fail
on the unchanged code, every run, on its own assertion. That test is then
frozen, its failing run is the log of step 2, and step 3 checks more: the
test passing looped and under AddressSanitizer, every changed test file in
full, and a reviewer who looks for a case the change still gets wrong and
writes it as a test that code runs.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Callable, Protocol, TypeVar

from scripts.ai.runtime import SESSION_SUFFIX
from scripts.ci_fix.agent import (
    API_ERROR_PAUSE_S,
    AgentTimeout,
    ModelUnavailable,
    Review,
    find_counterexample,
    review_candidate,
    run_fix_agent,
    write_failing_test,
)
from scripts.ci_fix.models import (
    CandidateAction,
    FailedTest,
    FixCandidate,
    FixContext,
    FixPolicy,
    FixTarget,
    FixTask,
    IssueRequest,
    TargetKind,
    UpstreamRun,
)
from scripts.ci_fix.policy import target_capabilities
from scripts.ci_fix.prior_fixes import (
    failing_test_history,
    find_prior_fixes,
    paths_in_log,
    pending_fixes,
    render_prior_fixes,
    sibling_branch_changes,
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
    default_commands,
    failure_report,
    plan_for,
    shows_crash,
    shows_failure_of,
    to_the_end,
    upstream_plan,
)
from scripts.common.git_clone import is_full_sha, shallow_clone_at_sha
from scripts.common.isolation import as_user, handed_to, isolated_user, kill_all
from scripts.common.logging_utils import log_group
from scripts.common.proc import (
    BOT_EMAIL,
    BOT_NAME,
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

# Where the bot's guide for each repository lives.
_GUIDES_DIR = Path(__file__).resolve().parent / "guides"
# The repository's own guidelines, given to the agent and the reviewer at the
# end of the bot's guide. Not CONTRIBUTING.md, which is about process (the
# DCO, design documents), nor .github/instructions/, which summarizes
# DEVELOPMENT_GUIDE.md for another review tool.
_REPO_DOCS = (
    "AGENTS.md",
    "DEVELOPMENT_GUIDE.md",
    "tests/README.md",
    "src/unit/README.md",
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
        return _refusal("The exact failed-job log is unavailable. No change was made.")

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
            run_test=run_test if runs_tests else None, deadline=deadline, job=job,
        )


# A fix for the default branch has to pass this many loops there, or every
# loop its test can run.
UPSTREAM_MIN_LOOPS = 10


def upstream_task(task: FixTask, sha: str) -> FixTask:
    """The task of making ``task``'s fix for ``task.target.upstream_branch``, at its commit ``sha``.

    It runs in that branch's own CI job, the one named like the failed job in
    the same workflow file at ``sha`` (``scripts/ci_fix/run.py`` ``plan_upstream``).
    """
    branch, release = task.target.upstream_branch, task.failure.base_branch
    return replace(
        task,
        failure=replace(task.failure, base_branch=branch, head_sha=sha, workflow_sha="", description=(
            f"{task.failure.description} It failed on release branch `{release}`, where the fix below was made. "
            f"The working tree is the default branch, `{branch}`, which has the failing test: make the same fix "
            f"here, written for this branch's code, or refuse and say why this branch needs none."
        )),
        target=FixTarget(
            kind=TargetKind.DRAFT_PR, repo_full_name=task.target.repo_full_name,
            base_repo_full_name=task.target.base_repo_full_name, branch=f"{branch}-fix", expected_sha=sha,
        ),
        policy=FixPolicy.GENERAL,
        open_fixes=(),
    )


def prepare_upstream(
    task: FixTask,
    job_log: str,
    release: FixCandidate,
    agent_func: AgentFunc = run_fix_agent,
    review_func: ReviewFunc | None = None,
    test_func: TestFunc | None = None,
    *,
    sha: str,
    repo_dir: str = "",
    job: JobCommands | None = None,
) -> FixCandidate:
    """``release``, the release branch's fix for ``task``, made for ``task.target.upstream_branch`` at ``sha``.

    A sweep's failure in a test the default branch shares is fixed there
    once, and backported. ``repo_dir`` is the default branch's own CI job's
    checkout, after that job's setup and build, or, when that job cannot
    host a fix, a clone is made on the bot's runner. The release branch's
    patch is applied there, and has to build and pass, looped at least
    ``UPSTREAM_MIN_LOOPS`` times. When it does not apply, or does not pass,
    the agent makes the same fix for the default branch's code, which is
    checked and reviewed like any other. Either is ``release.upstream``. When
    the agent finds the default branch needs none, ``release`` is the release
    branch's own fix, and says why in its verification; when no fix can be
    made there, ``release.upstream_refusal`` says why.
    """
    branch = task.target.upstream_branch
    on_branch = upstream_task(task, sha)
    with checkout(on_branch, repo_dir) as (repo_dir, _head):
        deadline = time.monotonic() + PREPARE_BUDGET_S
        run_test = test_func or run_test_command
        review = review_func or review_candidate
        tests = locate_unit_tests(repo_dir, failed_tests(task, job_log)) or release.failing_tests
        failed = ""
        try:
            run_git(repo_dir, "apply", "--whitespace=nowarn", "-", input=release.patch)
        except subprocess.CalledProcessError:
            logger.info("The fix does not apply to %s at %s as it is", branch, sha[:12])
        else:
            plan = plan_for(repo_dir, tests, scheduled=task.failure.scheduled, job=job)
            checks = [f"The fix applies to `{branch}` at `{sha[:12]}`, and was run there too."]
            if plan is None:
                failed = f"No command here can run the failing test on `{branch}`."
            else:
                failed = _verify(
                    release, repo_dir, plan, run_test, deadline, checks, min_loops=UPSTREAM_MIN_LOOPS,
                ) or _verify_added_tests(on_branch, release, repo_dir, run_test, deadline, checks, job)
            if not failed:
                return replace(release, upstream=UpstreamRun(
                    sha=sha, tests=tests, verification="\n".join(checks),
                    patch=release.patch, changed_paths=release.changed_paths,
                ))
            restore(repo_dir)
        made = _made_upstream(
            on_branch, task.failure.base_branch, repo_dir, release, tests, job_log, failed,
            agent_func=agent_func, review=review, run_test=run_test, deadline=deadline, job=job,
        )
    if made.agent_refused:
        # The default branch needs no fix: the release branch's is its own.
        return replace(release, verification="\n".join(filter(None, (
            release.verification, f"On `{branch}`, which has the failing test, the agent made no fix: {made.summary}",
        ))))
    if made.action is not CandidateAction.PATCH or made.not_run:
        return replace(release, upstream_refusal=(
            f"`{branch}` has the failing test, but no fix for it could be made there. {made.summary}"
        ))
    return replace(release, upstream=UpstreamRun(
        sha=sha, tests=tests, verification=made.verification, patch=made.patch, changed_paths=made.changed_paths,
        title=made.title or release.title, reasoning=made.reasoning,
    ))


def _made_upstream(
    task: FixTask,
    release_branch: str,
    repo_dir: str,
    candidate: FixCandidate,
    tests: tuple[FailedTest, ...],
    job_log: str,
    failed: str,
    *,
    agent_func: AgentFunc,
    review: ReviewFunc,
    run_test: TestFunc,
    deadline: float,
    job: JobCommands | None,
) -> FixCandidate:
    """The agent's fix for ``task``, the default branch's: ``candidate``, the release branch's, made for its code."""
    branch, sha = task.failure.base_branch, task.failure.head_sha
    context, plan = _establish(
        task, repo_dir, tests, paths_in_log(repo_dir, job_log), runs_tests=True, job=job, job_log=job_log,
    )
    refusal = _unverifiable_refusal(context, plan)
    if refusal is not None:
        return refusal
    shown = "\n\n".join(part for part in (
        f"The fix made on `{release_branch}`, which passed there:",
        f"Root cause: {candidate.root_cause}" if candidate.root_cause else "",
        f"The fix: {candidate.reasoning}" if candidate.reasoning else "",
        f"```diff\n{candidate.patch}\n```",
        f"On `{branch}` as it is, it did not pass:\n\n{failed}" if failed else
        f"It does not apply to `{branch}` as it is: this branch's code differs.",
    ) if part)
    return _fix_loop(
        task, repo_dir, sha, job_log, replace(context, release_fix=shown), plan,
        agent_func=agent_func, review=review, run_test=run_test, deadline=deadline, job=job,
        min_loops=UPSTREAM_MIN_LOOPS,
    )


def _unverifiable_refusal(context: FixContext, plan: VerifyPlan | None) -> FixCandidate | None:
    """Refuse when no command can run the failing test, where a fix may not be published on review alone."""
    if plan is not None:
        return None
    return _refusal(
        "No command here can run the failing check, so a fix could not be checked. "
        "No change was made.",
        verification="\n".join(_established_checks(context)),
    )


# A labelled issue's evidence is a test written first. It has to fail this
# many runs in a row on the unchanged code, and the agent gets this many
# attempts at writing one.
EVIDENCE_RUNS = 3
EVIDENCE_ATTEMPTS = 5
# What establishing the evidence may use of the budget, and one session of it.
_EVIDENCE_BUDGET_S = 90 * 60
_MAX_EVIDENCE_SESSION_S = 30 * 60
# The most lines of product source a labelled issue's change may add and
# remove: what a reviewer reads in one sitting. Tests are not counted.
MAX_PRODUCT_LINES = 300
# A change has to pass its test this many loops, or every loop the test can run.
CHANGE_MIN_LOOPS = 10
# What formatting a change, or regenerating a file from it, may take.
_TIDY_TIMEOUT_S = 5 * 60


class UnexpectedCrash(Exception):
    """A server crashed on unchanged code, in a run for an issue that reports no crash.

    It may be a vulnerability, which is reported in private: the attempt
    ends, and nothing about it is published or logged.
    """


@dataclass(frozen=True)
class _Evidence:
    """The tests that code saw fail before any change was made."""

    tests: tuple[FailedTest, ...]
    # The plan that reached them, which the change is then checked with.
    plan: VerifyPlan
    # The tests as a patch on the base commit, their files, and one failing
    # run's output; empty for a pull request's own tests, which are its head's.
    patch: str
    paths: tuple[str, ...]
    output: str
    # What code saw, for the report.
    check: str
    # The session's own reading of the issue: a feature or a bug, and its acceptance criteria.
    written: FixCandidate


def prepare_change(
    task: FixTask,
    agent_func: AgentFunc = run_fix_agent,
    review_func: ReviewFunc | None = None,
    test_func: TestFunc | None = None,
    *,
    evidence_func: AgentFunc = write_failing_test,
    repo_dir: str = "",
    job: JobCommands | None = None,
) -> FixCandidate:
    """The change the labelled issue ``task.request`` asks for, or why there is none.

    No job failed, so the evidence comes first: the agent writes a test, for
    a feature from acceptance criteria it reads in the issue, and code has to
    see it fail ``EVIDENCE_RUNS`` runs in a row on the unchanged code
    (:func:`_establish_evidence`). The change is then made and checked like a
    CI fix, in the same loop, with that test frozen and its failing run as
    the log, and has to pass more (:func:`_change_checks`,
    :func:`_counterexample_review`). The candidate's patch is the test and
    the change together, on the base commit.

    A server that crashes on the unchanged code, where the issue reports no
    crash, ends the attempt with nothing said about it (:class:`UnexpectedCrash`).
    """
    with checkout(task, repo_dir) as (repo_dir, base), _ending_at_a_crash():
        deadline = time.monotonic() + PREPARE_BUDGET_S
        run_test, stopping_at_crash = _runners(task, test_func)
        guide = _change_guide(task, repo_dir)
        evidence = _establish_evidence(
            task, repo_dir, base, guide,
            evidence_func=evidence_func, run_test=stopping_at_crash, deadline=deadline, job=job,
        )
        if isinstance(evidence, FixCandidate):
            return evidence
        made = _make_change(
            task, repo_dir, guide, evidence, agent_func=agent_func, review_func=review_func,
            run_test=run_test, stopping_at_crash=stopping_at_crash, deadline=deadline, job=job,
        )
        if made.action is not CandidateAction.PATCH:
            return made
        # What is published is the test and the change that makes it pass, as one patch on the base commit.
        paths = tuple(sorted({*evidence.paths, *made.changed_paths}))
        untracked = git_paths(repo_dir, "ls-files", "--others", "--exclude-standard", "-z", "--", *paths)
        if untracked:
            run_git(repo_dir, "add", "--intent-to-add", "--", *untracked)
        patch = git_output(
            repo_dir, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary", base, "--", *paths,
        )
        return replace(made, patch=patch, changed_paths=paths)


def prepare_review(
    task: FixTask,
    agent_func: AgentFunc = run_fix_agent,
    review_func: ReviewFunc | None = None,
    test_func: TestFunc | None = None,
    *,
    repo_dir: str = "",
    job: JobCommands | None = None,
) -> FixCandidate:
    """A committer's review comments (``task.request.review``) applied to the bot's own pull request.

    The checkout is the pull request's head. The tests it adds are the
    evidence: the change is checked with them as any labelled issue's is,
    and because a review may change a test, they also have to fail again
    with the product change taken out (:func:`_fails_without`). The
    candidate's patch is this round's change alone, on that head.
    """
    request = _request(task)
    with checkout(task, repo_dir) as (repo_dir, _head), _ending_at_a_crash():
        deadline = time.monotonic() + PREPARE_BUDGET_S
        run_test, stopping_at_crash = _runners(task, test_func)
        tests = locate_unit_tests(repo_dir, added_tests(request.earlier))
        plan = plan_for(repo_dir, tests, scheduled=False, job=job) if tests else None
        if plan is None:
            return _refusal(
                "This pull request adds no test that code can run, so a change to it could not be checked. "
                "No change was made."
            )
        evidence = _Evidence(
            tests=tests, plan=plan, patch="", paths=(), output="", check="",
            written=FixCandidate(action=CandidateAction.PATCH),
        )
        return _make_change(
            task, repo_dir, _change_guide(task, repo_dir), evidence,
            agent_func=agent_func, review_func=review_func, run_test=run_test, stopping_at_crash=stopping_at_crash,
            deadline=deadline, job=job,
            first_check=lambda checks: _fails_without(request, repo_dir, plan, stopping_at_crash, deadline, checks),
        )


def _request(task: FixTask) -> IssueRequest:
    if task.request is None:
        raise ValueError("a change is prepared for a labelled issue")
    return task.request


def _change_guide(task: FixTask, repo_dir: str) -> str:
    """The bot's guide for a change, ending with the repository's own guidelines."""
    guide = guide_for(task.failure.repo_full_name)
    return guide + repository_guidelines(repo_dir) if guide else ""


def _make_change(
    task: FixTask,
    repo_dir: str,
    guide: str,
    evidence: _Evidence,
    *,
    agent_func: AgentFunc,
    review_func: ReviewFunc | None,
    run_test: TestFunc,
    stopping_at_crash: TestFunc,
    deadline: float,
    job: JobCommands | None,
    first_check: Callable[[list[str]], str] | None = None,
) -> FixCandidate:
    """The change that makes ``evidence``'s tests pass, checked and reviewed, on the checkout's HEAD.

    HEAD holds the tests already (the evidence is a commit of the checkout's
    own, or the pull request's head), so that every round's patch is the
    change alone. ``first_check`` runs before the other checks of a round's
    change, and like them returns feedback, or "" after noting what it saw.
    """
    _ignore_generated_copies(repo_dir)
    head = git_output(repo_dir, "rev-parse", "HEAD").strip()
    sanitized = _with_sanitizer(job)
    plan = evidence.plan
    criteria = "".join(f"- {criterion}\n" for criterion in evidence.written.criteria)
    reviewing = bool(_request(task).review)
    context = FixContext(
        guide=guide,
        evidence_check=evidence.check,
        evidence_detail="" if reviewing else "\n\n".join(part for part in (
            evidence.check,
            f"The acceptance criteria the test was written from:\n\n{criteria}" if criteria else "",
            f"The test, which is in the working tree:\n\n```diff\n{evidence.patch}\n```\n",
        ) if part),
        frozen_patch=evidence.patch,
        # A review may ask for the test itself to change.
        frozen_tests=() if reviewing else evidence.tests,
        verification=(
            f"Code checks your change with `{plan.command(plan.loops)}`: the build must pass and the test must "
            f"pass, once and then looped, at least {min(CHANGE_MIN_LOOPS, plan.loops)} times. It then runs "
            "every test file the change touches in full, and the test once in a build with AddressSanitizer."
        ),
        verified_by_code=True,
    )

    def checks(candidate: FixCandidate, found: list[str]) -> str:
        failed = first_check(found) if first_check is not None else ""
        return failed or _change_checks(
            candidate, repo_dir, evidence, found, run_test=run_test, deadline=deadline, job=job, sanitized=sanitized,
        )

    made = _fix_loop(
        task, repo_dir, head, evidence.output, context, plan,
        agent_func=_tidied(agent_func, run_test),
        review=review_func or _counterexample_review(task, stopping_at_crash, deadline, sanitized),
        run_test=run_test, deadline=deadline, job=job, min_loops=CHANGE_MIN_LOOPS, also=checks,
    )
    return replace(
        made, feature=evidence.written.feature, criteria=evidence.written.criteria, test_patch=evidence.patch,
    )


def _runners(task: FixTask, test_func: TestFunc | None) -> tuple[TestFunc, TestFunc]:
    """How a labelled issue's commands run, both off the network: (run a check of the change, run and stop at a crash).

    The second is for any run whose crash cannot be pinned on the bot's
    change: the test's first runs, on unchanged code, and a reviewer's test
    for a case nobody has tried. A server that crashes there, where the
    issue reports no crash, may be a vulnerability, and the attempt ends
    before anything about the run is logged. A crash in the change's own
    checks is that change failing them, and goes back to its author.
    """
    request = _request(task)
    run = test_func or partial(run_test_command, offline=True, quiet=True)

    def checking(repo_dir: str, command: str, timeout: int) -> TestRun:
        result = run(repo_dir, command, timeout)
        _log_run(command, result)
        return result

    def stopping_at_crash(repo_dir: str, command: str, timeout: int) -> TestRun:
        result = run(repo_dir, command, timeout)
        if result.crashed and not request.crash_report:
            raise UnexpectedCrash
        _log_run(command, result)
        return result

    return checking, stopping_at_crash


def _log_run(command: str, result: TestRun) -> None:
    """Log a quiet command once its caller knows it may be shown."""
    logger.info("Ran `%s`", command)
    if not result.passed and not result.timed_out:
        _log_failed_output(result.output)


def _log_failed_output(output: str) -> None:
    # The only record of why a command failed, once the run is over.
    with log_group("Failed test command output"):
        logger.info("%s", output)


@contextmanager
def _ending_at_a_crash() -> Iterator[None]:
    """End the attempt at an :class:`UnexpectedCrash`, as one that stopped early, with nothing kept of it.

    The sessions kept with the run's evidence records go
    (``AgentProfile.keeps_session``): one of them wrote the test that
    crashes the server. What is left is a failed attempt like any other.
    """
    try:
        yield
    except UnexpectedCrash:
        directory = os.environ.get("CI_AGENT_EVIDENCE_DIR", "")
        for session in Path(directory).glob(f"*{SESSION_SUFFIX}") if directory else ():
            session.unlink(missing_ok=True)
        raise RuntimeError("the run stopped before it had a result") from None


_T = TypeVar("_T")


def _retried(
    session: Callable[[str, int], _T], repo_dir: str, feedback: str, timeout: Callable[[], int], label: str,
) -> _T:
    """Run ``session`` with ``feedback``; when it ends on a model API error, once more.

    The session judged nothing, and may have died mid-edit: the second one
    runs on the tree as the working commit has it.
    """
    try:
        return session(feedback, timeout())
    except ModelUnavailable as exc:
        logger.warning("%s: %s; running the session once more", label, exc)
        restore(repo_dir)
        time.sleep(API_ERROR_PAUSE_S)
        return session(feedback.replace(_KEPT_TREE, _RESET_TREE), timeout())


_COMMITTED = "The agent created a commit. CI-fix agents may edit files but may not commit or push."


def _establish_evidence(
    task: FixTask,
    repo_dir: str,
    base: str,
    guide: str,
    *,
    evidence_func: AgentFunc,
    run_test: TestFunc,
    deadline: float,
    job: JobCommands | None,
) -> _Evidence | FixCandidate:
    """The test that shows the issue, committed in the checkout, or the refusal there is none.

    A refusal is the agent's own (a feature whose requirements the issue
    does not settle, an issue that is not a bug), or code's after
    ``EVIDENCE_ATTEMPTS`` tests that did not qualify.
    """
    request = _request(task)
    context = FixContext(guide=guide)
    until = min(deadline, time.monotonic() + _EVIDENCE_BUDGET_S)
    feedback, last = "", "no test was written"
    for attempt in range(1, EVIDENCE_ATTEMPTS + 1):
        # Running out of time says nothing about the issue: it is a failed attempt, which is retried.
        if until - time.monotonic() < _MIN_ROUND_S:
            raise RuntimeError(f"Ran out of time after {attempt - 1} attempt(s) at the test. Last: {last}")
        try:
            written = _retried(
                lambda feedback, timeout: evidence_func(
                    task, repo_dir, "", feedback=feedback, context=context, timeout=timeout,
                ),
                repo_dir, feedback,
                lambda: max(60, min(int(until - time.monotonic()) // 2, _MAX_EVIDENCE_SESSION_S)),
                f"Evidence attempt {attempt}",
            )
        except AgentTimeout:
            return _refusal(
                "The agent investigated for its whole session without writing a test that shows the issue. "
                "No change was made."
            )
        if git_output(repo_dir, "rev-parse", "HEAD").strip() != base:
            return _refusal(_COMMITTED)
        if written.action is CandidateAction.REFUSE:
            return replace(written, patch="", changed_paths=(), port_commit="", agent_refused=True)
        evidence, problem = _evidence_of(
            request, repo_dir, base, written, run_test=run_test, deadline=deadline, job=job,
        )
        if evidence is not None:
            return evidence
        last = problem.splitlines()[0]
        tree = _KEPT_TREE if worktree_changed_paths(repo_dir, strict=True) else _RESET_TREE
        feedback = f"{problem}\n\n{tree}"
        logger.info("Evidence attempt %d: %s", attempt, last)
    return _refusal(
        f"No test that fails on the unchanged code was written in {EVIDENCE_ATTEMPTS} attempts, so no change was "
        f"made. Last: {last}"
    )


def _evidence_of(
    request: IssueRequest,
    repo_dir: str,
    base: str,
    written: FixCandidate,
    *,
    run_test: TestFunc,
    deadline: float,
    job: JobCommands | None,
) -> tuple[_Evidence | None, str]:
    """The test in the working tree as evidence, committed: (the evidence, ""), or (None, what is wrong with it).

    Only test files may have changed, and the test has to fail every run,
    on its own assertion: a build that breaks, a command that is missing or
    a server that does not start shows nothing about the issue. A crash
    report's test may fail by the server crashing.
    """
    paths = worktree_changed_paths(repo_dir, strict=True)
    product = [path for path in paths if not is_test_path(path)]
    if product:
        return None, (
            "This step writes the test alone, and you changed " + ", ".join(f"`{path}`" for path in product)
            + ": undo that. The change comes after code has seen the test fail."
        )
    # Formatted now, as the change will be: the change may not rewrite these lines later.
    _tidy(repo_dir, run_test)
    try:
        patch = build_approved_patch(repo_dir, paths, strict=True)
    except EmptyPatch:
        return None, "You wrote no test: no file changed."
    rejection = change_rejection(repo_dir, paths, patch)
    if rejection:
        return None, rejection
    # The tests the patch adds, as code reads them; else the ones the agent says it changed.
    added = added_tests(patch, limit=None)
    if len(added) > _MAX_TESTS:
        return None, (
            f"You wrote {len(added)} tests, and code runs at most {_MAX_TESTS}, each of which has to fail: "
            "write fewer, each checking more."
        )
    tests = locate_unit_tests(repo_dir, added or tuple(
        test for test in written.failing_tests if test.name and test.file in paths
    )[:_MAX_TESTS])
    plan = plan_for(repo_dir, tests, scheduled=False, job=job) if tests else None
    if plan is None:
        return None, (
            "No command here can run the test you wrote. Write a Tcl test with a literal name under `tests/unit/` "
            "or `tests/integration/`, or a C unit test under `src/unit/`, and name it in `failing_tests`."
        )
    built = _run(run_test, repo_dir, plan.build, deadline)
    if not built.passed:
        return None, f"With your test, `{plan.build}` does not build.\n\nIts output:\n{built.output}"
    # To the end, not to the first failure: every test has to be seen failing.
    plan, runs = _runs_before(to_the_end(plan, repo_dir), request, repo_dir, run_test, deadline)
    problem = _not_failing(plan, runs, request)
    if problem:
        return None, problem
    if tree_patch(repo_dir) != patch:
        return None, _TREE_CHANGED
    run_git(repo_dir, "add", "--", *paths)
    run_git(
        repo_dir, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}", "-c", "commit.gpgsign=false",
        "commit", "--quiet", "--no-verify", "-m", "Add the test that shows the issue",
    )
    crashed = " with the server crashing, as the issue reports" if any(run.crashed for run in runs) else ""
    command = plan.quoted(1)
    return _Evidence(
        tests=tests, plan=plan, patch=patch, paths=paths, output=runs[0].output, written=written,
        check=f"Before any change, on `{base[:12]}`, `{command}` failed {EVIDENCE_RUNS} of {EVIDENCE_RUNS} runs{crashed}.",
    ), ""


def _not_failing(plan: VerifyPlan, runs: list[TestRun], request: IssueRequest) -> str:
    """The agent's feedback for runs that are not evidence, or "" when every test failed every run.

    ``runs`` is :func:`_runs_before`'s: one run when it did not show the
    tests failing.
    """
    command, first = plan.quoted(1), runs[0]
    if first.passed and plan.ran(first):
        return (
            f"Your test passes on the unchanged code (`{command}`), so it does not show the issue.\n\n"
            f"Its output:\n{first.output}"
        )
    if first.passed:
        return (
            f"`{command}` passed without running your test: its name has to be one the command can select.\n\n"
            f"Its output:\n{first.output}"
        )
    if not _shows_failure(plan, first, request):
        return (
            f"`{command}` failed, but not on your test's own assertion. A build that breaks, a missing command, "
            f"or a server that does not start is not the issue.\n\nIts output:\n{first.output}"
        )
    failed = sum(_shows_failure(plan, run, request) for run in runs)
    if failed < EVIDENCE_RUNS:
        return (
            f"Your test failed {failed} of {EVIDENCE_RUNS} runs of `{command}` on the unchanged code. It has to "
            "fail every run: make it deterministic, by waiting for the state it depends on."
        )
    passing = [
        test for test in plan.tests
        if not all(shows_failure_of(run.report, (test,), attributed=True) or run.crashed for run in runs)
    ]
    if passing:
        return (
            "Every test you write has to fail on the unchanged code, and "
            + ", ".join(f"`{test.name}`" for test in passing) + f" did not, in every run of `{command}`. "
            f"A test that passes already shows nothing about the issue: drop it.\n\nIts output:\n{first.output}"
        )
    return ""


def _runs_before(
    plan: VerifyPlan, request: IssueRequest, repo_dir: str, run_test: TestFunc, deadline: float,
) -> tuple[VerifyPlan, list[TestRun]]:
    """Run ``plan``'s tests on code that should fail them: the plan that reached them, and its runs.

    A plan that cannot run a test alone is followed by its fallbacks, as a
    fix's verification is. One run when it does not show the tests failing,
    else ``EVIDENCE_RUNS``.
    """
    while True:
        first = _run(run_test, repo_dir, plan.command(1), deadline)
        if _shows_failure(plan, first, request) or (first.passed and plan.ran(first)) or plan.fallback is None:
            break
        plan = plan.fallback
    if not _shows_failure(plan, first, request):
        return plan, [first]
    return plan, [first, *(_run(run_test, repo_dir, plan.command(1), deadline) for _ in range(EVIDENCE_RUNS - 1))]


def _shows_failure(plan: VerifyPlan, run: TestRun, request: IssueRequest) -> bool:
    """Whether ``run`` failed on the planned tests themselves or, for a crash report, by the server crashing."""
    return not run.passed and (plan.failed(run) or (request.crash_report and run.crashed))


def _fails_without(
    request: IssueRequest, repo_dir: str, plan: VerifyPlan, run_test: TestFunc, deadline: float, checks: list[str],
) -> str:
    """Whether the pull request's tests still fail once its product change is taken out; feedback when not.

    A review may ask for the test to change, and a test is evidence only
    while it fails without the change it comes with. The product change, from
    the default branch to the working tree, is taken out for the runs and put
    back after them.
    """
    default_ref = default_branch_ref(repo_dir)
    if not default_ref:
        raise RuntimeError("the checkout has no default branch to take the pull request's change from")
    base = git_output(repo_dir, "merge-base", default_ref, "HEAD").strip()
    paths = tuple(
        path for path in git_paths(repo_dir, "diff", "--no-renames", "--name-only", "-z", base)
        if not is_test_path(path)
    )
    if not paths:
        return ""
    product = git_output(repo_dir, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--binary", base, "--", *paths)
    run_git(repo_dir, "apply", "-R", "--whitespace=nowarn", "-", input=product)
    try:
        plan, runs = _runs_before(plan, request, repo_dir, run_test, deadline)
    finally:
        run_git(repo_dir, "apply", "--whitespace=nowarn", "-", input=product)
    failed = sum(_shows_failure(plan, run, request) for run in runs)
    if failed < EVIDENCE_RUNS:
        return (
            f"Without the product change, the pull request's tests failed {failed} of {EVIDENCE_RUNS} runs of "
            f"`{plan.quoted(1)}`. A test has to fail every run without the change it comes with."
        )
    checks.append(f"Without the product change, `{plan.quoted(1)}` failed {EVIDENCE_RUNS} of {EVIDENCE_RUNS} runs.")
    return ""


def _change_checks(
    candidate: FixCandidate,
    repo_dir: str,
    evidence: _Evidence,
    checks: list[str],
    *,
    run_test: TestFunc,
    deadline: float,
    job: JobCommands | None,
    sanitized: JobCommands,
) -> str:
    """What a labelled issue's change has to pass besides its own test; "" when it does, else feedback.

    Every Tcl test file it touches, in full: a change that fixes one test can
    break its neighbours. Then the test once in a build with
    AddressSanitizer, last, because the build stays as it leaves it.
    """
    files = tuple(dict.fromkeys(
        path for path in (*evidence.paths, *candidate.changed_paths)
        if path.endswith(".tcl") and Path(repo_dir, path).is_file()
    ))
    whole = plan_for(repo_dir, tuple(FailedTest("", path) for path in files), scheduled=False, job=job)
    if whole is not None:
        command = whole.command(1)
        result = _run(run_test, repo_dir, command, deadline)
        if not result.passed:
            return (
                f"Every test file the change touches has to pass in full, and `{command}` does not.\n\n"
                f"Its output:\n{result.output}"
            )
        checks.append(f"With the fix, `{command}` passed.")
    under = plan_for(repo_dir, evidence.tests, scheduled=False, job=sanitized)
    if under is None:
        raise RuntimeError("the tests the change was checked with cannot be planned in a sanitizer build")
    failed = _verify_with_plan(replace(under, single_run=True), repo_dir, run_test, deadline, checks)
    return f"In a build with AddressSanitizer: {failed}" if failed else ""


_MAKE_RE = re.compile(r"^(\s*make)(?=\s|$)", re.MULTILINE)


def _with_sanitizer(job: JobCommands | None) -> JobCommands:
    """``job``'s commands, built with AddressSanitizer; plain ones where no job hosts the change."""
    if job is None:
        job = replace(default_commands(scheduled=False), unittest="make test-unit")
    return replace(
        job, build=_MAKE_RE.sub(r"\1 SANITIZER=address", job.build),
        unittest=f"{job.unittest} SANITIZER=address" if job.unittest else "",
    )


def _tidied(agent_func: AgentFunc, run_test: TestFunc) -> AgentFunc:
    """``agent_func``, with what it changed formatted and its generated files regenerated."""

    def author(task: FixTask, repo_dir: str, job_log: str, **options: object) -> FixCandidate:
        candidate = agent_func(task, repo_dir, job_log, **options)
        if candidate.action is CandidateAction.PATCH:
            _tidy(repo_dir, run_test)
        return candidate

    return author


def _tidy(repo_dir: str, run_test: TestFunc) -> None:
    """Format the lines the agent changed in C sources and regenerate the command table, as Valkey's CI checks both.

    Code does it: the agent has no shell, and neither is a judgement. Only
    the changed lines are formatted: the rest of a file already passes the
    repository's check, and another release of the formatter would rewrite
    lines the change has nothing to do with.
    """
    paths = worktree_changed_paths(repo_dir, strict=True)
    sources = [
        path for path in paths
        if path.startswith("src/") and path.endswith((".c", ".h", ".cpp", ".hpp")) and Path(repo_dir, path).is_file()
    ]
    commands = []
    # By its path: switching accounts may replace PATH, and with it the release that was installed.
    formatter = shutil.which("clang-format")
    if sources and formatter:
        changed = changed_lines(tree_patch(repo_dir))
        for path in sources:
            lines = " ".join(f"--lines={first}:{last}" for first, last in _ranges(changed.get(path, set())))
            if lines:
                commands.append(f"{shlex.quote(formatter)} -i {lines} -- {shlex.quote(path)}")
    elif sources:
        logger.warning("clang-format is not installed: the change is not formatted")
    if any(fnmatch.fnmatch(path, "src/commands/*.json") for path in paths):
        commands.append("python3 utils/generate-command-code.py")
    for command in commands:
        if not run_test(repo_dir, command, _TIDY_TIMEOUT_S).passed:
            logger.warning("`%s` failed: the pull request's own CI checks what it would have done", command)


def _ranges(lines: set[int]) -> list[tuple[int, int]]:
    """``lines`` as runs of consecutive line numbers: (first, last) of each."""
    ranges: list[tuple[int, int]] = []
    for line in sorted(line for line in lines if line > 0):
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], line)
        else:
            ranges.append((line, line))
    return ranges


def _counterexample_review(
    task: FixTask,
    run_test: TestFunc,
    deadline: float,
    job: JobCommands,
    reviewer: Callable[..., Review] = find_counterexample,
) -> ReviewFunc:
    """A review that looks for a case the change still gets wrong, and proves it with a test.

    The reviewer may write that case as a test, which code runs with the
    change (in ``job``'s build, the one the checks left). A test that fails
    stays in the tree and sends the change back with its output. One that
    passes showed nothing wrong and is dropped, as are edits that are not a
    test. Without a test, the reviewer's own verdict stands.
    """
    request = _request(task)

    def review(
        candidate: FixCandidate, repo_dir: str, job_log: str, *, context: FixContext, timeout: int,
    ) -> tuple[bool, str]:
        checked = tree_patch(repo_dir)
        before = _digests(repo_dir)
        context = replace(context, past_changes=_recent_changes(repo_dir, candidate.changed_paths))
        try:
            found = reviewer(task, candidate, repo_dir, job_log, context=context, timeout=timeout)
        except ModelUnavailable as exc:
            # The review changes nothing but its own test, and the change it judges has passed its checks.
            logger.warning("The review: %s; running it once more", exc)
            _reset_to(repo_dir, checked)
            time.sleep(API_ERROR_PAUSE_S)
            found = reviewer(task, candidate, repo_dir, job_log, context=context, timeout=timeout)
        after = _digests(repo_dir)
        wrote = sorted(path for path in {*before, *after} if before.get(path) != after.get(path))
        if not wrote:
            return found.verdict == "approve", found.reasoning
        tests = tuple(test for test in locate_unit_tests(repo_dir, found.tests) if test.name and test.file in wrote)
        plan = None
        if tests and all(is_test_path(path) for path in wrote):
            plan = plan_for(repo_dir, tests, scheduled=False, job=job)
        note = " The reviewer's edits were not a test that code could run, and were dropped."
        if plan is not None:
            plan = to_the_end(replace(plan, single_run=True), repo_dir)
            result = _run(run_test, repo_dir, plan.command(1), deadline)
            if _shows_failure(plan, result, request):
                return False, (
                    f"A reviewer wrote a test for a case the change gets wrong, and it fails: {found.reasoning}\n\n"
                    "The test is in the working tree, in " + ", ".join(f"`{path}`" for path in wrote) + ". Make the "
                    "change handle that case. If the test itself expects the wrong behaviour, correct the test "
                    f"and say why in `validation`.\n\nIts output:\n{result.output}"
                )
            note = " The reviewer's test did not fail on its own assertion, and was dropped."
            if result.passed and plan.ran(result):
                note = " The reviewer wrote a test for a case it expected the change to get wrong, and it passed."
        _reset_to(repo_dir, checked)
        logger.info("Counterexample review:%s", note)
        # A case the reviewer was sure of, shown to hold, is no objection left.
        approved = found.verdict == "approve" or (found.verdict == "counterexample" and note.endswith("it passed."))
        return approved, found.reasoning + note

    return review


def _reset_to(repo_dir: str, patch: str) -> None:
    """Put the working tree back to the working commit with ``patch`` applied: the change, without a reviewer's edits."""
    restore(repo_dir)
    if patch:
        run_git(repo_dir, "apply", "--whitespace=nowarn", "-", input=patch)


def _digests(repo_dir: str) -> dict[str, str]:
    """Every changed file of the working tree, by content."""
    return {path: _digest(Path(repo_dir, path)) for path in worktree_changed_paths(repo_dir, strict=True)}


def _recent_changes(repo_dir: str, paths: tuple[str, ...]) -> str:
    """The last merged commits to the product files in ``paths``, with their diffs; "" when they cannot be read."""
    product = [path for path in paths if not is_test_path(path)]
    if not product:
        return ""
    try:
        log = git_output(
            repo_dir, "log", "-3", "--no-merges", "--no-ext-diff", "--no-textconv", "--format=## %h %s%n%n%b",
            "--patch", "HEAD", "--", *product,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return ""
    if len(log) > _TEST_OUTPUT_CHARS:
        log = log[:_TEST_OUTPUT_CHARS] + "\n[... truncated ...]"
    return f"# The last commits to the files this change touches\n\n{log}" if log.strip() else ""


# A Tcl test that sleeps for a fixed time, which reviewers send back: the one
# rule of theirs code checks. The next comment that keeps coming back is a
# second check next to it.
_FIXED_SLEEP_RE = re.compile(r"^\s*after\s+\d+\s*;?\s*$")
_MAX_SLEEPS_SHOWN = 10


def _changes(patch: str) -> list[tuple[str, str, str]]:
    """Each line ``patch`` adds or removes: its file, ``+`` or ``-``, and the line."""
    changes: list[tuple[str, str, str]] = []
    path = ""
    for line in patch.splitlines():
        if line.startswith(("+++ b/", "--- a/")):
            path = line[6:]
        elif path and line[:1] in ("+", "-") and not line.startswith(("+++ ", "--- ")):
            changes.append((path, line[0], line[1:]))
    return changes


def change_rejection(
    repo_dir: str,
    changed_paths: tuple[str, ...],
    patch: str,
    frozen_patch: str = "",
    frozen_tests: tuple[FailedTest, ...] = (),
) -> str:
    """Why a labelled issue's change cannot be published as it is, or "".

    What a committer would send back before reading the change: CI
    configuration or vendored code, a new command (the project's own
    decision), the test that code saw fail rewritten, more product source
    than one sitting reviews, and a test that sleeps for a fixed time. The
    test is rewritten by removing a line that ``frozen_patch`` added, or by
    any change inside a test of ``frozen_tests``: a line added there can
    make it pass without the issue being fixed. The rest of its file is the
    change's to edit, as an older test of the behaviour may have to be.
    """

    def named(paths: list[str]) -> str:
        return ", ".join(f"`{path}`" for path in paths)

    ci = [path for path in changed_paths if path.startswith(".github/")]
    if ci:
        return f"The change touches CI configuration ({named(ci)}), which a labelled issue's change never does."
    vendored = [path for path in changed_paths if path.startswith("deps/")]
    if vendored:
        return (
            f"The change touches vendored code ({named(vendored)}), which changes only by an update from the "
            "project it comes from. Say which project needs the fix instead."
        )
    commands = [
        path for path in changed_paths
        if fnmatch.fnmatch(path, "src/commands/*.json") and not _in_head(repo_dir, path)
    ]
    if commands:
        return (
            f"The change adds a command ({named(commands)}). A new command is the project's to decide: refuse, "
            "and say what the issue needs."
        )
    lines = _changes(patch)
    # A lone brace or blank is in every test: only a line that says something identifies the frozen one.
    frozen = {(path, line.strip()) for path, sign, line in _changes(frozen_patch) if sign == "+" and len(line.strip()) > 3}
    rewritten = sorted(
        {path for path, sign, line in lines if sign == "-" and (path, line.strip()) in frozen}
        | {test.file for test in frozen_tests if _changes_test(repo_dir, changed_lines(patch), test)}
    )
    if rewritten:
        return (
            f"The change rewrites the test that code saw fail before it, in {named(rewritten)}. You may add tests "
            "to that file, and change its other tests. That test stays as it is."
        )
    product = sum(1 for path, _sign, _line in lines if not is_test_path(path))
    if product > MAX_PRODUCT_LINES:
        return (
            f"The change adds and removes {product} lines of product source, and more than {MAX_PRODUCT_LINES} is "
            "more than a reviewer reads in one sitting. Make the smallest change that makes the test pass, or "
            "refuse and say what a person should do."
        )
    sleeps = [
        f"- `{path}`: `{line.strip()}`" for path, sign, line in lines
        if sign == "+" and fnmatch.fnmatch(path, "tests/*.tcl") and _FIXED_SLEEP_RE.match(line)
    ]
    if sleeps:
        return (
            "The change sleeps for a fixed time in a test. Wait for the condition instead, with "
            "`wait_for_condition` or a `wait_for_*` helper:\n" + "\n".join(sleeps[:_MAX_SLEEPS_SHOWN])
        )
    return ""


def _in_head(repo_dir: str, path: str) -> bool:
    try:
        git_output(repo_dir, "cat-file", "-e", f"HEAD:{path}")
    except subprocess.CalledProcessError:
        return False
    return True


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
    # A log or issue names the file; one named with ".." would leave the checkout.
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
    verifies a candidate. What the history search found, and the
    repository's test guide, go to the agent too.
    """
    plan = None
    upstream_first = only_built = False
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
            upstream_first = only_built = True
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
    guide = guide_for(task.failure.repo_full_name, failed_job=True)
    if guide:
        guide += repository_guidelines(repo_dir)
    named_paths = tuple(dict.fromkeys([*(test.file for test in tests if test.file), *paths]))
    context = FixContext(
        prior_fixes=summarize_prior_fixes(fixes),
        prior_fixes_detail=render_prior_fixes(repo_dir, fixes),
        prior_fixes_searched=found is not None,
        test_history=history,
        unported_changes=unported_changes(repo_dir, named_paths) if upstream_first else "",
        unported_index=unported_index(repo_dir) if upstream_first else "",
        sibling_changes=(
            # The other release branches' sweeps, and the CI-sweep PRs that
            # collect their fixes until those are merged, each against its sweep.
            "".join(
                sibling_branch_changes(
                    repo_dir, named_paths,
                    own_branch=branch, base_branch=task.failure.base_branch, compared_with=compared_with,
                    skip=task.target.closed_fix_branches,
                )
                for branch, compared_with in (
                    (task.target.branch, ""),
                    (task.target.fix_branch, task.target.branch.removesuffix(f"/{task.failure.base_branch}")),
                )
                if branch
            )
            if upstream_first and task.policy is FixPolicy.BACKPORT else ""
        ),
        pending_fixes=(
            # A CI-sweep branch whose pull request was closed holds fixes that were turned down.
            pending_fixes(repo_dir, task.target.fix_branch)
            if task.target.fix_branch and task.target.fix_branch not in task.target.closed_fix_branches else ""
        ),
        open_fixes="".join(
            f"## #{fix.number}: {fix.title}\n\n```diff\n{fix.diff}\n```\n\n" for fix in task.open_fixes
        ),
        verification=described,
        verified_by_code=plan is not None,
        upstream_first=upstream_first,
        reviewed_only=reviewed_only,
        only_built=only_built,
        guide=guide,
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


def repository_guidelines(repo_dir: str) -> str:
    """The repository's own guidelines as its default branch has them, to end the bot's guide with.

    Naming their paths was not enough: of 93 fix sessions, 95% read the
    guide they were handed and 2% opened ``tests/README.md``. And they are
    taken from the default branch, not the checkout: a release branch cut
    before a guideline was written does not have it (Valkey's 9.0 and older
    have neither ``AGENTS.md`` nor ``DEVELOPMENT_GUIDE.md``), and a pull
    request cannot rewrite what its own fix is judged by. The checkout's copy
    stands in only where the default branch cannot be read.
    """
    default_ref = default_branch_ref(repo_dir)
    sections: list[str] = []
    for path in _REPO_DOCS:
        text = ""
        if default_ref:
            try:
                text = git_output(repo_dir, "show", f"{default_ref}:{path}")
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                text = ""
        if not text.strip():
            try:
                text = (Path(repo_dir) / path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        if text.strip():
            sections.append(f"## The repository's `{path}`\n\n{text.strip()}\n")
    if not sections:
        return ""
    return (
        "\n\n---\n\n# The repository's own guidelines\n\n"
        "As the default branch has them. They are the authority on style and conventions. What the "
        "policy above lets you change still decides what you may change, and a release branch may "
        "lack a helper or an option they mention: use only what this branch has.\n\n"
        + "\n".join(sections)
    )


def guide_for(repo_full_name: str, *, failed_job: bool = False) -> str:
    """The bot's guide for making a change to ``repo_full_name``, or "" when it has none.

    With ``failed_job``, it starts with how to tell what a failed CI job
    is (a test bug, a harness bug, a product bug) and to look for the fix in
    the test first. That is for a job that failed, not for a change made
    from a test written for it, which is right by construction.
    """
    name = repo_full_name.replace("/", "__")
    try:
        guide = (_GUIDES_DIR / f"{name}.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    if not failed_job:
        return guide
    try:
        return (_GUIDES_DIR / f"{name}.failed-job.md").read_text(encoding="utf-8") + "\n" + guide
    except OSError:
        return guide


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
    min_loops: int = 1,
    also: Callable[[FixCandidate, list[str]], str] | None = None,
) -> FixCandidate:
    established = _established_checks(context)
    feedback = ""
    last = "no attempt was made"
    for attempt in range(1, MAX_ROUNDS + 1):
        remaining = int(deadline - time.monotonic())
        # Running out of time, or an agent crash, says nothing about the
        # failure: it is a failed attempt, which may be retried, not a refusal.
        if attempt > 1 and remaining < _MIN_ROUND_S:
            raise RuntimeError(f"Ran out of time after {attempt - 1} round(s). Last: {last}")
        # What comes after the agent: verification and review, or review
        # alone. In a short budget, at most half of what is left.
        reserve = min(_VERIFY_RESERVE_S if run_test is not None else _REVIEW_RESERVE_S, remaining // 2)
        try:
            outcome = _retried(
                lambda feedback, timeout: agent_round(
                    task, repo_dir, original_head, job_log, context, feedback,
                    attempt=attempt, timeout=timeout, agent_func=agent_func,
                ),
                repo_dir, feedback,
                lambda: max(60, min(int(deadline - time.monotonic()) - reserve, _MAX_AGENT_ROUND_S)),
                f"CI-fix round {attempt}",
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
            raise RuntimeError(f"The agent failed in round {attempt}: {exc}. Last: {last}") from exc
        candidate = outcome.candidate
        if candidate.action is CandidateAction.REFUSE:
            return replace(candidate, verification="\n".join(established))
        if outcome.feedback:
            feedback = last = outcome.feedback
            logger.info("CI-fix round %d: %s", attempt, last)
            continue

        checks = list(established)
        round_plan = plan
        named = _named_plan(task, repo_dir, candidate, context, job) if run_test is not None else None
        if named is not None:
            round_plan, named_tests = named
            # Not the plan code made before the agent ran, which ran nothing.
            checks = [check for check in checks if check != context.verification]
            checks.append(
                ("On this branch the failing test may have another name, so code ran the ones the author named: "
                 if context.release_fix else
                 "Code could not tell from the log which test failed, so it ran the one the author named: ")
                + ", ".join(f"`{test.name}` in `{test.file}`" if test.name else f"`{test.file}`" for test in named_tests)
                + ". Check against the log that it is the test that failed."
            )
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
            if run_test is not None and round_plan is not None:
                failed = _verify(reviewed, repo_dir, round_plan, run_test, deadline, checks, min_loops=min_loops)
                if not failed:
                    failed = _verify_added_tests(task, reviewed, repo_dir, run_test, deadline, checks, job)
                if not failed and also is not None:
                    failed = also(reviewed, checks)
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
                    checks.append(f"Accepted on round {attempt} of {MAX_ROUNDS}.")
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
            candidate, verification="\n".join(checks), also_touches=touches,
            not_run=(context.reviewed_only or context.only_built) and named is None,
        )
    return _refusal(
        f"No candidate passed its test and review in {MAX_ROUNDS} rounds. Last: {last}",
        verification="\n".join(established),
    )


@dataclass(frozen=True)
class RoundOutcome:
    """One agent round: the checked candidate, applied in the tree as ``applied``.

    ``feedback`` is set, and nothing is applied, when the candidate was
    rejected or a port did not apply.
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
            frozen_patch=context.frozen_patch, frozen_tests=context.frozen_tests,
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
_PRODUCT_NEEDS_UPSTREAM = (
    "On a release branch, product source changes only by a commit merged upstream, and the patch "
    "changes {paths} without being one: choose `port`, or set `adapts_commit` to the merged "
    "default-branch commit the patch adapts, which it must mostly be. Without one, fix the test or "
    "the test harness if that is the correct fix; otherwise choose `refuse` and say what the "
    "default branch needs."
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


def _named_plan(
    task: FixTask, repo_dir: str, candidate: FixCandidate, context: FixContext, job: JobCommands | None,
) -> tuple[VerifyPlan, tuple[FailedTest, ...]] | None:
    """The plan that runs the tests the agent named, when code could name none: (plan, tests), or None.

    A valgrind or sanitizer report, or a test client's exception, names no
    test on its result line; the agent reads the whole log and can. On the
    default branch, the release branch's failing test may have another name
    there (a test split into variants), which its file may build from
    variables. Only a test that is in the checkout is run, and the reviewer is
    told which one it was and asked to check it against the log.
    """
    if not (context.reviewed_only or context.only_built or context.release_fix) or not candidate.failing_tests:
        return None
    named = []
    for test in locate_unit_tests(repo_dir, candidate.failing_tests):
        path = Path(repo_dir, test.file)
        if ".." in Path(test.file).parts or not _inside_checkout(repo_dir, path) or not path.is_file():
            continue
        # A name built from variables is not in the file: one that does not
        # run is caught by the run (VerifyPlan.ran), and the agent is told.
        if test.name and not context.release_fix and test.name not in path.read_text(encoding="utf-8", errors="replace"):
            continue
        named.append(test)
    if not named:
        return None
    plan = plan_for(repo_dir, tuple(named), scheduled=task.failure.scheduled, job=job)
    return (plan, plan.tests or tuple(named)) if plan is not None else None


def _inside_checkout(repo_dir: str, path: Path) -> bool:
    try:
        path.resolve().relative_to(Path(repo_dir).resolve())
    except ValueError:
        return False
    return True


def _established_checks(context: FixContext) -> list[str]:
    checks = []
    if context.evidence_check:
        checks.append(context.evidence_check)
    if context.prior_fixes:
        checks.append("Existing fixes found: " + "; ".join(context.prior_fixes.splitlines()))
    elif context.prior_fixes_searched:
        checks.append("No merged change that looks like a fix was found.")
    # A plan is the agent's instructions; what it ran is published instead (``_verify``).
    if context.verification and not context.verified_by_code:
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


# A Tcl test the patch adds: ``test "name"`` or ``test {name}`` on an added line. A name built
# from variables is not one ``--only`` can find, and is left out.
_ADDED_TEST_RE = re.compile(r'^\+\s*test\s+(?:"([^"$\[]+)"|\{([^}$\[]+)\})')
# A C unit test the patch adds: ``TEST(Suite, Name)`` or ``TEST_F``. A parameterized one runs
# under names its instantiation builds, and is left out.
_ADDED_UNIT_TEST_RE = re.compile(r"^\+\s*TEST(?:_F)?\(\s*(\w+)\s*,\s*(\w+)\s*\)")


def added_tests(patch: str, *, limit: int | None = _MAX_TESTS) -> tuple[FailedTest, ...]:
    """The tests ``patch`` adds, by name and file: Tcl tests under ``tests/``, C unit tests under ``src/unit/``."""
    tests: list[FailedTest] = []
    path = ""
    for line in patch.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif path.startswith("tests/") and path.endswith(".tcl"):
            match = _ADDED_TEST_RE.match(line)
            if match:
                tests.append(FailedTest(match.group(1) or match.group(2), path))
        elif path.startswith("src/unit/") and path.endswith(".cpp"):
            match = _ADDED_UNIT_TEST_RE.match(line)
            if match:
                tests.append(FailedTest(f"{match.group(1)}.{match.group(2)}", path))
    return tuple(dict.fromkeys(tests))[:limit]


def _verify_added_tests(
    task: FixTask,
    candidate: FixCandidate,
    repo_dir: str,
    run_test: TestFunc,
    deadline: float,
    checks: list[str],
    job: JobCommands | None,
) -> str:
    """Run each test the patch adds once; "" when they pass, else feedback for the agent.

    The failing test passing does not show that a test the patch brings with
    it does: an upstream test adapted with its fix can need more than this
    branch has.
    """
    for test in added_tests(candidate.patch):
        named = f"`{test.name}` in `{test.file}`"
        plan = plan_for(repo_dir, (test,), scheduled=task.failure.scheduled, job=job)
        if plan is None:
            checks.append(f"The patch adds {named}, which no command here can run.")
            continue
        failed = _verify_with_plan(replace(plan, single_run=True), repo_dir, run_test, deadline, checks)
        if failed:
            return f"Your patch adds {named}, which does not pass. {failed}"
    return ""


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
# A test's start: Tcl's ``test {name} {`` or ``test "name" {``, and a C unit test's ``TEST_F(Suite, Name)``.
_TEST_START_RE = re.compile(r"^\s*(?:test\s+[{\"]|TEST(?:_[FP])?\s*\()")


def _starts_test(text: str, name: str) -> bool:
    """Whether the source line ``text`` is where the test ``name`` starts."""
    if f"{{{name}}}" in text or f'"{name}"' in text:
        return True
    suite, _, case = name.partition(".")
    return bool(case) and re.search(rf"\(\s*{re.escape(suite)}\s*,\s*{re.escape(case)}\s*\)", text) is not None


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
    named = [number for number in starts if _starts_test(source[number - 1], test.name)]
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
    *,
    min_loops: int = 1,
) -> str:
    """Run the candidate's tests; "" when they hold, else feedback for the agent.

    The build or an earlier run could leave ignored files the patch does not
    show (a prebuilt server binary that keeps ``make`` from rebuilding), so
    those go first: the build and tests run from source. The plan's notes
    (a stand-in for the job, a run_solo file) go with what ran.
    """
    checks.extend(plan.notes)
    _remove_ignored(repo_dir)
    if any(path.startswith("deps/") for path in changed_lines(candidate.patch)):
        _rebuild_dependencies(repo_dir)
    return _verify_with_plan(plan, repo_dir, run_test, deadline, checks, min_loops=min(min_loops, plan.loops))


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
    *,
    min_loops: int = 1,
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
    if loops < min(min_loops, current.loops):
        # Nothing is known about the change: the attempt failed, and is retried.
        raise RuntimeError(f"Only {loops} loop(s) of `{current.quoted(1)}` fit in the time left, not {min_loops}.")
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
    """Reset the working tree to the working commit, keeping ignored build output."""
    for args in (("cherry-pick", "--quit"), ("reset", "--hard", "--quiet", "HEAD"), ("clean", "-fdq")):
        try:
            run_git(repo_dir, *args)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            logger.warning("Could not run git %s to reset the tree", " ".join(args))


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
    upstream_first: bool = False, frozen_patch: str = "", frozen_tests: tuple[FailedTest, ...] = (),
) -> FixCandidate:
    """``candidate`` with its patch built from the checkout, or a refusal.

    A commit is a refusal: the agent broke the one rule its checkout depends
    on. Anything else wrong with what it handed back raises
    :class:`_Rejected`, which goes back to it like a failing test. With
    ``upstream_first``, a patch's claim to adapt a merged upstream commit is
    kept only when code finds it does; otherwise the patch is the bot's own,
    which on a backport branch may change tests and the harness only.

    The patch is what the working tree holds, not what the agent listed: a
    file it changed and did not list is published with the rest and pointed
    out to the reviewer, rather than costing a round.
    """
    current_head = git_output(repo_dir, "rev-parse", "HEAD").strip()
    if current_head != original_head:
        return _refusal(_COMMITTED)

    changed_paths = worktree_changed_paths(repo_dir, strict=True)
    if candidate.action is CandidateAction.REFUSE:
        # Only a pull request the agent was shown can be the one that fixes it.
        shown = {fix.number for fix in task.open_fixes}
        return replace(
            candidate, patch="", changed_paths=(), port_commit="", adapts="", validation="",
            open_fix=candidate.open_fix if candidate.open_fix in shown else 0, agent_refused=True,
        )
    if not candidate.failing_check:
        # The job is what failed, whatever else the agent left out; for a labelled issue, the issue is.
        candidate = replace(
            candidate,
            failing_check=task.failure.job_name if task.request is None else f"issue #{task.request.number}",
        )

    capabilities = target_capabilities(task.target.kind)
    if task.request is not None and candidate.action is CandidateAction.PORT:
        raise _Rejected("A labelled issue's change is written here: choose `patch` or `refuse`.")
    if not capabilities.allows(candidate.action):
        raise _Rejected(
            f"The {candidate.action.value} action is not supported for "
            f"a {task.target.kind.value} target."
        )
    if candidate.action is CandidateAction.PORT:
        if changed_paths:
            raise _Rejected("You mixed an upstream port with working-tree edits: choose one.")
        if not is_full_sha(candidate.port_commit):
            raise _Rejected("The proposed upstream port is not a full commit SHA.")
        return replace(candidate, patch="", changed_paths=(), adapts="", validation="")

    if not changed_paths:
        raise _Rejected("You chose `patch` but changed no files.")
    try:
        patch = build_approved_patch(repo_dir, changed_paths, strict=True)
    except EmptyPatch:
        raise _Rejected("You chose `patch` but your changes make an empty patch.")
    adapts = ""
    if upstream_first and _adapts(repo_dir, candidate.adapts, changed_paths, patch):
        adapts = candidate.adapts
    product = tuple(path for path in changed_paths if not is_test_path(path))
    if upstream_first and task.policy is FixPolicy.BACKPORT and product and not adapts:
        raise _Rejected(_PRODUCT_NEEDS_UPSTREAM.format(paths=", ".join(f"`{path}`" for path in product)))
    if task.request is not None:
        out_of_scope = change_rejection(repo_dir, changed_paths, patch, frozen_patch, frozen_tests)
        if out_of_scope:
            raise _Rejected(out_of_scope)
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


def is_test_path(path: str) -> bool:
    """Whether ``path`` is test code or the test harness, not product source (Valkey: ``tests/``, ``src/unit/``)."""
    return any(part in ("tests", "test", "unit") for part in Path(path).parts[:-1])


def changed_text(diff: str, sign: str) -> set[str]:
    """The lines a diff adds (``+``) or removes (``-``), without their indentation; a lone brace or blank says nothing."""
    return {
        text for text in (line[1:].strip() for line in diff.splitlines()
                          if line.startswith(sign) and not line.startswith(sign * 3))
        if len(text) > 3
    }


def run_test_command(
    repo_dir: str, command: str, timeout: int = TEST_TIMEOUT_S, *, offline: bool = False, quiet: bool = False,
) -> TestRun:
    """Run ``command`` in ``repo_dir`` as the isolated account.

    ``offline`` keeps it off the network, and ``quiet`` keeps a failing
    command's output out of the log: the caller decides whether it may be shown.
    """
    env = filter_env(PROCESS_BASICS)
    argv = ["bash", "-c", command]
    user = isolated_user()
    started = time.monotonic()
    try:
        if user:
            with handed_to(user, Path(repo_dir)) as home:
                invocation = as_user(user, env, argv, home=home, cwd=repo_dir, network=not offline)
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
    logger.info("Test command exited %d after %.0fs%s", result.returncode, seconds, "" if quiet else f": {command}")
    excerpt = _excerpt(output)
    if result.returncode != 0 and not quiet:
        _log_failed_output(excerpt)
    return TestRun(
        passed=result.returncode == 0,
        output=excerpt,
        report=failure_report(output),
        seconds=seconds,
        crashed=result.returncode != 0 and shows_crash(output),
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
