"""Render detected test failures into GitHub issue title, body, and comment text.

The rendering is test-failure-specific (test name/file, error trace, the list
of CI jobs the failure appeared in); the create-or-update machinery lives in
:mod:`scripts.common.issue_dedup`.

A test failure's identity is the ``test_name`` + ``test_file`` pair, which is
the dedup fingerprint. Across recurrences we accumulate the set of failing
environments (CI jobs) into the issue body; because the dedup publisher's
``render`` callback can't see the previously published body, that merge is done
via the publisher's ``body_transform`` hook (see :func:`merge_environments`).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Callable

from scripts.common.incidents import compute_fingerprint
from scripts.common.issue_dedup import IssueContent
from scripts.test_failure_detector.parse_failures import UniqueFailure

MARKER_NAMESPACE = "valkey-ci-agent:test-failure"

_LABEL_NAME = "test-failure"


def fingerprint_for(failure: UniqueFailure) -> str:
    """Stable dedup key for a failure: a hash of test name + file.

    The identity is ``test_name`` + ``test_file``, hashed via
    :func:`scripts.common.incidents.compute_fingerprint` like the fuzzer
    pipeline, into a fixed-shape hex token that is safe to embed in the marker
    and the search query.

    The pair is the identity, so it goes in ``namespace`` (joined in order,
    never normalized) rather than ``shapes``. That keeps digits significant so
    PSYNC2 and PSYNC3 stay distinct, and preserves order so a name/file swap
    cannot collide.
    """
    return compute_fingerprint(
        namespace=(MARKER_NAMESPACE, failure.test_name, failure.test_file),
        shapes=(),
    )


def title_for(failure: UniqueFailure) -> str:
    """Issue title for a failure.

    Exposed rather than inlined in ``render_for`` so callers can pass the same
    title to ``IssueDedupPublisher.upsert`` as ``title_fallback`` when
    migrating issues off the old raw-fingerprint marker.
    """
    return _build_title(failure)


def render_for(failure: UniqueFailure) -> Callable[[str, int], IssueContent]:
    """Return the render callable expected by ``IssueDedupPublisher.upsert``."""
    def _render(marker: str, occurrences: int) -> IssueContent:
        return IssueContent(
            title=title_for(failure),
            body=_build_body(failure, marker, occurrences=occurrences),
            comment=_build_comment(failure),
            labels=(_LABEL_NAME,),
        )
    return _render


def merge_environments(failure: UniqueFailure) -> Callable[[str], str]:
    """Return a ``body_transform`` that folds this failure's environments into
    an existing issue body, preserving environments recorded by earlier runs.
    """
    def _transform(existing_body: str) -> str:
        existing_envs = _extract_environments_from_body(existing_body)
        new_envs = [j.job for j in failure.jobs if j.job not in existing_envs]
        if not new_envs:
            return existing_body
        return _update_environments_in_body(existing_body, existing_envs + new_envs)
    return _transform


def _build_title(failure: UniqueFailure) -> str:
    return f"[TEST-FAILURE] {failure.test_name} in {failure.test_file}"


def _build_body(failure: UniqueFailure, marker: str, *, occurrences: int) -> str:
    """Build the issue body for a test failure."""
    ci_links = "\n".join(
        f"- `{j.job}`: [CI link]({j.url})" for j in failure.jobs
    )
    env_list = ", ".join(f"`{j.job}`" for j in failure.jobs)

    return "\n".join([
        marker,
        f"<!-- {MARKER_NAMESPACE}:occurrences:{occurrences} -->",
        "",
        "**Summary**",
        "",
        f"`{failure.test_name}` in `{failure.test_file}` is failing in CI.",
        "",
        "**Failing test(s)**",
        "",
        f"- Test name: `{failure.test_name}`",
        f"- Test file: `{failure.test_file}`",
        "- CI link(s):",
        ci_links,
        "",
        "**Error stack trace**",
        "",
        "```",
        failure.error or "N/A",
        "```",
        "",
        f"**Environments:** {env_list}",
        "",
        "---",
        "*Auto-created by Test Failure Detector*",
    ])


def _build_comment(failure: UniqueFailure) -> str:
    """Build a comment for an existing issue that failed again."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    ci_links = "\n".join(
        f"- `{j.job}`: [CI link]({j.url})" for j in failure.jobs
    )
    return f"Test failed again on {today}.\n\n**Failed in:**\n{ci_links}"


def _extract_environments_from_body(body: str) -> list[str]:
    """Extract existing environment names from an issue body."""
    env_match = re.search(r"\*\*Environments:\*\*\s*(.+)", body)
    if not env_match:
        return []
    return re.findall(r"`([^`]+)`", env_match.group(1))


def _update_environments_in_body(body: str, all_envs: list[str]) -> str:
    """Replace the Environments line in the issue body with an updated list."""
    new_env_line = f"**Environments:** {', '.join(f'`{e}`' for e in all_envs)}"
    return re.sub(r"\*\*Environments:\*\*\s*.+", new_env_line, body)
