"""Command line for CI health: sync the store, then report from it.

Every command reads from GitHub at most; nothing here writes to a
repository, an issue or a comment. ``sync`` brings the store up to date;
``scorecard`` and ``blame`` compute from it and write JSON; ``report``
writes the whole site: the dashboard pages, the Markdown report and every
JSON export, and fails when the Daily data is stale so a dead token or a
stalled sync is never published as a quiet night. The workflow runs
``sync`` then ``report``; by hand, run whichever you need.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Callable, List, Optional

from github import Auth, Github

from scripts.ci_health.blame import compute_blame
from scripts.ci_health.releases import (
    WEEKLY_MAX_AGE_DAYS,
    generate_releases_data,
    render_release_strip,
    render_releases_html,
)
from scripts.ci_health.report import generate_report_data, render_html, render_markdown, stale_reason
from scripts.ci_health.scorecard import compute_scorecards
from scripts.ci_health.slack_report import BAR_RUNS, build_message, post
from scripts.ci_health.store import Cache
from scripts.ci_health.sync import WORKFLOW_FILES, GitHubRunsClient, HistorySync, RunsClient
from scripts.common.logging_utils import configure_logging, log_outcome

logger = logging.getLogger(__name__)

DEFAULT_REPO = "valkey-io/valkey"
DEFAULT_DB = "ci-health/history.sqlite"

ClientFactory = Callable[[str, str], RunsClient]


def _github_client(token: str, repo: str) -> RunsClient:
    if not token:
        raise SystemExit("a GitHub token is required (--token)")
    return GitHubRunsClient(Github(auth=Auth.Token(token)), repo, token=token)


def _workflow_file(name: str) -> str:
    return WORKFLOW_FILES.get(name, name)


def _write_json(data: object, output: Optional[str]) -> None:
    content = json.dumps(data, indent=2)
    if output:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content + "\n")
        logger.info("Wrote %s", path)
    else:
        print(content)


def run_sync(args: argparse.Namespace, make_client: ClientFactory) -> int:
    """Store new runs and the failures of their failed jobs; non-zero if the token failed."""
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    cache = Cache(args.db)
    try:
        syncer = HistorySync(make_client(args.token, args.repo), cache)
        summary = syncer.sync(
            workflow=args.workflow, branch=args.branch, since=args.since, until=args.until,
            progress=logger.info,
        )
        if args.weekly_budget > 0 and args.workflow in (None, "weekly"):
            # The sync above has just listed the weekly runs.
            summary["weekly"] = syncer.sync_weekly_branches(
                budget=args.weekly_budget, progress=logger.info, refresh_runs=False,
            )
    finally:
        cache.close()
    print(json.dumps(summary, indent=2))
    if summary["auth_failed"] or summary.get("weekly", {}).get("auth_failed"):
        log_outcome(logger, logging.ERROR, "GitHub refused the token: the sync is incomplete")
        return 1
    errors = len(summary["errors"]) + len(summary.get("weekly", {}).get("errors", []))
    if errors:
        log_outcome(logger, logging.WARNING, "Sync finished with %d error(s); see the summary", errors)
    else:
        log_outcome(
            logger, logging.INFO, "Synced %d new run(s), %d new failure(s)",
            summary["new_runs_fetched"], summary["new_failures_parsed"],
        )
    return 0


def run_scorecard(args: argparse.Namespace, make_client: ClientFactory) -> int:
    """Per-test scorecards over the stored history, as JSON."""
    cache = Cache(args.db)
    try:
        data = compute_scorecards(
            cache, days=args.days, branch=args.branch, workflow=_workflow_file(args.workflow),
            repo=args.repo,
        )
    finally:
        cache.close()
    _write_json(data, args.output)
    return 0


def run_blame(args: argparse.Namespace, make_client: ClientFactory) -> int:
    """Green-to-red onsets over the stored history with their commit ranges, as JSON.

    The commit range needs GitHub; without a token the onsets are still
    reported, with an empty range.
    """
    cache = Cache(args.db)
    try:
        client = make_client(args.token, args.repo) if args.token else None
        records = compute_blame(
            cache, client, days=args.days, branch=args.branch, workflow=_workflow_file(args.workflow),
            repo=args.repo,
        )
    finally:
        cache.close()
    _write_json(records, args.output)
    return 0


def run_report(args: argparse.Namespace, make_client: ClientFactory) -> int:
    """Write the site: dashboard pages, Markdown report and JSON exports.

    Daily freshness is the gate: a newest Daily run older than
    ``--max-age-days`` fails the command, since a regular nightly cadence
    makes that the sign of a dead token or a stalled sync. The per-commit CI
    and weekly release views only warn, because merges are bursty and the
    weekly backfill may still be in progress.
    """
    site = Path(args.site)
    site.mkdir(parents=True, exist_ok=True)
    cache = Cache(args.db)
    try:
        client = make_client(args.token, args.repo) if args.token else None
        daily = generate_report_data(
            cache, days=args.days, branch=args.branch, workflow="daily.yml", repo=args.repo, client=client,
        )
        ci = None
        if not args.no_ci:
            ci = generate_report_data(
                cache, branch=args.branch, workflow="ci.yml", repo=args.repo, client=client,
                per_run=True, max_runs=40,
            )
        releases = generate_releases_data(cache, repo=args.repo)
        strip = render_release_strip(releases["summary_rows"]) if releases["summary_rows"] else ""
        _write_text(render_html(daily, ci_data=ci, releases_strip=strip), site / "index.html")
        _write_text(render_releases_html(releases), site / "releases.html")
        _write_text(render_markdown(daily), site / "report.md")
        _write_json(daily, str(site / "report.json"))
        if ci is not None:
            _write_json(ci, str(site / "report-ci.json"))
        _write_json(releases, str(site / "report-releases.json"))
        # The report already carries the scorecards and the regressions over
        # a 90-day window; the standalone files are those same records, so
        # the commit ranges are compared once, not twice.
        _write_json(daily["scorecard"], str(site / "scorecards.json"))
        _write_json(daily["regressions"], str(site / "blame.json"))
    finally:
        cache.close()

    if ci is not None:
        reason = stale_reason(ci["summary"].get("latest_run_date"), max_age_days=args.max_age_days)
        if reason:
            logger.warning("CI view is stale: %s", reason)
    reason = stale_reason(releases["summary"].get("latest_week"), max_age_days=WEEKLY_MAX_AGE_DAYS)
    if reason:
        logger.warning("Release-branch view is stale: %s", reason)
    reason = stale_reason(daily["summary"].get("latest_run_date"), max_age_days=args.max_age_days)
    if reason:
        log_outcome(logger, logging.ERROR, "Daily data is stale, not publishing: %s", reason)
        return 1
    log_outcome(
        logger, logging.INFO, "Wrote the site to %s; newest Daily run %s",
        site, daily["summary"].get("latest_run_date"),
    )
    return 0


def run_slack(args: argparse.Namespace, make_client: ClientFactory) -> int:
    """Render the Slack message for the newest Daily run; post it unless ``--dry-run``.

    Without ``--always``, a run with nothing failed is not posted, which is
    what the message it replaces did too. ``--issues`` names a JSON file
    mapping a fingerprint to the detector's issue URL, so each line can link
    its issue.
    """
    cache = Cache(args.db)
    try:
        data = generate_report_data(cache, days=args.days, branch=args.branch, workflow="daily.yml", repo=args.repo)
        cards = compute_scorecards(cache, days=args.days, branch=args.branch, workflow="daily.yml", repo=args.repo)
    finally:
        cache.close()
    issue_urls: dict = {}
    if args.issues:
        issue_urls = json.loads(Path(args.issues).read_text())
    message = build_message(
        data, cards["scorecards"], dashboard_url=args.dashboard_url, issue_urls=issue_urls, bar_runs=args.bar_runs,
    )
    print(message.text)
    if args.dry_run:
        return 0
    if not message.failed and not args.always:
        log_outcome(logger, logging.INFO, "Newest Daily run passed; nothing posted")
        return 0
    if not args.webhook_url:
        log_outcome(logger, logging.ERROR, "No webhook URL: pass --webhook-url or set SLACK_WEBHOOK_URL")
        return 1
    post(message, args.webhook_url)
    log_outcome(logger, logging.INFO, "Posted the Daily CI message to Slack")
    return 0


def _write_text(content: str, path: Path) -> None:
    path.write_text(content)
    logger.info("Wrote %s", path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.ci_health.main",
        description="Keep a history of Valkey's Daily CI and report per-test flakiness from it.",
    )
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite history file (default: {DEFAULT_DB}).")
    parser.add_argument("--repo", default=DEFAULT_REPO, help=f"Repository whose CI to read (default: {DEFAULT_REPO}).")
    parser.add_argument("--token", default="", help="GitHub token with actions:read (and contents:read for blame).")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="Store new runs and parse the logs of their failed jobs.")
    sync.add_argument("--workflow", choices=sorted(WORKFLOW_FILES), default=None,
                      help="One workflow only (default: all of them).")
    sync.add_argument("--branch", default=None, help="Only runs on this branch.")
    sync.add_argument("--since", default=None, help="Only runs created at or after this ISO 8601 time.")
    sync.add_argument("--until", default=None, help="Only runs created at or before this ISO 8601 time.")
    sync.add_argument("--weekly-budget", type=int, default=300,
                      help="API calls to spend splitting weekly release-branch runs (0 skips it; default 300).")
    sync.set_defaults(func=run_sync)

    for name, func, help_text in (
        ("scorecard", run_scorecard, "Per-test flakiness scorecards as JSON."),
        ("blame", run_blame, "Regression onsets and the commits between last pass and first failure, as JSON."),
    ):
        report = sub.add_parser(name, help=help_text)
        report.add_argument("--days", type=int, default=30, help="Days of history in the recent window (default: 30).")
        report.add_argument("--branch", default="unstable", help="Branch to report on (default: unstable).")
        report.add_argument("--workflow", default="daily",
                            help="Workflow to report on: daily, weekly, ci, or a workflow file name (default: daily).")
        report.add_argument("--output", "-o", default=None, help="Write here instead of stdout.")
        report.set_defaults(func=func)

    site = sub.add_parser("report", help="Write the dashboard pages, Markdown report and JSON exports to a directory.")
    site.add_argument("--site", default="ci-health/site", help="Directory to write (default: ci-health/site).")
    site.add_argument("--days", type=int, default=30, help="Days of history in the recent window (default: 30).")
    site.add_argument("--branch", default="unstable", help="Branch to report on (default: unstable).")
    site.add_argument("--no-ci", action="store_true", help="Leave out the per-commit CI view.")
    site.add_argument("--max-age-days", type=int, default=2,
                      help="Fail if the newest Daily run is older than this (default: 2).")
    site.set_defaults(func=run_report)

    slack = sub.add_parser("slack", help="Render the Slack message for the newest Daily run and post it.")
    slack.add_argument("--dashboard-url", default="https://valkey-io.github.io/valkey-ci-agent/",
                       help="Where the dashboard is published; every line links its row there.")
    slack.add_argument("--webhook-url", default=os.environ.get("SLACK_WEBHOOK_URL", ""),
                       help="Slack incoming webhook (default: $SLACK_WEBHOOK_URL).")
    slack.add_argument("--issues", default=None,
                       help="JSON file mapping a failure's fingerprint to its detector issue URL.")
    slack.add_argument("--days", type=int, default=30, help="Days of history to read (default: 30).")
    slack.add_argument("--branch", default="unstable", help="Branch to report on (default: unstable).")
    slack.add_argument("--bar-runs", type=int, default=BAR_RUNS,
                       help=f"Runs shown in each line's history bar (default: {BAR_RUNS}).")
    slack.add_argument("--always", action="store_true", help="Post even when the newest run passed.")
    slack.add_argument("--dry-run", action="store_true", help="Print the message; post nothing.")
    slack.set_defaults(func=run_slack)
    return parser


def main(argv: Optional[List[str]] = None, make_client: ClientFactory = _github_client) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(verbose=args.verbose)
    return int(args.func(args, make_client))


if __name__ == "__main__":
    sys.exit(main())
