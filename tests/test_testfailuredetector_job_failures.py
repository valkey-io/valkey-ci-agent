"""Tests for Daily job failures the failures artifact does not record."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from scripts.common.valkey_ci import FILE_TIMEOUT_TEST_NAME
from scripts.test_failure_detector import issue_renderer
from scripts.test_failure_detector.job_failures import (
    JobFailure,
    find_unrecorded_failures,
    merge_failures,
    normalize_job_name,
)
from scripts.test_failure_detector.parse_failures import JobReference, UniqueFailure


def _job(job_id, name, conclusion="failure", step="test"):
    return SimpleNamespace(id=job_id, name=name, conclusion=conclusion, html_url=f"https://job/{job_id}",
                           steps=[SimpleNamespace(name="make", conclusion="success"),
                                  SimpleNamespace(name=step, conclusion=conclusion)])


def _find(jobs, logs, recorded=None, ignored=()):
    client = MagicMock()
    client.download_job_log.side_effect = lambda _repo, job_id: logs.get(job_id, "")
    result = find_unrecorded_failures(client, "valkey-io/valkey", recorded or {}, jobs=jobs, ignored_jobs=ignored)
    return result


_SUMMARY = (
    "2026-09-22T00:56:01.1692807Z Test Summary: 5821 passed, 1 failed\n"
    "2026-09-22T00:56:01.1692908Z !!! WARNING The following tests failed:\n"
    "2026-09-22T00:56:01.1692920Z \x1b[31m*** [TIMEOUT]: Throttling tears down in tests/integration/throttle-repl.tcl\x1b[0m\n"
)


def test_a_timeout_in_the_log_becomes_a_test_failure():
    tests, jobs = _find([_job(1, "test-ubuntu-32bit")], {1: _SUMMARY})
    assert jobs == []
    assert [(t.test_name, t.test_file) for t in tests] == [
        ("Throttling tears down", "tests/integration/throttle-repl.tcl")]
    assert tests[0].error == "[TIMEOUT]: Throttling tears down in tests/integration/throttle-repl.tcl"
    assert [ref.job for ref in tests[0].jobs] == ["test-ubuntu-32bit"]


@pytest.mark.parametrize("state", [
    "pid:74389", "port 21581", "ASSIGNED: sock55d0e3c0b890 (unit/type/list)", "SLEEPING, no more units to assign",
])
def test_a_client_timeout_gets_a_stable_name_for_its_file(state):
    """Client states from test_helper.tcl change every run; keying on them would open a new issue daily."""
    log = f"*** [TIMEOUT]: {state} in tests/integration/repl-compression.tcl\n"
    tests, _jobs = _find([_job(1, "test-valgrind-test (integration-type)")], {1: log})
    assert (tests[0].test_name, tests[0].test_file) == (FILE_TIMEOUT_TEST_NAME, "tests/integration/repl-compression.tcl")


def test_a_hung_test_keeps_its_own_name():
    log = "*** [TIMEOUT]: Throttling tears down in tests/integration/throttle-repl.tcl\n"
    tests, _jobs = _find([_job(1, "test-ubuntu-32bit")], {1: log})
    assert tests[0].test_name == "Throttling tears down"


def test_a_failure_without_a_test_becomes_a_job_failure():
    log = "*** [err]: Valgrind error: ==11258== Memcheck, a memory error detector\n"
    tests, jobs = _find([_job(1, "test-valgrind-test (unit)")], {1: log})
    assert tests == []
    assert jobs == [JobFailure(job="test-valgrind-test (unit)", url="https://job/1", step="test",
                               summary=("[err]: Valgrind error: ==11258== Memcheck, a memory error detector",),
                               jobs=[JobReference("test-valgrind-test (unit)", "https://job/1")])]


def test_a_leak_check_is_a_job_failure_whatever_its_pid():
    """runtest names its shutdown leak check by pid; as a test it would get a new issue every run."""
    def leak(pid):
        log = f"*** [err]: Check for memory leaks (pid {pid}) in tests/unit/type/list.tcl\n"
        return _find([_job(1, "test-sanitizer-address (gcc)")], {1: log})

    (tests, (first,)), (_tests, (second,)) = leak(4321), leak(98765)
    assert tests == []
    assert issue_renderer.job_fingerprint_for(first) == issue_renderer.job_fingerprint_for(second)


@pytest.mark.parametrize("log", [RuntimeError("502 Bad Gateway"), ""])
def test_a_job_whose_log_cannot_be_read_is_reported_as_unread_not_filed(log):
    """A download error or an expired log says nothing about how the job failed."""
    client = MagicMock()
    client.download_job_log.side_effect = log if isinstance(log, Exception) else (lambda *_a: log)
    unread = []

    tests, jobs = find_unrecorded_failures(
        client, "valkey-io/valkey", {}, jobs=[_job(1, "test-freebsd", step="make")], unread=unread,
    )

    assert (tests, jobs, unread) == ([], [], ["test-freebsd"])


def test_a_failure_that_printed_no_cause_is_reported_not_filed():
    unexplained = []
    client = MagicMock()
    client.download_job_log.return_value = "error: implicit declaration\n"
    tests, jobs = find_unrecorded_failures(
        client, "valkey-io/valkey", {}, jobs=[_job(1, "test-freebsd", step="make")], unexplained=unexplained,
    )
    assert (tests, jobs, unexplained) == ([], [], ["test-freebsd (make)"])


def test_successful_and_ignored_jobs_are_skipped():
    jobs = [_job(2, "test-ubuntu-jemalloc", conclusion="success"), _job(3, "consolidate-test-failures")]
    tests, failures = _find(jobs, {2: _SUMMARY, 3: _SUMMARY}, ignored=("consolidate-test-failures*",))
    assert (tests, failures) == ([], [])


_RECORDED = {"test-sanitizer-undefined-clang": {"valkey": [{"test_name": "x", "test_file": "tests/unit/x.tcl"}]}}


def test_a_recorded_job_still_reports_a_failure_the_artifact_does_not_hold():
    """One assertion in the artifact does not explain a timeout in the same job."""
    tests, failures = _find([_job(1, "test-sanitizer-undefined (clang)")], {1: _SUMMARY}, _RECORDED)
    assert [(t.test_name, t.test_file) for t in tests] == [("Throttling tears down", "tests/integration/throttle-repl.tcl")]
    assert failures == []


def test_a_recorded_job_reports_an_unattributed_summary_line():
    log = "*** [err]: x in tests/unit/x.tcl\n*** [err]: Sanitizer error: runtime error: shift exponent\n"
    tests, failures = _find([_job(1, "test-sanitizer-undefined (clang)")], {1: log}, _RECORDED)
    assert [(t.test_name, t.test_file) for t in tests] == [("x", "tests/unit/x.tcl")]  # merged with the artifact later
    assert [f.summary for f in failures] == [("[err]: Sanitizer error: runtime error: shift exponent",)]


def test_a_recorded_job_without_a_summary_is_explained_by_the_artifact():
    """--fastfail exits before the summary: the recorded assertion is the failure."""
    tests, failures = _find([_job(1, "test-sanitizer-undefined (clang)")], {1: "(Fast fail: test will exit now)\n"},
                            _RECORDED)
    assert (tests, failures) == ([], [])


def test_an_empty_artifact_entry_does_not_count_as_recorded():
    tests, _ = _find([_job(1, "test-ubuntu-32bit")], {1: _SUMMARY}, {"test-ubuntu-32bit": {"valkey": []}})
    assert len(tests) == 1


def test_normalized_job_names_match_artifact_keys():
    assert normalize_job_name("test-valgrind-test (unit)") == normalize_job_name("test-valgrind-test-unit")


def test_the_artifacts_and_the_logs_names_for_one_job_are_one_environment():
    """Seen in Daily run 38828538707: the artifact keys a job by its normalized name."""
    recorded = [UniqueFailure("INCREX BYFLOAT", "tests/unit/type/incr.tcl", "e",
                              [JobReference("test-valgrind-test-integration-type", "u")])]
    from_log = [UniqueFailure("INCREX BYFLOAT", "tests/unit/type/incr.tcl", "e",
                              [JobReference("test-valgrind-test (integration-type)", "u")])]
    (merged,) = merge_failures(recorded, from_log)
    assert [ref.job for ref in merged.jobs] == ["test-valgrind-test-integration-type"]


def test_merge_adds_new_environments_to_an_existing_failure():
    recorded = [UniqueFailure("t", "tests/a.tcl", "e", [JobReference("job-a")])]
    extra = [UniqueFailure("t", "tests/a.tcl", "e2", [JobReference("job-b")]),
             UniqueFailure("u", "tests/b.tcl", "e3", [JobReference("job-c")])]
    merged = merge_failures(recorded, extra)
    assert [ref.job for ref in merged[0].jobs] == ["job-a", "job-b"]
    assert [(f.test_name, f.test_file) for f in merged] == [("t", "tests/a.tcl"), ("u", "tests/b.tcl")]


def test_a_job_failure_issue_is_titled_by_its_cause_and_marks_its_job():
    failure = JobFailure(job="test-valgrind-test (unit)", url="https://job/1", step="test",
                         summary=("[err]: Valgrind error: Invalid read of size 8 at quicklistNext",))
    content = issue_renderer.render_job_issue(failure, "<!-- marker -->", 1)
    assert content.title == "[JOB-FAILURE] Valgrind error: Invalid read of size 8 at quicklistNext in Daily"
    assert content.labels == (issue_renderer.LABEL_NAME,)
    assert issue_renderer.job_marker("test-valgrind-test (unit)") in content.body
    assert issue_renderer.parse_job_marker(content.body) == "test-valgrind-test (unit)"
    assert issue_renderer.parse_job_marker("no marker here") == ""
    assert "Valgrind error: Invalid read" in content.body
    # Distinct causes never share an issue.
    other = JobFailure(job="test-valgrind-test (unit)", url="", step="test", summary=("[exception]: boom",))
    assert issue_renderer.job_fingerprint_for(failure) != issue_renderer.job_fingerprint_for(other)


def test_matrix_twins_failing_the_same_way_are_one_failure():
    """Daily runs each Valgrind leg twice; one leak must not become two issues."""
    def leak(pid):
        return f"*** [err]: Valgrind error: =={pid}== Memcheck, a memory error detector\n"

    jobs = [_job(1, "test-valgrind-test (unit)"), _job(2, "test-valgrind-no-malloc-usable-size-test (unit)"),
            _job(3, "test-valgrind-test (cluster)")]
    _tests, failures = _find(jobs, {1: leak(35021), 2: leak(34715), 3: leak(22231)})
    (failure,) = failures
    assert [ref.job for ref in failure.jobs] == [
        "test-valgrind-test (unit)", "test-valgrind-no-malloc-usable-size-test (unit)", "test-valgrind-test (cluster)",
    ]


def test_an_exception_is_one_failure_across_jobs_and_differently_named_steps():
    """Seen in Daily run 39082305846: the 32-bit jobs, with a FreeBSD mirror outage beside them."""
    exception = (
        "2026-10-17T03:12:44.1Z [exception]: Executing test client: ERR value is out of range.\n"
        "2026-10-17T03:12:44.2Z ERR value is out of range\n"
    )
    jobs = [_job(1, "test-ubuntu-32bit", step="test"), _job(2, "test-alpine-32bit", step="Run 32-bit Alpine tests"),
            _job(3, "test-freebsd", step="test")]
    unexplained = []
    client = MagicMock()
    client.download_job_log.side_effect = lambda _repo, job_id: {
        1: exception, 2: exception, 3: "Error updating repositories!\n",
    }[job_id]
    tests, (failure,) = find_unrecorded_failures(client, "valkey-io/valkey", {}, jobs=jobs, unexplained=unexplained)

    assert tests == []
    assert failure.summary == ("[exception]: Executing test client: ERR value is out of range.",)
    assert [ref.job for ref in failure.jobs] == ["test-ubuntu-32bit", "test-alpine-32bit"]
    assert unexplained == ["test-freebsd (test)"]


def test_each_cause_a_job_prints_is_its_own_failure():
    """A job's two causes are two issues, so one of them failing alone later updates its own."""
    both = (
        "*** [err]: Sanitizer error: ==1==ERROR: AddressSanitizer: heap-use-after-free on address 0x1\n"
        "    #0 0x55 in dictFind /valkey/src/dict.c:610\n"
        "*** [err]: Valgrind error: ==2== Memcheck, a memory error detector\n"
        "==2== Invalid read of size 8\n"
        "==2==    at 0x4A: quicklistNext (quicklist.c:120)\n"
    )
    later = "*** [err]: Valgrind error: ==7== Memcheck, a memory error detector\n==7== Invalid read of size 8\n" \
            "==7==    at 0x4B: quicklistNext (quicklist.c:121)\n"
    _tests, first = _find([_job(1, "test-valgrind-test (unit)")], {1: both})
    _tests, (alone,) = _find([_job(2, "test-valgrind-test (unit)")], {2: later})

    assert [f.summary for f in first] == [
        ("[err]: Sanitizer error: ERROR: AddressSanitizer: heap-use-after-free at dictFind",),
        ("[err]: Valgrind error: Invalid read of size 8 at quicklistNext",),
    ]
    assert issue_renderer.job_fingerprint_for(alone) == issue_renderer.job_fingerprint_for(first[1])


def _exception(test_file, line):
    return (
        "[exception]: Executing test client: ERR value is out of range.\n"
        " in valkey::__dispatch__ at tests/support/valkey.tcl:116\n"
        " in r at tests/test_helper.tcl:196\n"
        f" in r at {test_file}:{line}\n"
        " in test at tests/support/test.tcl:262\n"
        "\n"
        "Killing still running Valkey server 12431\n"
    )


def test_an_exception_is_located_past_its_own_text_and_a_long_framework_stack():
    """Daily job 116176011203: the error's text, then 30 framework frames, then the test file."""
    log = (
        "[exception]: Executing test client: missing operator at _@_\n"
        'in expression "...s -6 role] == "slave" _@_$$\n'
        '                [s ...".\n'
        " in wait_for_condition at tests/support/test.tcl:139\n"
        + " in start_server at tests/support/server.tcl:769\n"
          " in start_server at tests/support/cluster_util.tcl:292\n" * 15
        + " in start_cluster at tests/support/cluster_util.tcl:292\n"
        " in execute_test_file at tests/unit/cluster/faster-failover.tcl:181\n"
        " in execute_test_file at tests/test_helper.tcl:148\n"
        "\n"
        "##[error]Process completed with exit code 1.\n"
    )
    _tests, (failure,) = _find([_job(1, "test-ubuntu-jemalloc")], {1: log})
    assert failure.summary == (
        "[exception]: Executing test client: missing operator at _@_ at tests/unit/cluster/faster-failover.tcl",
    )


def test_an_exception_without_a_stack_is_not_located_by_a_later_line():
    log = (
        "[exception]: Executing test client: I/O error reading reply.\n"
        "Killing still running Valkey server 12431\n"
        " in r at tests/unit/type/zset.tcl:1559\n"  # not this exception's: the log moved on
    )
    _tests, (failure,) = _find([_job(1, "test-ubuntu-jemalloc")], {1: log})
    assert failure.summary == ("[exception]: Executing test client: I/O error reading reply.",)


def test_an_exception_is_located_by_the_first_test_file_in_its_stack():
    """Seen in Daily run 39082305846: the frames above it are the test framework's own."""
    jobs = [_job(1, "test-ubuntu-32bit"), _job(2, "test-alpine-32bit"), _job(3, "test-ubuntu-jemalloc")]
    _tests, failures = _find(jobs, {1: _exception("tests/unit/type/zset.tcl", 1559),
                                    2: _exception("tests/unit/type/zset.tcl", 1562),
                                    3: _exception("tests/unit/type/incr.tcl", 88)})

    assert [(f.summary, [ref.job for ref in f.jobs]) for f in failures] == [
        (("[exception]: Executing test client: ERR value is out of range. at tests/unit/type/zset.tcl",),
         ["test-ubuntu-32bit", "test-alpine-32bit"]),
        (("[exception]: Executing test client: ERR value is out of range. at tests/unit/type/incr.tcl",),
         ["test-ubuntu-jemalloc"]),
    ]


def test_a_valgrind_report_is_identified_by_its_first_error_and_frames():
    def report(pid, function):
        return (
            f"*** [err]: Valgrind error: =={pid}== Memcheck, a memory error detector\n"
            f"=={pid}== Copyright (C) 2002-2022, and GNU GPL'd, by Julian Seward et al.\n"
            f"=={pid}== Invalid read of size 8\n"
            f"=={pid}==    at 0x4A1B2C: {function} (quicklist.c:120)\n"
            f"=={pid}==    by 0x4A2D3E: listTypeNext (t_list.c:88)\n"
            f"=={pid}==    by 0x4F0011: lrangeGenericCommand (t_list.c:640)\n"
            f"=={pid}==    by 0x4F0099: call (server.c:3900)\n"
        )

    jobs = [_job(1, "test-valgrind-test (unit)"), _job(2, "test-valgrind-no-malloc-usable-size-test (unit)"),
            _job(3, "test-valgrind-test (integration)")]
    _tests, failures = _find(jobs, {1: report(101, "quicklistNext"), 2: report(202, "quicklistNext"),
                                    3: report(303, "quicklistGetIterator")})

    assert [f.summary for f in failures] == [
        ("[err]: Valgrind error: Invalid read of size 8 at quicklistNext < listTypeNext < lrangeGenericCommand",),
        ("[err]: Valgrind error: Invalid read of size 8 at quicklistGetIterator < listTypeNext < lrangeGenericCommand",),
    ]
    assert [len(f.jobs) for f in failures] == [2, 1]


def test_a_leak_is_identified_by_where_it_was_allocated_not_by_the_allocator():
    """Seen in Daily: the leak's top frames are malloc and zmalloc, the same for every leak."""
    def leak(pid, size, record, caller):
        return (
            f"*** [err]: Valgrind error: =={pid}== Memcheck, a memory error detector\n"
            f"=={pid}== {size} bytes in 3 blocks are definitely lost in loss record {record} of 1,501\n"
            f"=={pid}==    at 0x4848899: malloc (vg_replace_malloc.c:381)\n"
            f"=={pid}==    by 0x2A1B2C: ztrymalloc_usable_internal (zmalloc.c:155)\n"
            f"=={pid}==    by 0x2A1C3D: zmalloc_usable (zmalloc.c:180)\n"
            f"=={pid}==    by 0x2B0011: {caller} (module.c:900)\n"
            f"=={pid}==    by 0x2B0099: moduleLoad (module.c:1200)\n"
        )

    jobs = [_job(1, "test-valgrind-test (unit)"), _job(2, "test-valgrind-test (misc)")]
    _tests, failures = _find(jobs, {1: leak(11, "49,170", "1,489", "RM_Alloc"), 2: leak(22, "49,194", "1,471", "RM_Alloc")})

    (failure,) = failures
    assert failure.summary == (
        "[err]: Valgrind error: 49,170 bytes in 3 blocks are definitely lost at RM_Alloc < moduleLoad",
    )


def test_a_sanitizer_report_is_identified_by_its_error_not_the_server_log():
    log = (
        "*** [err]: Sanitizer error: 4321:M 17 Oct 2026 03:00:01.123 * Server initialized\n"
        "4321:M 17 Oct 2026 03:00:01.124 * Ready to accept connections tcp\n"
        "==4321==ERROR: AddressSanitizer: heap-use-after-free on address 0x602000001234 at pc 0x55d1\n"
        "    #0 0x55d1a2 in dictFind /valkey/src/dict.c:610\n"
        "    #1 0x55d1b3 in lookupKey /valkey/src/db.c:110\n"
    )
    _tests, (failure,) = _find([_job(1, "test-sanitizer-address (gcc)")], {1: log})
    assert failure.summary == (
        "[err]: Sanitizer error: ERROR: AddressSanitizer: heap-use-after-free at dictFind < lookupKey",
    )


def test_a_unit_test_job_that_names_no_failed_test_is_reported_not_filed():
    """With no ``[  FAILED  ] Suite.Test (N ms)`` line (the binary crashed, or would
    not build), nothing names the cause well enough to file it."""
    unexplained = []
    client = MagicMock()
    client.download_job_log.return_value = "make[1]: *** [Makefile:12: test-unit] Segmentation fault\n"
    _tests, jobs = find_unrecorded_failures(
        client, "valkey-io/valkey", {}, jobs=[_job(1, "test-ubuntu-jemalloc", step="unittest")],
        unexplained=unexplained,
    )
    assert jobs == [] and unexplained == ["test-ubuntu-jemalloc (unittest)"]


@pytest.mark.parametrize("step", ["Install EPEL", "Set up job", "Run actions/checkout@de0f", "Upload test failures",
                                  "Initialize containers", "testprep", "Build libbacktrace"])
def test_a_setup_step_failure_without_a_summary_is_not_reported(step):
    """A mirror or runner outage is not the repository's failure."""
    _tests, failures = _find([_job(1, "test-almalinux9-tls-module-no-tls", step=step)],
                             {1: "Error: Failed to download metadata for repo 'extras'\n"})
    assert failures == []


def test_a_setup_step_failure_that_printed_a_summary_is_still_reported():
    _tests, failures = _find([_job(1, "test-ubuntu-jemalloc", step="Install gtest")],
                             {1: "*** [err]: something the tests printed\n"})
    assert [f.job for f in failures] == ["test-ubuntu-jemalloc"]


def test_a_merged_failure_lists_every_job_on_its_issue():
    failure = JobFailure(job="a (unit)", url="https://job/1", step="test", summary=("[err]: x",),
                         jobs=[JobReference("a (unit)", "https://job/1"),
                               JobReference("b (unit)", "https://job/2")])
    content = issue_renderer.render_job_issue(failure, "<!-- m -->", 1)
    assert "`a (unit)`: [job log](https://job/1)" in content.body and "`b (unit)`: [job log](https://job/2)" in content.body
    assert "[`b (unit)`](https://job/2)" in content.comment
    assert issue_renderer.job_marker("a (unit)") in content.body


def test_daily_fingerprints_are_unchanged():
    # Existing Daily issues must keep matching.
    failure = UniqueFailure("t", "tests/unit/a.tcl")
    from scripts.common.incidents import compute_fingerprint

    assert issue_renderer.fingerprint_for(failure) == compute_fingerprint(
        namespace=(issue_renderer.MARKER_NAMESPACE, "t", "tests/unit/a.tcl"), shapes=(),
    )


# The tail of a real ``make test-unit`` log: gtest's own lines, colours and
# timestamps included, and the list gtest repeats the failure in at the end.
_GTEST = (
    "2026-10-19T20:57:53.8168531Z [4/299] Crc64Test.TestCrc64 (3 ms)\n"
    "2026-10-19T20:57:53.8174333Z \x1b[0;32m[ RUN      ] \x1b[mCrc64Test.TestCrc64\n"
    "2026-10-19T20:57:53.8174970Z test_crc64.cpp:29: Failure\n"
    "2026-10-19T20:57:53.8175536Z Expected equality of these values:\n"
    "2026-10-19T20:57:53.8177982Z     Which is: 16845390139448941002\n"
    "2026-10-19T20:57:53.8181827Z [calcula]: CRC64 '123456789'\n"
    "2026-10-19T20:57:53.8183082Z \x1b[0;31m[  FAILED  ] \x1b[mCrc64Test.TestCrc64 (0 ms)\n"
    "2026-10-19T20:57:53.8187230Z \x1b[0;31m[  FAILED  ] \x1b[m1 test, listed below:\n"
    "2026-10-19T20:57:53.8188482Z \x1b[0;31m[  FAILED  ] \x1b[mCrc64Test.TestCrc64\n"
    "2026-10-19T20:57:53.8189274Z  1 FAILED TEST\n"
    "2026-10-19T20:57:53.8189877Z [4/299] Crc64Test.TestCrc64 returned with exit code 1 (3 ms)\n"
)


def test_a_failed_c_unit_test_is_a_job_failure_named_for_the_test():
    """runtest prints nothing for it, so before this it was only ever "unexplained"."""
    tests, jobs = _find([_job(1, "test-ubuntu-jemalloc", step="unittest"),
                         _job(2, "test-ubuntu-arm", step="unittest")], {1: _GTEST, 2: _GTEST})

    assert tests == []
    assert [(job.step, job.summary, [ref.job for ref in job.jobs]) for job in jobs] == [
        ("unittest", ("[err]: Unit test Crc64Test.TestCrc64 failed in test_crc64.cpp",),
         ["test-ubuntu-jemalloc", "test-ubuntu-arm"]),
    ]
    content = issue_renderer.render_job_issue(jobs[0], "<!-- marker -->", 1)
    assert content.title == "[JOB-FAILURE] Unit test Crc64Test.TestCrc64 failed in test_crc64.cpp in Daily"


def test_a_c_unit_tests_issue_does_not_change_when_its_assertion_moves():
    moved = _GTEST.replace("test_crc64.cpp:29:", "test_crc64.cpp:31:").replace("16845390139448941002", "7")
    _, before = _find([_job(1, "test-ubuntu-jemalloc", step="unittest")], {1: _GTEST})
    _, after = _find([_job(1, "test-ubuntu-jemalloc", step="unittest")], {1: moved})

    assert issue_renderer.job_fingerprint_for(before[0]) == issue_renderer.job_fingerprint_for(after[0])


def test_a_c_unit_tests_issue_does_not_change_with_the_build_dirs_path_or_a_later_assertion():
    """gtest prints the file as the compiler saw it: a CMake or container build gives a path."""
    pathed = _GTEST.replace("test_crc64.cpp:29:", "/__w/valkey/valkey/src/unit/test_crc64.cpp:29:")
    later = _GTEST.replace(
        "[calcula]: CRC64 '123456789'\n",
        "[calcula]: CRC64 '123456789'\n2026-10-19T20:57:53.8182000Z test_helpers.h:12: Failure\n",
    )
    _, plain = _find([_job(1, "test-ubuntu-jemalloc", step="unittest")], {1: _GTEST})
    _, with_path = _find([_job(1, "test-ubuntu-jemalloc", step="unittest")], {1: pathed})
    _, with_later = _find([_job(1, "test-ubuntu-jemalloc", step="unittest")], {1: later})

    assert with_path[0].summary == with_later[0].summary == plain[0].summary == (
        "[err]: Unit test Crc64Test.TestCrc64 failed in test_crc64.cpp",
    )


def test_each_failed_c_unit_test_is_its_own_failure_and_a_parameterized_one_keeps_its_name():
    log = (
        "[ RUN      ] Crc64Test.TestCrc64\n"
        "test_crc64.cpp:29: Failure\n"
        "[  FAILED  ] Crc64Test.TestCrc64 (0 ms)\n"
        "[ RUN      ] Sizes/HashtableTest.Grow/2\n"
        "[  FAILED  ] Sizes/HashtableTest.Grow/2, where GetParam() = 64 (12 ms)\n"
    )
    _, jobs = _find([_job(1, "test-ubuntu-jemalloc", step="unittest")], {1: log})

    assert [job.summary for job in jobs] == [
        ("[err]: Unit test Crc64Test.TestCrc64 failed in test_crc64.cpp",),
        # No ``file:line: Failure`` before it (a crash, or a FAIL() with no location).
        ("[err]: Unit test Sizes/HashtableTest.Grow/2 failed",),
    ]
