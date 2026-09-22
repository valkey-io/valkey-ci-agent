"""Unit tests for timeout recovery from CI logs."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts.test_failure_detector.download import (
    JobInfo,
    get_job_info,
    normalize_job_name,
)
from scripts.test_failure_detector.main import _merge_timeout_recoveries
from scripts.test_failure_detector.parse_failures import (
    FailureType,
    JobReference,
    UniqueFailure,
)
from scripts.test_failure_detector.timeout_parser import (
    find_job_log,
    jobs_needing_log_scan,
    parse_timeouts_from_log,
)
from scripts.test_failure_detector.timeout_recovery import (
    RunLogs,
    enrich_log_only_errors,
    recover_timeouts,
)

# --- jobs_needing_log_scan ---

class TestJobsNeedingLogScan:
    def test_failed_job_with_empty_artifact_needs_scan(self) -> None:
        all_failures = {
            "test-ubuntu-jemalloc": {"valkey": [], "sentinel": []},
        }
        failed = {"test-ubuntu-jemalloc"}
        assert jobs_needing_log_scan(all_failures, failed) == {"test-ubuntu-jemalloc"}

    def test_failed_job_with_captured_timeout_skipped(self) -> None:
        """A job whose artifact already holds a timeout entry needs no scan."""
        all_failures = {
            "test-ubuntu-jemalloc": {
                "valkey": [{"test_name": "t", "test_file": "f.tcl",
                            "type": "timeout", "error": "Test timed out"}],
                "sentinel": [],
            },
        }
        failed = {"test-ubuntu-jemalloc"}
        assert jobs_needing_log_scan(all_failures, failed) == set()

    def test_failed_job_with_only_non_timeout_failures_still_scanned(self) -> None:
        """Captured assertions say nothing about timeouts (the runner excludes
        them from the artifact), so the job's log is still scanned: a timeout
        co-occurring with an assertion must not be lost."""
        all_failures = {
            "test-ubuntu-jemalloc": {
                "valkey": [{"test_name": "t", "test_file": "f.tcl", "error": "e"}],
                "sentinel": [],
            },
        }
        failed = {"test-ubuntu-jemalloc"}
        assert jobs_needing_log_scan(all_failures, failed) == {"test-ubuntu-jemalloc"}

    def test_failed_job_not_in_artifact_needs_scan(self) -> None:
        all_failures = {"test-ubuntu-jemalloc": {"valkey": [], "sentinel": []}}
        failed = {"test-valgrind-test"}
        assert jobs_needing_log_scan(all_failures, failed) == {"test-valgrind-test"}

    def test_successful_job_never_needs_scan(self) -> None:
        all_failures = {"test-ubuntu-jemalloc": {"valkey": []}}
        failed: set[str] = set()
        assert jobs_needing_log_scan(all_failures, failed) == set()

    def test_matrix_job_captured_timeout_matched_across_name_spellings(self) -> None:
        """The API names a matrix job "base (value)" but its artifact is keyed
        "base-value". Without normalizing, a sharded job's captured timeout is
        missed and the job gets rescanned and reported under both spellings."""
        all_failures = {
            "test-valgrind-test-unit": {
                "valkey": [{"test_name": "t", "test_file": "f.tcl",
                            "type": "timeout", "error": "Test timed out"}],
            },
        }
        failed = {"test-valgrind-test (unit)"}
        assert jobs_needing_log_scan(all_failures, failed) == set()

    def test_matrix_job_without_captured_timeout_still_scanned(self) -> None:
        """Normalizing must not suppress a scan the job needs."""
        all_failures = {
            "test-valgrind-test-unit": {
                "valkey": [{"test_name": "t", "test_file": "f.tcl", "error": "e"}],
            },
        }
        failed = {"test-valgrind-test (unit)"}
        assert jobs_needing_log_scan(all_failures, failed) == {"test-valgrind-test (unit)"}

    def test_multiple_failed_jobs_mixed(self) -> None:
        all_failures = {
            "job-a": {"valkey": [{"test_name": "t", "test_file": "f.tcl",
                                  "type": "timeout", "error": ""}]},
            "job-b": {"valkey": [], "sentinel": []},
        }
        failed = {"job-a", "job-b", "job-c"}
        result = jobs_needing_log_scan(all_failures, failed)
        assert result == {"job-b", "job-c"}


# --- parse_timeouts_from_log ---

# Simulated CI log output for a timeout event
SAMPLE_TIMEOUT_LOG = b"""\
2026-07-05T02:33:45.2195107Z ./runtest --valgrind --failures-output test-failures/valkey.json --verbose --clients 1 --timeout 2400
2026-07-05T04:13:45.0000000Z [ok]: Some passing test (1234 ms)
2026-07-05T04:13:50.0000000Z [TIMEOUT]: clients state report follows.
2026-07-05T04:13:50.0000000Z 5 => (IN PROGRESS) PSYNC2 test (pid 12345)
2026-07-05T04:13:50.0000000Z [TIMEOUT]: PSYNC2 test in tests/integration/replication-psync.tcl
2026-07-05T04:13:50.0000000Z 7 => (IN PROGRESS) Cluster slot migration (pid 12346)
2026-07-05T04:13:50.0000000Z [TIMEOUT]: Cluster slot migration in tests/unit/cluster.tcl
2026-07-05T04:13:55.0000000Z === Server log (pid 12345): ./tests/tmp/server.6510.2310/stdout ===
2026-07-05T04:14:00.0000000Z                    The End
2026-07-05T04:14:00.0000000Z !!! WARNING The following tests failed:
2026-07-05T04:14:00.0000000Z *** [TIMEOUT]: PSYNC2 test in tests/integration/replication-psync.tcl
2026-07-05T04:14:00.0000000Z *** [TIMEOUT]: Cluster slot migration in tests/unit/cluster.tcl
"""


class TestParseTimeoutsFromLog:
    def test_extracts_timeout_failures(self) -> None:
        results = parse_timeouts_from_log(
            SAMPLE_TIMEOUT_LOG, "test-valgrind-test", job_url="http://example.com/job/1",
        )
        assert len(results) == 2
        names = {f.test_name for f in results}
        assert names == {"PSYNC2 test", "Cluster slot migration"}

    def test_deduplicates_repeated_timeout_lines(self) -> None:
        results = parse_timeouts_from_log(SAMPLE_TIMEOUT_LOG, "job")
        # Each test appears twice in the sample (inline + summary), but only one UniqueFailure each
        assert len(results) == 2

    def test_populates_job_reference(self) -> None:
        results = parse_timeouts_from_log(
            SAMPLE_TIMEOUT_LOG, "test-valgrind-test", job_url="http://ci/job/42",
        )
        for f in results:
            assert len(f.jobs) == 1
            assert f.jobs[0].job == "test-valgrind-test"
            assert f.jobs[0].suite == "timeout"
            assert f.jobs[0].url == "http://ci/job/42"

    def test_sets_timeout_failure_type(self) -> None:
        results = parse_timeouts_from_log(SAMPLE_TIMEOUT_LOG, "job")
        for f in results:
            assert f.failure_type == FailureType.TIMEOUT

    def test_sets_timeout_error_message(self) -> None:
        results = parse_timeouts_from_log(SAMPLE_TIMEOUT_LOG, "job")
        for f in results:
            assert "timed out" in f.error.lower()

    def test_no_timeout_returns_empty(self) -> None:
        log = b"2026-07-05T02:33:45Z [ok]: Some test (100 ms)\nThe End\n"
        results = parse_timeouts_from_log(log, "job")
        assert results == []

    def test_handles_ansi_color_codes(self) -> None:
        log = (
            b"2026-07-05T04:13:50Z [\x1b[31mTIMEOUT\x1b[0m]: "
            b"My test in tests/unit/foo.tcl\n"
        )
        results = parse_timeouts_from_log(log, "job")
        assert len(results) == 1
        assert results[0].test_name == "My test"
        assert results[0].test_file == "tests/unit/foo.tcl"

    def test_empty_log_returns_empty(self) -> None:
        assert parse_timeouts_from_log(b"", "job") == []

    def test_binary_garbage_handled_gracefully(self) -> None:
        results = parse_timeouts_from_log(b"\x00\xff\xfe" * 100, "job")
        assert results == []

    def test_volatile_pid_name_demoted(self) -> None:
        """Log-recovered timeout with a volatile PID name gets demoted to
        nameless so it produces a stable fingerprint."""
        log = b"[TIMEOUT]: pid:92663 in tests/integration/replication.tcl\n"
        results = parse_timeouts_from_log(log, "job")
        assert len(results) == 1
        assert results[0].test_name == ""
        assert results[0].test_file == "tests/integration/replication.tcl"

    def test_volatile_hang_name_demoted(self) -> None:
        log = b"[TIMEOUT]: hang in tests/unit/cluster.tcl\n"
        results = parse_timeouts_from_log(log, "job")
        assert len(results) == 1
        assert results[0].test_name == ""

    def test_real_name_not_demoted(self) -> None:
        log = b"[TIMEOUT]: PSYNC2 test in tests/integration/replication-psync.tcl\n"
        results = parse_timeouts_from_log(log, "job")
        assert len(results) == 1
        assert results[0].test_name == "PSYNC2 test"

    def test_clients_report_captured_from_coloured_header(self) -> None:
        """The clients state report is the only diagnostic a timeout carries, so
        it has to survive the runner colouring the header. colorstr emits the
        escape whenever TERM matches *xterm*, as it does on the CI runners.
        """
        log = (
            b"2026-07-05T04:13:50Z [\x1b[0;31;49mTIMEOUT\x1b[0m]: "
            b"clients state report follows.\n"
            b"2026-07-05T04:13:50Z sock56195920b920 => (IN PROGRESS) "
            b"PSYNC2 test - hang\n"
            b"2026-07-05T04:13:50Z sock56195920b921 => (done)\n"
            b"2026-07-05T04:13:51Z [\x1b[0;31;49mTIMEOUT\x1b[0m]: "
            b"PSYNC2 test in tests/integration/replication-psync.tcl\n"
        )
        results = parse_timeouts_from_log(log, "job")
        assert len(results) == 1
        assert "(IN PROGRESS) PSYNC2 test - hang" in results[0].error
        # Cleaning happens before the match, so no escape reaches the issue.
        assert "\x1b" not in results[0].error


# --- find_job_log ---

class TestFindJobLog:
    def test_matches_numbered_prefix(self) -> None:
        logs = {
            "3_test-valgrind-test.txt": b"log content",
            "test-valgrind-test/system.txt": b"system",
        }
        assert find_job_log(logs, "test-valgrind-test") == b"log content"

    def test_matches_with_parentheses(self) -> None:
        logs = {
            "14_test-sanitizer-address (gcc).txt": b"gcc log",
            "test-sanitizer-address (gcc)/system.txt": b"system",
        }
        assert find_job_log(logs, "test-sanitizer-address (gcc)") == b"gcc log"

    def test_skips_subdirectory_files(self) -> None:
        logs = {
            "test-ubuntu-jemalloc/system.txt": b"system info",
            "5_test-ubuntu-jemalloc.txt": b"real log",
        }
        assert find_job_log(logs, "test-ubuntu-jemalloc") == b"real log"

    def test_returns_none_when_not_found(self) -> None:
        logs = {"3_other-job.txt": b"other"}
        assert find_job_log(logs, "test-missing-job") is None

    def test_case_insensitive_fallback(self) -> None:
        logs = {"3_Test-Ubuntu-Jemalloc.txt": b"content"}
        assert find_job_log(logs, "test-ubuntu-jemalloc") == b"content"


# --- _merge_timeout_recoveries ---


class TestMergeTimeoutRecoveries:
    def test_adds_new_timeout_to_list(self) -> None:
        existing = [
            UniqueFailure(
                test_name="assertion test",
                test_file="tests/unit/foo.tcl",
                failure_type=FailureType.ASSERTION,
                error="oops",
                jobs=[JobReference(job="job-a", suite="valkey", url="u")],
            )
        ]
        recovered = [
            UniqueFailure(
                test_name="PSYNC2 test",
                test_file="tests/integration/replication-psync.tcl",
                failure_type=FailureType.TIMEOUT,
                error="Test timed out",
                jobs=[JobReference(job="job-b", suite="timeout", url="u2")],
            )
        ]
        result = _merge_timeout_recoveries(existing, recovered)
        assert len(result) == 2
        timeout = [f for f in result if f.failure_type == FailureType.TIMEOUT]
        assert len(timeout) == 1
        assert timeout[0].test_name == "PSYNC2 test"

    def test_folds_job_into_existing_timeout(self) -> None:
        """If the same timeout already came from the artifact, fold the new
        job reference into it instead of creating a duplicate."""
        existing = [
            UniqueFailure(
                test_name="PSYNC2 test",
                test_file="tests/integration/replication-psync.tcl",
                failure_type=FailureType.TIMEOUT,
                error="Test timed out",
                jobs=[JobReference(job="job-a", suite="valkey", url="u1")],
            )
        ]
        recovered = [
            UniqueFailure(
                test_name="PSYNC2 test",
                test_file="tests/integration/replication-psync.tcl",
                failure_type=FailureType.TIMEOUT,
                error="Test timed out (no progress for the configured timeout period)",
                jobs=[JobReference(job="job-b", suite="timeout", url="u2")],
            )
        ]
        result = _merge_timeout_recoveries(existing, recovered)
        assert len(result) == 1
        assert len(result[0].jobs) == 2
        job_names = {j.job for j in result[0].jobs}
        assert job_names == {"job-a", "job-b"}

    def test_does_not_duplicate_same_job(self) -> None:
        """If the recovered job is already recorded, don't add it again."""
        existing = [
            UniqueFailure(
                test_name="PSYNC2 test",
                test_file="tests/integration/replication-psync.tcl",
                failure_type=FailureType.TIMEOUT,
                error="Test timed out",
                jobs=[JobReference(job="job-a", suite="valkey", url="u1")],
            )
        ]
        recovered = [
            UniqueFailure(
                test_name="PSYNC2 test",
                test_file="tests/integration/replication-psync.tcl",
                failure_type=FailureType.TIMEOUT,
                error="Test timed out",
                jobs=[JobReference(job="job-a", suite="timeout", url="u1")],
            )
        ]
        result = _merge_timeout_recoveries(existing, recovered)
        assert len(result) == 1
        assert len(result[0].jobs) == 1

    def test_empty_recoveries_returns_original(self) -> None:
        existing = [
            UniqueFailure(
                test_name="test", test_file="f.tcl",
                failure_type=FailureType.ASSERTION, error="e",
            )
        ]
        result = _merge_timeout_recoveries(existing, [])
        assert result is existing


class TestRunLogs:
    def test_downloads_once_and_reuses(self) -> None:
        client = MagicMock()
        client.download_run_logs.return_value = {"1_job.txt": b"log"}
        logs = RunLogs(client, "owner/repo", 123)

        first = logs.get()
        second = logs.get()

        assert first == {"1_job.txt": b"log"}
        assert second is first
        client.download_run_logs.assert_called_once_with("owner/repo", 123)

    def test_download_failure_is_cached_as_empty(self) -> None:
        """A failed fetch is not retried by a second consumer."""
        client = MagicMock()
        client.download_run_logs.side_effect = RuntimeError("boom")
        logs = RunLogs(client, "owner/repo", 123)

        assert logs.get() == {}
        assert logs.get() == {}
        client.download_run_logs.assert_called_once()


class TestSharedRunLogsAcrossConsumers:
    def test_recover_and_enrich_share_one_download(self) -> None:
        """recover_timeouts and enrich_log_only_errors read the same RunLogs, so
        the run's log zip is downloaded once, not once per consumer."""
        log = (
            b"[TIMEOUT]: clients state report follows.\n"
            b"*** [TIMEOUT]: SlowTest in tests/unit/slow.tcl\n"
        )
        client = MagicMock()
        client.download_run_logs.return_value = {"1_test-ubuntu.txt": log}
        job_info = JobInfo(
            urls={"test-ubuntu": "https://x/job/1"},
            step_urls={},
            failed={"test-ubuntu"},
        )
        run_logs = RunLogs(client, "owner/repo", 123)

        # A failed job with no captured timeout entry, so a scan is needed.
        all_failures = {"test-ubuntu": {"valkey": []}}
        recovered = recover_timeouts(all_failures, job_info, run_logs)

        # A recovered timeout carries its clients-state report, which excludes it
        # from enrichment. Without a candidate that still holds the bare
        # placeholder, enrichment returns before consulting the logs at all and
        # this test would prove nothing about the second consumer.
        gtest = UniqueFailure(
            test_name="ListpackTest.Insert",
            test_file="src/unit/valkey-unit-gtests",
            failure_type=FailureType.UNITTEST,
            error="gtest FAIL",
            jobs=[JobReference(job="test-ubuntu", suite="unittest", url="https://x/job/1")],
        )
        consulted: list[int] = []
        original_get = run_logs.get
        run_logs.get = lambda: (consulted.append(1), original_get())[1]

        enrich_log_only_errors([*recovered, gtest], job_info, run_logs)

        assert recovered  # the [TIMEOUT] line was recovered
        assert consulted, "enrichment never consulted the shared RunLogs"
        client.download_run_logs.assert_called_once()

class TestRecoverAndEnrichTimeouts:
    """recover_timeouts and enrich_log_only_errors over the run's shared logs."""

    def _job_info(self) -> JobInfo:
        return JobInfo(
            urls={"test-ubuntu": "https://x/job/1"},
            step_urls={},
            failed={"test-ubuntu"},
        )

    def test_a_timeout_only_in_the_log_is_recovered(self) -> None:
        """The watchdog can kill the run before the artifact is written, so a
        timed-out job can reach the sweep with no timeout entry."""
        log = (
            b"[TIMEOUT]: clients state report follows.\n"
            b"sock1 => (IN PROGRESS) SlowTest\n"
            b"*** [TIMEOUT]: SlowTest in tests/unit/slow.tcl\n"
        )
        client = MagicMock()
        client.download_run_logs.return_value = {"1_test-ubuntu.txt": log}
        run_logs = RunLogs(client, "owner/repo", 123)

        recovered = recover_timeouts({"test-ubuntu": {"valkey": []}},
                                     self._job_info(), run_logs)

        assert [f.test_name for f in recovered] == ["SlowTest"]
        assert recovered[0].failure_type == FailureType.TIMEOUT
        assert recovered[0].test_file == "tests/unit/slow.tcl"

    def test_a_captured_timeout_placeholder_gains_its_report(self) -> None:
        """A timeout the artifact did capture carries only "Test timed out",
        so the clients-state report is read back from the log."""
        log = (
            b"[TIMEOUT]: clients state report follows.\n"
            b"sock1 => (IN PROGRESS) SlowTest\n"
        )
        client = MagicMock()
        client.download_run_logs.return_value = {"1_test-ubuntu.txt": log}
        run_logs = RunLogs(client, "owner/repo", 123)
        failure = UniqueFailure(
            test_name="SlowTest",
            test_file="tests/unit/slow.tcl",
            failure_type=FailureType.TIMEOUT,
            error="Test timed out",
            jobs=[JobReference(job="test-ubuntu", suite="valkey",
                               url="https://x/job/1")],
        )

        enrich_log_only_errors([failure], self._job_info(), run_logs)

        assert "IN PROGRESS) SlowTest" in failure.error
        assert failure.error.startswith("Test timed out")

    def test_an_extracted_gtest_log_is_not_replaced(self) -> None:
        """The extraction action attaches the test's own log when it finds one.
        The run log interleaves every worker's output, so enriching over it
        would trade a full assertion for the progress lines around it.
        """
        extracted = (
            "[6/9] DictTest.Resize (2 ms)\n"
            "src/unit/test_dict.cpp:88: Failure\n"
            "Expected equality of these values:\n"
            "[6/9] DictTest.Resize returned with exit code 1 (2 ms)"
        )
        client = MagicMock()
        client.download_run_logs.return_value = {
            "1_test-ubuntu.txt": (
                b"[6/9] DictTest.Resize (2 ms)\n"
                b"[6/9] DictTest.Resize returned with exit code 1 (2 ms)\n"
            )
        }
        failure = UniqueFailure(
            test_name="DictTest.Resize",
            test_file="src/unit/valkey-unit-gtests",
            failure_type=FailureType.UNITTEST,
            error=extracted,
            jobs=[JobReference(job="test-ubuntu", suite="unittest", url="u")],
        )

        enrich_log_only_errors([failure], self._job_info(), RunLogs(client, "owner/repo", 1))

        assert failure.error == extracted

    def test_a_gtest_verdict_placeholder_gains_its_output(self) -> None:
        """With no per-test log the action falls back to a bare verdict, which
        is the case the run log has to fill in."""
        client = MagicMock()
        client.download_run_logs.return_value = {
            "1_test-ubuntu.txt": (
                b"[6/9] DictTest.Resize (2 ms)\n"
                b"src/unit/test_dict.cpp:88: Failure\n"
                b"[6/9] DictTest.Resize returned with exit code 1 (2 ms)\n"
            )
        }
        failure = UniqueFailure(
            test_name="DictTest.Resize",
            test_file="src/unit/valkey-unit-gtests",
            failure_type=FailureType.UNITTEST,
            error="gtest FAIL",
            jobs=[JobReference(job="test-ubuntu", suite="unittest", url="u")],
        )

        enrich_log_only_errors([failure], self._job_info(), RunLogs(client, "owner/repo", 1))

        assert "test_dict.cpp:88" in failure.error

    def test_the_run_log_zip_is_downloaded_once(self) -> None:
        """Both consumers read one RunLogs, so a run costs one download."""
        log = b"*** [TIMEOUT]: SlowTest in tests/unit/slow.tcl\n"
        client = MagicMock()
        client.download_run_logs.return_value = {"1_test-ubuntu.txt": log}
        run_logs = RunLogs(client, "owner/repo", 123)
        job_info = self._job_info()

        recovered = recover_timeouts({"test-ubuntu": {"valkey": []}},
                                     job_info, run_logs)
        enrich_log_only_errors(recovered, job_info, run_logs)

        client.download_run_logs.assert_called_once()


class TestTimeoutMarkerIsAnchored:
    """The marker is read only from the start of the runner's own line."""

    def _parse(self, text: str):
        return parse_timeouts_from_log(text.encode(), "job", "https://x/job")

    def test_the_runner_line_shapes_are_recovered(self) -> None:
        for line in (
            "[TIMEOUT]: SlowTest in tests/unit/slow.tcl",
            "*** [TIMEOUT]: SlowTest in tests/unit/slow.tcl",
            "2026-07-29T23:29:31.7923540Z [TIMEOUT]: SlowTest in tests/unit/slow.tcl",
        ):
            assert [f.test_name for f in self._parse(line)] == ["SlowTest"], line

    def test_quoted_marker_text_invents_no_timeout(self) -> None:
        """A test that asserts on or echoes text holding the marker would
        otherwise be filed as a timeout the runner never reported."""
        log = (
            'assert_equal "x [TIMEOUT]: FakeTest in tests/unit/fake.tcl" $res\n'
            "some output then [TIMEOUT]: Other in tests/unit/other.tcl\n"
        )
        assert self._parse(log) == []


def _job_info_for(api_job_name: str) -> JobInfo:
    """JobInfo as get_job_info derives it for one failed job."""
    job = MagicMock()
    job.name = api_job_name
    job.html_url = "u"
    job.conclusion = "failure"
    job.steps = []
    run = MagicMock()
    run.jobs.return_value = [job]
    repo = MagicMock()
    repo.get_workflow_run.return_value = run
    gh = MagicMock()
    gh.get_repo.return_value = repo
    with patch(
        "scripts.test_failure_detector.download.retry_github_call",
        side_effect=lambda op, **kw: op(),
    ):
        return get_job_info(gh, "owner/repo", 1)


class TestMatrixJobNameHandoff:
    """The artifact and the API spell a matrix job differently, and the two
    sides of recovery join on opposite spellings."""

    def test_a_log_is_found_by_either_spelling(self) -> None:
        api = "test-sanitizer-address (gcc)"
        logs = {f"1_{api}.txt": b"[TIMEOUT]: T in tests/unit/s.tcl\n"}
        assert find_job_log(logs, api) is not None
        assert find_job_log(logs, normalize_job_name(api)) is not None

    def test_a_matrix_job_placeholder_is_enriched(self) -> None:
        """Enrichment intersects the failure's job names, which carry the
        artifact's spelling, with the failed-job set from the API."""
        api = "test-sanitizer-address (gcc)"
        artifact_name = normalize_job_name(api)
        client = MagicMock()
        client.download_run_logs.return_value = {
            f"1_{api}.txt": (
                b"[TIMEOUT]: clients state report follows.\n"
                b"sock1 => (IN PROGRESS) SlowTest\n"
            )
        }
        failure = UniqueFailure(
            test_name="SlowTest", test_file="tests/unit/slow.tcl",
            failure_type=FailureType.TIMEOUT, error="Test timed out",
            jobs=[JobReference(job=artifact_name, suite="valkey", url="u")],
        )
        # Built the way get_job_info builds it, so the test fails if that
        # function stops recording both spellings of a failed matrix job.
        job_info = _job_info_for(api)

        enrich_log_only_errors(
            [failure], job_info, RunLogs(client, "owner/repo", 1),
        )

        assert "IN PROGRESS) SlowTest" in failure.error
