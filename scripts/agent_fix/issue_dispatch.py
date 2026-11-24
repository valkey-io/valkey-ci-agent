"""Start one fix run per issue that waits for one, up to a cap.

There are two queues: the open Daily detector issues, each fixed by a
``agent-fix-ci-issues.yml`` run, and the issues a committer labelled for a change
(:mod:`scripts.agent_fix.labelled`), each answered by a ``agent-fix-labelled.yml``
run. What follows describes the first; :func:`dispatch_labelled` describes
the second.

Each tick reads the target repository (never writes it) and dispatches
``agent-fix-ci-issues.yml`` in this repository for the newest open detector issues
that are worth an attempt. An issue is skipped while:

- an agent-fix-ci-issues run for it is queued or in progress (each run is named
  after its issue, see ``RUN_NAME``),
- it is assigned or an open pull request links it (``validate_issue``),
- the bot already reported a fix or a refusal for the occurrence it records
  (``fix_reported``), or ``MAX_FAILED_ATTEMPTS`` of its runs for that
  occurrence failed (``failed_attempts``; a failed attempt is never
  commented on the issue): a newer occurrence makes it eligible again.

At most ``max_runs`` agent-fix-ci-issues runs are active at once, counting the ones
already running. One run still handles exactly one issue.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from itertools import islice
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from github import Auth, Github

from scripts.agent_fix import labelled
from scripts.agent_fix.issues import IssueFailure, fix_reported, open_detector_issues, validate_issue
from scripts.agent_fix.models import MAX_FAILED_ATTEMPTS, IssuePublication
from scripts.common.github_client import retry_github_call
from scripts.common.identity import APP_LOGIN
from scripts.common.logging_utils import configure_logging

logger = logging.getLogger(__name__)

WORKFLOW = "agent-fix-ci-issues.yml"
# agent-fix-ci-issues.yml's run-name; the runs API does not return dispatch inputs.
RUN_NAME = re.compile(r"^Agent fix CI issue #(?P<issue>\d+)(?: \(run (?P<run>\d+)\))?$")
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


# The other queue: issues a committer labelled for a change. Its runs are
# named after the issue and the labelling they answer, as a detector issue's
# are after the issue and the occurrence; ``run`` is that labelling's event.
LABELLED_WORKFLOW = "agent-fix-labelled.yml"
LABELLED_RUN_NAME = re.compile(r"^Agent fix issue #(?P<issue>\d+)(?: \(label (?P<run>\d+)\))?")


def active_runs(workflow: Any, run_name: re.Pattern[str] = RUN_NAME) -> list[int]:
    """The issue of each queued or running run of ``workflow`` (0 when unnamed)."""
    runs = retry_github_call(
        lambda: list(islice(workflow.get_runs(), _RECENT_RUNS)),
        retries=2,
        description="list workflow runs",
    )
    active = []
    for run in runs:
        if str(getattr(run, "status", "") or "") not in _ACTIVE_STATUSES:
            continue
        match = run_name.match(str(getattr(run, "display_title", "") or ""))
        active.append(int(match.group("issue")) if match else 0)
    return active


def failed_attempts(workflow: Any, run_name: re.Pattern[str] = RUN_NAME) -> Counter[tuple[int, int]]:
    """Failed runs of ``workflow`` per (issue, occurrence run id or labelling).

    A failed attempt is not commented on its issue, so its failed run is the
    record. Runs dispatched before the run id was in their name match nothing.
    """
    runs = retry_github_call(
        lambda: list(islice(workflow.get_runs(status="failure"), _RECENT_RUNS)),
        retries=2,
        description="list failed workflow runs",
    )
    counts: Counter[tuple[int, int]] = Counter()
    for run in runs:
        match = run_name.match(str(getattr(run, "display_title", "") or ""))
        if match and match.group("run"):
            counts[(int(match.group("issue")), int(match.group("run")))] += 1
    return counts


def _skip_reason(
    issue: Any, failure: IssueFailure, *, repo_full_name: str, bot_login: str, running: set[int],
    failed: Counter[tuple[int, int]],
) -> str:
    """Why ``issue`` gets no run now ("" when it should get one). Cheapest checks first."""
    if failure.number in running:
        return f"an {WORKFLOW} run for it is in progress"
    _failure, rejection = validate_issue(issue, bot_login=bot_login)
    if rejection:
        return rejection
    if fix_reported(issue, failure, repo_full_name=repo_full_name, bot_login=bot_login):
        return f"the bot is done with run {failure.run_id}"
    if failed[(failure.number, failure.run_id)] >= MAX_FAILED_ATTEMPTS:
        return f"{MAX_FAILED_ATTEMPTS} attempts at run {failure.run_id} failed"
    return ""


def dispatch_issues(
    target: Any,
    workflow: Any,
    *,
    repo_full_name: str,
    bot_login: str,
    max_runs: int,
    dispatch: Callable[[int, int], None],
) -> list[Decision]:
    """Dispatch runs for eligible issues, newest first, until ``max_runs`` are active."""
    running = active_runs(workflow)
    slots = max_runs - len(running)
    if slots <= 0:
        logger.info("%d %s runs are active (cap %d); nothing to dispatch", len(running), WORKFLOW, max_runs)
        return []
    failed = failed_attempts(workflow)
    decisions = []
    for issue, failure in open_detector_issues(target, bot_login, limit=_MAX_ISSUES):
        if slots <= 0:
            break
        reason = _skip_reason(issue, failure, repo_full_name=repo_full_name, bot_login=bot_login,
                              running=set(running), failed=failed)
        if reason:
            decisions.append(Decision(issue.number, "skipped", reason))
            continue
        dispatch(issue.number, failure.run_id)
        decisions.append(Decision(issue.number, "dispatched"))
        slots -= 1
    return decisions


def dispatch_labelled(
    target: Any,
    workflow: Any,
    *,
    bot_login: str,
    max_runs: int,
    dispatch: Callable[[int, int, int], None],
) -> list[Decision]:
    """Dispatch a run for each labelled issue that waits for an answer, bugs first, until ``max_runs`` are active.

    Whether the labelling is one to act on is the run's to check, and to
    answer on the issue: this only reads. A labelling waits while a run for
    its issue is active, and is given up after ``MAX_FAILED_ATTEMPTS`` failed
    runs, the last of which says so on the issue. While changes are
    published as pull requests, nothing is dispatched once as many are open
    as may be, or after the last few were closed unmerged (``labelled.hold_reason``).
    """
    running = active_runs(workflow, LABELLED_RUN_NAME)
    slots = max_runs - len(running)
    if slots <= 0:
        logger.info("%d %s runs are active (cap %d); nothing to dispatch", len(running), LABELLED_WORKFLOW, max_runs)
        return []
    waiting = labelled.open_requests(target, bot_login, limit=_MAX_ISSUES)
    if waiting and labelled.publication() is IssuePublication.DRAFT_PR:
        held = labelled.hold_reason(target, bot_login)
        if held:
            return [Decision(issue.number, "skipped", held) for issue, _applied in waiting]
    failed = failed_attempts(workflow, LABELLED_RUN_NAME)
    decisions = []
    for issue, applied in waiting:
        if slots <= 0:
            break
        attempts = failed[(issue.number, applied.event)]
        if issue.number in running:
            decisions.append(Decision(issue.number, "skipped", f"an {LABELLED_WORKFLOW} run for it is in progress"))
        elif attempts >= MAX_FAILED_ATTEMPTS:
            decisions.append(Decision(issue.number, "skipped", f"{MAX_FAILED_ATTEMPTS} attempts at this labelling failed"))
        else:
            dispatch(issue.number, applied.event, attempts + 1)
            decisions.append(Decision(issue.number, "dispatched"))
            slots -= 1
    return decisions


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="the repository whose issues are fixed")
    parser.add_argument("--queue", choices=("detector", "labelled"), default="detector")
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
    name = LABELLED_WORKFLOW if args.queue == "labelled" else WORKFLOW
    workflow = Github(auth=Auth.Token(os.environ["GITHUB_TOKEN"])).get_repo(agent_repo).get_workflow(name)
    bot_login = f"{APP_LOGIN}[bot]"

    def start(issue: int, inputs: dict[str, str]) -> None:
        if args.dry_run:
            return
        dispatched = retry_github_call(
            lambda: workflow.create_dispatch(ref, {"issue": str(issue), **inputs}),
            retries=2,
            description=f"dispatch {name} for issue #{issue}",
        )
        if not dispatched:
            raise RuntimeError(f"{agent_repo}/{name}@{ref} rejected the dispatch for #{issue}")

    if args.queue == "labelled":
        decisions = dispatch_labelled(
            target, workflow, bot_login=bot_login, max_runs=args.max_runs,
            dispatch=lambda issue, label, attempt: start(issue, {"label_event": str(label), "attempt": str(attempt)}),
        )
    else:
        decisions = dispatch_issues(
            target, workflow, repo_full_name=args.repo, bot_login=bot_login,
            max_runs=args.max_runs, dispatch=lambda issue, run_id: start(issue, {"run_id": str(run_id)}),
        )
    if args.dry_run:
        decisions = [Decision(d.issue, "would dispatch") if d.action == "dispatched" else d for d in decisions]
    print(json.dumps([asdict(decision) for decision in decisions], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
