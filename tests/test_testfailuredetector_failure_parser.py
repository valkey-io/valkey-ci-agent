"""Unit tests for test failure parse/dedup logic."""

from __future__ import annotations

import pytest

from scripts.test_failure_detector.parse_failures import (
    JobReference,
    UniqueFailure,
    parse_and_deduplicate,
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

    def test_skips_entries_missing_test_name(self) -> None:
        data = {
            "job-1": {
                "suite": [
                    {"test_file": "foo.tcl", "error": "oops"},  # missing test_name
                    {"test_name": "real test", "test_file": "bar.tcl", "error": "err"},
                ]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert len(results) == 1
        assert results[0].test_name == "real test"

    def test_skips_entries_missing_test_file(self) -> None:
        data = {
            "job-1": {
                "suite": [
                    {"test_name": "orphan", "error": "oops"},  # missing test_file
                ]
            }
        }
        results = parse_and_deduplicate(data, {})
        assert results == []

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
