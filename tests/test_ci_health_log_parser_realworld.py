"""Real-world regression tests for the Valkey CI log parser.

Each test uses a log snippet extracted from actual valkey-io/valkey CI job
logs.  As new failure patterns are discovered, add a new test here with the
raw snippet so the parser never regresses.

Naming convention: test_<pattern>_<source_description>
"""

from __future__ import annotations

import re

from scripts.ci_health.log_parser import parse_job_log

# ---------------------------------------------------------------------------
# 1. Tcl [err] with GitHub Actions timestamps
# Source: job 69272507658, run 23722657826 (unstable daily, 2026-03-30)
# ---------------------------------------------------------------------------

_TCL_ERR_GHA_TIMESTAMP = """\
2026-03-31T00:28:17.2110000Z 38348:M 31 Mar 2026 00:28:17.211 * Discarding previously cached primary state.
2026-03-31T00:28:17.2110001Z 38348:M 31 Mar 2026 00:28:17.211 * Setting secondary replication ID to 6ac59a34.
2026-03-31T00:28:17.2110002Z [err]: The best replica can initiate an election immediately in an automatic failover in tests/unit/cluster/faster-failover.tcl
2026-03-31T00:28:17.2110003Z log message of '"*Successful partial resynchronization with primary*"' not found in ./tests/tmp/server.10822.103/stdout after line: 0 till line: 155
2026-03-31T00:28:17.2110004Z 
2026-03-31T00:28:17.2110005Z ===== Start of server log (pid 38545) =====
"""


class TestTclErrWithGHATimestamp:
    """[err] lines prefixed with GitHub Actions timestamps."""

    def test_faster_failover(self) -> None:
        failures = parse_job_log(_TCL_ERR_GHA_TIMESTAMP)
        assert len(failures) >= 1
        f = failures[0]
        assert "The best replica can initiate an election immediately" in f.test_name
        assert "faster-failover.tcl" in f.test_name
        assert "log message" in f.error_summary

    def test_timestamp_stripping_does_not_lose_content(self) -> None:
        """The error detail after [err] should still be captured."""
        failures = parse_job_log(_TCL_ERR_GHA_TIMESTAMP)
        assert any(
            "Successful partial resynchronization" in f.error_summary
            or "log message" in f.error_summary
            for f in failures
        )


# ---------------------------------------------------------------------------
# 2. Tcl [err] with GHA timestamps — replication test
# Source: job from run 23803833154 (fix-rdma-io-threads, 2026-03-31)
# ---------------------------------------------------------------------------

_TCL_ERR_REPLICATION = """\
2026-03-31T15:02:19.5643261Z ===== End of server stderr log (pid 54604) =====
2026-03-31T15:02:19.5643608Z 
2026-03-31T15:02:19.5644045Z [err]: diskless no replicas drop during rdb pipe in tests/integration/replication.tcl
2026-03-31T15:02:19.5644773Z rdb child didn't terminate
2026-03-31T15:02:19.7217411Z === () Starting server on 127.0.0.1:21815 ok
"""


class TestTclErrReplication:
    """[err] from a replication test with GHA timestamps."""

    def test_diskless_replication(self) -> None:
        failures = parse_job_log(_TCL_ERR_REPLICATION)
        assert len(failures) == 1
        assert "diskless no replicas drop during rdb pipe" in failures[0].test_name
        assert "replication.tcl" in failures[0].test_name
        assert "rdb child" in failures[0].error_summary


# ---------------------------------------------------------------------------
# 3. Tcl [exception] — maxmemory slave buffer crash
# Source: job 69432215377, run 23820775003 (flaky-failover, 2026-03-31)
# The test name is in a procedure call: test_slave_buffers {name} args
# ---------------------------------------------------------------------------

_TCL_EXCEPTION_MAXMEMORY = """\
===== End of server stderr log (pid 14153) =====

[exception]: Executing test client: I/O error reading reply.
I/O error reading reply
    while executing
"$rd_master setrange key:0 0 [string repeat A $payload_len]"
    ("uplevel" body line 53)
    invoked from within
"uplevel 1 $code"
    (procedure "test" line 63)
    invoked from within
"test "$test_name" {
            set slave [srv 0 client]
            set slave_host [srv 0 host]
            set slave_port [srv 0 port]
            s..."
    ("uplevel" body line 3)
    invoked from within
"uplevel 1 $code "
    (procedure "start_server" line 2)
    invoked from within
"start_server {} {
        set slave_pid [s process_id]
        test "$test_name" {
            set slave [srv 0 client]
            set slave_host [sr..."
    ("uplevel" body line 2)
    invoked from within
"uplevel 1 $code "
    (procedure "start_server" line 2)
    invoked from within
"start_server {tags {"maxmemory external:skip"}} {
        start_server {} {
        set slave_pid [s process_id]
        test "$test_name" {
         ..."
    (procedure "test_slave_buffers" line 2)
    invoked from within
"test_slave_buffers {slave buffer are counted correctly} 1000000 10 0 1"
    (file "tests/unit/maxmemory.tcl" line 392)
    invoked from within
"source $path"
    (procedure "execute_test_file" line 6)
"""


class TestTclExceptionMaxmemory:
    """[exception] with stack trace containing test name in procedure call."""

    def test_extracts_test_name_from_proc_call(self) -> None:
        failures = parse_job_log(_TCL_EXCEPTION_MAXMEMORY)
        assert len(failures) == 1
        f = failures[0]
        assert "slave buffer are counted correctly" in f.test_name
        assert "maxmemory.tcl" in f.test_name

    def test_error_summary_has_io_error(self) -> None:
        failures = parse_job_log(_TCL_EXCEPTION_MAXMEMORY)
        assert "I/O error reading reply" in failures[0].error_summary


# ---------------------------------------------------------------------------
# 4. Tcl [exception] — MOVED error in cluster test
# Source: job from run 23820775003 (cluster failover test)
# The test name has an unresolved $type variable.
# ---------------------------------------------------------------------------

_TCL_EXCEPTION_MOVED = """\
===== End of server stderr log (pid 31489) =====

[exception]: Executing test client: MOVED 1 127.0.0.1:24196.
MOVED 1 127.0.0.1:24196
    while executing
"wait_for_condition 1000 50 {
            [R 4 get key_991803] == 1024 &&
            [R 7 get key_991803] == 1024
        } else {
            puts "R..."
    ("uplevel" body line 63)
    invoked from within
"uplevel 1 $code"
    (procedure "test" line 63)
    invoked from within
"test "New non-empty replica reports zero repl offset and rank, and fails to win election - $type" {
        # Write some data to primary 0, slot 1, ma..."
    (procedure "test_nonempty_replica" line 2)
    invoked from within
"test_nonempty_replica "sigstop""
    ("uplevel" body line 2)
    invoked from within
"uplevel 1 $code"
    (procedure "cluster_setup" line 53)
"""


class TestTclExceptionMoved:
    """[exception] with MOVED error — test name has $type variable."""

    def test_extracts_test_name_stripping_type_variable(self) -> None:
        failures = parse_job_log(_TCL_EXCEPTION_MOVED)
        assert len(failures) == 1
        f = failures[0]
        # Should have the test name with $type stripped
        assert "New non-empty replica reports zero repl offset" in f.test_name
        # $type should be stripped from the display name
        assert "$type" not in f.test_name

    def test_error_summary_has_moved(self) -> None:
        failures = parse_job_log(_TCL_EXCEPTION_MOVED)
        assert "MOVED" in failures[0].error_summary


# ---------------------------------------------------------------------------
# 5. Tcl [exception] — memefficiency with foreach parameterization
# Source: job from run 23825749975 (unstable daily, 2026-04-01)
# The test name has $size_range — a foreach loop variable.
# ---------------------------------------------------------------------------

_TCL_EXCEPTION_MEMEFFICIENCY = """\
===== End of server stderr log (pid 13697) =====

[exception]: Executing test client: error writing "sock556845bd7480": connection timed out.
error writing "sock556845bd7480": connection timed out
    while executing
"$rd set $key $val"
    (procedure "test_memory_efficiency" line 9)
    invoked from within
"test_memory_efficiency $size_range"
    ("uplevel" body line 2)
    invoked from within
"uplevel 1 $code"
    (procedure "test" line 63)
    invoked from within
"test "Memory efficiency with values in range $size_range" {
            set efficiency [test_memory_efficiency $size_range]
            assert {$effic..."
    ("foreach" body line 2)
    invoked from within
"foreach {size_range expected_min_efficiency} {
        32    0.15
        64    0.25
        128   0.35
        1024  0.75
        16384 0.82
    } {
..."
    ("uplevel" body line 2)
    invoked from within
"uplevel 1 $code "
    (procedure "start_server" line 2)
    invoked from within
"start_server {tags {"memefficiency external:skip"}} {
    foreach {size_range expected_min_efficiency} {
        32    0.15
        64    0.25
       ..."
    (file "tests/unit/memefficiency.tcl" line 24)
    invoked from within
"source $path"
    (procedure "execute_test_file" line 6)
"""


class TestTclExceptionMemefficiency:
    """[exception] with foreach-parameterized test name."""

    def test_extracts_parameterized_test_name(self) -> None:
        failures = parse_job_log(_TCL_EXCEPTION_MEMEFFICIENCY)
        assert len(failures) == 1
        f = failures[0]
        # Should use the test "..." name since proc call is foreach
        assert "Memory efficiency" in f.test_name
        assert "memefficiency.tcl" in f.test_name

    def test_error_summary_has_connection_timeout(self) -> None:
        failures = parse_job_log(_TCL_EXCEPTION_MEMEFFICIENCY)
        assert "connection timed out" in failures[0].error_summary


# ---------------------------------------------------------------------------
# 6. GitHub Actions ##[error] fallback — process exit with no other pattern
# Source: synthetic based on real pattern (job exits with code 1, no Tcl markers)
# ---------------------------------------------------------------------------

_GHA_ERROR_ONLY = """\
2026-04-01T01:05:06.5070748Z HGETALL _hash
2026-04-01T01:05:06.5070912Z HEXISTS _hash -519618290
2026-04-01T01:05:06.5071107Z HDEL _hash -301167168329
2026-04-01T01:05:06.5089664Z ##[error]Process completed with exit code 1.
2026-04-01T01:05:06.5223518Z Post job cleanup.
"""


class TestGHAErrorFallback:
    """##[error] fallback when no Tcl/GTest/sentinel patterns match.

    Target behavior (issue #1): a generic runner exit-code message is NOT a
    test. A failed job whose log yields no attributable test collapses into a
    single stable per-job "unattributed failure" bucket, keyed by job name so
    it aggregates across days instead of splitting by exit code. The failure
    is never dropped — the run's failed-job count stays honest and the raw
    message is preserved as error detail for click-through.
    """

    def test_generic_exit_code_becomes_per_job_bucket(self) -> None:
        failures = parse_job_log(_GHA_ERROR_ONLY, job_name="test-valgrind-test")
        assert len(failures) == 1
        f = failures[0]
        # The runner exit code is not a test identity...
        assert "exit code" not in f.test_name
        # ...it is bucketed under the job that failed.
        assert f.test_name == "test-valgrind-test: unattributed failure"
        # The raw message is preserved as error detail.
        assert "exit code 1" in f.error_summary

    def test_unattributed_bucket_without_job_name(self) -> None:
        """Without a job name, a stable generic bucket is still used."""
        failures = parse_job_log(_GHA_ERROR_ONLY)
        assert len(failures) == 1
        assert failures[0].test_name == "unattributed failure"
        assert "exit code" not in failures[0].test_name

    def test_exit_codes_1_and_2_share_one_bucket(self) -> None:
        """exit code 1 and exit code 2 must not split into two heatmap rows."""
        log2 = _GHA_ERROR_ONLY.replace("exit code 1", "exit code 2")
        a = parse_job_log(_GHA_ERROR_ONLY, job_name="test-valgrind-test")
        b = parse_job_log(log2, job_name="test-valgrind-test")
        assert a[0].test_name == b[0].test_name

    def test_not_used_when_other_patterns_match(self) -> None:
        """##[error] should NOT add a bucket when a real test already matched."""
        log = (
            "2026-04-01T00:00:00.0000000Z [err]: Some test in tests/unit/foo.tcl\n"
            "2026-04-01T00:00:00.0000001Z some error detail\n"
            "2026-04-01T00:00:01.0000000Z ##[error]Process completed with exit code 1.\n"
        )
        failures = parse_job_log(log, job_name="some-job")
        # Should only have the [err] failure, not an unattributed bucket.
        assert len(failures) == 1
        assert "Some test" in failures[0].test_name
        assert "unattributed" not in failures[0].test_name


# ---------------------------------------------------------------------------
# 7. Mixed: [err] + [exception] in same log should not duplicate
# ---------------------------------------------------------------------------

_MIXED_ERR_AND_EXCEPTION = """\
2026-04-01T00:00:00.0000000Z [err]: Test A in tests/unit/a.tcl
2026-04-01T00:00:00.0000001Z assertion failed
2026-04-01T00:00:01.0000000Z [exception]: Executing test client: timeout.
2026-04-01T00:00:01.0000001Z timeout
2026-04-01T00:00:01.0000002Z     (procedure "test" line 63)
2026-04-01T00:00:01.0000003Z     invoked from within
2026-04-01T00:00:01.0000004Z "test "Test B" {"
2026-04-01T00:00:01.0000005Z     (file "tests/unit/b.tcl" line 10)
"""


class TestMixedPatterns:
    """Log with both [err] and [exception] should capture both."""

    def test_captures_both_err_and_exception(self) -> None:
        failures = parse_job_log(_MIXED_ERR_AND_EXCEPTION)
        names = {f.test_name for f in failures}
        assert any("Test A" in n for n in names)
        assert any("Test B" in n for n in names)
        assert len(failures) == 2


# ---------------------------------------------------------------------------
# 8. Logs with only passing tests — should return empty
# ---------------------------------------------------------------------------

_ALL_PASSING_WITH_TIMESTAMPS = """\
2026-04-01T00:00:00.0000000Z [ok]: Test 1 in tests/unit/basic.tcl (10 ms)
2026-04-01T00:00:00.0000001Z [ok]: Test 2 in tests/unit/basic.tcl (5 ms)
2026-04-01T00:00:00.0000002Z Passed 2, Failed 0
"""


class TestAllPassingWithTimestamps:
    """Logs with only [ok] markers and GHA timestamps should return empty."""

    def test_no_failures(self) -> None:
        assert parse_job_log(_ALL_PASSING_WITH_TIMESTAMPS) == []


# ---------------------------------------------------------------------------
# 9. Summary *** [err] with GHA timestamps
# ---------------------------------------------------------------------------

_SUMMARY_ERR_WITH_TIMESTAMPS = """\
2026-04-01T00:00:00.0000000Z 
2026-04-01T00:00:00.0000001Z                    The End
2026-04-01T00:00:00.0000002Z 
2026-04-01T00:00:00.0000003Z !!! WARNING The following tests failed:
2026-04-01T00:00:00.0000004Z 
2026-04-01T00:00:00.0000005Z *** [err]: Primaries will not time out in tests/unit/cluster/failover2.tcl
2026-04-01T00:00:00.0000006Z expected message found in log file: *Failover attempt expired*
"""


class TestSummaryErrWithTimestamps:
    """*** [err] summary lines with GHA timestamps."""

    def test_summary_err(self) -> None:
        failures = parse_job_log(_SUMMARY_ERR_WITH_TIMESTAMPS)
        assert len(failures) == 1
        assert "Primaries will not time out" in failures[0].test_name
        assert "failover2.tcl" in failures[0].test_name


# ---------------------------------------------------------------------------
# 10. Noise regression fixtures (issue #1)
# These strings appeared as standalone "tests" on the live dashboard heatmap
# where the parser mis-attributed non-test tokens as distinct failures.
# Minimal reproductions distilled from those live rows.
#
# Policy (see design discussion):
#   * generic runner banners are never test names
#   * a real failure signal that can't be attributed becomes ONE per-job
#     "unattributed failure" bucket (never dropped from the counts)
#   * volatile tokens (pids/ports) are normalized so incidents aggregate
# ---------------------------------------------------------------------------

# 10a. Bare [TIMEOUT] "clients state report follows." with an unresolvable
#      follow-up line (no (IN PROGRESS)/(SPAWNED SERVER) test to attribute to).
_TIMEOUT_CLIENTS_STATE_UNRESOLVABLE = """\
2026-07-02T03:11:00.0000000Z [TIMEOUT]: clients state report follows.
2026-07-02T03:11:00.0000001Z sock55f0a1b2c3d0 => (SLEEPING) 12
2026-07-02T03:11:00.0000002Z Killing still running Valkey server 12345
"""


class TestTimeoutClientsStateNoise:
    """'clients state report follows.' must never become a test row."""

    def test_generic_banner_not_a_test_name(self) -> None:
        failures = parse_job_log(
            _TIMEOUT_CLIENTS_STATE_UNRESOLVABLE, job_name="test-sanitizer-address"
        )
        assert all(
            "clients state report follows" not in f.test_name.lower() for f in failures
        )

    def test_timeout_still_recorded_as_unattributed(self) -> None:
        # A TIMEOUT is a real failure signal, so it is not lost — when the
        # real test can't be resolved it becomes the per-job bucket.
        failures = parse_job_log(
            _TIMEOUT_CLIENTS_STATE_UNRESOLVABLE, job_name="test-sanitizer-address"
        )
        assert len(failures) == 1
        assert failures[0].test_name == "test-sanitizer-address: unattributed failure"


# 10b. A bare pid token must never surface as a standalone "test".
_TIMEOUT_BARE_PID = """\
2026-07-01T04:00:00.0000000Z [TIMEOUT]: pid:51740 - tests/unit/type/stream.tcl
2026-07-01T04:00:00.0000001Z Waiting for background AOF rewrite to finish
"""


class TestBarePidNoise:
    """'pid:51740' style tokens are not test identities."""

    def test_bare_pid_not_a_standalone_test_name(self) -> None:
        failures = parse_job_log(_TIMEOUT_BARE_PID, job_name="test-external-standalone")
        for f in failures:
            assert not re.fullmatch(r"pid:\d+", f.test_name)
            assert not f.test_name.startswith("pid:")


# 10c. [exception] with no resolvable test/file, carrying a volatile pid.
#      Must be normalized so repeated incidents aggregate into one row
#      instead of a new row every night.
_EXCEPTION_VOLATILE_PID = """\
===== End of server stderr log (pid 15868) =====

[exception]: Executing test client: assertion:Process 15868 (valkey-server) generated signal 6.
assertion:Process 15868 (valkey-server) generated signal 6
    while executing
"debug reload"
    ("uplevel" body line 12)
    invoked from within
"uplevel 1 $code"
"""


class TestExceptionVolatilePid:
    """Unresolvable [exception] names must scrub volatile pids to aggregate."""

    def test_pid_normalized_out_of_exception_name(self) -> None:
        failures = parse_job_log(_EXCEPTION_VOLATILE_PID, job_name="test-macos")
        assert len(failures) == 1
        name = failures[0].test_name
        assert "15868" not in name  # raw pid must be gone
        assert "<pid>" in name  # normalized placeholder present

    def test_different_pids_aggregate_to_same_name(self) -> None:
        a = parse_job_log(_EXCEPTION_VOLATILE_PID, job_name="test-macos")
        b = parse_job_log(
            _EXCEPTION_VOLATILE_PID.replace("15868", "22991"), job_name="test-macos"
        )
        assert a[0].test_name == b[0].test_name


# ---------------------------------------------------------------------------
# 11. Display-time sanitizer (issue #1, Stage 3)
# Cleans rows already stored in the cache by an older parser version, so the
# dashboard renders cleanly even before a full re-parse. Uses the exact junk
# names observed on the live dashboard.
# ---------------------------------------------------------------------------

from scripts.ci_health.log_parser import sanitize_cached_failure


class TestSanitizeCachedFailure:
    """Display-time cleanup of stale cached failure names."""

    def test_drops_legacy_process_error_row(self) -> None:
        assert (
            sanitize_cached_failure(
                "Process error: Process completed with exit code 2."
            )
            is None
        )

    def test_drops_bare_generic_exit_code(self) -> None:
        assert sanitize_cached_failure("Process completed with exit code 1.") is None

    def test_drops_clients_state_banner(self) -> None:
        assert sanitize_cached_failure("clients state report follows.") is None

    def test_drops_bare_pid_token(self) -> None:
        assert sanitize_cached_failure("pid:51740") is None
        assert sanitize_cached_failure("pid:37814") is None

    def test_normalizes_volatile_exception_name(self) -> None:
        out = sanitize_cached_failure(
            "Exception: Executing test client: assertion:Process 15868 crashed"
        )
        assert out is not None
        assert "15868" not in out
        assert "<pid>" in out

    def test_volatile_exception_names_aggregate(self) -> None:
        a = sanitize_cached_failure("Exception: assertion:Process 15868 crashed")
        b = sanitize_cached_failure("Exception: assertion:Process 22991 crashed")
        assert a == b

    def test_keeps_real_test_name_unchanged(self) -> None:
        name = "Test dual-channel-replication primary gets cob overrun in tests/integration/dual-channel-replication.tcl"
        assert sanitize_cached_failure(name) == name


# ---------------------------------------------------------------------------
# 12. Server crash bucket (issue #1 follow-up, Track B)
# A failed job with a Valkey crash signature but no attributable test name
# gets a dedicated "<job>: server crash" bucket (signal in the detail),
# distinct from the generic unattributed bucket (build breaks / timeouts).
# Distilled from real sanitizer job 83957022030 (SIGSEGV, no [err] marker).
# ---------------------------------------------------------------------------

_SERVER_CRASH_NO_TEST = """\
Testing unit/moduleapi/misc
[ok]: RESP3: RM_ReplyWithError: an error reply (1 ms)
135168:X 29 Jun 2026 01:04:42.191 # valkey 255.255.255 crashed by signal: 11, si_code: 0
135168:X 29 Jun 2026 01:04:42.191 # Accessing address: 0x3e90002178f
135168:X 29 Jun 2026 01:04:42.191 # Killed by PID: 137103, UID: 1001
135168:X 29 Jun 2026 01:04:42.191 # Crashed running the instruction at: 0x7f17daf0e80b
------ STACK TRACE ------
Total test duration: 242 seconds
WARNING 1 test(s) failed.
##[error]Process completed with exit code 1.
"""


class TestServerCrashBucket:
    """Crash with no attributable test -> distinct per-job 'server crash' bucket."""

    def test_crash_becomes_server_crash_bucket(self) -> None:
        fs = parse_job_log(
            _SERVER_CRASH_NO_TEST, job_name="test-sanitizer-address (clang)"
        )
        assert len(fs) == 1
        assert fs[0].test_name == "test-sanitizer-address (clang): server crash"
        assert "unattributed" not in fs[0].test_name
        # signal surfaced in the error detail
        assert "signal: 11" in fs[0].error_summary

    def test_crash_bucket_without_job_name(self) -> None:
        fs = parse_job_log(_SERVER_CRASH_NO_TEST)
        assert fs[0].test_name == "server crash"

    def test_attributed_test_takes_precedence_over_crash(self) -> None:
        # A crash that DID produce an [err] stays attributed to the test.
        log = (
            _SERVER_CRASH_NO_TEST
            + "[err]: some real test in tests/unit/foo.tcl\nboom\n"
        )
        fs = parse_job_log(log, job_name="test-x")
        names = {f.test_name for f in fs}
        assert "some real test in tests/unit/foo.tcl" in names
        assert "test-x: server crash" not in names

    def test_reports_last_fatal_crash_not_earlier_benign(self) -> None:
        # Real logs may contain an earlier intentional crash-test (signal 6)
        # that passes, then the actual fatal crash (signal 11) that ends the
        # run. The detail must report the fatal (last) one, with its address.
        log = (
            "Testing unit/other\n"
            "999:M 29 Jun 2026 00:00:00.000 # valkey crashed by signal: 6, si_code: 0\n"
            "999:M 29 Jun 2026 00:00:00.000 # Accessing address: 0xdead\n"
            "[ok]: DEBUG SEGFAULT / crash log format (5 ms)\n"
            "135168:X 29 Jun 2026 01:04:42.191 # valkey crashed by signal: 11, si_code: 0\n"
            "135168:X 29 Jun 2026 01:04:42.191 # Accessing address: 0x3e90002178f\n"
            "135168:X 29 Jun 2026 01:04:42.191 # Killed by PID: 137103, UID: 1001\n"
            "WARNING 1 test(s) failed.\n"
            "##[error]Process completed with exit code 1.\n"
        )
        fs = parse_job_log(log, job_name="test-sanitizer-address (clang)")
        assert len(fs) == 1
        assert fs[0].test_name == "test-sanitizer-address (clang): server crash"
        assert "signal: 11" in fs[0].error_summary
        assert "0x3e90002178f" in fs[0].error_summary
        assert "signal: 6" not in fs[0].error_summary
