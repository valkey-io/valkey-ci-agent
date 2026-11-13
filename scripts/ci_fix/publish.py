"""Publish a prepared CI-fix candidate.

A candidate is published in one of these ways:

* push to a bot-owned PR branch, or, for a backport sweep PR, to the CI-sweep
  PR into it, which it opens when there is none;
* for a sweep PR's failure that a merged upstream commit fixes, name the
  upstream PR the sweep should backport;
* suggest a patch on a contributor PR;
* post a patch on a detector issue; or
* open a draft fix PR for a detector issue.

The PR's own CI decides; this module never merges.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from github.GithubException import GithubException

from scripts.backport import ci_sweep
from scripts.backport.pr_creator import create_pull_from_push_repo
from scripts.backport.sweep_prs import delete_stale_backport_branch, find_existing_pr
from scripts.ci_fix.comment import candidate_context, prose
from scripts.ci_fix.issue_state import has_open_linked_pr, parse_detector_occurrence
from scripts.ci_fix.models import (
    CandidateAction,
    Failure,
    FixCandidate,
    FixResult,
    FixTask,
    IssuePublication,
    ResultKind,
    TargetKind,
)
from scripts.ci_fix.policy import allows_ci_changes, can_update_branch, is_bot_author, target_capabilities
from scripts.ci_fix.push import (
    PushFailed,
    PushRefused,
    PushStale,
    commit_message,
    fit_subject,
    push_candidate,
)
from scripts.common.git_clone import is_full_sha
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)

# How _validate_issue_target says a PR already links the issue, which
# reconcile_draft_pr checks for.
_LINKED_PR = "already has an open linked pull request"


class TargetRevalidationFailed(Exception):
    """The target could not be revalidated because GitHub was unavailable."""


def publish_candidate(
    gh: Any,
    task: FixTask,
    candidate: FixCandidate,
    *,
    git_env: dict[str, str],
    issue_publication: IssuePublication = IssuePublication.DRAFT_PR,
) -> FixResult:
    """Publish ``candidate`` according to ``task.target``.

    ``issue_publication`` applies to a detector issue's fix: as a draft PR,
    or as a patch posted on the issue, which pushes nothing.
    """
    if candidate.action is CandidateAction.REFUSE:
        return FixResult(
            kind=ResultKind.REFUSED,
            summary=candidate.summary or candidate.reasoning or "No safe fix was found.",
            task=task,
            candidate=candidate,
        )

    target = task.target
    if not target_capabilities(target.kind).allows(candidate.action):
        return _refused(
            task,
            candidate,
            f"The {candidate.action.value} action is not supported for "
            f"a {target.kind.value} target.",
        )

    if target.fix_branch and candidate.action is CandidateAction.PORT:
        return _suggest_backport(gh, task, candidate)
    if target.kind is TargetKind.SUGGEST:
        return _publish_suggestion(gh, task, candidate)
    if candidate.not_run and candidate.action is CandidateAction.PATCH:
        # A patch the bot wrote and could not run is never pushed: it is
        # posted, for a person to apply. A port ran where it was merged.
        if target.kind is TargetKind.DRAFT_PR:
            return _publish_issue_comment(gh, task, candidate)
        limit = target_capabilities(TargetKind.SUGGEST).patch_limit
        if len(candidate.patch) > limit:
            return _refused(
                task,
                candidate,
                f"The fix could not be run here, so it was not pushed, and its {len(candidate.patch):,}-character "
                f"patch is more than a comment holds ({limit:,}).",
            )
        return _publish_suggestion(gh, task, candidate)
    if target.kind is TargetKind.UPDATE_PR:
        return _publish_update(gh, task, candidate, git_env=git_env)
    if issue_publication is IssuePublication.COMMENT:
        return _publish_issue_comment(gh, task, candidate)
    return _publish_draft(gh, task, candidate, git_env=git_env)


def _publish_issue_comment(gh: Any, task: FixTask, candidate: FixCandidate) -> FixResult:
    """The fix as a patch for the issue's comment: nothing pushed, the issue rechecked first."""
    try:
        rejection = _validate_issue_target(gh, task)
    except TargetRevalidationFailed as exc:
        return _failed(task, candidate, str(exc))
    if rejection:
        return _refused(task, candidate, f"Not posting the patch: {rejection}")
    limit = target_capabilities(TargetKind.SUGGEST).patch_limit
    if len(candidate.patch) > limit:
        # The candidate was prepared under the draft PR's limit; a comment
        # holds less. Said plainly, rather than a comment without its patch.
        # A patch that was not run is never pushed, so a draft PR is no help.
        advice = "" if candidate.not_run else " Set CI_FIX_ISSUES_PUBLISH=draft-pr for a fix this size."
        return _refused(
            task,
            candidate,
            f"The fix is a {len(candidate.patch):,}-character patch, more than a comment holds "
            f"({limit:,}); it was not posted.{advice}",
        )
    return FixResult(
        kind=ResultKind.SUGGESTED,
        summary="Prepared a patch, to post on the issue.",
        task=task,
        candidate=candidate,
    )


def _publish_suggestion(gh: Any, task: FixTask, candidate: FixCandidate) -> FixResult:
    try:
        rejection = _validate_pr_target(gh, task, require_bot=False)
    except TargetRevalidationFailed as exc:
        return _failed(task, candidate, str(exc))
    if rejection:
        return _refused(task, candidate, f"Not posting the suggestion: {rejection}.")
    return FixResult(
        kind=ResultKind.SUGGESTED,
        summary=(
            "Prepared a patch that could not be run here, so it was not pushed."
            if candidate.not_run else "Prepared a patch for the PR author."
        ),
        task=task,
        candidate=candidate,
    )


def _publish_update(
    gh: Any,
    task: FixTask,
    candidate: FixCandidate,
    *,
    git_env: dict[str, str],
) -> FixResult:
    if task.target.fix_branch:
        return _publish_ci_sweep(gh, task, candidate, git_env=git_env)

    def check() -> str:
        return _validate_pr_target(gh, task, require_bot=True)

    try:
        commit_sha = push_candidate(
            candidate,
            task.target,
            git_env=git_env,
            pre_push_check=check,
            allow_ci_changes=allows_ci_changes(task),
        )
    except PushRefused as exc:
        return _refused(task, candidate, str(exc))
    except PushFailed as exc:
        return _failed(task, candidate, str(exc))
    return FixResult(
        kind=ResultKind.PUSHED,
        summary=(
            f"Ported an upstream fix for {candidate.failing_check or task.failure.job_name}."
            if candidate.action is CandidateAction.PORT
            else f"Pushed a fix for {candidate.failing_check or task.failure.job_name}."
        ),
        task=task,
        candidate=candidate,
        commit_sha=commit_sha,
    )


def _suggest_backport(gh: Any, task: FixTask, candidate: FixCandidate) -> FixResult:
    """A merged upstream fix for a sweep's failure, as the pull request to backport.

    The sweep backports pull requests, and knows which it has by their
    number: a cherry-pick of the commit here would be one it does not know,
    and could backport again.
    """
    commit = candidate.port_commit.strip()
    try:
        number = _upstream_pr(gh, task.failure.repo_full_name, commit)
    except Exception as exc:  # noqa: BLE001 - retry can recover GitHub API failures
        return _failed(task, candidate, f"The pull request of upstream commit {commit[:12]} could not be found: {exc}")
    if not number:
        return _refused(task, candidate, f"Upstream commit {commit[:12]} fixes it, but no merged pull request has it.")
    return FixResult(
        kind=ResultKind.SUGGESTED,
        summary=f"#{number} fixes it upstream; it is to be backported to {task.failure.base_branch}.",
        task=task,
        candidate=candidate,
        pr_number=number,
    )


def _upstream_pr(gh: Any, repo_full_name: str, commit: str) -> int:
    """The merged pull request that brought ``commit`` to the default branch, or 0."""
    pulls = retry_github_call(
        lambda: list(gh.get_repo(repo_full_name).get_commit(commit).get_pulls()),
        retries=2,
        description=f"find the pull request of {commit[:12]}",
    )
    merged = [pull for pull in pulls if getattr(pull, "merged_at", None)]
    return int(merged[0].number) if merged else 0


def _publish_ci_sweep(
    gh: Any,
    task: FixTask,
    candidate: FixCandidate,
    *,
    git_env: dict[str, str],
) -> FixResult:
    """Add the fix to the sweep PR's CI-sweep PR, opening it when there is none.

    The fix was run on the sweep PR's head, which is checked again before the
    push, as for any update. The CI-sweep branch is that head with its fixes
    on top (:mod:`scripts.backport.ci_sweep`), so its pull request shows only
    the fixes.
    """
    target = task.target
    fixed = candidate.failing_check or task.failure.job_name
    try:
        pull, onto = _ci_sweep_pr(gh, task)
    except Exception as exc:  # noqa: BLE001 - retry can recover GitHub API failures
        return _failed(task, candidate, f"The CI-sweep branch {target.fix_branch} could not be checked: {exc}")
    if pull is not None:
        named = f"PR #{int(pull.number)} from {target.fix_branch}"
        if not is_bot_author(str(getattr(getattr(pull, "user", None), "login", "") or "")):
            return _refused(task, candidate, f"{named} is not owned by the CI agent.")
        if str(getattr(getattr(pull, "base", None), "ref", "") or "") != target.branch:
            return _refused(task, candidate, f"{named} is not into {target.branch}.")

    def check() -> str:
        return _validate_pr_target(gh, task, require_bot=True)

    try:
        commit_sha = push_candidate(
            candidate,
            target,
            git_env=git_env,
            pre_push_check=check,
            allow_ci_changes=allows_ci_changes(task),
            onto=onto,
            trailers=ci_sweep.trailers(task.failure.job_name, task.failure.run_url, candidate.adapts),
        )
    except PushRefused as exc:
        return _refused(task, candidate, str(exc))
    except PushFailed as exc:
        return _failed(task, candidate, str(exc))
    text = _ci_sweep_body(gh, task, candidate, commit_sha)
    if pull is None:
        try:
            pull = retry_github_call(
                lambda: create_pull_from_push_repo(
                    gh.get_repo(task.failure.repo_full_name),
                    base_repo=task.failure.repo_full_name,
                    push_repo=target.repo_full_name,
                    title=f"[CI] CI Fix Sweep for {task.failure.base_branch}",
                    body=text,
                    head_branch=target.fix_branch,
                    base_branch=target.branch,
                    draft=False,
                ),
                retries=2,
                description=f"open the CI-sweep PR from {target.fix_branch}",
            )
        except Exception as exc:  # noqa: BLE001 - the next attempt opens it from the pushed branch
            return _failed(
                task, candidate,
                f"The fix is on {target.fix_branch} ({commit_sha[:12]}), but its PR could not be opened: {exc}",
                commit_sha=commit_sha,
            )
        kind, summary = ResultKind.OPENED, f"Opened the CI-sweep PR #{int(pull.number)} with a fix for {fixed}."
        _point_sweep_at(gh, task, int(pull.number))
    else:
        try:
            retry_github_call(lambda: pull.edit(body=text), retries=2, description=f"describe #{int(pull.number)}")
        except Exception as exc:  # noqa: BLE001 - the fix is on the branch and in a comment
            logger.warning("Could not update #%s's table: %s", pull.number, exc)
        kind, summary = ResultKind.PUSHED, f"Pushed a fix for {fixed} to the CI-sweep PR #{int(pull.number)}."
    return FixResult(
        kind=kind, summary=summary, task=task, candidate=candidate, commit_sha=commit_sha,
        pr_number=int(pull.number), pr_url=str(getattr(pull, "html_url", "") or ""),
    )


def _ci_sweep_pr(gh: Any, task: FixTask) -> tuple[Any | None, str]:
    """The open CI-sweep PR and its head, or no PR and the head of a branch left without one.

    A branch whose pull request was merged or closed is deleted, as the sweep
    deletes its own stale branch: its fixes are on the sweep branch or were
    turned down. One that never had a pull request is a fix pushed before
    opening one failed, and is continued.
    """
    target = task.target
    pull = find_existing_pr(gh, task.failure.repo_full_name, target.repo_full_name, target.fix_branch)
    if pull is not None:
        return pull, str(pull.head.sha)
    repo = retry_github_call(
        lambda: gh.get_repo(target.repo_full_name), retries=2, description=f"get {target.repo_full_name}",
    )
    try:
        branch = retry_github_call(
            lambda: repo.get_branch(target.fix_branch), retries=2,
            description=f"get {target.repo_full_name}:{target.fix_branch}",
        )
    except GithubException as exc:
        if exc.status == 404:
            return None, ""
        raise
    owner = target.repo_full_name.split("/", 1)[0]
    closed = retry_github_call(
        lambda: list(repo.get_pulls(state="closed", head=f"{owner}:{target.fix_branch}")),
        retries=2, description=f"list closed PRs from {target.fix_branch}",
    )
    head = str(branch.commit.sha)
    if any(str(getattr(getattr(pull, "head", None), "sha", "") or "") == head for pull in closed):
        delete_stale_backport_branch(gh, target.repo_full_name, target.fix_branch)
        return None, ""
    return None, head


def _ci_sweep_body(gh: Any, task: FixTask, candidate: FixCandidate, commit_sha: str) -> str:
    """The CI-sweep PR's description, its table read from the commits the push put on the sweep's head.

    Not from the pull request's own list of commits, which can lag behind a
    push. When they cannot be read, the table has this fix alone.
    """
    try:
        compared = retry_github_call(
            lambda: gh.get_repo(task.target.repo_full_name).compare(task.target.expected_sha, commit_sha),
            retries=2, description=f"compare {task.target.fix_branch} with its sweep",
        )
        fixes = ci_sweep.commit_fixes(compared.commits)
    except Exception as exc:  # noqa: BLE001 - the fix is pushed either way
        logger.warning("Could not read the fixes on %s: %s", task.target.fix_branch, exc)
        fixes = []
    # GitHub can answer for a moment from before the push: this fix is there either way.
    if commit_sha not in {fix.sha for fix in fixes}:
        fixes.append(ci_sweep.Fix(
            commit_sha, commit_message(candidate).splitlines()[0],
            task.failure.job_name, task.failure.run_url, candidate.adapts,
        ))
    return ci_sweep.body(task.failure.base_branch, task.failure.pr_number, fixes)


def _point_sweep_at(gh: Any, task: FixTask, number: int) -> None:
    """Say at the top of the sweep PR that its CI fixes are in ``number``; the sweep says it again as it rebuilds."""
    try:
        sweep = retry_github_call(
            lambda: gh.get_repo(task.failure.repo_full_name).get_pull(task.failure.pr_number),
            retries=2, description=f"get #{task.failure.pr_number}",
        )
        text = ci_sweep.with_sweep_line(str(getattr(sweep, "body", "") or ""), number)
        retry_github_call(lambda: sweep.edit(body=text), retries=2, description=f"point #{sweep.number} at #{number}")
    except Exception as exc:  # noqa: BLE001 - the CI-sweep PR names the sweep PR either way
        logger.warning("Could not point the sweep PR at #%s: %s", number, exc)


def _publish_draft(
    gh: Any,
    task: FixTask,
    candidate: FixCandidate,
    *,
    git_env: dict[str, str],
) -> FixResult:
    # A deterministic occurrence branch may be left behind when a prior push
    # succeeded but PR creation failed. Its code is from that earlier attempt,
    # so recover it with generic metadata rather than describing it with this
    # run's newly generated candidate.
    reconciled = reconcile_draft_pr(gh, task)
    if reconciled is not None:
        return reconciled
    try:
        rejection = _validate_issue_target(gh, task)
    except TargetRevalidationFailed as exc:
        return _failed(task, candidate, str(exc))
    if rejection:
        return _refused(task, candidate, rejection)

    def check_issue() -> str:
        return _validate_issue_target(gh, task)

    try:
        commit_sha = push_candidate(
            candidate,
            task.target,
            git_env=git_env,
            pre_push_check=check_issue,
        )
    except PushStale as exc:
        # A concurrent or ambiguous prior push may have created the
        # deterministic occurrence branch after the first lookup.
        reconciled = reconcile_draft_pr(gh, task)
        if reconciled is not None:
            return reconciled
        return _refused(task, candidate, str(exc))
    except PushRefused as exc:
        return _refused(task, candidate, str(exc))
    except PushFailed as exc:
        return _failed(task, candidate, str(exc))

    reconciled = reconcile_draft_pr(
        gh,
        task,
        candidate=candidate,
        known_commit_sha=commit_sha,
    )
    if reconciled is not None:
        return reconciled
    return _failed(
        task,
        candidate,
        f"The fix was pushed as {commit_sha[:12]}, but its branch could "
        "not be found to open the draft PR.",
        commit_sha=commit_sha,
    )


def reconcile_draft_pr(
    gh: Any,
    task: FixTask,
    *,
    candidate: FixCandidate | None = None,
    known_commit_sha: str = "",
) -> FixResult | None:
    """Finish or reuse publication for one deterministic detector branch.

    ``None`` means the occurrence branch does not exist yet. An existing PR is
    returned unchanged; an existing branch without a PR is turned into the
    missing draft PR. This makes a retry complete an attempt whose branch push
    succeeded before PR creation failed.
    """
    target = task.target
    try:
        existing = find_existing_pr(
            gh,
            task.failure.repo_full_name,
            target.repo_full_name,
            target.branch,
        )
    except Exception as exc:  # noqa: BLE001 - retry can recover GitHub API failures
        return _failed(
            task,
            candidate,
            f"The occurrence branch {target.branch} could not be "
            f"revalidated: {exc}",
            commit_sha=known_commit_sha,
        )
    if existing is not None:
        rejection = _validate_existing_draft_pr(
            existing,
            task,
            expected_sha=known_commit_sha,
        )
        if rejection:
            return _refused(task, candidate, rejection)
        return _opened(
            task,
            candidate,
            existing,
            commit_sha=known_commit_sha,
        )

    branch_sha = known_commit_sha
    if not branch_sha:
        try:
            push_repo = retry_github_call(
                lambda: gh.get_repo(target.repo_full_name),
                retries=2,
                description=f"get {target.repo_full_name}",
            )
            branch = retry_github_call(
                lambda: push_repo.get_branch(target.branch),
                retries=2,
                description=f"get {target.repo_full_name}:{target.branch}",
            )
        except Exception as exc:  # noqa: BLE001 - retry can recover GitHub API failures
            if isinstance(exc, GithubException) and exc.status == 404:
                return None
            return _failed(
                task,
                candidate,
                f"The occurrence branch {target.branch} could not be checked: {exc}",
            )

        branch_sha = str(getattr(getattr(branch, "commit", None), "sha", "") or "")
        if not is_full_sha(branch_sha):
            return _failed(
                task,
                candidate,
                f"The occurrence branch {target.branch} has no valid commit SHA.",
            )
    commit_sha = branch_sha

    try:
        rejection = _validate_issue_target(gh, task)
    except TargetRevalidationFailed as exc:
        return _failed(task, candidate, str(exc), commit_sha=commit_sha)
    if rejection:
        # The exact PR may have appeared between the first lookup and the
        # issue-timeline check. One final exact lookup resolves that race.
        if _LINKED_PR in rejection:
            reused = _reuse_draft_pr(gh, task, candidate, commit_sha)
            if reused is not None:
                return reused
        # The branch is pushed and stays: say so, so that it is not lost.
        return FixResult(
            kind=ResultKind.REFUSED,
            summary=(
                f"{rejection} The fix is on {target.repo_full_name}:{target.branch} "
                f"({commit_sha[:12]}) without a pull request."
            ),
            task=task,
            candidate=candidate,
            commit_sha=commit_sha,
        )

    try:
        base_repo = retry_github_call(
            lambda: gh.get_repo(task.failure.repo_full_name),
            retries=2,
            description=f"get {task.failure.repo_full_name}",
        )
        existing = retry_github_call(
            lambda: create_pull_from_push_repo(
                base_repo,
                base_repo=task.failure.repo_full_name,
                push_repo=target.repo_full_name,
                title=_draft_title(
                    task,
                    candidate,
                ),
                body=_draft_body(
                    task,
                    candidate,
                ),
                head_branch=target.branch,
                base_branch=task.failure.base_branch,
                draft=True,
            ),
            retries=2,
            description=f"open draft CI-fix PR for issue #{task.failure.issue_number}",
        )
    except Exception as exc:
        reused = _reuse_draft_pr(gh, task, candidate, commit_sha)
        if reused is not None:
            return reused
        return _failed(
            task,
            candidate,
            f"The occurrence branch {target.branch} is at {commit_sha[:12]}, but the "
            f"draft PR could not be opened: {exc}",
            commit_sha=commit_sha,
        )
    return _opened(
        task,
        candidate,
        existing,
        commit_sha=commit_sha,
    )


def _reuse_draft_pr(gh: Any, task: FixTask, candidate: FixCandidate | None, commit_sha: str) -> FixResult | None:
    """The occurrence's draft PR, opened at ``commit_sha`` by a racing attempt, or None."""
    target = task.target
    try:
        existing = find_existing_pr(gh, task.failure.repo_full_name, target.repo_full_name, target.branch)
    except Exception:  # noqa: BLE001 - the caller reports its own failure
        return None
    if existing is None or _validate_existing_draft_pr(existing, task, expected_sha=commit_sha):
        return None
    return _opened(task, candidate, existing, commit_sha=commit_sha)


def _validate_pr_target(gh: Any, task: FixTask, *, require_bot: bool) -> str:
    target = task.target
    try:
        pr = retry_github_call(
            lambda: gh.get_repo(task.failure.repo_full_name).get_pull(task.failure.pr_number),
            retries=2,
            description=f"revalidate PR #{task.failure.pr_number}",
        )
    except Exception as exc:  # noqa: BLE001 - retry can recover GitHub API failures
        raise TargetRevalidationFailed(
            f"PR #{task.failure.pr_number} could not be revalidated ({exc})"
        ) from exc
    if str(getattr(pr, "state", "") or "") != "open":
        return f"PR #{task.failure.pr_number} is no longer open"
    if str(getattr(getattr(pr, "base", None), "ref", "") or "") != task.failure.base_branch:
        return f"PR #{task.failure.pr_number} no longer targets {task.failure.base_branch}"
    head = getattr(pr, "head", None)
    if str(getattr(getattr(head, "repo", None), "full_name", "") or "") != target.repo_full_name:
        return f"PR #{task.failure.pr_number} no longer comes from {target.repo_full_name}"
    if str(getattr(head, "ref", "") or "") != target.branch:
        return f"PR #{task.failure.pr_number} no longer has head branch {target.branch}"
    current_sha = str(getattr(head, "sha", "") or "")
    if current_sha != target.expected_sha:
        return (
            f"the PR head moved from {target.expected_sha[:12]} to "
            f"{current_sha[:12] or '(missing)'}"
        )
    if require_bot:
        author = str(getattr(getattr(pr, "user", None), "login", "") or "")
        if not can_update_branch(
            same_repo=target.repo_full_name == task.failure.repo_full_name,
            branch=target.branch,
            author=author,
        ):
            return f"PR #{task.failure.pr_number} is not owned by the CI agent"
    return ""


def _validate_issue_target(gh: Any, task: FixTask) -> str:
    """Recheck a detector issue before its fix is published.

    The base branch may have moved since the candidate was prepared: the
    draft PR is opened from the commit it was prepared on, and its own CI
    runs against the branch as it is then.
    """
    issue_number = task.failure.issue_number
    try:
        repo = retry_github_call(
            lambda: gh.get_repo(task.failure.repo_full_name),
            retries=2,
            description=f"get {task.failure.repo_full_name}",
        )
        issue = retry_github_call(
            lambda: repo.get_issue(issue_number),
            retries=2,
            description=f"revalidate issue #{issue_number}",
        )
    except Exception as exc:  # noqa: BLE001 - retry can recover GitHub API failures
        raise TargetRevalidationFailed(
            f"Issue #{issue_number} could not be revalidated ({exc})."
        ) from exc
    if str(getattr(issue, "state", "") or "") != "open":
        return f"Issue #{issue_number} is no longer open."
    if getattr(issue, "assignees", None):
        return f"Issue #{issue_number} is now assigned; leaving it to the assignee."
    try:
        if has_open_linked_pr(issue):
            return f"Issue #{issue_number} {_LINKED_PR}."
    except Exception as exc:  # noqa: BLE001 - retry can recover GitHub API failures
        raise TargetRevalidationFailed(
            f"Issue #{issue_number} links could not be revalidated ({exc})."
        ) from exc
    # A newer occurrence is the same test failing again while its fix was
    # prepared: the fix is for the test, and is still published
    # (scripts/ci_fix/issues.py records it for the newer run too).
    if parse_detector_occurrence(str(getattr(issue, "body", "") or "")) is None:
        return f"Issue #{issue_number} no longer contains a detector occurrence."
    return ""


def _validate_existing_draft_pr(
    pull: Any,
    task: FixTask,
    *,
    expected_sha: str = "",
) -> str:
    """Return why an exact occurrence PR cannot be safely reused."""
    target = task.target
    number = int(getattr(pull, "number", 0) or 0)
    label = f"PR #{number}" if number else "The existing PR"
    if str(getattr(pull, "state", "open") or "") != "open":
        return f"{label} is no longer open."
    if str(getattr(getattr(pull, "base", None), "ref", "") or "") != task.failure.base_branch:
        return f"{label} does not target {task.failure.base_branch}."
    head = getattr(pull, "head", None)
    if str(getattr(getattr(head, "repo", None), "full_name", "") or "") != target.repo_full_name:
        return f"{label} does not come from {target.repo_full_name}."
    if str(getattr(head, "ref", "") or "") != target.branch:
        return f"{label} does not use occurrence branch {target.branch}."
    head_sha = str(getattr(head, "sha", "") or "")
    if not is_full_sha(head_sha):
        return f"{label} has no valid head SHA."
    if expected_sha and head_sha != expected_sha:
        return (
            f"{label} moved from the pushed commit {expected_sha[:12]} to "
            f"{head_sha[:12]}."
        )
    return ""


def _source_label(failure: Failure) -> str:
    return failure.source.label if failure.source else "CI"


def _draft_title(task: FixTask, candidate: FixCandidate | None) -> str:
    failing_check = candidate.failing_check if candidate is not None else ""
    fallback = f"{_source_label(task.failure)} failure"
    return fit_subject(f"Fix {failing_check or task.failure.job_name or fallback}")


def _draft_body(task: FixTask, candidate: FixCandidate | None) -> str:
    failure = task.failure
    lines = []
    if failure.issue_number:
        lines += [f"Fixes #{failure.issue_number}", ""]
    description = prose(failure.description, 2_000) if failure.description else (
        f"`{failure.job_name}` failed in a {_source_label(failure)} run."
    )
    lines += [f"{description} [See the run]({failure.run_url}).", ""]
    if candidate and candidate.also_touches:
        # In code, unlinked: an open PR GitHub links to an issue keeps it
        # from a fix of its own, and this one has not shown it fixes them.
        touched = ", ".join(f"`#{number}`" for number in candidate.also_touches)
        one = len(candidate.also_touches) == 1
        lines += [
            f"It also changes the {'test of issue' if one else 'tests of issues'} {touched}, "
            f"without showing that it fixes {'it' if one else 'them'}.",
            "",
        ]
    if candidate is not None and candidate.action is CandidateAction.PORT:
        lines += [f"**Upstream fix:** `{candidate.port_commit}`", ""]
    if candidate is not None:
        lines += candidate_context(candidate, include_reasoning=True)
    if candidate is None:
        sign_off = "An earlier attempt prepared this branch: review its commit before merging."
    elif candidate.action is CandidateAction.PORT:
        sign_off = "The cherry-pick keeps the upstream commit message and its sign-off."
    else:
        sign_off = "Its commit has no DCO sign-off: whoever takes the change over signs it off."
    lines += [
        "---",
        "",
        f"<sub>Prepared by the CI-fix bot, which never merges. This PR's CI and a maintainer's "
        f"review decide. {sign_off}</sub>",
    ]
    return "\n".join(lines)


def _opened(
    task: FixTask,
    candidate: FixCandidate | None,
    pull: Any,
    *,
    commit_sha: str = "",
) -> FixResult:
    head_sha = str(
        getattr(getattr(pull, "head", None), "sha", "") or ""
    )
    return FixResult(
        kind=ResultKind.OPENED,
        summary=f"Opened draft PR #{int(pull.number)} for review and normal CI.",
        task=task,
        candidate=candidate,
        commit_sha=commit_sha or head_sha,
        pr_number=int(pull.number),
        pr_url=str(getattr(pull, "html_url", "") or ""),
    )


def _refused(
    task: FixTask,
    candidate: FixCandidate | None,
    summary: str,
) -> FixResult:
    return FixResult(
        kind=ResultKind.REFUSED,
        summary=summary,
        task=task,
        candidate=replace(candidate, summary=summary) if candidate is not None else None,
    )


def _failed(
    task: FixTask,
    candidate: FixCandidate | None,
    summary: str,
    *,
    commit_sha: str = "",
) -> FixResult:
    return FixResult(
        kind=ResultKind.FAILED,
        summary=summary,
        task=task,
        candidate=candidate,
        commit_sha=commit_sha,
    )
