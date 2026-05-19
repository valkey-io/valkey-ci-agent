"""Backport PR creation and duplicate detection."""

from __future__ import annotations

import logging
from typing import Any

from github import Github

from scripts.backport.models import (
    BackportPRContext,
    CherryPickResult,
    ResolutionResult,
)
from scripts.backport.utils import build_branch_name, build_pr_title
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)


def build_pull_create_head_ref(
    base_repo: str,
    push_repo: str | None,
    branch_name: str,
) -> str:
    """Return the head ref used when creating a pull request."""
    if not push_repo or push_repo == base_repo:
        return branch_name
    owner = push_repo.split("/")[0]
    return f"{owner}:{branch_name}"


def build_pull_search_head_ref(
    base_repo: str,
    push_repo: str | None,
    branch_name: str,
) -> str:
    """Return the head ref used when searching pull requests."""
    source_repo = push_repo or base_repo
    owner = source_repo.split("/")[0]
    return f"{owner}:{branch_name}"


def create_pull_from_push_repo(
    repo: Any,
    *,
    base_repo: str,
    push_repo: str | None,
    title: str,
    body: str,
    head_branch: str,
    base_branch: str,
    draft: bool | None = None,
) -> Any:
    """Create a PR from either the upstream branch or a different-owner fork."""
    head_ref = build_pull_create_head_ref(base_repo, push_repo, head_branch)
    kwargs: dict[str, Any] = {
        "title": title,
        "body": body,
        "head": head_ref,
        "base": base_branch,
    }
    if draft is not None:
        kwargs["draft"] = draft
    return repo.create_pull(**kwargs)


def pull_matches_push_repo(pr: Any, push_repo: str) -> bool:
    """Return whether a PR head belongs to the expected push repo."""
    head = getattr(pr, "head", None)
    repo = getattr(head, "repo", None)
    full_name = getattr(repo, "full_name", None)
    return isinstance(full_name, str) and full_name == push_repo


def _escape_table_cell(value: object) -> str:
    """Return markdown-table-safe text."""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return ""
    return text.replace("|", "\\|").replace("\n", "<br>")


def _was_llm_resolved(result: ResolutionResult) -> bool:
    return result.resolved_content is not None and result.source == "llm"


class BackportPRCreator:
    """Create backport branches and pull requests via the GitHub API."""

    def __init__(
        self,
        github_client: Github,
        base_repo: str,
        *,
        push_repo: str | None = None,
        backport_label: str = "backport",
        llm_conflict_label: str = "llm-resolved-conflicts",
    ) -> None:
        self._github = github_client
        self._base_repo = base_repo
        self._push_repo = push_repo
        self._backport_label = backport_label or "backport"
        self._llm_conflict_label = llm_conflict_label or "llm-resolved-conflicts"

    def create_backport_pr(
        self,
        context: BackportPRContext,
        cherry_pick_result: CherryPickResult,
        resolution_results: list[ResolutionResult] | None,
        branch_name: str | None = None,
    ) -> str:
        """Create backport PR from an already-pushed branch.

        If *branch_name* is provided, the branch is assumed to already
        exist on the remote (pushed from the local cherry-pick clone).
        Otherwise, falls back to creating the branch via the API from
        target branch HEAD (useful for testing).

        Returns the PR URL.

        """
        repo = retry_github_call(
            lambda: self._github.get_repo(self._base_repo),
            retries=3,
            description=f"get repo {self._base_repo}",
        )

        if branch_name is None:
            branch_name = build_branch_name(
                context.source_pr_number, context.target_branch,
            )
        assert branch_name is not None  # for mypy
        title = build_pr_title(context.source_pr_title, context.target_branch)

        had_conflicts = not cherry_pick_result.success
        any_llm_resolved = bool(
            resolution_results and any(_was_llm_resolved(r) for r in resolution_results)
        )

        body = self.build_pr_body(context, had_conflicts, resolution_results,
                                  applied_commits=cherry_pick_result.applied_commits)

        # Open the pull request (branch already exists on remote).
        logger.info(
            "Opening backport PR: %s -> %s", branch_name, context.target_branch,
        )
        pr = retry_github_call(
            lambda: create_pull_from_push_repo(
                repo,
                base_repo=self._base_repo,
                push_repo=self._push_repo,
                title=title,
                body=body,
                head_branch=branch_name,
                base_branch=context.target_branch,
            ),
            retries=3,
            description="create backport PR",
        )

        # Apply labels (best-effort — don't fail the run if labels are missing).
        labels = [self._backport_label]
        if any_llm_resolved:
            labels.append(self._llm_conflict_label)

        try:
            logger.info("Applying labels %s to PR #%d", labels, pr.number)
            retry_github_call(
                lambda: pr.add_to_labels(*labels),
                retries=3,
                description="apply labels to backport PR",
            )
        except Exception as exc:
            logger.warning("Failed to apply labels to PR #%d: %s", pr.number, exc)

        logger.info("Backport PR created: %s", pr.html_url)
        return pr.html_url

    @staticmethod
    def build_pr_body(
        context: BackportPRContext,
        had_conflicts: bool,
        resolution_results: list[ResolutionResult] | None,
        *,
        applied_commits: list[str] | None = None,
    ) -> str:
        """Build the PR body with links, commit list, conflict info.

        Includes:
        * Link to the source PR
        * List of cherry-picked commit SHAs
        * Whether conflicts were encountered
        * Per-file LLM resolution summaries (when applicable)
        * Human review disclaimer (when any file was LLM-resolved)

        """
        sections: list[str] = []
        results = resolution_results or []
        resolved_count = sum(result.resolved_content is not None for result in results)
        unresolved_count = len(results) - resolved_count

        if had_conflicts:
            if unresolved_count > 0:
                verdict = (
                    "Cherry-pick encountered conflicts and some files still need "
                    "manual follow-up."
                )
            elif resolved_count > 0:
                verdict = (
                    "Cherry-pick encountered conflicts and the conflicted files were "
                    "resolved automatically."
                )
            else:
                verdict = "Cherry-pick encountered conflicts."
        else:
            verdict = "Cherry-pick applied cleanly with no conflicts."

        sections.append("## Backport Summary\n\n" + verdict)
        sections.append(
            "\n".join([
                "| Field | Value |",
                "|---|---|",
                f"| Source PR | [#{context.source_pr_number}]({context.source_pr_url}) |",
                f"| Source title | {_escape_table_cell(context.source_pr_title)} |",
                f"| Target branch | `{context.target_branch}` |",
                f"| Cherry-picked commits | {len(applied_commits or context.commits)} |",
                f"| Conflicts detected | {'yes' if had_conflicts else 'no'} |",
                f"| Auto-resolved files | {resolved_count} |",
                f"| Unresolved files | {unresolved_count} |",
            ])
        )
        checklist = [
            "- Compare this backport against the source PR before merge.",
        ]
        if resolved_count > 0:
            checklist.append(
                "- Review the automatically resolved files carefully for semantic drift."
            )
        if unresolved_count > 0:
            checklist.append(
                "- Resolve the remaining conflicted files or close the PR if the backport is not viable."
            )
        sections.append("### Reviewer Checklist\n\n" + "\n".join(checklist))

        commits_list = "\n".join(f"- `{sha}`" for sha in (applied_commits or context.commits))
        sections.append(f"### Cherry-Picked Commits\n\n{commits_list}")

        # Per-file resolution summaries.
        if results:
            file_lines: list[str] = []
            for result in results:
                status = (
                    "Resolved automatically" if result.resolved_content is not None
                    else "Needs manual resolution"
                )
                file_lines.append(
                    f"- `{result.path}`: {status}. {result.resolution_summary}"
                )
            sections.append(
                "### Conflict Details\n\n" + "\n".join(file_lines)
            )

        # Human review disclaimer (when any file was LLM-resolved).
        any_llm_resolved = bool(results and any(_was_llm_resolved(r) for r in results))
        if any_llm_resolved:
            sections.append(
                "### Human Review Required\n\n"
                "Some conflicts in this backport were resolved using an LLM. "
                "These resolutions require careful human review to ensure "
                "correctness. Please verify that the resolved code matches "
                "the intent of the original pull request."
            )

        return "\n\n".join(sections)

    def check_duplicate(
        self,
        source_pr_number: int,
        target_branch: str,
    ) -> str | None:
        """Return existing backport PR URL if one exists, else ``None``.

        Checks for open PRs whose head branch matches the naming
        convention ``backport/<pr>-to-<branch>``.  Also checks recently
        closed PRs to handle label removal and re-addition.

        """
        branch_name = build_branch_name(source_pr_number, target_branch)

        repo = retry_github_call(
            lambda: self._github.get_repo(self._base_repo),
            retries=3,
            description=f"get repo {self._base_repo}",
        )

        # Check open PRs with matching head branch.
        head_ref = build_pull_search_head_ref(
            self._base_repo,
            self._push_repo,
            branch_name,
        )
        logger.info(
            "Checking for duplicate backport PR with head ref %s",
            head_ref,
        )
        open_pulls = retry_github_call(
            lambda: repo.get_pulls(state="open", head=head_ref),
            retries=3,
            description="search open PRs for duplicate",
        )
        expected_push_repo = self._push_repo or self._base_repo
        for pr in open_pulls:
            if not pull_matches_push_repo(pr, expected_push_repo):
                continue
            logger.info("Found existing open backport PR: %s", pr.html_url)
            return pr.html_url

        # Check closed PRs with matching head branch. Only treat a closed
        # PR as a duplicate if it was merged — a closed-but-not-merged PR
        # means the work was abandoned, and we should be free to reopen a
        # fresh backport. GitHub returns merged PRs as state=closed with
        # merged_at set.
        closed_pulls = retry_github_call(
            lambda: repo.get_pulls(state="closed", head=head_ref),
            retries=3,
            description="search closed PRs for duplicate",
        )
        for pr in closed_pulls:
            if not pull_matches_push_repo(pr, expected_push_repo):
                continue
            if pr.merged_at is not None:
                logger.info(
                    "Found existing merged backport PR: %s", pr.html_url,
                )
                return pr.html_url

        logger.info("No duplicate backport PR found for %s", branch_name)
        return None
