"""Tests for release-notes label disposition (pure).

The gate is single-label: only ``release-notes`` hard-includes. Everything else
(no label, or any other label) is a CANDIDATE that AI triage judges.
"""

from __future__ import annotations

from scripts.release_notes.classify import classify, disposition_for
from scripts.release_notes.models import MergedPR, PRDisposition


def _pr(number: int, labels: tuple[str, ...]) -> MergedPR:
    return MergedPR(number=number, title="t", author="a", url="u", labels=labels)


class TestDispositionFor:
    def test_release_notes_only_includes(self) -> None:
        assert disposition_for(("release-notes",)) is PRDisposition.INCLUDE

    def test_arbitrary_labels_are_candidates(self) -> None:
        assert disposition_for(("bug", "area/cluster")) is PRDisposition.CANDIDATE

    def test_empty_is_a_candidate(self) -> None:
        assert disposition_for(()) is PRDisposition.CANDIDATE

    def test_release_notes_with_other_labels_includes(self) -> None:
        assert disposition_for(("release-notes", "bug")) is PRDisposition.INCLUDE


class TestClassify:
    def test_partitions_and_restamps(self) -> None:
        prs = [
            _pr(1, ("release-notes",)),
            _pr(2, ("bug",)),
            _pr(3, ()),
            _pr(4, ("release-notes", "enhancement")),
        ]
        include, candidates = classify(prs)
        assert [p.number for p in include] == [1, 4]
        assert sorted(p.number for p in candidates) == [2, 3]
        # Disposition is stamped onto the returned objects.
        assert all(p.disposition is PRDisposition.INCLUDE for p in include)
        assert all(p.disposition is PRDisposition.CANDIDATE for p in candidates)

    def test_preserves_pr_fields(self) -> None:
        pr = MergedPR(number=9, title="Title", author="bob", url="https://x/9",
                      labels=("release-notes",), merge_commit_sha="abc")
        include, _ = classify([pr])
        out = include[0]
        assert (out.number, out.title, out.author, out.url, out.merge_commit_sha) == (
            9, "Title", "bob", "https://x/9", "abc",
        )

    def test_empty_input(self) -> None:
        assert classify([]) == ([], [])

    def test_backport_remapped_pr_classified_by_source_labels(self) -> None:
        # After discovery remaps a backport to its original PR, the MergedPR carries
        # the *source* PR's labels. A source labelled release-notes must INCLUDE,
        # even though the backport PR itself would have carried only "backport"
        # (which is a candidate). This pins that identity, not the on-line PR, gates.
        remapped = MergedPR(number=7, title="Fix a leak", author="alice",
                            url="https://x/7", labels=("release-notes",))
        include, candidates = classify([remapped])
        assert [p.number for p in include] == [7]
        assert candidates == []
