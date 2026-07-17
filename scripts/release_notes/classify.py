"""Classify PRs into include vs. triage-candidate buckets from the release label.

Only the ``release-notes`` label hard-includes a PR. Everything else is a
candidate that AI triage judges, so a change that is user-facing despite a missing
label is still caught. Unlike valkey's label-only ``check_release_notes`` gate
(which only asks "is the label present"), we additionally ask "is the change
actually user-facing" for everything the author did not explicitly opt in.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence

from scripts.release_notes.models import MergedPR, PRDisposition

# The label the release-notes workflow gate enforces; its presence hard-includes.
RELEASE_LABEL = "release-notes"


def disposition_for(labels: Sequence[str]) -> PRDisposition:
    """Map a PR's labels to a disposition.

    ``release-notes`` present -> INCLUDE. Anything else -> CANDIDATE (AI triage
    decides).
    """
    if RELEASE_LABEL in labels:
        return PRDisposition.INCLUDE
    return PRDisposition.CANDIDATE


def classify(prs: Sequence[MergedPR]) -> tuple[list[MergedPR], list[MergedPR]]:
    """Partition *prs* into ``(include, candidates)`` with stamped dispositions.

    ``include`` carried the ``release-notes`` label; ``candidates`` did not and go
    to AI triage. Each returned PR is re-stamped with its resolved disposition.
    """
    include: list[MergedPR] = []
    candidates: list[MergedPR] = []
    for pr in prs:
        disposition = disposition_for(pr.labels)
        stamped = replace(pr, disposition=disposition)
        if disposition is PRDisposition.INCLUDE:
            include.append(stamped)
        else:
            candidates.append(stamped)
    return include, candidates
