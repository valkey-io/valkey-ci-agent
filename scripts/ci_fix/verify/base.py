"""Verification backends: the boundary where a candidate fix is proven.

The AI proposes a fix and a targeted command (a ``FixProposal``). Code owns
everything about *where and whether* that command is trusted: it determines the
failed job from the linked run, classifies that job's environment, and selects
a backend. A backend takes a ``VerificationPlan`` and returns a
``VerificationResult`` whose verdict comes only from a real exit code (or a real
CI run conclusion), never from the model.

Backends:
- ``local`` runs the command on this Linux runner.
- ``docker`` runs it inside the failed job's container image.
- ``macos`` dispatches it to a macOS runner the agent controls and waits.

Each is a thin implementation of the same ``VerifyBackend`` protocol, so the
pipeline selects one and calls ``verify`` without knowing the mechanics.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class VerifyEnv(str, Enum):
    """The environment a failed job runs in, as classification supports it."""

    LOCAL = "local"          # plain Linux runner: run the command directly
    DOCKER = "docker"        # Linux runner + container: run inside the image
    MACOS = "macos"          # macOS runner: verify via the macOS backend
    UNSUPPORTED = "unsupported"  # cannot be classified/verified safely


@dataclass(frozen=True)
class FailedJob:
    """A job that did not succeed in the linked CI run (owned by code)."""

    name: str
    conclusion: str


@dataclass(frozen=True)
class VerificationPlan:
    """How and where code will verify a candidate fix.

    Produced by code from the actual failed job and its workflow definition,
    not from the AI. ``command`` is the AI's targeted verify command, but the
    environment, image, and backend are code's decision.
    """

    env: VerifyEnv
    command: str
    workdir: str = ""
    image: str = ""          # set only for DOCKER
    job_name: str = ""       # the failed job this plan verifies
    head_sha: str = ""       # the PR head SHA (macOS verifies against it)
    target_repo: str = ""    # the repo to check out for remote (macOS) verification


@dataclass(frozen=True)
class VerificationResult:
    """The factual outcome of running a plan. ``verified`` is never AI-decided.

    ``command`` and ``output_tail`` carry local evidence; ``run_url`` carries a
    remote CI run link (macOS). ``ran`` is False only when verification could
    not be attempted at all, which the pipeline treats as a refusal.
    """

    verified: bool
    ran: bool
    detail: str
    command: str = ""
    output_tail: str = ""
    run_url: str = ""


class VerifyBackend(Protocol):
    """Verifies a candidate fix already applied to ``repo_dir``.

    ``patch`` is the approved patch (the artifact under test); ``plan`` is the
    code-selected where/how. Local/Docker verification ignores ``patch`` (the
    fix is already in the working tree); the macOS backend transports it.
    """

    def verify(self, repo_dir: str, plan: VerificationPlan, patch: str) -> VerificationResult:
        ...


def backend_label(env: VerifyEnv, image: str = "") -> str:
    """A short human label for the verifier used, for the PR comment.

    Derived from the env (and image) so the comment string and the routing
    enum cannot drift apart.
    """
    if env is VerifyEnv.DOCKER:
        return f"docker:{image}" if image else "docker"
    return env.value
