"""Per-test flakiness scorecards computed from cached CI data."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from scripts.ci_health.fingerprint import fingerprint_for_name
from scripts.ci_health.log_parser import sanitize_cached_failure
from scripts.ci_health.store import Cache
from scripts.ci_health.windowing import run_key, select_runs

# A test failing this many most-recent CI days in a row is treated as an
# active regression ("persistent") rather than a flake, even if its
# all-history rate is still low (e.g. a newly-broken test).
PERSISTENT_STREAK_DAYS = 7

# A test with no failure for this many consecutive clean runs (CI days) is
# treated as presumed-fixed and moved to the collapsed "Resolved" sub-list.
# It reappears in the active roster the moment it fails again.
RESOLVED_QUIET_RUNS = 30

# A test quiet for at least this many clean runs (but not yet resolved) is
# "cooling off" -- greyed in the active roster as a soft fixed signal.
COOLING_QUIET_RUNS = 7


def _recent_streak(daily_series: List[int]) -> int:
    """Count trailing consecutive days with >=1 failure (most-recent first)."""
    streak = 0
    for v in reversed(daily_series):
        if v > 0:
            streak += 1
        else:
            break
    return streak


def _classify(days_failed: int, total_runs: int, recent_streak: int = 0) -> str:
    """Classify flakiness from failure *counts* + recent streak.

    Bands:
      * persistent: fails the majority of recorded runs (rate >= 50%) OR
        has failed every run for the last ``PERSISTENT_STREAK_DAYS``
        consecutive CI days -- actively/consistently broken, so it reads as
        a real regression rather than a flake.
      * rare: failed on exactly one CI day in all recorded history -- a
        one-off; effectively noise.
      * flaky: everything else -- recurring but intermittent.

    We classify on counts rather than a sub-1% rate because with ~daily CI
    the smallest expressible nonzero rate is 1/N (N = recorded days), so a
    hardcoded "< 1%" band is unreachable until years of history accrue.
    The rate itself is still reported for context.
    """
    rate = (days_failed / total_runs) if total_runs else 0.0
    if rate >= 0.5 or recent_streak >= PERSISTENT_STREAK_DAYS:
        return "persistent"
    if days_failed <= 1:
        return "rare"
    return "flaky"


def _trend(daily_hits: List[int]) -> float:
    """Simple linear regression slope over daily failure counts.

    Positive = getting worse, negative = improving.
    Returns 0.0 if insufficient data.
    """
    n = len(daily_hits)
    if n < 3:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(daily_hits) / n
    num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(daily_hits))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return round(num / den, 4) if den else 0.0


def _extract_category(test_name: str) -> str:
    """Categorize a failure by its test *harness*, not its directory.

    Valkey's tcl suites all boot a real server, so they are integration-
    level regardless of whether they live under ``tests/unit``,
    ``tests/unit/type`` or ``tests/integration`` -- the ``unit`` directory
    name is historical and misleading. Only the C/C++ gtest suite contains
    true unit tests, so ``unit`` is reserved for those.

      * unit        -- C/C++ gtest unit tests (the only real unit tests)
      * cluster     -- tcl tests exercising cluster mode
      * sentinel    -- tcl sentinel tests
      * modules     -- tcl module-API tests
      * integration -- all other server-booting tcl tests
      * other       -- job-level buckets (unattributed failures, sanitizer
                       / build jobs) with no tcl file
    """
    name = test_name
    # True unit tests: the C/C++ gtest binary, or a src/ path.
    if "GTest" in name or " in src/" in name:
        return "unit"
    if " in tests/" in name:
        segs = name.split(" in tests/", 1)[1].split("/")
        if "cluster" in segs:
            return "cluster"
        if "sentinel" in segs:
            return "sentinel"
        if "modules" in segs:
            return "modules"
        # unit/, unit/type/, integration/, ... are all server-booting tcl.
        return "integration"
    if "sentinel" in name.lower():
        return "sentinel"
    return "other"


def compute_scorecards(
    cache: Cache,
    days: int = 30,
    branch: str = "unstable",
    workflow: str = "daily.yml",
    repo: str = "valkey-io/valkey",
    per_run: bool = False,
    max_runs: int = 50,
) -> Dict:
    """Compute per-test flakiness scorecards.

    The failure *rate* and classification use ALL cached history (not just
    the recent ``days`` window), so the denominator grows over time and low
    rates become expressible -- with ~daily CI, a fixed window of N days
    can never express a rate below 1/N. The trend, sparkline and the
    recent-streak that drives the "persistent" badge use the recent
    ``days``-day window, so the "getting worse lately" signal stays
    recency-aware. ``runs_since_last_fail`` counts clean runs since the last
    failure; ``resolved`` (>= RESOLVED_QUIET_RUNS) flags presumed-fixed tests
    for the collapsed sub-list, and ``stale`` greys cooling-off rows.

    Returns a dict with metadata and a list of test scorecards sorted by
    failure rate (descending).
    """
    # Rate/roster span ALL cached runs; trend/sparkline span the recent window.
    runs = cache.query_runs(repo=repo, workflow=workflow, branch=branch)
    valid_runs = select_runs(runs, per_run=per_run)

    if not valid_runs:
        return {"meta": {"days": days, "total_runs": 0}, "scorecards": []}

    all_dates = [run_key(r, per_run) for r in valid_runs]
    total_runs = len(valid_runs)
    if per_run:
        recent_dates = all_dates[-max_runs:]
    else:
        recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%d"
        )
        recent_dates = [d for d in all_dates if d >= recent_cutoff]

    # Gather failures per test per date
    # test_name -> {date -> count}
    test_failures: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    # Track first/last seen
    test_first_seen: Dict[str, str] = {}
    test_last_seen: Dict[str, str] = {}

    for run in valid_runs:
        date_key = run_key(run, per_run)
        jobs = cache.query_jobs(run["run_id"], failed_only=True)
        for job in jobs:
            failures = cache.query_failures(job_id=job["job_id"])
            for f in failures:
                name = sanitize_cached_failure(f["test_name"])
                if name is None:
                    continue
                test_failures[name][date_key] += 1
                if name not in test_first_seen or date_key < test_first_seen[name]:
                    test_first_seen[name] = date_key
                if name not in test_last_seen or date_key > test_last_seen[name]:
                    test_last_seen[name] = date_key

    # Build scorecards
    scorecards: List[Dict] = []
    for test_name, date_counts in test_failures.items():
        days_failed = len(date_counts)  # over ALL history
        total_hits = sum(date_counts.values())
        failure_rate = round(days_failed / total_runs, 4)

        # Recent series drives trend + sparkline + persistent streak.
        daily_series = [date_counts.get(d, 0) for d in recent_dates]
        streak = _recent_streak(daily_series)
        last_seen = test_last_seen[test_name]
        # Clean CI runs (deduped days) since the test last failed. Because we
        # only record failures, this "quiet run count" is our proxy for fixed.
        runs_since_last_fail = sum(1 for d in all_dates if d > last_seen)
        resolved = runs_since_last_fail >= RESOLVED_QUIET_RUNS
        # "stale" (greyed) = cooling off: quiet for a while but not resolved.
        stale = runs_since_last_fail >= COOLING_QUIET_RUNS

        scorecards.append(
            {
                "test_name": test_name,
                "fingerprint": fingerprint_for_name(test_name),
                "category": _extract_category(test_name),
                "first_seen": test_first_seen[test_name],
                "last_seen": last_seen,
                "days_failed": days_failed,
                "total_hits": total_hits,
                "total_runs": total_runs,
                "failure_rate": failure_rate,
                "recent_streak": streak,
                "runs_since_last_fail": runs_since_last_fail,
                "resolved": resolved,
                "stale": stale,
                "classification": _classify(days_failed, total_runs, streak),
                "trend": _trend(daily_series),
                "daily_series": daily_series,
            }
        )

    # Sort by failure rate descending, then total hits
    scorecards.sort(key=lambda s: (-s["failure_rate"], -s["total_hits"]))

    return {
        "meta": {
            "days": days,
            "branch": branch,
            "workflow": workflow,
            "repo": repo,
            "total_runs": total_runs,
            "trend_days": days,
            "window_start": all_dates[0],
            "window_end": all_dates[-1],
            "generated": datetime.now(timezone.utc).isoformat(),
        },
        "scorecards": scorecards,
    }
