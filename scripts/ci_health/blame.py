"""Blame narrowing: identify commits likely responsible for test regressions."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Protocol

from scripts.ci_health.fingerprint import fingerprint_for_name
from scripts.ci_health.stats import regression_confidence
from scripts.ci_health.store import Cache
from scripts.ci_health.windowing import run_key, select_runs

logger = logging.getLogger(__name__)


class CommitComparer(Protocol):
    """The one GitHub capability blame needs: list commits between two SHAs.

    Kept as a protocol so the statistics layer has no GitHub client
    dependency; the sync layer supplies a PyGithub-backed implementation.
    """

    def compare_commits(self, base_sha: str, head_sha: str) -> List[Dict]: ...

# A detected regression whose test has stayed quiet for at least this many
# clean runs since its last failure is treated as "likely fixed" and moved to
# a collapsed sub-list. Tighter than the scorecard's resolved window because a
# regression is acute -- a couple of clean CI weeks is a strong fixed signal.
REGRESSION_ONGOING_QUIET_RUNS = 14


def compute_blame(
    cache: Cache,
    client: Optional[CommitComparer],
    days: int = 30,
    branch: str = "unstable",
    workflow: str = "daily.yml",
    repo: str = "valkey-io/valkey",
    per_run: bool = False,
    max_runs: int = 50,
) -> List[Dict]:
    """For each test that has a green→red transition, identify blame candidates.

    Finds the last passing run before the first failure and uses the GitHub
    compare API to list commits between those two SHAs.

    Returns a list of blame records sorted by recency (newest regressions first).
    """
    # Detection window: per-run keeps the last max_runs runs; per-day dedups
    # to one per day over the last `days` days.
    if per_run:
        window_runs = cache.query_runs(repo=repo, workflow=workflow, branch=branch)
        valid_runs = select_runs(window_runs, per_run=True)[-max_runs:]
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT00:00:00Z"
        )
        window_runs = cache.query_runs(
            repo=repo, workflow=workflow, branch=branch, since=since
        )
        valid_runs = select_runs(window_runs, per_run=False)

    if not valid_runs:
        return []

    # Build per-run failure sets
    run_failures: List[Dict] = []  # [{run, failing_tests: set}]
    for run in valid_runs:
        jobs = cache.query_jobs(run["run_id"], failed_only=True)
        failing = set()
        for job in jobs:
            for f in cache.query_failures(job_id=job["job_id"]):
                failing.add(f["test_name"])
        run_failures.append({"run": run, "failing_tests": failing})

    # All-history per-test failing keys, for the prior-aware confidence
    # baseline: a test's flakiness BEFORE onset (not just within the detection
    # window). The windowed pre-onset is trivially clean -- onset IS the first
    # in-window failure -- so a known flake would look novel without this.
    all_runs = cache.query_runs(repo=repo, workflow=workflow, branch=branch)
    hist_valid = select_runs(all_runs, per_run=per_run)
    all_dates = [run_key(r, per_run) for r in hist_valid]
    hist_fail_dates: Dict[str, set] = defaultdict(set)
    for run in hist_valid:
        dk = run_key(run, per_run)
        for job in cache.query_jobs(run["run_id"], failed_only=True):
            for f in cache.query_failures(job_id=job["job_id"]):
                hist_fail_dates[f["test_name"]].add(dk)

    def _confidence(test_name: str, onset: str):
        """Prior-aware confidence from all-history counts around onset."""
        fdates = hist_fail_dates.get(test_name, set())
        pre_total = sum(1 for d in all_dates if d < onset)
        post_total = sum(1 for d in all_dates if d >= onset)
        pre_fails = sum(1 for d in fdates if d < onset)
        post_fails = sum(1 for d in fdates if d >= onset)
        return regression_confidence(pre_fails, pre_total, post_fails, post_total)

    def _quiet_runs(test_name: str) -> int:
        """Clean runs since the test's most recent failure (any time)."""
        fdates = hist_fail_dates.get(test_name, set())
        if not fdates:
            return len(all_dates)
        last = max(fdates)
        return sum(1 for d in all_dates if d > last)

    # For each test, find the first appearance (green→red transition)
    # A transition is: test NOT in run[i-1] failures, but IS in run[i] failures
    all_tests = set()
    for rf in run_failures:
        all_tests.update(rf["failing_tests"])

    blame_records: List[Dict] = []
    for test_name in all_tests:
        # Find the first run where this test failed
        first_fail_idx = None
        for i, rf in enumerate(run_failures):
            if test_name in rf["failing_tests"]:
                first_fail_idx = i
                break

        if first_fail_idx is None:
            continue

        # Post-onset failure rate: of the runs from the first failure onward,
        # how many still failed. High => durable regression (blame reliable);
        # low => likely a one-off flake, so the blamed commit range is not
        # trustworthy.
        post_runs = run_failures[first_fail_idx:]
        post_fails = sum(1 for rf in post_runs if test_name in rf["failing_tests"])
        post_onset_rate = round(post_fails / len(post_runs), 4)

        # Chronological pass/fail series across the detection window (1 = failed
        # that run), plus the index of the onset run. Powers the Regressions-tab
        # sparkline with a marked regime-change point.
        window_series = [
            1 if test_name in rf["failing_tests"] else 0 for rf in run_failures
        ]

        # Wider all-history series for the sparkline display, so the full
        # baseline before onset is always visible regardless of the detection
        # window. (The heatmap-warning gate still uses the window series above.)
        _fdates = hist_fail_dates.get(test_name, set())
        history_series = [1 if d in _fdates else 0 for d in all_dates]

        # The "last green" is the run immediately before first_fail_idx
        if first_fail_idx == 0:
            # Test was already failing at the start of our window — no transition visible
            blame_records.append(
                {
                    "test_name": test_name,
                    "fingerprint": fingerprint_for_name(test_name),
                    "regression_date": run_failures[0]["run"]["run_date"][:10],
                    "first_fail_sha": run_failures[0]["run"].get("commit_sha", ""),
                    "last_pass_sha": None,
                    "blame_commits": [],
                    "post_onset_rate": post_onset_rate,
                    "confidence": "unknown",
                    "burst_p": None,
                    "p0_hat": None,
                    "runs_since_last_fail": _quiet_runs(test_name),
                    "ongoing": _quiet_runs(test_name) < REGRESSION_ONGOING_QUIET_RUNS,
                    "daily_series": window_series,
                    "onset_index": 0,
                    "history_series": history_series,
                    "history_onset_index": (
                        all_dates.index(run_key(run_failures[0]["run"], per_run))
                        if run_key(run_failures[0]["run"], per_run) in all_dates
                        else 0
                    ),
                    "note": "Already failing at start of window — extend --days for full history",
                }
            )
            continue

        last_pass = run_failures[first_fail_idx - 1]["run"]
        first_fail = run_failures[first_fail_idx]["run"]

        base_sha = last_pass.get("commit_sha", "")
        head_sha = first_fail.get("commit_sha", "")

        commits: List[Dict] = []
        if client and base_sha and head_sha and base_sha != head_sha:
            try:
                commits = client.compare_commits(base_sha, head_sha)
            except Exception as exc:
                logger.warning(
                    "blame compare_commits %s...%s failed (token may lack "
                    "Contents:Read); blame_commits will be empty: %s",
                    base_sha[:7],
                    head_sha[:7],
                    exc,
                )

        conf_label, burst_p, p0_hat = _confidence(
            test_name, run_key(first_fail, per_run)
        )
        quiet = _quiet_runs(test_name)
        blame_records.append(
            {
                "test_name": test_name,
                "fingerprint": fingerprint_for_name(test_name),
                "regression_date": first_fail["run_date"][:10],
                "last_pass_date": last_pass["run_date"][:10],
                "first_fail_sha": head_sha,
                "last_pass_sha": base_sha,
                "blame_commits": commits,
                "commit_count": len(commits),
                "post_onset_rate": post_onset_rate,
                "confidence": conf_label,
                "burst_p": burst_p,
                "p0_hat": p0_hat,
                "runs_since_last_fail": quiet,
                "ongoing": quiet < REGRESSION_ONGOING_QUIET_RUNS,
                "daily_series": window_series,
                "onset_index": first_fail_idx,
                "history_series": history_series,
                "history_onset_index": (
                    all_dates.index(run_key(first_fail, per_run))
                    if run_key(first_fail, per_run) in all_dates
                    else 0
                ),
            }
        )

    # Sort by regression date descending (newest regressions first)
    blame_records.sort(key=lambda r: r.get("regression_date", ""), reverse=True)
    return blame_records
