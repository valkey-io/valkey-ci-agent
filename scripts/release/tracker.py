"""A small, non-authoritative release dashboard and progress watcher.

The issue is presentation only. Every transition is derived again from live
PR, branch, workflow, and release state; issue text never authorizes a write.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

from github import Auth, Github
from github.GithubException import GithubException

from scripts.common.github_client import retry_github_call
from scripts.common.polling import add_poll_loop_args, run_poll_loop_from_args
from scripts.release.checks import CandidateCI, evaluate_candidate_ci
from scripts.release.models import ReleasePolicy
from scripts.release.policy import load_policy
from scripts.release.publish import resolve_tag_commit
from scripts.release_notes.release_cut import PREP_BRANCH_PREFIX

logger = logging.getLogger(__name__)

TRACKING_LABEL = "release-tracking"
_TRACKER_PREFIX = "<!-- valkey-release-tracker:v1 "
_STATUS_MARKER = "<!-- valkey-release-tracker:status -->"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", re.ASCII)
_REFRESHED_RE = re.compile(r"Status last changed \d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")
# Job names in .github/workflows/release-publish.yml, used only to read a
# stage's real completion time off the run. A rename there costs a timestamp,
# never a wrong one. The qualification job calls a reusable workflow, so its
# jobs surface as "<name> / <child>".
_QUALIFY_JOB = "Qualify exact candidate"
_PUBLISH_JOB = "Publish approved release"


class _PublishEvidence(NamedTuple):
    """What a Publish run's own jobs say about the stages it contains."""

    qualified_at: Any | None = None
    approved_at: Any | None = None
    # The protected publish job succeeded in SOME attempt of this run. A run's
    # listed conclusion cannot say that: onboard-backports failing after the
    # release exists makes the run a failure, and a rerun replaces it with the
    # newest attempt's. Only this job creates the release.
    published: bool = False
_ROOT = Path(__file__).resolve().parents[2]
_PHASES = (
    "Prepare",
    "Review notes",
    "Candidate CI",
    "Qualification",
    "Release approval",
    "Publication",
    "Production",
    "Follow-up",
)
_FOLLOW_UP_ACTION = (
    "Release owner: review and merge every linked downstream PR, verify the remaining linked outputs, "
    "then close this tracker."
)


@dataclass(frozen=True)
class Tracker:
    repo: str
    branch: str
    version: str
    stage: str
    tag: str
    prep_branch: str
    prepare_run_id: int

    def marker(self) -> str:
        _validate_tracker(self)
        payload = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":"))
        return f"{_TRACKER_PREFIX}{payload} -->"


def ensure_tracker(gh: Any, tracker: Tracker, *, agent_repo: str) -> Any:
    """Create or reuse the one open dashboard for *tracker.tag*."""
    _validate_tracker(tracker)
    repo = _repo(gh, tracker.repo)
    label = _ensure_label(repo)
    issue = None
    for candidate in itertools.islice(repo.get_issues(state="all", labels=[label]), 200):
        if not _is_bot_owned(candidate):
            continue
        existing = _tracker_from_issue(candidate, allow_body_fallback=True)
        if existing is not None and existing.tag == tracker.tag:
            issue = candidate
            break

    if issue is None:
        # Do not blindly retry an issue creation: GitHub can accept the write
        # and lose the response, which would create duplicate dashboards.
        try:
            issue = repo.create_issue(
                title=f"Release {tracker.tag}",
                body=_issue_body(tracker, agent_repo),
                labels=[label],
            )
        except Exception:
            issue = next(
                (
                    candidate
                    for candidate in itertools.islice(repo.get_issues(state="all", labels=[label]), 200)
                    if _is_bot_owned(candidate)
                    and (parsed := _tracker_from_issue(candidate, allow_body_fallback=True)) is not None
                    and parsed.tag == tracker.tag
                ),
                None,
            )
            if issue is None:
                raise
    else:
        if issue.state != "open":
            retry_github_call(
                lambda: issue.edit(state="open"),
                retries=2,
                description=f"reopen release tracker #{issue.number}",
            )
        # A rerun may have a newer preparation run. Keep the stable dashboard
        # URL while refreshing only bot-owned metadata and instructions.
        retry_github_call(
            lambda: issue.edit(
                title=f"Release {tracker.tag}",
                body=_issue_body(tracker, agent_repo),
            ),
            retries=2,
            description=f"refresh release tracker #{issue.number}",
        )

    existing_status = _find_status_comment(issue)
    if existing_status is None:
        # Seed the controller-owned status comment (and the authority marker
        # it carries) for a brand-new dashboard, or migrate a pre-comment
        # tracker whose marker still lives in the editable issue body.
        status, _ = _render_status(
            tracker,
            prepare_run=None,
            pr=None,
            branch_head="",
            candidate_sha="",
            candidate_ci=None,
            publish_run=None,
            evidence=_PublishEvidence(),
            release=None,
            production_run=None,
            agent_repo=agent_repo,
            dispatched=False,
        )
        _upsert_status(issue, status, tracker=tracker)
    else:
        # On reuse the comment already holds live evidence (linked PR,
        # candidate, release, phase). Replacing it with the empty initial
        # render would misreport the release until the next sync, so rebind
        # only the marker: a Prepare rerun changes just its prepare_run_id.
        _rebind_status_marker(existing_status, tracker)
    # The marker is needed in the create body only to recover an ambiguous
    # issue-creation response. Once the controller-owned status comment exists,
    # make the human-editable body purely presentational.
    _refresh_issue_body(issue, tracker, agent_repo)
    return issue


def _refresh_issue_body(issue: Any, tracker: Tracker, agent_repo: str) -> None:
    """Idempotently apply the current presentational UI to an existing tracker."""
    title = f"Release {tracker.tag}"
    body = _issue_body(tracker, agent_repo, include_marker=False)
    if getattr(issue, "title", "") == title and (issue.body or "") == body:
        return
    retry_github_call(
        lambda: issue.edit(title=title, body=body),
        retries=2,
        description=f"refresh release tracker #{issue.number} presentation",
    )


def parse_tracker(body: str) -> Tracker | None:
    if not isinstance(body, str):
        return None
    start = body.find(_TRACKER_PREFIX)
    if start < 0:
        return None
    end = body.find(" -->", start)
    if end < 0:
        return None
    payload = body[start + len(_TRACKER_PREFIX) : end]
    if len(payload) > 4096:
        return None
    try:
        raw = json.loads(payload)
        if not isinstance(raw, dict):
            return None
        tracker = Tracker(**raw)
        _validate_tracker(tracker)
        return tracker
    except Exception:
        # Issue bodies and comments are untrusted input. Malformed JSON,
        # unexpected field types, and parser recursion must stay contained to
        # the one issue carrying the invalid marker.
        return None


def sync_trackers(
    target_gh: Any,
    agent_gh: Any,
    automation_gh: Any,
    *,
    target_repo: str,
    agent_repo: str,
    automation_repo: str,
    policy: ReleasePolicy,
    dispatch: bool = True,
) -> list[str]:
    """Refresh every open tracker and advance a merged notes PR once."""
    if policy.repo != target_repo:
        raise ValueError(f"policy repository {policy.repo} does not match tracker target {target_repo}")
    repo = _repo(target_gh, target_repo)
    agent = _repo(agent_gh, agent_repo)
    automation = _repo(automation_gh, automation_repo)
    publish_workflow = retry_github_call(
        lambda: agent.get_workflow("release-publish.yml"),
        retries=2,
        description="get release publication workflow",
    )
    label = _ensure_label(repo)
    results: list[str] = []
    for issue in itertools.islice(repo.get_issues(state="open", labels=[label]), 100):
        if not _is_bot_owned(issue):
            logger.warning("Ignoring non-bot release tracker lookalike #%s", issue.number)
            continue
        tracker = None
        try:
            tracker = _tracker_from_issue(issue)
            if tracker is None:
                logger.warning("Ignoring release tracker #%s with invalid metadata", issue.number)
                results.append(f"#{issue.number}: invalid tracker metadata")
                continue
            if tracker.repo != target_repo:
                logger.warning(
                    "Ignoring release tracker #%s for unexpected repository %s",
                    issue.number,
                    tracker.repo,
                )
                results.append(f"#{issue.number}: unexpected tracker repository")
                continue
            result = _sync_one(
                issue,
                tracker,
                repo,
                agent,
                automation,
                publish_workflow,
                agent_repo=agent_repo,
                policy=policy,
                dispatch=dispatch,
            )
        except Exception as exc:
            logger.exception("Could not refresh release tracker #%s", issue.number)
            detail = re.sub(r"\s+", " ", str(exc)).replace("`", "'")[:500]
            if tracker is not None:
                try:
                    _upsert_status(
                        issue,
                        '<div align="center">\n\n## Release dashboard needs attention\n\n'
                        f"{_badge('tracker', 'refresh failed', 'cf222e')}\n\n</div>\n\n"
                        "> [!CAUTION]\n"
                        f"> **Tracker refresh failed:** `{type(exc).__name__}: {detail}`\n>\n"
                        "> No release action was authorized by this failure. Open the progress workflow logs and rerun it.",
                        tracker=tracker,
                    )
                except Exception:
                    logger.exception("Could not report release tracker #%s refresh failure", issue.number)
            result = f"#{issue.number}: refresh failed"
        results.append(result)
    return results


def _sync_one(
    issue: Any,
    tracker: Tracker,
    repo: Any,
    agent: Any,
    automation: Any,
    publish_workflow: Any,
    *,
    agent_repo: str,
    policy: ReleasePolicy,
    dispatch: bool,
) -> str:
    if tracker.branch not in policy.branches:
        raise ValueError(f"tracker branch {tracker.branch} is not allowed by release policy")
    _refresh_issue_body(issue, tracker, agent_repo)
    pr = _find_prep_pr(repo, tracker)
    branch_head = _branch_head(repo, tracker.branch)
    prepare_run = retry_github_call(
        lambda: agent.get_workflow_run(tracker.prepare_run_id),
        retries=2,
        description=f"get preparation run {tracker.prepare_run_id}",
    )
    candidate_sha = ""
    if pr is not None and getattr(pr, "merged", False):
        candidate_sha = (getattr(pr, "merge_commit_sha", "") or "").lower()
    candidate_ci = None
    if _SHA_RE.fullmatch(candidate_sha):
        try:
            candidate_ci = evaluate_candidate_ci(repo, policy, candidate_sha)
        except Exception:
            # Candidate CI is maintainer context, not publication authority.
            # A transient checks API failure must not strand a release that
            # can still pass the exact no-publish qualification matrix.
            logger.warning("Could not load candidate CI for %s", candidate_sha[:12], exc_info=True)
            candidate_ci = CandidateCI(
                workflow_url="",
                workflow_status="unavailable",
                workflow_conclusion=None,
                suite_id=None,
                checks=(),
            )
    release = _find_release(repo, tracker.tag, candidate_sha, tracker.stage != "ga")

    publish_title = f"Publish release on {tracker.branch} @ {candidate_sha}" if candidate_sha else ""
    # Publication enforces that its own workflow revision is still the
    # controller's current main (release-publish.yml fails otherwise), so a
    # run dispatched from an older controller commit can never succeed. The
    # lookup therefore prefers a run at the current controller head; an
    # incomplete run from an older head is cancelled and re-dispatched fresh,
    # which keeps exactly one live approval prompt. For the DISPATCH decision
    # a completed run counts only at the current head, so a stale failure
    # never suppresses the automatic re-dispatch. A stale SUCCESS is adopted
    # afterwards for display only (see below), because that run is the
    # shipped release and its rows are history.
    controller_sha = _branch_head(agent, getattr(agent, "default_branch", "main"))
    publish_run = _find_run(publish_workflow, publish_title, controller_sha) if publish_title else None
    any_publish_run = publish_run or (_find_run(publish_workflow, publish_title) if publish_title else None)
    if (
        publish_run is None
        and any_publish_run is not None
        and getattr(any_publish_run, "status", "") != "completed"
        and getattr(any_publish_run, "head_sha", "") == controller_sha
    ):
        publish_run = any_publish_run
    if (
        publish_run is None
        and any_publish_run is not None
        and getattr(any_publish_run, "status", "") != "completed"
        and getattr(any_publish_run, "head_sha", "") != controller_sha
    ):
        if dispatch and release is None:
            # Only while nothing shipped. After the release exists the run
            # may still be finishing post-publication work (first-GA backport
            # onboarding), and cancelling it would both interrupt that and
            # destroy the evidence this dashboard reports.
            cancelled = retry_github_call(
                lambda: any_publish_run.cancel(),
                retries=2,
                description=f"cancel stale publication run {any_publish_run.id}",
            )
            if cancelled is False:
                raise RuntimeError(f"GitHub refused cancellation of stale publication run {any_publish_run.id}")
            logger.warning(
                "Cancelled stale publication run %s after controller main moved",
                any_publish_run.id,
            )
        else:
            publish_run = any_publish_run

    dispatched = False
    if (
        dispatch
        and release is None
        and candidate_sha
        and _SHA_RE.fullmatch(candidate_sha)
        and branch_head == candidate_sha
        and publish_run is None
    ):
        accepted = retry_github_call(
            lambda: publish_workflow.create_dispatch(
                agent.default_branch,
                inputs={"branch": tracker.branch, "candidate_sha": candidate_sha},
            ),
            retries=1,
            description=f"dispatch publication for {tracker.tag}",
        )
        if accepted is False:
            raise RuntimeError(f"GitHub refused publication dispatch for {tracker.tag}")
        dispatched = True

    # Display provenance for a SHIPPED release, decided separately from the
    # dispatch selection above. `_find_run` answers an operational question
    # ("is there a run to wait on or replace?"), so it prefers an active run
    # and otherwise returns the newest COMPLETED match whatever its
    # conclusion. Neither preference suits history: the release exists
    # because one run succeeded, and a later failed or cancelled
    # re-dispatch, or the controller's own main advancing past the head the
    # successful run used, must not make a published release render as one
    # that never qualified. Gated on the release existing, which is also
    # what makes this unreachable from the dispatch decision (that requires
    # `release is None`), so no stale run can ever suppress a re-dispatch.
    # When no run is listed as a success (the shipping run's conclusion is
    # `failure` once onboard-backports fails after publishing, and a rerun
    # relists it under its newest attempt), fall back to the newest match at
    # any head and let its jobs prove or refuse publication in the render.
    if release is not None and publish_title:
        shipped = _latest_successful_run(publish_workflow, publish_title)
        publish_run = shipped or publish_run or any_publish_run

    # The jobs are one more API call per pass; ask only when a row will show
    # what they say: a stamp on a passed or approval-waiting stage, or proof
    # of publication once the release exists.
    evidence = _PublishEvidence()
    if publish_run is not None and (
        release is not None or getattr(publish_run, "status", "") in {"completed", "waiting", "pending"}
    ):
        evidence = _publish_evidence(publish_run)

    production_run = _find_production_run(automation, tracker.tag) if release else None
    body, summary = _render_status(
        tracker,
        prepare_run=prepare_run,
        pr=pr,
        branch_head=branch_head,
        candidate_sha=candidate_sha,
        candidate_ci=candidate_ci,
        publish_run=publish_run,
        evidence=evidence,
        release=release,
        production_run=production_run,
        agent_repo=agent_repo,
        dispatched=dispatched,
    )
    _upsert_status(issue, body, tracker=tracker)
    return f"#{issue.number}: {summary}"


def _render_status(
    tracker: Tracker,
    *,
    prepare_run: Any | None,
    pr: Any | None,
    branch_head: str,
    candidate_sha: str,
    candidate_ci: CandidateCI | None,
    publish_run: Any | None,
    evidence: _PublishEvidence = _PublishEvidence(),
    release: Any | None,
    production_run: Any | None,
    agent_repo: str,
    dispatched: bool,
) -> tuple[str, str]:
    repo_url = f"https://github.com/{tracker.repo}"
    prep_url = f"https://github.com/{agent_repo}/actions/runs/{tracker.prepare_run_id}"
    prep_branch_url = f"{repo_url}/tree/{tracker.prep_branch}"
    prepare_status, prepare_evidence = _run_status(
        prepare_run,
        label=f"Prepare run {tracker.prepare_run_id}",
        fallback_url=prep_url,
    )

    notes_status = _status_badge("Waiting", "9a6700")
    notes_evidence = f"[Preparation branch `{tracker.prep_branch}`]({prep_branch_url})"
    notes_action = "Wait for the preparation PR."
    current = "Preparing the release notes."
    next_action = "Wait for preparation to finish."
    summary = "preparing notes"
    pr_link = ""
    if prepare_run is not None and prepare_run.status == "completed":
        if prepare_run.conclusion == "success":
            current = "Release preparation completed and the release-notes PR is pending."
            next_action = "Wait for the release-notes PR to appear."
            summary = "preparation completed"
        else:
            current = "Release preparation failed."
            next_action = "Open the Prepare run, fix the failure, and rerun Prepare Release."
            summary = "preparation failed"

    if pr is not None:
        pr_link = f"[PR #{pr.number}]({pr.html_url})"
        notes_evidence = f"{pr_link} · [Preparation branch `{tracker.prep_branch}`]({prep_branch_url})"
        if getattr(pr, "merged", False):
            notes_status = _status_badge("Merged", "1a7f37")
            notes_action = "Complete"
            current = "The release-notes PR is merged and the candidate is fixed."
            next_action = "Wait for exact-candidate qualification."
            summary = "notes PR merged"
        elif pr.state == "closed":
            notes_status = _status_badge("Closed", "cf222e")
            notes_action = "Rerun Prepare Release."
            current = "The release-notes PR closed without merging."
            next_action = notes_action
            summary = "notes PR closed"
        elif getattr(pr, "draft", False):
            notes_status = _status_badge("Review needed", "9a6700")
            notes_action = "Review, mark ready, and merge the PR."
            current = "The release-notes PR is waiting for maintainer review."
            next_action = notes_action
            summary = "notes PR held"
        else:
            notes_status = _status_badge("Ready for review", "0969da")
            notes_action = "Review and merge the PR."
            current = "The release-notes PR is ready for review."
            next_action = notes_action
            summary = "waiting for notes PR merge"

    candidate_status = _status_badge("Not started", "57606a")
    candidate_evidence = "Candidate not established"
    candidate_action = "Merge the canonical release-notes PR."
    if candidate_sha:
        candidate_url = f"{repo_url}/commit/{candidate_sha}"
        candidate_evidence = f"[Candidate `{candidate_sha[:12]}`]({candidate_url})"
        if branch_head != candidate_sha:
            candidate_status = _status_badge("Blocked", "cf222e")
            candidate_action = "Rerun Prepare Release for the new branch head."
            current = "The release branch moved after the candidate was reviewed."
            next_action = candidate_action
            summary = "candidate invalidated by branch movement"
        elif candidate_ci is None or candidate_ci.state == "missing":
            candidate_status = _status_badge("Not available", "9a6700")
            candidate_action = "Advisory only; qualification can continue."
        elif candidate_ci.state == "unavailable":
            candidate_status = _status_badge("Unavailable", "9a6700")
            candidate_action = "Advisory only; inspect progress logs if this persists."
        else:
            ci_run_id = candidate_ci.workflow_url.rstrip("/").rsplit("/", 1)[-1]
            ci_link = (
                f"[Candidate CI run {ci_run_id}]({candidate_ci.workflow_url})"
                if candidate_ci.workflow_url
                else "Candidate CI run not found"
            )
            candidate_evidence += (
                f" · {ci_link} · {candidate_ci.passed_count} of {len(candidate_ci.checks)} configured checks passed"
            )
            if candidate_ci.state == "passed":
                candidate_status = _status_badge("Passed", "1a7f37")
                candidate_action = "Complete"
            elif candidate_ci.state == "running":
                candidate_status = _status_badge("Running", "0969da")
                pending = ", ".join(check.name for check in candidate_ci.checks if not check.passed)
                candidate_action = (
                    f"Advisory only; still running: {pending}."
                    if pending
                    else "Advisory only; qualification can continue."
                )
            else:
                candidate_status = _status_badge("Failed", "cf222e")
                failed_checks = ", ".join(check.name for check in candidate_ci.checks if not check.passed)
                candidate_action = (
                    f"Advisory only; inspect if unexpected: {failed_checks}."
                    if failed_checks
                    else "Advisory only; inspect if unexpected."
                )

    qualification_status = _status_badge("Not started", "57606a")
    qualification_evidence = "No Publish run"
    qualification_action = "Wait for exact-candidate qualification."
    approval_status = _status_badge("Not ready", "57606a")
    approval_evidence = "Qualification has not passed"
    approval_action = "No action yet."
    publish_link = (
        f"[Publish run {publish_run.id}]({publish_run.html_url})" if publish_run is not None else ""
    )
    qualification_passed = False
    qualified_at, approved_at, published = evidence
    qualified_stamp = _stamp(qualified_at, "finished")
    approved_stamp = _stamp(approved_at, "approved")
    if dispatched:
        qualification_status = _status_badge("Starting", "0969da")
        qualification_evidence = "Publish workflow dispatched"
        qualification_action = "Wait for the run to appear."
        current = "Qualification is starting."
        next_action = qualification_action
        summary = "publication dispatched"
    elif publish_run is not None:
        qualification_evidence = publish_link
        if publish_run.status == "completed" and publish_run.conclusion == "success":
            qualification_passed = True
            qualification_status = _status_badge("Passed", "1a7f37")
            qualification_evidence = f"{publish_link}{qualified_stamp}"
            qualification_action = "Complete"
            approval_status = _status_badge("Approved", "1a7f37")
            approval_evidence = f"{publish_link}{approved_stamp}"
            approval_action = "Complete"
            current = "Qualification and protected publication completed."
            next_action = "Wait for production automation."
            summary = "publication completed"
        elif publish_run.status == "completed":
            qualification_status = _status_badge("Failed", "cf222e")
            qualification_action = "Inspect and rerun the Publish workflow."
            approval_status = _status_badge("Not reached", "57606a")
            approval_evidence = publish_link
            approval_action = "Fix the failed Publish run first."
            current = "The Publish workflow failed."
            next_action = qualification_action
            summary = "publication failed"
        elif publish_run.status in {"waiting", "pending"}:
            qualification_passed = True
            qualification_status = _status_badge("Passed", "1a7f37")
            qualification_evidence = f"{publish_link}{qualified_stamp}"
            qualification_action = "Complete"
            approval_status = _status_badge("Waiting for approval", "8250df")
            approval_evidence = publish_link
            approval_action = "Review the plan, then approve the `release` environment."
            current = "Qualification passed and release approval is required."
            next_action = approval_action
            summary = "waiting for release approval"
        else:
            qualification_status = _status_badge("Running", "0969da")
            qualification_action = "Wait for exact-candidate qualification."
            approval_status = _status_badge("Not ready", "57606a")
            approval_evidence = publish_link
            approval_action = "No action until qualification passes."
            current = "Exact-candidate qualification is running."
            next_action = qualification_action
            summary = "validating and qualifying"

    if release is not None and not qualification_passed:
        # A controller-published release PROVES both stages: publication is
        # gated on qualification and on the protected `release` approval, and
        # this row is history once the release exists. The proof is the
        # protected publish job succeeding in some attempt of the found run,
        # never the run's listed conclusion: onboard-backports failing after
        # the release exists makes the run a failure, a rerun replaces the
        # conclusion with the newest attempt's, and a run can be cancelled
        # after it created the release. Anchoring on the job rather than on
        # the conclusion also separates that history from a genuinely failed
        # run beside a hand-created release.
        if published:
            latest = getattr(publish_run, "conclusion", None) or getattr(publish_run, "status", "unknown")
            note = f"{publish_link} · latest attempt {latest}"
            qualification_status = _status_badge("Passed", "1a7f37")
            qualification_evidence = f"{note}{qualified_stamp}"
            qualification_action = "Complete"
            approval_status = _status_badge("Approved", "1a7f37")
            approval_evidence = f"{note}{approved_stamp}"
            approval_action = "Complete"
        elif publish_run is None or getattr(publish_run, "status", "") == "completed":
            # Nothing found that published this release: no run for the exact
            # candidate, or a finished run whose publish job never succeeded.
            # This is also how a hand-created, out-of-band release looks, and
            # that needs a human, not a green badge. A live run that has not
            # published is current state and keeps its own rows above.
            qualification_status = _status_badge("Unverified", "9a6700")
            qualification_evidence = (
                "Released, but no matching Publish run found"
                if publish_run is None
                else f"Released, but {publish_link} did not publish it"
            )
            qualification_action = "Confirm the publication run, or investigate an out-of-band release."
            approval_status = _status_badge("Unverified", "9a6700")
            approval_evidence = qualification_evidence
            approval_action = qualification_action

    release_status = _status_badge("Not published", "57606a")
    release_evidence = "No GitHub release"
    release_action = "Complete qualification and release approval."
    if release is not None:
        release_status = _status_badge("Published", "1a7f37")
        release_evidence = (
            f"[GitHub release {tracker.tag}]({release.html_url})"
            f"{_stamp(getattr(release, 'published_at', None), 'published')}"
        )
        release_action = "Complete"
        current = "The GitHub release is published."
        next_action = "Wait for production automation and its protected approval."
        summary = "release published"

    production_status = _status_badge("Not started", "57606a")
    production_evidence = "No production run"
    production_action = "Wait for the GitHub release event."
    follow_up_status = _status_badge("Not started", "57606a")
    follow_up_evidence = "Downstream work has not completed"
    follow_up_action = "No action yet."
    if production_run is not None:
        production_link = f"[Production run {production_run.id}]({production_run.html_url})"
        production_evidence = production_link
        follow_up_evidence = f"{production_link}<br>**Manual follow-up:** {_downstream_links(tracker)}"
        if production_run.status == "completed" and production_run.conclusion == "success":
            production_status = _status_badge("Passed", "1a7f37")
            production_evidence = f"{production_link}{_stamp(getattr(production_run, 'updated_at', None), 'finished')}"
            production_action = "Complete"
            follow_up_status = _status_badge("Release owner review", "8250df")
            follow_up_action = _FOLLOW_UP_ACTION
            current = "Production automation completed."
            next_action = follow_up_action
            summary = "production automation completed"
        elif production_run.status == "completed":
            production_status = _status_badge("Failed", "cf222e")
            production_action = "Inspect and rerun the failed production workflow."
            current = "Production automation failed."
            next_action = production_action
            summary = "production automation failed"
        elif production_run.status in {"waiting", "pending"}:
            production_status = _status_badge("Waiting for approval", "8250df")
            production_action = "Approve the `release-publish` environment."
            current = "Production approval is required."
            next_action = production_action
            summary = "waiting for production approval"
        else:
            production_status = _status_badge("Running", "0969da")
            production_action = "Wait for production automation."
            current = "Production automation is running."
            next_action = production_action
            summary = "production automation running"

    refreshed = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    phase, failed = _presentation_state(
        prepare_run=prepare_run,
        pr=pr,
        branch_head=branch_head,
        candidate_sha=candidate_sha,
        publish_run=publish_run,
        release=release,
        production_run=production_run,
        dispatched=dispatched,
    )
    callout = "CAUTION" if failed else ("IMPORTANT" if "approval" in summary else "NOTE")
    phase_color = "cf222e" if failed else ("1a7f37" if phase == len(_PHASES) else "0969da")
    rows = (
        ("Prepare", prepare_status, prepare_evidence, "Complete" if prepare_run and prepare_run.conclusion == "success" else "Wait for or rerun Prepare Release."),
        ("Release notes", notes_status, notes_evidence, notes_action),
        ("Candidate CI", candidate_status, candidate_evidence, candidate_action),
        ("Qualification", qualification_status, qualification_evidence, qualification_action),
        ("Release approval", approval_status, approval_evidence, approval_action),
        ("Publication", release_status, release_evidence, release_action),
        ("Production", production_status, production_evidence, production_action),
        ("Follow-up", follow_up_status, follow_up_evidence, follow_up_action),
    )
    lines = [
        '<div align="center">',
        "",
        "## Current release status",
        "",
        _status_badge(_PHASES[phase - 1], phase_color),
        "",
        "</div>",
        "",
        f"> [!{callout}]",
        f"> **{current}**",
        ">",
        f"> **Next step:** {next_action}",
        "",
        "## Live release status",
        "",
        "| Stage | Status | Evidence | Next action |",
        "|---|---|---|---|",
        *(f"| {stage} | {status} | {evidence} | {action} |" for stage, status, evidence, action in rows),
    ]
    if candidate_ci is not None and candidate_ci.workflow_status not in {"missing", "unavailable"}:
        lines.extend((
            "",
            f"<details><summary>Candidate CI: {candidate_ci.passed_count} of {len(candidate_ci.checks)} configured checks passed</summary>",
            "",
            "| Configured check | Status | Evidence |",
            "|---|---|---|",
            *(
                f"| `{check.name}` | {_check_status_badge(check)} | "
                f"{f'[`{check.name}` check]({check.url})' if check.url else 'No check run'} |"
                for check in candidate_ci.checks
            ),
            "",
            "</details>",
        ))
    lines.extend((
        "",
        "<details><summary>Security and recovery model</summary>",
        "",
        "This dashboard is a projection of live GitHub state. It cannot choose a candidate, authorize publication, or skip either protected approval. The canonical merged preparation PR, exact branch SHA, no-publish qualification run, live approver membership, and environment gates remain authoritative. Ordinary candidate CI is advisory context; the exact no-publish qualification matrix is the technical publication gate.",
        "",
        "</details>",
        "",
        f"<sub>Status last changed {refreshed} · Dashboard only; not release authority.</sub>",
    ))
    return "\n".join(lines), summary


def _downstream_links(tracker: Tracker) -> str:
    """Return deterministic follow-up links without expanding App access.

    The production run remains the success authority. These links expose the
    branches and repositories that its production jobs update, including
    partial results when a later job fails, without asking the controller App
    to read every downstream repository.
    """
    owner = tracker.repo.split("/", 1)[0]

    def pr_search(repo: str, branch: str, label: str) -> str:
        query = f"is%3Apr+head%3A{branch}"
        return f"[{label} PR search](https://github.com/{owner}/{repo}/pulls?q={query})"

    links = [
        f"[Hashes](https://github.com/{owner}/valkey-hashes/blob/main/README)",
        pr_search("valkey-container", f"update-{tracker.tag}", "Container"),
    ]
    if tracker.stage == "ga":
        patch = int(tracker.version.rsplit(".", 1)[1])
        if patch == 0:
            links.append(pr_search("valkey-doc", f"update-docs-{tracker.tag}", "Documentation"))
        else:
            links.append(f"[Documentation tag](https://github.com/{owner}/valkey-doc/tree/{tracker.tag})")
        links.extend((
            pr_search("valkey-io.github.io", f"update-website-{tracker.tag}", "Website"),
            pr_search("valkey-helm", f"update-valkey-{tracker.tag}", "Helm"),
        ))

    major, minor = (int(part) for part in tracker.version.split(".", 2)[:2])
    if major > 8 or (major == 8 and minor >= 1):
        links.append(pr_search("valkey-bundle", "valkey-bundle-update", "Bundle"))
    return " · ".join(links)


def _run_status(run: Any | None, *, label: str, fallback_url: str = "") -> tuple[str, str]:
    if run is None:
        evidence = f"[{label}]({fallback_url})" if fallback_url else "Run not found"
        return _status_badge("Waiting", "9a6700"), evidence
    evidence = f"[{label}]({run.html_url})"
    if run.status != "completed":
        return _status_badge("Running", "0969da"), evidence
    if run.conclusion == "success":
        return _status_badge("Passed", "1a7f37"), evidence
    return _status_badge("Failed", "cf222e"), evidence


def _check_status_badge(check: Any) -> str:
    if check.passed:
        return _status_badge("Passed", "1a7f37")
    if check.status in {"queued", "in_progress", "pending", "waiting"}:
        return _status_badge("Running", "0969da")
    if check.status == "missing":
        return _status_badge("Missing", "cf222e")
    return _status_badge("Failed", "cf222e")

def _issue_body(tracker: Tracker, agent_repo: str, *, include_marker: bool = True) -> str:
    repo_url = f"https://github.com/{tracker.repo}"
    trackers_url = f"{repo_url}/issues?q=is%3Aissue+is%3Aopen+label%3A{TRACKING_LABEL}"
    prep_branch_url = f"{repo_url}/tree/{tracker.prep_branch}"
    prepare_run_url = f"https://github.com/{agent_repo}/actions/runs/{tracker.prepare_run_id}"
    lines: tuple[str, ...] = (
        '<div align="center">',
        "",
        f"# Valkey {tracker.tag}",
        "",
        " ".join(
            (
                _badge("release", tracker.tag, "0969da"),
                _badge("stage", tracker.stage.upper(), "8250df"),
                _badge("line", tracker.branch, "57606a"),
            )
        ),
        "",
        f"[`{tracker.repo}`]({repo_url}) · [`{tracker.branch}`]({repo_url}/tree/{tracker.branch}) · "
        f"[All active releases]({trackers_url})",
        "",
        "</div>",
        "",
        "> [!NOTE]",
        "> This issue contains stable release identity and operator guidance. The controller-owned status comment shows live state, exact evidence, and the next action.",
        ">",
        "> Editing this issue never authorizes or advances the release.",
        "",
        "## Release identity",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Release line | [`{tracker.branch}`]({repo_url}/tree/{tracker.branch}) |",
        f"| Preparation branch | [`{tracker.prep_branch}`]({prep_branch_url}) |",
        f"| Preparation run | [Prepare run {tracker.prepare_run_id}]({prepare_run_url}) |",
        "",
        "## Release path",
        "",
        "`Prepare` → `Review notes` → `Candidate CI` → `Qualification` → `Release approval` → "
        "`Publication` → `Production` → `Follow-up`",
        "",
        "Qualification starts automatically after the canonical release-notes PR merges. Candidate CI remains visible as advisory context; the exact no-publish qualification matrix is the technical gate. Publication and production each retain a protected human approval.",
        "",
        "## Human checkpoints",
        "",
        "- Review and merge the canonical release-notes PR when the live status links it.",
        "- Review the rendered release plan before approving the `release` environment.",
        "- Review production evidence before approving the `release-publish` environment.",
        f"- {_FOLLOW_UP_ACTION}",
    )
    if include_marker:
        lines = (tracker.marker(), *lines)
    return "\n".join(lines)


def _escape_badge(value: str) -> str:
    return value.replace("-", "--").replace("_", "__").replace(" ", "%20").replace("/", "%2F").replace("&", "%26")


def _badge(label: str, message: str, color: str) -> str:
    alt = f"{label}: {message}"
    return f"![{alt}](https://img.shields.io/badge/{_escape_badge(label)}-{_escape_badge(message)}-{color}?style=flat-square)"


def _status_badge(message: str, color: str) -> str:
    return f"![{message}](https://img.shields.io/badge/-{_escape_badge(message)}-{color}?style=flat-square)"

def _presentation_state(
    *,
    prepare_run: Any | None,
    pr: Any | None,
    branch_head: str,
    candidate_sha: str,
    publish_run: Any | None,
    release: Any | None,
    production_run: Any | None,
    dispatched: bool,
) -> tuple[int, bool]:
    """Derive the dashboard phase from live objects, never rendered prose."""
    if production_run is not None:
        completed = getattr(production_run, "status", "") == "completed"
        succeeded = completed and getattr(production_run, "conclusion", None) == "success"
        return (8 if succeeded else 7), completed and not succeeded
    if release is not None:
        return 6, False
    if publish_run is not None:
        status = getattr(publish_run, "status", "")
        if status == "completed":
            succeeded = getattr(publish_run, "conclusion", None) == "success"
            return (6 if succeeded else 4), not succeeded
        if status in {"waiting", "pending"}:
            return 5, False
        return 4, False
    if dispatched:
        return 4, False
    if candidate_sha:
        return 3, branch_head != candidate_sha
    if pr is not None:
        if getattr(pr, "merged", False):
            return 3, False
        return 2, getattr(pr, "state", "") == "closed"
    prepare_failed = (
        prepare_run is not None
        and getattr(prepare_run, "status", "") == "completed"
        and getattr(prepare_run, "conclusion", None) != "success"
    )
    return 1, prepare_failed

def _find_prep_pr(repo: Any, tracker: Tracker) -> Any | None:
    pulls = retry_github_call(
        lambda: list(
            repo.get_pulls(
                state="all",
                sort="updated",
                direction="desc",
                head=f"{tracker.repo.split('/', 1)[0]}:{tracker.prep_branch}",
                base=tracker.branch,
            )
        ),
        retries=2,
        description=f"find preparation PR for {tracker.tag}",
    )
    if pulls:
        return pulls[0]

    # GitHub may stop matching the `head=owner:branch` filter after the
    # preparation branch is deleted. Closed PR metadata still retains its
    # head ref, so fall back to the release line without losing the reviewed
    # merge identity from the dashboard or automatic transition.
    closed = retry_github_call(
        lambda: repo.get_pulls(
            state="closed",
            sort="updated",
            direction="desc",
            base=tracker.branch,
        ),
        retries=2,
        description=f"find closed preparation PR fallback for {tracker.tag}",
    )
    return next(
        (
            pr
            for pr in itertools.islice(closed, 500)
            if getattr(getattr(pr, "head", None), "ref", "") == tracker.prep_branch
            and getattr(getattr(getattr(pr, "head", None), "repo", None), "full_name", "") == tracker.repo
        ),
        None,
    )


def _find_release(
    repo: Any,
    tag: str,
    expected_sha: str = "",
    expected_prerelease: bool | None = None,
) -> Any | None:
    releases = retry_github_call(lambda: repo.get_releases(), retries=2, description="list releases")
    release = next((release for release in itertools.islice(releases, 100) if release.tag_name == tag), None)
    if release is not None and getattr(release, "draft", False):
        raise RuntimeError(f"release {tag} exists only as a draft; it is not published provenance")
    if (
        release is not None
        and expected_prerelease is not None
        and bool(getattr(release, "prerelease", False)) != expected_prerelease
    ):
        raise RuntimeError(f"release {tag} has the wrong prerelease classification")
    if release is not None and expected_sha:
        actual = resolve_tag_commit(repo, tag)
        if actual != expected_sha:
            raise RuntimeError(
                f"release {tag} resolves to {actual or '<unknown>'}, expected candidate {expected_sha}"
            )
    return release


def _publish_evidence(run: Any) -> _PublishEvidence:
    """Read each stage's real moment, and whether publication happened, off the jobs.

    One Publish run spans qualification, the approval wait and publication,
    so the run's own end time belongs to none of the first two: on the
    9.2.0-rc1 run qualification finished at 22:37, approval released the
    publish job at 22:48, and the run ended at 22:49. The job records carry
    the real moments, so ask for them rather than inventing one from the run.

    All attempts are read, so a rerun that failed early cannot hide the
    attempt that shipped. Job names mirror release-publish.yml; a rename here
    degrades to no timestamp and no proof rather than a wrong one, which is
    the whole point.
    """
    if run is None:
        return _PublishEvidence()
    try:
        jobs = list(
            retry_github_call(
                lambda: run.jobs(_filter="all"),
                retries=2,
                description=f"list jobs of publish run {getattr(run, 'id', '?')}",
            )
        )
    except Exception:  # noqa: BLE001 - a dashboard never fails on decoration
        return _PublishEvidence()

    qualified_at = None
    approved_at = None
    published = False
    for job in jobs:
        name = getattr(job, "name", "") or ""
        if getattr(job, "conclusion", None) != "success":
            continue
        if name.startswith(f"{_QUALIFY_JOB} / "):
            # the last qualification job to finish IS qualification finishing
            done = getattr(job, "completed_at", None)
            if done is not None and (qualified_at is None or done > qualified_at):
                qualified_at = done
        elif name == _PUBLISH_JOB:
            # the protected job starts the moment the approval is granted
            published = True
            started = getattr(job, "started_at", None)
            if started is not None and (approved_at is None or started > approved_at):
                approved_at = started
    return _PublishEvidence(qualified_at, approved_at, published)


def _stamp(moment: Any, label: str) -> str:
    """Render " · <label> <UTC>" for a datetime, or "" for anything else.

    Every row is recomputed from scratch on each pass, so without a time a
    reader cannot tell a stage that finished minutes ago from one that
    finished days ago. Only moments that genuinely belong to the row are
    passed in: one Publish run spans qualification, approval and
    publication, so its end time is not when qualification finished.
    Read from the live object rather than stored on the tracker, keeping
    the dashboard a projection of GitHub state.
    """
    formatter = getattr(moment, "strftime", None)
    if formatter is None:
        return ""
    try:
        return f" · {label} {formatter('%Y-%m-%d %H:%M UTC')}"
    except (TypeError, ValueError):
        return ""


def _latest_successful_run(workflow: Any, title: str) -> Any | None:
    """The newest SUCCEEDED run matching *title*, at any head.

    Separate from :func:`_find_run`, which prefers an active run and then
    the newest completed one whatever its conclusion: those preferences
    answer "what should happen next", while this answers "which run
    produced what already shipped".
    """
    if not title:
        return None
    runs = retry_github_call(
        lambda: workflow.get_runs(status="success"),
        retries=2,
        description=f"list successful {workflow.name} runs",
    )
    for run in itertools.islice(runs, 500):
        if (getattr(run, "display_title", "") or "") == title:
            return run
    return None


def _find_run(workflow: Any, title: str, head_sha: str = "") -> Any | None:
    if not title:
        return None
    runs = retry_github_call(
        lambda: workflow.get_runs(head_sha=head_sha) if head_sha else workflow.get_runs(),
        retries=2,
        description=f"list {workflow.name} runs",
    )
    fallback = None
    for run in itertools.islice(runs, 500):
        if (getattr(run, "display_title", "") or "") != title:
            continue
        if getattr(run, "status", "") != "completed":
            return run
        if fallback is None:
            fallback = run
    return fallback


def _find_production_run(repo: Any, tag: str) -> Any | None:
    workflow = retry_github_call(
        lambda: repo.get_workflow("build-release.yml"),
        retries=2,
        description="get production workflow",
    )
    return _find_run(workflow, f"Build Release {tag} (prod)")


def _branch_head(repo: Any, branch: str) -> str:
    return retry_github_call(
        lambda: repo.get_branch(branch).commit.sha,
        retries=2,
        description=f"read {branch} head",
    )


def _ensure_label(repo: Any) -> Any:
    try:
        return retry_github_call(
            lambda: repo.get_label(TRACKING_LABEL),
            retries=2,
            description=f"get {TRACKING_LABEL} label",
        )
    except GithubException as exc:
        if exc.status != 404:
            raise
    return retry_github_call(
        lambda: repo.create_label(
            TRACKING_LABEL,
            "1d76db",
            "Tracks an in-progress Valkey release",
        ),
        retries=2,
        description=f"create {TRACKING_LABEL} label",
    )


def _find_status_comment(issue: Any) -> Any | None:
    """Find the bot-owned controller status comment, if the dashboard has one."""
    comments = retry_github_call(
        lambda: list(issue.get_comments()),
        retries=2,
        description=f"list tracker #{getattr(issue, 'number', '?')} comments",
    )
    bot_login = getattr(getattr(issue, "user", None), "login", "")
    return next(
        (
            comment
            for comment in comments
            if _STATUS_MARKER in (comment.body or "")
            and getattr(getattr(comment, "user", None), "login", "") == bot_login
        ),
        None,
    )


def _rebind_status_marker(comment: Any, tracker: Tracker) -> None:
    """Point the authority marker at this preparation run, nothing else.

    The sync loop reads its authority (tag, prep branch, prepare_run_id) from
    the marker inside the status comment, and a Prepare rerun mints a new
    prepare_run_id. Splice the new marker into the existing comment so the
    rendered live status survives the rerun untouched.
    """
    body = comment.body or ""
    marker = tracker.marker()
    if marker in body:
        return
    # The marker is always a single line. Replacing the whole line repairs a
    # malformed (e.g. unterminated) marker instead of silently keeping it,
    # which matters because ensure_tracker strips the issue-body copy right
    # after this rebind.
    lines = body.split("\n")
    for index, line in enumerate(lines):
        if _TRACKER_PREFIX in line:
            lines[index] = marker
            updated = "\n".join(lines)
            break
    else:
        updated = body.replace(_STATUS_MARKER, f"{_STATUS_MARKER}\n{marker}", 1)
    if updated == body:
        return
    retry_github_call(
        lambda: comment.edit(updated),
        retries=2,
        description="rebind tracker status marker",
    )


def _upsert_status(issue: Any, body: str, *, tracker: Tracker | None = None) -> None:
    marker = f"{tracker.marker()}\n" if tracker is not None else ""
    rendered = f"{_STATUS_MARKER}\n{marker}{body.rstrip()}\n"
    existing = _find_status_comment(issue)
    if existing is None:
        retry_github_call(
            lambda: issue.create_comment(rendered),
            retries=2,
            description=f"create tracker #{issue.number} status",
        )
    else:
        if _REFRESHED_RE.sub("Status last changed <timestamp> UTC", existing.body or "") == _REFRESHED_RE.sub(
            "Status last changed <timestamp> UTC", rendered
        ):
            return
        retry_github_call(
            lambda: existing.edit(rendered),
            retries=2,
            description=f"update tracker #{issue.number} status",
        )


def _tracker_from_issue(issue: Any, *, allow_body_fallback: bool = False) -> Tracker | None:
    """Read authority metadata from the bot-owned status comment.

    The issue-body fallback exists only to migrate trackers created before
    the marker moved out of maintainer-editable content, and only the trusted
    ensure_tracker path (which immediately re-seeds the status comment) may
    use it. The sync loop must never derive dispatch decisions from content a
    maintainer, or any App with issues:write, can edit.
    """
    bot_login = getattr(getattr(issue, "user", None), "login", "")
    comments = retry_github_call(
        lambda: list(issue.get_comments()),
        retries=2,
        description=f"read tracker #{getattr(issue, 'number', '?')} metadata",
    )
    for comment in comments:
        if (
            getattr(getattr(comment, "user", None), "login", "") == bot_login
            and _STATUS_MARKER in (comment.body or "")
            and (tracker := parse_tracker(comment.body or "")) is not None
        ):
            return tracker
    if allow_body_fallback:
        return parse_tracker(issue.body or "")
    return None


def _repo(gh: Any, name: str) -> Any:
    return retry_github_call(lambda: gh.get_repo(name), retries=2, description=f"get {name}")


def _is_bot_owned(issue: Any) -> bool:
    login = (getattr(getattr(issue, "user", None), "login", "") or "").casefold()
    return login.endswith("[bot]")


def _validate_tracker(tracker: Tracker) -> None:
    string_fields = (
        tracker.repo,
        tracker.branch,
        tracker.version,
        tracker.stage,
        tracker.tag,
        tracker.prep_branch,
    )
    if not all(isinstance(value, str) for value in string_fields):
        raise ValueError("tracker text fields must be strings")
    if not _REPO_RE.fullmatch(tracker.repo):
        raise ValueError("tracker repo must be owner/name")
    if not re.fullmatch(r"[0-9]+\.[0-9]+", tracker.branch, re.ASCII):
        raise ValueError("tracker branch must be MAJOR.MINOR")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", tracker.version, re.ASCII):
        raise ValueError("tracker version must be MAJOR.MINOR.PATCH")
    if not re.fullmatch(r"ga|rc[1-9][0-9]*", tracker.stage, re.ASCII):
        raise ValueError("tracker stage must be ga or rcN")
    expected_tag = tracker.version if tracker.stage == "ga" else f"{tracker.version}-{tracker.stage}"
    if tracker.tag != expected_tag:
        raise ValueError("tracker tag does not match version and stage")
    if tracker.prep_branch != f"{PREP_BRANCH_PREFIX}/{tracker.version}-{tracker.stage}":
        raise ValueError("tracker preparation branch is not canonical")
    if type(tracker.prepare_run_id) is not int or tracker.prepare_run_id <= 0:
        raise ValueError("tracker prepare run id must be positive")


def _write_outputs(values: dict[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValueError(f"multiline workflow output refused for {key}")
            output.write(f"{key}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    ensure = sub.add_parser("ensure")
    for name in ("repo", "branch", "version", "stage", "tag", "prep-branch", "agent-repo"):
        ensure.add_argument(f"--{name}", required=True)
    ensure.add_argument("--prepare-run-id", required=True, type=int)

    sync = sub.add_parser("sync")
    sync.add_argument("--target-repo", default="valkey-io/valkey")
    sync.add_argument("--agent-repo", default="valkey-io/valkey-ci-agent")
    sync.add_argument("--automation-repo", default="valkey-io/valkey-release-automation")
    sync.add_argument("--policy", default=str(_ROOT / "release_policy.yml"))
    sync.add_argument("--no-dispatch", action="store_true")
    add_poll_loop_args(sync)
    args = parser.parse_args(argv)

    target_token = os.environ.get("TARGET_GITHUB_TOKEN", "")
    agent_token = os.environ.get("AGENT_GITHUB_TOKEN", "") or os.environ.get("GITHUB_TOKEN", "")
    automation_token = os.environ.get("AUTOMATION_GITHUB_TOKEN", "")
    if not target_token:
        parser.error("TARGET_GITHUB_TOKEN is required")
    target_gh = Github(auth=Auth.Token(target_token))
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.command == "ensure":
        tracker = Tracker(
            repo=args.repo,
            branch=args.branch,
            version=args.version,
            stage=args.stage,
            tag=args.tag,
            prep_branch=args.prep_branch,
            prepare_run_id=args.prepare_run_id,
        )
        issue = ensure_tracker(target_gh, tracker, agent_repo=args.agent_repo)
        _write_outputs({"issue_number": str(issue.number), "issue_url": issue.html_url})
        print(issue.html_url)
        return 0

    if not agent_token:
        parser.error("AGENT_GITHUB_TOKEN is required for sync")
    if not automation_token:
        parser.error("AUTOMATION_GITHUB_TOKEN is required for sync")
    agent_gh = Github(auth=Auth.Token(agent_token))
    automation_gh = Github(auth=Auth.Token(automation_token))
    policy = load_policy(args.policy)
    def _poll() -> list[str]:
        return sync_trackers(
            target_gh,
            agent_gh,
            automation_gh,
            target_repo=args.target_repo,
            agent_repo=args.agent_repo,
            automation_repo=args.automation_repo,
            policy=policy,
            dispatch=not args.no_dispatch,
        )

    for iteration in run_poll_loop_from_args(_poll, args, logger=logger):
        for result in iteration:
            print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
