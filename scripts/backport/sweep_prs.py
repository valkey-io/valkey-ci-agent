"""Pull request and remote-branch operations for backport sweeps."""

from __future__ import annotations

import logging
from typing import Any

from github.GithubException import GithubException

from scripts.backport.pr_creator import (
    build_pull_search_head_ref,
    create_pull_from_push_repo,
    pull_matches_push_repo,
)
from scripts.backport.sweep_graphql import GitHubGraphQLClient
from scripts.backport.sweep_models import BranchSweepResult, CandidateResult
from scripts.backport.sweep_reporting import build_pr_body
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)


def find_existing_pr(gh: Any, base_repo: str, push_repo: str, branch: str) -> Any | None:
    repo = retry_github_call(lambda: gh.get_repo(base_repo), retries=2, description=f"get {base_repo}")
    head_ref = build_pull_search_head_ref(base_repo, push_repo, branch)
    pulls = retry_github_call(
        lambda: list(repo.get_pulls(state="open", head=head_ref)),
        retries=2, description="list PRs",
    )
    for pull in pulls:
        if pull_matches_push_repo(pull, push_repo):
            return pull
    return None


def delete_stale_backport_branch(gh: Any, push_repo: str, branch: str) -> None:
    repo = retry_github_call(lambda: gh.get_repo(push_repo), retries=2, description=f"get {push_repo}")
    try:
        ref = retry_github_call(
            lambda: repo.get_git_ref(f"heads/{branch}"),
            retries=1,
            description=f"check ref {branch}",
        )
    except GithubException as exc:
        if exc.status == 404:
            return
        logger.warning("Could not prune stale backport branch %s: %s", branch, exc)
        return
    logger.info("Deleting stale backport branch %s on %s (no open PR)", branch, push_repo)
    retry_github_call(lambda: ref.delete(), retries=2, description=f"delete ref {branch}")


def upsert_pr(
    gh: Any,
    base_repo: str,
    push_repo: str,
    target_branch: str,
    head_branch: str,
    result: BranchSweepResult,
    existing_pr: Any | None,
    gql: GitHubGraphQLClient | None = None,
    branch_applied: list[CandidateResult] | None = None,
) -> str:
    repo = retry_github_call(lambda: gh.get_repo(base_repo), retries=2, description=f"get {base_repo}")
    previous_body = getattr(existing_pr, "body", None) if existing_pr else None
    body = build_pr_body(
        result,
        branch_applied=branch_applied,
        previous_body=previous_body if isinstance(previous_body, str) else None,
    )
    title = f"[backport] Backport sweep for {target_branch}"

    if existing_pr:
        retry_github_call(lambda: existing_pr.edit(title=title, body=body), retries=2, description="update PR")
        # The sweep branch is always green, so any PR we update is ready for
        # review. Promote a leftover draft (e.g. from an older sweep) back to
        # ready. PyGithub does not expose this transition, so use GraphQL.
        if getattr(existing_pr, "draft", False) and gql is not None:
            node_id = getattr(existing_pr, "node_id", None)
            if node_id:
                mark_pr_ready_for_review(gql, node_id)
                logger.info(
                    "Marked PR #%d on %s ready for review",
                    existing_pr.number, base_repo,
                )
        logger.info("Updated PR #%d on %s", existing_pr.number, base_repo)
        return existing_pr.html_url

    pr = retry_github_call(
        lambda: create_pull_from_push_repo(
            repo,
            base_repo=base_repo,
            push_repo=push_repo,
            title=title,
            body=body,
            head_branch=head_branch,
            base_branch=target_branch,
            draft=False,
        ),
        retries=2,
        description="create PR",
    )
    logger.info("Created PR #%d on %s", pr.number, base_repo)
    return pr.html_url


def mark_pr_ready_for_review(gql: GitHubGraphQLClient, pr_node_id: str) -> None:
    mutation = """
    mutation($id: ID!) {
      markPullRequestReadyForReview(input: {pullRequestId: $id}) {
        pullRequest { isDraft }
      }
    }
    """
    gql.execute(mutation, {"id": pr_node_id})
