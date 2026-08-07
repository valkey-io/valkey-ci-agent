"""Tests for recovering gtest failure output from a job's console log."""

from __future__ import annotations

from scripts.test_failure_detector.gtest_log_parser import (
    parse_gtest_failures_from_log,
)

# A real block as GitHub stores it: an ISO timestamp on every line and gtest's
# colour codes around its status tags.
_REAL_LOG = (
    "2026-07-29T23:29:31.7910142Z [6/298] DictTest.BasicOps (2 ms)\n"
    "2026-07-29T23:29:31.7912499Z \x1b[0;33mNote: Google Test filter = DictTest.BasicOps\n"
    "2026-07-29T23:29:31.7914000Z [==========] Running 1 test from 1 test suite.\n"
    "2026-07-29T23:29:31.7916184Z \x1b[0;32m[ RUN      ] \x1b[mDictTest.BasicOps\n"
    "2026-07-29T23:29:31.7916693Z test_dict.cpp:34: Failure\n"
    "2026-07-29T23:29:31.7917000Z Expected equality of these values:\n"
    "2026-07-29T23:29:31.7917500Z   got\n"
    "2026-07-29T23:29:31.7918000Z     Which is: \"myvalue\"\n"
    "2026-07-29T23:29:31.7918500Z   \"wrongvalue\"\n"
    "2026-07-29T23:29:31.7919098Z \x1b[0;31m[  FAILED  ] \x1b[mDictTest.BasicOps (0 ms)\n"
    "2026-07-29T23:29:31.7923059Z  1 FAILED TEST\n"
    "2026-07-29T23:29:31.7923540Z [6/298] DictTest.BasicOps returned with exit code 1 (2 ms)\n"
)


class TestParseGtestFailuresFromLog:
    def test_recovers_the_failing_tests_output(self) -> None:
        out = parse_gtest_failures_from_log(_REAL_LOG.encode())
        assert list(out) == ["DictTest.BasicOps"]
        body = out["DictTest.BasicOps"]
        assert "test_dict.cpp:34: Failure" in body
        assert "Expected equality of these values:" in body
        assert '"wrongvalue"' in body

    def test_strips_timestamps_and_colour_codes(self) -> None:
        """Both are log transport, not diagnostic. The timestamp also has to go
        or two runs of one failure would never compare equal in the recurrence
        check."""
        body = parse_gtest_failures_from_log(_REAL_LOG.encode())["DictTest.BasicOps"]
        assert "2026-07-29T23:29:31" not in body
        assert "\x1b[" not in body
        assert "[ RUN      ] DictTest.BasicOps" in body

    def test_a_passing_test_is_not_reported(self) -> None:
        """Only a block closed by a non-zero exit code is a failure, so a test
        that passed on a retry contributes nothing."""
        log = (
            "[1/2] DictTest.Passing (1 ms)\n"
            "[  OK  ] DictTest.Passing\n"
            "[2/2] DictTest.Other (1 ms)\n"
            "[2/2] DictTest.Other returned with exit code 1 (1 ms)\n"
        )
        assert list(parse_gtest_failures_from_log(log.encode())) == ["DictTest.Other"]

    def test_interleaved_blocks_stay_separate(self) -> None:
        """gtest-parallel runs tests concurrently, so one test's output can
        appear between another's start and end."""
        log = (
            "[1/2] SuiteA.One (1 ms)\n"
            "a-first-line\n"
            "[2/2] SuiteB.Two (1 ms)\n"
            "b-first-line\n"
            "[1/2] SuiteA.One returned with exit code 1 (1 ms)\n"
            "[2/2] SuiteB.Two returned with exit code 1 (1 ms)\n"
        )
        out = parse_gtest_failures_from_log(log.encode())
        assert set(out) == {"SuiteA.One", "SuiteB.Two"}
        assert "a-first-line" in out["SuiteA.One"]
        assert "b-first-line" in out["SuiteB.Two"]
        assert "b-first-line" not in out["SuiteA.One"]

    def test_a_log_with_no_gtest_blocks_yields_nothing(self) -> None:
        """The normal case for a job that runs no unit tests."""
        assert parse_gtest_failures_from_log(b"make: *** [all] Error 1\n") == {}

    def test_an_unterminated_block_is_not_reported(self) -> None:
        """A job killed mid-test leaves an open block, which says nothing about
        whether the test failed."""
        log = "[1/2] SuiteA.One (1 ms)\nsome output\n"
        assert parse_gtest_failures_from_log(log.encode()) == {}

    def test_a_runaway_block_is_capped(self) -> None:
        """A test that logs in a loop must not put thousands of lines in an
        issue body. The head holds the assertion."""
        noise = "".join(f"line {i}\n" for i in range(5000))
        log = (
            "[1/1] SuiteA.One (1 ms)\n"
            f"{noise}"
            "[1/1] SuiteA.One returned with exit code 1 (1 ms)\n"
        )
        body = parse_gtest_failures_from_log(log.encode())["SuiteA.One"]
        assert body.count("\n") < 250
        assert "line 0" in body

    def test_invalid_utf8_does_not_raise(self) -> None:
        log = b"[1/1] SuiteA.One (1 ms)\n\xff\xfe bad bytes\n[1/1] SuiteA.One returned with exit code 1 (1 ms)\n"
        assert "SuiteA.One" in parse_gtest_failures_from_log(log)

    def test_a_retry_that_passed_is_not_reported(self) -> None:
        """gtest-parallel reruns a failed test and closes the retry with exit
        code 0. Only the non-zero close is a failure, so a test that passed on
        retry contributes no output."""
        log = (
            "[1/1] SuiteA.One (1 ms)\n"
            "first attempt failed\n"
            "[1/1] SuiteA.One returned with exit code 1 (1 ms)\n"
            "[1/1] SuiteA.One (1 ms)\n"
            "second attempt passed\n"
            "[1/1] SuiteA.One returned with exit code 0 (1 ms)\n"
        )
        assert parse_gtest_failures_from_log(log.encode()) == {}
