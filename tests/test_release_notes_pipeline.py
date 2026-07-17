"""Tests for the shared discover->classify->generate->render pipeline."""

from __future__ import annotations

import os
import shutil

import pytest

from scripts.release_notes import pipeline as pipeline_mod
from scripts.release_notes.models import (
    CategorizedBullet,
    DiscoveryResult,
    GenerationResult,
    MergedPR,
    TriageDecision,
    TriageResult,
)

_FIXTURE_CLONE = os.path.join(os.path.dirname(__file__), "fixtures", "valkey_clone")


@pytest.fixture
def clone(tmp_path):
    dest = tmp_path / "clone"
    shutil.copytree(_FIXTURE_CLONE, dest)
    return str(dest)


def _patch(monkeypatch, *, prs, bullets=(), skipped=(), triage_result=None):
    monkeypatch.setattr(pipeline_mod.discover_mod, "discover",
                        lambda *a, **k: DiscoveryResult(base_tag="9.1.0-rc1", head_ref="9.1", prs=prs))
    monkeypatch.setattr(pipeline_mod.generate_mod, "generate",
                        lambda *a, **k: GenerationResult(bullets=bullets, skipped=skipped))

    # Stub AI triage. By default every label-less candidate is left undecided, so a
    # test that doesn't opt in behaves like the old "unlabelled -> triage" partition.
    # Tests exercising AI include/exclude pass an explicit triage_result.
    def _fake_triage(candidates, **k):
        if triage_result is not None:
            return triage_result
        return TriageResult(undecided=tuple(c.number for c in candidates))

    monkeypatch.setattr(pipeline_mod.triage_mod, "triage", _fake_triage)


def _all_lines(grouped):
    return [line for lines in grouped.values() for line in lines]


def test_empty_range(monkeypatch, clone):
    _patch(monkeypatch, prs=())
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.had_prs is False
    assert r.grouped == {}


def test_generates_and_renders(monkeypatch, clone):
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs,
           bullets=(CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix"),))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.had_prs and r.included == 1 and r.bullet_count == 1
    assert r.grouped["Bug Fixes"] == ["* fix by @a (#40)"]


def test_undecided_candidate_surfaced_as_triage(monkeypatch, clone):
    # A label-less PR AI triage returns no verdict for falls back to human triage.
    prs = (MergedPR(number=50, title="untagged", author="z", url="u", labels=()),)
    _patch(monkeypatch, prs=prs)  # default stub leaves all candidates undecided
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert [p.number for p in r.triage] == [50]
    assert r.ai_included == () and r.ai_excluded == ()
    assert r.included == 0


def test_ai_included_candidate_generates_a_bullet(monkeypatch, clone):
    # A label-less PR AI triage judges user-facing joins generation and is credited,
    # and it is surfaced in ai_included for the PR body (with the model's reason).
    prs = (MergedPR(number=50, title="adds a config", author="z", url="u", labels=()),)
    _patch(monkeypatch, prs=prs,
           bullets=(CategorizedBullet(pr_number=50, author="z", category="Bug Fixes", text="fix"),),
           triage_result=TriageResult(included=(
               TriageDecision(pr_number=50, included=True, reason="adds CONFIG x"),)))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.included == 1 and r.bullet_count == 1
    assert r.grouped["Bug Fixes"] == ["* fix by @z (#50)"]
    assert [(p.number, p.reason, p.included) for p in r.ai_included] == [(50, "adds CONFIG x", True)]
    assert r.triage == ()


def test_ai_excluded_candidate_dropped_and_surfaced(monkeypatch, clone):
    # A label-less PR AI triage judges internal-only is not generated and is
    # surfaced in ai_excluded so a maintainer can sanity-check the drop.
    prs = (MergedPR(number=51, title="refactor", author="z", url="u", labels=()),)
    _patch(monkeypatch, prs=prs,
           triage_result=TriageResult(excluded=(
               TriageDecision(pr_number=51, included=False, reason="internal refactor"),)))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.included == 0 and r.bullet_count == 0
    assert [(p.number, p.reason) for p in r.ai_excluded] == [(51, "internal refactor")]
    assert r.ai_included == () and r.triage == ()


def test_labelled_pr_bypasses_triage(monkeypatch, clone):
    # A release-notes-labelled PR is included directly and never appears in any
    # AI-triage bucket, even though the stub would leave a candidate undecided.
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs,
           bullets=(CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix"),))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.included == 1 and r.bullet_count == 1
    assert r.ai_included == () and r.ai_excluded == () and r.triage == ()


def test_unresolved_backports_passthrough(monkeypatch, clone):
    # An unresolved backport flagged by hydrate_prs must reach RegenResult so the
    # release-cut PR body can surface the suspect credit to a reviewer.
    from scripts.release_notes.models import UnresolvedBackport
    prs = (MergedPR(number=500, title="[Backport 9.1] Fix", author="bot", url="u",
                    labels=("backport",)),)
    unresolved_backports = (UnresolvedBackport(number=500, title="[Backport 9.1] Fix"),)
    monkeypatch.setattr(
        pipeline_mod.discover_mod, "discover",
        lambda *a, **k: DiscoveryResult(
            base_tag="9.1.0-rc1", head_ref="9.1", prs=prs,
            unresolved_backports=unresolved_backports,
        ),
    )
    monkeypatch.setattr(pipeline_mod.generate_mod, "generate",
                        lambda *a, **k: GenerationResult(bullets=(), skipped=()))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert [b.number for b in r.unresolved_backports] == [500]


def test_unresolved_cherry_picks_passthrough(monkeypatch, clone):
    # A cherry-pick suspect flagged by discover() must reach RegenResult so the
    # release-cut PR body can surface the unconfirmed credit to a reviewer.
    from scripts.release_notes.models import UnresolvedCherryPick
    prs = (MergedPR(number=80, title="port fix", author="dev", url="u", labels=()),)
    unresolved_cherry_picks = (UnresolvedCherryPick(
        number=80, sha="rangesha", source_shas=("deadbeefdeadbeef",), subject="port fix (#80)"),)
    monkeypatch.setattr(
        pipeline_mod.discover_mod, "discover",
        lambda *a, **k: DiscoveryResult(
            base_tag="9.1.0-rc1", head_ref="9.1", prs=prs,
            unresolved_cherry_picks=unresolved_cherry_picks,
        ),
    )
    monkeypatch.setattr(pipeline_mod.generate_mod, "generate",
                        lambda *a, **k: GenerationResult(bullets=(), skipped=()))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert [c.number for c in r.unresolved_cherry_picks] == [80]


def test_no_usable_bullets_yields_empty_grouped(monkeypatch, clone):
    # Included PRs but generate produces nothing: bullet_count is 0 and grouped is
    # empty, which is what the cut's blank-cut guard (included and not bullet_count)
    # keys on to refuse the cut.
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs, bullets=(), skipped=(40,))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.bullet_count == 0
    assert r.grouped == {}


def test_reserved_only_bullets_count_as_zero(monkeypatch, clone):
    # Regression: bullet_count must reflect what group_bullets actually renders,
    # not what the model returned. If the model's only bullet is under a reserved
    # category ("Security Fixes", auto-generated at release), group_bullets drops
    # it -> grouped == {} -> bullet_count 0, so the cut's blank-cut guard
    # (included and not bullet_count) fires instead of silently cutting empty notes.
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs, bullets=(
        CategorizedBullet(pr_number=40, author="a", category="Security Fixes", text="hallucinated"),
    ))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.bullet_count == 0        # the reserved-category bullet was dropped, not rendered
    assert r.grouped == {}
    # The dropped PR is folded into skipped so the cut's PR body names it; the
    # label gate is label-only, so this is the only signal it would get.
    assert r.skipped == (40,)


def test_reserved_dropped_pr_folded_into_skipped_others_render(monkeypatch, clone):
    # One PR's only bullet lands under a reserved category (dropped) while another
    # renders normally. The dropped one is surfaced in skipped; the rendered one is
    # not (it is credited by its surviving bullet), and bullet_count counts only it.
    prs = (
        MergedPR(number=40, title="t40", author="a", url="u", labels=("release-notes",)),
        MergedPR(number=41, title="t41", author="b", url="u", labels=("release-notes",)),
    )
    _patch(monkeypatch, prs=prs, bullets=(
        CategorizedBullet(pr_number=40, author="a", category="Security Fixes", text="dropped"),
        CategorizedBullet(pr_number=41, author="b", category="Bug Fixes", text="kept"),
    ))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.bullet_count == 1
    assert r.skipped == (40,)
    assert any("kept" in line for line in _all_lines(r.grouped))


def test_duplicate_pr_bullets_deduped_and_recorded(monkeypatch, clone):
    # The model emits two bullets for the same PR; only the first survives and the
    # PR number is recorded so the caller can flag it in the body.
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs, bullets=(
        CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="first"),
        CategorizedBullet(pr_number=40, author="a", category="New Features", text="second"),
    ))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.bullet_count == 1            # second dropped
    assert r.duplicate_prs == (40,)
    lines = _all_lines(r.grouped)
    assert any("first" in line for line in lines)
    assert not any("second" in line for line in lines)


def test_reserved_bullet_does_not_shadow_real_note(monkeypatch, clone):
    # Regression: the model emits a reserved-section bullet first for a PR, then
    # the PR's real note. The real note must render and the PR must be credited,
    # not discarded as a duplicate and misreported as declined.
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs, bullets=(
        CategorizedBullet(pr_number=40, author="a", category="Security Fixes", text="reserved"),
        CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="real note"),
    ))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.bullet_count == 1
    assert any("real note" in line for line in _all_lines(r.grouped))
    assert not any("reserved" in line for line in _all_lines(r.grouped))
    assert r.skipped == ()          # the PR rendered, so it is not declined
    assert r.duplicate_prs == (40,)  # still flagged as a multi-bullet PR


def test_all_reserved_multi_bullet_pr_declined_not_duplicate(monkeypatch, clone):
    # The model emits multiple bullets for one PR, all under reserved categories.
    # Every bullet is dropped, so the PR renders nowhere and is reported as declined.
    # It must NOT also be flagged as a duplicate: that section asserts a "surviving
    # bullet" to confirm, and nothing survived.
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs, bullets=(
        CategorizedBullet(pr_number=40, author="a", category="Security Fixes", text="one"),
        CategorizedBullet(pr_number=40, author="a", category="Contributors", text="two"),
    ))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.bullet_count == 0
    assert r.skipped == (40,)         # rendered nowhere -> declined
    assert r.duplicate_prs == ()      # not also flagged as a duplicate


def test_uncertain_bullet_surfaced(monkeypatch, clone):
    # A rendered bullet the model flagged uncertain is reported as an UncertainNote
    # so the cut can list it in the PR body; the bullet still renders normally.
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs, bullets=(
        CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix",
                          uncertain=True, uncertain_reason="could be Behavior Changes"),
    ))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.bullet_count == 1
    assert [(n.pr_number, n.category, n.reason) for n in r.uncertain] == [
        (40, "Bug Fixes", "could be Behavior Changes")
    ]


def test_confident_bullets_produce_no_uncertain_notes(monkeypatch, clone):
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs, bullets=(
        CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix"),
    ))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.uncertain == ()


def test_uncertain_dropped_bullet_not_surfaced(monkeypatch, clone):
    # A bullet flagged uncertain but dropped by group_bullets (reserved category)
    # must NOT appear in the uncertain notes: it isn't rendered, so there is
    # nothing for a reviewer to check. Only rendered notes are surfaced.
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs, bullets=(
        CategorizedBullet(pr_number=40, author="a", category="Security Fixes", text="dropped",
                          uncertain=True, uncertain_reason="should not surface"),
    ))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None)
    assert r.grouped == {}
    assert r.uncertain == ()


def test_dedup_bullets_by_pr_keeps_first_preserves_order(monkeypatch):
    bl = [
        CategorizedBullet(pr_number=1, author="a", category="Bug Fixes", text="one"),
        CategorizedBullet(pr_number=2, author="b", category="Bug Fixes", text="two"),
        CategorizedBullet(pr_number=1, author="a", category="New Features", text="dup"),
    ]
    kept, dups = pipeline_mod._dedup_bullets_by_pr(bl)
    assert [b.pr_number for b in kept] == [1, 2]
    assert [b.text for b in kept] == ["one", "two"]
    assert dups == (1,)


def test_dedup_prefers_renderable_over_reserved_bullet(monkeypatch):
    # The model emits a reserved-section bullet FIRST for a PR, then its real note.
    # First-seen dedup would keep the reserved one (dropped by group_bullets) and
    # discard the real note, so the PR renders nowhere and is misreported as
    # declined. Dedup must prefer the renderable bullet regardless of order.
    bl = [
        CategorizedBullet(pr_number=1, author="a", category="Security Fixes", text="reserved"),
        CategorizedBullet(pr_number=1, author="a", category="Bug Fixes", text="real note"),
    ]
    kept, dups = pipeline_mod._dedup_bullets_by_pr(bl)
    assert [b.text for b in kept] == ["real note"]
    assert dups == (1,)


def test_dedup_all_reserved_keeps_first(monkeypatch):
    # When every bullet for a PR is reserved there is nothing renderable to prefer;
    # keep the first so the pipeline still folds the PR into skipped (not credited).
    bl = [
        CategorizedBullet(pr_number=1, author="a", category="Security Fixes", text="one"),
        CategorizedBullet(pr_number=1, author="a", category="Contributors", text="two"),
    ]
    kept, dups = pipeline_mod._dedup_bullets_by_pr(bl)
    assert [b.text for b in kept] == ["one"]
    assert dups == (1,)
