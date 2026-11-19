"""Split weekly release-branch runs into synthetic per-branch runs.

The ``weekly.yml`` workflow fires once (Sundays, from ``unstable``) and
invokes ``daily.yml`` as a reusable workflow per release branch, so all
branches' jobs land in a single workflow run distinguished only by a job
name prefix like ``run-daily-for-release-branches (8.0) / <job>``.

This module splits one such run into synthetic per-branch runs so every
downstream consumer (report, blame, scorecard) sees an ordered series of
runs per release branch, exactly like the daily and CI series.

Synthetic runs are stored with ``workflow_file = "weekly-split"`` and
``branch = "<X.Y>"``; raw weekly runs keep ``weekly.yml``/``unstable`` so
the two never mix in queries.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List

#: workflow_file value for synthetic per-branch runs in the cache.
WEEKLY_SPLIT_WORKFLOW = "weekly-split"

# Job name prefix produced by the weekly.yml matrix fan-out.
_BRANCH_PREFIX_RE = re.compile(
    r"^run-daily-for-release-branches \((\d+\.\d+)\)\s*/\s*(.*)$"
)


def parse_branch_job(job_name: str):
    """Return ``(branch, stripped_name)`` for a fan-out job, else ``None``.

    Setup jobs (e.g. ``determine-release-branches``) don't match and
    return ``None`` — they carry no per-branch signal.
    """
    m = _BRANCH_PREFIX_RE.match(job_name or "")
    if not m:
        return None
    return m.group(1), m.group(2)


def split_jobs_by_branch(jobs: List[Dict]) -> Dict[str, List[Dict]]:
    """Partition a weekly run's jobs by release branch.

    Returns ``{branch: [job, ...]}`` with each job's ``name`` stripped of
    the fan-out prefix (so it matches the equivalent daily.yml job name).
    Non-branch jobs are dropped.
    """
    by_branch: Dict[str, List[Dict]] = {}
    for job in jobs:
        parsed = parse_branch_job(job.get("name", ""))
        if parsed is None:
            continue
        branch, stripped = parsed
        j = dict(job)
        j["name"] = stripped
        by_branch.setdefault(branch, []).append(j)
    return by_branch


def synthetic_run_id(real_run_id: int, branch_index: int) -> int:
    """Deterministic run_id for a (weekly run, branch) synthetic run.

    Negative so it can never collide with real GitHub run IDs, with room
    for up to 100 branches per run.
    """
    return -(real_run_id * 100 + branch_index)


def source_run_id(run_id: int) -> int:
    """Real GitHub run id for any cached run id.

    Inverse of :func:`synthetic_run_id` for weekly-split synthetic runs
    (negative ids); real (positive) ids pass through unchanged. Used to
    build job-log URLs, which need the real workflow run id.
    """
    return run_id if run_id >= 0 else (-run_id) // 100


def build_synthetic_runs(run: Dict, by_branch: Dict[str, List[Dict]]) -> List[Dict]:
    """Build synthetic per-branch run dicts (cache schema) for one weekly run.

    Branch indices are assigned in sorted-branch order so the synthetic
    IDs are stable across re-ingestion. The per-branch status is derived
    from that branch's job conclusions. ``commit_sha`` is left empty: the
    weekly run's head SHA is the *unstable* SHA, not the release branch
    tip, and storing it would produce misleading blame/compare links.
    """
    synthetic: List[Dict] = []
    for idx, branch in enumerate(sorted(by_branch)):
        jobs = by_branch[branch]
        any_failed = any(j.get("conclusion") == "failure" for j in jobs)
        status = "failure" if any_failed else "success"
        synthetic.append(
            {
                "run_id": synthetic_run_id(run["run_id"], idx),
                "repo": run.get("repo", "valkey-io/valkey"),
                "workflow_file": WEEKLY_SPLIT_WORKFLOW,
                "status": status,
                "branch": branch,
                "commit_sha": "",
                "run_date": run["run_date"],
                "duration_secs": run.get("duration_secs"),
                "raw_json": json.dumps(
                    {"source_run_id": run["run_id"], "release_branch": branch}
                ),
            }
        )
    return synthetic
