"""Tests for per-release-branch report generation (releases page data)."""

from __future__ import annotations

import json
from typing import Dict, List

from scripts.ci_health.releases import (
    _health_tier,
    discover_release_branches,
    generate_releases_data,
)
from scripts.ci_health.store import Cache
from scripts.ci_health.weekly import WEEKLY_SPLIT_WORKFLOW

# The seed below uses fixed 2026-07 run dates. ``generate_report_data`` defaults
# to a 30-day window ending today, so any test that reads runs through it must
# pass a window wide enough to reach those dates or it silently sees no data.
SEED_WINDOW_DAYS = 3650

# ---------------------------------------------------------------------------
# Fixture: a cache with two weeks of synthetic weekly-split data
# ---------------------------------------------------------------------------


def _srun(run_id: int, branch: str, date: str, status: str) -> Dict:
    return {
        "run_id": run_id,
        "repo": "valkey-io/valkey",
        "workflow_file": WEEKLY_SPLIT_WORKFLOW,
        "status": status,
        "branch": branch,
        "commit_sha": "",
        "run_date": f"{date}T06:00:00Z",
        "duration_secs": 100,
        "raw_json": json.dumps({}),
    }


def _jobs(names_conclusions: List) -> List[Dict]:
    return [
        {
            "job_id": jid,
            "name": name,
            "status": "completed",
            "conclusion": conclusion,
            "raw_json": json.dumps({}),
        }
        for jid, name, conclusion in names_conclusions
    ]


def _seed(temp_db_path: str) -> Cache:
    """Two branches, two Sundays. 8.0 has a real failing test in week 2;
    9.0 is build-broken (all jobs fail, unattributed)."""
    cache = Cache(temp_db_path)
    cache.store_runs(
        [
            _srun(-100, "8.0", "2026-07-05", "success"),
            _srun(-101, "9.0", "2026-07-05", "failure"),
            _srun(-200, "8.0", "2026-07-12", "failure"),
            _srun(-201, "9.0", "2026-07-12", "failure"),
        ]
    )
    # 8.0 week 1: all green
    cache.store_jobs(-100, _jobs([(1, "test-a", "success"), (2, "test-b", "success")]))
    # 9.0 week 1: both jobs fail, unattributed
    cache.store_jobs(-101, _jobs([(3, "test-a", "failure"), (4, "test-b", "failure")]))
    cache.store_failures(
        3,
        [
            {
                "test_name": "test-a: unattributed failure",
                "error_summary": "exit 1",
                "log_lines": "",
            }
        ],
    )
    cache.store_failures(
        4,
        [
            {
                "test_name": "test-b: unattributed failure",
                "error_summary": "exit 1",
                "log_lines": "",
            }
        ],
    )
    # 8.0 week 2: one job fails with an attributed test name
    cache.store_jobs(-200, _jobs([(5, "test-a", "failure"), (6, "test-b", "success")]))
    cache.store_failures(
        5,
        [
            {
                "test_name": "Test replica sync in tests/integration/replication",
                "error_summary": "assert failed",
                "log_lines": "",
            }
        ],
    )
    # 9.0 week 2: both fail again, unattributed
    cache.store_jobs(-201, _jobs([(7, "test-a", "failure"), (8, "test-b", "failure")]))
    cache.store_failures(
        7,
        [
            {
                "test_name": "test-a: unattributed failure",
                "error_summary": "exit 1",
                "log_lines": "",
            }
        ],
    )
    cache.store_failures(
        8,
        [
            {
                "test_name": "test-b: unattributed failure",
                "error_summary": "exit 1",
                "log_lines": "",
            }
        ],
    )
    return cache


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthTier:
    def test_tiers(self) -> None:
        assert _health_tier(0, 52) == "ok"
        assert _health_tier(3, 52) == "warn"
        assert _health_tier(11, 52) == "crit"  # >= 20%
        assert _health_tier(1, 0) == "warn"  # degenerate: no total


class TestDiscoverBranches:
    def test_newest_first_numeric_order(self, temp_db_path: str) -> None:
        cache = _seed(temp_db_path)
        assert discover_release_branches(cache) == ["9.0", "8.0"]

    def test_empty_cache(self, temp_db_path: str) -> None:
        assert discover_release_branches(Cache(temp_db_path)) == []


class TestGenerateReleasesData:
    def test_summary_rows_match_job_conclusions(self, temp_db_path: str) -> None:
        cache = _seed(temp_db_path)
        data = generate_releases_data(cache)
        rows = {r["branch"]: r for r in data["summary_rows"]}

        r80 = rows["8.0"]
        assert r80["latest_week"] == "2026-07-12"
        assert r80["failed_jobs"] == 1 and r80["total_jobs"] == 2
        assert r80["tier"] == "crit"  # 1/2 = 50% >= 20%
        assert r80["failing_tests"] == 1 and r80["unattributed_jobs"] == 0
        assert not r80["build_broken"]
        assert [t["failed_jobs"] for t in r80["trend"]] == [0, 1]

        r90 = rows["9.0"]
        assert r90["failed_jobs"] == 2 and r90["tier"] == "crit"
        assert r90["failing_tests"] == 0 and r90["unattributed_jobs"] == 2
        assert r90["build_broken"]

    def test_per_branch_reports_are_weekly_columns(self, temp_db_path: str) -> None:
        cache = _seed(temp_db_path)
        data = generate_releases_data(cache)
        d = data["per_branch"]["8.0"]
        assert d["dates"] == ["2026-07-05", "2026-07-12"]
        assert [c["label"] for c in d["columns"]] == ["7/5", "7/12"]
        assert not d["per_run"]
        # The attributed test appears in the timeline for the failing week
        name = "Test replica sync in tests/integration/replication"
        assert name in d["tests"]
        assert d["tests"][name]["timeline"]["2026-07-12"] is not None

    def test_overall_summary(self, temp_db_path: str) -> None:
        cache = _seed(temp_db_path)
        data = generate_releases_data(cache)
        assert data["branches"] == ["9.0", "8.0"]
        assert data["summary"]["branch_count"] == 2
        assert data["summary"]["latest_week"] == "2026-07-12"

    def test_empty_cache_yields_empty_dataset(self, temp_db_path: str) -> None:
        data = generate_releases_data(Cache(temp_db_path))
        assert data["branches"] == []
        assert data["summary_rows"] == []
        assert data["summary"]["latest_week"] is None


class TestSanitizeStripsFanoutPrefix:
    """Historical rows parsed before splitting carry the fan-out job prefix;
    display-time sanitize must strip it so they aggregate with fresh rows."""

    def test_prefixed_and_stripped_rows_aggregate(self, temp_db_path: str) -> None:
        from scripts.ci_health.log_parser import sanitize_cached_failure

        prefixed = (
            "run-daily-for-release-branches (8.1) / "
            "test-ubuntu-32bit: unattributed failure"
        )
        stripped = "test-ubuntu-32bit: unattributed failure"
        assert sanitize_cached_failure(prefixed) == sanitize_cached_failure(stripped)

        # Non-prefixed names pass through untouched
        name = "Test replica sync in tests/integration/replication"
        assert sanitize_cached_failure(name) == name

    def test_end_to_end_single_heatmap_row(self, temp_db_path: str) -> None:
        """One branch, two weeks: week 1's failure stored with the historical
        prefixed name (as the generic sync wrote it), week 2's with the
        stripped name. The report must show ONE test row spanning both."""
        cache = Cache(temp_db_path)
        cache.store_runs(
            [
                _srun(-300, "8.1", "2026-07-05", "failure"),
                _srun(-301, "8.1", "2026-07-12", "failure"),
            ]
        )
        cache.store_jobs(-300, _jobs([(31, "test-x", "failure")]))
        cache.store_jobs(-301, _jobs([(32, "test-x", "failure")]))
        cache.store_failures(
            31,
            [
                {
                    "test_name": "run-daily-for-release-branches (8.1) / "
                    "test-x: unattributed failure",
                    "error_summary": "exit 1",
                    "log_lines": "",
                }
            ],
        )
        cache.store_failures(
            32,
            [
                {
                    "test_name": "test-x: unattributed failure",
                    "error_summary": "exit 1",
                    "log_lines": "",
                }
            ],
        )
        data = generate_releases_data(cache)
        tests = data["per_branch"]["8.1"]["tests"]
        assert list(tests) == ["test-x: unattributed failure"]
        timeline = tests["test-x: unattributed failure"]["timeline"]
        assert timeline["2026-07-05"] is not None
        assert timeline["2026-07-12"] is not None


class TestReleaseStrip:
    """Compact release-health strip embedded on the main (daily/CI) page."""

    def test_badges_link_and_label(self, temp_db_path: str) -> None:
        from scripts.ci_health.releases import render_release_strip

        cache = _seed(temp_db_path)
        rows = generate_releases_data(cache)["summary_rows"]
        strip = render_release_strip(rows)
        assert 'href="releases.html#branch-9.0"' in strip
        assert 'href="releases.html#branch-8.0"' in strip
        assert "🔴" in strip  # both seeded branches are crit
        assert "1/2" in strip and "2/2" in strip
        assert "Release branches (weekly):" in strip
        assert "build broken" in strip  # tooltip on 9.0

    def test_empty_rows_render_nothing(self) -> None:
        from scripts.ci_health.releases import render_release_strip

        assert render_release_strip([]) == ""

    def test_main_page_embeds_strip(self, temp_db_path: str) -> None:
        from scripts.ci_health.releases import render_release_strip
        from scripts.ci_health.report import generate_report_data, render_html

        cache = _seed(temp_db_path)
        rows = generate_releases_data(cache)["summary_rows"]
        idx = render_html(
            generate_report_data(
                cache,
                days=SEED_WINDOW_DAYS,
                workflow=WEEKLY_SPLIT_WORKFLOW,
                branch="8.0",
            ),
            releases_strip=render_release_strip(rows),
        )
        assert idx.count('class="rel-strip"') == 1
        assert 'href="releases.html#branch-9.0"' in idx

    def test_main_page_without_strip_unchanged(self, temp_db_path: str) -> None:
        """Default render (no strip arg) must not emit any strip markup."""
        from scripts.ci_health.report import generate_report_data, render_html

        cache = _seed(temp_db_path)
        idx = render_html(
            generate_report_data(
                cache,
                days=SEED_WINDOW_DAYS,
                workflow=WEEKLY_SPLIT_WORKFLOW,
                branch="8.0",
            )
        )
        assert 'class="rel-strip"' not in idx


class TestFailureDeepLinks:
    """Heatmap fail cells and Run Details job names link to GitHub job logs."""

    def test_source_run_id_roundtrip(self) -> None:
        from scripts.ci_health.weekly import source_run_id, synthetic_run_id

        assert source_run_id(synthetic_run_id(29183329995, 3)) == 29183329995
        assert source_run_id(12345) == 12345  # real ids pass through

    def test_weekly_split_cells_link_to_real_run(self, temp_db_path: str) -> None:
        from scripts.ci_health.report import _render_heatmap_table, generate_report_data

        cache = _seed(temp_db_path)  # synthetic ids -> real runs 1,2
        # The seed uses fixed 2026-07 dates; widen the day window so the test
        # does not expire once those dates fall outside the default 30 days.
        data = generate_report_data(
            cache, days=SEED_WINDOW_DAYS, workflow=WEEKLY_SPLIT_WORKFLOW, branch="8.0"
        )
        table = _render_heatmap_table(data)
        # job 5 fails in synthetic run -200 -> real run id 2
        assert (
            'href="https://github.com/valkey-io/valkey/actions/runs/2/job/5"' in table
        )
        assert 'target="_blank" rel="noopener noreferrer"' in table

    def test_daily_run_details_job_links(self, temp_db_path: str) -> None:
        from scripts.ci_health.report import generate_report_data, render_html

        cache = _seed(temp_db_path)
        data = generate_report_data(
            cache, days=SEED_WINDOW_DAYS, workflow=WEEKLY_SPLIT_WORKFLOW, branch="8.0"
        )
        run = [r for r in data["runs"] if r["failed_jobs"]][0]
        assert run["failed_job_urls"], "run_info should carry job urls"
        page = render_html(data)
        assert (
            'class="job-link" href="https://github.com/valkey-io/valkey/actions/runs/'
            in page
        )

    def test_timeline_entries_carry_job_urls(self, temp_db_path: str) -> None:
        from scripts.ci_health.report import generate_report_data

        cache = _seed(temp_db_path)
        data = generate_report_data(
            cache, days=SEED_WINDOW_DAYS, workflow=WEEKLY_SPLIT_WORKFLOW, branch="9.0"
        )
        for info in data["tests"].values():
            for entry in info["timeline"].values():
                if entry is not None:
                    assert entry["job_urls"], entry


class TestReleasesTabs:
    """Tab bar + Regressions/Scorecard panels on the releases page."""

    def test_four_panels_heatmap_active(self, temp_db_path: str) -> None:
        import re

        from scripts.ci_health.releases import render_releases_html

        cache = _seed(temp_db_path)
        h = render_releases_html(generate_releases_data(cache))
        assert h.count('class="tab-panel') == 4
        assert re.search(r'<div class="tab-panel active" id="tab-heatmap"', h)
        for t in ("heatmap", "regressions", "rundetails", "scorecard"):
            assert f'id="tab-{t}"' in h
        # Summary strip stays above the tab bar
        assert h.index('class="rel-summary"') < h.index('class="tabs"')

    def test_per_branch_regression_and_scorecard_sections(
        self, temp_db_path: str
    ) -> None:
        from scripts.ci_health.releases import render_releases_html

        cache = _seed(temp_db_path)
        h = render_releases_html(generate_releases_data(cache))
        reg = h.split('id="tab-regressions"')[1].split('id="tab-rundetails"')[0]
        sc = h.split('id="tab-scorecard"')[1]
        # One wf-title per branch in each panel (9.0 + 8.0 seeded)
        assert reg.count('class="wf-title"') == 2
        assert sc.count('class="wf-title"') == 2
        # Scorecards populated for branches with failures
        assert sc.count("scorecard-table") >= 1


class TestRunDetailsTab:
    """Per-branch weekly run tables with linked failing jobs."""

    def test_tables_weeks_and_links(self, temp_db_path: str) -> None:
        import re

        from scripts.ci_health.releases import render_releases_html

        cache = _seed(temp_db_path)
        h = render_releases_html(generate_releases_data(cache))
        panel = h.split('id="tab-rundetails"')[1].split('id="tab-scorecard"')[0]

        # One table per branch, stub gone
        assert panel.count('class="wf-title"') == 2
        assert panel.count("scorecard-table") == 2
        assert "Run details are being added" not in h

        # Week rows newest-first, linked to the REAL run page
        days = re.findall(r'noreferrer">(\d{4}-\d{2}-\d{2}) ↗', panel)
        assert days[:2] == ["2026-07-12", "2026-07-05"]  # per branch
        # synthetic -200 -> real run 2; -101/-201 (9.0) -> 1 and 2
        assert 'href="https://github.com/valkey-io/valkey/actions/runs/2"' in panel

        # Failing jobs link to job logs; passing week shows PASS badge
        assert re.search(r'actions/runs/\d+/job/\d+"', panel)
        assert '<span class="badge pass">PASS</span>' in panel
        assert "FAIL (2/2)" in panel  # 9.0 weeks


class TestRenderReleasesHtml:
    def test_layout_badges_and_defaults(self, temp_db_path: str) -> None:
        import re

        from scripts.ci_health.releases import render_releases_html

        cache = _seed(temp_db_path)
        html_out = render_releases_html(generate_releases_data(cache))

        # Balanced structural tags
        for tag in ("table", "details", "thead", "tbody", "tr"):
            opens = len(re.findall(f"<{tag}[ >]", html_out))
            closes = len(re.findall(f"</{tag}>", html_out))
            assert opens == closes, f"unbalanced <{tag}>"

        # Summary rows newest-first, sections match
        assert re.findall(r'#branch-([\d.]+)"', html_out)[:2] == ["9.0", "8.0"]

        # 9.0 is build-broken (badge, section open); 8.0 crit but attributed
        # (unhealthy badge, section open)
        opens = dict(
            re.findall(
                r'<details id="branch-([\d.]+)" class="rel-branch"( open)?>',
                html_out,
            )
        )
        assert opens == {"9.0": " open", "8.0": " open"}
        assert html_out.count("🔴 build broken") == 2  # 9.0 summary + section
        assert html_out.count("🔴 unhealthy") == 2  # 8.0 summary + section

        # Sparkline, unattributed marker, shared methodology note, heatmaps
        assert '<svg class="spark"' in html_out
        assert "unattributed" in html_out
        assert "structurally broken" in html_out
        assert html_out.count('class="heatmap-scroll"') == 2
        # Per-day weekly tables never use the CI horizontal-scroll mode
        assert 'class="heatmap-scroll scroll-right"' not in html_out
        # Cross-link to the main dashboard
        assert 'href="index.html"' in html_out

    def test_index_page_links_to_releases(self, temp_db_path: str) -> None:
        from scripts.ci_health.report import generate_report_data, render_html

        cache = _seed(temp_db_path)
        idx = render_html(
            generate_report_data(
                cache,
                days=SEED_WINDOW_DAYS,
                workflow=WEEKLY_SPLIT_WORKFLOW,
                branch="8.0",
            )
        )
        assert idx.count('href="releases.html"') == 1
        assert "Release branch health" in idx

    def test_empty_dataset_renders(self, temp_db_path: str) -> None:
        from scripts.ci_health.releases import render_releases_html

        html_out = render_releases_html(generate_releases_data(Cache(temp_db_path)))
        assert "Valkey Release Branch Health" in html_out
        assert "no data yet" in html_out
