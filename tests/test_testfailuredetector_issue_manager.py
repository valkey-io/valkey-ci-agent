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
        MEMORY_ERROR_NAMESPACE,
        _bound_backtick_runs,
        _build_body,
        _build_title,
        _extract_environments_from_body,
        _extract_error_from_body,
        _extract_errors_from_body,
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
        _merge_same_fingerprint_failures,
        process_failures,
    )
    from scripts.test_failure_detector.parse_failures import (
        FailureType,
        JobReference,
        UniqueFailure,
        cross_tool_anchor,
        error_class,
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


def _make_valgrind_failure(size: str, job: str) -> UniqueFailure:
    """A nameless valgrind leak whose byte count varies run to run.

    The parser keeps the two size variants as distinct UniqueFailures while
    the fingerprint normalizes digits away, so the pair collides on one
    fingerprint. `Invalid read of size N` is used because the count scrubber
    only strips bytes/blocks phrases, leaving the digit for the fingerprint
    normalizer to collapse.
    """
    return UniqueFailure(
        test_name="", test_file="tests/unit/other.tcl",
        failure_type=FailureType.VALGRIND,
        error=(
            f"==1== Invalid read of size {size}\n"
            "==1==    at 0xA: dictResize (dict.c:100)"
        ),
        jobs=[JobReference(job=job, suite="s", url=f"https://ci/{job}")],
    )


class TestPublisherReuse:
    """The publisher caches the issue listing for its lifetime, so failures
    sharing a marker namespace must share one publisher. A fresh publisher per
    failure re-lists every issue in the repository for every failure.
    """

    def test_one_listing_per_namespace_not_per_failure(self) -> None:
        listings = []

        mock_repo = MagicMock()
        mock_repo.get_issues.side_effect = lambda **kw: listings.append(kw) or []
        created = MagicMock()
        created.number = 1
        created.html_url = "https://x/1"
        mock_repo.create_issue.return_value = created
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        failures = [
            _make_failure(test_name=f"test {i}", test_file="t.tcl")
            for i in range(6)
        ]
        result = process_failures(mock_gh, "o/r", failures, run_id=1)

        assert result["created"] == 6
        # Two listings for the one shared publisher: open, and recently closed.
        assert len(listings) == 2

    def test_each_namespace_gets_its_own_publisher(self) -> None:
        listings = []

        mock_repo = MagicMock()
        mock_repo.get_issues.side_effect = lambda **kw: listings.append(kw) or []
        created = MagicMock()
        created.number = 1
        created.html_url = "https://x/1"
        mock_repo.create_issue.return_value = created
        mock_gh = MagicMock()
        mock_gh.get_repo.return_value = mock_repo

        failures = [
            UniqueFailure(
                test_name=f"test {ftype.value}", test_file="t.tcl",
                failure_type=ftype, error="boom",
                jobs=[JobReference(job="j", suite="s", url="u")],
            )
            for ftype in (
                FailureType.ASSERTION, FailureType.TIMEOUT, FailureType.UNITTEST,
            )
        ]
        result = process_failures(mock_gh, "o/r", failures, run_id=1)

        assert result["created"] == 3
        # Three distinct namespaces, two listings each.
        assert len(listings) == 6


class TestMergeSameFingerprintFailures:
    """Same-run failures that hash to one fingerprint must publish as one
    issue carrying every job, not race for it (the run-id idempotency key
    rejects the loser and its environments/CI links silently vanish, which is
    why issue #91 listed one environment though both valgrind jobs failed)."""

    def test_same_fingerprint_failures_merge_jobs(self) -> None:
        f1 = _make_valgrind_failure("4", "valgrind-ubuntu")
        f2 = _make_valgrind_failure("8", "valgrind-arm64")
        assert fingerprint_for(f1) == fingerprint_for(f2)

        merged = _merge_same_fingerprint_failures([f1, f2])

        assert len(merged) == 1
        assert {j.job for j in merged[0].jobs} == {"valgrind-ubuntu", "valgrind-arm64"}

    def test_merge_does_not_duplicate_shared_job(self) -> None:
        f1 = _make_valgrind_failure("4", "valgrind-ubuntu")
        f2 = _make_valgrind_failure("8", "valgrind-ubuntu")

        merged = _merge_same_fingerprint_failures([f1, f2])

        assert len(merged) == 1
        assert [j.job for j in merged[0].jobs] == ["valgrind-ubuntu"]

    def test_distinct_fingerprints_stay_separate(self) -> None:
        failures = [
            _make_failure(test_name="a"),
            _make_failure(test_name="b"),
        ]
        assert _merge_same_fingerprint_failures(failures) == failures

    @patch("scripts.test_failure_detector.manage_issues.IssueDedupPublisher")
    def test_process_failures_publishes_colliding_pair_once(self, mock_publisher_cls) -> None:
        """End to end: the colliding pair reaches upsert as one failure whose
        render carries both environments, instead of a second upsert that the
        idempotency key would reject."""
        publisher = mock_publisher_cls.return_value
        publisher.upsert.return_value = ("created", "https://x/issues/91")

        f1 = _make_valgrind_failure("4", "valgrind-ubuntu")
        f2 = _make_valgrind_failure("8", "valgrind-arm64")
        result = process_failures(
            MagicMock(), "valkey-io/valkey", [f1, f2], run_id=29944432899,
        )

        assert result == {
            "created": 1, "updated": 0, "skipped": 0, "skipped_closed": 0, "errors": 0,
        }
        assert publisher.upsert.call_count == 1
        body = publisher.upsert.call_args.kwargs["render"]("<!-- m -->", 1).body
        assert "`valgrind-ubuntu`" in body
        assert "`valgrind-arm64`" in body


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


class TestValgrindBannerTitle:
    """The valgrind runner prepends a banner ('Valgrind error: Memcheck, a
    memory error detector') that every valgrind issue would share. The title
    must surface the real diagnostic line instead."""

    def test_title_uses_diagnostic_not_banner(self) -> None:
        error = (
            " Valgrind error: ==6554== Memcheck, a memory error detector\n"
            "==6554== Copyright (C) 2002-2022\n"
            "==6554== \n"
            "==6554== HEAP SUMMARY:\n"
            "==6554== 49 bytes in 1 blocks are definitely lost in loss record 900\n"
            "==6554==    at 0x4846828: malloc (...)\n"
        )
        f = UniqueFailure(
            test_name="", test_file="tests/unit/other.tcl",
            failure_type=FailureType.VALGRIND, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        title = title_for(f)
        assert "Memcheck" not in title
        # No source frame in this trace (only the malloc interceptor), so the
        # title is kind + size without a site.
        assert "Definitely lost" in title

    def test_title_names_leaking_code_path_not_the_allocator(self) -> None:
        """Valgrind resolves its malloc interceptor to a source file inside the
        preload library, so a file-only plumbing check lets it through and every
        leak title reads "in malloc". The title must name the first frame that
        identifies the leaking code path, matching the identity's anchor.
        """
        error = (
            " Valgrind error: ==6554== Memcheck, a memory error detector\n"
            "==6554== 41 bytes in 1 blocks are definitely lost in loss record 900 of 1,109\n"
            "==6554==    at 0x4846828: malloc (vg_replace_malloc.c:307)\n"
            "==6554==    by 0x3189FB: ztrymalloc_usable_internal (zmalloc.c:172)\n"
            "==6554==    by 0x29072A: sdsdup (sds.c:190)\n"
            "==6554==    by 0x1E8076: debugCommand (debug.c:569)\n"
        )
        f = UniqueFailure(
            test_name="", test_file="tests/unit/other.tcl",
            failure_type=FailureType.VALGRIND, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        title = title_for(f)
        assert "debugCommand (debug.c:569)" in title
        assert "malloc" not in title
        assert "zmalloc.c" not in title

    def test_two_leaks_sharing_an_allocator_get_distinct_titles(self) -> None:
        """Two leaks whose stacks differ only past the shared allocator frames
        must not collapse to one title, or an issue list cannot tell them apart.
        """
        def leak(site_func: str, site_loc: str) -> UniqueFailure:
            error = (
                "==6554== 41 bytes in 1 blocks are definitely lost in loss record 9 of 99\n"
                "==6554==    at 0x4846828: malloc (vg_replace_malloc.c:307)\n"
                "==6554==    by 0x3189FB: ztrymalloc_usable_internal (zmalloc.c:172)\n"
                f"==6554==    by 0x1E8076: {site_func} ({site_loc})\n"
            )
            return UniqueFailure(
                test_name="", test_file="tests/unit/other.tcl",
                failure_type=FailureType.VALGRIND, error=error,
                jobs=[JobReference(job="j", suite="s", url="u")],
            )
        first = leak("debugCommand", "debug.c:569")
        second = leak("clusterCommand", "cluster.c:123")
        assert title_for(first) != title_for(second)
        assert fingerprint_for(first) != fingerprint_for(second)

    def test_sanitizer_banner_stripped(self) -> None:
        error = (
            " Sanitizer error: \n"
            "==12617==ERROR: LeakSanitizer: detected memory leaks\n"
            "Direct leak of 41 byte(s)\n"
        )
        f = UniqueFailure(
            test_name="", test_file="tests/unit/foo.tcl",
            failure_type=FailureType.SANITIZER, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        title = title_for(f)
        assert "Sanitizer error:" not in title
        assert "detected memory leaks" in title

    def test_sanitizer_title_stable_across_test_file(self) -> None:
        """One sanitizer bug detected under different test files across runs
        keeps one title, matching its file-independent fingerprint."""
        error = (
            "==1==ERROR: AddressSanitizer: heap-use-after-free\n"
            "    #1 0x55 in freeStringObject object.c:400\n"
        )
        def san(test_file: str) -> UniqueFailure:
            return UniqueFailure(
                test_name="", test_file=test_file,
                failure_type=FailureType.SANITIZER, error=error,
                jobs=[JobReference(job="j", suite="s", url="u")],
            )
        first = san("tests/unit/type/string.tcl")
        second = san("tests/unit/expire.tcl")
        assert fingerprint_for(first) == fingerprint_for(second)
        assert title_for(first) == title_for(second)

    def test_sanitizer_title_scrubs_volatile_address(self) -> None:
        """The address and registers in an AddressSanitizer diagnostic line
        drift every run; the title drops them so it does not change while the
        fingerprint stays stable."""
        def san(address: str, pc: str) -> UniqueFailure:
            error = (
                f"==1==ERROR: AddressSanitizer: heap-use-after-free on address "
                f"{address} at pc {pc} bp 0x7ffd sp 0x7ffd\n"
                "    #1 0x55 in freeStringObject object.c:400\n"
            )
            return UniqueFailure(
                test_name="", test_file="tests/unit/expire.tcl",
                failure_type=FailureType.SANITIZER, error=error,
                jobs=[JobReference(job="j", suite="s", url="u")],
            )
        title = title_for(san("0x60200000eff0", "0x000000abcdef"))
        assert "0x" not in title
        assert "heap-use-after-free" in title
        assert title == title_for(san("0x602000001234", "0x000000fedcba"))

    def test_error_severity_tag_stripped_tool_name_kept(self) -> None:
        """The 'ERROR:' tag is dropped from titles (it says nothing) but the
        tool name after it stays so maintainers see which detector fired."""
        error = (
            "==107611==ERROR: LeakSanitizer: detected memory leaks\n"
            "Direct leak of 128 byte(s) in 4 object(s) allocated from:\n"
        )
        f = UniqueFailure(
            test_name="", test_file="tests/unit/fuzzer.tcl",
            failure_type=FailureType.SANITIZER, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        title = title_for(f)
        assert "ERROR:" not in title
        assert "LeakSanitizer: detected memory leaks" in title

    def test_sanitizer_leak_title_shows_size_and_site(self) -> None:
        """When the report has the "SUMMARY: AddressSanitizer: N byte(s) leaked"
        line, the title leads with the size and names the leaking code path
        instead of the magnitude-free "detected memory leaks" banner."""
        error = (
            " Sanitizer error: \n"
            "==6366==ERROR: LeakSanitizer: detected memory leaks\n"
            "Direct leak of 41 byte(s) in 1 object(s) allocated from:\n"
            "    #0 0x55ba0d2fbe33 in malloc (src/valkey-server+0x20de33)\n"
            "    #1 0x55ba0d702bb3 in ztrymalloc_usable_internal src/zmalloc.c:172:17\n"
            "    #2 0x55ba0d5f5c07 in _sdsnewlen src/sds.c:102:22\n"
            "    #3 0x55ba0d435e7f in debugCommand src/debug.c:569:9\n"
            "SUMMARY: AddressSanitizer: 41 byte(s) leaked in 1 allocation(s).\n"
        )
        f = UniqueFailure(
            test_name="", test_file="tests/unit/other.tcl",
            failure_type=FailureType.SANITIZER, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        title = title_for(f)
        assert "detected memory leaks" not in title
        assert "Leaked memory" in title
        # The site skips the allocator wrappers (zmalloc.c/sds.c) and names the
        # code path that leaked.
        assert "debugCommand (debug.c:569)" in title

    def test_sanitizer_leak_title_survives_missing_frames(self) -> None:
        """A summary line with no source frames still yields a size-based
        title rather than falling back to the banner."""
        error = (
            "==1==ERROR: LeakSanitizer: detected memory leaks\n"
            "Direct leak of 96 byte(s) in 2 object(s) allocated from:\n"
            "    #0 0x1 in malloc (src/valkey-server+0x1)\n"
            "SUMMARY: AddressSanitizer: 96 byte(s) leaked in 2 allocation(s).\n"
        )
        f = UniqueFailure(
            test_name="", test_file="tests/unit/foo.tcl",
            failure_type=FailureType.SANITIZER, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        assert "Leaked memory" in title_for(f)

    def test_sanitizer_leak_site_skips_interceptor_frame(self) -> None:
        """GCC's ASan interceptor frame carries a real file:line into the
        sanitizer's own sources (asan_malloc_linux.cpp), so it passes the
        source-frame regex; the site must skip it like the allocator
        wrappers, or every leak titles as 'in malloc'."""
        error = (
            " Sanitizer error: \n"
            "==3021==ERROR: LeakSanitizer: detected memory leaks\n"
            "Direct leak of 49 byte(s) in 1 object(s) allocated from:\n"
            "    #0 0x7f8a4a2b476f in malloc ../../../../src/libsanitizer/asan/asan_malloc_linux.cpp:69\n"
            "    #1 0x55c908b21a02 in ztrymalloc_usable_internal /home/runner/work/valkey/valkey/src/zmalloc.c:172\n"
            "    #2 0x55c908d1e222 in debugCommand /home/runner/work/valkey/valkey/src/debug.c:569\n"
            "SUMMARY: AddressSanitizer: 49 byte(s) leaked in 1 allocation(s).\n"
        )
        f = UniqueFailure(
            test_name="", test_file="",
            failure_type=FailureType.SANITIZER, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )
        title = title_for(f)
        assert "asan_malloc_linux" not in title
        assert "debugCommand (debug.c:569)" in title


class TestValgrindRecurrenceStaysQuiet:
    """A recurrence of the same valgrind leak differs only in ==PID== markers,
    sizes, and loss-record coordinates. The fingerprint calls it the same bug,
    so the recurrence comment must not flag it as a new error."""

    _RUN1 = (
        "Valgrind error: ==12345== Memcheck, a memory error detector\n"
        "==12345== 49 bytes in 1 blocks are definitely lost in loss record 900 of 1,109\n"
        "==12345==    by 0x1E8076: ztrymalloc_usable_internal (zmalloc.c:172)\n"
        "==12345==    by 0x3CD456: debugCommand (debug.c:569)\n"
    )

    def _failure(self, error: str) -> UniqueFailure:
        return UniqueFailure(
            test_name="", test_file="",
            failure_type=FailureType.VALGRIND, error=error,
            jobs=[JobReference(job="j", suite="s", url="u")],
        )

    def _recur(self, old_error: str, new_error: str) -> str:
        body = _build_body(self._failure(old_error), marker="<!-- m -->", occurrences=1)
        renderer = renderer_for(self._failure(new_error))
        renderer.merge_environments(body)
        return renderer.render("<!-- m -->", 2).comment

    def test_same_leak_new_pid_and_size_stays_quiet(self) -> None:
        rerun = (
            self._RUN1.replace("12345", "999")
            .replace("49 bytes", "41 bytes")
            .replace("900 of 1,109", "850 of 1,050")
        )
        assert "New error stack trace" not in self._recur(self._RUN1, rerun)

    def test_different_allocation_site_is_flagged(self) -> None:
        other = self._RUN1.replace(
            "debugCommand (debug.c:569)", "clusterCommand (cluster.c:1201)",
        )
        assert "New error stack trace" in self._recur(self._RUN1, other)

    def test_heap_usage_totals_stay_quiet(self) -> None:
        """Valgrind's heap totals count every allocation the server made, so
        they drift on every run of one leak. The comparison must scrub them or
        each recurrence reposts the whole trace.
        """
        with_totals = self._RUN1 + (
            "==12345==   total heap usage: 18,053 allocs, 4,513 frees, "
            "1,383,828 bytes allocated\n"
        )
        rerun = with_totals.replace("18,053 allocs, 4,513 frees", "18,066 allocs, 4,517 frees")
        assert "New error stack trace" not in self._recur(with_totals, rerun)

    def test_sanitizer_build_id_stays_quiet(self) -> None:
        """A sanitizer frame's BuildId changes whenever the binary is rebuilt,
        which is every CI run, so it is not evidence of a different bug.
        """
        def report(build_id: str) -> str:
            return (
                "==1==ERROR: LeakSanitizer: detected memory leaks\n"
                "Direct leak of 41 byte(s) in 1 object(s) allocated from:\n"
                f"    #0 0x55 in malloc (/src/valkey-server+0x20de33) (BuildId: {build_id})\n"
                "    #4 0x55 in debugCommand /src/debug.c:569:9\n"
            )
        assert "New error stack trace" not in self._recur(
            report("eaa319adda1cfec4d818"), report("2bf960fbb10e52be190f"),
        )

    def test_macos_leaks_footprint_stays_quiet(self) -> None:
        """The leaks report's footprint measures the live server rather than
        the leak, so it varies run to run for one leak.
        """
        def report(footprint: str) -> str:
            return (
                "Check for memory leaks in tests/unit/other.tcl\n"
                f"Physical footprint:         {footprint}\n"
                f"Physical footprint (peak):  {footprint}\n"
                "Process 9761: 1 leak for 48 total leaked bytes.\n"
                "    1 (48 bytes) ROOT LEAK: <malloc in sdsnewlen>\n"
            )
        assert "New error stack trace" not in self._recur(
            report("2865K"), report("2801K"),
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


def _macos_leaks_error(pid: int, leaks: int, leaked_bytes: int, address: str) -> str:
    """A macOS /usr/bin/leaks failure blob as recorded in the artifact
    (the real shape emitted by /usr/bin/leaks on the macOS jobs)."""
    return (
        f" Check for memory leaks (pid {pid}) in tests/unit/multi.tcl\n"
        f"Expected '*0 leaks*' to equal or match 'Process:         valkey-server [{pid}]\n"
        "Path:            /Users/USER/*/valkey-server\n"
        "Load Address:    0x102610000\n"
        "Platform:        macOS\n"
        "Analysis Tool:   /usr/bin/leaks\n"
        "----\n"
        "leaks Report Version: 4.0\n"
        f"Process {pid}: 14810 nodes malloced for 1403 KB\n"
        f"Process {pid}: {leaks} leak for {leaked_bytes} total leaked bytes.\n"
        "\n"
        f"    {leaks} ({leaked_bytes} bytes) ROOT LEAK: {address} [{leaked_bytes}]\n"
        "\n"
        "child process exited abnormally'\n"
    )


class TestMacosLeaksTitle:
    """macOS /usr/bin/leaks failures (the memory-leak type). Their first line
    is the Tcl test name with a volatile PID; the title must surface the
    report's totals line instead."""

    def _failure(self, error: str) -> UniqueFailure:
        return UniqueFailure(
            test_name="", test_file="tests/unit/multi.tcl",
            failure_type=FailureType.MEMORY_LEAK, error=error,
            jobs=[JobReference(job="test-macos-latest", suite="valkey", url="u")],
        )

    def test_title_names_the_leak_without_its_magnitude(self) -> None:
        # The totals line is the blob's payload, but its counts drift between
        # runs of one leak, so the title names the leak and leaves them to the
        # trace. This blob's roots are bare addresses, so it has no site to
        # name and falls back to the test file, which is what its identity
        # keys on and the only thing separating two such leaks.
        f = self._failure(_macos_leaks_error(9443, 1, 48, "0x953074d20"))
        assert title_for(f) == "[TEST-FAILURE] Leaked memory in tests/unit/multi.tcl"

    def test_title_omits_the_test_file(self) -> None:
        """A leak in shared code is reported after whichever test file exposed
        it, and the fingerprint keys on the leak site rather than the file. If
        the title carried the file, one issue's title would be rewritten each
        time the same leak surfaced elsewhere."""
        site = "<malloc in sdsnewlen 0x953074d20>"
        under_multi = UniqueFailure(
            test_name="", test_file="tests/unit/multi.tcl",
            failure_type=FailureType.MEMORY_LEAK,
            error=_macos_leaks_error(9443, 1, 48, site),
            jobs=[JobReference(job="test-macos-latest", suite="valkey", url="u")],
        )
        under_expire = UniqueFailure(
            test_name="", test_file="tests/unit/expire.tcl",
            failure_type=FailureType.MEMORY_LEAK,
            error=_macos_leaks_error(7211, 1, 48, site),
            jobs=[JobReference(job="test-macos-latest", suite="valkey", url="u")],
        )
        # One fingerprint, so one issue; therefore one stable title.
        assert fingerprint_for(under_multi) == fingerprint_for(under_expire)
        assert title_for(under_multi) == title_for(under_expire)
        assert "multi.tcl" not in title_for(under_multi)

    def test_title_stable_across_pids_and_addresses(self) -> None:
        t1 = title_for(self._failure(_macos_leaks_error(9443, 1, 48, "0x953074d20")))
        t2 = title_for(self._failure(_macos_leaks_error(7211, 1, 48, "0x9dd024100")))
        assert t1 == t2
        assert "9443" not in t1
        assert "pid" not in t1.lower()

    def test_title_stable_across_leak_magnitudes(self) -> None:
        # These two share a fingerprint (see the next test), so they are one
        # issue. Carrying their magnitudes would retitle it on each recurrence.
        f1 = self._failure(_macos_leaks_error(9443, 1, 48, "0x953074d20"))
        f2 = self._failure(_macos_leaks_error(9443, 12, 4096, "0x953074d20"))
        assert fingerprint_for(f1) == fingerprint_for(f2)
        assert title_for(f1) == title_for(f2)

    def test_unsymbolicated_leaks_in_one_file_share_a_fingerprint(self) -> None:
        """Documents a known granularity limit: with bare-address ROOT LEAK
        lines (no symbol names), the blob carries no allocation-site signal,
        so two different leaks in the same test file collapse into one issue
        (see fingerprint_for). Symbolicated roots stay distinct via the
        root-site anchor in normalize_error_identity."""
        f1 = self._failure(_macos_leaks_error(9443, 1, 48, "0x953074d20"))
        f2 = self._failure(_macos_leaks_error(9443, 12, 4096, "0x9dd024100"))
        assert fingerprint_for(f1) == fingerprint_for(f2)

    def test_symbolicated_leaks_in_one_file_stay_distinct(self) -> None:
        f1 = self._failure(
            _macos_leaks_error(9443, 1, 48, "<malloc in sdsnewlen 0x953074d20>")
        )
        f2 = self._failure(
            _macos_leaks_error(9443, 1, 48, "<malloc in clusterInit 0x9dd024100>")
        )
        assert fingerprint_for(f1) != fingerprint_for(f2)


# --- Cross-tool merging of valgrind and sanitizer reports ---


def _vg(body: str) -> str:
    """A valgrind report: runner wrapper, Memcheck banner, then *body*."""
    return (
        " Valgrind error: ==6554== Memcheck, a memory error detector\n"
        "==6554== Copyright (C) 2002-2022, by Julian Seward et al.\n"
        f"{body}"
    )


_VG_USE_AFTER_FREE = _vg(
    "==6554== Invalid read of size 4\n"
    "==6554==    at 0x1E8076: lookupKey (db.c:120)\n"
    "==6554==    by 0x1E9000: getCommand (t_string.c:75)\n"
    "==6554==  Address 0x5a1b2c0 is 8 bytes inside a block of size 32 free'd\n"
    "==6554==    at 0x484BB2F: free (vg_replace_malloc.c:872)\n"
)

_ASAN_USE_AFTER_FREE = (
    " Sanitizer error: ==1234==ERROR: AddressSanitizer: heap-use-after-free on "
    "address 0x60300000eff8 at pc 0x55b bp 0x7ff sp 0x7ff\n"
    "READ of size 4 at 0x60300000eff8 thread T0\n"
    "    #0 0x55ba1234 in lookupKey /home/runner/work/valkey/valkey/src/db.c:120:9\n"
    "    #1 0x55ba5678 in getCommand /home/runner/work/valkey/valkey/src/t_string.c:75:5\n"
)

# A buffer overflow at the same site as the use-after-free above. Valgrind words
# the offending access identically ("Invalid read of size 4") for both and only
# the address description tells them apart.
_VG_BUFFER_OVERFLOW = _vg(
    "==6554== Invalid read of size 4\n"
    "==6554==    at 0x1E8076: lookupKey (db.c:120)\n"
    "==6554==    by 0x1E9000: getCommand (t_string.c:75)\n"
    "==6554==  Address 0x5a1b2c0 is 4 bytes after a block of size 32 alloc'd\n"
    "==6554==    at 0x4846828: malloc (vg_replace_malloc.c:381)\n"
)

_VG_LEAK_SAME_SITE = _vg(
    "==6554== 49 bytes in 1 blocks are definitely lost in loss record 900 of 1,109\n"
    "==6554==    at 0x4846828: malloc (vg_replace_malloc.c:381)\n"
    "==6554==    by 0x1E8076: lookupKey (db.c:120)\n"
)

_UBSAN_RUNTIME_ERROR = (
    " Sanitizer error: src/bitops.c:88:12: runtime error: signed integer overflow\n"
    "    #0 0x55ba9999 in bitcountCommand "
    "/home/runner/work/valkey/valkey/src/bitops.c:88:12\n"
)


def _memory_failure(
    failure_type: FailureType,
    error: str,
    job: str,
    test_file: str = "tests/unit/other.tcl",
) -> UniqueFailure:
    return UniqueFailure(
        test_name="", test_file=test_file, failure_type=failure_type, error=error,
        jobs=[JobReference(job=job, suite="valkey", url=f"https://x/{job}")],
    )


class TestCrossToolErrorClass:
    """The coarse class each tool's vocabulary maps onto."""

    def test_valgrind_and_sanitizer_agree_on_use_after_free(self) -> None:
        assert (
            error_class(_VG_USE_AFTER_FREE, FailureType.VALGRIND)
            == error_class(_ASAN_USE_AFTER_FREE, FailureType.SANITIZER)
            == "use-after-free"
        )

    def test_valgrind_access_class_comes_from_address_description(self) -> None:
        """Valgrind words the access line identically for a use-after-free and
        an overflow, so the class must come from the address description that
        follows it, not the access line."""
        assert error_class(_VG_USE_AFTER_FREE, FailureType.VALGRIND) == "use-after-free"
        assert error_class(_VG_BUFFER_OVERFLOW, FailureType.VALGRIND) == "buffer-overflow"

    def test_leaks_classify_as_leak_in_both_tools(self) -> None:
        """Both tools' leak vocabularies map onto one class, so a leak the two
        describe differently can still merge. The valgrind input carries the
        Memcheck banner and the wrapper prefix, so the class is read from the
        loss-record line rather than the report's first line."""
        asan_leak = (
            " Sanitizer error: ==1==ERROR: LeakSanitizer: detected memory leaks\n"
            "Direct leak of 41 byte(s) in 1 object(s) allocated from:\n"
            "    #0 0x55b in malloc\n"
            "    #1 0x55c in lookupKey /src/db.c:120:9\n"
        )
        assert error_class(_VG_LEAK_SAME_SITE, FailureType.VALGRIND) == "leak"
        assert error_class(asan_leak, FailureType.SANITIZER) == "leak"

    def test_ubsan_runtime_error(self) -> None:
        assert error_class(_UBSAN_RUNTIME_ERROR, FailureType.SANITIZER) == "runtime-error"

    def test_unrecognized_vocabulary_is_unclassified(self) -> None:
        """None, not a catch-all: an unclassifiable report must stay on its
        per-tool identity rather than merge on a guess."""
        assert error_class(_vg("==6554== something we do not know\n"),
                           FailureType.VALGRIND) is None

    def test_only_valgrind_and_sanitizer_are_classified(self) -> None:
        for failure_type in FailureType:
            if failure_type in (FailureType.VALGRIND, FailureType.SANITIZER):
                continue
            assert error_class(_ASAN_USE_AFTER_FREE, failure_type) is None


class TestCrossToolMerge:
    """One bug both tools caught resolves to one issue."""

    def test_same_bug_shares_namespace_and_fingerprint(self) -> None:
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        assert marker_namespace_for(vg) == marker_namespace_for(asan)
        assert marker_namespace_for(vg) == MEMORY_ERROR_NAMESPACE
        assert fingerprint_for(vg) == fingerprint_for(asan)

    def test_merge_keeps_both_traces_and_both_jobs(self) -> None:
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        merged = _merge_same_fingerprint_failures([vg, asan])
        assert len(merged) == 1
        survivor = merged[0]
        # The survivor is chosen by type, not by artifact order, so the absorbed
        # trace is the valgrind one regardless of which job came first.
        assert survivor.failure_type == FailureType.SANITIZER
        assert [label for label, _ in survivor.extra_traces] == ["Valgrind"]
        assert survivor.extra_traces[0][1] == _VG_USE_AFTER_FREE
        assert {j.job for j in survivor.jobs} == {"test-valgrind", "test-sanitizer-address"}

    def test_merge_is_independent_of_processing_order(self) -> None:
        def merge(order: list[UniqueFailure]) -> UniqueFailure:
            return _merge_same_fingerprint_failures(order)[0]

        def pair() -> tuple[UniqueFailure, UniqueFailure]:
            return (
                _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind"),
                _memory_failure(
                    FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
                ),
            )

        vg, asan = pair()
        forward = merge([vg, asan])
        vg2, asan2 = pair()
        reverse = merge([asan2, vg2])
        # The published title comes from the survivor and the publisher
        # re-titles on every update, so an order-dependent survivor made one
        # issue alternate between the two tools' wording run to run.
        assert title_for(forward) == title_for(reverse)
        assert forward.failure_type == reverse.failure_type
        assert (
            [label for label, _ in forward.extra_traces]
            == [label for label, _ in reverse.extra_traces]
        )
        assert {j.job for j in forward.jobs} == {j.job for j in reverse.jobs}

    def test_same_tool_twice_does_not_duplicate_the_trace(self) -> None:
        """Two failures of one type that hash together are the same tool on the
        same bug; a second near-identical trace adds nothing."""
        a = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        b = _memory_failure(
            FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind-no-malloc-usable-size",
        )
        merged = _merge_same_fingerprint_failures([a, b])
        assert len(merged) == 1
        assert merged[0].extra_traces == []
        assert len(merged[0].jobs) == 2


class TestCrossToolSeparation:
    """Everything not confidently the same bug must stay a separate issue.

    These are the correctness cases for the cross-tool identity: a missed merge
    only costs a duplicate issue, while a wrong merge hides one bug inside
    another's issue.
    """

    def test_same_site_different_class_stays_separate(self) -> None:
        """A leak and a use-after-free reported at one allocation site are two
        bugs, so the anchor alone cannot be the identity."""
        leak = _memory_failure(FailureType.VALGRIND, _VG_LEAK_SAME_SITE, "test-valgrind")
        uaf = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        assert fingerprint_for(leak) != fingerprint_for(uaf)

    def test_use_after_free_and_overflow_stay_separate(self) -> None:
        uaf = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        overflow = _memory_failure(
            FailureType.VALGRIND, _VG_BUFFER_OVERFLOW, "test-valgrind",
        )
        assert fingerprint_for(uaf) != fingerprint_for(overflow)

    def test_same_class_different_site_stays_separate(self) -> None:
        other_site = _ASAN_USE_AFTER_FREE.replace("db.c:120", "cluster.c:900")
        a = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        b = _memory_failure(FailureType.SANITIZER, other_site, "test-sanitizer-address")
        assert fingerprint_for(a) != fingerprint_for(b)

    def test_unclassifiable_report_keeps_its_per_tool_namespace(self) -> None:
        f = _memory_failure(
            FailureType.VALGRIND,
            _vg("==6554== unrecognized diagnostic\n"
                "==6554==    at 0x1E8076: lookupKey (db.c:120)\n"),
            "test-valgrind",
        )
        assert marker_namespace_for(f) == "valkey-ci-agent:valgrind-error"

    def test_report_without_stack_frames_keeps_its_per_tool_namespace(self) -> None:
        """No frames means no anchor, so there is nothing to match on."""
        f = _memory_failure(
            FailureType.VALGRIND, " Valgrind error: Invalid read of size 4\n", "test-valgrind",
        )
        assert marker_namespace_for(f) == "valkey-ci-agent:valgrind-error"

    def test_ubsan_runtime_error_does_not_merge_with_a_memory_error(self) -> None:
        ub = _memory_failure(
            FailureType.SANITIZER, _UBSAN_RUNTIME_ERROR, "test-sanitizer-undefined",
        )
        uaf = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        assert fingerprint_for(ub) != fingerprint_for(uaf)

    def test_macos_memory_leak_is_excluded_from_the_merge(self) -> None:
        """/usr/bin/leaks runs where no other leak detector does and emits no
        stack frames, so it keeps its own namespace and identity."""
        f = _memory_failure(
            FailureType.MEMORY_LEAK,
            "Check for memory leaks (pid 9443) in tests/unit/other.tcl\n"
            "Process 9443: 1 leak for 48 total leaked bytes\n",
            "test-macos-latest",
        )
        assert marker_namespace_for(f) == "valkey-ci-agent:memory-leak"

    def test_other_types_keep_their_namespaces(self) -> None:
        expected = {
            FailureType.ASSERTION: "valkey-ci-agent:test-failure",
            FailureType.TIMEOUT: "valkey-ci-agent:test-timeout",
            FailureType.EXCEPTION: "valkey-ci-agent:test-exception",
            FailureType.MEMORY_LEAK: "valkey-ci-agent:memory-leak",
            FailureType.UNITTEST: "valkey-ci-agent:unittest-failure",
        }
        for failure_type, namespace in expected.items():
            f = _memory_failure(failure_type, _ASAN_USE_AFTER_FREE, "job")
            assert marker_namespace_for(f) == namespace


class TestMultiTraceBody:
    def _body(self, failure: UniqueFailure) -> str:
        return _build_body(failure, marker="<!-- m -->", occurrences=1)

    def test_single_trace_is_not_collapsed(self) -> None:
        body = self._body(
            _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        )
        assert "<details>" not in body
        assert _extract_error_from_body(body) == _VG_USE_AFTER_FREE.strip()

    def test_the_body_holds_one_trace_and_the_other_goes_to_a_comment(self) -> None:
        """The body keeps the shape of a hand-filed issue, one plain fenced
        trace, so it stays predictable to read and to parse."""
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        merged = _merge_same_fingerprint_failures([vg, asan])[0]
        content = renderer_for(merged).render("<!-- m -->", 1)
        assert "<details>" not in content.body
        assert content.body.count("**Error stack trace**") == 1
        # The survivor is the sanitizer, so its report is the body's.
        assert "heap-use-after-free" in content.body
        assert "Invalid read of size 4" not in content.body
        assert "Invalid read of size 4" in content.creation_comment
        assert "**New error stack trace**" in content.creation_comment

    def test_both_tools_appear_in_the_environments_line(self) -> None:
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        body = self._body(_merge_same_fingerprint_failures([vg, asan])[0])
        assert "`test-valgrind`" in body
        assert "`test-sanitizer-address`" in body


class TestRecurrenceCommentWithMultipleTraces:
    """New traces go in comments; the body is never rewritten to add one.

    An issue can record a trace per tool, so the recurrence check has to read
    every stored trace. Reading only the first would drop a new trace
    from the comment (it is the only place a new trace appears) and would also
    call an unchanged trace new on every run, commenting forever.
    """

    def _two_trace_body(self) -> str:
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        merged = _merge_same_fingerprint_failures([vg, asan])[0]
        return _build_body(merged, marker="<!-- m -->", occurrences=1)

    def _comment(self, body: str, failure: UniqueFailure, occurrences: int = 3) -> str:
        renderer = renderer_for(failure)
        # The publisher runs body_transform before render on the update path.
        renderer.merge_environments(body)
        return renderer.render("<!-- m -->", occurrences).comment

    def test_extracts_the_single_trace_a_body_records(self) -> None:
        traces = _extract_errors_from_body(self._two_trace_body())
        assert len(traces) == 1
        assert "heap-use-after-free" in traces[0]

    def test_extracts_the_single_trace_of_a_one_tool_body(self) -> None:
        body = _build_body(
            _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind"),
            marker="<!-- m -->", occurrences=1,
        )
        assert _extract_errors_from_body(body) == [_VG_USE_AFTER_FREE.strip()]

    def test_changed_trace_is_reported_in_the_comment(self) -> None:
        moved = _ASAN_USE_AFTER_FREE.replace("db.c:120:9", "cluster.c:900:5").replace(
            "lookupKey", "clusterProcessPacket",
        )
        comment = self._comment(
            self._two_trace_body(),
            _memory_failure(FailureType.SANITIZER, moved, "test-sanitizer-address"),
        )
        assert "New error stack trace" in comment
        assert "clusterProcessPacket" in comment

    def test_unchanged_trace_posts_no_new_error_section(self) -> None:
        """A trace matching the one the body stores says nothing new."""
        body = self._two_trace_body()
        comment = self._comment(body, _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        ))
        assert "New error stack trace" not in comment

    def test_body_is_not_rewritten_to_add_a_trace(self) -> None:
        """A recurrence carrying a tool the body lacks leaves the body alone."""
        body = _build_body(
            _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind"),
            marker="<!-- m -->", occurrences=1,
        )
        renderer = renderer_for(_memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        ))
        transformed = renderer.merge_environments(body)
        assert "heap-use-after-free" not in transformed
        # It surfaces in the comment instead.
        assert "heap-use-after-free" in renderer.render("<!-- m -->", 2).comment

    def test_the_body_never_collapses_its_trace(self) -> None:
        """One plain fenced block, matching a hand-filed issue."""
        body = self._two_trace_body()
        assert "<details>" not in body
        assert "<summary>" not in body


class TestCrossToolAnchorDepth:
    """The two tools do not unwind one stack to the same depth.

    Observed on a real Daily run: for one leak in debugCommand, valgrind
    stopped at readQueryFromClient while the sanitizer continued two frames
    further into the event loop. Requiring whole-chain equality never matched a
    real pair, so the identity keys on the frames nearest the bug.
    """

    def _leak(self, frames: str, failure_type: FailureType) -> UniqueFailure:
        if failure_type == FailureType.VALGRIND:
            error = _vg("==6554== 49 bytes in 1 blocks are definitely lost\n" + frames)
        else:
            error = (
                " Sanitizer error: ==1==ERROR: LeakSanitizer: detected memory leaks\n"
                "Direct leak of 41 byte(s) in 1 object(s) allocated from:\n" + frames
            )
        return _memory_failure(failure_type, error, "job")

    def test_shared_leading_frames_merge_despite_extra_outer_frames(self) -> None:
        vg = self._leak(
            "==6554==    at 0x484: malloc (vg_replace_malloc.c:381)\n"
            "==6554==    by 0x1E8: debugCommand (debug.c:569)\n"
            "==6554==    by 0x1E9: call (server.c:3600)\n"
            "==6554==    by 0x1EA: processCommand (server.c:4000)\n",
            FailureType.VALGRIND,
        )
        asan = self._leak(
            "    #0 0x55b in malloc\n"
            "    #1 0x55c in debugCommand /src/debug.c:569:9\n"
            "    #2 0x55d in call /src/server.c:3600:5\n"
            "    #3 0x55e in processCommand /src/server.c:4000:5\n"
            "    #4 0x55f in readQueryFromClient /src/networking.c:2500:5\n"
            "    #5 0x560 in connSocketEventHandler /src/socket.c:280:5\n",
            FailureType.SANITIZER,
        )
        assert fingerprint_for(vg) == fingerprint_for(asan)

    def test_divergence_within_the_anchor_still_separates(self) -> None:
        """Only the outer frames may differ. A different function near the bug
        is a different bug."""
        vg = self._leak(
            "==6554==    by 0x1E8: debugCommand (debug.c:569)\n"
            "==6554==    by 0x1E9: call (server.c:3600)\n"
            "==6554==    by 0x1EA: processCommand (server.c:4000)\n",
            FailureType.VALGRIND,
        )
        asan = self._leak(
            "    #1 0x55c in clusterCommand /src/cluster.c:900:9\n"
            "    #2 0x55d in call /src/server.c:3600:5\n"
            "    #3 0x55e in processCommand /src/server.c:4000:5\n",
            FailureType.SANITIZER,
        )
        assert fingerprint_for(vg) != fingerprint_for(asan)

    def test_short_stack_is_used_whole(self) -> None:
        """A stack shorter than the cap is the whole path the tool reported, so
        it is kept rather than rejected for being short."""
        f = self._leak(
            "==6554==    by 0x1E8: debugCommand (debug.c:569)\n", FailureType.VALGRIND,
        )
        assert cross_tool_anchor(f.error)
        assert marker_namespace_for(f) == MEMORY_ERROR_NAMESPACE


class TestMergedIssueRecordsBothTools:
    """A merged issue keeps the legacy body shape, so which tools reported the
    bug is recorded in the comment rather than in an extra body row."""

    def _merged(self) -> UniqueFailure:
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        return _merge_same_fingerprint_failures([vg, asan])[0]

    def test_the_body_carries_only_the_template_rows(self) -> None:
        body = _build_body(self._merged(), marker="<!-- m -->", occurrences=1)
        assert "- Test name:" in body
        assert "- Test file:" in body
        assert "- CI link(s):" in body
        assert "Failure type:" not in body
        assert "<details>" not in body

    def test_the_other_tool_is_named_in_the_comment(self) -> None:
        content = renderer_for(self._merged()).render("<!-- m -->", 1)
        assert "Valgrind" in content.creation_comment
        assert "Invalid read of size 4" in content.creation_comment

    def test_both_jobs_are_recorded(self) -> None:
        body = _build_body(self._merged(), marker="<!-- m -->", occurrences=1)
        assert "`test-valgrind`" in body
        assert "`test-sanitizer-address`" in body


class TestBodyShapeMatchesLegacyAcrossTypes:
    """Every failure type renders the same body shape.

    The detector's issues sit alongside ones filed by hand from the repository's
    test-failure template, so they follow its wording: a "<what> in <where> is
    failing in CI" summary over a Failing test(s) list, then the trace.
    """

    def _body(self, failure: UniqueFailure) -> str:
        return _build_body(failure, marker="<!-- m -->", occurrences=1)

    def test_named_failure_reads_like_the_template(self) -> None:
        body = self._body(_make_failure())
        assert (
            "`PSYNC2 test` in `tests/integration/replication-psync.tcl` "
            "is failing in CI." in body
        )

    def test_every_type_shares_the_summary_shape(self) -> None:
        for failure_type in FailureType:
            f = _memory_failure(
                failure_type, _VG_USE_AFTER_FREE, "job", test_file="tests/unit/other.tcl",
            )
            body = self._body(f)
            assert "**Summary**" in body
            assert "**Failing test(s)**" in body
            assert "is failing in CI." in body
            assert "**Error details**" not in body

    def test_merged_failure_keeps_the_template_summary(self) -> None:
        """A merged issue's summary reads like any other. The two tools are
        named in the Failure type row and the trace labels, so nothing is added
        to the sentence."""
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        body = self._body(_merge_same_fingerprint_failures([vg, asan])[0])
        summary = body.split("**Failing test(s)**")[0]
        assert "is failing in CI." in summary
        assert "Reported by" not in body

    def test_ci_links_are_indented_under_their_bullet(self) -> None:
        """The links are children of the "CI link(s):" bullet, so they must be
        indented; at the same level Markdown renders them as siblings."""
        body = self._body(_make_failure(jobs=[
            ("job-a", "suite", "https://example.com/a"),
            ("job-b", "suite", "https://example.com/b"),
        ]))
        assert "- CI link(s):\n    - `job-a`: [CI link](https://example.com/a)" in body
        assert "    - `job-b`: [CI link](https://example.com/b)" in body


class TestMergedBodyFitsGitHubLimit:
    """GitHub rejects a body over 65536 characters with a 422.

    A merged failure renders one trace per tool, so a per-trace cap sized for a
    single trace let two oversized traces exceed the limit on their own. The
    create call was rejected, which meant the bug both tools found was the one
    that got no issue.
    """

    _GITHUB_BODY_LIMIT = 65536

    def _frames(self, count: int, sanitizer: bool) -> str:
        if sanitizer:
            return "".join(
                f"    #{i} 0x55ba1234 in someFunctionName{i} "
                f"/home/runner/work/valkey/valkey/src/somefile{i}.c:{i}:9\n"
                for i in range(count)
            )
        return "".join(
            f"==6554==    by 0x1E80{i:02d}: someFunctionName{i} "
            f"(src/somefile{i}.c:{i})\n"
            for i in range(count)
        )

    def _merged(self, frame_count: int) -> UniqueFailure:
        vg = _memory_failure(
            FailureType.VALGRIND,
            "==6554== 49 bytes in 1 blocks are definitely lost\n"
            + self._frames(frame_count, sanitizer=False),
            "test-valgrind",
        )
        asan = _memory_failure(
            FailureType.SANITIZER,
            "==1==ERROR: LeakSanitizer: detected memory leaks\n"
            + self._frames(frame_count, sanitizer=True),
            "test-sanitizer-address",
        )
        return _merge_same_fingerprint_failures([vg, asan])[0]

    def test_two_oversized_traces_fit(self) -> None:
        for frame_count in (400, 800, 4000):
            body = _build_body(self._merged(frame_count), marker="<!-- m -->", occurrences=1)
            assert len(body) <= self._GITHUB_BODY_LIMIT, (
                f"{frame_count} frames per tool rendered {len(body)} characters"
            )

    def test_both_traces_survive_truncation_across_body_and_comment(self) -> None:
        content = renderer_for(self._merged(4000)).render("<!-- m -->", 1)
        assert "LeakSanitizer" in content.body
        assert "trace truncated by Test Failure Detector" in content.body
        assert "definitely lost" in content.creation_comment
        assert len(content.body) <= self._GITHUB_BODY_LIMIT
        assert len(content.creation_comment) <= self._GITHUB_BODY_LIMIT

    def test_a_trace_of_backticks_cannot_inflate_the_body(self) -> None:
        """The fence grows to out-run backtick runs in the text and is written
        twice per block, so an unbounded one multiplied the body past the
        limit."""
        bomb = "`" * 40_000
        vg = _memory_failure(FailureType.VALGRIND, bomb, "test-valgrind")
        asan = _memory_failure(FailureType.SANITIZER, bomb, "test-sanitizer-address")
        merged = _merge_same_fingerprint_failures([vg, asan])[0]
        body = _build_body(merged, marker="<!-- m -->", occurrences=1)
        assert len(body) <= self._GITHUB_BODY_LIMIT

    def test_a_comment_of_backticks_cannot_inflate_the_comment(self) -> None:
        renderer = renderer_for(
            _memory_failure(FailureType.VALGRIND, "`" * 40_000, "test-valgrind")
        )
        renderer._new_error = "`" * 40_000
        comment = renderer.render("<!-- m -->", 2).comment
        assert len(comment) <= self._GITHUB_BODY_LIMIT


class TestLongBacktickRunsCannotCloseTheFence:
    """The fence is clamped, so a backtick run at least as long as the clamp
    would close the block early and the body would store a truncated trace. The
    round-trip would then read back less than was published and every recurrence
    would post the same trace as new. Truncation is no defence: it keeps the
    head and the tail of the trace, so a run in either survives.
    """

    # At the clamp, one under, one over, and far past it. 63 is under the
    # threshold and must be left alone.
    @pytest.mark.parametrize("run_length", [63, 64, 65, 70, 5_000])
    def test_a_long_run_still_round_trips(self, run_length: int) -> None:
        trace = f"Sanitizer error: uaf\n{'`' * run_length}\ntail\n"
        failure = _memory_failure(FailureType.SANITIZER, trace, "test-sanitizer-address")
        body = _build_body(failure, marker="<!-- m -->", occurrences=1)
        # What the body stores is the bounded form, which is what a later run
        # compares against.
        assert _extract_error_from_body(body) == _bound_backtick_runs(trace).strip()

    @pytest.mark.parametrize("run_length", [64, 70, 5_000])
    def test_a_long_run_posts_no_comment_on_recurrence(self, run_length: int) -> None:
        trace = f"Sanitizer error: uaf\n{'`' * run_length}\ntail\n"
        failure = _memory_failure(FailureType.SANITIZER, trace, "test-sanitizer-address")
        body = _build_body(failure, marker="<!-- m -->", occurrences=1)
        for occurrence in range(2, 6):
            renderer = renderer_for(failure)
            body = renderer.merge_environments(body)
            comment = renderer.render("<!-- m -->", occurrence).comment
            assert "New error stack trace" not in comment

    def test_bounding_leaves_ordinary_traces_untouched(self) -> None:
        """Real tool output carries runs of three or four backticks; those must
        reach the issue exactly as emitted."""
        trace = "Sanitizer error: uaf\n```\ncode\n```\ntail\n"
        assert _bound_backtick_runs(trace) == trace

    def test_a_bounded_run_keeps_every_backtick(self) -> None:
        """The run is split, not trimmed: only zero-width spaces are added."""
        bounded = _bound_backtick_runs("`" * 200)
        assert bounded.count("`") == 200
        assert "​" in bounded


class TestAbsorbedTraceReachesTheIssue:
    """A tool's report must not be lost when its issue already exists.

    The publisher's update path writes the body it was handed by
    body_transform, not the freshly rendered one, so a trace added to the render
    never reaches an existing issue. The comment is the only channel that path
    publishes, and the recurrence check considered only the surviving failure's
    own trace, so the absorbed tool's report went nowhere at all.
    """

    def _valgrind_only_body(self) -> str:
        return _build_body(
            _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind"),
            marker="<!-- m -->", occurrences=1,
        )

    def _merged(self) -> UniqueFailure:
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        return _merge_same_fingerprint_failures([vg, asan])[0]

    def test_absorbed_trace_is_reported_in_the_comment(self) -> None:
        renderer = renderer_for(self._merged())
        renderer.merge_environments(self._valgrind_only_body())
        comment = renderer.render("<!-- m -->", 2).comment
        assert "heap-use-after-free" in comment
        assert "**New error stack trace**" in comment

    def test_a_trace_the_issue_already_stores_is_not_repeated(self) -> None:
        renderer = renderer_for(self._merged())
        renderer.merge_environments(self._valgrind_only_body())
        comment = renderer.render("<!-- m -->", 2).comment
        assert "Invalid read of size 4" not in comment

    def test_the_same_trace_is_not_reported_on_every_later_run(self) -> None:
        """The body keeps only its first-seen trace, so without a record of what
        was already published the comment would repeat every run."""
        body = self._valgrind_only_body()
        posted = []
        for occurrence in range(2, 7):
            renderer = renderer_for(self._merged())
            body = renderer.merge_environments(body)
            comment = renderer.render("<!-- m -->", occurrence).comment
            posted.append("heap-use-after-free" in comment)
        assert posted[0] is True
        assert not any(posted[1:]), "absorbed trace was reported more than once"

    def test_the_body_is_never_given_the_absorbed_trace(self) -> None:
        body = self._valgrind_only_body()
        for occurrence in range(2, 5):
            renderer = renderer_for(self._merged())
            body = renderer.merge_environments(body)
            renderer.render("<!-- m -->", occurrence)
        assert "heap-use-after-free" not in body
        assert body.count("<details>") == 0


class TestForgedMarkersInTraceText:
    """Trace text is producer-controlled and embedded in the body verbatim.

    The publisher finds an issue by searching a whole body for its marker, so a
    report containing a marker-shaped comment would be read as claiming that
    fingerprint, and an unrelated failure would be absorbed into its issue and
    silently dropped.
    """

    def _failure_with_forged_marker(self) -> tuple[UniqueFailure, str]:
        victim = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        forged = f"<!-- {marker_namespace_for(victim)}:{fingerprint_for(victim)} -->"
        attacker = _memory_failure(
            FailureType.SANITIZER,
            _ASAN_USE_AFTER_FREE.replace("lookupKey", "unrelatedFunction") + forged,
            "test-sanitizer-address",
        )
        return attacker, forged

    def test_a_marker_in_trace_text_is_defused(self) -> None:
        attacker, forged = self._failure_with_forged_marker()
        body = _build_body(attacker, marker="<!-- real -->", occurrences=1)
        assert forged not in body
        assert "<!-- real -->" in body

    def test_the_trace_is_still_readable(self) -> None:
        attacker, _ = self._failure_with_forged_marker()
        body = _build_body(attacker, marker="<!-- real -->", occurrences=1)
        assert "unrelatedFunction" in body

    def test_defusing_does_not_make_a_trace_look_new_every_run(self) -> None:
        """The stored trace was defused when published, so the comparison has to
        defuse too or the recurrence check would never match it."""
        attacker, _ = self._failure_with_forged_marker()
        body = _build_body(attacker, marker="<!-- real -->", occurrences=1)
        renderer = renderer_for(attacker)
        renderer.merge_environments(body)
        assert "New error stack trace" not in renderer.render("<!-- real -->", 2).comment


class TestForgedRowHeadingsInTraceText:
    """The row readers are anchored to a line start but search the whole body,
    and the trace sits above the real rows. A report line beginning with a row
    heading therefore matches before the real row: the environments would be read
    out of the trace and written back into it, corrupting the published trace
    while the real row went stale.
    """

    _TRACE = "Sanitizer error: uaf\n**Environments:** `forged-job`\nmore trace\n"

    def _failure(self, job: str = "real-job") -> UniqueFailure:
        return _memory_failure(FailureType.SANITIZER, self._TRACE, job)

    def test_environments_are_read_from_the_real_row(self) -> None:
        body = _build_body(self._failure(), marker="<!-- m -->", occurrences=1)
        assert _extract_environments_from_body(body) == ["real-job"]

    def test_a_forged_row_does_not_refile_every_job_each_run(self) -> None:
        failure = self._failure()
        body = _build_body(failure, marker="<!-- m -->", occurrences=1)
        for _ in range(4):
            renderer = renderer_for(failure)
            body = renderer.merge_environments(body)
            assert renderer._newly_failing == []

    def test_the_trace_is_never_rewritten(self) -> None:
        """The environments update must not touch the embedded trace."""
        failure = self._failure()
        body = _build_body(failure, marker="<!-- m -->", occurrences=1)
        body = renderer_for(failure).merge_environments(body)
        assert "`forged-job`, `real-job`" not in body

    def test_a_new_job_still_reaches_the_real_row(self) -> None:
        body = _build_body(self._failure(), marker="<!-- m -->", occurrences=1)
        renderer = renderer_for(self._failure("second-job"))
        updated = renderer.merge_environments(body)
        assert renderer._newly_failing == ["second-job"]
        assert "**Environments:** `real-job`, `second-job`" in updated

    def test_the_forged_heading_is_still_readable(self) -> None:
        body = _build_body(self._failure(), marker="<!-- m -->", occurrences=1)
        assert "Environments:" in body
        assert "more trace" in body


class TestAbsorbedTraceOnCreation:
    """A same-run merge creates the issue, so its absorbed report has no
    recurrence comment to ride on and is posted right after creation instead."""

    def _merged(self) -> UniqueFailure:
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        return _merge_same_fingerprint_failures([vg, asan])[0]

    def test_creation_comment_carries_the_absorbed_trace(self) -> None:
        content = renderer_for(self._merged()).render("<!-- m -->", 1)
        assert "Invalid read of size 4" in content.creation_comment
        assert "**New error stack trace**" in content.creation_comment

    def test_a_single_tool_failure_posts_no_creation_comment(self) -> None:
        content = renderer_for(
            _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        ).render("<!-- m -->", 1)
        assert content.creation_comment == ""

    def test_the_creation_comment_is_not_repeated_on_the_next_run(self) -> None:
        """The record of published traces has to start at creation. The body
        keeps only the survivor's trace, so a first recurrence reading no record
        finds the absorbed trace absent, calls it new, and posts it again."""
        merged = self._merged()
        body = renderer_for(merged).render("<!-- m -->", 1).body
        posted = []
        for occurrence in range(2, 6):
            renderer = renderer_for(merged)
            body = renderer.merge_environments(body)
            comment = renderer.render("<!-- m -->", occurrence).comment
            posted.append("Invalid read of size 4" in comment)
        assert not any(posted), "absorbed trace was published twice"

    def test_a_single_tool_body_records_no_traces_at_creation(self) -> None:
        """The record exists only to suppress a trace already published in a
        comment. A failure that posts no creation comment must not start with
        one, or a changed trace would go unreported."""
        content = renderer_for(
            _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        ).render("<!-- m -->", 1)
        assert "reported-traces" not in content.body

    def test_the_record_is_not_read_as_a_fingerprint_claim(self) -> None:
        """issue_dedup treats a namespaced marker ending in bare hex as a
        fingerprint claim, and a claimed issue is not adoptable by title. A
        one-entry record would match if its digests were bare hex."""
        from scripts.common.issue_dedup import _fingerprint_marker_re

        merged = self._merged()
        body = renderer_for(merged).render("<!-- m -->", 1).body
        assert "reported-traces" in body
        assert _fingerprint_marker_re(marker_namespace_for(merged)).search(body) is None

    def test_a_real_fingerprint_marker_still_reads_as_a_claim(self) -> None:
        """The other half: the exclusion must not blind the claim check."""
        from scripts.common.issue_dedup import _fingerprint_marker_re

        merged = self._merged()
        ns = marker_namespace_for(merged)
        claimed = f"<!-- {ns}:{fingerprint_for(merged)} -->"
        assert _fingerprint_marker_re(ns).search(claimed) is not None

    def test_a_changed_absorbed_trace_is_still_reported(self) -> None:
        """The record is keyed by trace content, not by tool. Keying it by tool
        would suppress that tool's next trace even after the trace changed,
        because the label is checked before any content comparison."""
        body = renderer_for(self._merged()).render("<!-- m -->", 1).body

        moved = _VG_USE_AFTER_FREE.replace("Invalid read of size 4", "Invalid write of size 8")
        vg = _memory_failure(FailureType.VALGRIND, moved, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        changed = _merge_same_fingerprint_failures([vg, asan])[0]

        renderer = renderer_for(changed)
        renderer.merge_environments(body)
        assert renderer._new_error is not None

    def test_an_unchanged_absorbed_trace_is_not_reported_again(self) -> None:
        """The other half of the same rule: identical content stays suppressed."""
        body = renderer_for(self._merged()).render("<!-- m -->", 1).body
        renderer = renderer_for(self._merged())
        renderer.merge_environments(body)
        assert renderer._new_error is None

    def test_the_creation_comment_defuses_markers(self) -> None:
        """The comment embeds tool output verbatim like the body does, so a
        marker-shaped comment in it must be inert too."""
        vg = _memory_failure(
            FailureType.VALGRIND,
            _VG_USE_AFTER_FREE + "<!-- valkey-ci-agent:memory-error:abc123def456 -->\n",
            "test-valgrind",
        )
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        merged = _merge_same_fingerprint_failures([vg, asan])[0]
        content = renderer_for(merged).render("<!-- m -->", 1)
        assert "<!-- valkey-ci-agent:memory-error:abc123def456 -->" not in content.creation_comment


class TestBodyMatchesTheHandFiledFormat:
    """The body reproduces the format of an issue filed from the repository's
    test-failure template, field for field, so a reader or a parser sees one
    shape whichever type the failure came from.

    Reference: the rows are Test name, Test file, CI link(s), followed by the
    trace and the environments line. No row is added, dropped, or renamed.
    """

    _EXPECTED_ROWS = ("- Test name: ", "- Test file: ", "- CI link(s):")

    def test_every_type_fills_every_row(self) -> None:
        for failure_type in FailureType:
            body = _build_body(
                _memory_failure(
                    failure_type, _VG_USE_AFTER_FREE, "job",
                    test_file="tests/unit/other.tcl",
                ),
                marker="<!-- m -->", occurrences=1,
            )
            for row in self._EXPECTED_ROWS:
                assert row in body, f"{failure_type.value} is missing {row!r}"

    def test_a_failure_with_no_test_name_is_filled_not_dropped(self) -> None:
        body = _build_body(
            _memory_failure(FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "job"),
            marker="<!-- m -->", occurrences=1,
        )
        assert "- Test name: `[no test]`" in body

    def test_a_failure_with_no_test_file_is_filled_not_dropped(self) -> None:
        f = UniqueFailure(
            test_name="", test_file="", failure_type=FailureType.EXCEPTION,
            error="Intentional runtime exception", jobs=[
                JobReference(job="j", suite="s", url="u"),
            ],
        )
        body = _build_body(f, marker="<!-- m -->", occurrences=1)
        assert "- Test file: `[no test]`" in body
        assert "in `[no test]` is failing in CI." in body

    def test_no_row_the_template_does_not_define_is_added(self) -> None:
        body = _build_body(
            _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "job"),
            marker="<!-- m -->", occurrences=1,
        )
        for absent in ("Failure type:", "Test file context:", "**Error details**"):
            assert absent not in body

    def test_ci_links_sit_at_the_template_indentation(self) -> None:
        body = _build_body(
            _make_failure(jobs=[("job-a", "suite", "https://example.com/a")]),
            marker="<!-- m -->", occurrences=1,
        )
        assert "- CI link(s):\n    - `job-a`: [CI link](https://example.com/a)" in body


class TestLeakTitlesCarryNoMagnitude:
    """A leak title names the leaking code path, never the number of bytes.

    One leak is reported at different sizes by different builds: on a real Daily
    run the same sdsnewlen leak was 41 bytes on the standard valgrind and
    sanitizer jobs and 49 on the NO_MALLOC_USABLE_SIZE ones, which account an
    allocation differently. All of those jobs dedup to one issue, and the
    publisher rewrites its title on every update, so a size in the title would
    flip between runs. The exact figures stay in the trace.
    """

    def _valgrind_leak(self, size: str) -> UniqueFailure:
        return _memory_failure(
            FailureType.VALGRIND,
            _vg(
                f"==6554== {size} in 1 blocks are definitely lost\n"
                "==6554==    at 0x4846828: malloc (vg_replace_malloc.c:381)\n"
                "==6554==    by 0x1E8076: debugCommand (debug.c:569)\n"
            ),
            "test-valgrind",
        )

    def _sanitizer_leak(self, size: str) -> UniqueFailure:
        return _memory_failure(
            FailureType.SANITIZER,
            "==1==ERROR: LeakSanitizer: detected memory leaks\n"
            f"SUMMARY: AddressSanitizer: {size} leaked in 1 allocation(s).\n"
            "    #0 0x55b in malloc\n"
            "    #1 0x55c in debugCommand /src/debug.c:569:9\n",
            "test-sanitizer-address",
        )

    def test_valgrind_leak_title_has_no_byte_count(self) -> None:
        title = title_for(self._valgrind_leak("41 bytes"))
        assert title == "[TEST-FAILURE] Definitely lost in debugCommand (debug.c:569)"
        assert "41" not in title
        assert " N " not in title

    def test_valgrind_leak_title_is_stable_across_build_sizes(self) -> None:
        """The 41-byte and 49-byte reports of one leak must title identically."""
        assert title_for(self._valgrind_leak("41 bytes")) == title_for(
            self._valgrind_leak("49 bytes")
        )

    def test_sanitizer_leak_title_has_no_byte_count(self) -> None:
        title = title_for(self._sanitizer_leak("41 byte(s)"))
        assert "Leaked memory" in title
        assert "debugCommand (debug.c:569)" in title
        assert "41" not in title

    def test_no_leak_title_shows_a_placeholder_count(self) -> None:
        """A scrubbed count read as a formatting fault, so the size is left out
        rather than printed as N."""
        for failure in (
            self._valgrind_leak("41 bytes"),
            self._sanitizer_leak("41 byte(s)"),
        ):
            title = title_for(failure)
            assert "N bytes" not in title
            assert "N byte(s)" not in title


def _leaks_blob(
    sites: list[str],
    test_file: str = "tests/unit/other.tcl",
    leaked_bytes: int = 48,
) -> str:
    """A macOS leaks report blaming *sites*, or bare addresses when empty."""
    roots = "".join(
        f"    1 ({leaked_bytes} bytes) ROOT LEAK: "
        f"<malloc in {site} 0x60000{index}> [{leaked_bytes}]\n"
        for index, site in enumerate(sites)
    ) or f"    1 ({leaked_bytes} bytes) ROOT LEAK: 0x600001d1c100 [{leaked_bytes}]\n"
    return (
        f"Check for memory leaks (pid 9443) in {test_file}\n"
        f"Process 9443: {max(len(sites), 1)} leak for {leaked_bytes} "
        f"total leaked bytes\n{roots}"
    )


class TestDistinctLeaksGetDistinctTitles:
    """Two leaks that are separate issues must not share one title.

    A macOS leaks report has no stack frames, so its title has less to work with
    than the other types, which all carry a file:line. Identical titles are
    unreadable in an issue list and feed the publisher's title fallback, which
    adopts an issue by exact title.
    """

    def _failure(self, error: str, test_file: str) -> UniqueFailure:
        return UniqueFailure(
            test_name="", test_file=test_file,
            failure_type=FailureType.MEMORY_LEAK, error=error,
            jobs=[JobReference(job="test-macos-latest", suite="valkey", url="u")],
        )

    def _assert_distinct(self, first: UniqueFailure, second: UniqueFailure) -> None:
        assert fingerprint_for(first) != fingerprint_for(second), (
            "fixture error: these should be separate issues"
        )
        assert title_for(first) != title_for(second)

    def test_reports_sharing_their_first_site_stay_distinct(self) -> None:
        """Naming only the first site gave two multi-root reports one title."""
        self._assert_distinct(
            self._failure(
                _leaks_blob(["sdsnewlen", "clusterInit"]), "tests/unit/other.tcl",
            ),
            self._failure(
                _leaks_blob(["sdsnewlen", "dictExpand"]), "tests/unit/other.tcl",
            ),
        )

    def test_unsymbolicated_reports_in_different_files_stay_distinct(self) -> None:
        """With no site to name, the file is the only thing left."""
        self._assert_distinct(
            self._failure(_leaks_blob([], "tests/unit/dump.tcl"), "tests/unit/dump.tcl"),
            self._failure(_leaks_blob([], "tests/unit/geo.tcl"), "tests/unit/geo.tcl"),
        )

    def test_the_title_is_stable_across_magnitude_and_report_order(self) -> None:
        """Sites are sorted and counts left out, so one leak keeps one title."""
        first = self._failure(
            _leaks_blob(["sdsnewlen", "clusterInit"], leaked_bytes=48),
            "tests/unit/other.tcl",
        )
        second = self._failure(
            _leaks_blob(["clusterInit", "sdsnewlen"], leaked_bytes=144),
            "tests/unit/other.tcl",
        )
        assert fingerprint_for(first) == fingerprint_for(second)
        assert title_for(first) == title_for(second)

    def test_a_report_blaming_many_sites_is_summarized(self) -> None:
        """A title listing a dozen functions is unreadable, and the fingerprint
        keys on the full set regardless."""
        title = title_for(self._failure(
            _leaks_blob(["a1", "b2", "c3", "d4", "e5"]), "tests/unit/other.tcl",
        ))
        assert "and 3 more" in title
        assert len(title) <= 256


class TestCommentsUseOnlyTemplateWording:
    """Comments reuse the template's headings and add no prose of their own.

    Detector issues sit beside ones filed by hand, so every section a reader or
    a parser meets should be one the template defines. Explanatory sentences and
    per-tool headings were invented here and are not part of it.
    """

    _INVENTED = (
        "also reported by",
        "The issue body holds",
        "Valgrind trace",
        "Sanitizer trace",
        "Reported by",
    )

    def _merged(self) -> UniqueFailure:
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        return _merge_same_fingerprint_failures([vg, asan])[0]

    def test_the_creation_comment_adds_no_prose(self) -> None:
        content = renderer_for(self._merged()).render("<!-- m -->", 1)
        assert content.creation_comment
        for phrase in self._INVENTED:
            assert phrase not in content.creation_comment

    def test_the_creation_comment_uses_the_template_heading(self) -> None:
        content = renderer_for(self._merged()).render("<!-- m -->", 1)
        assert "**New error stack trace**" in content.creation_comment
        assert "Invalid read of size 4" in content.creation_comment

    def test_the_recurrence_comment_adds_no_prose(self) -> None:
        body = _build_body(
            _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind"),
            marker="<!-- m -->", occurrences=1,
        )
        renderer = renderer_for(self._merged())
        renderer.merge_environments(body)
        comment = renderer.render("<!-- m -->", 2).comment
        assert "**New error stack trace**" in comment
        for phrase in self._INVENTED:
            assert phrase not in comment

    def test_the_body_adds_no_prose(self) -> None:
        body = _build_body(self._merged(), marker="<!-- m -->", occurrences=1)
        for phrase in self._INVENTED:
            assert phrase not in body


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


class TestCreationCommentCarriesItsContext:
    """The comment naming a second tool's report lists the jobs it came from.

    It uses the same headings as a recurrence comment. A bare trace read as
    truncated output: nothing in it said which jobs reported the bug or linked
    to their runs.
    """

    def _merged(self) -> UniqueFailure:
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        return _merge_same_fingerprint_failures([vg, asan])[0]

    def test_the_comment_lists_the_failing_jobs(self) -> None:
        comment = renderer_for(self._merged()).render("<!-- m -->", 1).creation_comment
        assert "**Failed in:**" in comment
        assert "`test-valgrind`" in comment
        assert "`test-sanitizer-address`" in comment

    def test_the_comment_still_carries_the_trace(self) -> None:
        comment = renderer_for(self._merged()).render("<!-- m -->", 1).creation_comment
        assert "**New error stack trace**" in comment
        assert "Invalid read of size 4" in comment

    def test_the_headings_match_a_recurrence_comment(self) -> None:
        """Both comments use the same sections, so a reader meets one format."""
        merged = self._merged()
        creation = renderer_for(merged).render("<!-- m -->", 1).creation_comment
        renderer = renderer_for(merged)
        renderer.merge_environments(_build_body(
            _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind"),
            marker="<!-- m -->", occurrences=1,
        ))
        recurrence = renderer.render("<!-- m -->", 2).comment
        assert "**Failed in:**" in creation
        assert "**Failed in:**" in recurrence

    def test_a_failure_with_no_jobs_omits_the_section(self) -> None:
        """A merged failure always has jobs, but an empty list must not leave a
        heading with nothing under it."""
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        merged = _merge_same_fingerprint_failures([vg, asan])[0]
        merged.jobs.clear()
        comment = renderer_for(merged).render("<!-- m -->", 1).creation_comment
        assert "**Failed in:**" not in comment
        assert "**New error stack trace**" in comment


class TestBothCommentsShareOneStructure:
    """Every comment the detector posts follows the recurrence model:
    an opening line, then New error stack trace, then Failed in.

    The comment carrying a second tool's report is rendered by the same function
    as a recurrence, so the two cannot drift. Built separately, it lost the
    opening line and the job list and used a heading of its own.
    """

    def _merged(self) -> UniqueFailure:
        vg = _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind")
        asan = _memory_failure(
            FailureType.SANITIZER, _ASAN_USE_AFTER_FREE, "test-sanitizer-address",
        )
        return _merge_same_fingerprint_failures([vg, asan])[0]

    def _sections(self, comment: str) -> list[str]:
        return [
            line for line in comment.split("\n")
            if line.startswith("**") or line.startswith("Test failed again on")
        ]

    def test_the_creation_comment_follows_the_model(self) -> None:
        comment = renderer_for(self._merged()).render("<!-- m -->", 1).creation_comment
        sections = self._sections(comment)
        assert sections[0].startswith("Test failed again on")
        assert "**New error stack trace**" in sections
        assert "**Failed in:**" in sections

    def test_a_recurrence_comment_follows_the_same_model(self) -> None:
        merged = self._merged()
        renderer = renderer_for(merged)
        renderer.merge_environments(_build_body(
            _memory_failure(FailureType.VALGRIND, _VG_USE_AFTER_FREE, "test-valgrind"),
            marker="<!-- m -->", occurrences=1,
        ))
        sections = self._sections(renderer.render("<!-- m -->", 2).comment)
        assert sections[0].startswith("Test failed again on")
        assert "**New error stack trace**" in sections
        assert "**Failed in:**" in sections

    def test_neither_comment_invents_a_heading(self) -> None:
        merged = self._merged()
        content = renderer_for(merged).render("<!-- m -->", 1)
        allowed = {
            "**New error stack trace**",
            "**Failed in:**",
            "**Newly failing in:**",
        }
        for comment in (content.creation_comment, content.comment):
            for section in self._sections(comment):
                if section.startswith("Test failed again on"):
                    continue
                assert any(section.startswith(a.rstrip("*")) for a in allowed), (
                    f"unexpected section: {section!r}"
                )
