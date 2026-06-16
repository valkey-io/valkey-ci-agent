"""Deterministic environment selection for a CI workflow job.

The AI hints which job a failure belongs to; code owns the security-relevant
decision of which environment that job runs in, and therefore which controlled
verifier may run the command. This module parses a GitHub Actions workflow
narrowly, reading only ``runs-on`` and ``container.image`` to classify the job
as local, docker(image), macos, or unsupported. It does not extract or replay
the job's steps. Anything it does not clearly understand (matrix-indirected
runners, dynamic or malformed images, self-hosted runners, non-Linux/non-macOS
platforms) is unsupported, and the caller refuses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

from scripts.ci_fix.verify.base import VerifyEnv


@dataclass(frozen=True)
class JobEnvironment:
    """The verifier environment for one workflow job.

    ``image`` is set only for DOCKER. ``reason`` is set only for UNSUPPORTED.
    """

    env: VerifyEnv
    image: str = ""
    reason: str = ""


# A container image we are willing to run: a normal image reference (optionally
# with a registry host and port, and a tag or @sha256 digest), no expression
# interpolation (``${{ ... }}``) and no shell-surprising characters.
_IMAGE_RE = re.compile(
    r"^[a-z0-9][a-z0-9._/-]*"            # registry/repository path
    r"(:[0-9]+)?"                         # optional registry port
    r"([a-z0-9._/-]*)"                    # optional path after port
    r"(:[a-zA-Z0-9._-]+)?"                # optional tag
    r"(@sha256:[a-f0-9]{64})?$"           # optional digest
)

# GitHub-hosted x86-64 Linux runner labels we can reproduce locally. An arm
# label (e.g. ubuntu-24.04-arm) is deliberately excluded: verifying an
# arm-specific failure on x86 would be wrong, so it stays unsupported.
_X86_LINUX_RUNNERS = frozenset({
    "ubuntu-latest",
    "ubuntu-24.04",
    "ubuntu-22.04",
    "ubuntu-20.04",
})


def classify_job_environment(workflow_yaml: str, job_id: str) -> JobEnvironment:
    """Classify the runner environment of ``job_id`` in ``workflow_yaml``.

    Returns a ``JobEnvironment``; ``env`` is UNSUPPORTED (with a reason)
    whenever the job's environment cannot be determined safely. Never raises for
    malformed input - a parse failure is reported as UNSUPPORTED.
    """
    try:
        doc = yaml.safe_load(workflow_yaml)
    except yaml.YAMLError as exc:
        return JobEnvironment(VerifyEnv.UNSUPPORTED, reason=f"workflow YAML did not parse: {exc}")
    if not isinstance(doc, dict):
        return JobEnvironment(VerifyEnv.UNSUPPORTED, reason="workflow root is not a mapping")

    jobs = doc.get("jobs")
    if not isinstance(jobs, dict) or job_id not in jobs:
        return JobEnvironment(VerifyEnv.UNSUPPORTED, reason=f"job {job_id!r} not found in workflow")
    job = jobs[job_id]
    if not isinstance(job, dict):
        return JobEnvironment(VerifyEnv.UNSUPPORTED, reason=f"job {job_id!r} is not a mapping")

    env = _classify_env(job)
    if env is VerifyEnv.UNSUPPORTED:
        return JobEnvironment(VerifyEnv.UNSUPPORTED, reason=f"unsupported runner: {job.get('runs-on')!r}")
    if env is VerifyEnv.DOCKER:
        image = _container_image(job)
        if not image:
            return JobEnvironment(
                VerifyEnv.UNSUPPORTED,
                reason="container image is dynamic or malformed; cannot run it safely",
            )
        return JobEnvironment(VerifyEnv.DOCKER, image=image)
    return JobEnvironment(env)


def _classify_env(job: dict[str, Any]) -> VerifyEnv:
    runs_on = job.get("runs-on")
    if not isinstance(runs_on, str):
        # A list runner (self-hosted matrix) or a matrix expression.
        return VerifyEnv.UNSUPPORTED
    label = runs_on.strip().lower()
    if "${{" in label:
        return VerifyEnv.UNSUPPORTED  # matrix-indirected runner
    if label.startswith("macos"):
        return VerifyEnv.MACOS
    # Only x86-64 Linux is locally verifiable on this runner. An arm Linux
    # runner (e.g. ubuntu-24.04-arm) must NOT be verified on x86, so it is
    # unsupported until an arm-capable verifier exists. Match an explicit
    # allowlist of x86 ubuntu labels rather than any "ubuntu" prefix.
    if label in _X86_LINUX_RUNNERS:
        return VerifyEnv.DOCKER if job.get("container") is not None else VerifyEnv.LOCAL
    # windows, self-hosted, arm Linux, or anything else.
    return VerifyEnv.UNSUPPORTED


def _container_image(job: dict[str, Any]) -> str:
    container = job.get("container")
    if isinstance(container, str):
        image = container.strip()
    elif isinstance(container, dict) and isinstance(container.get("image"), str):
        image = container["image"].strip()
    else:
        return ""
    if "${{" in image or not _IMAGE_RE.fullmatch(image):
        return ""
    return image
