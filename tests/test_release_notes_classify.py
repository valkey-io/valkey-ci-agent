"""Tests for release-notes label disposition (pure)."""

from __future__ import annotations

from scripts.release_notes.classify import classify, disposition_for
from scripts.release_notes.models import MergedPR, PRDisposition


def _pr(number: int, labels: tuple[str, ...]) -> MergedPR:
    return MergedPR(number=number, title="t", author="a", url="u", labels=labels)


class TestDispositionFor:
    def test_release_notes_only_includes(self) -> None:
        assert disposition_for(("release-notes",)) is PRDisposition.INCLUDE

    def test_no_release_notes_only_excludes(self) -> None:
        assert disposition_for(("no-release-notes",)) is PRDisposition.EXCLUDE

    def test_neither_label_triages(self) -> None:
        assert disposition_for(("bug", "area/cluster")) is PRDisposition.TRIAGE

    def test_empty_triages(self) -> None:
        assert disposition_for(()) is PRDisposition.TRIAGE

    def test_both_labels_triage_not_include(self) -> None:
        # A contradiction the CI check rejects: must never silently include.
        assert disposition_for(("release-notes", "no-release-notes")) is PRDisposition.TRIAGE

    def test_release_notes_with_other_labels_includes(self) -> None:
        assert disposition_for(("release-notes", "bug")) is PRDisposition.INCLUDE


class TestClassify:
    def test_partitions_and_restamps(self) -> None:
        prs = [
            _pr(1, ("release-notes",)),
            _pr(2, ("no-release-notes",)),
            _pr(3, ()),
            _pr(4, ("release-notes", "no-release-notes")),
        ]
        include, exclude, triage = classify(prs)
        assert [p.number for p in include] == [1]
        assert [p.number for p in exclude] == [2]
        assert sorted(p.number for p in triage) == [3, 4]
        # Disposition is stamped onto the returned objects.
        assert include[0].disposition is PRDisposition.INCLUDE
        assert exclude[0].disposition is PRDisposition.EXCLUDE
        assert all(p.disposition is PRDisposition.TRIAGE for p in triage)

    def test_preserves_pr_fields(self) -> None:
        pr = MergedPR(number=9, title="Title", author="bob", url="https://x/9",
                      labels=("release-notes",), merge_commit_sha="abc")
        include, _, _ = classify([pr])
        out = include[0]
        assert (out.number, out.title, out.author, out.url, out.merge_commit_sha) == (
            9, "Title", "bob", "https://x/9", "abc",
        )

    def test_empty_input(self) -> None:
        assert classify([]) == ([], [], [])
