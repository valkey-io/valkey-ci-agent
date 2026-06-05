"""GitHub Actions workflow run and artifact retrieval.

Lists recent runs of a target workflow file and downloads their uploaded
artifact bundles into an in-memory ``{path: bytes}`` map.
"""

from __future__ import annotations

import io
import logging
import time
import zipfile
from dataclasses import dataclass
from itertools import islice
from typing import TYPE_CHECKING, Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.common.github_client import (
    RETRYABLE_HTTP_STATUS,
    retry_github_call,
    transient_backoff_delay,
)

if TYPE_CHECKING:
    from github import Github

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkflowArtifact:
    artifact_id: int
    name: str
    size_in_bytes: int
    expired: bool


class ArtifactClient:
    """Fetches workflow artifacts and logs from GitHub Actions."""

    def __init__(self, github_client: Github, *, token: str, retries: int = 3) -> None:
        if not token:
            raise ValueError("GitHub token is required")
        self._gh = github_client
        self._token = token
        self._retries = retries

    def list_recent_runs(
        self, repo_full_name: str, workflow_file: str,
        *, event: str = "schedule", max_runs: int = 1,
    ) -> list[Any]:
        def _fetch() -> list[Any]:
            repo = self._gh.get_repo(repo_full_name)
            workflow = repo.get_workflow(workflow_file)
            return list(islice(workflow.get_runs(event=event, status="completed"), max_runs))

        return retry_github_call(
            _fetch, retries=self._retries, description=f"list runs {workflow_file}",
        )

    def list_run_artifacts(self, repo_full_name: str, run_id: int) -> list[WorkflowArtifact]:
        repo = self._gh.get_repo(repo_full_name)

        def _fetch() -> Any:
            _, data = repo._requester.requestJsonAndCheck(
                "GET", f"/repos/{repo_full_name}/actions/runs/{run_id}/artifacts",
            )
            return data

        payload = retry_github_call(_fetch, retries=self._retries,
                                    description=f"list artifacts {run_id}")
        if not isinstance(payload, dict):
            return []
        return [
            WorkflowArtifact(
                artifact_id=a["id"], name=a["name"],
                size_in_bytes=a.get("size_in_bytes", 0),
                expired=a.get("expired", False),
            )
            for a in payload.get("artifacts", [])
            if isinstance(a, dict)
            and isinstance(a.get("id"), int)
            and isinstance(a.get("name"), str)
        ]

    def download_artifact(self, repo_full_name: str, artifact_id: int) -> dict[str, bytes]:
        return _extract_zip(self._download(
            f"/repos/{repo_full_name}/actions/artifacts/{artifact_id}/zip"
        ))

    def _download(self, path: str) -> bytes:
        url = f"https://api.github.com{path}"
        req = Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "valkey-ci-agent",
        })
        # Use an unredirected header for the token: urllib forwards normal
        # headers on cross-host redirects, but GitHub redirects to signed S3
        # URLs that must not receive our token. add_unredirected_header keeps
        # the Authorization off the redirected request.
        req.add_unredirected_header("Authorization", f"Bearer {self._token}")
        # Hand-rolled retry rather than retry_github_call: this is a raw urllib
        # call (not a PyGithub operation) and needs HTTP-status-specific
        # handling for the 404/expired case below. Retry classification and
        # backoff are shared with retry_github_call so behavior stays uniform.
        for attempt in range(self._retries + 1):
            try:
                with urlopen(req, timeout=120) as resp:
                    return resp.read()
            except HTTPError as exc:
                if exc.code == 404:
                    logger.warning("Artifact not found at %s (likely expired)", path)
                    return b""
                if exc.code in RETRYABLE_HTTP_STATUS and attempt < self._retries:
                    time.sleep(transient_backoff_delay(attempt))
                    continue
                raise
            except (URLError, TimeoutError, ConnectionError):
                if attempt < self._retries:
                    time.sleep(transient_backoff_delay(attempt))
                    continue
                raise
        raise AssertionError("unreachable: retry loop must return or raise")


# Defends against a buggy fuzzer producing a runaway log dump that would
# exhaust the runner. Real fuzzer artifacts are typically <50 MB.
_MAX_UNCOMPRESSED_BYTES = 500 * 1024 * 1024


def _extract_zip(blob: bytes) -> dict[str, bytes]:
    if not blob:
        return {}
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            total = sum(m.file_size for m in members)
            if total > _MAX_UNCOMPRESSED_BYTES:
                logger.warning(
                    "Artifact uncompressed size %d exceeds cap %d; refusing to extract",
                    total, _MAX_UNCOMPRESSED_BYTES,
                )
                return {}
            return {m.filename: zf.read(m) for m in members}
    except zipfile.BadZipFile:
        logger.warning("Artifact zip is corrupt; returning empty")
        return {}
