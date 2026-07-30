"""Unit tests for test failure parse/dedup logic."""

from __future__ import annotations

import pytest

from scripts.test_failure_detector.parse_failures import (
    FailureType,
    JobReference,
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

    def test_strips_valgrind_pids(self) -> None:
        error = "==12345== Invalid read of size 4\n==12345==    at 0xABCDEF: someFunc (file.c:123)"
        result = normalize_error_identity(error)
        assert "12345" not in result

    def test_strips_hex_addresses(self) -> None:
        error = "==999== Invalid read of size 4\n==999==    at 0xDEADBEEF: func (file.c:10)"
        result = normalize_error_identity(error)
        assert "DEADBEEF" not in result.upper()
        assert "deadbeef" not in result.lower()

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

    def test_startup_reason_line_distinguishes_failures(self) -> None:
        """Startup blobs share the 'Can't start' header and a bare 'ERROR:'
        separator; the fatal reason on the last line must split them."""
        template = (
            "Can't start src/valkey-server\n"
            "CONFIGURATION:\n"
            "dir ./tests/tmp/server.31337.5\n"
            "port 21111\n"
            "ERROR:\n"
            "*** FATAL CONFIG FILE ERROR (Version 255.255.255) ***\n"
            "{reason}"
        )
        e1 = template.format(reason="Unable to bind unix socket: Address already in use")
        e2 = template.format(reason="argument couldn't be parsed into an integer")
        assert normalize_error_identity(e1) != normalize_error_identity(e2)

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

    def test_strips_temp_paths(self) -> None:
        error = "ERROR: can't open /tmp/valkey-test-abc123/config\nSanitizer error"
        result = normalize_error_identity(error)
        assert "/tmp/" not in result

    def test_strips_ansi_codes(self) -> None:
        error = "\033[31m[  TIMEOUT ]\033[0m test timed out"
        result = normalize_error_identity(error)
        assert "\033" not in result

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

    def test_strips_port_and_pid_annotations(self) -> None:
        error = "Error connecting on port=6379 pid 12345\nInvalid operation"
        result = normalize_error_identity(error)
        assert "6379" not in result
        assert "12345" not in result


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


def _asan_uaf(
    line: int,
    root: str = "/home/runner/work/valkey/valkey",
    caller_line: int = 3942,
) -> str:
    return (
        "==1==ERROR: AddressSanitizer: heap-use-after-free\n"
        f"    #1 0x55 in zslDeleteNode {root}/src/t_zset.c:{line}:9\n"
        f"    #2 0x55 in call {root}/src/server.c:{caller_line}:5\n"
    )


class TestSourceLocationInStackAnchor:
    """The stack anchor carries the source location of the frame that names the
    bug, so two bugs in one function stay distinct, while the caller lines and
    the runner's workspace layout stay out of the identity so one bug does not
    become a fresh issue per commit or per platform.
    """

    def test_caller_line_drift_does_not_split_one_bug(self) -> None:
        """Frames below the bug are its callers. Their lines move whenever
        unrelated code in them is edited, which is most commits, so carrying
        them would refile one leak against every such commit.
        """
        first = normalize_error_identity(_asan_uaf(1200, caller_line=3933))
        second = normalize_error_identity(_asan_uaf(1200, caller_line=3942))
        assert first == second

    def test_bug_frame_keeps_its_line_and_callers_do_not(self) -> None:
        identity = normalize_error_identity(_asan_uaf(1200, caller_line=3942))
        assert "t_zset.c:1200" in identity
        assert "server.c)" in identity
        assert "3942" not in identity

    def test_same_function_different_line_stays_distinct(self) -> None:
        first = normalize_error_identity(_asan_uaf(1200))
        second = normalize_error_identity(_asan_uaf(3455))
        assert first != second
        assert "t_zset.c:1200" in first
        assert "t_zset.c:3455" in second

    def test_runner_workspace_layout_does_not_split_one_bug(self) -> None:
        identities = {
            normalize_error_identity(_asan_uaf(1200, root))
            for root in (
                "/home/runner/work/valkey/valkey",
                "/__w/valkey/valkey",
                "/Users/runner/work/valkey/valkey",
            )
        }
        assert len(identities) == 1

    def test_four_digit_line_survives_the_bare_number_scrub(self) -> None:
        """A line number is not noise. The bare-number pattern drops runs of
        four or more digits, which would erase exactly the line numbers that
        tell two bugs in one function apart.
        """
        assert "t_zset.c:3455" in scrub_volatile_tokens(_asan_uaf(3455))

    def test_pids_are_still_scrubbed(self) -> None:
        """The line-number carve-out must not spare a genuine PID."""
        scrubbed = scrub_volatile_tokens("server started with pid 12345 ok")
        assert "12345" not in scrubbed


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


def _asan_leak(frames: str) -> str:
    return (
        " Sanitizer error: \n"
        "=================================================================\n"
        "==6349==ERROR: LeakSanitizer: detected memory leaks\n\n"
        "Direct leak of 41 byte(s) in 1 object(s) allocated from:\n"
        f"{frames}"
        "SUMMARY: AddressSanitizer: 41 byte(s) leaked in 1 allocation(s).\n"
    )


class TestSanitizerLeakIdentityAcrossToolchains:
    """The same leak reported by different compilers differs only in which
    allocation-plumbing frames were inlined away. Keeping those frames in the
    identity files one issue per compiler for a single bug."""

    # clang inlines sdsnewlen/sdsdup into their caller and reports the malloc
    # interceptor as a binary offset.
    _CLANG = _asan_leak(
        "    #0 0x55f in malloc (/home/runner/work/valkey/valkey/src/valkey-server+0x20de33)\n"
        "    #1 0x571 in ztrymalloc_usable_internal /home/runner/work/valkey/valkey/src/zmalloc.c:172:17\n"
        "    #2 0x571 in zmalloc_usable /home/runner/work/valkey/valkey/src/zmalloc.c:268:17\n"
        "    #3 0x464 in _sdsnewlen /home/runner/work/valkey/valkey/src/sds.c:102:22\n"
        "    #4 0x2a4 in debugCommand /home/runner/work/valkey/valkey/src/debug.c:569:9\n"
        "    #5 0x4b2 in call /home/runner/work/valkey/valkey/src/server.c:3942:5\n"
    )

    # gcc keeps sdsnewlen/sdsdup as frames and resolves malloc into the
    # sanitizer's own sources.
    _GCC = _asan_leak(
        "    #0 0x7f1 in malloc ../../../../src/libsanitizer/asan/asan_malloc_linux.cpp:69\n"
        "    #1 0x55d in ztrymalloc_usable_internal /home/runner/work/valkey/valkey/src/zmalloc.c:172\n"
        "    #2 0x55d in zmalloc_usable /home/runner/work/valkey/valkey/src/zmalloc.c:268\n"
        "    #3 0x55d in _sdsnewlen /home/runner/work/valkey/valkey/src/sds.c:102\n"
        "    #4 0x55d in sdsnewlen /home/runner/work/valkey/valkey/src/sds.c:169\n"
        "    #5 0x55d in sdsdup /home/runner/work/valkey/valkey/src/sds.c:190\n"
        "    #6 0x55d in debugCommand /home/runner/work/valkey/valkey/src/debug.c:569\n"
        "    #7 0x552 in call /home/runner/work/valkey/valkey/src/server.c:3942\n"
    )

    def test_same_leak_across_compilers_same_identity(self) -> None:
        assert normalize_error_identity(self._CLANG) == normalize_error_identity(self._GCC)

    def test_identity_keeps_the_leaking_code_path(self) -> None:
        identity = normalize_error_identity(self._CLANG)
        assert "debugCommand" in identity
        assert "zmalloc" not in identity
        assert "sds" not in identity
        assert "malloc" not in identity

    def test_different_leak_sites_still_distinct(self) -> None:
        other = self._GCC.replace(
            "debugCommand /home/runner/work/valkey/valkey/src/debug.c:569",
            "clusterCommand /home/runner/work/valkey/valkey/src/cluster.c:120",
        )
        assert normalize_error_identity(self._GCC) != normalize_error_identity(other)


def _startup_blob(reason: str) -> str:
    """A start_server_error blob: exe, full config dump, then the reason.

    The config dump is long enough to push the ERROR: section past the
    identity extraction's significant-line window if it were not elided.
    """
    config = "\n".join(f"config-directive-{i} value-{i}" for i in range(40))
    return f"Can't start /path/to/valkey-server\nCONFIGURATION:\n{config}\nERROR:\n{reason}"


class TestStartupFailureIdentity:
    """Startup fingerprints must anchor on the failure reason after ERROR:,
    not on the config dump shared by every startup blob."""

    def test_different_reasons_different_identity(self) -> None:
        bad_directive = _startup_blob(
            "*** FATAL CONFIG FILE ERROR (Version 9.0.0) ***\n"
            "Bad directive or wrong number of arguments"
        )
        bind_failure = _startup_blob("Unable to bind unix socket: Permission denied")
        assert normalize_error_identity(bad_directive) != normalize_error_identity(bind_failure)

    def test_identity_names_the_reason(self) -> None:
        blob = _startup_blob("Unable to bind unix socket: Permission denied")
        assert "Unable to bind unix socket" in normalize_error_identity(blob)

    def test_same_reason_across_runs_same_identity(self) -> None:
        # Config contents (ports, dirs) drift between runs of the same cause.
        blob = _startup_blob("Unable to bind unix socket: Permission denied")
        rerun = blob.replace("value-3", "value-3-changed")
        assert normalize_error_identity(blob) == normalize_error_identity(rerun)

    def test_blob_without_error_section_falls_back_to_first_line(self) -> None:
        identity = normalize_error_identity("Can't start /path/to/valkey-server")
        assert identity == "Can't start valkey-server"

    def test_identity_ignores_the_runners_workspace_layout(self) -> None:
        """The exe path is the runner's layout, not the bug: the same failure is
        under /home/runner on Linux, /Users/runner on macOS, and /__w in a
        container job. Keeping it files one issue per platform."""
        reason = (
            "*** FATAL CONFIG FILE ERROR (Version 9.0.0) ***\n"
            "Bad directive or wrong number of arguments"
        )
        identities = {
            normalize_error_identity(
                _startup_blob(reason).replace("/path/to/valkey-server", exe)
            )
            for exe in (
                "/home/runner/work/valkey/valkey/src/valkey-server",
                "/Users/runner/work/valkey/valkey/src/valkey-server",
                "/__w/valkey/valkey/src/valkey-server",
            )
        }
        assert len(identities) == 1

    def test_identity_still_separates_different_executables(self) -> None:
        server = _startup_blob("Unable to bind unix socket: Permission denied")
        sentinel = server.replace("valkey-server", "valkey-sentinel")
        assert normalize_error_identity(server) != normalize_error_identity(sentinel)


def _leaks_blob(pid: int, root_site: str) -> str:
    """A macOS /usr/bin/leaks failure as the test proc reports it."""
    return (
        f"Check for memory leaks (pid {pid}) in tests/unit/other.tcl\n"
        f"Expected '*0 leaks*' to equal or match 'Process {pid}: 1 leak for 48 total leaked bytes.\n"
        f"leaks Report Version: 4.0\n"
        f"Process {pid}: 1 leak for 48 total leaked bytes.\n"
        f"    1 (48 bytes) ROOT LEAK: <{root_site} 0x600001d1c100> [48]'"
    )


class TestMacosLeaksIdentity:
    """macOS leaks blobs carry no stack frames; the ROOT LEAK site lines are
    the only allocation-site signal and must anchor the identity."""

    def test_same_root_across_runs_same_identity(self) -> None:
        assert normalize_error_identity(
            _leaks_blob(9443, "malloc in sdsnewlen")
        ) == normalize_error_identity(_leaks_blob(7121, "malloc in sdsnewlen"))

    def test_different_roots_same_file_different_identity(self) -> None:
        assert normalize_error_identity(
            _leaks_blob(9443, "malloc in sdsnewlen")
        ) != normalize_error_identity(_leaks_blob(9443, "malloc in clusterInit"))

    def test_identity_names_the_root_site(self) -> None:
        identity = normalize_error_identity(_leaks_blob(9443, "malloc in sdsnewlen"))
        assert "malloc in sdsnewlen" in identity

    def test_two_leaks_in_one_file_stay_separate_through_parse(self) -> None:
        """The runner names the leak check after the server it checked, not the
        leak, so every leak in a test file arrives under one name. The producer
        emits these nameless for that reason; parsing has to keep them apart on
        their root sites, or the second leak is dropped into the first's issue
        and only the first trace survives.
        """
        all_failures = {
            "test-macos-latest": {
                "valkey": [
                    {
                        "test_name": "",
                        "test_file": "tests/unit/other.tcl",
                        "type": "memory-leak",
                        "error": _leaks_blob(9443, "malloc in sdsnewlen"),
                    },
                    {
                        "test_name": "",
                        "test_file": "tests/unit/other.tcl",
                        "type": "memory-leak",
                        "error": _leaks_blob(7121, "malloc in clusterInit"),
                    },
                ]
            }
        }
        results = parse_and_deduplicate(all_failures, {})
        assert len(results) == 2
        sites = sorted(
            "clusterInit" if "clusterInit" in r.error else "sdsnewlen" for r in results
        )
        assert sites == ["clusterInit", "sdsnewlen"]
