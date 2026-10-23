"""Start one ci-fix-issues run per open Daily detector issue, up to a cap.

Each tick reads the target repository (never writes it) and dispatches
``ci-fix-issues.yml`` in this repository for the newest open detector issues
that are worth an attempt. An issue is skipped while:

- a ci-fix-issues run for it is queued or in progress (each run is named
  after its issue, see ``RUN_NAME``),
- it is assigned or an open pull request links it (``validate_issue``),
- the bot already reported a fix or a refusal for the occurrence it records,
  or two attempts that failed (``fix_reported``): a newer occurrence makes it
  eligible again.

At most ``max_runs`` ci-fix-issues runs are active at once, counting the ones
already running. One run still handles exactly one issue.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from github import Auth, Github

from scripts.ci_fix.issues import IssueFailure, fix_reported, open_detector_issues, validate_issue
from scripts.common.github_client import retry_github_call
from scripts.common.identity import APP_LOGIN
from scripts.common.logging_utils import configure_logging

logger = logging.getLogger(__name__)

WORKFLOW = "ci-fix-issues.yml"
# ci-fix-issues.yml's run-name; the runs API does not return dispatch inputs.
RUN_NAME = re.compile(r"^CI fix issue #(?P<issue>\d+)$")
_ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "requested", "pending"})
# Recent runs examined for active ones. A run outlives far fewer later runs
# than this at any realistic cap.
_RECENT_RUNS = 100
# Newest open detector issues considered per tick.
_MAX_ISSUES = 100


@dataclass(frozen=True)
class Decision:
    issue: int
    action: str  # "dispatched", "would dispatch" (dry run) or "skipped"
    reason: str = ""


def active_runs(workflow: Any) -> list[int]:
    """The issue of each queued or running ci-fix-issues run (0 when unnamed)."""
    runs = retry_github_call(
        lambda: list(workflow.get_runs()[:_RECENT_RUNS]),
        retries=2,
        description=f"list {WORKFLOW} runs",
    )
    active = []
    for run in runs:
        if str(getattr(run, "status", "") or "") not in _ACTIVE_STATUSES:
            continue
        match = RUN_NAME.match(str(getattr(run, "display_title", "") or ""))
        active.append(int(match.group("issue")) if match else 0)
    return active


def _skip_reason(
    issue: Any, failure: IssueFailure, *, repo_full_name: str, bot_login: str, running: set[int],
) -> str:
    """Why ``issue`` gets no run now ("" when it should get one). Cheapest checks first."""
    if failure.number in running:
        return f"a {WORKFLOW} run for it is in progress"
    _failure, rejection = validate_issue(issue, bot_login=bot_login)
    if rejection:
        return rejection
    if fix_reported(issue, failure, repo_full_name=repo_full_name, bot_login=bot_login):
        return f"the bot is done with run {failure.run_id}"
    return ""


def dispatch_issues(
    target: Any,
    workflow: Any,
    *,
    repo_full_name: str,
    bot_login: str,
    max_runs: int,
    dispatch: Callable[[int], None],
) -> list[Decision]:
    """Dispatch runs for eligible issues, newest first, until ``max_runs`` are active."""
    running = active_runs(workflow)
    slots = max_runs - len(running)
    if slots <= 0:
        logger.info("%d %s runs are active (cap %d); nothing to dispatch", len(running), WORKFLOW, max_runs)
        return []
    decisions = []
    for issue, failure in open_detector_issues(target, bot_login, limit=_MAX_ISSUES):
        if slots <= 0:
            break
        reason = _skip_reason(issue, failure, repo_full_name=repo_full_name, bot_login=bot_login,
                              running=set(running))
        if reason:
            decisions.append(Decision(issue.number, "skipped", reason))
            continue
        dispatch(issue.number)
        decisions.append(Decision(issue.number, "dispatched"))
        slots -= 1
    return decisions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="the repository whose detector issues are fixed")
    parser.add_argument("--max-runs", type=int, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.max_runs < 1:
        parser.error("--max-runs must be at least 1")
    configure_logging()

    # The target token reads the target repository; this repository's own
    # token lists and dispatches its runs.
    target = Github(auth=Auth.Token(os.environ["TARGET_TOKEN"])).get_repo(args.repo)
    agent_repo, ref = os.environ["GITHUB_REPOSITORY"], os.environ.get("GITHUB_REF_NAME", "main")
    workflow = Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"])).get_repo(agent_repo).get_workflow(WORKFLOW)
    bot_login = f"{APP_LOGIN}[bot]"

    def dispatch(issue: int) -> None:
        if args.dry_run:
            return
        dispatched = retry_github_call(
            lambda: workflow.create_dispatch(ref, {"issue": str(issue)}),
            retries=2,
            description=f"dispatch {WORKFLOW} for issue #{issue}",
        )
        if not dispatched:
            raise RuntimeError(f"{agent_repo}/{WORKFLOW}@{ref} rejected the dispatch for #{issue}")

    decisions = dispatch_issues(
        target, workflow, repo_full_name=args.repo, bot_login=bot_login,
        max_runs=args.max_runs, dispatch=dispatch,
    )
    if args.dry_run:
        decisions = [Decision(d.issue, "would dispatch") if d.action == "dispatched" else d for d in decisions]
    print(json.dumps([asdict(decision) for decision in decisions], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
