"""Assign a release-notes disposition to each discovered PR from its labels.

Pure (no I/O): every input is already on the :class:`MergedPR`, so this is
trivially unit-testable. The label names and the "exactly one label" rule are
the convention this automation defines for gating release-note PRs; keep them
in step with whatever label gate the ``release-notes`` workflow enforces.
"""

from __future__ import annotations

from typing import Sequence

from scripts.release_notes.models import MergedPR, PRDisposition

# Must match the labels the release-notes workflow gate enforces.
RELEASE_LABEL = "release-notes"
NO_RELEASE_LABEL = "no-release-notes"


def disposition_for(labels: Sequence[str]) -> PRDisposition:
    """Map a PR's labels to a :class:`PRDisposition`.

    ``no-release-notes`` always suppresses inclusion, so a PR carrying both
    labels is a contradiction the CI check would reject: it goes to TRIAGE, never
    silently INCLUDE. A PR carrying neither also goes to TRIAGE so an untagged
    change surfaces to a human rather than vanishing.
    """
    has_release = RELEASE_LABEL in labels
    has_no_release = NO_RELEASE_LABEL in labels
    if has_release and not has_no_release:
        return PRDisposition.INCLUDE
    if has_no_release and not has_release:
        return PRDisposition.EXCLUDE
    return PRDisposition.TRIAGE


def classify(prs: Sequence[MergedPR]) -> tuple[list[MergedPR], list[MergedPR], list[MergedPR]]:
    """Partition *prs* into ``(include, exclude, triage)``.

    Each returned :class:`MergedPR` is re-stamped with its computed
    disposition (the dataclass is frozen, so a new instance is produced).
    """
    include: list[MergedPR] = []
    exclude: list[MergedPR] = []
    triage: list[MergedPR] = []
    for pr in prs:
        disposition = disposition_for(pr.labels)
        stamped = MergedPR(
            number=pr.number,
            title=pr.title,
            author=pr.author,
            url=pr.url,
            body=pr.body,
            labels=pr.labels,
            merge_commit_sha=pr.merge_commit_sha,
            disposition=disposition,
        )
        if disposition is PRDisposition.INCLUDE:
            include.append(stamped)
        elif disposition is PRDisposition.EXCLUDE:
            exclude.append(stamped)
        else:
            triage.append(stamped)
    return include, exclude, triage
