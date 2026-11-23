"""Command line for CI health: sync the store, then report from it.

Every command reads from GitHub at most; nothing here writes to a
repository, an issue or a comment. ``sync`` brings the store up to date,
``scorecard`` and ``blame`` compute from it and write JSON. The workflow
runs the three in that order; by hand, run whichever you need.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Callable, List, Optional

from github import Auth, Github

from scripts.ci_health.blame import compute_blame
from scripts.ci_health.scorecard import compute_scorecards
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
    return parser


def main(argv: Optional[List[str]] = None, make_client: ClientFactory = _github_client) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(verbose=args.verbose)
    return int(args.func(args, make_client))


if __name__ == "__main__":
    sys.exit(main())
