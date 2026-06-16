"""macOS verification: dispatch the agent's verify-macos job and wait.

macOS cannot be built on the Linux runner, so the candidate patch is verified
on a macOS runner the agent controls. The patch is transported as an input
(not a local commit, whose SHA a separate workflow cannot fetch); the job checks
out the PR head SHA, applies the patch, runs the command, and the conclusion is
the verdict. This module is purely the dispatch-and-wait transport behind the
``VerifyBackend`` protocol; orchestration (apply/review/push) lives in the
pipeline.
"""

from __future__ import annotations

import base64
import logging
import time
import uuid
from typing import Any

from scripts.ci_fix.review import MAX_REVIEWABLE_PATCH_CHARS
from scripts.ci_fix.verify.base import VerificationPlan, VerificationResult
from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)

VERIFY_MACOS_WORKFLOW = "ci-fix-verify-macos.yml"

# Transport backstop only: the review cap (MAX_REVIEWABLE_PATCH_CHARS) already
# bounds the raw patch upstream, so any patch reaching here is small. This guard
# just keeps the base64 dispatch input safely under GitHub's workflow_dispatch
# input size limit (~64 KB). base64 inflates ~4/3, so allow that much headroom
# over the review cap.
_MAX_PATCH_BYTES = (MAX_REVIEWABLE_PATCH_CHARS * 4) // 3 + 1024
_POLL_INTERVAL_S = 20
_DEFAULT_TIMEOUT_S = 60 * 60


class MacosVerifier:
    """Dispatches and waits on the agent-owned ``verify-macos`` job."""

    def __init__(
        self,
        github_client: Any,
        *,
        agent_repo_full_name: str,
        ref: str = "main",
        timeout: int = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._gh = github_client
        self._agent_repo = agent_repo_full_name
        self._ref = ref
        self._timeout = timeout

    def verify(self, repo_dir: str, plan: VerificationPlan, patch: str) -> VerificationResult:
        """Verify ``patch`` against ``plan.head_sha`` on a macOS runner."""
        encoded = base64.b64encode(patch.encode("utf-8")).decode("ascii")
        if len(encoded) > _MAX_PATCH_BYTES:
            return VerificationResult(
                verified=False, ran=False,
                detail=(
                    f"patch is too large for macOS dispatch verification "
                    f"({len(encoded)} > {_MAX_PATCH_BYTES} bytes); refusing"
                ),
            )

        token = uuid.uuid4().hex
        dispatched_at = time.time()
        if not self._dispatch(plan, encoded, token):
            return VerificationResult(
                verified=False, ran=False,
                detail="could not dispatch the macOS verification job",
            )

        run = self._await_run(token, since=dispatched_at)
        if run is None:
            return VerificationResult(
                verified=False, ran=False,
                detail=f"macOS verification did not complete within {self._timeout}s",
            )
        url = str(getattr(run, "html_url", "") or "")
        conclusion = str(getattr(run, "conclusion", "") or "")
        if conclusion == "success":
            return VerificationResult(
                verified=True, ran=True, detail="targeted macOS verification passed", run_url=url,
            )
        return VerificationResult(
            verified=False, ran=True,
            detail=f"targeted macOS verification did not pass ({conclusion})", run_url=url,
        )

    def _dispatch(self, plan: VerificationPlan, encoded_patch: str, token: str) -> bool:
        inputs = {
            "target_repo": plan.target_repo,
            "head_sha": plan.head_sha,
            "patch_b64": encoded_patch,
            "verify_command": plan.command,
            "workdir": plan.workdir,
            "correlation": token,
        }

        def _do() -> bool:
            repo = self._gh.get_repo(self._agent_repo)
            workflow = repo.get_workflow(VERIFY_MACOS_WORKFLOW)
            return bool(workflow.create_dispatch(self._ref, inputs))

        try:
            return retry_github_call(_do, retries=2, description="dispatch verify-macos")
        except Exception as exc:  # noqa: BLE001 - dispatch failure is a clean refusal
            logger.warning("verify-macos dispatch failed: %s", exc)
            return False

    def _await_run(self, token: str, *, since: float) -> Any | None:
        """Find the run carrying ``token`` and poll it to completion."""
        deadline = time.time() + self._timeout
        run = None
        while time.time() < deadline:
            if run is None:
                run = self._find_run(token, since=since)
            else:
                run = self._reload(run)
            if run is not None and str(getattr(run, "status", "")) == "completed":
                return run
            time.sleep(_POLL_INTERVAL_S)
        if run is not None:
            run = self._reload(run)
            if str(getattr(run, "status", "")) == "completed":
                return run
        return None

    def _find_run(self, token: str, *, since: float) -> Any | None:
        marker = f"[token:{token}]"

        def _list() -> list[Any]:
            repo = self._gh.get_repo(self._agent_repo)
            workflow = repo.get_workflow(VERIFY_MACOS_WORKFLOW)
            return list(workflow.get_runs()[:20])

        try:
            runs = retry_github_call(_list, retries=2, description="list verify-macos runs")
        except Exception as exc:  # noqa: BLE001
            logger.warning("listing verify-macos runs failed: %s", exc)
            return None
        for run in runs:
            # GitHub exposes the custom run-name as display_title; name is the
            # workflow name. The token marker lives in the run-name.
            haystack = (
                f"{getattr(run, 'display_title', '') or ''} "
                f"{getattr(run, 'name', '') or ''}"
            )
            if marker not in haystack:
                continue
            if not _run_created_after(run, since):
                # A run bearing the token but created before we dispatched is
                # not ours; never trust a pre-existing run's conclusion.
                continue
            return run
        return None

    def _reload(self, run: Any) -> Any:
        def _get() -> Any:
            return self._gh.get_repo(self._agent_repo).get_workflow_run(run.id)

        try:
            return retry_github_call(_get, retries=2, description="reload verify-macos run")
        except Exception as exc:  # noqa: BLE001
            logger.warning("reloading verify-macos run failed: %s", exc)
            return run


# Tolerance for comparing the run's server-side created_at against our local
# dispatch time. GitHub timestamps are second-granularity and clocks can skew,
# so a run created in the same second can look slightly earlier. The fresh-UUID
# token match is the real identity check; this guard only excludes clearly
# pre-existing runs, so a generous tolerance is safe.
_CREATED_AT_TOLERANCE_S = 120


def _run_created_after(run: Any, since: float) -> bool:
    """True if ``run`` was plausibly created for this dispatch (token + recency).

    Allows ``_CREATED_AT_TOLERANCE_S`` of skew before ``since`` so a same-second
    or slightly-skewed run is not wrongly judged stale. Runs whose creation time
    cannot be read are treated as not-after (the token match still gates).
    """
    created = getattr(run, "created_at", None)
    if created is None:
        return False
    try:
        return created.timestamp() >= since - _CREATED_AT_TOLERANCE_S
    except (AttributeError, TypeError, ValueError):
        return False
