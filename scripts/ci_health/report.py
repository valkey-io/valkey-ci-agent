"""HTML report generator for Valkey CI failure trends."""

from __future__ import annotations

import html
import json
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from string import Template
from typing import Dict, List, Optional

from scripts.ci_health.blame import REGRESSION_ONGOING_QUIET_RUNS, compute_blame
from scripts.ci_health.log_parser import sanitize_cached_failure
from scripts.ci_health.scorecard import (
    COOLING_QUIET_RUNS,
    PERSISTENT_STREAK_DAYS,
    RESOLVED_QUIET_RUNS,
    compute_scorecards,
)
from scripts.ci_health.stats import regression_rate_lower_bound
from scripts.ci_health.store import Cache
from scripts.ci_health.weekly import source_run_id
from scripts.ci_health.windowing import run_key, select_runs

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"
logger = logging.getLogger(__name__)

# Freshness guard: how many days the newest run may lag before the report is
# considered stale. The daily dashboard normally sees a run from today or
# yesterday, so a gap beyond this means the feed has stalled (dead token or
# upstream CI stopped). Kept generous so a single skipped upstream night does
# not false-alarm; an expired token is caught immediately by the sync's
# auth_failed flag regardless of this threshold.
MAX_RUN_AGE_DAYS = 2

# Heatmap regression-warning gate (effect-size, not significance). Flag an
# ongoing regression only when we are HEATMAP_WARN_CI confident its post-onset
# failure rate is at least HEATMAP_WARN_MEANINGFUL_RATE, with a minimum number
# of failures as an evidence floor. Tunable after seeing it live.
HEATMAP_WARN_CI = 0.90
HEATMAP_WARN_MEANINGFUL_RATE = 0.05
HEATMAP_WARN_MIN_FAILURES = 2

# Collapse heatmap rows with identical failure-day signatures into one
# expandable cluster row when at least this many tests share the signature
# (a single regression breaking many tests at once floods the table).
HEATMAP_CLUSTER_MIN = 5

# Heatmap trend sparkline: 7-day moving-average overlay, and the half-life
# for the recency-weighted (EWMA) flakiness number in the tooltip. The decay
# lets a formerly-flaky test that got fixed read as reliable within a few
# weeks, unlike a flat 90-day average.
HEATMAP_SPARK_MA_WINDOW = 7
HEATMAP_EWMA_HALF_LIFE = 21


def stale_reason(
    latest_run_date: str | None,
    *,
    now: datetime | None = None,
    max_age_days: int = MAX_RUN_AGE_DAYS,
) -> str | None:
    """Return a human-readable reason if the report data is stale, else None.

    *latest_run_date* is the ``YYYY-MM-DD`` date of the newest run in the
    report (``summary['latest_run_date']``), or None when there are no runs.
    A truthy return value means the caller should fail loudly (non-zero exit)
    so a silent sync failure becomes visible instead of redeploying stale data.
    """
    if not latest_run_date:
        return "report has no runs — sync likely failed (dead token or empty cache)"
    now = now or datetime.now(timezone.utc)
    try:
        latest = datetime.strptime(latest_run_date[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return f"unparseable latest_run_date: {latest_run_date!r}"
    age_days = (now.date() - latest.date()).days
    if age_days > max_age_days:
        return (
            f"newest run is {age_days} days old ({latest_run_date}); expected "
            f"within {max_age_days} — sync likely stalled "
            f"(dead token or upstream CI stopped)"
        )
    return None


def _asset(name: str) -> str:
    """Read a bundled static asset (CSS/JS) shipped alongside this module."""
    return (_ASSETS_DIR / name).read_text(encoding="utf-8")


# Shared table heads for scorecard / regression tables (also used by the
# releases page renderer).
SC_HEAD = (
    "<thead><tr><th>#</th><th>Test</th><th>Class</th><th>Trend</th>"
    '<th>Category</th><th>Rate</th><th title="runs failed / total runs">Runs</th>'
    "<th>Recent activity</th></tr></thead>"
)
REG_HEAD = (
    "<thead><tr><th>Test</th><th>First failed</th><th>Last passed</th>"
    "<th>Suspect range</th><th>Baseline</th><th>Surprise</th>"
    "<th>Onset</th><th>Post-onset</th></tr></thead>"
)


def job_log_url(repo: str, run_id: int, job_id: int) -> str:
    """GitHub job-log page URL, valid for real and weekly-split run ids."""
    return (
        f"https://github.com/{repo}/actions/runs/{source_run_id(run_id)}/job/{job_id}"
    )


def generate_report_data(
    cache: Cache,
    days: int = 30,
    branch: str = "unstable",
    workflow: str = "daily.yml",
    repo: str = "valkey-io/valkey",
    client=None,
    per_run: bool = False,
    max_runs: int = 50,
) -> Dict:
    """Build the data structure for the failure trend report.

    If *client* (a ``GitHubActionsClient``) is provided, the report will
    include the list of commits between consecutive runs.

    With ``per_run=True`` (CI mode), every completed run is its own column and
    the window is the last ``max_runs`` runs. Otherwise (Daily mode) runs are
    deduplicated to one per calendar day over the last ``days`` days.
    """
    if per_run:
        all_runs = cache.query_runs(repo=repo, workflow=workflow, branch=branch)
        runs = select_runs(all_runs, per_run=True)[-max_runs:]
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
            "%Y-%m-%dT00:00:00Z"
        )
        all_runs = cache.query_runs(
            repo=repo, workflow=workflow, branch=branch, since=since
        )
        runs = select_runs(all_runs, per_run=False)

    # For each run, gather jobs and failures
    run_details: List[Dict] = []
    # test_name -> [{run_date, status, error_summary, job_names}]
    test_timeline: Dict[str, Dict[str, Dict]] = defaultdict(dict)

    for run in runs:
        rid = run["run_id"]
        date_key = run_key(run, per_run)
        all_jobs = cache.query_jobs(rid)
        failed_jobs = cache.query_jobs(rid, failed_only=True)

        run_info = {
            "run_id": rid,
            "date": date_key,
            "day": run["run_date"][:10],
            "status": run["status"],
            "commit_sha": run.get("commit_sha", ""),
            "total_jobs": len(all_jobs),
            "failed_jobs": len(failed_jobs),
            "failed_job_names": sorted(j["name"] for j in failed_jobs),
            "failed_job_urls": [
                job_log_url(repo, rid, j["job_id"])
                for j in sorted(failed_jobs, key=lambda j: j["name"])
            ],
        }

        # Gather failures for this run
        run_failures: List[Dict] = []
        for j in failed_jobs:
            for f in cache.query_failures(job_id=j["job_id"]):
                # Clean stale cached noise at display time (mirrors the parser).
                clean = sanitize_cached_failure(f["test_name"])
                if clean is None:
                    continue
                run_failures.append({**f, "test_name": clean, "job_name": j["name"]})

        # Group by test_name
        by_test: Dict[str, List[Dict]] = defaultdict(list)
        for f in run_failures:
            by_test[f["test_name"]].append(f)

        run_info["unique_failures"] = len(by_test)
        run_info["failure_names"] = sorted(by_test.keys())
        # Map each failure name to its job_ids for linking
        run_info["failure_jobs"] = {
            test_name: sorted(set(inst["job_id"] for inst in instances))
            for test_name, instances in by_test.items()
        }
        run_details.append(run_info)

        # Record in timeline
        for test_name, instances in by_test.items():
            error_summaries = sorted(
                set(inst["error_summary"][:120] for inst in instances)
            )
            job_names = sorted(set(inst["job_name"] for inst in instances))
            job_urls = [
                job_log_url(repo, rid, jid)
                for jid in sorted(set(inst["job_id"] for inst in instances))
            ]
            test_timeline[test_name][date_key] = {
                "count": len(instances),
                "errors": error_summaries,
                "jobs": job_names,
                "job_urls": job_urls,
            }

    # Sort tests by total failure count (most frequent first)
    test_totals = {
        name: sum(d["count"] for d in dates.values())
        for name, dates in test_timeline.items()
    }
    sorted_tests = sorted(test_totals.keys(), key=lambda n: -test_totals[n])

    dates = [r["date"] for r in run_details]

    # Per-column descriptors so the renderer can label headers generically:
    # per-day mode -> M/D label; per-run mode -> short commit SHA.
    columns: List[Dict] = []
    for r in run_details:
        key = r["date"]
        day = r.get("day", key[:10])
        if per_run:
            sha = r.get("commit_sha", "") or ""
            columns.append(
                {
                    "key": key,
                    "label": sha[:7] if sha else day,
                    "title": f"{day} · {sha[:10]}" if sha else day,
                }
            )
        else:
            columns.append(
                {"key": key, "label": f"{int(key[5:7])}/{int(key[8:10])}", "title": key}
            )

    # Record each run's previous SHA so the report can render a GitHub compare
    # link (needs no API/token) even when commit lists aren't fetched.
    for i in range(1, len(run_details)):
        run_details[i]["prev_sha"] = run_details[i - 1]["commit_sha"]

    # Fetch commits between consecutive runs if a client is provided
    if client:
        for i in range(1, len(run_details)):
            prev_sha = run_details[i - 1]["commit_sha"]
            curr_sha = run_details[i]["commit_sha"]
            if prev_sha and curr_sha and prev_sha != curr_sha:
                try:
                    commits = client.compare_commits(prev_sha, curr_sha)
                    run_details[i]["commits_since_prev"] = commits
                except Exception as exc:
                    logger.warning(
                        "compare_commits %s...%s failed (token may lack "
                        "Contents:Read); commit list will be empty: %s",
                        prev_sha[:7],
                        curr_sha[:7],
                        exc,
                    )
                    run_details[i]["commits_since_prev"] = []
            else:
                run_details[i]["commits_since_prev"] = []

        # Fetch commit messages for each unique SHA
        seen_shas: Dict[str, str] = {}
        for run in run_details:
            sha = run.get("commit_sha", "")
            if sha and sha not in seen_shas:
                try:
                    info = client.get_commit(sha)
                    seen_shas[sha] = info.get("message_full", "")
                except Exception as exc:
                    logger.warning("get_commit %s failed: %s", sha[:7], exc)
                    seen_shas[sha] = ""
            run["commit_message"] = seen_shas.get(sha, "")

    # Compute long-term (90-day) failure rates for each test
    long_term_since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime(
        "%Y-%m-%dT00:00:00Z"
    )
    lt_runs = cache.query_runs(
        repo=repo, workflow=workflow, branch=branch, since=long_term_since
    )
    lt_seen_dates: set[str] = set()
    for r in lt_runs:
        if r["status"] not in ("in_progress", "queued", "skipped", "action_required"):
            lt_seen_dates.add(r["run_date"][:10])
    lt_total = len(lt_seen_dates) or 1

    # Count days each test failed in the 90-day window
    lt_test_days: Dict[str, set] = defaultdict(set)
    for r in lt_runs:
        if r["status"] in ("in_progress", "queued", "skipped", "action_required"):
            continue
        date_key = r["run_date"][:10]
        for j in cache.query_jobs(r["run_id"], failed_only=True):
            for f in cache.query_failures(job_id=j["job_id"]):
                clean = sanitize_cached_failure(f["test_name"])
                if clean is None:
                    continue
                lt_test_days[clean].add(date_key)

    return {
        "dates": dates,
        "columns": columns,
        "per_run": per_run,
        "runs": run_details,
        "tests": {
            name: {
                "total": test_totals[name],
                "days_failed": len(test_timeline[name]),
                "score_90d": round(
                    len(lt_test_days.get(name, set())) / lt_total * 100, 1
                ),
                "timeline": {d: test_timeline[name].get(d) for d in dates},
            }
            for name in sorted_tests
        },
        "summary": {
            "days": days,
            "repo": repo,
            "branch": branch,
            "workflow": workflow,
            "total_runs": len(run_details),
            "failed_runs": sum(1 for r in run_details if r["status"] == "failure"),
            "unique_tests_failed": len(sorted_tests),
            # Newest run date (YYYY-MM-DD) in the window, or None if empty.
            # Consumed by stale_reason() to fail loudly on a stalled sync.
            "latest_run_date": dates[-1] if dates else None,
        },
        # Full 90-day flakiness roster (the "board of shame"), independent of
        # the recent-window heatmap above. Ranked worst-first by compute_scorecards.
        "scorecard": compute_scorecards(
            cache,
            days=90,
            branch=branch,
            workflow=workflow,
            repo=repo,
            per_run=per_run,
            max_runs=max_runs,
        ),
        # Detected green->red regressions (blame). compute_blame is client-safe:
        # without commit-API access, blame_commits is empty but the transition
        # SHAs remain, which is all the compare-link view needs.
        "regressions": compute_blame(
            cache,
            client,
            days=90,
            branch=branch,
            workflow=workflow,
            repo=repo,
            per_run=per_run,
            max_runs=max_runs,
        ),
    }


def _render_heatmap_table(data: Dict) -> str:
    """Render one workflow's failure heatmap as a self-contained <table>.

    Column headers come from ``data['columns']`` (M/D in per-day mode, short
    commit SHA in per-run mode); warning markers are computed from this
    dataset's own regressions.
    """
    columns = data.get("columns") or [
        {"key": k, "label": k, "title": k} for k in data.get("dates", [])
    ]
    dates = data.get("dates", [])
    tests = data.get("tests", {})
    per_run = data.get("per_run", False)
    runs = data.get("runs", [])
    reg_warnings = _regression_warnings(data.get("regressions", []))
    reg_by_name = {r["test_name"]: r for r in data.get("regressions", [])}

    date_headers = ""
    col_cls = "date-col rot" if per_run else "date-col"
    for c in columns:
        label = html.escape(c.get("label", c["key"]))
        cell = f"<span>{label}</span>" if per_run else label
        date_headers += (
            f'<th class="{col_cls}" title="{html.escape(c.get("title", c["key"]))}">'
            f"{cell}</th>"
        )

    run_status_cells = ""
    for run in runs:
        cls = "pass" if run["status"] == "success" else "fail"
        day = run.get("day", run["date"][:10])
        title = (
            f"{day}: {run['status']} "
            f"({run['failed_jobs']}/{run['total_jobs']} jobs failed)"
        )
        run_status_cells += f'<td class="cell {cls}" title="{html.escape(title)}"></td>'

    # Trend sparkline data: the embedded 90-day scorecard already carries a
    # per-day failure series and quiet-run counts for every test in it.
    sc_by_name = {
        sc["test_name"]: sc for sc in data.get("scorecard", {}).get("scorecards", [])
    }
    trend_hdr = "trend" if per_run else "90d trend"
    trend_ttl = (
        "Per-run failures over the scorecard window; blue line = "
        f"{HEATMAP_SPARK_MA_WINDOW}-run moving average"
        if per_run
        else f"Daily failures over the last 90 days; blue line = "
        f"{HEATMAP_SPARK_MA_WINDOW}-day moving average"
    )
    unit = "runs" if per_run else "days"

    def _trend_cell(series: List[int], sc: Dict | None, prefix: str = "") -> str:
        """One spark <td>: bars + MA line, EWMA/recency facts in the tooltip."""
        ewma_pct = _ewma_rate([1 if v else 0 for v in series]) * 100
        fdays = sum(1 for v in series if v)
        parts = [
            f"{prefix}flakiness {ewma_pct:.0f}% (recent-weighted, "
            f"{HEATMAP_EWMA_HALF_LIFE}-{unit[:-1]} half-life)"
        ]
        if sc is not None:
            quiet = sc.get("runs_since_last_fail", 0)
            parts.append(
                "last failed in the latest run"
                if quiet == 0
                else f"last failed {quiet} runs ago"
            )
        parts.append(f"failed {fdays} of {len(series)} {unit} in window")
        tip = html.escape(" · ".join(parts))
        return (
            f'<td class="spark-cell" title="{tip}">'
            f"{_sparkline(series, ma_window=HEATMAP_SPARK_MA_WINDOW)}</td>"
        )

    test_rows = ""

    def _test_row(test_name: str, info: Dict, row_attrs: str = "") -> str:
        """Render one test's <tr>. *row_attrs* is injected into the tag."""
        short_name = _short_test_name(test_name)
        sc = sc_by_name.get(test_name)
        series = (sc or {}).get("daily_series", [])
        cells = ""
        for d in dates:
            entry = info["timeline"][d]
            if entry is None:
                cells += '<td class="cell none" title="no failure"></td>'
            else:
                n = entry["count"]
                jobs = ", ".join(entry["jobs"][:3])
                if len(entry["jobs"]) > 3:
                    jobs += f" +{len(entry['jobs']) - 3}"
                errs = "; ".join(entry["errors"][:2])
                urls = entry.get("job_urls") or []
                extra = " · click for job log" if urls else ""
                tip = html.escape(f"{n}x\nJobs: {jobs}\nError: {errs}{extra}")
                if urls:
                    cells += (
                        f'<td class="cell fail" title="{tip}">'
                        f'<a href="{urls[0]}" target="_blank" '
                        f'rel="noopener noreferrer">{n}</a></td>'
                    )
                else:
                    cells += f'<td class="cell fail" title="{tip}">{n}</td>'
        warn_lb = reg_warnings.get(test_name)
        warn_marker = ""
        if warn_lb is not None:
            onset = reg_by_name.get(test_name, {}).get("regression_date", "?")
            wtip = html.escape(
                f"Likely regression — 90% confident it now fails "
                f">={warn_lb * 100:.0f}% of runs since {onset}. Click for details."
            )
            warn_marker = f'<a class="regwarn" href="#regressions" title="{wtip}">⚠️</a>'
        return f"""<tr{row_attrs}>
            <td class="test-name" title="{html.escape(test_name)}">{warn_marker}{html.escape(short_name)}</td>
            {_trend_cell(series, sc)}
            {cells}
        </tr>"""

    # Cluster rows with identical failure-day signatures. One regression
    # breaking many tests at once produces a block of visually identical
    # rows; collapse clusters of HEATMAP_CLUSTER_MIN+ under a single
    # expandable summary row so the table stays scannable.
    sig_members: Dict[tuple, List[str]] = defaultdict(list)
    for test_name, info in tests.items():
        sig = tuple(d for d in dates if info["timeline"][d] is not None)
        if sig:
            sig_members[sig].append(test_name)
    clustered: Dict[str, int] = {}  # member test_name -> group id
    groups: List[List[str]] = []
    for sig, members in sig_members.items():
        if len(members) >= HEATMAP_CLUSTER_MIN:
            for m in members:
                clustered[m] = len(groups)
            groups.append(members)

    def _cluster_row(gid: int, members: List[str]) -> str:
        """Render a cluster's collapsed summary row (toggled by report.js)."""
        n = len(members)
        first = tests[members[0]]
        sig_set = {d for d in dates if first["timeline"][d] is not None}
        cells = ""
        for d in dates:
            if d in sig_set:
                cells += (
                    f'<td class="cell fail" '
                    f'title="{n} tests failed · click row to expand">{n}</td>'
                )
            else:
                cells += '<td class="cell none" title="no failure"></td>'
        freq_pct = round(len(sig_set) / len(dates) * 100) if dates else 0
        # Envelope trend: element-wise max of the members' daily series,
        # representing the cluster's shared failure pattern honestly.
        member_series = [
            s for m in members if (s := sc_by_name.get(m, {}).get("daily_series", []))
        ]
        env: List[int] = []
        if member_series:
            longest = max(len(s) for s in member_series)
            padded = [[0] * (longest - len(s)) + s for s in member_series]
            env = [max(col) for col in zip(*padded)]
        warn_marker = ""
        warned = [m for m in members if m in reg_warnings]
        if warned:
            wtip = html.escape(
                f"{len(warned)} of these tests flagged as likely regressions. "
                "Click for details."
            )
            warn_marker = f'<a class="regwarn" href="#regressions" title="{wtip}">⚠️</a>'
        preview = ", ".join(_short_test_name(m) for m in members[:3])
        if n > 3:
            preview += f", … +{n - 3} more"
        return f"""<tr class="hm-cluster-toggle" data-group="{gid}" title="{html.escape(preview)}">
            <td class="test-name"><span class="hm-caret">▸</span> {warn_marker}{n} tests · identical failure days</td>
            {_trend_cell(env, None, prefix=f"cluster envelope ({freq_pct}% of shown {unit}) · ")}
            {cells}
        </tr>"""

    emitted: set = set()
    for test_name, info in tests.items():
        gid = clustered.get(test_name)
        if gid is None:
            test_rows += _test_row(test_name, info)
        elif gid not in emitted:
            emitted.add(gid)
            test_rows += _cluster_row(gid, groups[gid])
            for m in groups[gid]:
                test_rows += _test_row(
                    m,
                    tests[m],
                    row_attrs=(
                        f' class="hm-cluster-member" data-group="{gid}"'
                        ' style="display:none"'
                    ),
                )

    if per_run:
        cap_lead = "Columns are merge runs (one per commit)"
    else:
        cap_lead = "Columns are days"
    cluster_note = (
        f" <b>{sum(len(g) for g in groups)} tests</b> with identical failure "
        "days are collapsed into cluster rows — click a ▸ row to expand."
        if groups
        else ""
    )
    caption = (
        '<caption class="hint" style="text-align:left; caption-side:top; '
        'margin-bottom:8px;">'
        f"{cap_lead}, rows are unique test failures. Each cell shows how many "
        "jobs hit that failure. "
        '<span style="display:inline-block; width:10px; height:10px; '
        'background:#da3633; border-radius:2px; vertical-align:middle;"></span> failed '
        '<span style="display:inline-block; width:10px; height:10px; '
        'background:#238636; border-radius:2px; vertical-align:middle;"></span> passed '
        '<span style="display:inline-block; width:10px; height:10px; '
        'background:#21262d; border-radius:2px; vertical-align:middle;"></span> no failure.'
        " A red <b>Run status</b> with no test cells below it means the run "
        "failed on build / sanitizer / setup / timeout — no individual test failed."
        f"{cluster_note}"
        "</caption>"
    )
    scroll_cls = "heatmap-scroll scroll-right" if per_run else "heatmap-scroll"
    wrap_open = '<div class="heatmap-scroll-wrap has-fade">' if per_run else ""
    wrap_close = "</div>" if per_run else ""
    return f"""{wrap_open}<div class="{scroll_cls}"><table data-total-cols="{len(columns)}">
  {caption}
  <thead>
    <tr><th class="test-name">Test</th><th class="spark-col" title="{trend_ttl}">{trend_hdr}</th>{date_headers}</tr>
    <tr><td class="test-name" style="color:#8b949e">Run status</td><td></td>{run_status_cells}</tr>
  </thead>
  <tbody>
    {test_rows}
  </tbody>
</table></div>{wrap_close}"""


def render_html(
    data: Dict, ci_data: Dict | None = None, releases_strip: str = ""
) -> str:
    """Render the report data as a self-contained HTML file.

    *data* is the Daily (nightly, per-day) report. When *ci_data* (the CI,
    per-run report) is provided, each tab stacks the CI view on top of Daily.
    *releases_strip* is pre-rendered release-health badge HTML (from
    releases.render_release_strip); empty when there is no weekly data.
    """
    summary = data["summary"]
    runs = data["runs"]
    repo = summary.get("repo", "valkey-io/valkey")

    # --- Heatmap panel body: CI on top, Daily below (collapsed) ---
    if ci_data:
        n_ci = len(ci_data.get("columns", []))
        heatmap_body = (
            f'<h3 class="wf-title">CI · last {n_ci} merge runs (per commit) '
            "— freshest signal</h3>"
            + _render_heatmap_table(ci_data)
            + '<details class="wf-daily" open><summary>Daily · nightly full suite '
            f"(last {summary.get('days', 14)} days)</summary>"
            + _render_heatmap_table(data)
            + "</details>"
        )
    else:
        heatmap_body = _render_heatmap_table(data)

    # Build per-run detail rows for the bottom table
    run_detail_rows = ""
    for run in reversed(runs):  # newest first
        if run["status"] == "success":
            status_badge = '<span class="badge pass">PASS</span>'
        else:
            status_badge = f'<span class="badge fail">FAIL ({run["failed_jobs"]}/{run["total_jobs"]})</span>'
        jobs_list = run["failed_job_names"][:5]
        jobs_urls = run.get("failed_job_urls", [])[:5]
        jobs_extra = len(run["failed_job_names"]) - 5
        if jobs_list:
            jobs_html = '<div class="job-list">'
            for k, jn in enumerate(jobs_list):
                label = html.escape(jn)
                if k < len(jobs_urls):
                    label = (
                        f'<a class="job-link" href="{jobs_urls[k]}" target="_blank" '
                        f'rel="noopener noreferrer">{label} ↗</a>'
                    )
                jobs_html += f'<div class="job-entry">{label}</div>'
            if jobs_extra > 0:
                jobs_html += f'<div class="job-entry" style="color:#8b949e">+{jobs_extra} more</div>'
            jobs_html += "</div>"
        else:
            jobs_html = "—"
        sha = run.get("commit_sha", "")
        commit_msg = run.get("commit_message", "") or ""
        sha_link = _commit_link(sha, repo, title=commit_msg) if sha else "—"

        # Commits since previous run. The compare LINK needs no API/token —
        # it just points the browser at GitHub's diff between the two SHAs.
        prev_sha = run.get("prev_sha", "")
        compare_link = ""
        if prev_sha and sha and prev_sha != sha:
            compare_link = (
                f'<a class="job-link" '
                f'href="https://github.com/{repo}/compare/{prev_sha}...{sha}" '
                f'target="_blank" rel="noopener noreferrer" '
                f'title="commits between the previous run and this one">'
                f"{prev_sha[:7]}…{sha[:7]} ↗</a>"
            )
        commits = run.get("commits_since_prev", [])
        if commits:
            commits_html = '<div class="commit-list">'
            for c in commits:
                full_msg = c.get("message", "")
                c_link = _commit_link(c["sha"], repo, title=full_msg)
                author = html.escape(c.get("author", "")[:20])
                msg_short = html.escape(full_msg[:80])
                msg_tip = html.escape(full_msg, quote=True)
                commits_html += (
                    f'<div class="commit-entry">{c_link} '
                    f'<span class="commit-author">{author}</span> '
                    f'<span title="{msg_tip}">{msg_short}</span></div>'
                )
            if compare_link:
                commits_html += f'<div class="commit-entry">{compare_link}</div>'
            commits_html += "</div>"
        elif compare_link:
            commits_html = f'<div class="commit-list">{compare_link}</div>'
        else:
            commits_html = '<span class="no-commits">—</span>'

        run_detail_rows += f"""<tr>
            <td>{run["date"]}</td>
            <td>{status_badge}</td>
            <td>{sha_link}</td>
            <td>{run["unique_failures"]}</td>
            <td class="failures-cell">{_render_failure_names(run.get("failure_names", []), run.get("failure_jobs", {}), repo, run.get("run_id", 0))}</td>
            <td class="jobs-cell">{jobs_html}</td>
            <td class="commits-cell">{commits_html}</td>
        </tr>"""

    all_scorecards = data.get("scorecard", {}).get("scorecards", [])
    active = [s for s in all_scorecards if not s.get("resolved")]
    resolved = [s for s in all_scorecards if s.get("resolved")]
    scorecard_rows = _render_scorecard_rows(active)
    resolved_section = _render_resolved_section(resolved)
    all_regs = data.get("regressions", [])
    ongoing = [r for r in all_regs if r.get("ongoing", True)]
    fixed = [r for r in all_regs if not r.get("ongoing", True)]
    regressions_rows = _render_regression_rows(ongoing, repo)
    fixed_regressions = _render_fixed_regressions(fixed, repo)

    # --- CI (per-run) blocks stacked ABOVE the Daily views ---
    ci_scorecard = ""
    ci_regressions = ""
    if ci_data:
        ci_sc = ci_data.get("scorecard", {}).get("scorecards", [])
        ci_active = [s for s in ci_sc if not s.get("resolved")]
        ci_scorecard = (
            '<h3 class="wf-title">CI · per-commit flakiness</h3>'
            '<table class="scorecard-table">'
            f"{SC_HEAD}<tbody>{_render_scorecard_rows(ci_active)}</tbody></table>"
            '<h3 class="wf-title">Daily · nightly full suite</h3>'
        )
        ci_regs = ci_data.get("regressions", [])
        ci_ongoing = [r for r in ci_regs if r.get("ongoing", True)]
        ci_fixed = [r for r in ci_regs if not r.get("ongoing", True)]
        ci_regressions = (
            '<h3 class="wf-title">CI · per-commit regressions</h3>'
            '<table class="scorecard-table">'
            f"{REG_HEAD}<tbody>{_render_regression_rows(ci_ongoing, repo)}</tbody>"
            "</table>"
            f"{_render_fixed_regressions(ci_fixed, repo)}"
            '<h3 class="wf-title">Daily · nightly full suite</h3>'
        )

    return Template(_HTML_TEMPLATE).substitute(
        styles=_asset("report.css"),
        script=_asset("report.js"),
        repo=html.escape(repo),
        branch=html.escape(summary["branch"]),
        workflow=html.escape(summary["workflow"]),
        days=summary["days"],
        total_runs=summary["total_runs"],
        failed_runs=summary["failed_runs"],
        unique_tests=summary["unique_tests_failed"],
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        heatmap_body=heatmap_body,
        releases_strip=releases_strip,
        ci_scorecard=ci_scorecard,
        ci_regressions=ci_regressions,
        run_detail_rows=run_detail_rows,
        scorecard_rows=scorecard_rows,
        resolved_section=resolved_section,
        regressions_rows=regressions_rows,
        fixed_regressions=fixed_regressions,
        persistent_streak=PERSISTENT_STREAK_DAYS,
        cooling_runs=COOLING_QUIET_RUNS,
        resolved_runs=RESOLVED_QUIET_RUNS,
        warn_ci=f"{HEATMAP_WARN_CI * 100:.0f}%",
        warn_rate=f"{HEATMAP_WARN_MEANINGFUL_RATE * 100:.0f}%",
        warn_min_fails=HEATMAP_WARN_MIN_FAILURES,
        report_json=html.escape(json.dumps(data, indent=2)),
    )


def render_markdown(data: Dict) -> str:
    """Render the report data as GitHub-Flavored Markdown."""
    summary = data["summary"]
    repo = summary.get("repo", "valkey-io/valkey")
    dates = data["dates"]
    tests = data["tests"]
    runs = data["runs"]

    lines: List[str] = []
    lines.append("# Valkey CI Failure Report")
    lines.append("")
    lines.append(
        f"`{summary['workflow']}` · `{summary['branch']}` · `{repo}` · "
        f"last {summary['days']} days · generated "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    )
    lines.append("")
    lines.append(
        f"| Runs | Failed | Unique failures |\n"
        f"|------|--------|----------------|\n"
        f"| {summary['total_runs']} | {summary['failed_runs']} "
        f"| {summary['unique_tests_failed']} |"
    )

    # Failure heatmap table
    if tests:
        lines.append("")
        lines.append("## Failure Heatmap")
        lines.append("")
        day_labels = [d[5:] for d in dates]  # "04-01" from "2026-04-01"
        header = "| Test | Freq | " + " | ".join(day_labels) + " |"
        sep = "|------|------|" + "|".join("---" for _ in dates) + "|"
        lines.append(header)
        lines.append(sep)
        for test_name, info in tests.items():
            short = _short_test_name(test_name)
            freq = f"{info['days_failed']}/{len(dates)}d"
            cells = []
            for d in dates:
                entry = info["timeline"][d]
                if entry is None:
                    cells.append("·")
                else:
                    cells.append(f"**{entry['count']}**")
            lines.append(f"| `{short}` | {freq} | " + " | ".join(cells) + " |")

    # Run details
    lines.append("")
    lines.append("## Run Details (newest first)")
    lines.append("")
    lines.append("| Date | Status | Commit | # | Failures |")
    lines.append("|------|--------|--------|---|----------|")
    for run in reversed(runs):
        status = (
            "✅"
            if run["status"] == "success"
            else f"❌ {run['failed_jobs']}/{run['total_jobs']}"
        )
        sha = run.get("commit_sha", "")
        sha_md = (
            f"[`{sha[:7]}`](https://github.com/{repo}/commit/{sha})" if sha else "—"
        )
        run_id = run.get("run_id", 0)
        failure_jobs = run.get("failure_jobs", {})
        failure_parts = []
        for n in run.get("failure_names", [])[:5]:
            short = f"`{_short_test_name(n)}`"
            job_ids = failure_jobs.get(n, [])
            if job_ids:
                job_links = "".join(
                    f"[[{i + 1}]](https://github.com/{repo}/actions/runs/{run_id}/job/{jid})"
                    for i, jid in enumerate(job_ids)
                )
                short += f" {job_links}"
            failure_parts.append(short)
        if len(run.get("failure_names", [])) > 5:
            failure_parts.append(f"+{len(run['failure_names']) - 5} more")
        failures = ", ".join(failure_parts) if failure_parts else "—"
        lines.append(
            f"| {run['date']} | {status} | {sha_md} | {run['unique_failures']} | {failures} |"
        )

    lines.append("")
    return "\n".join(lines)



def _short_test_name(name: str) -> str:
    """Shorten a test name for display in the grid."""
    # Strip " in tests/..." suffix for the grid, keep it in the tooltip
    if " in tests/" in name:
        name = name.split(" in tests/")[0]
    # Strip "(exception)" suffix
    name = name.replace(" (exception)", "")
    # Truncate
    if len(name) > 60:
        name = name[:57] + "..."
    return name


def _commit_link(sha: str, repo: str = "valkey-io/valkey", title: str = "") -> str:
    """Render a short commit SHA as a GitHub link."""
    if not sha:
        return ""
    short = sha[:7]
    tip = html.escape(title, quote=True) if title else sha
    return (
        f'<a href="https://github.com/{repo}/commit/{sha}" '
        f'class="sha" title="{tip}">{short}</a>'
    )


def _render_failure_names(
    names: List[str],
    failure_jobs: Optional[Dict[str, List[int]]] = None,
    repo: str = "valkey-io/valkey",
    run_id: int = 0,
) -> str:
    """Render a list of failure names with linked job IDs."""
    if not names:
        return "—"
    failure_jobs = failure_jobs or {}
    items = ""
    for n in names:
        short = html.escape(_short_test_name(n))
        job_ids = failure_jobs.get(n, [])
        if job_ids:
            links = " ".join(
                f'<a href="https://github.com/{repo}/actions/runs/{run_id}/job/{jid}" '
                f'class="job-link">[{i + 1}]</a>'
                for i, jid in enumerate(job_ids)
            )
            items += f'<div class="failure-entry">{short} {links}</div>'
        else:
            items += f'<div class="failure-entry">{short}</div>'
    return f'<div class="failure-list">{items}</div>'


def _ewma_rate(series: List[int], half_life: int = HEATMAP_EWMA_HALF_LIFE) -> float:
    """Recency-weighted failure rate over a per-day series (newest last).

    Each day contributes its failed/not-failed bit weighted by
    ``0.5 ** (age / half_life)``, so recent behavior dominates: a test that
    was flaky but got fixed decays toward 0 within a few half-lives, while
    a chronically flaky test holds its level.
    """
    if not series:
        return 0.0
    n = len(series)
    wsum = 0.0
    fsum = 0.0
    for i, v in enumerate(series):
        w = 0.5 ** ((n - 1 - i) / half_life)
        wsum += w
        if v:
            fsum += w
    return fsum / wsum if wsum else 0.0


def _sparkline(
    series: List[int],
    width: int = 90,
    height: int = 16,
    mark_index: int | None = None,
    ma_window: int | None = None,
) -> str:
    """Render a per-day failure-count series as a compact inline SVG bar chart.

    If *mark_index* is given, a vertical tick is drawn at that bar to mark a
    regime change (e.g. a regression's onset run). If *ma_window* is given,
    a thin trailing moving-average polyline is drawn over the bars.
    """
    if not series:
        return ""
    n = len(series)
    mx = max(series) or 1
    step = width / n
    bw = max(0.6, step * 0.8)
    bars = []
    for i, v in enumerate(series):
        h = round(v / mx * (height - 2)) if v else 0
        x = i * step
        y = height - max(h, 1)
        color = "#da3633" if v else "#30363d"
        bars.append(
            f'<rect x="{x:.2f}" y="{y}" width="{bw:.2f}" '
            f'height="{max(h, 1)}" fill="{color}"/>'
        )
    if mark_index is not None and 0 <= mark_index < n:
        mx_x = mark_index * step
        bars.append(
            f'<rect x="{max(0.0, mx_x - 1):.2f}" y="0" width="2" height="{height}" '
            f'fill="#d29922"><title>onset</title></rect>'
        )
    if ma_window and n >= 2:
        pts = []
        for i in range(n):
            lo = max(0, i - ma_window + 1)
            avg = sum(series[lo : i + 1]) / (i - lo + 1)
            x = i * step + bw / 2
            y_avg = height - 1 - (avg / mx) * (height - 3)
            pts.append(f"{x:.2f},{y_avg:.2f}")
        bars.append(
            f'<polyline points="{" ".join(pts)}" fill="none" '
            f'stroke="#58a6ff" stroke-width="1" vector-effect="non-scaling-stroke"/>'
        )
    return (
        f'<svg class="spark" width="100%" height="{height}" '
        f'viewBox="0 0 {width} {height}" preserveAspectRatio="none">'
        f"{''.join(bars)}</svg>"
    )


def _render_scorecard_rows(scorecards: List[Dict]) -> str:
    """Render the flaky-test leaderboard rows (ranked worst-first)."""
    rows = ""
    for i, sc in enumerate(scorecards, 1):
        name = sc["test_name"]
        short = _short_test_name(name)
        cls = sc.get("classification", "rare")
        trend = sc.get("trend", 0.0)
        cat = sc.get("category", "other")
        rate = sc.get("failure_rate", 0.0) * 100
        days_failed = sc.get("days_failed", 0)
        total_runs = sc.get("total_runs", 0)
        series = sc.get("daily_series", [])
        stale = sc.get("stale", False)

        if trend > 0.05:
            arrow, tr_cls, tr_title = "↑", "trend-up", "getting worse"
        elif trend < -0.05:
            arrow, tr_cls, tr_title = "↓", "trend-down", "improving"
        else:
            arrow, tr_cls, tr_title = "→", "trend-flat", "flat"
        tr_title = f"{tr_title} (slope {trend:+.3f}/day)"
        rate_str = f"{rate:.0f}%" if rate >= 1 else f"{rate:.1f}%"
        tr_cls_attr = ' class="stale-row"' if stale else ""
        stale_title = " · stale (no failure in recent window)" if stale else ""

        rows += (
            f"<tr{tr_cls_attr} "
            f'data-cat="{html.escape(cat)}" data-class="{cls}" '
            f'data-trend="{trend}" data-rate="{rate:.4f}" data-days="{days_failed}" '
            f'data-stale="{int(stale)}">'
            f'<td class="rank">{i}</td>'
            f'<td class="test-name" title="{html.escape(name)}{stale_title}">'
            f"{html.escape(short)}</td>"
            f'<td><span class="badge-{cls}" title="{cls}">{cls}</span></td>'
            f'<td class="{tr_cls}" title="{tr_title}">{arrow}</td>'
            f'<td><span class="cat-chip">{html.escape(cat)}</span></td>'
            f'<td class="freq" title="failed {days_failed} of {total_runs} '
            f'recorded runs (all history)">{rate_str}</td>'
            f'<td class="freq">{days_failed}/{total_runs}</td>'
            f'<td class="spark-cell">{_sparkline(series)}</td>'
            f"</tr>"
        )
    return rows


def _render_resolved_section(resolved: List[Dict]) -> str:
    """Render presumed-fixed tests as a collapsed <details> sub-list.

    Returns "" when nothing is resolved, so the block is omitted entirely.
    """
    if not resolved:
        return ""
    rows = _render_scorecard_rows(resolved)
    return (
        '<details class="resolved-block">'
        f"<summary>Resolved / quiet ({len(resolved)}) — no failure in the "
        f"last {RESOLVED_QUIET_RUNS}+ runs</summary>"
        '<table class="scorecard-table">'
        "<thead><tr><th>#</th><th>Test</th><th>Class</th><th>Trend</th>"
        '<th>Category</th><th title="Share of all recorded runs the test '
        'failed">Rate</th><th title="runs failed / total runs">Runs</th><th>Recent activity</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></details>"
    )


def _surprise_str(burst_p) -> str:
    """Honest 'surprise' %: how unusual the burst is vs the test's baseline.

    = (1 - burst_p) * 100. NOT a probability of regression -- burst_p is
    P(data | baseline), so this reads as 'more extreme than X% of outcomes
    the test's normal flakiness would produce'.
    """
    if burst_p is None:
        return "—"
    pct = (1.0 - burst_p) * 100.0
    if pct >= 99.9:
        return ">99.9%"
    if pct >= 10:
        return f"{pct:.0f}%"
    return f"{pct:.1f}%"


def _regression_warnings(regressions: List[Dict]) -> Dict[str, float]:
    """Map test_name -> post-onset fail-rate lower bound for MEANINGFUL rows.

    Flags an ongoing regression for a heatmap ⚠️ only when it is failing
    often enough to matter, judged by an effect-size test rather than a
    p-value: from the Beta posterior over the post-onset failure rate, we
    require the lower end of a ``HEATMAP_WARN_CI`` credible interval to be at
    least ``HEATMAP_WARN_MEANINGFUL_RATE`` -- i.e. "we're 90% confident this
    test now fails >= 5% of runs". A ``HEATMAP_WARN_MIN_FAILURES`` floor
    guards against the degenerate tiny-window case (a lone failure on the
    most recent run) where an uninformative prior over-concentrates.

    Returns the lower bound per flagged test (used for the tooltip).
    """
    warn: Dict[str, float] = {}
    for r in regressions:
        if not r.get("ongoing"):
            continue
        series = r.get("daily_series")
        onset = r.get("onset_index")
        if series is None or onset is None:
            continue
        post = series[onset:]
        fails = sum(post)
        total = len(post)
        if fails < HEATMAP_WARN_MIN_FAILURES:
            continue
        lb = regression_rate_lower_bound(fails, total, credible=HEATMAP_WARN_CI)
        if lb >= HEATMAP_WARN_MEANINGFUL_RATE:
            warn[r["test_name"]] = lb
    return warn


def _render_regression_rows(records: List[Dict], repo: str = "valkey-io/valkey") -> str:
    """Render detected green->red regressions (blame), newest first.

    The "suspect range" is a GitHub compare link between the last green and
    the first red run -- the exact commit range to bisect. It needs no API
    or token; it just points the browser at GitHub's diff view.
    """
    if not records:
        return (
            '<tr><td colspan="8" class="no-commits">'
            "No ongoing regressions detected. 🎉</td></tr>"
        )
    conf_badge = {
        "high": "badge-persistent",
        "medium": "badge-flaky",
        "low": "badge-rare",
        "unknown": "badge-rare",
    }
    rows = ""
    for r in records:
        name = sanitize_cached_failure(r.get("test_name", ""))
        if name is None:
            continue
        short = _short_test_name(name)
        reg = r.get("regression_date", "")
        last_pass = r.get("last_pass_date", "—")
        conf = r.get("confidence", "unknown")
        rate = r.get("post_onset_rate", 0.0) * 100
        p0 = r.get("p0_hat")
        burst_p = r.get("burst_p")
        lp = r.get("last_pass_sha") or ""
        ff = r.get("first_fail_sha") or ""
        if lp and ff and lp != ff:
            suspect = (
                f'<a class="job-link" '
                f'href="https://github.com/{repo}/compare/{lp}...{ff}" '
                f'target="_blank" rel="noopener noreferrer" '
                f'title="commits between the last green and first red run">'
                f"{lp[:7]}…{ff[:7]} ↗</a>"
            )
        elif ff:
            suspect = (
                f'<a class="job-link" href="https://github.com/{repo}/commit/{ff}" '
                f'target="_blank" rel="noopener noreferrer">{ff[:7]} ↗</a> '
                '<span class="no-commits">(already failing at window start)</span>'
            )
        else:
            suspect = '<span class="no-commits">—</span>'
        p0_str = f"{p0 * 100:.2f}%" if p0 is not None else "—"
        if burst_p is not None:
            surprise = (1.0 - burst_p) * 100.0
            conf_title = (
                f"surprise vs baseline: more unusual than {surprise:.1f}% of this "
                f"test's normal-flakiness outcomes (burst probability {burst_p:.3g}, "
                f"baseline {p0_str}, post-onset {rate:.0f}%). Not a literal "
                f"probability of regression."
            )
        else:
            conf_title = "no clean pre-onset history to learn a baseline"
        cls = conf_badge.get(conf, "badge-rare")
        rows += (
            f'<tr data-conf="{conf}">'
            f'<td class="test-name" title="{html.escape(name)}">{html.escape(short)}</td>'
            f'<td class="freq">{reg}</td>'
            f'<td class="freq">{last_pass}</td>'
            f"<td>{suspect}</td>"
            f'<td class="freq" title="learned baseline fail rate (posterior mean)">'
            f"{p0_str}</td>"
            f'<td><span class="{cls}" title="{html.escape(conf_title)}">'
            f"{_surprise_str(burst_p)}</span></td>"
            f'<td class="spark-cell">'
            f"{_sparkline(r.get('history_series') or r.get('daily_series', []), mark_index=r.get('history_onset_index', r.get('onset_index')))}"
            f"</td>"
            f'<td class="freq">{rate:.0f}%</td>'
            f"</tr>"
        )
    return rows


def _render_fixed_regressions(
    records: List[Dict], repo: str = "valkey-io/valkey"
) -> str:
    """Render likely-fixed regressions as a collapsed <details> sub-list.

    Returns "" when nothing is fixed, so the block is omitted entirely.
    """
    if not records:
        return ""
    rows = _render_regression_rows(records, repo)
    return (
        '<details class="resolved-block">'
        f"<summary>Likely fixed ({len(records)}) — no failure in the last "
        f"{REGRESSION_ONGOING_QUIET_RUNS}+ runs</summary>"
        '<table class="scorecard-table">'
        "<thead><tr><th>Test</th><th>First failed</th><th>Last passed</th>"
        "<th>Suspect range</th><th>Baseline</th><th>Surprise</th>"
        "<th>Onset</th><th>Post-onset</th></tr></thead>"
        f"<tbody>{rows}</tbody></table></details>"
    )


_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Valkey CI Failure Report — ${branch}</title>
<style>
${styles}
</style>
</head>
<body>
<h1>Valkey CI Failure Report</h1>
<p class="meta">${workflow} · ${branch} · ${repo} · last ${days} days · generated ${generated} · <a href="releases.html" class="rel-branch-link" title="weekly full-suite health per supported release branch (7.2, 8.0, ...)">Release branch health →</a></p>
<p class="hint">Valkey CI failure trends for the <b>${branch}</b> branch — nightly Daily suite and per-commit CI. Tracks which tests fail, how often, and whether they are getting better or worse.</p>
${releases_strip}

<div class="stats">
  <div class="stat"><div class="stat-val">${total_runs}</div><div class="stat-label">runs</div></div>
  <div class="stat"><div class="stat-val">${failed_runs}</div><div class="stat-label">failed</div></div>
  <div class="stat"><div class="stat-val">${unique_tests}</div><div class="stat-label">unique failures</div></div>
</div>

<div class="tabs" role="tablist">
  <button class="tab active" data-tab="heatmap" role="tab">Heatmap</button>
  <button class="tab" data-tab="regressions" role="tab">Regressions</button>
  <button class="tab" data-tab="rundetails" role="tab">Run Details</button>
  <button class="tab" data-tab="scorecard" role="tab">Flakiness Scorecard</button>
</div>

<div class="tab-panel active" id="tab-heatmap" role="tabpanel">
${heatmap_body}
  <details class="methodology">
    <summary>What the ⚠️ means</summary>
    <div class="hint">
      <p>A ⚠️ next to a test marks it as a <b>likely meaningful regression</b> — not merely a statistically surprising one. It asks <a href="https://en.wikipedia.org/wiki/Effect_size" target="_blank" rel="noopener noreferrer">"how much does it fail"</a> (effect size) rather than <a href="https://en.wikipedia.org/wiki/Statistical_significance" target="_blank" rel="noopener noreferrer">"how surprising is it"</a> (significance), so a single failure of a normally-clean test — which looks very surprising against a near-zero baseline — does <b>not</b> trip it.</p>
      <p>We model the test's failure rate <i>since onset</i> as a <a href="https://en.wikipedia.org/wiki/Beta_distribution" target="_blank" rel="noopener noreferrer">Beta</a> posterior (the <a href="https://en.wikipedia.org/wiki/Conjugate_prior" target="_blank" rel="noopener noreferrer">conjugate prior</a> for a pass/fail rate, seeded with a weak <a href="https://en.wikipedia.org/wiki/Jeffreys_prior" target="_blank" rel="noopener noreferrer">Jeffreys prior</a>) and take the lower bound of its <a href="https://en.wikipedia.org/wiki/Credible_interval" target="_blank" rel="noopener noreferrer">${warn_ci} credible interval</a>. The ⚠️ appears only when that bound is at least <b>${warn_rate}</b> — i.e. we are ${warn_ci} confident the test now fails at least ${warn_rate} of runs — and it has failed at least <b>${warn_min_fails}</b> times. The credible interval naturally requires enough evidence: a tiny sample yields a wide posterior whose lower bound stays low, so weak signals don't flag until they earn it.</p>
    </div>
  </details>
</div>

<div class="tab-panel" id="tab-scorecard" role="tabpanel">
<div class="section">
  <h2>Flaky Test Scorecard</h2>
  <p class="hint">Every test that has failed in recorded CI history, ranked worst-first — the full flaky roster, independent of the recent heatmap above.
    Rate = share of all recorded runs the test failed (the denominator grows as history accrues, so low rates become expressible over time).
    Class: <span class="badge-persistent">persistent</span> = fails a majority of runs, or the last ${persistent_streak} runs straight ·
    <span class="badge-flaky">flaky</span> = recurring / intermittent ·
    <span class="badge-rare">rare</span> = a single one-off failure.
    Trend: <span class="trend-up">↑</span> worse / <span class="trend-down">↓</span> better / <span class="trend-flat">→</span> flat (recent window).
    Greyed rows are cooling off (no failure in the last ${cooling_runs}+ runs); tests quiet for ${resolved_runs}+ runs drop to the collapsed <b>Resolved</b> sub-list below. The activity sparkline shows failure counts over the recent window.
  </p>
  ${ci_scorecard}
  <div id="scorecard-controls">
    <label>Class:
      <select id="sc-class"><option value="">all</option><option value="persistent">persistent</option><option value="flaky">flaky</option><option value="rare">rare</option></select>
    </label>
    <label style="margin-left:10px;">Category:
      <select id="sc-cat"><option value="">all</option></select>
    </label>
    <span style="margin-left:10px;">Sort:</span>
    <button data-sort="rate">rate</button>
    <button data-sort="trend">trend</button>
    <button data-sort="days">days</button>
  </div>
  <table class="scorecard-table">
    <thead><tr><th>#</th><th>Test</th><th>Class</th><th>Trend</th><th>Category</th><th title="Share of all recorded runs the test failed">Rate</th><th title="runs failed / total runs">Runs</th><th title="Failures over the recent window">Recent activity</th></tr></thead>
    <tbody id="scorecard-body">${scorecard_rows}</tbody>
  </table>
  ${resolved_section}
</div>
</div>

<div class="tab-panel" id="tab-rundetails" role="tabpanel">
<div class="section">
  <h2>Run Details — Daily workflow (newest first)</h2>
  <p class="hint">Each row is one nightly <b>Daily</b>-workflow run. This tab covers the Daily workflow only; per-commit CI detail lives in the Heatmap and Regressions tabs. Status shows failed/total jobs. Numbered links like [1][2] go to the specific job logs on GitHub. Hover over a commit SHA to see the commit message.</p>
  <table class="detail-table">
    <thead><tr><th>Date</th><th>Status</th><th>Commit</th><th>#</th><th>Unique Failures</th><th>Failed Jobs</th><th>Commits since prev run</th></tr></thead>
    <tbody>${run_detail_rows}</tbody>
  </table>
</div>
</div>

<div class="tab-panel" id="tab-regressions" role="tabpanel">
<div class="section">
  <h2>Regressions (blame)</h2>
  <p class="hint">Ongoing green→red transitions, newest first (likely-fixed ones collapse below). <b>Suspect range</b> links to the commits between the last green run and the first red run — the starting point for bisecting a regression.
    <b>Baseline</b> is the test's learned historical fail rate. <b>Surprise</b> is how unusual the failures since onset are versus that baseline (100% − burst probability), <i>not</i> a literal probability of regression:
    <span class="badge-persistent">≥99%</span> (e.g. a clean test breaking) ·
    <span class="badge-flaky">≥90%</span> ·
    <span class="badge-rare">lower</span> (plausibly just this test's usual flakiness) ·
    <span class="badge-rare">—</span> (no clean pre-onset history).
    Post-onset = share of runs that failed since the transition. Hover a confidence badge for details.
  </p>
  ${ci_regressions}
  <table class="scorecard-table">
    <thead><tr><th>Test</th><th>First failed</th><th>Last passed</th><th>Suspect range</th><th title="learned baseline fail rate (posterior mean)">Baseline</th><th title="surprise vs baseline (100% − burst probability)">Surprise</th><th title="pass/fail across the window; amber tick marks the onset run">Onset</th><th title="share of runs failed since onset">Post-onset</th></tr></thead>
    <tbody>${regressions_rows}</tbody>
  </table>
  ${fixed_regressions}
  <details class="methodology">
    <summary>How this works</summary>
    <div class="hint">
      <p>The surprise score is <a href="https://en.wikipedia.org/wiki/Bayesian_inference" target="_blank" rel="noopener noreferrer">Bayesian</a>, not a fixed threshold. Each test's baseline fail rate is modelled as a <a href="https://en.wikipedia.org/wiki/Beta_distribution" target="_blank" rel="noopener noreferrer">Beta distribution</a> learned from its full history — the <a href="https://en.wikipedia.org/wiki/Conjugate_prior" target="_blank" rel="noopener noreferrer">conjugate prior</a> for a pass/fail rate, seeded with a weak <a href="https://en.wikipedia.org/wiki/Jeffreys_prior" target="_blank" rel="noopener noreferrer">Jeffreys prior</a> so a test with little history isn't over-trusted.</p>
      <p>The "burst probability" is how likely the failures <i>since onset</i> are under that baseline — the upper tail of the <a href="https://en.wikipedia.org/wiki/Beta-binomial_distribution" target="_blank" rel="noopener noreferrer">Beta-binomial</a> <a href="https://en.wikipedia.org/wiki/Posterior_predictive_distribution" target="_blank" rel="noopener noreferrer">posterior-predictive</a> distribution. The displayed <b>Surprise = 100% − burst probability</b>, meaning "more extreme than X% of what this test's normal flakiness would produce". It is <b>not</b> P(regression) — that would be the <a href="https://en.wikipedia.org/wiki/Misuse_of_p-values" target="_blank" rel="noopener noreferrer">transposed-conditional fallacy</a> (it answers <i>P(data | no&nbsp;regression)</i>, not the reverse).</p>
      <p>Why it adapts per test: a historically clean test is damning after a single fresh failure, while a chronically flaky test needs a much larger burst before it looks surprising. A regression whose test then stays quiet for 14+ runs is treated as likely fixed and collapses into the sub-list above.</p>
    </div>
  </details>
</div>
</div>

<details>
  <summary>Raw JSON data</summary>
  <pre>${report_json}</pre>
</details>
<script>
${script}
</script>
</body>
</html>
"""
