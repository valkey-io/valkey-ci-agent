"""Tests for the shared discover->classify->generate->render pipeline."""

from __future__ import annotations

import os
import shutil
from dataclasses import replace

import pytest

from scripts.release_notes import pipeline as pipeline_mod
from scripts.release_notes import projects
from scripts.release_notes.models import (
    CategorizedBullet,
    DiscoveryResult,
    GenerationResult,
    MergedPR,
    TriageDecision,
    TriageResult,
    UnresolvedBackport,
)

_FIXTURE_CLONE = os.path.join(os.path.dirname(__file__), "fixtures", "valkey_clone")


@pytest.fixture
def clone(tmp_path):
    dest = tmp_path / "clone"
    shutil.copytree(_FIXTURE_CLONE, dest)
    return str(dest)


def _patch(
    monkeypatch, *, prs, bullets=(), skipped=(), triage_result=None,
    unresolved_backports=(),
):
    monkeypatch.setattr(pipeline_mod.discover_mod, "discover",
                        lambda *a, **k: DiscoveryResult(
                            base_tag="9.1.0-rc1", head_ref="9.1", prs=prs,
                            unresolved_backports=unresolved_backports,
                        ))
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
    assert r.had_prs is False
    assert r.grouped == {}


def test_generates_and_renders(monkeypatch, clone):
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs,
           bullets=(CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix"),))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
    assert r.had_prs and r.included == 1 and r.bullet_count == 1
    assert r.grouped["Bug Fixes"] == ["* fix by @a (#40)"]


def test_profile_values_are_forwarded_explicitly(monkeypatch, clone):
    profile = replace(
        projects.VALKEY_PROFILE,
        generation_prompt_project="sentinel generation project",
        triage_prompt_project="sentinel triage project",
        category_guidance="sentinel category guidance",
        categories=("Configuration", "Other Changes"),
    )
    prs = (
        MergedPR(number=40, title="change a", author="a", url="u", labels=()),
        MergedPR(number=41, title="change b", author="b", url="u", labels=()),
    )
    monkeypatch.setattr(
        pipeline_mod.discover_mod,
        "discover",
        lambda *a, **k: DiscoveryResult(
            base_tag="9.1.0-rc1", head_ref="9.1", prs=prs
        ),
    )
    captured = {}

    def fake_triage(candidates, **kwargs):
        captured["triage_project"] = kwargs["project_description"]
        return TriageResult(
            included=tuple(
                TriageDecision(pr_number=pr.number, included=True, reason="user-facing")
                for pr in candidates
            )
        )

    def fake_generate(include, **kwargs):
        captured["generation_project"] = kwargs["project_description"]
        captured["category_guidance"] = kwargs["category_guidance"]
        captured["categories"] = kwargs["categories"]
        return GenerationResult(
            bullets=(
                CategorizedBullet(
                    pr_number=40, author="a", category="Other Changes", text="change a"
                ),
                CategorizedBullet(
                    pr_number=41, author="b", category="Configuration", text="change b"
                ),
            ),
            skipped=(),
        )

    monkeypatch.setattr(pipeline_mod.triage_mod, "triage", fake_triage)
    monkeypatch.setattr(pipeline_mod.generate_mod, "generate", fake_generate)

    result = pipeline_mod.regenerate_unreleased(
        object(), clone, head_ref="9.1", tag_glob=None, profile=profile
    )

    assert captured == {
        "triage_project": profile.triage_prompt_project,
        "generation_project": profile.generation_prompt_project,
        "category_guidance": profile.category_guidance,
        "categories": profile.categories,
    }
    assert list(result.grouped) == ["Configuration", "Other Changes"]


def test_contributor_authors_exclude_bots_and_unresolved_backports(
    monkeypatch, clone
):
    prs = (
        MergedPR(number=40, title="source", author="original-author", url="u",
                 labels=("release-notes",)),
        MergedPR(number=50, title="unresolved backport", author="backport-operator",
                 url="u", labels=("release-notes",)),
        MergedPR(number=60, title="bot PR", author="automation[bot]", url="u",
                 labels=("release-notes",)),
    )
    _patch(
        monkeypatch, prs=prs,
        unresolved_backports=(
            UnresolvedBackport(number=50, title="unresolved backport"),
        ),
    )

    result = pipeline_mod.regenerate_unreleased(
        object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE
    )

    assert result.pr_authors == ("original-author",)


def test_undecided_candidate_surfaced_as_triage(monkeypatch, clone):
    # A label-less PR AI triage returns no verdict for falls back to human triage.
    prs = (MergedPR(number=50, title="untagged", author="z", url="u", labels=()),)
    _patch(monkeypatch, prs=prs)  # default stub leaves all candidates undecided
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
    assert r.included == 0 and r.bullet_count == 0
    assert [(p.number, p.reason) for p in r.ai_excluded] == [(51, "internal refactor")]
    assert r.ai_included == () and r.triage == ()


def test_guardrail_included_candidate_is_separate_from_ai_included(monkeypatch, clone):
    prs = (MergedPR(
        number=52,
        title="Reject crafted payload that can crash the server",
        author="z",
        url="u",
        labels=(),
    ),)
    _patch(
        monkeypatch,
        prs=prs,
        bullets=(
            CategorizedBullet(
                pr_number=52, author="z", category="Bug Fixes", text="Reject the payload"
            ),
        ),
        triage_result=TriageResult(included=(
            TriageDecision(
                pr_number=52,
                included=True,
                reason="release-safety guardrail (server crash; AI exclusion)",
                uncertain=True,
                guardrail=True,
            ),
        )),
    )
    result = pipeline_mod.regenerate_unreleased(
        object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE
    )
    assert [pr.number for pr in result.guardrail_included] == [52]
    assert result.ai_included == ()
    assert [(impact.number, impact.reason) for impact in result.impact_review] == [
        (52, "server crash, assertion, or availability failure"),
    ]
    assert result.bullet_count == 1


def test_labelled_risky_pr_still_surfaces_for_urgency_review(monkeypatch, clone):
    prs = (MergedPR(
        number=53,
        title="Fix RESP3 protocol type violation",
        author="z",
        url="u",
        labels=("release-notes",),
    ),)
    _patch(
        monkeypatch,
        prs=prs,
        bullets=(
            CategorizedBullet(
                pr_number=53,
                author="z",
                category="Command and API Updates",
                text="Return the correct RESP3 type",
            ),
        ),
    )
    result = pipeline_mod.regenerate_unreleased(
        object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE
    )
    assert [impact.number for impact in result.impact_review] == [53]
    assert result.guardrail_included == ()


def test_labelled_pr_bypasses_triage(monkeypatch, clone):
    # A release-notes-labelled PR is included directly and never appears in any
    # AI-triage bucket, even though the stub would leave a candidate undecided.
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs,
           bullets=(CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix"),))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
    assert r.included == 1 and r.bullet_count == 1
    assert r.ai_included == () and r.ai_excluded == () and r.triage == ()


def test_diff_collector_sees_hard_excluded_peers(monkeypatch, clone):
    # A no-release-notes source can share a combined sweep SHA with an included
    # source. It is not sent to either AI stage, but the collector still needs to
    # see it so the combined patch is not misattributed to the included PR.
    prs = (
        MergedPR(
            number=40,
            title="included",
            author="a",
            url="u",
            labels=("release-notes",),
            merge_commit_sha="shared",
        ),
        MergedPR(
            number=41,
            title="excluded",
            author="b",
            url="u",
            labels=("no-release-notes",),
            merge_commit_sha="shared",
        ),
    )
    _patch(
        monkeypatch,
        prs=prs,
        bullets=(
            CategorizedBullet(
                pr_number=40,
                author="a",
                category="Bug Fixes",
                text="fix",
            ),
        ),
    )
    seen = []
    real_collector = pipeline_mod.PRDiffCollector

    def recording_collector(repo_dir, all_prs):
        seen.extend(pr.number for pr in all_prs)
        return real_collector(repo_dir, all_prs)

    monkeypatch.setattr(pipeline_mod, "PRDiffCollector", recording_collector)

    pipeline_mod.regenerate_unreleased(
        object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE
    )

    assert seen == [40, 41]


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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
    assert [c.number for c in r.unresolved_cherry_picks] == [80]


def test_no_usable_bullets_yields_empty_grouped(monkeypatch, clone):
    # Included PRs but generate produces nothing: bullet_count is 0 and grouped is
    # empty, which is what the cut's blank-cut guard (included and not bullet_count)
    # keys on to refuse the cut.
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs, bullets=(), skipped=(40,))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
    assert r.bullet_count == 1
    assert [(n.pr_number, n.category, n.reason) for n in r.uncertain] == [
        (40, "Bug Fixes", "could be Behavior Changes")
    ]


def test_confident_bullets_produce_no_uncertain_notes(monkeypatch, clone):
    prs = (MergedPR(number=40, title="t", author="a", url="u", labels=("release-notes",)),)
    _patch(monkeypatch, prs=prs, bullets=(
        CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix"),
    ))
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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
    r = pipeline_mod.regenerate_unreleased(object(), clone, head_ref="9.1", tag_glob=None, profile=projects.VALKEY_PROFILE)
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


class TestPriorWordingAlignment:
    """The wording-alignment seam between generation and rendering.

    A real local "remote" with two release branches stands in for the cut's own
    clone, because the seam's whole premise is that the sibling lines' published
    notes are already on disk (``git clone`` fetches every branch) and no extra
    network call is needed.
    """

    _PUBLISHED = "Fix a memory leak in ZDIFF when the result set becomes empty"

    @pytest.fixture
    def clone(self, tmp_path):
        from scripts.common.proc import run_git

        remote = str(tmp_path / "remote")
        os.makedirs(remote)
        run_git(remote, "init", "-q", "--initial-branch", "8.1")
        run_git(remote, "config", "user.email", "t@e")
        run_git(remote, "config", "user.name", "t")
        heading = "Valkey 8.1.9  -  Released Tue 21 July 2026"
        (tmp_path / "remote" / "00-RELEASENOTES").write_text(
            "\n".join([
                heading, "-" * len(heading), "",
                "Upgrade urgency LOW.", "",
                "### Bug Fixes",
                f"* {self._PUBLISHED} by @a (#40)",
                "",
            ]),
            encoding="utf-8",
        )
        run_git(remote, "add", "00-RELEASENOTES")
        run_git(remote, "commit", "-q", "-m", "8.1 notes")
        run_git(remote, "checkout", "-q", "-b", "9.0", "8.1")
        run_git(remote, "commit", "-q", "--allow-empty", "-m", "9.0")

        clone = str(tmp_path / "clone")
        run_git(None, "clone", "-q", "--branch", "9.0", remote, clone)
        # The fixture data files the cut reads live alongside the git metadata.
        shutil.copytree(_FIXTURE_CLONE, clone, dirs_exist_ok=True)
        return clone

    def _regen(self, monkeypatch, clone, *, bullets=None, discovery_kwargs=None, **kwargs):
        prs = (MergedPR(number=40, title="Fix ZDIFF leak", author="a", url="u",
                        labels=("release-notes",)),)
        monkeypatch.setattr(
            pipeline_mod.discover_mod, "discover",
            lambda *a, **k: DiscoveryResult(
                base_tag="9.0.0", head_ref="9.0", prs=prs, **(discovery_kwargs or {})
            ),
        )
        monkeypatch.setattr(
            pipeline_mod.generate_mod, "generate",
            lambda *a, **k: GenerationResult(
                bullets=bullets or (
                    CategorizedBullet(
                        pr_number=40, author="a", category="Bug Fixes",
                        text="Fix leak in ZDIFF",
                    ),
                ),
                skipped=(),
            ),
        )
        monkeypatch.setattr(
            pipeline_mod.triage_mod, "triage",
            lambda candidates, **k: TriageResult(),
        )
        return pipeline_mod.regenerate_unreleased(
            object(), clone, head_ref="9.0", tag_glob=None,
            release_branch="9.0", profile=projects.VALKEY_PROFILE, **kwargs,
        )

    def test_published_wording_replaces_generated_wording(self, monkeypatch, clone):
        r = self._regen(monkeypatch, clone)

        assert r.grouped["Bug Fixes"] == [f"* {self._PUBLISHED} by @a (#40)"]
        assert r.prior_lines_read == ("8.1",)
        assert [n.pr_number for n in r.aligned] == [40]
        assert r.aligned[0].release_line == "8.1"
        assert r.aligned[0].text == self._PUBLISHED
        # The generated wording is kept for the PR body so a reviewer can compare.
        assert r.aligned[0].generated_text == "Fix leak in ZDIFF"

    def test_the_line_being_cut_is_not_consulted(self, monkeypatch, clone):
        # 9.0 carries the same file (it branched from 8.1), so a seam that failed
        # to exclude the line under cut would report "carried from 9.0".
        r = self._regen(monkeypatch, clone)
        assert "9.0" not in r.prior_lines_read

    def test_flag_off_keeps_this_line_independent(self, monkeypatch, clone):
        r = self._regen(monkeypatch, clone, align_prior_wording=False)

        assert r.grouped["Bug Fixes"] == ["* Fix leak in ZDIFF by @a (#40)"]
        assert r.aligned == ()
        assert r.prior_lines_read == ()
        assert r.prior_lines_failed == ()

    def test_matching_wording_reported_as_already_consistent(self, monkeypatch, clone):
        r = self._regen(monkeypatch, clone, bullets=(
            CategorizedBullet(
                pr_number=40, author="a", category="Bug Fixes", text=self._PUBLISHED,
            ),
        ))
        assert r.consistent_alignments == (40,)
        assert r.aligned == ()

    def test_scope_disagreement_declines_and_is_marked_for_review(self, monkeypatch, clone):
        # The seam has to hand align_wording the note-side scope detector, because
        # the guardrail that normally polices scope claims reads the *original*
        # PR's title/body -- byte-identical on every line -- so only comparing the
        # two wordings can catch a backport that was adapted rather than
        # cherry-picked. Here 8.1 published no environment limit while this cut's
        # note limits the fix to 32-bit builds: carrying either text over the
        # other would publish a claim one line's diff does not support, so the
        # carry is refused and a human is asked which line is right.
        r = self._regen(monkeypatch, clone, bullets=(
            CategorizedBullet(
                pr_number=40, author="a", category="Bug Fixes",
                text="Fix a memory leak in ZDIFF on 32-bit builds",
            ),
        ))
        assert r.grouped["Bug Fixes"] == [
            "* Fix a memory leak in ZDIFF on 32-bit builds by @a (#40)"
        ]
        assert r.aligned == ()
        assert [d.pr_number for d in r.declined_alignments] == [40]
        assert r.declined_alignments[0].needs_review is True
        assert "32-bit" in r.declined_alignments[0].reason

    def test_carried_note_keeps_its_uncertainty(self, monkeypatch, clone):
        # The model's flag judged the change, not the prose. Carrying wording from
        # another line does not answer "is this user-facing?", so the note must
        # still reach the reviewer.
        r = self._regen(monkeypatch, clone, bullets=(
            CategorizedBullet(
                pr_number=40, author="a", category="Bug Fixes", text="Fix leak",
                uncertain=True, uncertain_reason="unclear whether user-facing",
            ),
        ))
        assert [n.pr_number for n in r.aligned] == [40]
        assert [(n.pr_number, n.reason) for n in r.uncertain] == [
            (40, "unclear whether user-facing"),
        ]

    def test_unconfirmed_credit_declines_without_holding(self, monkeypatch, clone):
        r = self._regen(
            monkeypatch, clone,
            discovery_kwargs={"unresolved_backports": (
                UnresolvedBackport(number=40, title="Fix ZDIFF leak", url="u"),
            )},
        )
        assert r.grouped["Bug Fixes"] == ["* Fix leak in ZDIFF by @a (#40)"]
        assert [d.pr_number for d in r.declined_alignments] == [40]
        assert not r.declined_alignments[0].needs_review

    def test_alignment_failure_degrades_to_generated_wording(self, monkeypatch, clone):
        def boom(*_a, **_k):
            raise RuntimeError("index build failed")

        monkeypatch.setattr(pipeline_mod.prior_notes_mod, "build_index", boom)
        r = self._regen(monkeypatch, clone)

        assert r.grouped["Bug Fixes"] == ["* Fix leak in ZDIFF by @a (#40)"]
        assert r.aligned == ()
        assert r.prior_lines_read == ()

    def test_reserved_category_bullet_is_not_reported_as_aligned(self, monkeypatch, clone):
        # group_bullets drops a reserved-category bullet, so the note never
        # renders; reporting a carry for it would describe text nobody can see.
        r = self._regen(monkeypatch, clone, bullets=(
            CategorizedBullet(
                pr_number=40, author="a", category="Security Fixes", text="Fix leak",
            ),
        ))
        assert r.aligned == ()
        assert 40 in r.skipped
