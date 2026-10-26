"""Run selection and column keying shared by the report/blame/scorecard views.

The dashboard treats a workflow's history as an ordered series of runs. Two
indexing modes are supported:

* **per-day** (default, for the nightly Daily workflow): runs are deduplicated
  to one per calendar day and the column key is ``YYYY-MM-DD``.
* **per-run** (for the per-commit CI workflow): every completed run is its own
  column, keyed by ``"<full-ISO-timestamp>#<run_id>"``.

Both key forms are lexicographically sortable in chronological order and are
safe as dict keys, so all downstream logic (timeline maps, ``key < onset``
comparisons, credible-interval series) works unchanged across modes. The
per-run key is timestamp-prefixed, so ``key[:10]`` still yields the run's date
(used by the freshness guard and human-readable displays).
"""

from __future__ import annotations

from typing import Dict, List

# Run statuses that are not settled pass/fail outcomes and are excluded from
# every view (mirrors the report's filter; also drops fork-PR "action_required"
# runs that would otherwise mask real scheduled/merge runs).
EXCLUDED_STATUSES = ("in_progress", "queued", "skipped", "action_required")


def run_key(run: Dict, per_run: bool) -> str:
    """Column key for a run: per-run timestamp#id, or per-day date."""
    if per_run:
        return f"{run['run_date']}#{run['run_id']}"
    return run["run_date"][:10]


def select_runs(all_runs: List[Dict], per_run: bool) -> List[Dict]:
    """Return settled runs oldest-first.

    In per-day mode, deduplicate to the first run seen per calendar day. In
    per-run mode, keep every settled run (each is its own column).
    """
    active = [r for r in all_runs if r["status"] not in EXCLUDED_STATUSES]
    active.sort(key=lambda r: (r["run_date"], r["run_id"]))
    if per_run:
        return active
    seen: set = set()
    out: List[Dict] = []
    for r in active:
        day = r["run_date"][:10]
        if day in seen:
            continue
        seen.add(day)
        out.append(r)
    return out
