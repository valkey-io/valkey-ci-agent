"""Pull request and remote-branch operations for backport sweeps."""

from __future__ import annotations

import logging
import os
from typing import Any

from github.GithubException import GithubException

from scripts.backport.diff_comments import marked_source_pr_urls, reconcile_diff_comments
from scripts.backport.pr_creator import (
    _LABEL_DEFAULTS,
    build_pull_search_head_ref,
    create_pull_from_push_repo,
    pull_matches_push_repo,
)
from scripts.backport.sweep_graphql import GitHubGraphQLClient
from scripts.backport.sweep_models import BranchSweepResult, CandidateResult
from scripts.backport.sweep_reporting import build_pr_body, result_is_on_backport_branch
from scripts.common.github_client import retry_github_call
from scripts.common.proc import BOT_NAME

logger = logging.getLogger(__name__)

# See scripts/backport/main.py: the comment author follows the token identity,
# so a fork run with a personal PAT overrides the ownership-gate login.
DIFF_COMMENT_LOGIN = os.environ.get("CI_AGENT_DIFF_COMMENT_LOGIN") or BOT_NAME


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
    backport_label: str = "backport",
    llm_conflict_label: str = "ai-resolved-conflicts",
) -> str:
    repo = retry_github_call(lambda: gh.get_repo(base_repo), retries=2, description=f"get {base_repo}")
    previous_body = getattr(existing_pr, "body", None) if existing_pr else None
    body = build_pr_body(
        result,
        branch_applied=branch_applied,
        previous_body=previous_body if isinstance(previous_body, str) else None,
    )
    title = f"[backport] Backport sweep for {target_branch}"

    # The AI-resolved signal is durable on each candidate, so scan this run's
    # results and everything already applied to the branch; a PR that first
    # gains an AI-resolved commit in a later top-up run still gets the label.
    labels = [backport_label]
    all_candidates = list(result.results) + list(branch_applied or [])
    if any(getattr(c, "resolved_by_ai", False) for c in all_candidates):
        labels.append(llm_conflict_label)

    if existing_pr:
        retry_github_call(lambda: existing_pr.edit(title=title, body=body), retries=2, description="update PR")
        _apply_labels(repo, existing_pr, labels)
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
        comment_urls = _reconcile_sweep_diff_comments(
            existing_pr, result, branch_applied=branch_applied,
        )
        _relink_body_to_comments(
            existing_pr, result, branch_applied, comment_urls,
        )
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
    _apply_labels(repo, pr, labels)
    comment_urls = _reconcile_sweep_diff_comments(pr, result, branch_applied=branch_applied)
    _relink_body_to_comments(pr, result, branch_applied, comment_urls)
    return pr.html_url


def _apply_labels(repo: Any, pr: Any, labels: list[str]) -> None:
    """Apply *labels* to *pr*, creating any missing on *repo* first.

    Best-effort and idempotent: ``add_to_labels`` re-adds existing labels
    without error, so re-running the sweep on an already-labeled PR is a
    no-op. A failure here is logged and swallowed so labeling never fails a
    sweep whose branch is already green and pushed.
    """
    for label in labels:
        _ensure_label_exists(repo, label)
    try:
        logger.info("Applying labels %s to PR #%d", labels, pr.number)
        retry_github_call(
            lambda: pr.add_to_labels(*labels),
            retries=3,
            description="apply labels to sweep PR",
        )
    except Exception as exc:  # noqa: BLE001 - labeling must not fail the sweep
        logger.warning("Failed to apply labels to PR #%d: %s", pr.number, exc)


def _ensure_label_exists(repo: Any, label: str) -> None:
    """Create *label* on *repo* if it does not already exist. Best-effort."""
    try:
        retry_github_call(
            lambda: repo.get_label(label),
            retries=3,
            description=f"check label {label!r}",
        )
        return
    except GithubException as exc:
        if exc.status != 404:
            logger.warning("Could not verify label %r: %s", label, exc)
            return
    except Exception as exc:  # noqa: BLE001 - transport/parse failure is non-fatal
        logger.warning("Could not verify label %r: %s", label, exc)
        return

    color, description = _LABEL_DEFAULTS.get(
        label, ("ededed", f"Created by valkey-ci-agent for label {label!r}"),
    )
    try:
        logger.info("Creating missing label %r", label)
        retry_github_call(
            lambda: repo.create_label(name=label, color=color, description=description),
            retries=3,
            description=f"create label {label!r}",
        )
    except GithubException as exc:
        if exc.status == 422:  # created concurrently — fine
            return
        logger.error("Failed to create label %r: %s", label, exc)
    except Exception as exc:  # noqa: BLE001 - transport/parse failure is non-fatal
        logger.error("Failed to create label %r: %s", label, exc)


def _relink_body_to_comments(
    pr: Any,
    result: BranchSweepResult,
    branch_applied: list[CandidateResult] | None,
    comment_urls: dict[int, str],
) -> None:
    """Rebuild the PR body so each applied row links to its AI-diff comment.

    Best-effort: the comments already exist, so a body re-edit failure here
    must never fail the sweep.
    """
    if not comment_urls:
        return
    try:
        linked = build_pr_body(
            result,
            branch_applied=branch_applied,
            previous_body=getattr(pr, "body", None) if isinstance(getattr(pr, "body", None), str) else None,
            comment_urls=comment_urls,
        )
        retry_github_call(
            lambda: pr.edit(body=linked), retries=2, description="relink PR body to comments",
        )
    except Exception as exc:
        logger.warning("Failed to relink sweep PR body to comments: %s", exc)


def _reconcile_sweep_diff_comments(
    pr: Any,
    result: BranchSweepResult,
    *,
    branch_applied: list[CandidateResult] | None = None,
) -> dict[int, str]:
    """Reconcile AI-resolution diff comments on the sweep PR.

    Each currently-applied source PR's comments are reconciled against its
    fresh resolutions. A prior comment group is deleted only when its source
    PR is no longer on the sweep branch at all. A source PR that is still on
    the branch but was not freshly re-resolved this run (e.g. it shows up as
    already-on-branch) keeps its comments untouched, so reruns do not wipe
    still-relevant diffs. The marker identity is the source PR number.

    Returns ``{source_pr: comment_url}`` so the PR body can link each row to its
    AI-resolution comment. Best-effort: a comment failure must never fail the
    sweep.
    """
    # Fresh resolutions by source PR, only for candidates on the branch this run.
    desired_by_pr: dict[int, CandidateResult] = {
        c.source_pr_number: c
        for c in result.results
        if c.outcome == "applied" and c.resolutions
    }
    # Every source PR still represented on the sweep branch (applied this run or
    # already on the branch from a prior sweep). Their comments must be kept.
    on_branch = {
        c.source_pr_number
        for c in result.results
        if result_is_on_backport_branch(c)
    }
    if branch_applied is not None:
        on_branch.update(
            c.source_pr_number
            for c in branch_applied
            if result_is_on_backport_branch(c)
        )

    try:
        prior_urls = marked_source_pr_urls(pr, bot_login=DIFF_COMMENT_LOGIN)
        prior = set(prior_urls)
    except Exception as exc:
        logger.warning("Could not list prior AI-diff comment groups on sweep PR: %s", exc)
        prior_urls = {}
        prior = set()

    # Reconcile fresh resolutions; delete only groups whose source PR has left
    # the branch entirely.
    stale = {pr_num for pr_num in prior if pr_num not in on_branch and pr_num not in desired_by_pr}
    comment_urls: dict[int, str] = {
        pr_num: url
        for pr_num, url in prior_urls.items()
        if pr_num in on_branch and pr_num not in desired_by_pr
    }
    for source_pr in sorted(desired_by_pr.keys() | stale):
        candidate = desired_by_pr.get(source_pr)
        try:
            links = reconcile_diff_comments(
                pr,
                source_pr,
                candidate.resolutions if candidate else [],
                source_title=candidate.source_pr_title if candidate else None,
                resolved_commit_sha=candidate.resolved_commit_sha if candidate else None,
                bot_login=DIFF_COMMENT_LOGIN,
            )
            # All paths for a source PR point at the same grouped comment.
            if links:
                comment_urls[source_pr] = next(iter(links.values()))
        except Exception as exc:
            logger.warning(
                "Failed to reconcile diff comments for source PR #%d on sweep PR: %s",
                source_pr, exc,
            )
    return comment_urls


def mark_pr_ready_for_review(gql: GitHubGraphQLClient, pr_node_id: str) -> None:
    mutation = """
    mutation($id: ID!) {
      markPullRequestReadyForReview(input: {pullRequestId: $id}) {
        pullRequest { isDraft }
      }
    }
    """
    gql.execute(mutation, {"id": pr_node_id})
