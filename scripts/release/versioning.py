"""Deterministic version derivation from a release branch and existing tags.

Given a release branch (``M.m``), the operator's intent (rc/ga/patch), and the
repository's existing tags, exactly one next version and stage follow. The
operator never types a version, so a Start Release dispatch cannot introduce a
version that disagrees with the branch or repeats an existing tag.

Valkey's tag model (mirrored from the release-notes cut):

    M.m.p       final release (stage "ga")
    M.m.p-rcN   release candidate, N >= 1

Only tags on the requested release line are considered; tags in any other
format are ignored rather than guessed at.
"""

from __future__ import annotations

import re
from typing import Iterable

from scripts.release.models import DerivedRelease, ReleaseIntent

# No leading zeros: git treats 9.01 and 9.1 as distinct refs, so accepting
# a zero-padded component would derive versions for the wrong branch, and a
# zero-padded tag (9.01.0) would be counted onto the wrong release line.
_BRANCH_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_FINAL_TAG_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_RC_TAG_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-rc([1-9]\d*)$")


def parse_release_branch(branch: str) -> tuple[int, int]:
    """Return ``(major, minor)`` for an ``M.m`` release branch.

    Raises :class:`ValueError` for anything else (``unstable``, ``main``, a
    full ``M.m.p`` version, ...), so a wrong-branch dispatch fails before any
    GitHub state is touched.
    """
    m = _BRANCH_RE.match(branch.strip())
    if not m:
        raise ValueError(f"not a release branch: {branch!r} (expected MAJOR.MINOR, e.g. '9.1')")
    return int(m.group(1)), int(m.group(2))


def derive_version(branch: str, intent: ReleaseIntent, tags: Iterable[str]) -> DerivedRelease:
    """Derive the next version and stage for *branch* from existing *tags*.

    Pure and deterministic: the same branch, intent, and tag set always yield
    the same result. Raises :class:`ValueError` when the intent is impossible
    for the branch's tag state (e.g. an RC after the line already shipped a
    final release), so the impossibility is reported instead of guessed around.
    """
    major, minor = parse_release_branch(branch)

    finals: list[int] = []  # patch numbers of final releases on this line
    initial_rcs: list[int] = []  # rc numbers of M.m.0 release candidates
    for tag in tags:
        f = _FINAL_TAG_RE.match(tag)
        if f and (int(f.group(1)), int(f.group(2))) == (major, minor):
            finals.append(int(f.group(3)))
            continue
        r = _RC_TAG_RE.match(tag)
        if r and (int(r.group(1)), int(r.group(2)), int(r.group(3))) == (major, minor, 0):
            initial_rcs.append(int(r.group(4)))

    # Any final release on the line closes the rc/ga window: deriving M.m.0
    # while e.g. M.m.1 exists would produce a version *lower* than a shipped
    # release (reachable when the .0 tag was deleted or the line was seeded
    # at .1), so the guard is "any final", not "the .0 final".
    line_released = bool(finals)

    if intent is ReleaseIntent.RC:
        if line_released:
            raise ValueError(
                f"{branch} already has a final release ({branch}.{max(finals)}); "
                f"release candidates only precede the initial release of a line "
                f"(use intent 'patch')"
            )
        next_rc = max(initial_rcs) + 1 if initial_rcs else 1
        return DerivedRelease(version=f"{major}.{minor}.0", stage=f"rc{next_rc}")

    if intent is ReleaseIntent.GA:
        if line_released:
            raise ValueError(
                f"{branch} already has a final release ({branch}.{max(finals)}); "
                f"use intent 'patch' for the next release on this line"
            )
        return DerivedRelease(version=f"{major}.{minor}.0", stage="ga")

    if intent is ReleaseIntent.PATCH:
        if not finals:
            raise ValueError(
                f"no final release exists on {branch}; a patch requires an initial release (use intent 'rc' or 'ga')"
            )
        return DerivedRelease(version=f"{major}.{minor}.{max(finals) + 1}", stage="ga")

    raise ValueError(f"cannot derive a version for intent {intent.value!r}")
