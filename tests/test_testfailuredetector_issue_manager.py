"""Tests for test-failure issue creation/update (mocked GitHub API)."""

from __future__ import annotations

import re
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

# PyGithub requires urllib3 v2 + OpenSSL 1.1.1+. On older dev hosts the import
# fails at collection time. Guard with a skip so the test file is still valid.
try:
    from scripts.common.issue_dedup import _fingerprint_marker_re
    from scripts.test_failure_detector.issue_renderer import (
        MARKER_NAMESPACE,
        _build_body,
        _build_title,
        _extract_environments_from_body,
        _extract_error_from_body,
        _update_environments_in_body,
        fingerprint_for,
        label_for,
        marker_namespace_for,
        renderer_for,
        title_for,
        trace_digest,
    )
    from scripts.test_failure_detector.manage_issues import (
        CLOSED_ISSUE_LOOKBACK,
        process_failures,
    )
    from scripts.test_failure_detector.parse_failures import (
        FailureType,
        JobReference,
        UniqueFailure,
        normalize_error_identity,
    )

    _SKIP_REASON = None
except ImportError as _exc:
    _SKIP_REASON = f"PyGithub import failed: {_exc}"

pytestmark = pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")


# --- Helper fixtures ---


def _make_failure(
    test_name: str = "PSYNC2 test",
    test_file: str = "tests/integration/replication-psync.tcl",
    error: str = "Expected replica to be in sync",
    jobs: list[tuple[str, str, str]] | None = None,
) -> UniqueFailure:
    if jobs is None:
        jobs = [("test-ubuntu-latest", "integration", "https://example.com/job/1")]
    return UniqueFailure(
        test_name=test_name,
        test_file=test_file,
        error=error,
        jobs=[JobReference(job=j, suite=s, url=u) for j, s, u in jobs],
    )


# --- Unit tests for the renderer ---


class TestBuildIssueTitle:
    def test_format(self) -> None:
        title = _build_title(_make_failure())
        assert title == "[TEST-FAILURE] PSYNC2 test in tests/integration/replication-psync.tcl"


class TestFingerprint:
    def test_is_stable_hex_token(self) -> None:
        """Hashed, not raw: a fixed-shape lowercase-hex token safe to embed in
        an HTML comment marker and a search query."""
        fp = fingerprint_for(_make_failure())
        assert re.fullmatch(r"[0-9a-f]{20}", fp)

    def test_deterministic(self) -> None:
        assert fingerprint_for(_make_failure()) == fingerprint_for(_make_failure())

    def test_distinguishes_name_and_file(self) -> None:
        base = fingerprint_for(_make_failure())
        assert fingerprint_for(_make_failure(test_name="other")) != base
        assert fingerprint_for(_make_failure(test_file="other.tcl")) != base

    def test_digits_are_significant(self) -> None:
        """PSYNC2 vs PSYNC3 must not collapse; the identity is not normalized."""
        assert (
            fingerprint_for(_make_failure(test_name="PSYNC2"))
            != fingerprint_for(_make_failure(test_name="PSYNC3"))
        )

    def test_unsafe_characters_do_not_leak(self) -> None:
        """Quotes, newlines, and comment-breaking text are hashed away, so the
        marker/query embedding can't be broken by hostile test names."""
        fp = fingerprint_for(_make_failure(
            test_name='evil "--> <!-- ' + "\n" + 'x', test_file="a\"b\nc",
        ))
        assert re.fullmatch(r"[0-9a-f]{20}", fp)


class TestBuildIssueBody:
    def _body(self, failure: UniqueFailure) -> str:
        return _build_body(failure, marker="<!-- m -->", occurrences=1)

    def test_contains_marker_and_occurrences(self) -> None:
        body = self._body(_make_failure())
        assert "<!-- m -->" in body
        assert f"<!-- {MARKER_NAMESPACE}:occurrences:1 -->" in body

    def test_contains_test_name(self) -> None:
        assert "`PSYNC2 test`" in self._body(_make_failure())

    def test_contains_test_file(self) -> None:
        assert "`tests/integration/replication-psync.tcl`" in self._body(_make_failure())

    def test_contains_error_trace(self) -> None:
        assert "assertion failed at line 42" in self._body(
            _make_failure(error="assertion failed at line 42")
        )

    def test_contains_environments_and_links(self) -> None:
        body = self._body(_make_failure(jobs=[
            ("job-a", "suite", "https://example.com/run"),
            ("job-b", "suite", "url2"),
        ]))
        assert "`job-a`" in body
        assert "`job-b`" in body
        assert "[CI link](https://example.com/run)" in body

    def test_contains_auto_created_footer(self) -> None:
        assert "Auto-created by Test Failure Detector" in self._body(_make_failure())


class TestTraceTextCannotForgeBodyStructure:
    """A tool's report is embedded verbatim, so text in it that looks like the
    body's own markers or rows must not be read back as the real thing."""

    def _body(self, error: str) -> str:
        return _build_body(_make_failure(error=error), marker="<!-- real -->",
                           occurrences=1)

    def test_a_marker_shaped_comment_in_a_trace_is_inert(self) -> None:
        body = self._body(f"<!-- {MARKER_NAMESPACE}:abcdef1234567890abcd -->\nboom")
        assert "<! --" in body
        assert body.count("<!-- real -->") == 1

    def test_an_environments_row_in_a_trace_does_not_shadow_the_real_row(self) -> None:
        """The row readers are anchored to a line start but search the whole
        body, and the trace sits above the real row, so anchoring alone would
        leave the trace matching first."""
        body = self._body("**Environments:** `forged-from-trace`\nboom")
        assert _extract_environments_from_body(body) == ["test-ubuntu-latest"]
        updated = _update_environments_in_body(body, ["test-ubuntu-latest", "job-b"])
        assert "`forged-from-trace`" in updated

    def test_a_long_backtick_run_does_not_close_the_fence(self) -> None:
        """The fence is clamped, so a run reaching the clamp would close the
        block early and the trace would render as broken markdown."""
        body = self._body("`" * 70 + "\nboom")
        assert "boom" in _extract_error_from_body(body)
        # No run in the body may be as long as the fence that has to enclose it.
        fence = max(len(m) for m in re.findall(r"`+", body))
        runs_in_trace = re.findall(r"`+", _extract_error_from_body(body))
        assert runs_in_trace and max(len(r) for r in runs_in_trace) < fence


class TestReportedTraceRecordIsNotAFingerprintClaim:
    def test_a_recorded_digest_does_not_read_as_a_claim(self) -> None:
        """issue_dedup treats a namespaced marker ending in bare hex as a
        fingerprint claim. A lone recorded digest would satisfy that and make
        the issue unadoptable by title, so the digest is not bare hex."""
        digest = trace_digest("some tool output")
        body = f"<!-- valkey-ci-agent:reported-traces:{digest} -->"
        assert _fingerprint_marker_re(MARKER_NAMESPACE).search(body) is None


class TestExtractEnvironments:
    def test_extracts_backtick_envs(self) -> None:
        body = "**Environments:** `job-a`, `job-b`, `job-c`"
        assert _extract_environments_from_body(body) == ["job-a", "job-b", "job-c"]

    def test_returns_empty_when_no_match(self) -> None:
        assert _extract_environments_from_body("No environments line here") == []


class TestUpdateEnvironments:
    def test_replaces_environments_line(self) -> None:
        body = "Some text\n**Environments:** `old-job`\nMore text"
        updated = _update_environments_in_body(body, ["old-job", "new-job"])
        assert "**Environments:** `old-job`, `new-job`" in updated
        assert "Some text" in updated
        assert "More text" in updated


class TestMergeEnvironments:
    """The body_transform hook that carries the running env list forward."""

    def test_adds_new_environment(self) -> None:
        renderer = renderer_for(_make_failure(jobs=[("new-job", "suite", "url")]))
        result = renderer.merge_environments("**Environments:** `old-job`")
        assert "`old-job`" in result
        assert "`new-job`" in result

    def test_no_change_when_env_already_present(self) -> None:
        body = "**Environments:** `test-ubuntu-latest`"
        renderer = renderer_for(_make_failure())  # job is test-ubuntu-latest
        assert renderer.merge_environments(body) == body


# --- Integration tests with a mocked publisher ---


class TestProcessFailures:
    @patch("scripts.test_failure_detector.manage_issues.IssueDedupPublisher")
    def test_tallies_actions(self, mock_publisher_cls) -> None:
        publisher = mock_publisher_cls.return_value
        publisher.upsert.side_effect = [
            ("created", "https://x/issues/1"),
            ("updated", "https://x/issues/2"),
            ("skipped-duplicate", "https://x/issues/3"),
            ("skipped-recently-closed", "https://x/issues/4"),
        ]

        failures = [
            _make_failure(test_name="a"),
            _make_failure(test_name="b"),
            _make_failure(test_name="c"),
            _make_failure(test_name="d"),
        ]
        result = process_failures(MagicMock(), "valkey-io/valkey", failures)

        assert result == {
            "created": 1, "updated": 1, "skipped": 1, "skipped_closed": 1, "errors": 0,
        }

    @patch("scripts.test_failure_detector.manage_issues.IssueDedupPublisher")
    def test_one_failing_upsert_does_not_abort_the_batch(self, mock_publisher_cls) -> None:
        """A raised exception on one failure is counted as an error and skipped;
        the failures after it are still processed."""
        publisher = mock_publisher_cls.return_value
        publisher.upsert.side_effect = [
            ("created", "https://x/issues/1"),
            RuntimeError("boom"),  # failure b must not kill the loop
            ("updated", "https://x/issues/3"),
        ]

        failures = [
            _make_failure(test_name="a"),
            _make_failure(test_name="b"),
            _make_failure(test_name="c"),
        ]
        result = process_failures(MagicMock(), "valkey-io/valkey", failures)

        assert result == {
            "created": 1, "updated": 1, "skipped": 0, "skipped_closed": 0, "errors": 1,
        }
        # All three were attempted despite the middle one raising.
        assert publisher.upsert.call_count == 3

    @patch("scripts.test_failure_detector.manage_issues.IssueDedupPublisher")
    def test_unexpected_action_is_isolated_as_error(self, mock_publisher_cls) -> None:
        """An unexpected upsert action is contained as a single errored failure
        rather than propagating and aborting the run."""
        publisher = mock_publisher_cls.return_value
        publisher.upsert.side_effect = [
            ("bogus-action", "https://x/issues/1"),
            ("created", "https://x/issues/2"),
        ]

        result = process_failures(
            MagicMock(), "valkey-io/valkey",
            [_make_failure(test_name="a"), _make_failure(test_name="b")],
        )

        assert result == {
            "created": 1, "updated": 0, "skipped": 0, "skipped_closed": 0, "errors": 1,
        }

    @patch("scripts.test_failure_detector.manage_issues.IssueDedupPublisher")
    def test_passes_run_id_as_idempotency_key(self, mock_publisher_cls) -> None:
        publisher = mock_publisher_cls.return_value
        publisher.upsert.return_value = ("created", "https://x/issues/1")

        process_failures(MagicMock(), "valkey-io/valkey", [_make_failure()], run_id=12345)

        kwargs = publisher.upsert.call_args.kwargs
        assert kwargs["idempotency_key"] == "12345"
        assert kwargs["fingerprint"] == fingerprint_for(_make_failure())
        assert callable(kwargs["body_transform"])
        # The migration fallback title matches what render produces.
        assert kwargs["title_fallback"] == title_for(_make_failure())
        assert kwargs["title_fallback"] == _build_title(_make_failure())

    @patch("scripts.test_failure_detector.manage_issues.IssueDedupPublisher")
    def test_no_run_id_means_no_idempotency_key(self, mock_publisher_cls) -> None:
        publisher = mock_publisher_cls.return_value
        publisher.upsert.return_value = ("created", "https://x/issues/1")

        process_failures(MagicMock(), "valkey-io/valkey", [_make_failure()])

        assert publisher.upsert.call_args.kwargs["idempotency_key"] is None

    @patch("scripts.test_failure_detector.manage_issues.IssueDedupPublisher")
    def test_detector_opts_in_to_closed_lookback(self, mock_publisher_cls) -> None:
        """The recently-closed check is off by default on the shared publisher;
        the detector must enable it explicitly with its 1-day window."""
        publisher = mock_publisher_cls.return_value
        publisher.upsert.return_value = ("created", "https://x/issues/1")

        process_failures(MagicMock(), "valkey-io/valkey", [_make_failure()])

        kwargs = mock_publisher_cls.call_args.kwargs
        assert kwargs["closed_lookback"] == CLOSED_ISSUE_LOOKBACK
        assert CLOSED_ISSUE_LOOKBACK == timedelta(days=1)

    def test_render_callable_produces_labelled_content(self) -> None:
        content = renderer_for(_make_failure()).render("<!-- m -->", 1)
        assert content.labels == ("test-failure",)
        assert content.title.startswith("[TEST-FAILURE]")


class TestRecurrenceCommentNewlyFailing:
    """The recurrence comment calls out environments failing for the first time
    on this run (PR #24 review r3431750542)."""

    def test_names_newly_failing_environments(self) -> None:
        # New job 'test-arm64' is not in the prior body; the body_transform
        # records it, then render names it in the recurrence comment.
        renderer = renderer_for(_make_failure(jobs=[("test-arm64", "suite", "url")]))
        renderer.merge_environments("**Environments:** `test-ubuntu-latest`")
        comment = renderer.render("<!-- m -->", 2).comment
        assert "**Newly failing in:** `test-arm64`" in comment
        assert "Test failed again on" in comment

    def test_omits_newly_failing_line_when_no_new_environment(self) -> None:
        # The only job is already recorded, so there is nothing new to call out.
        renderer = renderer_for(_make_failure())  # job is test-ubuntu-latest
        renderer.merge_environments("**Environments:** `test-ubuntu-latest`")
        comment = renderer.render("<!-- m -->", 2).comment
        assert "Newly failing in" not in comment

    def test_no_newly_failing_line_without_body_transform(self) -> None:
        # On the create path body_transform never runs, so the comment (unused
        # there) carries no newly-failing line rather than a spurious one.
        comment = renderer_for(_make_failure()).render("<!-- m -->", 1).comment
        assert "Newly failing in" not in comment


class TestExtractErrorFromBody:
    """Round-trips the Error stack trace section written by _build_body."""

    def test_extracts_trace_written_by_build_body(self) -> None:
        body = _build_body(
            _make_failure(error="assertion failed at line 42"),
            marker="<!-- m -->", occurrences=1,
        )
        assert _extract_error_from_body(body) == "assertion failed at line 42"

    def test_returns_empty_when_no_error_section(self) -> None:
        # Issues created before the Error stack trace section existed.
        assert _extract_error_from_body("**Environments:** `job-a`") == ""

    def test_round_trips_error_containing_backtick_fence(self) -> None:
        """An error that itself contains ``` must survive the body round-trip
        intact; a truncated read-back would make _detect_new_error flag a
        spurious "new error" on every recurrence."""
        error = "assertion failed\n```\nembedded block\n```\ntrailing context"
        body = _build_body(
            _make_failure(error=error), marker="<!-- m -->", occurrences=1,
        )
        assert _extract_error_from_body(body) == error


class TestRecurrenceCommentNewError:
    """The recurrence comment surfaces a changed error trace so a triager can
    notice the failure mode shifted without diffing the issue body."""

    def _body_with_error(self, error: str) -> str:
        return _build_body(
            _make_failure(error=error), marker="<!-- m -->", occurrences=1,
        )

    def test_calls_out_changed_trace(self) -> None:
        # The issue recorded one trace; this run failed with a different one.
        renderer = renderer_for(_make_failure(error="NEW: segfault in dictResize"))
        renderer.merge_environments(self._body_with_error("OLD: timeout waiting for sync"))
        comment = renderer.render("<!-- m -->", 2).comment
        assert "**New error stack trace**" in comment
        assert "NEW: segfault in dictResize" in comment

    def test_stays_quiet_when_trace_unchanged(self) -> None:
        renderer = renderer_for(_make_failure(error="same error every time"))
        renderer.merge_environments(self._body_with_error("same error every time"))
        comment = renderer.render("<!-- m -->", 2).comment
        assert "New error stack trace" not in comment

    def test_normalized_equal_trace_stays_quiet(self) -> None:
        # Differs only in run-specific noise (port, timestamp, hex address);
        # normalization treats these as the same trace.
        old = "conn failed 2026-06-26 10:00:00 port=6379 at 0xdead"
        new = "conn failed 2026-06-27 11:22:33 port=7000 at 0xbeef"
        renderer = renderer_for(_make_failure(error=new))
        renderer.merge_environments(self._body_with_error(old))
        comment = renderer.render("<!-- m -->", 2).comment
        assert "New error stack trace" not in comment

    def test_empty_new_error_stays_quiet(self) -> None:
        renderer = renderer_for(_make_failure(error=""))
        renderer.merge_environments(self._body_with_error("OLD: some trace"))
        comment = renderer.render("<!-- m -->", 2).comment
        assert "New error stack trace" not in comment

    def test_legacy_issue_without_recorded_trace_stays_quiet(self) -> None:
        # A legacy issue with no Error stack trace section has no baseline, so
        # the trace must not be called out. Otherwise it would diff against ""
        # and re-post the same "new" trace on every recurrence.
        legacy_body = "**Environments:** `test-ubuntu-latest`"
        renderer = renderer_for(_make_failure(error="some real trace"))
        renderer.merge_environments(legacy_body)
        comment = renderer.render("<!-- m -->", 2).comment
        assert "New error stack trace" not in comment

    def test_no_new_error_line_without_body_transform(self) -> None:
        # Create path: body_transform never runs, so no spurious callout.
        comment = renderer_for(_make_failure()).render("<!-- m -->", 1).comment
        assert "New error stack trace" not in comment


# --- Tests for type-specific fingerprinting and rendering ---


class TestTypeSpecificFingerprint:
    """Fingerprints are scoped by failure type so different categories
    cannot collide, and nameless errors are fingerprinted by error identity."""

    def test_different_types_different_fingerprints(self) -> None:
        """Same test name + file, different type => different fingerprint."""
        assertion = _make_failure()
        timeout = UniqueFailure(
            test_name="PSYNC2 test",
            test_file="tests/integration/replication-psync.tcl",
            failure_type=FailureType.TIMEOUT,
            error="Test timed out",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        assert fingerprint_for(assertion) != fingerprint_for(timeout)

    def test_sanitizer_fingerprint_ignores_pid(self) -> None:
        """Same sanitizer error with different PIDs => same fingerprint."""
        f1 = UniqueFailure(
            test_name="", test_file="",
            failure_type=FailureType.SANITIZER,
            error="==111== ERROR: AddressSanitizer: heap-buffer-overflow\n==111==    at 0xAAA: dictResize (dict.c:100)",
        )
        f2 = UniqueFailure(
            test_name="", test_file="",
            failure_type=FailureType.SANITIZER,
            error="==222== ERROR: AddressSanitizer: heap-buffer-overflow\n==222==    at 0xBBB: dictResize (dict.c:100)",
        )
        assert fingerprint_for(f1) == fingerprint_for(f2)

    def test_valgrind_cross_file_same_fingerprint(self) -> None:
        """Same valgrind error in different test files => same fingerprint.
        The test_file is intentionally excluded from the nameless fingerprint."""
        f1 = UniqueFailure(
            test_name="", test_file="tests/unit/expire.tcl",
            failure_type=FailureType.VALGRIND,
            error="==1== Invalid read of size 4\n==1==    at 0xA: dictResize (dict.c:100)",
        )
        f2 = UniqueFailure(
            test_name="", test_file="tests/unit/cluster.tcl",
            failure_type=FailureType.VALGRIND,
            error="==2== Invalid read of size 4\n==2==    at 0xB: dictResize (dict.c:100)",
        )
        assert fingerprint_for(f1) == fingerprint_for(f2)

    def test_valgrind_same_leak_two_jobs_one_fingerprint(self) -> None:
        """The real #114/#115 case: one leak from debugCommand, reported by two
        valgrind jobs, differs only in the leaked size and whether the trailing
        "ERROR SUMMARY" line falls inside the identity window. Both must produce
        one fingerprint so the pair collapses into a single issue."""
        def report(size: int, extra_tail: str) -> UniqueFailure:
            error = (
                " Valgrind error: ==1== Memcheck, a memory error detector\n"
                "==1== HEAP SUMMARY:\n"
                f"==1== {size} bytes in 1 blocks are definitely lost in loss record 900 of 1,111\n"
                "==1==    at 0x4846828: malloc (vgpreload_memcheck.so)\n"
                "==1==    by 0x318A40: ztrymalloc_usable_internal (zmalloc.c:172)\n"
                "==1==    by 0x29078A: sdsdup (sds.c:190)\n"
                "==1==    by 0x1E80D6: debugCommand (debug.c:569)\n"
                f"{extra_tail}"
            )
            return UniqueFailure(
                test_name="", test_file="tests/unit/other.tcl",
                failure_type=FailureType.VALGRIND, error=error,
                jobs=[JobReference(job="j", suite="s", url="u")],
            )
        f114 = report(49, "==1== ERROR SUMMARY: 36 errors from 36 contexts (suppressed: 0 from 0)")
        f115 = report(41, "==1== still reachable: 931,751 bytes in 12,710 blocks\n==1== suppressed: 0 bytes")
        assert fingerprint_for(f114) == fingerprint_for(f115)

    def test_different_sanitizer_bugs_different_fingerprints(self) -> None:
        f1 = UniqueFailure(
            test_name="", test_file="",
            failure_type=FailureType.SANITIZER,
            error="==1== ERROR: AddressSanitizer: heap-buffer-overflow\n==1==    at 0xA: dictResize (dict.c:100)",
        )
        f2 = UniqueFailure(
            test_name="", test_file="",
            failure_type=FailureType.SANITIZER,
            error="==1== ERROR: AddressSanitizer: use-after-free\n==1==    at 0xA: listRelease (adlist.c:50)",
        )
        assert fingerprint_for(f1) != fingerprint_for(f2)

    def test_unittest_has_named_fingerprint(self) -> None:
        """gtest failures have test_name, so they use the named path."""
        f = UniqueFailure(
            test_name="DictTest.BasicOps",
            test_file="src/unit/valkey-unit-gtests",
            failure_type=FailureType.UNITTEST,
        )
        assert f.has_test_identity
        fp = fingerprint_for(f)
        assert re.fullmatch(r"[0-9a-f]{20}", fp)

    def test_assertion_fingerprint_unchanged_from_legacy(self) -> None:
        """Assertion-type fingerprint uses the same namespace as before
        so existing issues are still matched."""
        f = _make_failure()
        ns = marker_namespace_for(f)
        assert ns == MARKER_NAMESPACE

    def test_every_type_has_its_own_namespace(self) -> None:
        """One namespace per type, so two types cannot share an issue.

        A type missing from the table silently falls back to the assertion
        namespace, where a title collision would let it adopt an assertion's
        issue and leave itself unfiled.
        """
        namespaces = {
            t: marker_namespace_for(UniqueFailure("", "", failure_type=t))
            for t in FailureType
        }
        assert len(set(namespaces.values())) == len(namespaces), namespaces


class TestTypeSpecificRendering:
    """Type-specific title prefixes, labels, and body format."""

    def test_every_type_shares_one_title_prefix(self) -> None:
        """The type is named in the body, not the title, so an issue is not
        retitled when the same bug is reattributed to another type."""
        for failure_type in FailureType:
            f = UniqueFailure(
                test_name="", test_file="tests/unit/expire.tcl",
                failure_type=failure_type,
                error="Invalid read of size 4",
                jobs=[JobReference(job="j", suite="s", url="u")],
            )
            assert title_for(f).startswith("[TEST-FAILURE]")

    def test_valgrind_leak_title_format(self) -> None:
        # The Memcheck banner is the first line of every
        # valgrind report, so a first-line title gives all valgrind issues
        # the same name. A leak title leads with the leak kind, then the
        # size and the first non-plumbing source frame:
        # "Definitely lost in debugCommand (debug.c:569)".
        error = (
            " Valgrind error: ==6554== Memcheck, a memory error detector\n"
            "==6554== Copyright (C) 2002-2022, and GNU GPL'd, by Julian Seward et al.\n"
            "==6554== HEAP SUMMARY:\n"
            "==6554== 49 bytes in 1 blocks are definitely lost in loss record 900 of 1,109\n"
            "==6554==    at 0x4846828: malloc (in /usr/libexec/valgrind/vgpreload_memcheck-amd64-linux.so)\n"
            "==6554==    by 0x3189FB: ztrymalloc_usable_internal (zmalloc.c:172)\n"
            "==6554==    by 0x2902DE: _sdsnewlen (sds.c:102)\n"
            "==6554==    by 0x1E8076: debugCommand (debug.c:569)\n"
        )
        f = UniqueFailure(
            test_name="", test_file="tests/unit/other.tcl",
            failure_type=FailureType.VALGRIND,
            error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        # The test file is the volatile detection context (valgrind keys its
        # fingerprint on the error, not the file), so it is not in the title.
        title = title_for(f)
        assert title == (
            "[TEST-FAILURE] Definitely lost in debugCommand (debug.c:569)"
        )

    def test_valgrind_leak_title_ignores_loss_record_and_pid_drift(self) -> None:
        # Loss-record coordinates and PIDs drift between runs of the same
        # leak and must not affect the title.
        def leak(record: str, pid: str) -> UniqueFailure:
            error = (
                f" Valgrind error: =={pid}== Memcheck, a memory error detector\n"
                f"=={pid}== 49 bytes in 1 blocks are definitely lost in {record}\n"
                f"=={pid}==    by 0x1E8076: debugCommand (debug.c:569)\n"
            )
            return UniqueFailure(
                test_name="", test_file="tests/unit/other.tcl",
                failure_type=FailureType.VALGRIND, error=error,
                jobs=[JobReference(job="j", suite="s", url="u")],
            )
        t1 = title_for(leak("loss record 900 of 1,109", "6554"))
        t2 = title_for(leak("loss record 903 of 1,214", "7801"))
        assert t1 == t2
        assert "loss record" not in t1
        assert "6554" not in t1

    def test_valgrind_titles_distinguish_different_leak_sites(self) -> None:
        def leak(site: str) -> UniqueFailure:
            error = (
                " Valgrind error: ==1== Memcheck, a memory error detector\n"
                "==1== 49 bytes in 1 blocks are definitely lost in loss record 900 of 1,109\n"
                f"==1==    by 0x1E8076: {site}\n"
            )
            return UniqueFailure(
                test_name="", test_file="tests/unit/other.tcl",
                failure_type=FailureType.VALGRIND, error=error,
                jobs=[JobReference(job="j", suite="s", url="u")],
            )
        t_debug = title_for(leak("debugCommand (debug.c:569)"))
        t_cluster = title_for(leak("clusterCommand (cluster.c:123)"))
        assert t_debug != t_cluster

    def test_timeout_title_with_test_name(self) -> None:
        f = UniqueFailure(
            test_name="PSYNC2 test",
            test_file="tests/integration/replication-psync.tcl",
            failure_type=FailureType.TIMEOUT,
            error="Test timed out",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        title = title_for(f)
        assert title.startswith("[TEST-FAILURE]")
        assert "PSYNC2 test" in title

    def test_unittest_title(self) -> None:
        f = UniqueFailure(
            test_name="DictTest.BasicOps",
            test_file="src/unit/valkey-unit-gtests",
            failure_type=FailureType.UNITTEST,
            error="gtest FAIL",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        title = title_for(f)
        assert title.startswith("[TEST-FAILURE]")
        assert "DictTest.BasicOps" in title

    def test_startup_title_without_test_name(self) -> None:
        # Startup keys its fingerprint on the error, not the file, so the file
        # is left out of the title and the same failure keeps one title across
        # the different files it is detected under.
        def startup(test_file: str) -> UniqueFailure:
            return UniqueFailure(
                test_name="", test_file=test_file,
                failure_type=FailureType.STARTUP,
                error="Can't start /path/to/valkey-server",
                jobs=[JobReference(job="j", suite="s", url="u")],
            )
        title = title_for(startup("tests/unit/cluster.tcl"))
        assert title.startswith("[TEST-FAILURE]")
        assert "cluster.tcl" not in title
        assert title == title_for(startup("tests/unit/expire.tcl"))

    def test_all_types_use_test_failure_label(self) -> None:
        for ftype in FailureType:
            f = UniqueFailure(
                test_name="t" if ftype in (FailureType.ASSERTION, FailureType.TIMEOUT, FailureType.UNITTEST) else "",
                test_file="f.tcl",
                failure_type=ftype,
                error="some error",
                jobs=[JobReference(job="j", suite="s", url="u")],
            )
            assert label_for(f) == "test-failure"

    def test_renderer_uses_test_failure_label(self) -> None:
        f = UniqueFailure(
            test_name="DictTest.Ops",
            test_file="src/unit/valkey-unit-gtests",
            failure_type=FailureType.UNITTEST,
            error="gtest FAIL",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        content = renderer_for(f).render("<!-- m -->", 1)
        assert content.labels == ("test-failure",)

    def test_nameless_body_uses_the_same_sections_as_a_named_one(self) -> None:
        """Every type shares one body shape, so an issue reads the same
        whichever type it came from. A nameless failure has no test name to
        list, but the sections around it are identical."""
        f = UniqueFailure(
            test_name="", test_file="tests/unit/expire.tcl",
            failure_type=FailureType.SANITIZER,
            error="Sanitizer error: heap-buffer-overflow",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        body = _build_body(f, marker="<!-- m -->", occurrences=1)
        assert "**Failing test(s)**" in body
        assert "**Error details**" not in body
        assert "is failing in CI." in body
        # Every row the template defines is filled, so one shape parses for all
        # types; a failure naming no test gets the filler.
        assert "- Test name: `[no test]`" in body
        assert "- Test file: `tests/unit/expire.tcl`" in body

    def test_named_body_fills_template_rows_without_type_row(self) -> None:
        """A named failure fills the Test name and Test file rows and adds no
        Failure type row."""
        f = UniqueFailure(
            test_name="PSYNC2 test",
            test_file="tests/integration/replication-psync.tcl",
            failure_type=FailureType.TIMEOUT,
            error="Test timed out",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        body = _build_body(f, marker="<!-- m -->", occurrences=1)
        assert "- Test name: `PSYNC2 test`" in body
        assert "- Test file: `tests/integration/replication-psync.tcl`" in body
        assert "Failure type:" not in body

    def test_type_specific_namespace_in_body(self) -> None:
        """Body uses type-specific namespace for the occurrences marker."""
        f = UniqueFailure(
            test_name="", test_file="",
            failure_type=FailureType.VALGRIND,
            error="Valgrind error: Invalid read",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        body = _build_body(f, marker="<!-- m -->", occurrences=3)
        assert "valkey-ci-agent:valgrind-error:occurrences:3" in body


class TestTitleFollowsFingerprint:
    """The publisher re-titles on every update, so two reports of one bug (equal
    fingerprints) must render one title. A token that is volatile in the title
    but scrubbed from the identity rewrites the same issue's title every run.
    """

    # (label, failure type, run-1 error, run-2 error) for one bug seen twice,
    # each pair differing only in tokens the identity treats as noise.
    _RECURRENCES = (
        (
            "valgrind leak size and loss record drift",
            FailureType.VALGRIND,
            "Valgrind error: 49 bytes in 1 blocks are definitely lost in loss "
            "record 900 of 1,109\n"
            "   at 0x484A2F3: malloc (vg_replace_malloc.c:381)\n"
            "   by 0x1E9111: debugCommand (debug.c:569)\n",
            "Valgrind error: 52 bytes in 1 blocks are definitely lost in loss "
            "record 913 of 1,120\n"
            "   at 0x484A2F3: malloc (vg_replace_malloc.c:381)\n"
            "   by 0x1E9111: debugCommand (debug.c:569)\n",
        ),
        (
            "valgrind access width differs between builds",
            FailureType.VALGRIND,
            "Valgrind error: Invalid read of size 4\n"
            "   at 0x1E9111: lookupKey (db.c:120)\n",
            "Valgrind error: Invalid read of size 8\n"
            "   at 0x1E9111: lookupKey (db.c:120)\n",
        ),
        (
            "sanitizer leak size drift",
            FailureType.SANITIZER,
            "Sanitizer error: ERROR: LeakSanitizer: detected memory leaks\n"
            "    #1 0x55bc22 in debugCommand /home/runner/src/debug.c:569:9\n"
            "SUMMARY: AddressSanitizer: 41 byte(s) leaked in 1 allocation(s).\n",
            "Sanitizer error: ERROR: LeakSanitizer: detected memory leaks\n"
            "    #1 0x55bc22 in debugCommand /home/runner/src/debug.c:569:9\n"
            "SUMMARY: AddressSanitizer: 48 byte(s) leaked in 1 allocation(s).\n",
        ),
        (
            "asan address and registers differ every run",
            FailureType.SANITIZER,
            "Sanitizer error: ERROR: AddressSanitizer: heap-use-after-free on "
            "address 0x60300000eff8 at pc 0x55b1a2 bp 0x7ffd sp 0x7ffc\n"
            "    #1 0x55bb11 in lookupKey /home/runner/src/db.c:412\n",
            "Sanitizer error: ERROR: AddressSanitizer: heap-use-after-free on "
            "address 0x60300000abcd at pc 0x55b9f3 bp 0x7ff1 sp 0x7ff0\n"
            "    #1 0x55bb11 in lookupKey /home/runner/src/db.c:412\n",
        ),
        (
            "startup log reason carries a clock and a runner path",
            FailureType.STARTUP,
            "Can't start /home/runner/work/valkey/valkey/src/valkey-server\n"
            "CONFIGURATION:\nport 21111\nERROR:\n"
            "943:C 29 Jul 04:15:32.117 * Valkey is starting\n",
            "Can't start /Users/runner/work/valkey/valkey/src/valkey-server\n"
            "CONFIGURATION:\nport 22345\nERROR:\n"
            "987:C 30 Jul 11:02:07.882 * Valkey is starting\n",
        ),
        (
            "macos leaks pid drift",
            FailureType.MEMORY_LEAK,
            "Check for memory leaks (pid 9443) in tests/unit/dump.tcl\n"
            "Process 9443: 1 leak for 48 total leaked bytes.\n",
            "Check for memory leaks (pid 9871) in tests/unit/dump.tcl\n"
            "Process 9871: 1 leak for 48 total leaked bytes.\n",
        ),
    )

    @pytest.mark.parametrize(
        "label,failure_type,first,second",
        _RECURRENCES,
        ids=[case[0] for case in _RECURRENCES],
    )
    def test_equal_fingerprints_render_equal_titles(
        self, label: str, failure_type: FailureType, first: str, second: str,
    ) -> None:
        def failure(error: str) -> UniqueFailure:
            return UniqueFailure(
                test_name="", test_file="tests/unit/other.tcl",
                failure_type=failure_type, error=error,
                jobs=[JobReference(job="j", suite="s", url="u")],
            )

        f1, f2 = failure(first), failure(second)
        assert fingerprint_for(f1) == fingerprint_for(f2), (
            f"{label}: the two reports should be one bug"
        )
        assert title_for(f1) == title_for(f2), f"{label}: title churns"

    def test_title_stays_within_the_length_github_accepts(self) -> None:
        # A Tcl test description is unbounded, and GitHub truncates past 256,
        # which would break the publisher's exact-match title fallback.
        f = UniqueFailure(
            test_name="x" * 400, test_file="tests/unit/" + "y" * 200 + ".tcl",
            failure_type=FailureType.ASSERTION, error="boom",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        assert len(title_for(f)) <= 256

    def test_title_has_no_newlines(self) -> None:
        # GitHub rewrites a newline in a title, so the stored title would differ
        # from the rendered one and never match the fallback again.
        f = UniqueFailure(
            test_name="", test_file="tests/unit/other.tcl",
            failure_type=FailureType.EXCEPTION,
            error="Executing test client: boom\n    while executing\n\"$r read\"",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        assert "\n" not in title_for(f)


class TestStartupFailureTitle:
    """A startup blob's first line names only the executable, which is the
    same for every startup failure; the title must carry the reason after the
    ERROR: header so two causes are tellable apart in an issue list."""

    def _failure(self, reason: str) -> UniqueFailure:
        config = "\n".join(f"directive-{i} value-{i}" for i in range(40))
        error = (
            "Can't start /path/to/valkey-server\n"
            f"CONFIGURATION:\n{config}\nERROR:\n{reason}"
        )
        return UniqueFailure(
            test_name="", test_file="tests/unit/introspection.tcl",
            failure_type=FailureType.STARTUP, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )

    def test_title_names_the_reason(self) -> None:
        title = title_for(self._failure("Unable to bind unix socket: Permission denied"))
        assert "Unable to bind unix socket" in title

    def test_title_skips_fatal_banner(self) -> None:
        title = title_for(self._failure(
            "*** FATAL CONFIG FILE ERROR (Version 9.0.0) ***\n"
            "Bad directive or wrong number of arguments"
        ))
        assert "***" not in title
        assert "Bad directive" in title

    def test_different_causes_get_different_titles(self) -> None:
        t1 = title_for(self._failure("Unable to bind unix socket: Permission denied"))
        t2 = title_for(self._failure("Bad directive or wrong number of arguments"))
        assert t1 != t2

    def test_blob_without_error_section_falls_back(self) -> None:
        f = UniqueFailure(
            test_name="", test_file="",
            failure_type=FailureType.STARTUP,
            error="Can't start /path/to/valkey-server",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        assert "Can't start" in title_for(f)

    def test_title_survives_runner_status_tag_whitespace(self) -> None:
        """The runner's "[err]: " tag is stripped upstream and leaves a leading
        space. Without tolerating it the startup branch never fires and the
        title becomes the executable path truncated mid-word."""
        f = self._failure("Bad directive or wrong number of arguments")
        f.error = f" {f.error}"
        title = title_for(f)
        assert "Bad directive" in title
        assert "valkey-serve" not in title

    def test_title_skips_progress_and_position_lines(self) -> None:
        """The harness's "###" marker and the config loader's position/echo
        lines precede the reason but name no cause."""
        title = title_for(self._failure(
            "### Starting server for test \n\n"
            "*** FATAL CONFIG FILE ERROR (Version 9.0.0) ***\n"
            "Reading the configuration file, at line 30\n"
            ">>> 'invalid-config-key-that-does-not-exist bogus'\n"
            "Bad directive or wrong number of arguments"
        ))
        assert "Bad directive or wrong number of arguments" in title
        assert "###" not in title
        assert "at line" not in title

    def test_valgrind_wrapped_startup_matches_plain_startup(self) -> None:
        """Under valgrind the capture opens with the tool's own banner and
        interleaves ==PID== markers. It is the same config error, so it must
        not mint a second issue."""
        plain = self._failure(
            "*** FATAL CONFIG FILE ERROR (Version 9.0.0) ***\n"
            "Reading the configuration file, at line 30\n"
            "Bad directive or wrong number of arguments"
        )
        under_valgrind = self._failure(
            "### Starting server for test \n"
            "==6688== Memcheck, a memory error detector\n"
            "==6688== Using Valgrind-3.22.0 and LibVEX\n\n"
            "*** FATAL CONFIG FILE ERROR (Version 9.0.0) ***\n"
            "Reading the configuration file, at line 30\n"
            "Bad directive or wrong number of arguments\n"
            "==6688== HEAP SUMMARY:\n"
        )
        assert title_for(plain) == title_for(under_valgrind)
        assert normalize_error_identity(plain.error) == normalize_error_identity(
            under_valgrind.error
        )


class TestTraceTruncation:
    """GitHub rejects bodies over 65536 chars; oversized traces are capped
    keeping head (names the error) and tail (holds the summary totals)."""

    def _big_failure(self) -> UniqueFailure:
        error = "HEAD: first line names the error\n" + ("x" * 100 + "\n") * 1000 + "TAIL: summary totals"
        return UniqueFailure(
            test_name="", test_file="",
            failure_type=FailureType.VALGRIND, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )

    def test_body_stays_under_github_limit(self) -> None:
        body = _build_body(self._big_failure(), marker="<!-- m -->", occurrences=1)
        assert len(body) < 65536

    def test_truncation_keeps_head_and_tail_and_says_so(self) -> None:
        body = _build_body(self._big_failure(), marker="<!-- m -->", occurrences=1)
        assert "HEAD: first line names the error" in body
        assert "TAIL: summary totals" in body
        assert "trace truncated" in body

    def test_short_trace_untouched(self) -> None:
        f = UniqueFailure(
            test_name="", test_file="",
            failure_type=FailureType.VALGRIND, error="short trace",
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        body = _build_body(f, marker="<!-- m -->", occurrences=1)
        assert "trace truncated" not in body
        assert "short trace" in body

    def test_truncated_recurrence_stays_quiet(self) -> None:
        """The stored trace is the truncated form; the fresh full-length trace
        must compare equal to it or every recurrence posts a new-error comment."""
        f = self._big_failure()
        body = _build_body(f, marker="<!-- m -->", occurrences=1)
        renderer = renderer_for(self._big_failure())
        renderer.merge_environments(body)
        comment = renderer.render("<!-- m -->", 2).comment
        assert "New error stack trace" not in comment


class TestExceptionTitle:
    """Uncaught test-client exceptions arrive wrapped in the runner's
    "Executing test client: <message>" prefix. The title must surface the
    message, not the wrapper."""

    def _failure(self, error: str) -> UniqueFailure:
        return UniqueFailure(
            test_name="", test_file="tests/unit/networking.tcl",
            failure_type=FailureType.EXCEPTION, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )

    def test_strips_executing_test_client_prefix(self) -> None:
        error = (
            " Executing test client: Intentional runtime exception for detector testing.\n"
            " in error at tests/unit/networking.tcl:12\n"
            " in test at tests/support/test.tcl:262\n"
        )
        title = title_for(self._failure(error))
        assert "Executing test client:" not in title
        assert "Intentional runtime exception for detector testing." in title
        assert title.startswith("[TEST-FAILURE] ")

    def test_title_stable_across_volatile_ports_and_pids(self) -> None:
        """The fingerprint scrubs ports/PIDs, so one recurring exception keeps
        one issue; the title must scrub them too or the publisher rewrites it
        with the new port on every recurrence."""
        template = " Executing test client: couldn't open socket: connection refused, port {port}\n"
        t1 = title_for(self._failure(template.format(port=21079)))
        t2 = title_for(self._failure(template.format(port=21987)))
        assert t1 == t2
        assert "21079" not in t1

_ASAN_USE_AFTER_FREE = (
    " Sanitizer error: ==1234==ERROR: AddressSanitizer: heap-use-after-free on "
    "address 0x60300000eff8 at pc 0x55b bp 0x7ff sp 0x7ff\n"
    "READ of size 4 at 0x60300000eff8 thread T0\n"
    "    #0 0x55ba1234 in lookupKey /home/runner/work/valkey/valkey/src/db.c:120:9\n"
    "    #1 0x55ba5678 in getCommand /home/runner/work/valkey/valkey/src/t_string.c:75:5\n"
)

_UBSAN_RUNTIME_ERROR = (
    " Sanitizer error: src/bitops.c:88:12: runtime error: signed integer overflow\n"
    "    #0 0x55ba9999 in bitcountCommand "
    "/home/runner/work/valkey/valkey/src/bitops.c:88:12\n"
)


# The shape /usr/bin/leaks emits when the server runs with MallocStackLogging
# enabled, captured from a macos-latest runner. The heading quotes the same
# "ROOT LEAK: <...>" text as the allocation line below it, so a pattern that
# does not anchor on the allocation line's leading count reads the heading and
# carries its trailing quote into the identity and the title.
_SYMBOLICATED_LEAKS_REPORT = """\
leaks Report Version: 4.0, multi-line stacks
Process 17415: 14782 nodes malloced for 1552 KB
Process 17415: 1 leak for 64 total leaked bytes.

STACK OF 1 INSTANCE OF 'ROOT LEAK: <malloc in _sdsnewlen>':
3   valkey-server                         0x100416628 call + 992
2   valkey-server                         0x10031ac18 debugCommand + 2592
1   valkey-server                         0x1003ebe9c _sdsnewlen + 168
0   libsystem_malloc.dylib                0x18b4a4178 _malloc_zone_malloc + 152
====
    1 (64 bytes) ROOT LEAK: <malloc in _sdsnewlen 0xa1cc41080> [64]
"""


class TestSymbolicatedMacosLeaksReport:
    """A symbolicated macOS leaks report keys on its allocation site.

    Verified against a macos-latest runner: with MallocStackLogging enabled the
    report names the leaking function, which is what lets one leak be one issue
    however many test files expose it. Without it the roots are bare addresses
    and the file becomes the identity instead.
    """

    def _failure(self, test_file: str) -> UniqueFailure:
        return UniqueFailure(
            test_name="", test_file=test_file,
            failure_type=FailureType.MEMORY_LEAK,
            error=(
                f"Check for memory leaks (pid 17415) in {test_file}\n"
                f"{_SYMBOLICATED_LEAKS_REPORT}"
            ),
            jobs=[JobReference(job="test-macos-latest", suite="valkey", url="u")],
        )

    def test_the_title_names_the_leaking_function(self) -> None:
        assert title_for(self._failure("tests/unit/other.tcl")) == (
            "[TEST-FAILURE] Leaked memory in _sdsnewlen"
        )

    def test_the_heading_does_not_leak_into_the_title(self) -> None:
        """The heading ends in "'>':", which an unanchored match carried over."""
        title = title_for(self._failure("tests/unit/other.tcl"))
        assert ">" not in title
        assert "'" not in title
        assert "STACK OF" not in title

    def test_one_leak_is_one_issue_across_test_files(self) -> None:
        """The file is not identity once the report names a site, so a leak in
        shared code does not become an issue per file that exposes it."""
        first = self._failure("tests/unit/dump.tcl")
        second = self._failure("tests/unit/geo.tcl")
        assert fingerprint_for(first) == fingerprint_for(second)
        assert title_for(first) == title_for(second)
        assert "tests/unit" not in title_for(first)

    def test_the_identity_names_the_site_once(self) -> None:
        identity = normalize_error_identity(
            self._failure("tests/unit/other.tcl").error
        )
        assert identity.count("_sdsnewlen") == 1
