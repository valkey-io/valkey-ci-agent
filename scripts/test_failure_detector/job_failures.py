"""Find Daily job failures that the failures artifact does not record.

The ``all-test-failures`` artifact only holds assertion failures. A job also
fails on a test timeout, a valgrind or sanitizer report at server shutdown,
the shutdown leak check, or an exception in the test framework, and none of
those reach the artifact. For every failed job this reads what runtest prints
about them: its closing summary ("The following tests failed:" followed by
``*** [status]: ...`` lines), and an ``[exception]: ...`` line, which runtest
prints on its own before it exits without a summary. A line naming a test and
its file becomes an ordinary test failure, so it lands on that test's issue;
anything else becomes a job-level failure, described by its cause: the
exception, or a valgrind or sanitizer report's first error and frames rather
than its banner. A job the artifact already records is read too, since one
assertion there does not explain a Valgrind report or a timeout in the same
job.

One cause is one job-level failure, however many jobs and steps it fails:
failures are the same when they print the same cause. A failed job that
printed no cause at all (a unit test, a build break, a crash, a package
mirror) is not filed: nothing identifies what failed, and grouping such jobs
by step or job would merge unrelated causes or file an issue per job. Those
jobs are reported to the caller (``unexplained``) instead. One in a setup step
(installing packages, checking out, uploading results) is not reported at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from scripts.common.incidents import compute_fingerprint
from scripts.common.valkey_ci import gtest_failure, gtest_file_of, strip_log_prefix, test_in_file
from scripts.common.workflow_artifacts import ArtifactClient
from scripts.common.workflow_jobs import FAILED_JOB_CONCLUSIONS, job_is_ignored
from scripts.test_failure_detector.parse_failures import JobReference, UniqueFailure

logger = logging.getLogger(__name__)

_SUMMARY_RE = re.compile(r"^\*\*\* \[(?P<status>[A-Za-z]+)\]: (?P<data>.+)$")
# runtest prints an exception on its own and exits before its summary, with
# the Tcl stack under it (`` in r at tests/unit/type/zset.tcl:1559``), maybe
# after lines of the error's own text (``in expression "..."``). The first
# frame in a test file, past the test framework's own, says where.
_EXCEPTION_RE = re.compile(r"^\[exception\]: (?P<data>.+)$")
_TCL_FRAME_RE = re.compile(r"^\s*in \S+ at (?P<file>tests/\S+\.tcl):\d+$")
_TCL_FRAMEWORK_RE = re.compile(r"^tests/(?:support/|helpers/|test_helper\.tcl$)")
# Where the log has moved on without a stack: an Actions annotation, another
# runtest status line, or runtest's own cleanup.
_LOG_BOUNDARY_RE = re.compile(
    r"^(?:##\[|\[(?:ok|err|exception|TIMEOUT|skip|ignore)\]:|\*\*\* |Killing still running)"
)
_EXCEPTION_LINES = 200
# A valgrind or sanitizer report is the server's whole stderr, which runtest
# prints after the summary line. Its cause is the first error and the frames
# under it; the first line is only valgrind's banner or a server log line.
_REPORT_RE = re.compile(r"^(?P<kind>Valgrind error|Sanitizer error):")
_VALGRIND_ERROR_RE = re.compile(
    r"^==\d+== (?P<error>(?:Invalid|Mismatched|Conditional jump|Use of uninitialised|Syscall param|"
    r"Source and destination overlap|Argument|Warning:)[^\n]*|[\d,]+ (?:\([^)]*\) )?bytes in [\d,]+ blocks are "
    r"(?:definitely|possibly) lost)"
)
# A leak's stack starts in the allocator (malloc < ztrymalloc_usable_internal
# < zmalloc_usable); the frames that tell leaks apart are below it.
_ALLOCATOR_FRAME_RE = re.compile(r"^(?:(?:c|m|re)alloc|operator new.*|z\w*alloc\w*|je_\w+)$")
_VALGRIND_FRAME_RE = re.compile(r"^==\d+==\s+(?:at|by) 0x[0-9A-Fa-f]+: (?P<function>[^\s(]+)")
_SANITIZER_ERROR_RE = re.compile(r"(?P<error>ERROR: \w+Sanitizer: [\w-]+|runtime error: .+)")
_SANITIZER_FRAME_RE = re.compile(r"^\s*#\d+ 0x[0-9a-f]+ in (?P<function>\S+)")
_REPORT_FRAMES = 3
_REPORT_LINES = 400
# The leak check runtest runs at each server's shutdown is reported as a test
# named by that server's pid ("Check for memory leaks (pid 4321)"). Like a
# valgrind report it is not a test's own failure, and runtest's artifact
# leaves it out too, so it is a job-level failure: as a test it would get a
# new issue for every pid.
_LEAK_CHECK_RE = re.compile(r"^Check for memory leaks\b")
# Steps that prepare the runner rather than build or test the repository.
_SETUP_STEP_RE = re.compile(
    r"^(?:set up job|initialize containers|complete job|stop containers|run actions/|post )"
    r"|\b(?:install|checkout|cache|upload|prep|testprep|libbacktrace)\b",
    re.IGNORECASE,
)


@dataclass
class JobFailure:
    """A failure not attributable to a recorded test, in one or more jobs.

    ``job`` and ``url`` name the first job seen; ``jobs`` lists every job of
    the run that failed this way.
    """

    job: str
    url: str
    step: str = ""
    summary: tuple[str, ...] = ()
    jobs: list[JobReference] = field(default_factory=list)

    def identity(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """The (namespace, shapes) that make two failures the same one: their cause.

        Neither the job nor the step is part of it, so one cause failing in
        several jobs, under differently named steps, is one failure.
        """
        return ("cause",), self.summary


def normalize_job_name(name: str) -> str:
    """Compare a run's job name with an artifact key, ignoring separators.

    The artifact keys a matrix job as ``test-valgrind-test-unit`` while the run
    displays ``test-valgrind-test (unit)``.
    """
    return re.sub(r"[^a-z0-9]", "", name.lower())


def find_unrecorded_failures(
    artifact_client: ArtifactClient,
    repo_full_name: str,
    all_failures: dict[str, Any],
    *,
    jobs: Iterable[Any],
    ignored_jobs: Iterable[str] = (),
    unread: list[str] | None = None,
    unexplained: list[str] | None = None,
) -> tuple[list[UniqueFailure], list[JobFailure]]:
    """Return failures found only in the logs of ``jobs``.

    A failed job whose log cannot be read (expired, or the download failed)
    is left out: nothing is known about how it failed, so filing it would
    make an issue of the download error. Its name is appended to ``unread``,
    so the caller can report the inspection as incomplete. A failed job that
    printed no cause is appended to ``unexplained`` as ``<job> (<step>)``.

    ``jobs`` is one exact-attempt snapshot from
    :func:`scripts.common.workflow_jobs.jobs_for_run_attempt`, so every view of
    the run uses the same GitHub objects.
    """
    recorded = {
        normalize_job_name(job)
        for job, suites in all_failures.items()
        if isinstance(suites, dict) and any(isinstance(entries, list) and entries for entries in suites.values())
    }
    patterns = tuple(ignored_jobs)
    tests: dict[tuple[str, str], UniqueFailure] = {}
    job_failures: dict[str, JobFailure] = {}
    for job in jobs:
        name = str(getattr(job, "name", "") or "")
        if str(getattr(job, "conclusion", "") or "") not in FAILED_JOB_CONCLUSIONS:
            continue
        if job_is_ignored(name, patterns):
            continue
        url = str(getattr(job, "html_url", "") or "")
        summary = _failure_summary(artifact_client, repo_full_name, job)
        if summary is None:
            if unread is not None:
                unread.append(name)
            continue
        causes: dict[str, None] = {}  # ordered, without repeats
        for status, data in summary:
            test = test_in_file(data)
            if test is None or status == "exception" or _LEAK_CHECK_RE.match(data):
                causes[f"[{status}]: {data}"] = None
                continue
            failure = tests.setdefault(test, UniqueFailure(
                test_name=test[0], test_file=test[1], error=f"[{status}]: {data}",
            ))
            if not any(ref.job == name for ref in failure.jobs):
                failure.jobs.append(JobReference(job=name, url=url))
        if not causes and (summary or normalize_job_name(name) in recorded):
            # Every line named a test, or the artifact explains the failure.
            continue
        step = _failed_step(job)
        if not causes:
            if _SETUP_STEP_RE.search(step):
                logger.info("Not reporting %s: it failed in setup step %r without a summary", name, step)
            elif unexplained is not None:
                unexplained.append(f"{name} ({step or 'no failed step'})")
            continue
        # One failure per cause: a job that printed two causes is in two
        # issues, so each cause keeps its issue when it later fails alone.
        for cause in causes:
            found = JobFailure(job=name, url=url, step=step, summary=(cause,))
            namespace, shapes = found.identity()
            same = job_failures.setdefault(compute_fingerprint(namespace=namespace, shapes=shapes), found)
            if not any(ref.job == name for ref in same.jobs):
                same.jobs.append(JobReference(job=name, url=url))
    return list(tests.values()), list(job_failures.values())


def merge_failures(recorded: list[UniqueFailure], extra: list[UniqueFailure]) -> list[UniqueFailure]:
    """Fold log-derived test failures into the artifact's, one entry per test."""
    by_key = {(f.test_name, f.test_file): f for f in recorded}
    for failure in extra:
        existing = by_key.get((failure.test_name, failure.test_file))
        if existing is None:
            by_key[(failure.test_name, failure.test_file)] = failure
            recorded.append(failure)
            continue
        for ref in failure.jobs:
            # The artifact keys a job as ``test-valgrind-test-unit``, its log
            # as ``test-valgrind-test (unit)``: the same environment.
            if not any(normalize_job_name(r.job) == normalize_job_name(ref.job) for r in existing.jobs):
                existing.jobs.append(ref)
    return recorded


def _failure_summary(artifact_client: ArtifactClient, repo_full_name: str, job: Any) -> list[tuple[str, str]] | None:
    """The job log's runtest summary lines, or None when the log cannot be read."""
    try:
        log = artifact_client.download_job_log(repo_full_name, int(job.id))
    except Exception as exc:  # noqa: BLE001 - reported to the caller as an unread log
        logger.warning("Could not download the log of job %s: %s", getattr(job, "id", "?"), exc)
        return None
    if not log.strip():
        # The download answers an expired log with nothing; a real log is never empty.
        logger.warning("The log of job %s is gone (expired)", getattr(job, "id", "?"))
        return None
    plain = [strip_log_prefix(raw) for raw in log.splitlines()]
    lines: list[tuple[str, str]] = []
    for index, line in enumerate(plain):
        if match := _SUMMARY_RE.match(line):
            data = match.group("data").strip()
            if report := _REPORT_RE.match(data):
                data = _report_cause(report.group("kind"), data, plain[index + 1:index + 1 + _REPORT_LINES])
            lines.append((match.group("status"), data))
        elif match := _EXCEPTION_RE.match(line):
            where = _exception_location(plain[index + 1:index + 1 + _EXCEPTION_LINES])
            lines.append(("exception", match.group("data").strip() + (f" at {where}" if where else "")))
        elif (unit_test := gtest_failure(line)) is not None:
            lines.append(("err", _gtest_cause(unit_test, plain[:index])))
    return lines


def _gtest_cause(test: str, before: list[str]) -> str:
    """The cause of C unit test ``test``'s failure: its name and the file it failed in.

    The file of its first failed assertion, by base name and without its
    line (scripts/common/valkey_ci.py): the issue stays the same when the
    assertion moves, or another job's build prints the path differently.
    """
    file = gtest_file_of(test, before)
    return f"Unit test {test} failed in {file}" if file else f"Unit test {test} failed"


def _exception_location(following: list[str]) -> str:
    """The test file the exception's Tcl stack first reaches past the framework, or ""."""
    in_stack = False
    for line in following:
        frame = _TCL_FRAME_RE.match(line)
        if frame is not None:
            in_stack = True
            if not _TCL_FRAMEWORK_RE.match(frame.group("file")):
                return frame.group("file")
            continue
        # Before the stack: the error's own text, skipped. After it, or past
        # a boundary of the log, there is no frame to find.
        if in_stack or _LOG_BOUNDARY_RE.match(line):
            break
    return ""


def _report_cause(kind: str, first: str, following: list[str]) -> str:
    """``<kind>: <first error> at <frames>`` from the report runtest printed, or ``first``."""
    report = [first[len(kind) + 1:].strip()]
    for line in following:
        if _SUMMARY_RE.match(line):
            break
        report.append(line)
    if kind == "Valgrind error":
        error_re, frame_re = _VALGRIND_ERROR_RE, _VALGRIND_FRAME_RE
    else:
        error_re, frame_re = _SANITIZER_ERROR_RE, _SANITIZER_FRAME_RE
    for index, line in enumerate(report):
        error = error_re.search(line)
        if error is None:
            continue
        stack = [frame.group("function") for frame in map(frame_re.match, report[index + 1:index + 30]) if frame]
        while len(stack) > 1 and _ALLOCATOR_FRAME_RE.search(stack[0]):
            stack.pop(0)
        frames = stack[:_REPORT_FRAMES]
        return f"{kind}: {error.group('error').strip()}" + (f" at {' < '.join(frames)}" if frames else "")
    return first


def _failed_step(job: Any) -> str:
    for step in getattr(job, "steps", None) or ():
        if str(getattr(step, "conclusion", "") or "") in FAILED_JOB_CONCLUSIONS:
            return str(getattr(step, "name", "") or "")
    return ""
