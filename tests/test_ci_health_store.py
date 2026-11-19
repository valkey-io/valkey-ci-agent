"""Unit tests for the Cache class."""

from __future__ import annotations

import json

from scripts.ci_health.store import Cache


def _sample_run(run_id: int = 1, **overrides) -> dict:
    base = {
        "run_id": run_id,
        "workflow_file": "daily.yml",
        "status": "failure",
        "branch": "unstable",
        "commit_sha": "abc123",
        "run_date": "2024-01-15T08:30:00Z",
        "duration_secs": 3600,
        "raw_json": json.dumps({"id": run_id}),
    }
    base.update(overrides)
    return base


def _sample_job(job_id: int = 100, **overrides) -> dict:
    base = {
        "job_id": job_id,
        "name": "build-ubuntu",
        "status": "completed",
        "conclusion": "failure",
        "raw_json": json.dumps({"id": job_id}),
    }
    base.update(overrides)
    return base


def _sample_failure(**overrides) -> dict:
    base = {
        "test_name": "unit/auth -- test_acl",
        "error_summary": "Expected OK got ERR",
        "log_lines": "line1\nline2",
    }
    base.update(overrides)
    return base


class TestCacheRuns:
    def test_has_run_false_on_empty(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        assert cache.has_run(999) is False

    def test_store_and_has_run(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1)])
        assert cache.has_run(1) is True
        assert cache.has_run(2) is False

    def test_store_runs_duplicate_ignored(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1)])
        cache.store_runs([_sample_run(1)])  # should not raise
        assert len(cache.query_runs()) == 1

    def test_query_runs_no_filter(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1), _sample_run(2)])
        results = cache.query_runs()
        assert len(results) == 2

    def test_query_runs_workflow_filter(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs(
            [
                _sample_run(1, workflow_file="daily.yml"),
                _sample_run(2, workflow_file="weekly.yml"),
            ]
        )
        results = cache.query_runs(workflow="daily.yml")
        assert len(results) == 1
        assert results[0]["workflow_file"] == "daily.yml"

    def test_query_runs_status_filter(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs(
            [
                _sample_run(1, status="success"),
                _sample_run(2, status="failure"),
            ]
        )
        results = cache.query_runs(status="failure")
        assert len(results) == 1
        assert results[0]["status"] == "failure"

    def test_query_runs_branch_filter(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs(
            [
                _sample_run(1, branch="main"),
                _sample_run(2, branch="unstable"),
            ]
        )
        results = cache.query_runs(branch="main")
        assert len(results) == 1
        assert results[0]["branch"] == "main"

    def test_query_runs_date_range(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs(
            [
                _sample_run(1, run_date="2024-01-10T00:00:00Z"),
                _sample_run(2, run_date="2024-01-15T00:00:00Z"),
                _sample_run(3, run_date="2024-01-20T00:00:00Z"),
            ]
        )
        results = cache.query_runs(
            since="2024-01-12T00:00:00Z", until="2024-01-18T00:00:00Z"
        )
        assert len(results) == 1
        assert results[0]["run_id"] == 2


class TestCacheJobs:
    def test_has_jobs_for_run_false(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        assert cache.has_jobs_for_run(1) is False

    def test_store_and_query_jobs(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1)])
        cache.store_jobs(1, [_sample_job(100), _sample_job(101, conclusion="success")])
        assert cache.has_jobs_for_run(1) is True
        all_jobs = cache.query_jobs(1)
        assert len(all_jobs) == 2

    def test_query_jobs_failed_only(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1)])
        cache.store_jobs(
            1,
            [
                _sample_job(100, conclusion="failure"),
                _sample_job(101, conclusion="success"),
            ],
        )
        failed = cache.query_jobs(1, failed_only=True)
        assert len(failed) == 1
        assert failed[0]["conclusion"] == "failure"


class TestCacheLogs:
    def test_has_log_false(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        assert cache.has_log(100) is False

    def test_store_and_get_log(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1)])
        cache.store_jobs(1, [_sample_job(100)])
        cache.store_log(100, "some log content")
        assert cache.has_log(100) is True
        assert cache.get_log(100) == "some log content"

    def test_get_log_missing(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        assert cache.get_log(999) is None


class TestCacheFailures:
    def test_has_failures_false(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        assert cache.has_failures_for_job(100) is False

    def test_store_and_query_failures(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1)])
        cache.store_jobs(1, [_sample_job(100)])
        cache.store_failures(100, [_sample_failure()])
        assert cache.has_failures_for_job(100) is True
        results = cache.query_failures(job_id=100)
        assert len(results) == 1
        assert results[0]["test_name"] == "unit/auth -- test_acl"

    def test_mark_unparseable(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1)])
        cache.store_jobs(1, [_sample_job(100)])
        cache.mark_unparseable(100)
        assert cache.has_failures_for_job(100) is True
        results = cache.query_failures(job_id=100)
        assert len(results) == 0  # no actual failures, just marked

    def test_query_failures_test_name_pattern(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1)])
        cache.store_jobs(1, [_sample_job(100)])
        cache.store_failures(
            100,
            [
                _sample_failure(test_name="unit/auth -- test_acl"),
                _sample_failure(test_name="unit/cluster -- test_repl"),
            ],
        )
        results = cache.query_failures(test_name_pattern="%auth%")
        assert len(results) == 1
        assert results[0]["test_name"] == "unit/auth -- test_acl"

    def test_query_failures_date_filter(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1)])
        cache.store_jobs(1, [_sample_job(100)])
        cache.store_failures(100, [_sample_failure()])
        # parsed_at is set to now, so filtering with a future date should return results
        results = cache.query_failures(since="2020-01-01T00:00:00Z")
        assert len(results) == 1
        # filtering with a far-future since should return nothing
        results = cache.query_failures(since="2099-01-01T00:00:00Z")
        assert len(results) == 0

    def test_store_failures_duplicate_ignored(self, temp_db_path: str) -> None:
        cache = Cache(temp_db_path)
        cache.store_runs([_sample_run(1)])
        cache.store_jobs(1, [_sample_job(100)])
        cache.store_failures(100, [_sample_failure()])
        cache.store_failures(100, [_sample_failure()])  # duplicate
        results = cache.query_failures(job_id=100)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# Property-based tests (hypothesis)
# ---------------------------------------------------------------------------

import hypothesis.strategies as st
from hypothesis import given, settings

# -- Hypothesis strategies for generating valid cache records --

_workflow_files = st.sampled_from(["daily.yml", "weekly.yml"])
_statuses = st.sampled_from(["success", "failure", "cancelled"])
_conclusions = st.sampled_from(["success", "failure", "cancelled", None])
_iso_dates = st.dates().map(lambda d: d.isoformat() + "T00:00:00Z")
_ids = st.integers(min_value=1, max_value=2**31)
_text = st.text(
    min_size=1,
    max_size=50,
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")),
)


@st.composite
def workflow_run_strategy(draw):
    return {
        "run_id": draw(_ids),
        "workflow_file": draw(_workflow_files),
        "status": draw(_statuses),
        "branch": draw(_text),
        "commit_sha": draw(_text),
        "run_date": draw(_iso_dates),
        "duration_secs": draw(st.integers(min_value=0, max_value=100_000)),
        "raw_json": draw(_text),
    }


@st.composite
def job_strategy(draw):
    return {
        "job_id": draw(_ids),
        "name": draw(_text),
        "status": draw(_statuses),
        "conclusion": draw(_conclusions),
        "raw_json": draw(_text),
    }


@st.composite
def failure_strategy(draw):
    return {
        "test_name": draw(_text),
        "error_summary": draw(_text),
        "log_lines": draw(_text),
    }


# Property 1: Cache round-trip preserves all fields
class TestCacheRoundTripProperty:
    """**Validates: Requirements 1.3, 2.2, 3.2, 4.3**"""

    @given(run=workflow_run_strategy())
    @settings(max_examples=100)
    def test_workflow_run_round_trip(self, tmp_path_factory, run: dict) -> None:
        """Storing a workflow run and querying it back preserves all fields."""
        db_path = str(tmp_path_factory.mktemp("db") / "cache.db")
        cache = Cache(db_path)
        cache.store_runs([run])
        results = cache.query_runs()
        assert len(results) == 1
        got = results[0]
        assert got["run_id"] == run["run_id"]
        assert got["workflow_file"] == run["workflow_file"]
        assert got["status"] == run["status"]
        assert got["branch"] == run["branch"]
        assert got["commit_sha"] == run["commit_sha"]
        assert got["run_date"] == run["run_date"]
        assert got["duration_secs"] == run["duration_secs"]

    @given(run=workflow_run_strategy(), job=job_strategy())
    @settings(max_examples=100)
    def test_job_round_trip(self, tmp_path_factory, run: dict, job: dict) -> None:
        """Storing a job and querying it back preserves all fields."""
        db_path = str(tmp_path_factory.mktemp("db") / "cache.db")
        cache = Cache(db_path)
        cache.store_runs([run])
        cache.store_jobs(run["run_id"], [job])
        results = cache.query_jobs(run["run_id"])
        assert len(results) == 1
        got = results[0]
        assert got["job_id"] == job["job_id"]
        assert got["run_id"] == run["run_id"]
        assert got["name"] == job["name"]
        assert got["status"] == job["status"]
        assert got["conclusion"] == job["conclusion"]

    @given(
        run=workflow_run_strategy(),
        job=job_strategy(),
        log_content=st.text(min_size=0, max_size=500),
    )
    @settings(max_examples=100)
    def test_log_round_trip(
        self, tmp_path_factory, run: dict, job: dict, log_content: str
    ) -> None:
        """Storing a log and retrieving it back preserves the content."""
        db_path = str(tmp_path_factory.mktemp("db") / "cache.db")
        cache = Cache(db_path)
        cache.store_runs([run])
        cache.store_jobs(run["run_id"], [job])
        cache.store_log(job["job_id"], log_content)
        got = cache.get_log(job["job_id"])
        assert got == log_content

    @given(run=workflow_run_strategy(), job=job_strategy(), failure=failure_strategy())
    @settings(max_examples=100)
    def test_failure_round_trip(
        self, tmp_path_factory, run: dict, job: dict, failure: dict
    ) -> None:
        """Storing a test failure and querying it back preserves all fields."""
        db_path = str(tmp_path_factory.mktemp("db") / "cache.db")
        cache = Cache(db_path)
        cache.store_runs([run])
        cache.store_jobs(run["run_id"], [job])
        cache.store_failures(job["job_id"], [failure])
        results = cache.query_failures(job_id=job["job_id"])
        assert len(results) == 1
        got = results[0]
        assert got["job_id"] == job["job_id"]
        assert got["test_name"] == failure["test_name"]
        assert got["error_summary"] == failure["error_summary"]
        assert got["log_lines"] == failure["log_lines"]


# Property 4: Query filters return only matching results
class TestQueryFiltersProperty:
    """**Validates: Requirements 5.1, 5.2, 5.3**"""

    # -- Strategies for filter values --
    _filter_workflow = st.one_of(st.none(), _workflow_files)
    _filter_status = st.one_of(st.none(), _statuses)
    _filter_branch = st.one_of(
        st.none(), st.sampled_from(["main", "unstable", "release"])
    )
    _filter_date = st.one_of(st.none(), _iso_dates)

    @st.composite
    @staticmethod
    def _unique_runs(draw):
        """Generate a list of workflow runs with unique run_ids."""
        branches = st.sampled_from(["main", "unstable", "release"])
        runs = draw(
            st.lists(
                st.fixed_dictionaries(
                    {
                        "run_id": _ids,
                        "workflow_file": _workflow_files,
                        "status": _statuses,
                        "branch": branches,
                        "commit_sha": _text,
                        "run_date": _iso_dates,
                        "duration_secs": st.integers(min_value=0, max_value=100_000),
                        "raw_json": st.just("{}"),
                    }
                ),
                min_size=1,
                max_size=15,
                unique_by=lambda r: r["run_id"],
            )
        )
        return runs

    @given(data=st.data())
    @settings(max_examples=100)
    def test_query_runs_filters(self, tmp_path_factory, data) -> None:
        """Every result matches all active predicates and no matching record is missing."""
        db_path = str(tmp_path_factory.mktemp("db") / "cache.db")
        cache = Cache(db_path)

        runs = data.draw(self._unique_runs())
        cache.store_runs(runs)

        workflow = data.draw(self._filter_workflow)
        status = data.draw(self._filter_status)
        branch = data.draw(self._filter_branch)
        since = data.draw(self._filter_date)
        until = data.draw(self._filter_date)

        results = cache.query_runs(
            workflow=workflow,
            status=status,
            branch=branch,
            since=since,
            until=until,
        )

        # Build the expected set by applying all predicates manually
        expected = runs
        if workflow is not None:
            expected = [r for r in expected if r["workflow_file"] == workflow]
        if status is not None:
            expected = [r for r in expected if r["status"] == status]
        if branch is not None:
            expected = [r for r in expected if r["branch"] == branch]
        if since is not None:
            expected = [r for r in expected if r["run_date"] >= since]
        if until is not None:
            expected = [r for r in expected if r["run_date"] <= until]

        expected_ids = {r["run_id"] for r in expected}
        result_ids = {r["run_id"] for r in results}

        # Every returned result matches all predicates
        for r in results:
            if workflow is not None:
                assert r["workflow_file"] == workflow
            if status is not None:
                assert r["status"] == status
            if branch is not None:
                assert r["branch"] == branch
            if since is not None:
                assert r["run_date"] >= since
            if until is not None:
                assert r["run_date"] <= until

        # Completeness: no matching record is missing
        assert result_ids == expected_ids

    @given(data=st.data())
    @settings(max_examples=100)
    def test_query_jobs_failed_only_filter(self, tmp_path_factory, data) -> None:
        """failed_only=True returns only jobs with conclusion='failure'; False returns all."""
        db_path = str(tmp_path_factory.mktemp("db") / "cache.db")
        cache = Cache(db_path)

        run_id = data.draw(st.integers(min_value=1, max_value=2**31))
        run = {
            "run_id": run_id,
            "workflow_file": "daily.yml",
            "status": "failure",
            "branch": "main",
            "commit_sha": "abc",
            "run_date": "2024-01-01T00:00:00Z",
            "duration_secs": 100,
            "raw_json": "{}",
        }
        cache.store_runs([run])

        jobs = data.draw(
            st.lists(
                st.fixed_dictionaries(
                    {
                        "job_id": _ids,
                        "name": _text,
                        "status": st.just("completed"),
                        "conclusion": _conclusions,
                        "raw_json": st.just("{}"),
                    }
                ),
                min_size=1,
                max_size=10,
                unique_by=lambda j: j["job_id"],
            )
        )
        cache.store_jobs(run_id, jobs)

        failed_only = data.draw(st.booleans())
        results = cache.query_jobs(run_id, failed_only=failed_only)

        if failed_only:
            expected = [j for j in jobs if j["conclusion"] == "failure"]
            for r in results:
                assert r["conclusion"] == "failure"
        else:
            expected = jobs

        expected_ids = {j["job_id"] for j in expected}
        result_ids = {r["job_id"] for r in results}
        assert result_ids == expected_ids

    @given(data=st.data())
    @settings(max_examples=100)
    def test_query_failures_filters(self, tmp_path_factory, data) -> None:
        """Every result matches all active predicates and no matching record is missing."""
        db_path = str(tmp_path_factory.mktemp("db") / "cache.db")
        cache = Cache(db_path)

        # Create a parent run and multiple jobs with unique IDs
        run_id = data.draw(st.integers(min_value=1, max_value=2**30))
        run = {
            "run_id": run_id,
            "workflow_file": "daily.yml",
            "status": "failure",
            "branch": "main",
            "commit_sha": "abc",
            "run_date": "2024-01-01T00:00:00Z",
            "duration_secs": 100,
            "raw_json": "{}",
        }
        cache.store_runs([run])

        job_ids = data.draw(
            st.lists(
                st.integers(min_value=1, max_value=2**30),
                min_size=1,
                max_size=3,
                unique=True,
            )
        )
        for jid in job_ids:
            cache.store_jobs(
                run_id,
                [
                    {
                        "job_id": jid,
                        "name": "job",
                        "status": "completed",
                        "conclusion": "failure",
                        "raw_json": "{}",
                    }
                ],
            )

        # Use a fixed set of test name prefixes so we can test LIKE patterns
        test_prefixes = ["unit/auth", "unit/cluster", "integration/repl"]
        all_failures = []
        # We need to control parsed_at for since/until filtering.
        # Use store_failures which sets parsed_at to now. We'll use fixed
        # since/until values relative to "now" to make the test deterministic.
        for jid in job_ids:
            num_failures = data.draw(st.integers(min_value=0, max_value=3))
            for i in range(num_failures):
                prefix = data.draw(st.sampled_from(test_prefixes))
                failure = {
                    "test_name": f"{prefix} -- test_{jid}_{i}",
                    "error_summary": "err",
                    "log_lines": "log",
                }
                all_failures.append({"job_id": jid, **failure})
                cache.store_failures(jid, [failure])

        # Pick random filter values
        filter_job_id = data.draw(st.one_of(st.none(), st.sampled_from(job_ids)))
        filter_pattern = data.draw(
            st.one_of(
                st.none(),
                st.sampled_from(["%auth%", "%cluster%", "%repl%"]),
            )
        )

        results = cache.query_failures(
            job_id=filter_job_id,
            test_name_pattern=filter_pattern,
            # Skip since/until for this test since parsed_at is set to "now"
            # and we can't control it precisely in the property test.
        )

        def _like_matches(value: str, pattern: str) -> bool:
            """Simulate SQL LIKE: % matches any sequence, _ matches one char."""
            import re

            # Escape regex specials, then convert SQL LIKE wildcards
            regex = ""
            for ch in pattern:
                if ch == "%":
                    regex += ".*"
                elif ch == "_":
                    regex += "."
                else:
                    regex += re.escape(ch)
            return re.fullmatch(regex, value) is not None

        # Build expected set
        expected = all_failures
        if filter_job_id is not None:
            expected = [f for f in expected if f["job_id"] == filter_job_id]
        if filter_pattern is not None:
            expected = [
                f for f in expected if _like_matches(f["test_name"], filter_pattern)
            ]

        expected_keys = {(f["job_id"], f["test_name"]) for f in expected}
        result_keys = {(r["job_id"], r["test_name"]) for r in results}

        # Every returned result matches all predicates
        for r in results:
            if filter_job_id is not None:
                assert r["job_id"] == filter_job_id
            if filter_pattern is not None:
                assert _like_matches(r["test_name"], filter_pattern)

        # Completeness
        assert result_keys == expected_keys


class TestParserVersionInvalidation:
    """Cache invalidates parse results when parser version bumps."""

    def test_version_bump_clears_failures(self, tmp_path) -> None:
        import scripts.ci_health.store as cache_mod

        db = str(tmp_path / "test.db")
        c = Cache(db)
        c.store_runs([_sample_run(1)])
        c.store_jobs(1, [_sample_job(100)])
        c.store_failures(
            100, [{"test_name": "old name", "error_summary": "x", "log_lines": "y"}]
        )
        assert c.query_failures(job_id=100) != []
        c._conn.close()

        # Simulate a parser version bump
        old_version = cache_mod.PARSER_VERSION
        cache_mod.PARSER_VERSION = old_version + 1
        try:
            c2 = Cache(db)
            # Failures wiped
            assert c2.query_failures(job_id=100) == []
            # But runs and jobs still exist
            assert c2.has_run(1)
            assert c2.has_jobs_for_run(1)
            c2._conn.close()
        finally:
            cache_mod.PARSER_VERSION = old_version

    def test_same_version_keeps_failures(self, tmp_path) -> None:
        db = str(tmp_path / "test.db")
        c = Cache(db)
        c.store_runs([_sample_run(1)])
        c.store_jobs(1, [_sample_job(100)])
        c.store_failures(
            100, [{"test_name": "kept", "error_summary": "x", "log_lines": "y"}]
        )
        c._conn.close()

        c2 = Cache(db)
        assert len(c2.query_failures(job_id=100)) == 1
        assert c2.query_failures(job_id=100)[0]["test_name"] == "kept"
        c2._conn.close()
