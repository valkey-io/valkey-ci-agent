"""Classify PRs into include/exclude/triage buckets based on release-notes labels."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from scripts.release_notes.models import MergedPR, PRDisposition

# Must match the labels enforced by the release-notes workflow gate.
RELEASE_LABEL = "release-notes"
NO_RELEASE_LABEL = "no-release-notes"


def disposition_for(labels: Sequence[str]) -> PRDisposition:
    """Map a PR's labels to a disposition. Both or neither label yields TRIAGE."""
    has_release = RELEASE_LABEL in labels
    has_no_release = NO_RELEASE_LABEL in labels
    if has_release and not has_no_release:
        return PRDisposition.INCLUDE
    if has_no_release and not has_release:
        return PRDisposition.EXCLUDE
    return PRDisposition.TRIAGE


def classify(prs: Sequence[MergedPR]) -> tuple[list[MergedPR], list[MergedPR], list[MergedPR]]:
    """Partition *prs* into ``(include, exclude, triage)`` with stamped dispositions."""
    include: list[MergedPR] = []
    exclude: list[MergedPR] = []
    triage: list[MergedPR] = []
    for pr in prs:
        disposition = disposition_for(pr.labels)
        stamped = replace(pr, disposition=disposition)
        if disposition is PRDisposition.INCLUDE:
            include.append(stamped)
        elif disposition is PRDisposition.EXCLUDE:
            exclude.append(stamped)
        else:
            triage.append(stamped)
    return include, exclude, triage
