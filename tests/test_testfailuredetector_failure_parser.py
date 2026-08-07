"""Unit tests for test failure parse/dedup logic."""

from __future__ import annotations

import pytest

from scripts.test_failure_detector.parse_failures import (
    FailureType,
    UniqueFailure,
    normalize_error_identity,
    parse_and_deduplicate,
    scrub_volatile_tokens,
)

# --- Fixture data mimicking real all-test-failures.json ---

SAMPLE_ALL_FAILURES = {
    "test-ubuntu-latest": {
        "integration": [
            {
                "test_name": "PSYNC2 test",
                "test_file": "tests/integration/replication-psync.tcl",
                "error": "Expected replica to be in sync within 5000ms",
            },
            {
                "test_name": "Lazy free of stream",
                "test_file": "tests/unit/lazyfree.tcl",
                "error": "assertion:Expected 0 == 1",
            },
        ],
        "sentinel": [
            {
                "test_name": "PSYNC2 test",
                "test_file": "tests/integration/replication-psync.tcl",
                "error": "Expected replica to be in sync within 5000ms",
            },
        ],
    },
    "test-ubuntu-latest-cluster": {
        "integration": [
            {
                "test_name": "PSYNC2 test",
                "test_file": "tests/integration/replication-psync.tcl",
                "error": "Expected replica to be in sync within 5000ms",
            },
            {
                "test_name": "Cluster slot migration",
                "test_file": "tests/unit/cluster.tcl",
                "error": "timeout waiting for cluster to be stable",
            },
        ],
    },
}

SAMPLE_JOB_URLS = {
    "test-ubuntu-latest": "https://github.com/valkey-io/valkey/actions/runs/123/job/456",
    "test-ubuntu-latest-cluster": "https://github.com/valkey-io/valkey/actions/runs/123/job/789",
}


class TestParseAndDeduplicate:
    def test_deduplicates_same_test_across_jobs(self) -> None:
        """Same test failing in multiple jobs should produce one UniqueFailure."""
        results = parse_and_deduplicate(SAMPLE_ALL_FAILURES, SAMPLE_JOB_URLS)

        psync_failures = [f for f in results if f.test_name == "PSYNC2 test"]
        assert len(psync_failures) == 1

        psync = psync_failures[0]
        # Should appear in both jobs (but deduplicated within test-ubuntu-latest)
        job_names = [j.job for j in psync.jobs]
        assert "test-ubuntu-latest" in job_names
        assert "test-ubuntu-latest-cluster" in job_names
        assert len(psync.jobs) == 2

    def test_deduplicates_same_test_across_suites_within_job(self) -> None:
        """Same test in multiple suites of the same job should only record the job once."""
        results = parse_and_deduplicate(SAMPLE_ALL_FAILURES, SAMPLE_JOB_URLS)

        psync_failures = [f for f in results if f.test_name == "PSYNC2 test"]
        assert len(psync_failures) == 1

        psync = psync_failures[0]
        # test-ubuntu-latest appears in both integration and sentinel suites,
        # but should only be recorded once
        ubuntu_refs = [j for j in psync.jobs if j.job == "test-ubuntu-latest"]
        assert len(ubuntu_refs) == 1

    def test_unique_failures_count(self) -> None:
        """Should produce 3 unique failures from the sample data."""
        results = parse_and_deduplicate(SAMPLE_ALL_FAILURES, SAMPLE_JOB_URLS)
        assert len(results) == 3

        names = {f.test_name for f in results}
        assert names == {"PSYNC2 test", "Lazy free of stream", "Cluster slot migration"}

    def test_job_urls_are_attached(self) -> None:
        """Job references should include the URL from job_urls mapping."""
        results = parse_and_deduplicate(SAMPLE_ALL_FAILURES, SAMPLE_JOB_URLS)

        cluster_failures = [f for f in results if f.test_name == "Cluster slot migration"]
        assert len(cluster_failures) == 1
        assert cluster_failures[0].jobs[0].url == SAMPLE_JOB_URLS["test-ubuntu-latest-cluster"]

    def test_missing_job_url_gives_empty_string(self) -> None:
        """If a job name isn't in job_urls, the URL should be empty."""
        results = parse_and_deduplicate(SAMPLE_ALL_FAILURES, {})

        for failure in results:
            for job_ref in failure.jobs:
                assert job_ref.url == ""

    def test_empty_failures_returns_empty_list(self) -> None:
        results = parse_and_deduplicate({}, {})
        assert results == []

    def test_no_failures_in_suites_returns_empty(self) -> None:
        """Jobs with empty failure lists should produce no results."""
        data = {"job-1": {"suite-a": [], "suite-b": []}}
        results = parse_and_deduplicate(data, {})
        assert results == []

    def test_entries_missing_test_name_grouped_by_error(self) -> None:
        """Entries without test_name but with error are kept (grouped by error
        identity). This supports sanitizer/valgrind nameless failures."""
        data = {
            "job-1": {
                "suite": [
                    {"test_file": "foo.tcl", "error": "oops"},  # no test_name
                    {"test_name": "real test", "test_file": "bar.tcl", "error": "err"},
                ]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 2
        named = [f for f in results if f.test_name == "real test"]
        nameless = [f for f in results if not f.test_name]
        assert len(named) == 1
        assert len(nameless) == 1
        assert nameless[0].error == "oops"

    def test_entries_missing_test_name_and_error_are_skipped(self) -> None:
        """Entries with neither test_name nor error cannot be fingerprinted."""
        data = {
            "job-1": {
                "suite": [
                    {"test_file": "foo.tcl", "error": ""},  # no name, no error
                    {"test_name": "real test", "test_file": "bar.tcl", "error": "err"},
                ]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].test_name == "real test"

    def test_non_string_field_values_do_not_abort_the_batch(self) -> None:
        """A producer bug that emits a non-string field (int PID, null) must
        degrade to one bad entry, not a TypeError that loses the whole run."""
        data = {
            "job-1": {
                "suite": [
                    {"test_name": "", "test_file": "f.tcl",
                     "type": "sanitizer", "error": 12345},
                    {"test_name": None, "test_file": "g.tcl", "error": "real"},
                    {"test_name": "good", "test_file": "h.tcl", "error": "err"},
                ]
            }
        }
        results = parse_and_deduplicate(data, {})
        names = {f.test_name for f in results}
        assert "good" in names
        # The int-error entry has no usable identity and is skipped; the
        # None-name entry still groups by its error text.
        assert any(f.error == "real" for f in results)

    def test_entries_missing_test_file_with_error_are_kept(self) -> None:
        """Entries with test_name but no test_file are kept when they have
        a test_name (the name itself provides identity)."""
        data = {
            "job-1": {
                "suite": [
                    {"test_name": "orphan", "error": "oops"},  # no test_file
                ]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].test_name == "orphan"
        assert results[0].test_file == ""

    def test_preserves_error_from_first_occurrence(self) -> None:
        """The error message should come from the first occurrence."""
        data = {
            "job-1": {"suite": [{"test_name": "t", "test_file": "f.tcl", "error": "first error"}]},
            "job-2": {"suite": [{"test_name": "t", "test_file": "f.tcl", "error": "second error"}]},
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].error == "first error"

    def test_display_name(self) -> None:
        f = UniqueFailure(test_name="my test", test_file="tests/foo.tcl")
        assert f.display_name == "my test in tests/foo.tcl"

    def test_separator_in_test_name_does_not_collide(self) -> None:
        """Two distinct (name, file) pairs that would join to the same string
        under a ' in ' separator must stay separate. The grouping key is a
        tuple, so a separator appearing inside a test name can't cause a
        collision."""
        data = {
            "job-1": {
                "suite": [
                    # Under f"{name} in {file}" both collapse to
                    # "foo in bar.tcl in baz.tcl", but they are different tests.
                    {"test_name": "foo in bar.tcl", "test_file": "baz.tcl", "error": "a"},
                    {"test_name": "foo", "test_file": "bar.tcl in baz.tcl", "error": "b"},
                ]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 2
        assert {(f.test_name, f.test_file) for f in results} == {
            ("foo in bar.tcl", "baz.tcl"),
            ("foo", "bar.tcl in baz.tcl"),
        }


# --- Tests for FailureType and typed parsing ---


class TestNormalizeErrorIdentity:
    """Test that error normalization strips volatile tokens and keeps
    the meaningful error type/location for stable fingerprinting."""

    @pytest.mark.parametrize("error,volatile", [
        ("==12345== Invalid read of size 4\n"
         "==12345==    at 0xABCDEF: someFunc (file.c:123)", "12345"),
        ("==999== Invalid read of size 4\n"
         "==999==    at 0xDEADBEEF: func (file.c:10)", "deadbeef"),
        ("ERROR: can't open /tmp/valkey-test-abc123/config\nSanitizer error", "/tmp/"),
        ("\033[31m[  TIMEOUT ]\033[0m test timed out", "\033"),
        ("Error connecting on port=6379 pid 12345\nInvalid operation", "6379"),
    ])
    def test_volatile_tokens_are_stripped(self, error: str, volatile: str) -> None:
        """A token that changes between runs of one bug must not reach the
        identity, or the failure gets a fresh issue every night."""
        assert volatile not in normalize_error_identity(error).lower()

    def test_same_error_different_pids_normalizes_equal(self) -> None:
        e1 = "==100== Invalid read of size 4\n==100==    at 0xAAA: dictResize (dict.c:55)"
        e2 = "==200== Invalid read of size 4\n==200==    at 0xBBB: dictResize (dict.c:55)"
        assert normalize_error_identity(e1) == normalize_error_identity(e2)

    def test_different_errors_normalize_different(self) -> None:
        e1 = "==1== Invalid read of size 4\n==1==    at 0xA: dictResize (dict.c:55)"
        e2 = "==1== Invalid write of size 8\n==1==    at 0xA: hashExpand (hash.c:99)"
        assert normalize_error_identity(e1) != normalize_error_identity(e2)

    def test_sanitizer_frames_distinguish_allocation_sites(self) -> None:
        """Two LSan leaks identical except for the allocation site must not
        collapse: sanitizer '#N 0xADDR in func' frames feed the identity."""
        template = (
            "==1==ERROR: LeakSanitizer: detected memory leaks\n"
            "Direct leak of 128 byte(s) in 4 object(s) allocated from:\n"
            "    #0 0x7fc342efd9c7 in malloc asan_malloc_linux.cpp:69\n"
            "    #1 0x5623ea63298d in ztrymalloc_usable_internal src/zmalloc.c:172\n"
            "    #2 0x5623ea64f111 in {site}\n"
            "\n"
            "SUMMARY: AddressSanitizer: 128 byte(s) leaked in 4 allocation(s)."
        )
        e1 = template.format(site="kvstoreInit src/kvstore.c:88")
        e2 = template.format(site="clusterInit src/cluster.c:1042")
        assert normalize_error_identity(e1) != normalize_error_identity(e2)

    def test_sanitizer_frames_stable_across_reruns(self) -> None:
        """Same leak with different PIDs/addresses/sizes keeps one identity."""
        e1 = (
            "==107611==ERROR: LeakSanitizer: detected memory leaks\n"
            "Direct leak of 128 byte(s) in 4 object(s) allocated from:\n"
            "    #1 0x5623ea63298d in kvstoreInit src/kvstore.c:88\n"
            "SUMMARY: AddressSanitizer: 128 byte(s) leaked in 4 allocation(s)."
        )
        e2 = (
            "==209344==ERROR: LeakSanitizer: detected memory leaks\n"
            "Direct leak of 96 byte(s) in 4 object(s) allocated from:\n"
            "    #1 0x559911aa22bb in kvstoreInit src/kvstore.c:88\n"
            "SUMMARY: AddressSanitizer: 96 byte(s) leaked in 4 allocation(s)."
        )
        assert normalize_error_identity(e1) == normalize_error_identity(e2)

    def test_valgrind_error_summary_excluded_from_identity(self) -> None:
        """One leak reported by two runs must keep one identity even when
        valgrind's "ERROR SUMMARY: N errors from N contexts" line lands inside
        the identity window for one run and past it for the other. The count
        varies run to run, so the line must not reach the identity at all.
        """
        stack = (
            "==1== 41 bytes in 1 blocks are definitely lost in loss record 900 of 1,111\n"
            "==1==    at 0x4846828: malloc (vgpreload_memcheck.so)\n"
            "==1==    by 0x318A40: ztrymalloc_usable_internal (zmalloc.c:172)\n"
            "==1==    by 0x1E80D6: debugCommand (debug.c:569)\n"
        )
        with_summary = stack + "==1== ERROR SUMMARY: 36 errors from 36 contexts (suppressed: 0 from 0)"
        without_summary = stack + "==1== \n==1== extra reachable-block line\n==1== another line"
        assert normalize_error_identity(with_summary) == normalize_error_identity(without_summary)

    def test_startup_identity_stable_across_reruns(self) -> None:
        """Same startup failure with different temp dirs/ports deduplicates."""
        template = (
            "Can't start src/valkey-server\n"
            "CONFIGURATION:\n"
            "dir ./tests/tmp/server.{run}\n"
            "port {port}\n"
            "ERROR:\n"
            "Unable to bind unix socket: Address already in use"
        )
        e1 = template.format(run="31337.5", port=21111)
        e2 = template.format(run="40021.9", port=21987)
        assert normalize_error_identity(e1) == normalize_error_identity(e2)

    def test_sanitizer_output_keeps_error_type(self) -> None:
        error = (
            "==555== ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1234\n"
            "==555==    at 0xABC: serverCron (server.c:300)\n"
            "==555== SUMMARY: AddressSanitizer: heap-buffer-overflow server.c:300 in serverCron"
        )
        result = normalize_error_identity(error)
        assert "heap-buffer-overflow" in result

    def test_empty_error_returns_empty(self) -> None:
        assert normalize_error_identity("") == ""


class TestAllocationWrappersOnlyAreSkipped:
    """zmalloc.c and sds.c hold allocation wrappers, whose presence depends on
    the toolchain's inlining, and ordinary code that can itself be the faulting
    frame. Skipping the whole file discarded the second kind, so two distinct
    defects in one file reduced to the same identity."""

    def _access(self, func: str, line: int) -> str:
        return (
            "Invalid write of size 1\n"
            f"   at 0x1: {func} (sds.c:{line})\n"
            "   by 0x2: debugCommand (debug.c:569)\n"
        )

    def test_two_defects_in_one_file_stay_distinct(self) -> None:
        assert (
            normalize_error_identity(self._access("sdscatlen", 102))
            != normalize_error_identity(self._access("sdsrange", 640))
        )

    def test_an_allocation_wrapper_is_still_skipped(self) -> None:
        """A leak's anchor must name the leaking code path, not the allocator
        chain above it, or one leak reads differently per compiler."""
        leak = (
            "49 bytes in 1 blocks are definitely lost\n"
            "   at 0x1: malloc (vg_replace_malloc.c:381)\n"
            "   by 0x2: ztrymalloc_usable_internal (zmalloc.c:172)\n"
            "   by 0x3: _sdsnewlen (sds.c:102)\n"
            "   by 0x4: debugCommand (debug.c:569)\n"
        )
        assert "debugCommand (debug.c:569)" in normalize_error_identity(leak)
        assert "sdsnewlen" not in normalize_error_identity(leak)


class TestParseWithTypes:
    """Test the extended parsing that handles the 'type' field."""

    def test_backward_compat_no_type_field_defaults_to_assertion(self) -> None:
        data = {"job-1": {"suite": [{"test_name": "t", "test_file": "f.tcl", "error": "err"}]}}
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].failure_type == FailureType.ASSERTION

    def test_assertion_type_explicit(self) -> None:
        data = {"job-1": {"suite": [{"test_name": "t", "test_file": "f.tcl", "type": "assertion", "error": "err"}]}}
        results = parse_and_deduplicate(data, {})
        assert results[0].failure_type == FailureType.ASSERTION

    def test_sanitizer_entry_without_test_name(self) -> None:
        data = {
            "test-sanitizer-address-gcc": {
                "valkey": [{
                    "test_name": "",
                    "test_file": "tests/unit/expire.tcl",
                    "type": "sanitizer",
                    "error": "Sanitizer error: ==123== ERROR: AddressSanitizer: heap-buffer-overflow"
                }]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].failure_type == FailureType.SANITIZER
        assert results[0].test_file == "tests/unit/expire.tcl"
        assert not results[0].has_test_identity

    def test_same_sanitizer_error_across_jobs_deduplicates(self) -> None:
        """Same bug with different PIDs/addresses from different jobs collapses."""
        data = {
            "test-sanitizer-address-gcc": {
                "valkey": [{
                    "test_name": "",
                    "test_file": "tests/unit/expire.tcl",
                    "type": "sanitizer",
                    "error": "==111== ERROR: AddressSanitizer: heap-buffer-overflow\n==111==    at 0xAAA: dictResize (dict.c:100)"
                }]
            },
            "test-sanitizer-address-clang": {
                "valkey": [{
                    "test_name": "",
                    "test_file": "tests/unit/expire.tcl",
                    "type": "sanitizer",
                    "error": "==222== ERROR: AddressSanitizer: heap-buffer-overflow\n==222==    at 0xBBB: dictResize (dict.c:100)"
                }]
            },
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert len(results[0].jobs) == 2

    def test_same_valgrind_error_different_test_files_deduplicates(self) -> None:
        """Same leak detected after different test files produces one failure."""
        data = {
            "test-valgrind-test": {
                "valkey": [{
                    "test_name": "",
                    "test_file": "tests/unit/expire.tcl",
                    "type": "valgrind",
                    "error": "==1== Invalid read of size 4\n==1==    at 0xA: dictResize (dict.c:100)"
                }]
            },
            "test-valgrind-misc": {
                "valkey": [{
                    "test_name": "",
                    "test_file": "tests/unit/cluster.tcl",
                    "type": "valgrind",
                    "error": "==2== Invalid read of size 4\n==2==    at 0xB: dictResize (dict.c:100)"
                }]
            },
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert len(results[0].jobs) == 2

    def test_different_valgrind_errors_stay_separate(self) -> None:
        """Different bugs produce separate failures even from the same job."""
        data = {
            "test-valgrind-test": {
                "valkey": [
                    {
                        "test_name": "",
                        "test_file": "tests/unit/expire.tcl",
                        "type": "valgrind",
                        "error": "==1== Invalid read of size 4\n==1==    at 0xA: dictResize (dict.c:100)"
                    },
                    {
                        "test_name": "",
                        "test_file": "tests/unit/expire.tcl",
                        "type": "valgrind",
                        "error": "==1== Invalid write of size 8\n==1==    at 0xA: hashExpand (hash.c:200)"
                    },
                ]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 2

    def test_timeout_with_test_name(self) -> None:
        data = {
            "test-ubuntu-jemalloc": {
                "valkey": [{
                    "test_name": "PSYNC2 partial sync",
                    "test_file": "tests/integration/replication-psync.tcl",
                    "type": "timeout",
                    "error": "Test timed out"
                }]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].failure_type == FailureType.TIMEOUT
        assert results[0].test_name == "PSYNC2 partial sync"
        assert results[0].has_test_identity

    def test_unittest_failure(self) -> None:
        data = {
            "test-ubuntu-jemalloc": {
                "unittest": [{
                    "test_name": "DictTest.BasicOperations",
                    "test_file": "src/unit/valkey-unit-gtests",
                    "type": "unittest",
                    "error": "gtest FAIL"
                }]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].failure_type == FailureType.UNITTEST
        assert results[0].test_name == "DictTest.BasicOperations"

    def test_startup_failure(self) -> None:
        data = {
            "test-ubuntu-jemalloc": {
                "valkey": [{
                    "test_name": "",
                    "test_file": "tests/unit/cluster.tcl",
                    "type": "startup",
                    "error": "Can't start /path/to/valkey-server\nCONFIGURATION:\n...\nERROR:\nFailed listening on port 6379"
                }]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].failure_type == FailureType.STARTUP
        assert not results[0].has_test_identity

    def test_exception_failure(self) -> None:
        data = {
            "test-ubuntu-jemalloc": {
                "valkey": [{
                    "test_name": "",
                    "test_file": "",
                    "type": "exception",
                    "error": "can't read \"fd\": no such variable"
                }]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].failure_type == FailureType.EXCEPTION

    def test_unknown_type_classified_as_exception(self) -> None:
        data = {"job": {"s": [{"test_name": "t", "test_file": "f.tcl", "type": "bogus", "error": "x"}]}}
        results = parse_and_deduplicate(data, {})
        assert results[0].failure_type == FailureType.EXCEPTION

    def test_entry_with_no_test_name_and_no_error_is_skipped(self) -> None:
        data = {"job": {"s": [{"test_name": "", "test_file": "f.tcl", "type": "sanitizer", "error": ""}]}}
        results = parse_and_deduplicate(data, {})
        assert results == []

    def test_display_name_for_nameless_failure(self) -> None:
        f = UniqueFailure(test_name="", test_file="tests/unit/expire.tcl", failure_type=FailureType.SANITIZER)
        assert "[sanitizer]" in f.display_name
        assert "expire.tcl" in f.display_name

    def test_display_name_for_nameless_failure_no_file(self) -> None:
        f = UniqueFailure(test_name="", test_file="", failure_type=FailureType.EXCEPTION)
        assert "[exception]" in f.display_name
        assert "unknown" in f.display_name

    def test_mixed_types_in_single_run(self) -> None:
        """A run can produce failures of multiple types from the same job."""
        data = {
            "test-valgrind-test": {
                "valkey": [
                    {"test_name": "PSYNC2 test", "test_file": "tests/integration/replication-psync.tcl", "type": "assertion", "error": "Expected sync"},
                    {"test_name": "", "test_file": "tests/integration/replication-psync.tcl", "type": "valgrind", "error": "==1== Invalid read\n==1==    at 0xA: func (x.c:1)"},
                    {"test_name": "PSYNC2 test", "test_file": "tests/integration/replication-psync.tcl", "type": "timeout", "error": "Test timed out"},
                ]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 3
        types = {f.failure_type for f in results}
        assert types == {FailureType.ASSERTION, FailureType.VALGRIND, FailureType.TIMEOUT}


class TestVolatileTestNameDemotion:
    """Volatile test names (pid:NNN, hang) are runner-state artifacts, not real
    test identities. They must be demoted to nameless so every run with a
    different PID does not mint a new issue."""

    def test_pid_colon_number_demoted(self) -> None:
        data = {
            "job": {
                "valkey": [{
                    "test_name": "pid:92663",
                    "test_file": "tests/integration/replication.tcl",
                    "type": "timeout",
                    "error": "Test timed out",
                }]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].test_name == ""
        assert results[0].test_file == "tests/integration/replication.tcl"

    def test_hang_demoted(self) -> None:
        data = {
            "job": {
                "valkey": [{
                    "test_name": "hang",
                    "test_file": "tests/unit/cluster.tcl",
                    "type": "timeout",
                    "error": "Test timed out",
                }]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].test_name == ""

    def test_different_pids_same_file_produce_one_failure(self) -> None:
        """Two entries with different volatile PIDs in the same file should
        collapse into one failure, not two."""
        data = {
            "job-a": {
                "valkey": [{
                    "test_name": "pid:111",
                    "test_file": "tests/integration/replication.tcl",
                    "type": "timeout",
                    "error": "Test timed out",
                }]
            },
            "job-b": {
                "valkey": [{
                    "test_name": "pid:222",
                    "test_file": "tests/integration/replication.tcl",
                    "type": "timeout",
                    "error": "Test timed out",
                }]
            },
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert len(results[0].jobs) == 2

    def test_real_test_name_not_demoted(self) -> None:
        """Real test names that happen to contain 'pid' are not demoted."""
        data = {
            "job": {
                "valkey": [{
                    "test_name": "PSYNC2 test repid change",
                    "test_file": "tests/integration/replication.tcl",
                    "type": "timeout",
                    "error": "Test timed out",
                }]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert results[0].test_name == "PSYNC2 test repid change"

    def test_nameless_timeouts_in_different_files_stay_separate(self) -> None:
        """After demotion, timeouts in different files must remain distinct
        issues, not collapse into one."""
        data = {
            "job": {
                "valkey": [
                    {
                        "test_name": "pid:111",
                        "test_file": "tests/integration/replication.tcl",
                        "type": "timeout",
                        "error": "Test timed out",
                    },
                    {
                        "test_name": "pid:222",
                        "test_file": "tests/unit/cluster.tcl",
                        "type": "timeout",
                        "error": "Test timed out",
                    },
                ]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 2
        files = {f.test_file for f in results}
        assert files == {
            "tests/integration/replication.tcl",
            "tests/unit/cluster.tcl",
        }


# Real-shaped Memcheck leak report (issue #91): runner wrapper prefix, tool
# banner, heap summary, then the loss record with its allocation stack.
_MEMCHECK_LEAK = """ Valgrind error: ==6554== Memcheck, a memory error detector
==6554== Copyright (C) 2002-2022, and GNU GPL'd, by Julian Seward et al.
==6554== Command: /path/to/valkey-server ./tests/tmp/valkey.conf.6549.2
==6554== HEAP SUMMARY:
==6554==     in use at exit: 1,080,661 bytes in 13,544 blocks
==6554== 49 bytes in 1 blocks are definitely lost in loss record 900 of 1,109
==6554==    at 0x4846828: malloc (in /usr/libexec/valgrind/vgpreload_memcheck-amd64-linux.so)
==6554==    by 0x3189FB: ztrymalloc_usable_internal (zmalloc.c:172)
==6554==    by 0x29072A: sdsdup (sds.c:190)
==6554==    by 0x1E8076: debugCommand (debug.c:569)
==6554==    by 0x2AD78C: call (server.c:3942)
"""


class TestValgrindLeakIdentity:
    """Memcheck leak fingerprints must anchor on the allocation stack, not on
    banner boilerplate or heap-layout coordinates that drift between runs."""

    def test_identity_excludes_banner_and_heap_summary(self) -> None:
        identity = normalize_error_identity(_MEMCHECK_LEAK)
        assert "Memcheck, a memory error detector" not in identity
        assert "HEAP SUMMARY" not in identity

    def test_identity_excludes_loss_record_and_sizes(self) -> None:
        identity = normalize_error_identity(_MEMCHECK_LEAK)
        assert "loss record" not in identity
        assert "49" not in identity

    def test_identity_includes_allocation_stack(self) -> None:
        identity = normalize_error_identity(_MEMCHECK_LEAK)
        assert "debugCommand" in identity

    def test_same_leak_across_runs_same_identity(self) -> None:
        # Next run: new PID, drifted size, moved loss record.
        rerun = (
            _MEMCHECK_LEAK.replace("6554", "7801")
            .replace("49 bytes in 1 blocks", "52 bytes in 1 blocks")
            .replace("loss record 900 of 1,109", "loss record 903 of 1,214")
            .replace("1,080,661 bytes in 13,544 blocks", "1,093,102 bytes in 13,671 blocks")
        )
        assert normalize_error_identity(_MEMCHECK_LEAK) == normalize_error_identity(rerun)

    def test_different_allocation_stack_different_identity(self) -> None:
        # Same report shape; the only difference is where the leak was allocated.
        other = _MEMCHECK_LEAK.replace(
            "debugCommand (debug.c:569)", "clusterCommand (cluster.c:123)",
        )
        assert normalize_error_identity(_MEMCHECK_LEAK) != normalize_error_identity(other)

    def test_identity_survives_loss_record_crossing_four_digits(self) -> None:
        # A four-digit loss record is scrubbed by the bare-number pattern as
        # well as the loss-record phrase. If the bare-number pattern runs
        # first it strips the digits and strands "in loss record  of", which
        # then reaches the identity and mints a second issue for one leak.
        rerun = _MEMCHECK_LEAK.replace(
            "loss record 900 of 1,109", "loss record 1001 of 1,110",
        )
        assert normalize_error_identity(_MEMCHECK_LEAK) == normalize_error_identity(rerun)
        assert "loss record" not in normalize_error_identity(rerun)

    def test_identity_stable_across_full_heap_coordinate_drift(self) -> None:
        # Every count in the report may drift between runs of one leak: the
        # size, the loss record, the total record count, and the in-use
        # totals. None of them may reach the identity.
        identities = set()
        for size, record, total in (
            ("49 bytes", "900 of 1,109", "1,080,661 bytes in 13,544 blocks"),
            ("52 bytes", "1001 of 1,110", "1,093,102 bytes in 13,671 blocks"),
            ("1,024 bytes", "12 of 998", "972,701 bytes in 13,549 blocks"),
        ):
            report = (
                _MEMCHECK_LEAK.replace("49 bytes", size)
                .replace("900 of 1,109", record)
                .replace("1,080,661 bytes in 13,544 blocks", total)
            )
            identities.add(normalize_error_identity(report))
        assert len(identities) == 1


class TestAccessWidthScrubbing:
    """One out-of-bounds access is reported at whatever width the compiler
    chose for that load, so the width drifts between builds of one bug while
    the diagnostic and the stack site identify it.
    """

    def test_access_width_does_not_split_one_bug(self) -> None:
        def report(size: str) -> str:
            return (
                f"==1== Invalid read of size {size}\n"
                "==1==    at 0xA: dictResize (dict.c:100)"
            )
        assert normalize_error_identity(report("4")) == normalize_error_identity(
            report("8")
        )

    def test_diagnostic_text_is_kept(self) -> None:
        scrubbed = scrub_volatile_tokens("Invalid read of size 4")
        assert "Invalid read of size" in scrubbed
        assert "4" not in scrubbed


class TestParenthesizedCountScrubbing:
    """LeakSanitizer writes its counts as "41 byte(s)" and "1 allocation(s)".
    A trailing word boundary cannot match after ")", and a bare "bytes?"
    branch would match the "byte" inside "byte(s)" first, so these forms need
    their own alternative or their digits reach the identity and drift.
    """

    def test_parenthesized_units_scrub_completely(self) -> None:
        for text in (
            "41 byte(s)", "1 object(s)", "1 allocation(s)", "2 leak(s)",
        ):
            assert scrub_volatile_tokens(text).strip() == "", text

    def test_no_stranded_plural_suffix(self) -> None:
        scrubbed = scrub_volatile_tokens(
            "SUMMARY: AddressSanitizer: 41 byte(s) leaked in 1 allocation(s)."
        )
        assert "(s)" not in scrubbed
        assert "41" not in scrubbed

    def test_bare_units_still_scrub(self) -> None:
        for text in ("41 bytes", "1 blocks", "1 leak", "2 leaks", "1 byte"):
            assert scrub_volatile_tokens(text).strip() == "", text

    def test_sanitizer_leak_size_drift_keeps_one_identity(self) -> None:
        def report(size: str, objects: str, allocs: str) -> str:
            return (
                " Sanitizer error: \n"
                "==6144==ERROR: LeakSanitizer: detected memory leaks\n"
                f"Direct leak of {size} in {objects} allocated from:\n"
                "    #4 0x55 in debugCommand /src/debug.c:569:9\n"
                f"SUMMARY: AddressSanitizer: {size} leaked in {allocs}.\n"
            )
        first = report("41 byte(s)", "1 object(s)", "1 allocation(s)")
        second = report("52 byte(s)", "2 object(s)", "2 allocation(s)")
        assert normalize_error_identity(first) == normalize_error_identity(second)
