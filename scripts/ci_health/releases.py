"""Per-release-branch report generation for the releases page.

Consumes the synthetic ``weekly-split`` per-branch series produced by
``sync_weekly_branches`` and builds, for every release branch, the same
report dataset the daily/CI views use (heatmap timeline, scorecard,
regressions) plus a compact summary row for the top-of-page strip.

Weekly runs land on Sundays, so per-day column keys are naturally sparse
(one column per week). Synthetic runs carry no commit SHA — the weekly
run's head SHA is the *unstable* tip, not the release branch tip — so
compare links and blame commit lists are intentionally absent; onset
dates remain.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from string import Template
from typing import Dict, List

from scripts.ci_health.report import (
    REG_HEAD,
    SC_HEAD,
    _asset,
    _render_fixed_regressions,
    _render_heatmap_table,
    _render_regression_rows,
    _render_scorecard_rows,
    _sparkline,
    generate_report_data,
)
from scripts.ci_health.store import Cache
from scripts.ci_health.weekly import WEEKLY_SPLIT_WORKFLOW

# The weekly run fires on Sunday; a newest week older than this means the
# Sunday run was missed or the sync stopped.
WEEKLY_MAX_AGE_DAYS = 9

#: A branch whose latest week has >= this share of jobs failing is "crit".
CRIT_JOB_FAIL_RATIO = 0.20

#: Substring marking parser pseudo-rows for jobs that died before any test
#: ran (build/compile/setup failures) — no test name could be attributed.
UNATTRIBUTED_MARKER = "unattributed failure"


def _branch_sort_key(branch: str) -> List[int]:
    return [int(x) for x in branch.split(".")]


def discover_release_branches(
    cache: Cache, repo: str = "valkey-io/valkey"
) -> List[str]:
    """Release branches present in the weekly-split series, newest first."""
    runs = cache.query_runs(repo=repo, workflow=WEEKLY_SPLIT_WORKFLOW)
    branches = sorted({r["branch"] for r in runs}, key=_branch_sort_key, reverse=True)
    return branches


def _health_tier(failed_jobs: int, total_jobs: int) -> str:
    """'ok' (no failures), 'warn' (some), or 'crit' (>=20% of jobs red)."""
    if failed_jobs <= 0:
        return "ok"
    if total_jobs and failed_jobs / total_jobs >= CRIT_JOB_FAIL_RATIO:
        return "crit"
    return "warn"


def _summary_row(branch: str, data: Dict) -> Dict:
    """Compact summary-strip row for one branch's report dataset."""
    runs = data.get("runs", [])
    latest = runs[-1] if runs else None

    failure_names = (latest or {}).get("failure_names", [])
    attributed = [n for n in failure_names if UNATTRIBUTED_MARKER not in n]
    unattributed = len(failure_names) - len(attributed)

    failed_jobs = (latest or {}).get("failed_jobs", 0)
    total_jobs = (latest or {}).get("total_jobs", 0)
    tier = _health_tier(failed_jobs, total_jobs)

    return {
        "branch": branch,
        "latest_week": (latest or {}).get("day"),
        "failed_jobs": failed_jobs,
        "total_jobs": total_jobs,
        "tier": tier,
        # Distinct failing tests in the latest week, split by whether a
        # test name could be attributed. High unattributed + crit tier
        # reads as "branch is structurally broken (build/setup)", not
        # "N flaky tests".
        "failing_tests": len(attributed),
        "unattributed_jobs": unattributed,
        "build_broken": tier == "crit" and unattributed > len(attributed),
        # Weeks trend, oldest-first, for the summary sparkline.
        "trend": [
            {
                "day": r.get("day"),
                "failed_jobs": r.get("failed_jobs", 0),
                "total_jobs": r.get("total_jobs", 0),
            }
            for r in runs
        ],
    }


def generate_releases_data(
    cache: Cache,
    repo: str = "valkey-io/valkey",
    days: int = 119,
) -> Dict:
    """Build the releases-page dataset: per-branch reports + summary rows.

    *days* bounds the recent window (default 119 = 17 weeks of Sundays).
    Branch order is newest-first throughout.
    """
    branches = discover_release_branches(cache, repo=repo)
    per_branch: Dict[str, Dict] = {}
    summary_rows: List[Dict] = []

    for branch in branches:
        data = generate_report_data(
            cache,
            days=days,
            branch=branch,
            workflow=WEEKLY_SPLIT_WORKFLOW,
            repo=repo,
        )
        per_branch[branch] = data
        summary_rows.append(_summary_row(branch, data))

    return {
        "branches": branches,
        "per_branch": per_branch,
        "summary_rows": summary_rows,
        "summary": {
            "repo": repo,
            "days": days,
            "branch_count": len(branches),
            "latest_week": max(
                (row["latest_week"] for row in summary_rows if row["latest_week"]),
                default=None,
            ),
        },
    }


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

_TIER_BADGE = {
    "ok": ('<span class="rel-badge rel-ok">🟢 healthy</span>', 0),
    "warn": ('<span class="rel-badge rel-warn">🟡 failures</span>', 1),
    "crit": ('<span class="rel-badge rel-crit">🔴 unhealthy</span>', 2),
}


def _badge(row: Dict) -> str:
    if row.get("build_broken"):
        return '<span class="rel-badge rel-crit">🔴 build broken</span>'
    return _TIER_BADGE.get(row["tier"], _TIER_BADGE["warn"])[0]


def _summary_table(rows: List[Dict]) -> str:
    body = ""
    for row in rows:
        b = html.escape(row["branch"])
        trend_series = [t["failed_jobs"] for t in row["trend"]]
        tests_cell = str(row["failing_tests"])
        if row["unattributed_jobs"]:
            tests_cell += (
                f' <span class="rel-unattr" title="jobs that failed before any '
                f'test ran (build / setup / timeout)">+{row["unattributed_jobs"]} '
                f"unattributed</span>"
            )
        body += f"""<tr>
          <td><a href="#branch-{b}" class="rel-branch-link">{b}</a></td>
          <td>{_badge(row)}</td>
          <td class="rel-jobs">{row["failed_jobs"]}/{row["total_jobs"]}</td>
          <td>{tests_cell}</td>
          <td class="rel-spark">{_sparkline(trend_series)}</td>
          <td class="rel-week">{html.escape(row["latest_week"] or "—")}</td>
        </tr>"""
    return f"""<table class="rel-summary">
      <thead><tr>
        <th>Branch</th><th>Health</th>
        <th title="failed / total jobs in the latest weekly run">Jobs failed</th>
        <th title="distinct failing tests in the latest weekly run">Failing tests</th>
        <th title="failed jobs per week, oldest to newest">Trend</th>
        <th>Latest week</th>
      </tr></thead>
      <tbody>{body}</tbody>
    </table>"""


def _branch_sections(data: Dict) -> str:
    sections = ""
    for branch in data["branches"]:
        b = html.escape(branch)
        row = next(r for r in data["summary_rows"] if r["branch"] == branch)
        open_attr = " open" if row["tier"] == "crit" else ""
        n_weeks = len(data["per_branch"][branch].get("columns", []))
        sections += f"""<details id="branch-{b}" class="rel-branch"{open_attr}>
          <summary><span class="rel-branch-title">{b}</span> {_badge(row)}
            <span class="rel-summary-inline">{row["failed_jobs"]}/{row["total_jobs"]} jobs failed
            · latest week {html.escape(row["latest_week"] or "—")}</span></summary>
          <h3 class="wf-title">Weekly full suite · last {n_weeks} weeks (columns are Sundays)</h3>
          {_render_heatmap_table(data["per_branch"][branch])}
        </details>"""
    return sections


_STRIP_EMOJI = {"ok": "🟢", "warn": "🟡", "crit": "🔴"}


def render_release_strip(summary_rows: List[Dict]) -> str:
    """Compact release-health strip for the main (daily/CI) page header.

    One badge per branch — ``9.1 🟡 3/52`` — deep-linking to that branch's
    section on releases.html. Returns "" when there is no weekly data yet
    (graceful pre-backfill degradation).
    """
    if not summary_rows:
        return ""
    badges = ""
    for row in summary_rows:
        b = html.escape(row["branch"])
        emoji = _STRIP_EMOJI.get(row["tier"], "🟡")
        state = (
            "build broken"
            if row.get("build_broken")
            else {
                "ok": "healthy",
                "warn": "some job failures",
                "crit": "unhealthy",
            }.get(row["tier"], "")
        )
        tip = html.escape(
            f"{row['branch']}: {state} — {row['failed_jobs']}/{row['total_jobs']} "
            f"jobs failed in the week of {row['latest_week'] or '?'}"
        )
        badges += (
            f'<a class="rel-strip-badge" href="releases.html#branch-{b}" '
            f'title="{tip}">{b} {emoji} '
            f'<span class="rel-strip-jobs">{row["failed_jobs"]}/{row["total_jobs"]}</span></a>'
        )
    return (
        '<div class="rel-strip"><span class="rel-strip-label">Release branches '
        "(weekly):</span>" + badges + "</div>"
    )


def _branch_run_details(data: Dict) -> str:
    """Per-branch weekly run tables: week, status, failing jobs (linked)."""
    from scripts.ci_health.weekly import source_run_id

    repo = data["summary"]["repo"]
    max_jobs = 8
    sections = ""
    for branch in data["branches"]:
        d = data["per_branch"][branch]
        rows = ""
        for run in reversed(d.get("runs", [])):  # newest week first
            real = source_run_id(run["run_id"])
            week_link = (
                f'<a class="job-link" '
                f'href="https://github.com/{repo}/actions/runs/{real}" '
                f'target="_blank" rel="noopener noreferrer">{run["day"]} ↗</a>'
            )
            if run["status"] == "success":
                badge = '<span class="badge pass">PASS</span>'
            else:
                badge = (
                    f'<span class="badge fail">FAIL '
                    f"({run['failed_jobs']}/{run['total_jobs']})</span>"
                )
            names = run.get("failed_job_names", [])
            urls = run.get("failed_job_urls", [])
            jobs_html = "—"
            if names:
                jobs_html = '<div class="job-list">'
                for k, jn in enumerate(names[:max_jobs]):
                    label = html.escape(jn)
                    if k < len(urls):
                        label = (
                            f'<a class="job-link" href="{urls[k]}" '
                            f'target="_blank" rel="noopener noreferrer">{label} ↗</a>'
                        )
                    jobs_html += f'<div class="job-entry">{label}</div>'
                if len(names) > max_jobs:
                    jobs_html += (
                        f'<div class="job-entry" style="color:#8b949e">'
                        f"+{len(names) - max_jobs} more</div>"
                    )
                jobs_html += "</div>"
            rows += f"<tr><td>{week_link}</td><td>{badge}</td><td>{jobs_html}</td></tr>"
        sections += f'<h3 class="wf-title">{html.escape(branch)}</h3>'
        if not rows:
            sections += '<p class="hint">No weekly runs recorded yet.</p>'
            continue
        sections += (
            '<table class="scorecard-table">'
            "<thead><tr><th>Week</th><th>Status</th>"
            "<th>Failed jobs (link to log)</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    return sections


def _branch_regressions(data: Dict) -> str:
    """Per-branch regression tables (ongoing + fixed) for the Regressions tab."""
    repo = data["summary"]["repo"]
    sections = ""
    for branch in data["branches"]:
        d = data["per_branch"][branch]
        regs = d.get("regressions", [])
        ongoing = [r for r in regs if r.get("ongoing", True)]
        fixed = [r for r in regs if not r.get("ongoing", True)]
        sections += f'<h3 class="wf-title">{html.escape(branch)}</h3>'
        if not ongoing and not fixed:
            sections += '<p class="hint">No green→red regressions detected.</p>'
            continue
        sections += (
            '<table class="scorecard-table">'
            f"{REG_HEAD}<tbody>{_render_regression_rows(ongoing, repo)}</tbody></table>"
            f"{_render_fixed_regressions(fixed, repo)}"
        )
    return sections


def _branch_scorecards(data: Dict) -> str:
    """Per-branch static flakiness scorecards for the Scorecard tab."""
    sections = ""
    for branch in data["branches"]:
        d = data["per_branch"][branch]
        cards = d.get("scorecard", {}).get("scorecards", [])
        active = [c for c in cards if not c.get("resolved")]
        sections += f'<h3 class="wf-title">{html.escape(branch)}</h3>'
        if not active:
            sections += '<p class="hint">No recorded test failures.</p>'
            continue
        sections += (
            '<table class="scorecard-table">'
            f"{SC_HEAD}<tbody>{_render_scorecard_rows(active)}</tbody></table>"
        )
    return sections


def render_releases_html(data: Dict) -> str:
    """Render the releases page: summary strip + per-branch sections."""
    return Template(_RELEASES_TEMPLATE).substitute(
        styles=_asset("report.css"),
        script=_asset("report.js"),
        repo=html.escape(data["summary"]["repo"]),
        branch_count=data["summary"]["branch_count"],
        latest_week=html.escape(data["summary"]["latest_week"] or "no data yet"),
        latest_week_raw=html.escape(data["summary"]["latest_week"] or ""),
        max_age_days=WEEKLY_MAX_AGE_DAYS,
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        summary_table=_summary_table(data["summary_rows"]),
        branch_sections=_branch_sections(data),
        regression_sections=_branch_regressions(data),
        scorecard_sections=_branch_scorecards(data),
        run_details_sections=_branch_run_details(data),
    )


_RELEASES_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Valkey Release Branch Health</title>
<style>
${styles}
/* Releases page additions */
.rel-summary{border-collapse:collapse;margin:14px 0;width:100%;max-width:760px}
.rel-summary th,.rel-summary td{padding:6px 12px;text-align:left;border-bottom:1px solid #21262d;font-size:0.9rem}
.rel-summary th{color:#8b949e;font-weight:600;font-size:0.8rem}
.rel-jobs{font-family:monospace}
.rel-spark{min-width:100px}
.rel-week{color:#8b949e}
.rel-badge{font-size:0.8rem;white-space:nowrap}
.rel-unattr{color:#8b949e;font-size:0.8rem}
.rel-branch{margin:14px 0;border:1px solid #21262d;border-radius:6px;padding:4px 14px}
.rel-branch>summary{cursor:pointer;padding:8px 0;font-size:0.95rem}
.rel-branch-title{font-weight:700;font-size:1.05rem;margin-right:6px}
.rel-summary-inline{color:#8b949e;font-size:0.85rem;margin-left:8px}
</style>
</head>
<body>
<h1>Valkey Release Branch Health</h1>
<div class="stale-banner" role="alert" hidden data-latest="${latest_week_raw}" data-max-age-days="${max_age_days}" data-what="weekly run"></div>
<p class="meta">weekly full suite per release branch · ${repo} · ${branch_count} branches · latest week ${latest_week} · generated ${generated}</p>
<p class="hint">Every Sunday the <b>Weekly</b> workflow runs the full Daily test matrix against each supported release branch. This page tracks per-branch health — the summary below answers "which shipped versions are unhealthy?", the sections underneath show which tests fail per week. Unlike <a href="index.html">the unstable dashboard</a>, the goal here is <b>visibility</b>, not deflaking.</p>

${summary_table}

<div class="tabs" role="tablist">
  <button class="tab active" data-tab="heatmap" role="tab">Heatmap</button>
  <button class="tab" data-tab="regressions" role="tab">Regressions</button>
  <button class="tab" data-tab="rundetails" role="tab">Run Details</button>
  <button class="tab" data-tab="scorecard" role="tab">Flakiness Scorecard</button>
</div>

<div class="tab-panel active" id="tab-heatmap" role="tabpanel">
<p class="hint">A red <b>Run status</b> with no test rows below it — or a high "unattributed" count — means jobs died on build / sanitizer / setup / timeout before any test ran: the branch is structurally broken, not flaky. 🔴 sections start expanded; healthier branches are collapsed. Failure cells link to the failing job's log.</p>
${branch_sections}
</div>

<div class="tab-panel" id="tab-regressions" role="tabpanel">
<div class="section">
<h2>Green→red regressions per release branch</h2>
<p class="hint">Tests that went from consistently passing to failing, per branch. Onset is the first failing week; weekly cadence means onset resolution is "some week" and suspect ranges span a week of backports. Commit compare links are absent by design (the weekly run's SHA is the unstable tip, not the release branch tip).</p>
${regression_sections}
</div>
</div>

<div class="tab-panel" id="tab-rundetails" role="tabpanel">
<div class="section">
<h2>Weekly runs per release branch</h2>
${run_details_sections}
</div>
</div>

<div class="tab-panel" id="tab-scorecard" role="tabpanel">
<div class="section">
<h2>Flakiness scorecard per release branch</h2>
<p class="hint">Every test that has failed in recorded weekly history, ranked worst-first per branch. Rate = share of recorded runs the test failed.</p>
${scorecard_sections}
</div>
</div>

<script>
${script}
</script>
</body>
</html>
"""
