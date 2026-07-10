"""Tests for the release-cut orchestration.

Branch resolution and the version/notes promotion are exercised against the
release-format primitives (:mod:`scripts.release_notes.release_format`,
:mod:`version_bump`, :mod:`contributors`); the fixture clone supplies only data
files, and git and GitHub are mocked.
"""

from __future__ import annotations

import os
import shutil

import pytest

from scripts.release_notes import release_cut as rc
from scripts.release_notes.release_cut import (
    BranchPlan,
    commit_title,
    promote_and_bump,
    resolve_branch_plan,
    stage_release_name,
)

_FIXTURE_CLONE = os.path.join(os.path.dirname(__file__), "fixtures", "valkey_clone")


@pytest.fixture
def clone(tmp_path):
    dest = tmp_path / "clone"
    shutil.copytree(_FIXTURE_CLONE, dest)
    return str(dest)


class TestNormalizeStage:
    """Stage is case-insensitive: RC1/GA/Rc2 normalize to rc1/ga/rc2.

    _normalize_stage is the single normalization choke point feeding plan.stage;
    commit_title/stage_release_name/branch names all consume that normalized
    value (they do not lowercase their own argument), so this guards the whole
    cut against a maintainer dispatching an uppercase stage.
    """

    def test_uppercase_rc_lowercased(self) -> None:
        assert rc._normalize_stage("RC1") == "rc1"
        assert rc._normalize_stage("Rc2") == "rc2"
        assert rc._normalize_stage("RC10") == "rc10"

    def test_uppercase_ga_lowercased(self) -> None:
        assert rc._normalize_stage("GA") == "ga"
        assert rc._normalize_stage("Ga") == "ga"

    def test_surrounding_whitespace_stripped(self) -> None:
        assert rc._normalize_stage("  RC1  ") == "rc1"

    def test_invalid_mixed_case_still_rejected(self) -> None:
        with pytest.raises(ValueError):
            rc._normalize_stage("Beta")


class TestStageHelpers:
    def test_release_name_rc(self) -> None:
        assert stage_release_name("9.1.0", "rc2") == "9.1.0-rc2"

    def test_release_name_ga(self) -> None:
        assert stage_release_name("9.1.0", "ga") == "9.1.0"

    def test_commit_title_rc(self) -> None:
        assert commit_title("9.1.0", "rc2") == "Update version to 9.1.0-rc2 and add release notes"

    def test_commit_title_ga(self) -> None:
        assert commit_title("9.1.0", "ga") == "Add release notes entry for Valkey 9.1.0 GA"


class TestResolveBranchPlan:
    def _exists(self, monkeypatch, present):
        monkeypatch.setattr(rc, "_remote_branch_exists",
                            lambda repo_dir, branch: branch in present)

    def test_rc1_creates_pre_release(self, monkeypatch) -> None:
        self._exists(monkeypatch, set())
        plan = resolve_branch_plan("/d", version="9.1.0", stage="rc1", source_ref="unstable")
        assert plan == BranchPlan("rc1", "pre-release-9.1.0", "unstable", False, None)

    def test_uppercase_rc1_normalizes_and_routes_to_pre_release(self, monkeypatch) -> None:
        # A maintainer dispatching "RC1" must resolve exactly like "rc1": the
        # plan's stage is the lowercased value, so every downstream name is too.
        self._exists(monkeypatch, set())
        plan = resolve_branch_plan("/d", version="9.1.0", stage="RC1", source_ref="unstable")
        assert plan == BranchPlan("rc1", "pre-release-9.1.0", "unstable", False, None)

    def test_uppercase_ga_normalizes_and_fires_rename(self, monkeypatch) -> None:
        # "GA" must route to the M.m line and fire the pre-release rename, not be
        # compared raw (which would skip the ga branch and cut onto the rc line).
        self._exists(monkeypatch, {"pre-release-9.1.0"})
        plan = resolve_branch_plan("/d", version="9.1.0", stage="GA", source_ref="unstable")
        assert plan.stage == "ga"
        assert plan.target == "9.1"
        assert plan.rename_from == "pre-release-9.1.0"

    def test_rcN_continues_pre_release(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"pre-release-9.1.0"})
        # Avoid the sequence-warning fetch by stubbing it.
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)
        plan = resolve_branch_plan("/d", version="9.1.0", stage="rc2", source_ref="unstable")
        assert plan.target == "pre-release-9.1.0"
        assert plan.base_ref == "pre-release-9.1.0"
        assert plan.continuing is True
        assert plan.rename_from is None

    def test_ga_renames_pre_release(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"pre-release-9.1.0"})
        plan = resolve_branch_plan("/d", version="9.1.0", stage="ga", source_ref="unstable")
        assert plan.target == "9.1"
        assert plan.base_ref == "pre-release-9.1.0"
        assert plan.continuing is True
        assert plan.rename_from == "pre-release-9.1.0"

    def test_ga_continues_existing_minor(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"9.1"})
        plan = resolve_branch_plan("/d", version="9.1.1", stage="ga", source_ref="unstable")
        assert plan.target == "9.1"
        assert plan.rename_from is None

    def test_ga_first_release_from_source(self, monkeypatch) -> None:
        self._exists(monkeypatch, set())
        plan = resolve_branch_plan("/d", version="9.1.0", stage="ga", source_ref="unstable")
        assert plan.target == "9.1"
        assert plan.base_ref == "unstable"
        assert plan.continuing is False

    def test_rc1_first_cut_has_no_warning(self, monkeypatch) -> None:
        self._exists(monkeypatch, set())
        plan = resolve_branch_plan("/d", version="9.1.0", stage="rc1", source_ref="unstable")
        assert plan.rc_warning is None

    def test_rcN_first_cut_warns_no_prior_line(self, monkeypatch) -> None:
        # rc2 dispatched but pre-release-9.1.0 does not exist yet: rc1 was skipped.
        self._exists(monkeypatch, set())
        plan = resolve_branch_plan("/d", version="9.1.0", stage="rc2", source_ref="unstable")
        assert plan.target == "pre-release-9.1.0"
        assert plan.continuing is False
        assert plan.rc_warning is not None
        assert "rc1" in plan.rc_warning
        assert "does not exist" in plan.rc_warning

    def test_rcN_continuation_carries_sequence_warning(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"pre-release-9.1.0"})
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: "out-of-seq detail")
        plan = resolve_branch_plan("/d", version="9.1.0", stage="rc3", source_ref="unstable")
        assert plan.rc_warning == "out-of-seq detail"

    def test_ga_both_branches_raises(self, monkeypatch) -> None:
        # pre-release-9.1.0 AND 9.1 both present is an inconsistent state: refuse
        # rather than orphan the pre-release line / drop its rc history.
        self._exists(monkeypatch, {"pre-release-9.1.0", "9.1"})
        with pytest.raises(ValueError, match="inconsistent state"):
            resolve_branch_plan("/d", version="9.1.0", stage="ga", source_ref="unstable")

    def test_ga_continuation_carries_branch_warning(self, monkeypatch) -> None:
        # GA continues an existing 9.1; the continuation warning (dup heading /
        # lingering pre-release) is plumbed onto branch_warning.
        self._exists(monkeypatch, {"9.1"})
        monkeypatch.setattr(rc, "_warn_ga_continuation", lambda *a, **k: "dup heading detail")
        plan = resolve_branch_plan("/d", version="9.1.1", stage="ga", source_ref="unstable")
        assert plan.target == "9.1"
        assert plan.rename_from is None
        assert plan.branch_warning == "dup heading detail"

    def test_rc_after_ga_warns_and_suppresses_first_cut(self, monkeypatch) -> None:
        # 9.1 exists (already went GA) but pre-release-9.1.0 was deleted by the
        # rename. A further rc recreates it; warn on branch_warning, and the rc1
        # first-cut wording must not also fire.
        self._exists(monkeypatch, {"9.1"})
        plan = resolve_branch_plan("/d", version="9.1.0", stage="rc1", source_ref="unstable")
        assert plan.target == "pre-release-9.1.0"
        assert plan.continuing is False
        assert plan.branch_warning is not None
        assert "9.1" in plan.branch_warning
        assert plan.rc_warning is None

    def test_bad_stage_raises(self, monkeypatch) -> None:
        self._exists(monkeypatch, set())
        with pytest.raises(ValueError):
            resolve_branch_plan("/d", version="9.1.0", stage="beta", source_ref="unstable")

    def test_bad_version_raises(self, monkeypatch) -> None:
        self._exists(monkeypatch, set())
        with pytest.raises(ValueError):
            resolve_branch_plan("/d", version="9.1", stage="rc1", source_ref="unstable")


class TestDeleteRemoteBranch:
    """The GA-rename branch delete must not report success when it fails.

    A failed delete that leaves pre-release-M.m.p on origin alongside the M.m
    line is exactly the state the next GA of that line hard-refuses, so it must
    surface as a non-zero signal rather than being swallowed.
    """

    def test_delete_success(self, monkeypatch) -> None:
        calls = []
        monkeypatch.setattr(rc, "run_git", lambda *a, **k: calls.append(a) or None)
        # _remote_branch_exists must not even be consulted on success.
        monkeypatch.setattr(rc, "_remote_branch_exists",
                            lambda *a, **k: pytest.fail("should not check on success"))
        rc._delete_remote_branch("/d", "pre-release-9.1.0", {})
        assert any("--delete" in c for c in calls)

    def test_delete_failure_but_branch_gone_is_tolerated(self, monkeypatch) -> None:
        # Push failed, yet the branch is confirmed absent -> desired end state, no raise.
        def _boom(*a, **k):
            raise RuntimeError("push rejected")
        monkeypatch.setattr(rc, "run_git", _boom)
        monkeypatch.setattr(rc, "_remote_branch_exists", lambda *a, **k: False)
        rc._delete_remote_branch("/d", "pre-release-9.1.0", {})  # no exception

    def test_delete_failure_branch_still_present_raises(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("protected ref")
        monkeypatch.setattr(rc, "run_git", _boom)
        monkeypatch.setattr(rc, "_remote_branch_exists", lambda *a, **k: True)
        with pytest.raises(RuntimeError) as exc:
            rc._delete_remote_branch("/d", "pre-release-9.1.0", {})
        assert "pre-release-9.1.0" in str(exc.value)

    def test_delete_failure_existence_check_also_fails_raises(self, monkeypatch) -> None:
        # If we cannot even confirm the branch is gone, assume the worst and raise.
        def _boom(*a, **k):
            raise RuntimeError("push rejected")
        def _boom_exists(*a, **k):
            raise RuntimeError("ls-remote failed")
        monkeypatch.setattr(rc, "run_git", _boom)
        monkeypatch.setattr(rc, "_remote_branch_exists", _boom_exists)
        with pytest.raises(RuntimeError):
            rc._delete_remote_branch("/d", "pre-release-9.1.0", {})


class TestRollbackCreatedLine:
    """The rollback of a run-created release line is lease-guarded on the OID the
    line was created at, so a line another writer advanced is never blindly
    deleted."""

    _OID = "a" * 40

    def test_deletes_with_lease_on_created_oid(self, monkeypatch) -> None:
        # Normal rollback: the line still points at the OID we created it at, so
        # the delete goes through, pinned to that OID via --force-with-lease.
        calls = []
        monkeypatch.setattr(rc, "run_git", lambda *a, **k: calls.append(a) or None)
        rc._rollback_created_line("/d", "9.1", self._OID, {})
        assert (
            "/d", "push", f"--force-with-lease=refs/heads/9.1:{self._OID}",
            "origin", "--delete", "9.1",
        ) in calls, calls

    def test_empty_oid_refuses_to_delete(self, monkeypatch) -> None:
        # The OID could not be read at create time: refuse to delete blind (a
        # stranded line is recoverable; a wrongly deleted commit is not).
        monkeypatch.setattr(rc, "run_git",
                            lambda *a, **k: pytest.fail("must not push when OID unknown"))
        rc._rollback_created_line("/d", "9.1", "", {})  # no raise, no push

    def test_stale_lease_leaves_branch_intact(self, monkeypatch, caplog) -> None:
        # The line advanced past the OID we created it at (another writer): the
        # lease rejects the delete. The branch is still present, so we leave it
        # intact and warn rather than force it away.
        def _boom(*a, **k):
            raise RuntimeError("stale info")
        monkeypatch.setattr(rc, "run_git", _boom)
        monkeypatch.setattr(rc, "_remote_branch_exists", lambda *a, **k: True)
        with caplog.at_level("WARNING"):
            rc._rollback_created_line("/d", "9.1", self._OID, {})  # no raise
        assert any("no longer points at the commit" in r.message for r in caplog.records)

    def test_already_gone_is_a_noop(self, monkeypatch) -> None:
        # The delete failed but the branch is confirmed absent (someone else
        # removed it): the desired end state, so no raise and no warning-worthy
        # inconsistency.
        def _boom(*a, **k):
            raise RuntimeError("remote ref does not exist")
        monkeypatch.setattr(rc, "run_git", _boom)
        monkeypatch.setattr(rc, "_remote_branch_exists", lambda *a, **k: False)
        rc._rollback_created_line("/d", "9.1", self._OID, {})  # no raise


class TestRcSequenceWarning:
    """The out-of-sequence rc detection feeding BranchPlan.rc_warning."""

    def _stub_notes(self, monkeypatch, notes):
        # _warn_rc_sequence fetches the branch then reads 00-RELEASENOTES from it.
        monkeypatch.setattr(rc, "run_git", lambda *a, **k: None)
        monkeypatch.setattr(rc, "git_output", lambda *a, **k: notes)

    def _warn(self, monkeypatch, stage_lc, notes):
        self._stub_notes(monkeypatch, notes)
        return rc._warn_rc_sequence("/d", "pre-release-9.1.0", stage_lc, 9, 1, 0)

    def test_in_sequence_returns_none(self, monkeypatch) -> None:
        # Line records up to rc1; rc2 is exactly next.
        assert self._warn(monkeypatch, "rc2", "Valkey 9.1.0-rc1 (2026-06-01)\n") is None

    def test_first_rc_on_empty_line_in_sequence(self, monkeypatch) -> None:
        # No dated rc heading yet; rc1 is expected.
        assert self._warn(monkeypatch, "rc1", "no dated headings here") is None

    def test_repeat_rc_warns(self, monkeypatch) -> None:
        # Line already records rc1 and rc2; re-cutting rc2 is a repeat.
        msg = self._warn(monkeypatch, "rc2",
                         "Valkey 9.1.0-rc2 (2026-06-08)\nValkey 9.1.0-rc1 (2026-06-01)\n")
        assert msg is not None
        assert "re-cuts" in msg
        assert "rc3" in msg  # next expected

    def test_gap_rc_warns(self, monkeypatch) -> None:
        # Line records up to rc1; jumping to rc4 skips rc2/rc3.
        msg = self._warn(monkeypatch, "rc4", "Valkey 9.1.0-rc1 (2026-06-01)\n")
        assert msg is not None
        assert "skips ahead" in msg
        assert "rc2" in msg  # next expected

    def test_only_matches_this_versions_headings(self, monkeypatch) -> None:
        # A different patch line's rc headings must not be counted.
        msg = self._warn(monkeypatch, "rc1", "Valkey 9.1.1-rc5 (2026-06-01)\n")
        assert msg is None  # 9.1.0 has no rc yet, so rc1 is in sequence

    def test_unreadable_notes_returns_none(self, monkeypatch) -> None:
        def _boom(*a, **k):
            raise RuntimeError("no such branch")
        monkeypatch.setattr(rc, "run_git", _boom)
        assert rc._warn_rc_sequence("/d", "pre-release-9.1.0", "rc2", 9, 1, 0) is None

    def test_first_cut_rc1_no_warning(self) -> None:
        assert rc._warn_rc_first_cut("rc1", "pre-release-9.1.0") is None

    def test_first_cut_rcN_warns(self) -> None:
        msg = rc._warn_rc_first_cut("rc2", "pre-release-9.1.0")
        assert msg is not None
        assert "rc1" in msg
        assert "pre-release-9.1.0" in msg

    def test_first_cut_ga_no_warning(self) -> None:
        # Non-rc stages never go through this helper's warning.
        assert rc._warn_rc_first_cut("ga", "pre-release-9.1.0") is None


class TestCanonicalVersion:
    """The single version-normalization choke point."""

    def test_strips_trailing_space(self) -> None:
        assert rc.canonical_version("9.1.0 ") == "9.1.0"

    def test_drops_leading_zeros(self) -> None:
        # version.h / headings / branch names must all agree on the canonical form.
        assert rc.canonical_version("09.1.0") == "9.1.0"
        assert rc.canonical_version("9.01.00") == "9.1.0"

    def test_already_canonical_unchanged(self) -> None:
        assert rc.canonical_version("9.1.0") == "9.1.0"

    @pytest.mark.parametrize("bad", ["9.1", "v9.1.0", "9.1.0-rc1", "nope", ""])
    def test_malformed_raises(self, bad) -> None:
        with pytest.raises(ValueError):
            rc.canonical_version(bad)

    @pytest.mark.parametrize("bad", ["9.256.0", "256.0.0", "9.1.256"])
    def test_component_over_255_raises(self, bad) -> None:
        with pytest.raises(ValueError, match="out of range 0-255"):
            rc.canonical_version(bad)


class TestGaAndRcAfterGaWarnings:
    """Branch-model warnings for GA continuation and rc-after-GA."""

    def _stub(self, monkeypatch, *, pre_exists, ga_notes):
        monkeypatch.setattr(rc, "_remote_branch_exists",
                            lambda repo_dir, branch: pre_exists if branch.startswith("pre-release") else True)
        monkeypatch.setattr(rc, "run_git", lambda *a, **k: None)
        monkeypatch.setattr(rc, "git_output", lambda *a, **k: ga_notes)

    def test_ga_continuation_warns_on_lingering_pre_release(self, monkeypatch) -> None:
        self._stub(monkeypatch, pre_exists=True, ga_notes="")
        msg = rc._warn_ga_continuation("/d", "9.1", "pre-release-9.1.1", "9.1.1")
        assert msg is not None
        assert "pre-release-9.1.1" in msg
        assert "NOT be carried" in msg

    def test_ga_continuation_warns_on_duplicate_heading(self, monkeypatch) -> None:
        notes = "Valkey 9.1 release notes\n====\n\nValkey 9.1.0 GA  -  Released 2026-06-30\n----\n"
        self._stub(monkeypatch, pre_exists=False, ga_notes=notes)
        msg = rc._warn_ga_continuation("/d", "9.1", "pre-release-9.1.0", "9.1.0")
        assert msg is not None
        assert "SECOND dated heading" in msg

    def test_ga_continuation_clean_returns_none(self, monkeypatch) -> None:
        # No lingering pre-release, no prior same-version GA heading: normal patch.
        notes = "Valkey 9.1 release notes\n====\n\nValkey 9.1.0 GA  -  Released 2026-06-30\n----\n"
        self._stub(monkeypatch, pre_exists=False, ga_notes=notes)
        assert rc._warn_ga_continuation("/d", "9.1", "pre-release-9.1.1", "9.1.1") is None

    def test_rc_after_ga_warns_when_ga_exists(self, monkeypatch) -> None:
        monkeypatch.setattr(rc, "_remote_branch_exists", lambda repo_dir, branch: branch == "9.1")
        msg = rc._warn_rc_after_ga("/d", "9.1", "pre-release-9.1.0", "9.1.0")
        assert msg is not None
        assert "9.1" in msg and "recreates" in msg

    def test_rc_after_ga_none_when_no_ga(self, monkeypatch) -> None:
        monkeypatch.setattr(rc, "_remote_branch_exists", lambda repo_dir, branch: False)
        assert rc._warn_rc_after_ga("/d", "9.1", "pre-release-9.1.0", "9.1.0") is None


class TestSecurityHelpers:
    """--security-fix sanitization and duplicate-listing detection."""

    def test_sanitize_drops_empty_and_collapses_newlines(self) -> None:
        out = rc._sanitize_security_fixes(["  ", "fix\nmulti (#7)", ""])
        assert out == ["fix multi (#7)"]

    def test_sanitize_all_empty_returns_none(self) -> None:
        assert rc._sanitize_security_fixes(["", "   "]) is None
        assert rc._sanitize_security_fixes(None) is None

    def test_dup_prs_intersects_noted(self) -> None:
        dup = rc._security_dup_prs(["CVE fix (#7)", "other (#9)"], {7, 8})
        assert dup == [7]

    def test_dup_prs_empty_without_overlap(self) -> None:
        assert rc._security_dup_prs(["CVE fix (#7)"], {8, 9}) == []
        assert rc._security_dup_prs(None, {7}) == []


class TestTrailingPrRegex:
    """The dedup regex must tolerate hand-edited trailing punctuation."""

    @pytest.mark.parametrize("line,expected", [
        ("* x by @a (#44)", {44}),
        ("* x by @a (#44).", {44}),
        ("* x by @a (#44):", {44}),
        ("* x by @a (#44) ", {44}),
        # A trailing run of refs credits only the last (the merge PR); render never
        # emits this, but a hand-edited line might.
        ("* x by @a (#44)(#45)", {45}),
    ])
    def test_credited_tolerates_trailing_punctuation(self, line, expected) -> None:
        assert rc._credited_pr_numbers(line) == expected

    def test_security_section_pr_refs_not_credited(self) -> None:
        # A CVE summary ending in "(#500)" is prose, not a PR credit. It must not
        # seed the dedup set, or a later cut would drop an unrelated real PR #500.
        # A normal bullet's (#44) in the same file is still credited.
        notes = (
            "Valkey 9.1.0-rc1  -  Released Tue 24 June 2026\n"
            "-----\n\n"
            "### Security Fixes\n"
            "* (CVE-2026-23479) Use-after-free in unblock client flow (#500)\n\n"
            "### Bug Fixes\n"
            "* Fix a thing by @a (#44)\n"
        )
        assert rc._credited_pr_numbers(notes) == {44}

    def test_new_dated_section_ends_security_scope(self) -> None:
        # After the Security Fixes section, a later dated section's normal bullets
        # are credited again (the section flag resets on the next "## "/"### ").
        notes = (
            "### Security Fixes\n"
            "* (CVE-2026-1) something (#500)\n\n"
            "## Valkey 9.1 release notes\n"
            "### Bug Fixes\n"
            "* real note (#77)\n"
        )
        assert rc._credited_pr_numbers(notes) == {77}


class TestPromoteAndBump:
    def _grouped_with_bullet(self, clone):
        from scripts.release_notes import render as render_mod
        fmt = render_mod.load_format_module(clone)
        from scripts.release_notes.models import CategorizedBullet
        return render_mod.group_bullets(
            [CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix a crash")],
            fmt)

    def test_promotes_dated_section_and_bumps_version(self, clone, monkeypatch) -> None:
        # No contributor base -> skip the network lookup entirely.
        grouped = self._grouped_with_bullet(clone)
        version_text = open(os.path.join(clone, "src", "version.h"), encoding="utf-8").read()
        new_notes, new_version = promote_and_bump(
            clone,
            grouped=grouped,
            dest_notes_text="",          # first cut: no prior changelog
            dest_version_text=version_text,
            version="9.1.0", stage_lc="rc1", urgency="LOW", date="2026-06-25",
            repo_full_name="valkey-io/valkey", contrib_base=None, token=None,
            security_fixes=None,
        )
        # Dated section rendered, bullet included, never an unreleased block.
        assert "Valkey 9.1.0-rc1" in new_notes
        assert "* fix a crash by @a (#40)" in new_notes
        assert "## Unreleased" not in new_notes
        # version.h macros bumped.
        assert '#define VALKEY_VERSION "9.1.0"' in new_version
        assert "#define VALKEY_VERSION_NUM 0x00090100" in new_version
        assert '#define VALKEY_RELEASE_STAGE "rc1"' in new_version

    def test_drains_prior_rc_notes(self, clone) -> None:
        # A prior rc1 dated section on the destination must survive into rc2.
        grouped = self._grouped_with_bullet(clone)
        prior = (
            "Valkey 9.1 release notes\n========================\n\n"
            "Valkey 9.1.0-rc1  -  Released 2026-06-01\n"
            "---------------------------------------\n\n"
            "Upgrade urgency LOW: ...\n\n### Bug Fixes\n* earlier fix by @x (#1)\n"
        )
        version_text = open(os.path.join(clone, "src", "version.h"), encoding="utf-8").read()
        new_notes, _ = promote_and_bump(
            clone, grouped=grouped, dest_notes_text=prior,
            dest_version_text=version_text, version="9.1.0", stage_lc="rc2",
            urgency="LOW", date="2026-06-25", repo_full_name="valkey-io/valkey",
            contrib_base=None, token=None, security_fixes=None,
        )
        assert "Valkey 9.1.0-rc2" in new_notes
        assert "Valkey 9.1.0-rc1" in new_notes      # prior rc retained
        assert "* earlier fix by @x (#1)" in new_notes
        assert "* fix a crash by @a (#40)" in new_notes

    def test_contributor_list_included(self, clone, monkeypatch) -> None:
        grouped = self._grouped_with_bullet(clone)
        version_text = open(os.path.join(clone, "src", "version.h"), encoding="utf-8").read()
        # Stub the contributor lookup so no network is touched.
        monkeypatch.setattr(
            rc.gc, "list_contributors", lambda *a, **k: ["Jane Doe @jane", "Bob @bob"]
        )
        new_notes, _ = promote_and_bump(
            clone, grouped=grouped, dest_notes_text="",
            dest_version_text=version_text, version="9.1.0", stage_lc="rc1",
            urgency="LOW", date="2026-06-25", repo_full_name="valkey-io/valkey",
            contrib_base="9.0.0", token=None, security_fixes=None,
        )
        assert "### Contributors" in new_notes
        assert "Jane Doe @jane" in new_notes

    def test_contributor_refs_resolved_for_compare_api(self, clone, monkeypatch) -> None:
        # Regression: the contributor base is a remote-tracking ref
        # (origin/unstable) and the head is the literal "HEAD". Both resolve for
        # git but 404 the GitHub compare API, which silently drops to the
        # names-only git-shortlog fallback. promote_and_bump must dereference both
        # to SHAs (via _compare_ref) before calling list_contributors, so the API
        # path, and thus the "Full Name @handle" format, is preserved.
        grouped = self._grouped_with_bullet(clone)
        version_text = open(os.path.join(clone, "src", "version.h"), encoding="utf-8").read()
        captured: dict = {}

        def _list(repo, base, head, token, *, repo_dir=None):
            captured["base"] = base
            captured["head"] = head
            return ["Jane Doe @jane"]

        monkeypatch.setattr(rc.gc, "list_contributors", _list)
        # Stub ref resolution so no real git repo is needed: prove the values
        # passed to list_contributors are what _compare_ref returned, not the
        # raw origin/unstable / HEAD refs.
        monkeypatch.setattr(rc, "_compare_ref",
                            lambda repo_dir, ref: {"origin/unstable": "base_sha", "HEAD": "head_sha"}[ref])
        promote_and_bump(
            clone, grouped=grouped, dest_notes_text="",
            dest_version_text=version_text, version="9.1.0", stage_lc="rc1",
            urgency="LOW", date="2026-06-25", repo_full_name="valkey-io/valkey",
            contrib_base="origin/unstable", token="t", security_fixes=None,
        )
        assert captured["base"] == "base_sha"
        assert captured["head"] == "head_sha"  # never the literal "HEAD"

    def test_compare_ref_dereferences_to_sha(self, tmp_path) -> None:
        # _compare_ref turns a branch name into the commit SHA the compare API
        # wants; an unresolvable ref falls back to the ref as given.
        from scripts.common.proc import git_output, run_git
        repo = str(tmp_path / "r")
        os.makedirs(repo)
        run_git(repo, "init", "-q")
        run_git(repo, "config", "user.email", "t@e")
        run_git(repo, "config", "user.name", "t")
        (tmp_path / "r" / "f").write_text("x")
        run_git(repo, "add", "f")
        run_git(repo, "commit", "-q", "-m", "c")
        sha = git_output(repo, "rev-parse", "HEAD").strip()
        assert rc._compare_ref(repo, "HEAD") == sha
        assert rc._compare_ref(repo, "no-such-ref") == "no-such-ref"  # graceful fallback


class TestCutOrchestration:
    """End-to-end cut() with git + GitHub + pipeline mocked, real fixture worktree."""

    def _setup(self, monkeypatch, clone, *, line_exists, bullets=True, triage=(),
               had_prs=True, duplicate_prs=(), uncertain=(), unresolved=(),
               unresolved_backports=(), stub_contrib_base=True, writes=None):
        from scripts.release_notes import pipeline as pipeline_mod
        from scripts.release_notes import render as render_mod
        from scripts.release_notes.models import CategorizedBullet
        from scripts.release_notes.pipeline import RegenResult

        fmt = render_mod.load_format_module(clone)
        bl = ([CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix")]
              if bullets else [])
        grouped = render_mod.group_bullets(bl, fmt)
        monkeypatch.setattr(
            pipeline_mod, "regenerate_unreleased",
            lambda *a, **k: RegenResult(
                base_tag="9.0.0", grouped=grouped,
                included=1 if bullets else 0,
                bullet_count=sum(len(v) for v in grouped.values()), skipped=(),
                triage=tuple(triage), had_prs=had_prs,
                duplicate_prs=tuple(duplicate_prs), uncertain=tuple(uncertain),
                unresolved=tuple(unresolved),
                unresolved_backports=tuple(unresolved_backports)),
        )
        # Record git commands; emulate worktree by copying the clone tree on add
        # and actually removing it on remove, so cut()'s cleanup is exercised (a
        # no-op remove would leave .release-dest behind and mask a cleanup leak).
        calls = []

        def _fake_git(repo_dir, *args, **kwargs):
            calls.append(args)
            if args[:1] == ("worktree",) and args[1] == "add":
                dest = args[-2]
                shutil.copytree(clone, dest, dirs_exist_ok=True)
            elif args[:1] == ("worktree",) and args[1] == "remove":
                shutil.rmtree(args[-1], ignore_errors=True)
            from unittest.mock import MagicMock
            return MagicMock()

        monkeypatch.setattr(rc, "run_git", _fake_git)
        # cut() reads the OID it creates the release line at (git rev-parse
        # origin/<base>^{commit}) so a rollback can lease-guard its delete. The
        # fixture clone has no such ref, so stub it to a deterministic OID.
        monkeypatch.setattr(rc, "git_output", lambda *a, **k: "a" * 40)
        # Capture every _write(path, text) so a test can assert on the notes cut()
        # actually produces, rather than reading a post-cleanup filesystem path
        # that only survives if the worktree-remove was stubbed to a no-op.
        if writes is not None:
            real_write = rc._write

            def _spy_write(path, text):
                writes[path] = text
                return real_write(path, text)

            monkeypatch.setattr(rc, "_write", _spy_write)
        monkeypatch.setattr(rc, "_remote_branch_exists", lambda d, b: line_exists.get(b, False))
        if stub_contrib_base:
            monkeypatch.setattr(rc, "_contrib_base", lambda *a, **k: None)
        return calls

    def test_rc1_creates_line_and_prs_prep_branch_into_it(self, monkeypatch, clone):
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={})
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []

        def _create_pull(**kw):
            created.append(kw)
            return MagicMock(number=len(created), html_url=f"https://x/{len(created)}")

        repo.create_pull.side_effect = _create_pull
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        # The release line was created (it did not exist), and exactly one PR is
        # opened: the prep branch into the release line. No companion reset PR;
        # the source branch is never modified.
        pushed = [c for c in calls if c[:1] == ("push",)]
        assert any("refs/heads/pre-release-9.1.0" in " ".join(c) for c in pushed), pushed
        assert len(created) == 1
        assert created[0]["head"].startswith("agent/release-cut/")
        assert created[0]["base"] == "pre-release-9.1.0"
        # The source branch is never pushed to.
        assert not any("HEAD:unstable" in " ".join(c) or ":refs/heads/unstable" in " ".join(c)
                       for c in pushed)
        # rc1 is not a rename (plan.rename_from is None), so no branch is deleted.
        assert not any("--delete" in c for c in pushed), pushed

    def test_included_prs_but_no_bullets_aborts_without_pr(self, monkeypatch, clone):
        # The cut()-level guard: PRs were included but generation produced no
        # renderable bullets. cut() must return 1 and open no PR / push nothing,
        # rather than commit empty notes. Override _setup's RegenResult with a
        # guard-tripping one (included=1, bullet_count=0, empty grouped).
        from unittest.mock import MagicMock

        from scripts.release_notes import pipeline as pipeline_mod
        from scripts.release_notes.pipeline import RegenResult

        calls = self._setup(monkeypatch, clone, line_exists={})
        monkeypatch.setattr(
            pipeline_mod, "regenerate_unreleased",
            lambda *a, **k: RegenResult(
                base_tag="9.0.0", grouped={},
                included=1, bullet_count=0, skipped=(40,), triage=(), had_prs=True,
                duplicate_prs=()),
        )
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []
        repo.create_pull.side_effect = lambda **kw: created.append(kw) or MagicMock(number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc_code = rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        assert rc_code == 1                     # aborted
        assert created == []                    # no PR opened
        assert not [c for c in calls if c[:1] == ("push",)]  # nothing pushed

    def test_uppercase_ga_cuts_minor_line_with_lowercased_names(self, monkeypatch, clone):
        # End-to-end: a dispatch of "GA" must route to the M.m line and emit the
        # GA-titled commit, with the prep branch lowercased. commit_title and
        # stage_release_name do not normalize their own argument, so this proves
        # resolve_branch_plan's normalization holds across the whole cut() path.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True})
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []

        def _create_pull(**kw):
            created.append(kw)
            return MagicMock(number=1, html_url="https://x/1")

        repo.create_pull.side_effect = _create_pull
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="GA",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        assert created[0]["base"] == "9.1"                          # M.m line, not pre-release
        assert created[0]["head"] == "agent/release-cut/9.1.0-ga"   # prep branch lowercased
        assert created[0]["title"] == "Add release notes entry for Valkey 9.1.0 GA"
        # No raw "-GA" leaks into the prep-branch push refspec.
        assert not any("9.1.0-GA" in " ".join(c) for c in calls if c[:1] == ("push",))
        # The GA rename must delete the old pre-release branch on origin (destructive):
        # leaving it alongside the new 9.1 line is the inconsistent state the next GA
        # hard-refuses, so assert the delete fired and targeted exactly that branch.
        assert ("push", "origin", "--delete", "pre-release-9.1.0") in calls

    def test_triage_listed_in_release_pr_body(self, monkeypatch, clone):
        from unittest.mock import MagicMock

        from scripts.release_notes.models import MergedPR
        triage = (MergedPR(number=7, title="Untagged | thing", author="bob", url="https://x/7"),)
        calls = self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True}, triage=triage)
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []

        def _create_pull(**kw):
            created.append(kw)
            return MagicMock(number=1, html_url="https://x/1")

        repo.create_pull.side_effect = _create_pull
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        body = created[0]["body"]
        assert "Needs triage" in body
        assert "[#7](https://x/7)" in body
        assert "Untagged \\| thing" in body  # pipe escaped for the table

    def test_unresolved_commits_listed_in_release_pr_body(self, monkeypatch, clone):
        # A range commit that resolved to no PR must surface in the PR body so a
        # shipped-but-un-noted change is visible, not silently dropped.
        from unittest.mock import MagicMock

        from scripts.release_notes.models import UnresolvedCommit
        unresolved = (
            UnresolvedCommit(sha="abcdef1234567890", subject="rewritten pick | thing"),
        )
        self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True},
                    unresolved=unresolved)
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []

        def _create_pull(**kw):
            created.append(kw)
            return MagicMock(number=1, html_url="https://x/1")

        repo.create_pull.side_effect = _create_pull
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        body = created[0]["body"]
        assert "Commits with no resolvable PR" in body
        assert "abcdef123456" in body  # sha truncated to 12 in the table
        assert "rewritten pick \\| thing" in body  # pipe escaped for the table

    def test_unresolved_backports_listed_in_release_pr_body(self, monkeypatch, clone):
        # A note credited to a backport whose original PR could not be recovered
        # must surface in the PR body so a reviewer can correct the credit; a log
        # line alone is too easy to miss for a normal-looking note.
        from unittest.mock import MagicMock

        from scripts.release_notes.models import UnresolvedBackport
        unresolved_backports = (
            UnresolvedBackport(number=512, title="[Backport 9.1] port fix | thing",
                               url="https://github.com/valkey-io/valkey/pull/512"),
        )
        self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True},
                    unresolved_backports=unresolved_backports)
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []

        def _create_pull(**kw):
            created.append(kw)
            return MagicMock(number=1, html_url="https://x/1")

        repo.create_pull.side_effect = _create_pull
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        body = created[0]["body"]
        assert "credited to a backport" in body
        assert "[#512](https://github.com/valkey-io/valkey/pull/512)" in body  # linked
        assert "port fix \\| thing" in body  # pipe escaped for the table

    def test_rc_out_of_sequence_warned_in_pr_body(self, monkeypatch, clone):
        # rc2 dispatched with no pre-release line yet: the first-cut warning must
        # surface in the release PR body for the reviewer.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={})
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []

        def _create_pull(**kw):
            created.append(kw)
            return MagicMock(number=1, html_url="https://x/1")

        repo.create_pull.side_effect = _create_pull
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        body = created[0]["body"]
        assert "Release candidate out of sequence" in body
        assert "rc1" in body

    def test_in_sequence_rc_has_no_warning_in_pr_body(self, monkeypatch, clone):
        # rc1 first cut is in sequence: no warning section in the body.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={})
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []

        def _create_pull(**kw):
            created.append(kw)
            return MagicMock(number=1, html_url="https://x/1")

        repo.create_pull.side_effect = _create_pull
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        assert "out of sequence" not in created[0]["body"]

    def _cut_body(self, monkeypatch, clone, *, line_exists, cut_kwargs,
                  bullets=True, triage=(), had_prs=True, duplicate_prs=(), uncertain=()):
        """Run cut() with GitHub mocked and return the created PR's body."""
        from unittest.mock import MagicMock
        self._setup(monkeypatch, clone, line_exists=line_exists, bullets=bullets,
                    triage=triage, had_prs=had_prs, duplicate_prs=duplicate_prs,
                    uncertain=uncertain)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []
        repo.create_pull.side_effect = lambda **kw: created.append(kw) or MagicMock(
            number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())
        base = dict(
            repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None,
            contrib_base_ref=None, security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        base.update(cut_kwargs)
        rc.cut(repo, **base)
        return created[0]["body"] if created else None

    def test_body_always_shows_resolved_range(self, monkeypatch, clone):
        body = self._cut_body(monkeypatch, clone, line_exists={}, cut_kwargs={})
        assert "computed over `9.0.0..HEAD`" in body

    def test_rc_after_ga_warned_in_body(self, monkeypatch, clone):
        # 9.1 exists; rc1 of 9.1.0 recreates a deleted pre-release line.
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={})
        assert "Release line state looks off" in body
        assert "already exists as a GA line" in body

    def test_baseline_unanchored_warned_in_body(self, monkeypatch, clone):
        body = self._cut_body(monkeypatch, clone, line_exists={},
                              cut_kwargs={"version": "9.0.0", "baseline_unanchored": True})
        assert "baseline is unanchored" in body

    def test_empty_range_explained_in_body(self, monkeypatch, clone):
        body = self._cut_body(monkeypatch, clone, line_exists={},
                              cut_kwargs={}, bullets=False, had_prs=False)
        assert "Empty release notes" in body
        assert "No merged PRs were found" in body

    def test_all_triage_empty_notes_explained_in_body(self, monkeypatch, clone):
        from scripts.release_notes.models import MergedPR
        triage = (MergedPR(number=7, title="thing", author="bob", url="https://x/7"),)
        body = self._cut_body(monkeypatch, clone, line_exists={}, cut_kwargs={},
                              bullets=False, had_prs=True, triage=triage)
        assert "Empty release notes" in body
        assert "unlabelled or double-labelled" in body
        assert "Needs triage" in body  # the table is still rendered

    def test_duplicate_pr_warned_in_body(self, monkeypatch, clone):
        body = self._cut_body(monkeypatch, clone, line_exists={}, cut_kwargs={},
                              duplicate_prs=(40,))
        assert "noted more than once" in body
        assert "#40" in body

    def test_uncertain_notes_flagged_in_body(self, monkeypatch, clone):
        from scripts.release_notes.models import UncertainNote
        body = self._cut_body(
            monkeypatch, clone, line_exists={}, cut_kwargs={},
            uncertain=(UncertainNote(pr_number=40, category="Other Changes",
                                     reason="unclear if user-facing"),),
        )
        assert "Notes to double-check" in body
        assert "#40" in body
        assert "Other Changes" in body
        assert "unclear if user-facing" in body

    def test_security_dup_warned_in_body(self, monkeypatch, clone):
        # The fixture bullet credits #40; a --security-fix naming #40 lists it twice.
        body = self._cut_body(monkeypatch, clone, line_exists={},
                              cut_kwargs={"security_fixes": ["Fix CVE (#40)"]})
        assert "Security fixes need a look" in body
        assert "#40" in body

    def test_security_urgency_without_fixes_warned_in_body(self, monkeypatch, clone):
        body = self._cut_body(monkeypatch, clone, line_exists={},
                              cut_kwargs={"urgency": "SECURITY", "security_fixes": None})
        assert "Security fixes need a look" in body
        assert "no security content" in body

    def _advisory_repo(self, monkeypatch, clone, *, advisories):
        """A cut() harness whose repo returns *advisories* from the GHSA API.

        Returns ``(repo, created, writes, calls)``: ``created`` accumulates each
        opened PR's kwargs, ``writes`` maps each written path to the text cut()
        wrote (so a test asserts on what the cut *produces and commits*, not on a
        post-cleanup filesystem path), and ``calls`` records the git commands (to
        assert cleanup fired). Uses the real ``security_mod`` path (only
        git/publish are mocked), so the advisory fetch, version match, and merge
        run end to end.
        """
        from unittest.mock import MagicMock
        writes: dict[str, str] = {}
        calls = self._setup(monkeypatch, clone, line_exists={}, writes=writes)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.get_repository_advisories.return_value = advisories
        created = []
        repo.create_pull.side_effect = lambda **kw: created.append(kw) or MagicMock(
            number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())
        return repo, created, writes, calls

    @staticmethod
    def _written_notes(writes: dict, clone: str) -> str:
        """Return the 00-RELEASENOTES text cut() wrote to the dest worktree.

        Keyed on the ``.release-dest`` worktree path, not just the filename: the
        notes must be written into the throwaway worktree that becomes the PR
        diff, never back into the source clone. A placement regression that wrote
        to ``clone/00-RELEASENOTES`` would leave the dest path unwritten and fail
        here rather than silently pass.
        """
        dest_notes = os.path.join(clone, ".release-dest", rc.NOTES_FILE)
        assert dest_notes in writes, (
            f"cut() wrote no {rc.NOTES_FILE} to the dest worktree; wrote to {list(writes)}"
        )
        return writes[dest_notes]

    @staticmethod
    def _assert_worktree_removed(calls: list, clone: str) -> None:
        """Assert cut() cleaned up its .release-dest worktree."""
        dest = os.path.join(clone, ".release-dest")
        removed = [c for c in calls if c[:2] == ("worktree", "remove") and dest in c]
        assert removed, f"cut() did not remove the worktree; git calls={calls}"
        assert not os.path.exists(dest), ".release-dest should be gone after cut()"

    def test_advisory_cve_rendered_into_notes(self, monkeypatch, clone):
        # A published advisory patched in 9.1.0 lands as a Security Fixes bullet in
        # the release-branch notes, in the maintainer's "(CVE-...) summary" form.
        from tests.test_release_notes_security import _advisory, _vuln
        adv = _advisory(cve_id="CVE-2026-23479", ghsa_id="GHSA-a",
                        summary="Use-After-Free in unblock client flow",
                        vulnerabilities=[_vuln(patched="9.1.0")])
        repo, created, writes, calls = self._advisory_repo(monkeypatch, clone, advisories=[adv])
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="SECURITY", date="2026-06-25", tag_glob=None, base_ref=None,
            contrib_base_ref=None, security_fixes=None, token="t", git_env={},
            dry_run=False, security_from_advisories=True,
        )
        # Assert on the notes cut() actually wrote to the dest worktree, not a
        # path left behind by a stubbed cleanup.
        notes = self._written_notes(writes, clone)
        assert "### Security Fixes" in notes
        assert "* (CVE-2026-23479) Use-After-Free in unblock client flow" in notes
        # SECURITY urgency now HAS content, so the "no security content" warning is gone.
        assert "no security content" not in created[0]["body"]
        # The matched-advisory body header names what was auto-rendered.
        assert "Security fixes (auto-generated from advisories)" in created[0]["body"]
        assert "Rendered 1 published advisory fix" in created[0]["body"]
        assert "CVE-2026-23479" in created[0]["body"]
        # The disclaimer to add embargoed CVEs is present.
        assert "embargoed or draft CVEs" in created[0]["body"]
        # cut() cleaned up its throwaway worktree (no leak).
        self._assert_worktree_removed(calls, clone)

    def test_advisory_fetch_failure_disclaimed_in_body(self, monkeypatch, clone):
        repo, created, _writes, _calls = self._advisory_repo(monkeypatch, clone, advisories=None)
        repo.get_repository_advisories.side_effect = RuntimeError("no advisory permission")
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None,
            contrib_base_ref=None, security_fixes=None, token="t", git_env={},
            dry_run=False, security_from_advisories=True,
        )
        body = created[0]["body"]
        assert "Security advisories could not be read" in body
        assert "no advisory permission" in body

    def test_unreadable_advisory_flagged_not_reported_as_non_match(self, monkeypatch, clone):
        # An advisory whose raw_data can't be read is surfaced in the body as
        # "could not be read ... MAY fix this version", NOT silently as a non-match.
        from tests.test_release_notes_security import _advisory, _vuln
        adv = _advisory(cve_id="CVE-2026-9", ghsa_id="GHSA-z", summary="s",
                        raise_on={"raw_data"}, vulnerabilities=[_vuln(patched="9.1.0")])
        repo, created, _writes, _calls = self._advisory_repo(monkeypatch, clone, advisories=[adv])
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None,
            contrib_base_ref=None, security_fixes=None, token="t", git_env={},
            dry_run=False, security_from_advisories=True,
        )
        body = created[0]["body"]
        assert "could **not** be read" in body
        assert "CVE-2026-9" in body
        assert "MAY fix this version" in body

    def test_manual_security_fix_wins_over_advisory(self, monkeypatch, clone):
        # An advisory and a --security-fix both name CVE-2026-23479: the manual
        # wording is what ships, and the CVE is listed once.
        from tests.test_release_notes_security import _advisory, _vuln
        adv = _advisory(cve_id="CVE-2026-23479", ghsa_id="GHSA-a",
                        summary="auto-generated wording",
                        vulnerabilities=[_vuln(patched="9.1.0")])
        repo, created, writes, calls = self._advisory_repo(monkeypatch, clone, advisories=[adv])
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="SECURITY", date="2026-06-25", tag_glob=None, base_ref=None,
            contrib_base_ref=None,
            security_fixes=["CVE-2026-23479: hand-written wording"],
            token="t", git_env={}, dry_run=False, security_from_advisories=True,
        )
        notes = self._written_notes(writes, clone)
        assert "hand-written wording" in notes
        assert "auto-generated wording" not in notes
        self._assert_worktree_removed(calls, clone)

    def test_clean_cut_has_no_warning_sections(self, monkeypatch, clone):
        body = self._cut_body(monkeypatch, clone, line_exists={}, cut_kwargs={})
        assert "⚠️" not in body
        assert "Empty release notes" not in body

    def test_existing_line_not_recreated(self, monkeypatch, clone):
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True})
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = MagicMock(number=2, html_url="https://x/2")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        # No create-line push (branch already exists); only the prep-branch push.
        line_create = [c for c in calls
                       if c[:1] == ("push",) and "refs/heads/pre-release-9.1.0" in " ".join(c)]
        assert line_create == []

    def test_recut_fetches_prep_branch_before_force_with_lease(self, monkeypatch, clone):
        # A re-cut of the same stage finds the agent-namespaced prep branch already
        # on the remote. The fresh clone never fetched it, so --force-with-lease has
        # no basis and would reject with "stale info". Assert the prep branch is
        # fetched (populating the tracking ref) immediately before the lease push.
        from unittest.mock import MagicMock
        prep = "agent/release-cut/9.1.0-rc2"
        calls = self._setup(
            monkeypatch, clone,
            line_exists={"pre-release-9.1.0": True, prep: True},
        )
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = MagicMock(number=2, html_url="https://x/2")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        refspec = f"+refs/heads/{prep}:refs/remotes/origin/{prep}"
        fetch_idx = next(
            (i for i, c in enumerate(calls)
             if c[:2] == ("fetch", "origin") and refspec in c),
            None,
        )
        assert fetch_idx is not None, calls
        lease_idx = next(
            i for i, c in enumerate(calls)
            if c[:2] == ("push", "--force-with-lease") and f"HEAD:{prep}" in c
        )
        # The fetch must precede the lease push so the tracking ref is current.
        assert fetch_idx < lease_idx, (fetch_idx, lease_idx)

    def test_first_cut_skips_prep_fetch(self, monkeypatch, clone):
        # On a first cut the prep branch is absent, so there is no tracking ref to
        # refresh; the push creates it. No prep-branch fetch should be issued.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={})
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = MagicMock(number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        prep_fetch = [c for c in calls
                      if c[:2] == ("fetch", "origin") and "refs/remotes/origin/agent/release-cut" in " ".join(c)]
        assert prep_fetch == [], prep_fetch

    def test_dry_run_pushes_nothing(self, monkeypatch, clone):
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={})
        repo = MagicMock()
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=True,
        )
        assert [c for c in calls if c[:1] == ("push",)] == []
        repo.create_pull.assert_not_called()

    def test_contrib_base_matches_notes_baseline(self, monkeypatch, clone):
        # The credits must span the same range as the bullets: the contributor
        # base passed to promote_and_bump equals regen.base_tag (9.0.0 here),
        # not whatever `git describe` would return from the source branch. _setup
        # leaves the real _contrib_base in place here so the wiring is exercised;
        # promote_and_bump is captured to read what it received.
        from unittest.mock import MagicMock
        self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True},
                    stub_contrib_base=False)
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)

        captured = {}

        def _promote(valkey_clone_dir, **kw):
            captured["contrib_base"] = kw["contrib_base"]
            return "NOTES", "VERSION"

        monkeypatch.setattr(rc, "promote_and_bump", _promote)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = MagicMock(number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        # regen.base_tag is 9.0.0; the real _contrib_base must return it (via the
        # notes_base_ref branch), never reaching git describe.
        assert captured["contrib_base"] == "9.0.0"

    def test_failed_pr_rolls_back_a_run_created_line(self, monkeypatch, clone):
        # The release line is created (step 4) before the prep branch + PR are
        # known-good (step 5). A GA rename where 9.1 does not yet exist creates it,
        # then must delete pre-release-9.1.0. If step 5 raises, the freshly created
        # 9.1 must be rolled back, or 9.1 and pre-release-9.1.0 both sit on
        # origin, the inconsistent state the next GA hard-refuses.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True})

        def _boom(*a, **k):
            raise RuntimeError("prep push / create_pull failed")

        monkeypatch.setattr(rc, "_commit_push_release_pr", _boom)
        repo = MagicMock()

        with pytest.raises(RuntimeError, match="prep push"):
            rc.cut(
                repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
                valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="ga",
                urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
                security_fixes=None, token="t", git_env={}, dry_run=False,
            )
        # 9.1 was created this run, so the failure rolls it back, with a lease
        # pinned to the OID it was created at so a concurrently-advanced line is
        # never blindly deleted.
        assert (
            "push", "--force-with-lease=refs/heads/9.1:" + "a" * 40, "origin", "--delete", "9.1"
        ) in calls, calls
        # The GA-rename delete of the pre-release branch is never reached (step 6),
        # so pre-release-9.1.0 is left intact for the retry.
        assert not any(
            c[:1] == ("push",) and c[-1] == "pre-release-9.1.0" and "--delete" in c
            for c in calls
        ), calls
        self._assert_worktree_removed(calls, clone)

    def test_failed_pr_leaves_a_preexisting_line_untouched(self, monkeypatch, clone):
        # A continued cut (the line already exists) never created the line, so a
        # step-5 failure must NOT delete it: that would destroy a line carrying
        # prior RCs' history. No rollback delete should fire.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True})
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)

        def _boom(*a, **k):
            raise RuntimeError("prep push / create_pull failed")

        monkeypatch.setattr(rc, "_commit_push_release_pr", _boom)
        repo = MagicMock()

        with pytest.raises(RuntimeError, match="prep push"):
            rc.cut(
                repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
                valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc2",
                urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
                security_fixes=None, token="t", git_env={}, dry_run=False,
            )
        # The line pre-existed, so nothing is deleted (neither a plain nor a
        # lease-guarded delete push fires).
        assert not [c for c in calls if c[:1] == ("push",) and "--delete" in c], calls
        self._assert_worktree_removed(calls, clone)


class TestContribBase:
    def test_explicit_wins(self, monkeypatch) -> None:
        # Explicit --contrib-base-ref beats even the notes baseline.
        assert rc._contrib_base("/d", explicit="9.0.0", notes_base_ref="9.0.1") == "9.0.0"

    def test_notes_base_ref_used_before_describe(self, monkeypatch) -> None:
        # The fix: the notes baseline anchors contributors, ahead of git describe.
        # describe would (wrongly) return an older nearest tag, but must not be hit.
        def _git(d, *a):
            raise AssertionError(f"git should not run when notes_base_ref is set: {a}")
        monkeypatch.setattr(rc, "git_output", _git)
        assert rc._contrib_base("/d", explicit=None, notes_base_ref="9.0.0") == "9.0.0"

    def test_falls_back_to_last_tag_when_no_baseline(self, monkeypatch) -> None:
        # rc2+/ga path: notes baseline is a tag passed through, but if None we
        # still resolve via describe.
        monkeypatch.setattr(rc, "git_output",
                            lambda d, *a: "9.0.5\n" if a[0] == "describe" else "")
        assert rc._contrib_base("/d", explicit=None, notes_base_ref=None) == "9.0.5"

    def test_falls_back_to_root_commit(self, monkeypatch) -> None:
        def _git(d, *a):
            if a[0] == "describe":
                raise RuntimeError("no tags")
            if a[0] == "rev-list":
                return "rootsha\n"
            return ""
        monkeypatch.setattr(rc, "git_output", _git)
        assert rc._contrib_base("/d", explicit=None, notes_base_ref=None) == "rootsha"


class TestDedupAgainstDestination:
    """The tag-independent dedup: drop PRs the release line already credits.

    Without an RC tag to bound the range (the agent never pushes tags; a fork has
    none), discovery re-finds every PR on a continued cut, most visibly GA after
    the final RC. These cover the dedup that keeps promotion idempotent anyway.
    """

    _GA_PLAN = BranchPlan("ga", "9.1", "pre-release-9.1.0", True, "pre-release-9.1.0")

    @staticmethod
    def _meta(already_credited, noted_bullet_count):
        # The section reads only these two fields; the rest are placeholders.
        return rc._NotesMeta(
            regen=None, already_credited=already_credited,
            noted_bullet_count=noted_bullet_count, urgency="LOW",
            security_fixes=None, security_dup_prs=(), baseline_unanchored=False,
        )

    def test_no_new_prs_section_renders_when_all_credited(self) -> None:
        # Every PR in range was already credited on the line, so the dated section
        # is version-bump-only; the body must say so (not read as a generation miss).
        section = rc._no_new_prs_section(self._meta([44, 45], 0), self._GA_PLAN)
        assert "No new release notes" in section
        assert "#44" in section and "#45" in section
        assert "9.1" in section  # names the target line

    def test_no_new_prs_section_empty_when_nothing_dropped(self) -> None:
        assert rc._no_new_prs_section(self._meta([], 0), self._GA_PLAN) == ""

    def test_no_new_prs_section_silent_when_a_new_note_survives(self) -> None:
        # Regression (PR #58): a duplicate PR was dropped (#44) but another PR still
        # produced a bullet, so the dated section carries real content. The section
        # must stay silent rather than falsely claim "No new release notes".
        assert rc._no_new_prs_section(self._meta([44], 1), self._GA_PLAN) == ""

    def test_credited_reads_trailing_pr_refs(self) -> None:
        text = (
            "Valkey 9.1.0-rc1 - Released\n\n"
            "### Bug Fixes\n"
            "* fix a thing by @a (#44)\n"
            "* and another by @b (#51)\n"
        )
        assert rc._credited_pr_numbers(text) == {44, 51}

    def test_credited_ignores_non_bullet_and_inline_refs(self) -> None:
        # A "(#N)" in prose or a heading is not a credit; only a trailing ref on
        # a bullet line is. Mirrors the guidance comment that mentions "(#N)".
        text = (
            "See PR (#999) for context.\n"
            "## Heading mentioning (#998)\n"
            "* real credit by @a (#44)\n"
            "* a bullet with a mid-line (#7) ref but no trailing one\n"
        )
        assert rc._credited_pr_numbers(text) == {44}

    def test_drop_removes_only_overlapping_bullets(self) -> None:
        grouped = {
            "Performance and Efficiency Improvements": ["* already shipped by @a (#44)"],
            "Bug Fixes": ["* clearly new by @b (#60)", "* also new by @c (#61)"],
        }
        filtered, dropped = rc._drop_already_credited(grouped, {44})
        assert dropped == [44]
        all_lines = [line for lines in filtered.values() for line in lines]
        assert not any("(#44)" in line for line in all_lines)
        assert any("(#60)" in line for line in all_lines)   # new PRs survive
        assert any("(#61)" in line for line in all_lines)
        # The category emptied by the drop is removed; the one with survivors stays.
        assert "Performance and Efficiency Improvements" not in filtered
        assert filtered["Bug Fixes"] == ["* clearly new by @b (#60)", "* also new by @c (#61)"]

    def test_drop_is_noop_without_overlap(self) -> None:
        grouped = {"Bug Fixes": ["* new by @a (#60)"]}
        filtered, dropped = rc._drop_already_credited(grouped, set())
        assert dropped == []
        assert filtered == grouped

    def test_ga_after_final_rc_drops_all_and_warns(self, clone, monkeypatch) -> None:
        # End-to-end-ish: dest already credits #44; the source block re-found #44
        # (no tag to bound the range). The cut must drop it, render an empty dated
        # section, and warn in the PR body.
        from scripts.release_notes import pipeline as pipeline_mod
        from scripts.release_notes import render as render_mod
        from scripts.release_notes.models import CategorizedBullet
        from scripts.release_notes.pipeline import RegenResult

        fmt = render_mod.load_format_module(clone)
        bl = [CategorizedBullet(pr_number=44, author="a", category="Bug Fixes", text="fix")]
        grouped = render_mod.group_bullets(bl, fmt)
        monkeypatch.setattr(
            pipeline_mod, "regenerate_unreleased",
            lambda *a, **k: RegenResult(
                base_tag="unstable", grouped=grouped,
                included=1, bullet_count=1, skipped=(), triage=(), had_prs=True,
            ),
        )
        # Destination line already credits #44 (carried from rc1).
        dest_notes = (
            "Valkey 9.1 release notes\n========================\n\n"
            "Valkey 9.1.0-rc1  -  Released 2026-06-01\n"
            "---------------------------------------\n\n"
            "Upgrade urgency LOW: ...\n\n### Bug Fixes\n* fix by @a (#44)\n"
        )
        captured = {}

        # Drive cut() with git/GitHub/promote stubbed; assert the dedup + warning.
        from scripts.release_notes import release_cut as rcmod
        monkeypatch.setattr(rcmod, "resolve_branch_plan", lambda *a, **k: self._GA_PLAN)
        monkeypatch.setattr(rcmod, "_remote_branch_exists", lambda d, b: True)
        monkeypatch.setattr(rcmod, "run_git", lambda *a, **k: None)
        monkeypatch.setattr(rcmod, "_read",
                            lambda p: dest_notes if p.endswith("00-RELEASENOTES")
                            else open(os.path.join(clone, "src", "version.h")).read())

        def _capture_promote(*a, **k):
            captured["grouped"] = k["grouped"]
            return ("NEWNOTES", "NEWVERSION")
        monkeypatch.setattr(rcmod, "promote_and_bump", _capture_promote)
        monkeypatch.setattr(rcmod, "_print_dry_run",
                            lambda *a, **k: captured.setdefault("already", list(a[4].already_credited)))

        rcmod.cut(
            object(), repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0",
            stage="ga", urgency="LOW", date="2026-06-29", tag_glob=None,
            base_ref=None, contrib_base_ref=None, security_fixes=None,
            token="t", git_env={}, dry_run=True,
        )
        # #44 was dropped before render saw the grouped bullets.
        all_lines = [line for lines in captured["grouped"].values() for line in lines]
        assert not any("(#44)" in line for line in all_lines)
        assert captured["already"] == [44]


class TestContinuingCutBaseline:
    """A continuing cut (rc2+, GA drain) anchors discovery to the release line.

    The rc2+ default is a `<version>-rc*` tag glob, but this workflow never pushes
    RC tags and the fork carries none, so `git describe --match` would fail with
    "no tag reachable" and abort the cut. cut() must instead anchor discovery to
    plan.base_ref (the pre-release / M.m line) and drop the doomed glob, while
    leaving an explicit --base-ref and rc1 first cuts untouched.
    """

    _RC_CONTINUE_PLAN = BranchPlan("rc2", "pre-release-9.1.0", "pre-release-9.1.0", True, None)
    _RC_FIRST_PLAN = BranchPlan("rc1", "pre-release-9.1.0", "unstable", False, None)

    @staticmethod
    def _capture_regen(monkeypatch, captured):
        # Stub the whole cut() surface below discovery; record exactly what
        # regenerate_unreleased was handed, then abort the run so no worktree /
        # PR machinery has to be mocked.
        from scripts.release_notes import pipeline as pipeline_mod

        def _regen(repo, clone_dir, *, head_ref, tag_glob, base_ref):
            captured["head_ref"] = head_ref
            captured["tag_glob"] = tag_glob
            captured["base_ref"] = base_ref
            raise _StopCut

        monkeypatch.setattr(pipeline_mod, "regenerate_unreleased", _regen)

    def _run(self, monkeypatch, *, plan, base_ref, tag_glob):
        captured = {}
        self._capture_regen(monkeypatch, captured)
        monkeypatch.setattr(rc, "resolve_branch_plan", lambda *a, **k: plan)
        with pytest.raises(_StopCut):
            rc.cut(
                object(), repo_full_name="valkey-io/valkey", source_clone_dir="/d",
                valkey_clone_dir="/d", source_ref="unstable", version="9.1.0",
                stage=plan.stage, urgency="LOW", date="2026-06-29",
                tag_glob=tag_glob, base_ref=base_ref, contrib_base_ref=None,
                security_fixes=None, token="t", git_env={}, dry_run=True,
            )
        return captured

    def test_rc2_no_base_ref_anchors_to_line_and_drops_glob(self, monkeypatch) -> None:
        # The bug: rc2+ arrives with base_ref=None and tag_glob="9.1.0-rc*".
        # cut() must swap in plan.base_ref and clear the glob so discovery walks
        # pre-release-9.1.0..unstable instead of a describe --match that has no tag.
        captured = self._run(
            monkeypatch, plan=self._RC_CONTINUE_PLAN,
            base_ref=None, tag_glob="9.1.0-rc*",
        )
        assert captured["base_ref"] == "pre-release-9.1.0"
        assert captured["tag_glob"] is None

    def test_explicit_base_ref_on_continuing_cut_is_not_overridden(self, monkeypatch) -> None:
        # A maintainer-supplied --base-ref always wins, glob stays cleared as passed.
        captured = self._run(
            monkeypatch, plan=self._RC_CONTINUE_PLAN,
            base_ref="8.0.0", tag_glob=None,
        )
        assert captured["base_ref"] == "8.0.0"
        assert captured["tag_glob"] is None

    def test_rc1_first_cut_baseline_untouched(self, monkeypatch) -> None:
        # rc1 first cut does not continue a line (plan.continuing is False), so the
        # derived-base / nearest-tag inputs pass through verbatim.
        captured = self._run(
            monkeypatch, plan=self._RC_FIRST_PLAN,
            base_ref="9.0.0", tag_glob=None,
        )
        assert captured["base_ref"] == "9.0.0"
        assert captured["tag_glob"] is None

    def test_rc1_first_cut_with_glob_untouched(self, monkeypatch) -> None:
        # An M.0.0-style rc1 that fell back to a glob (no derivable base) also must
        # not be rewritten: plan.continuing is False, so the glob survives.
        captured = self._run(
            monkeypatch, plan=self._RC_FIRST_PLAN,
            base_ref=None, tag_glob="9.1.0-rc*",
        )
        assert captured["base_ref"] is None
        assert captured["tag_glob"] == "9.1.0-rc*"


class _StopCut(Exception):
    """Sentinel to abort cut() right after the discovery call in baseline tests."""
