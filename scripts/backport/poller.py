"""Poll release branches and run a backport sweep when no sweep PR is open.

The daily sweep tops a rolling backport PR up to ``--max-candidates`` validated
cherry-picks, then waits for the next cron tick. When that PR merges, the next
batch of board candidates is not picked up until the following day.

This poller closes that gap without per-branch workflow files or cross-repo
dispatch. For each registered ``{repo, branch}`` it applies one rule:

    If an open sweep PR already exists for the branch, do nothing -- a human is
    reviewing it and we must not pile more cherry-picks on. Otherwise run a
    sweep, which discovers the current board state and opens a fresh PR.

The open-PR check is the entire state model: a merge closes the sweep PR, the
next poll sees the gap and tops the board back up, and the new PR self-locks the
branch again until it too merges. Run it on a short cron for near-merge latency.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from github import Auth, Github

from scripts.backport.registry import load_registry
from scripts.backport.sweep import _BRANCH_PREFIX, run_backport_sweep
from scripts.backport.sweep_prs import find_existing_pr

if TYPE_CHECKING:
    from scripts.backport.registry import BranchEntry, RepoEntry  # noqa: F401

logger = logging.getLogger(__name__)


def poll_branch(
    *,
    repo_entry: "RepoEntry",
    branch_entry: "BranchEntry",
    github_token: str,
    max_candidates: int = 2,
    dry_run: bool = False,
) -> dict:
    """Run a sweep for one branch unless an open sweep PR already exists.

    ``max_candidates`` defaults to 2 to match the daily sweep's effective cap,
    not ``run_backport_sweep``'s own default of 5.

    When ``dry_run`` is set, the open-PR check still runs but a branch with no
    open PR reports ``would-sweep`` instead of sweeping. This previews the
    poller's decision without any writes.

    Returns a result dict describing the action taken: ``skipped-open-pr`` when
    a sweep PR is already open, ``swept`` (or ``would-sweep`` under dry run) when
    no PR is open, or ``error`` when the open-PR check itself failed. A failed
    check degrades to an error result rather than crashing, matching how
    ``run_backport_sweep`` handles failures.
    """
    repo_full_name = repo_entry.repo
    push_repo = repo_entry.effective_push_repo
    target_branch = branch_entry.branch
    backport_branch = f"{_BRANCH_PREFIX}/{target_branch}"

    gh = Github(auth=Auth.Token(github_token))
    try:
        existing_pr = find_existing_pr(gh, repo_full_name, push_repo, backport_branch)
    except Exception as exc:
        logger.exception("Error checking for open sweep PR on %s", target_branch)
        return {
            "repo": repo_full_name,
            "branch": target_branch,
            "action": "error",
            "error": str(exc),
        }

    if existing_pr is not None:
        logger.info(
            "Branch %s: open sweep PR #%d exists, skipping",
            target_branch,
            existing_pr.number,
        )
        return {
            "repo": repo_full_name,
            "branch": target_branch,
            "action": "skipped-open-pr",
            "pr": existing_pr.html_url,
        }

    if dry_run:
        logger.info("Branch %s: no open sweep PR, would sweep (dry run)", target_branch)
        return {
            "repo": repo_full_name,
            "branch": target_branch,
            "action": "would-sweep",
        }

    logger.info("Branch %s: no open sweep PR, running sweep", target_branch)
    result = run_backport_sweep(
        repo_entry=repo_entry,
        branch_entry=branch_entry,
        github_token=github_token,
        max_candidates=max_candidates,
    )
    return {
        "repo": repo_full_name,
        "branch": target_branch,
        "action": "swept",
        "found": result.candidates_found,
        "applied": result.applied_count,
        "pr": result.pr_url,
        "error": result.error,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registry",
        default="repos.yml",
        help="Path to registry YAML (default: repos.yml)",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="Repository full name (must exist in registry)",
    )
    parser.add_argument(
        "--branch",
        required=True,
        help="Target branch (must exist in registry for this repo)",
    )
    parser.add_argument("--target-token", required=True)
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=2,
        help="Cap the number of applied cherry-picks per branch (0 = unlimited)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the poll decision without running a sweep",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    registry = load_registry(args.registry)
    repo_entry, branch_entry = registry.get_branch(args.repo, args.branch)

    result = poll_branch(
        repo_entry=repo_entry,
        branch_entry=branch_entry,
        github_token=args.target_token,
        max_candidates=args.max_candidates,
        dry_run=args.dry_run,
    )

    print(json.dumps(result, indent=2))

    if result.get("error"):
        logger.error(
            "Backport poll failure: %s: %s",
            result["branch"],
            result["error"],
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
