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

    def test_expected_oid_leases_the_delete(self, monkeypatch) -> None:
        # With an expected_oid the delete is pinned to that commit via
        # --force-with-lease, so it cannot remove a branch a concurrent writer
        # advanced past the OID carried into the release line.
        calls = []
        monkeypatch.setattr(rc, "run_git", lambda *a, **k: calls.append(a) or None)
        monkeypatch.setattr(rc, "_remote_branch_exists",
                            lambda *a, **k: pytest.fail("should not check on success"))
        rc._delete_remote_branch("/d", "pre-release-9.1.0", {}, expected_oid="b" * 40)
        assert (
            "/d", "push", "--force-with-lease=refs/heads/pre-release-9.1.0:" + "b" * 40,
            "origin", "--delete", "pre-release-9.1.0",
        ) in calls, calls

    def test_lease_rejected_branch_advanced_raises_with_hint(self, monkeypatch) -> None:
        # The lease is rejected (the pre-release branch advanced past the OID
        # carried into M.m: a concurrent push after the rename branched). The
        # branch is still present, so deleting it would lose that commit; raise
        # with a reconcile hint rather than force it away.
        def _boom(*a, **k):
            raise RuntimeError("stale info")
        monkeypatch.setattr(rc, "run_git", _boom)
        monkeypatch.setattr(rc, "_remote_branch_exists", lambda *a, **k: True)
        with pytest.raises(RuntimeError) as exc:
            rc._delete_remote_branch("/d", "pre-release-9.1.0", {}, expected_oid="c" * 40)
        assert "advanced past" in str(exc.value)
        assert "would lose that commit" in str(exc.value)


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

    def test_security_fix_prs_in_notes_intersects_noted(self) -> None:
        found = rc._security_fix_prs_in_notes(["CVE fix (#7)", "other (#9)"], {7, 8, 9})
        assert found == [7, 9]  # sorted, deterministic

    def test_security_fix_prs_in_notes_empty_without_overlap(self) -> None:
        assert rc._security_fix_prs_in_notes(["CVE fix (#7)"], {8, 9}) == []
        assert rc._security_fix_prs_in_notes(None, {7}) == []


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


class TestUnresolvedBackportsSection:
    """The PR-body table flagging notes credited to a backport whose original PR was
    not recovered (recovery found none, or a recovered source failed validation)."""

    def test_empty_renders_nothing(self) -> None:
        assert rc._unresolved_backports_section(()) == ""

    def test_linked_and_unlinked_rows(self) -> None:
        from scripts.release_notes.models import UnresolvedBackport
        section = rc._unresolved_backports_section((
            UnresolvedBackport(number=512, title="[Backport 9.1] port fix | thing",
                               url="https://github.com/valkey-io/valkey/pull/512"),
            UnresolvedBackport(number=513, title="[Backport 9.0] other"),  # no url
        ))
        assert "credited to a backport" in section
        # URL present -> markdown link; the pipe in the title is escaped for the table.
        assert "[#512](https://github.com/valkey-io/valkey/pull/512)" in section
        assert "port fix \\| thing" in section
        # No URL -> bare #N reference.
        assert "| #513 |" in section


class TestUnresolvedCherryPicksSection:
    """The PR-body table flagging notes whose -x cherry-pick origin could not be confirmed."""

    def test_empty_renders_nothing(self) -> None:
        assert rc._unresolved_cherry_picks_section(()) == ""

    def test_rows_list_credited_pr_and_source_shas(self) -> None:
        from scripts.release_notes.models import UnresolvedCherryPick
        section = rc._unresolved_cherry_picks_section((
            UnresolvedCherryPick(
                number=80, sha="rangesha0123456789",
                source_shas=("deadbeefdeadbeef", "cafef00dcafef00d"),
                subject="port fix (#80)",
            ),
        ))
        assert "unconfirmed cherry-pick origin" in section
        assert "| #80 |" in section
        # Subject is rendered in the table for maintainer triage.
        assert "port fix (#80)" in section
        # SHAs are truncated to 12 chars and shown in code spans.
        assert "`rangesha0123`" in section
        assert "`deadbeefdead`" in section
        assert "`cafef00dcafe`" in section


class TestPromoteAndBump:
    def _grouped_with_bullet(self, clone):
        from scripts.release_notes import render as render_mod
        from scripts.release_notes.models import CategorizedBullet
        return render_mod.group_bullets(
            [CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix a crash")])

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
            repo_full_name="valkey-io/valkey", contrib_base=None,
            contrib_head="unstable", token=None,
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
            contrib_base=None, contrib_head="origin/pre-release-9.1.0",
            token=None, security_fixes=None,
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
            contrib_base="9.0.0", contrib_head="unstable", token=None,
            security_fixes=None,
        )
        assert "### Contributors" in new_notes
        assert "Jane Doe @jane" in new_notes

    def test_contributor_refs_resolved_for_compare_api(self, clone, monkeypatch) -> None:
        # Regression: the contributor base is a remote-tracking ref
        # (origin/unstable) and the head is a branch ref. Both resolve for git but
        # 404 the GitHub compare API, which silently drops to the names-only
        # git-shortlog fallback. promote_and_bump must dereference both to SHAs
        # (via _compare_ref) before calling list_contributors, so the API path,
        # and thus the "Full Name @handle" format, is preserved.
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
        # raw branch refs.
        monkeypatch.setattr(
            rc, "_compare_ref",
            lambda repo_dir, ref: {
                "origin/unstable": "base_sha", "origin/pre-release-9.1.0": "head_sha"
            }[ref],
        )
        promote_and_bump(
            clone, grouped=grouped, dest_notes_text="",
            dest_version_text=version_text, version="9.1.0", stage_lc="rc2",
            urgency="LOW", date="2026-06-25", repo_full_name="valkey-io/valkey",
            contrib_base="origin/unstable",
            contrib_head="origin/pre-release-9.1.0", token="t", security_fixes=None,
        )
        assert captured["base"] == "base_sha"
        assert captured["head"] == "head_sha"  # the resolved head SHA, not a raw ref

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
               unresolved_backports=(), unresolved_prs=(), stub_contrib_base=True,
               writes=None):
        from scripts.release_notes import pipeline as pipeline_mod
        from scripts.release_notes import render as render_mod
        from scripts.release_notes.models import CategorizedBullet
        from scripts.release_notes.pipeline import RegenResult

        bl = ([CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix")]
              if bullets else [])
        grouped = render_mod.group_bullets(bl)
        monkeypatch.setattr(
            pipeline_mod, "regenerate_unreleased",
            lambda *a, **k: RegenResult(
                base_tag="9.0.0", grouped=grouped,
                included=1 if bullets else 0,
                bullet_count=sum(len(v) for v in grouped.values()), skipped=(),
                triage=tuple(triage), had_prs=had_prs,
                duplicate_prs=tuple(duplicate_prs), uncertain=tuple(uncertain),
                unresolved=tuple(unresolved),
                unresolved_backports=tuple(unresolved_backports),
                unresolved_prs=tuple(unresolved_prs)),
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

    def _capture_discovery_range(self, monkeypatch, clone, *, line_exists, cut_kwargs):
        # Run cut() with regenerate_unreleased replaced by a spy that records the
        # (base_ref, tag_glob) discovery is asked to walk, so a test can assert the
        # range without a real git clone. Returns that captured pair.
        from unittest.mock import MagicMock

        from scripts.release_notes import pipeline as pipeline_mod
        from scripts.release_notes import render as render_mod
        from scripts.release_notes.models import CategorizedBullet
        from scripts.release_notes.pipeline import RegenResult

        self._setup(monkeypatch, clone, line_exists=line_exists)
        grouped = render_mod.group_bullets(
            [CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix")]
        )
        captured = {}

        def _spy(repo, clone_dir, *, head_ref, tag_glob, base_ref=None):
            captured["base_ref"] = base_ref
            captured["head_ref"] = head_ref
            captured["tag_glob"] = tag_glob
            return RegenResult(
                base_tag=base_ref or "8.1.8", grouped=grouped, included=1,
                bullet_count=1, skipped=(), triage=(), had_prs=True)

        monkeypatch.setattr(pipeline_mod, "regenerate_unreleased", _spy)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = MagicMock(number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        base = dict(
            repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, urgency="LOW", date="2026-06-25", tag_glob=None,
            base_ref=None, contrib_base_ref=None, security_fixes=None, token="t",
            git_env={}, dry_run=False,
        )
        base.update(cut_kwargs)
        rc.cut(repo, **base)
        return captured

    def test_patch_ga_does_not_walk_empty_minor_range(self, monkeypatch, clone):
        # Regression: a patch GA (8.1.9 ga) continuing the existing 8.1 line must
        # not pass base_ref="8.1" to discovery (that would walk origin/8.1..8.1, an
        # empty range, and ship a notes-less PR). The plan base for a patch GA is
        # the M.m branch itself, not a pre-release-* branch, so the continuing-cut
        # override must be skipped and discovery left to resolve the previous patch
        # tag (8.1.8..8.1).
        captured = self._capture_discovery_range(
            monkeypatch, clone, line_exists={"8.1": True},
            cut_kwargs={"source_ref": "8.1", "version": "8.1.9", "stage": "ga"},
        )
        assert captured["base_ref"] is None, captured  # tag resolution, not origin/8.1..8.1
        assert captured["tag_glob"] is None, captured

    def test_rc_continuation_walks_the_line_not_source_ref(self, monkeypatch, clone):
        # An rc2 continuing pre-release-9.1.0 must discover the line itself, not
        # source_ref (unstable). In fork-at-freeze, unstable has advanced into the
        # next minor while the RC fixes live only on the line; walking
        # pre-release..unstable would note the next minor's PRs and miss the RC
        # fixes. So head = origin/pre-release-9.1.0 (the line tip) and base = the
        # fork point (stubbed to "a"*40 by _setup's git_output), with the glob
        # dropped since no RC tag is reachable on the fork.
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)
        captured = self._capture_discovery_range(
            monkeypatch, clone, line_exists={"pre-release-9.1.0": True},
            cut_kwargs={"source_ref": "unstable", "version": "9.1.0", "stage": "rc2"},
        )
        assert captured["head_ref"] == "origin/pre-release-9.1.0", captured
        assert captured["base_ref"] == "a" * 40, captured  # fork point, not the line name
        assert captured["tag_glob"] is None, captured

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
        # The delete is lease-guarded on the OID carried into 9.1 (created_line_oid,
        # stubbed to "a"*40 by _setup), so a concurrent push onto pre-release-9.1.0
        # after the rename branched is not silently lost.
        assert (
            "push", "--force-with-lease=refs/heads/pre-release-9.1.0:" + "a" * 40,
            "origin", "--delete", "pre-release-9.1.0",
        ) in calls, calls

    def test_ga_rename_delete_leases_on_the_oid_carried_into_the_line(self, monkeypatch, clone):
        # Provenance guard: the delete lease must be pinned to the OID that M.m was
        # actually created from (origin/pre-release-M.m.p), not some other ref. Make
        # rev-parse of that ref return a distinct OID from every other git_output, so
        # a regression that leased on the wrong commit would fail here.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True})
        carried_oid = "b" * 40

        def _git_output(_d, *a, **k):
            if a == ("rev-parse", "--verify", "origin/pre-release-9.1.0^{commit}"):
                return carried_oid + "\n"
            return "a" * 40 + "\n"

        monkeypatch.setattr(rc, "git_output", _git_output)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.side_effect = lambda **kw: MagicMock(number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="ga",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        # The pre-release line is created at carried_oid, so the delete leases on it.
        assert (
            "push", f"--force-with-lease=refs/heads/pre-release-9.1.0:{carried_oid}",
            "origin", "--delete", "pre-release-9.1.0",
        ) in calls, calls

    def test_ga_rename_refuses_blind_delete_when_line_created_out_of_band(self, monkeypatch, clone):
        # Residual-hole guard: the plan resolves as a rename (only pre-release exists),
        # but M.m appears out of band (a racing GA) between plan resolution and the
        # create step. This run then neither creates M.m nor holds a lease OID, so it
        # must not blind-delete pre-release-M.m.p (which could silently drop a commit
        # pushed onto it). It raises for manual reconcile and leaves the branch intact.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True})

        # Stateful: "9.1" is absent at plan time (-> rename plan) but present at the
        # step-4 create check (-> this run does not create it, created_line_oid="").
        ga_seen = {"n": 0}

        def _exists(_d, b):
            if b == "9.1":
                ga_seen["n"] += 1
                return ga_seen["n"] > 1  # False at plan time, True at step 4
            return b == "pre-release-9.1.0"

        monkeypatch.setattr(rc, "_remote_branch_exists", _exists)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.side_effect = lambda **kw: MagicMock(number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        with pytest.raises(RuntimeError, match="was not created by this run"):
            rc.cut(
                repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
                valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="ga",
                urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None,
                contrib_base_ref=None, security_fixes=None, token="t", git_env={}, dry_run=False,
            )
        # No delete of pre-release-9.1.0 fired (neither leased nor unconditional): the
        # branch is left intact for the operator to reconcile.
        assert not any(
            c[:1] == ("push",) and "--delete" in c and c[-1] == "pre-release-9.1.0"
            for c in calls
        ), calls
        # The worktree is still cleaned up on the way out (finally).
        self._assert_worktree_removed(calls, clone)

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

    def test_unresolved_prs_listed_in_release_pr_body(self, monkeypatch, clone):
        # A range commit whose resolved PR could not be fetched (a moved/deleted
        # PR, an issue, a cross-repo (#N)) must surface in the PR body so a shipped
        # change is not dropped silently, only logged.
        from unittest.mock import MagicMock

        from scripts.release_notes.models import UnresolvedPR
        unresolved_prs = (UnresolvedPR(number=777, sha="abcdef1234567890"),)
        self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True},
                    unresolved_prs=unresolved_prs)
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
        assert "Commits whose PR could not be fetched" in body
        assert "abcdef123456" in body  # sha truncated to 12 in the table
        assert "#777" in body          # the PR number as referenced

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
        # The body must show the precise range: the resolved mode, source/target
        # branches, and both ends as `ref @ <sha>` so a reviewer can audit the
        # exact commits, not just the branch-model names. _setup stubs git_output
        # to a deterministic 40-char SHA, abbreviated to 12 here.
        body = self._cut_body(monkeypatch, clone, line_exists={}, cut_kwargs={})
        assert "computed over the range below (`9.0.0..unstable`)" in body
        assert "mode: rc1" in body
        assert "source_ref: unstable" in body
        assert "target_branch: pre-release-9.1.0" in body
        assert "base: 9.0.0 @ aaaaaaaaaaaa" in body
        assert "head: unstable @ aaaaaaaaaaaa" in body

    def test_body_range_mode_labels_continuation(self, monkeypatch, clone):
        # A continued cut (rc2 onto an existing pre-release line) must label the
        # mode as a continuation and target that line, so the range block reflects
        # the resolved branch plan, not a fresh cut.
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)
        body = self._cut_body(
            monkeypatch, clone, line_exists={"pre-release-9.1.0": True},
            cut_kwargs={"stage": "rc2"},
        )
        assert "mode: rc2 continuation" in body
        assert "target_branch: pre-release-9.1.0" in body

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

    def test_security_fix_pr_excluded_from_generated_notes(self, monkeypatch, clone):
        # The fixture bullet credits #40; a --security-fix naming #40 would list it
        # twice. Instead of warning, the cut drops the generated bullet so #40
        # appears only under Security Fixes, and the body explains the exclusion.
        body = self._cut_body(monkeypatch, clone, line_exists={},
                              cut_kwargs={"security_fixes": ["Fix CVE (#40)"]})
        assert "Excluded from generated notes" in body
        assert "#40" in body
        # This is an informational note, not a warning, and does not hold the PR.
        assert "Security fixes need a look" not in body

    def test_security_fix_pr_dropped_from_dated_section(self, monkeypatch, clone):
        # The generated bullet for a PR supplied as a --security-fix must be absent
        # from the rendered dated section (only its Security Fixes entry remains),
        # so the notes are not inconsistent. Assert on the notes cut() actually
        # wrote, not just the PR body.
        from unittest.mock import MagicMock
        writes: dict[str, str] = {}
        self._setup(monkeypatch, clone, line_exists={}, writes=writes)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = MagicMock(number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="rc1",
            urgency="SECURITY", date="2026-06-25", tag_glob=None, base_ref=None,
            contrib_base_ref=None, security_fixes=["(CVE-2026-1) UAF in unblock (#40)"],
            token="t", git_env={}, dry_run=False,
        )
        notes = self._written_notes(writes, clone)
        # The security entry is present; the generated bullet for #40 is not.
        assert "CVE-2026-1" in notes
        assert "(#40)" in notes  # the Security Fixes entry keeps the ref
        # No generated category bullet still credits #40 (the fixture bullet said
        # "fix", so its normal-category line is gone).
        assert "* fix" not in notes

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

    def _cut_created(self, monkeypatch, clone, *, line_exists, cut_kwargs,
                     bullets=True, triage=(), had_prs=True, duplicate_prs=(), uncertain=()):
        """Run cut() with GitHub mocked and return the created PR's full kwargs.

        Like :meth:`_cut_body` but returns the whole ``create_pull`` kwargs dict so
        a test can assert on ``draft`` (the hold decision), not just the body.
        """
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
        return created[0] if created else None

    def test_clean_cut_opens_ready(self, monkeypatch, clone):
        # No flagged signals: the PR opens ready (not a draft) with no hold banner.
        kw = self._cut_created(monkeypatch, clone, line_exists={}, cut_kwargs={})
        assert kw["draft"] is False
        assert "Held as a draft" not in kw["body"]

    def test_flagged_cut_held_as_draft(self, monkeypatch, clone):
        # A cut with a flagged signal (unanchored baseline) opens as a draft and
        # leads the body with the hold banner naming the reason.
        kw = self._cut_created(monkeypatch, clone, line_exists={},
                               cut_kwargs={"version": "9.0.0", "baseline_unanchored": True})
        assert kw["draft"] is True
        assert "Held as a draft" in kw["body"]
        assert "baseline is unanchored" in kw["body"]
        # The banner leads the body, before the summary line.
        assert kw["body"].index("Held as a draft") < kw["body"].index("Cuts **")

    def test_force_ready_opens_flagged_cut_ready(self, monkeypatch, clone):
        # force_ready overrides the hold: the same flagged cut opens ready, and the
        # banner records that the flags were overridden rather than held.
        kw = self._cut_created(monkeypatch, clone, line_exists={},
                               cut_kwargs={"version": "9.0.0", "baseline_unanchored": True,
                                           "force_ready": True})
        assert kw["draft"] is False
        assert "Opened ready despite" in kw["body"]
        assert "force_ready" in kw["body"]
        assert "Held as a draft" not in kw["body"]
        # The warning section itself still renders below the banner.
        assert "baseline is unanchored" in kw["body"]

    def test_triage_only_cut_is_held(self, monkeypatch, clone):
        # An advisory-tier signal (PRs needing triage, notes still non-empty) also
        # holds: any reviewer-facing signal opens the PR as a draft.
        from scripts.release_notes.models import MergedPR
        triage = (MergedPR(number=7, title="thing", author="bob", url="https://x/7"),)
        kw = self._cut_created(monkeypatch, clone, line_exists={}, cut_kwargs={},
                               triage=triage)
        assert kw["draft"] is True
        assert "PRs need triage" in kw["body"]

    def test_dry_run_previews_hold(self, monkeypatch, clone, capsys):
        # --dry-run shows the hold decision the real cut would make.
        from unittest.mock import MagicMock
        self._setup(monkeypatch, clone, line_exists={})
        repo = MagicMock()
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.0.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=True, baseline_unanchored=True,
        )
        out = capsys.readouterr().out
        assert "PR would open: DRAFT (held)" in out
        assert "baseline is unanchored" in out

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

    def test_contrib_head_on_ga_drain_is_the_line_tip_not_source_ref(self, monkeypatch, clone):
        # Regression: on a GA that drains a pre-release line, the contributor list
        # must be collected over the release line (fork_point..origin/pre-release-M.m.p),
        # not contrib_base..source_ref. source_ref (unstable) has advanced into the
        # next minor at freeze; crediting to its tip would list unstable's post-freeze
        # authors instead of the RC-fix authors on the line. cut() feeds the same head
        # discovery walked (notes_head_ref = the line tip) as contrib_head, so the
        # credits span exactly the notes range. Reverting contrib_head back to HEAD/
        # source_ref must fail this test.
        from unittest.mock import MagicMock
        # GA drain: pre-release line exists, M.m does not -> rename/drain plan whose
        # base_ref is the pre-release branch, so _continuing_line_range fires.
        self._setup(monkeypatch, clone, line_exists={"pre-release-9.1.0": True})

        captured = {}

        def _promote(valkey_clone_dir, **kw):
            captured["contrib_head"] = kw["contrib_head"]
            return "NOTES", "VERSION"

        monkeypatch.setattr(rc, "promote_and_bump", _promote)
        repo = MagicMock()

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, source_ref="unstable", version="9.1.0", stage="ga",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None,
            contrib_base_ref=None, security_fixes=None, token="t", git_env={}, dry_run=True,
        )
        # The line tip _continuing_line_range resolved, not "unstable".
        assert captured["contrib_head"] == "origin/pre-release-9.1.0"

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


class TestNotesRange:
    """The precise base/head-ref + SHA range surfaced in the PR body / dry-run."""

    _RANGE = rc._NotesRange(
        mode="rc2 continuation", source_ref="unstable",
        target_branch="pre-release-9.2.0", base_ref="origin/pre-release-9.2.0",
        base_sha="a" * 40, head_ref="unstable", head_sha="b" * 40,
    )

    def test_plan_mode_fresh_rc1(self) -> None:
        plan = BranchPlan("rc1", "pre-release-9.1.0", "unstable", False, None)
        assert rc._plan_mode(plan) == "rc1"

    def test_plan_mode_continuation(self) -> None:
        plan = BranchPlan("rc2", "pre-release-9.1.0", "pre-release-9.1.0", True, None)
        assert rc._plan_mode(plan) == "rc2 continuation"

    def test_plan_mode_ga_rename(self) -> None:
        plan = BranchPlan("ga", "9.1", "pre-release-9.1.0", True, "pre-release-9.1.0")
        assert rc._plan_mode(plan) == "ga rename"

    def test_short_sha_abbreviates_full_sha(self) -> None:
        assert rc._short_sha("a" * 40) == "a" * 12

    def test_short_sha_passes_non_sha_through(self) -> None:
        # An unresolvable ref degrades to the ref name; show it verbatim, not clipped.
        assert rc._short_sha("origin/unstable") == "origin/unstable"

    def test_short_sha_empty_is_unknown(self) -> None:
        assert rc._short_sha("") == "unknown"

    def test_range_lines_show_mode_refs_and_shas(self) -> None:
        lines = rc._notes_range_lines(self._RANGE)
        assert lines == [
            "mode: rc2 continuation",
            "source_ref: unstable",
            "target_branch: pre-release-9.2.0",
            "base: origin/pre-release-9.2.0 @ aaaaaaaaaaaa",
            "head: unstable @ bbbbbbbbbbbb",
        ]

    def test_body_section_renders_fenced_block(self) -> None:
        section = rc._notes_range_body_section(self._RANGE, regen=None)
        assert "`origin/pre-release-9.2.0..unstable`" in section
        assert "mode: rc2 continuation" in section
        assert "base: origin/pre-release-9.2.0 @ aaaaaaaaaaaa" in section
        assert section.count("```") == 2  # fenced code block

    def test_body_section_falls_back_when_range_missing(self) -> None:
        # When the range could not be captured, keep the coarse one-liner so the
        # body still states the span.
        from unittest.mock import MagicMock
        regen = MagicMock(base_tag="9.0.0")
        section = rc._notes_range_body_section(None, regen)
        assert section == "- Release notes computed over `9.0.0..HEAD`.\n"

    def test_resolve_notes_range_dereferences_refs_to_shas(self, tmp_path) -> None:
        # End-to-end resolution against a real repo: both ends dereference to the
        # committed SHA the compare API accepts; the mode reflects the plan.
        from scripts.common.proc import git_output, run_git
        repo = str(tmp_path / "r")
        os.makedirs(repo)
        run_git(repo, "init", "-q")
        run_git(repo, "config", "user.email", "t@e")
        run_git(repo, "config", "user.name", "t")
        (tmp_path / "r" / "f").write_text("x")
        run_git(repo, "add", "f")
        run_git(repo, "commit", "-q", "-m", "c")
        run_git(repo, "branch", "-M", "unstable")
        sha = git_output(repo, "rev-parse", "HEAD").strip()

        from unittest.mock import MagicMock
        plan = BranchPlan("rc2", "pre-release-9.1.0", "pre-release-9.1.0", True, None)
        regen = MagicMock(base_tag="unstable")  # a resolvable ref for this fixture repo
        # head_ref is the ref discovery actually walked (the line tip on a
        # continuing cut); here both ends point at the fixture's single commit.
        rng = rc._resolve_notes_range(
            repo, plan, source_ref="unstable", head_ref="unstable", regen=regen
        )
        assert rng.mode == "rc2 continuation"
        assert rng.target_branch == "pre-release-9.1.0"
        assert rng.base_sha == sha
        assert rng.head_sha == sha


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


class TestRootCommit:
    def test_returns_oldest_root(self, monkeypatch) -> None:
        # A history with several roots (unrelated trees merged in) prints newest
        # first; the oldest (last line) is chosen so the range stays complete.
        monkeypatch.setattr(rc, "git_output", lambda d, *a: "newroot\noldroot\n")
        assert rc._root_commit("/d") == "oldroot"

    def test_none_when_unreadable(self, monkeypatch) -> None:
        def _boom(d, *a):
            raise RuntimeError("not a git repo")
        monkeypatch.setattr(rc, "git_output", _boom)
        assert rc._root_commit("/d") is None

    def test_none_when_empty_output(self, monkeypatch) -> None:
        monkeypatch.setattr(rc, "git_output", lambda d, *a: "\n")
        assert rc._root_commit("/d") is None


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
            security_fixes=None, security_noted_prs=(), baseline_unanchored=False,
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

        bl = [CategorizedBullet(pr_number=44, author="a", category="Bug Fixes", text="fix")]
        grouped = render_mod.group_bullets(bl)
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
        # This test targets the already-credited dedup, not the topology walk;
        # stub _continuing_line_range so no real merge-base runs against the clone.
        monkeypatch.setattr(
            rcmod, "_continuing_line_range",
            lambda repo_dir, pln, *, source_ref, git_env: ("forksha", f"origin/{pln.base_ref}"),
        )
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

    def _run(self, monkeypatch, *, plan, base_ref, tag_glob, tag_glob_derived=False,
             prev_release=None, root="rootsha", baseline_unanchored=False):
        captured = {}
        self._capture_regen(monkeypatch, captured)
        monkeypatch.setattr(rc, "resolve_branch_plan", lambda *a, **k: plan)
        # Stub the git topology walk: this class tests the base/head wiring, not
        # merge-base itself (covered by TestContinuingLineRange). Return a fixed
        # fork point + line ref so no real repo is needed.
        monkeypatch.setattr(
            rc, "_continuing_line_range",
            lambda repo_dir, pln, *, source_ref, git_env: ("forksha", f"origin/{pln.base_ref}"),
        )
        # Stub previous-release resolution (a non-continuing first cut with a
        # derived glob consults it); prev_release=None models the first-release-ever
        # fallback, a ("tag", "sha") tuple models a resolvable previous release.
        monkeypatch.setattr(
            rc.discover_mod, "resolve_previous_release_tag",
            lambda repo_dir, version: prev_release,
        )
        # Stub the root-commit resolution the unanchored guard uses so no real repo
        # is needed; root=None models an unreadable history (guard cannot degrade).
        monkeypatch.setattr(rc, "_root_commit", lambda repo_dir, ref="HEAD": root)
        with pytest.raises(_StopCut):
            rc.cut(
                object(), repo_full_name="valkey-io/valkey", source_clone_dir="/d",
                valkey_clone_dir="/d", source_ref="unstable", version="9.1.0",
                stage=plan.stage, urgency="LOW", date="2026-06-29",
                tag_glob=tag_glob, tag_glob_derived=tag_glob_derived, base_ref=base_ref,
                contrib_base_ref=None,
                security_fixes=None, token="t", git_env={}, dry_run=True,
                baseline_unanchored=baseline_unanchored,
            )
        return captured

    def test_rc2_no_base_ref_walks_the_line_and_drops_glob(self, monkeypatch) -> None:
        # rc2+ arrives with base_ref=None and tag_glob="9.1.0-rc*". cut() must
        # discover the line itself: head = origin/pre-release-9.1.0 (the line tip,
        # from _continuing_line_range), base = the fork point (stubbed "forksha"),
        # glob cleared. Not pre-release-9.1.0..unstable (which would note the next
        # minor's PRs and miss the RC fixes), and not a describe --match with no tag.
        captured = self._run(
            monkeypatch, plan=self._RC_CONTINUE_PLAN,
            base_ref=None, tag_glob="9.1.0-rc*",
        )
        assert captured["head_ref"] == "origin/pre-release-9.1.0"
        assert captured["base_ref"] == "forksha"
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

    def test_rc1_unanchored_degrades_to_root_not_abort(self, monkeypatch) -> None:
        # The claim's scenario: rc1 whose repo has no previous release (first release
        # ever / tagless fork). main.py resolved no baseline (base_ref None, glob None)
        # and set baseline_unanchored=True. Handing None/None to discovery would call
        # resolve_last_tag(unstable), which finds no reachable tag in the fork-at-freeze
        # model and raises, aborting the cut before the unanchored banner renders. The
        # guard must instead degrade to root..head so the cut proceeds as a draft PR.
        captured = self._run(
            monkeypatch, plan=self._RC_FIRST_PLAN,
            base_ref=None, tag_glob=None, baseline_unanchored=True, root="rootsha",
        )
        assert captured["base_ref"] == "rootsha"   # degraded to root, not None -> no abort
        assert captured["tag_glob"] is None
        assert captured["head_ref"] == "unstable"  # first cut still walks source_ref

    def test_explicit_glob_on_first_cut_is_preserved(self, monkeypatch) -> None:
        # A non-continuing first cut carrying an *explicit* --tag-glob (tag_glob_derived
        # False, the _run default) is the maintainer's intent: it is passed straight to
        # discovery, not rewritten to a previous-release baseline. Resolve-or-fail-loud
        # is the explicit-override contract, same as --base-ref.
        captured = self._run(
            monkeypatch, plan=self._RC_FIRST_PLAN,
            base_ref=None, tag_glob="9.1.0-rc*",
        )
        assert captured["base_ref"] is None
        assert captured["tag_glob"] == "9.1.0-rc*"

    # A mis-dispatched rc2 (no pre-release line yet) resolves to a non-continuing
    # plan carrying the rc_warning, but based on source_ref with a *derived* rc glob.
    _RC2_MISDISPATCH_PLAN = BranchPlan(
        "rc2", "pre-release-9.1.0", "unstable", False, None,
        rc_warning="rc2 dispatched but pre-release-9.1.0 does not exist yet",
    )

    def test_misdispatched_rc2_derived_glob_anchors_to_prev_release(self, monkeypatch) -> None:
        # The fix: a mis-dispatched rc2 (non-continuing, derived "9.1.0-rc*" glob)
        # would otherwise abort in resolve_last_tag (no such tag on unstable in the
        # fork-at-freeze model), swallowing the rc_warning. Instead the derived glob
        # is swapped for the resolved previous-release tag so the cut proceeds and
        # the warning reaches the (draft) PR.
        captured = self._run(
            monkeypatch, plan=self._RC2_MISDISPATCH_PLAN,
            base_ref=None, tag_glob="9.1.0-rc*", tag_glob_derived=True,
            prev_release=("9.0.0", "sha0"),
        )
        assert captured["base_ref"] == "9.0.0"  # previous release, not the doomed glob
        assert captured["tag_glob"] is None
        assert captured["head_ref"] == "unstable"  # first cut still walks source_ref

    def test_first_release_ever_derived_glob_degrades_to_root(self, monkeypatch) -> None:
        # No earlier release resolves (first release ever / tagless fork): drop the
        # derived glob and flag the baseline unanchored, then the unanchored guard
        # degrades to the root commit so discovery walks root..head instead of
        # handing None/None to resolve_last_tag (which would find no reachable tag on
        # unstable in the fork-at-freeze model and abort the cut).
        captured = self._run(
            monkeypatch, plan=self._RC2_MISDISPATCH_PLAN,
            base_ref=None, tag_glob="9.1.0-rc*", tag_glob_derived=True,
            prev_release=None, root="rootsha",
        )
        assert captured["base_ref"] == "rootsha"  # degraded to root, not None
        assert captured["tag_glob"] is None
        assert captured["head_ref"] == "unstable"

    def test_unanchored_without_readable_root_stays_none(self, monkeypatch) -> None:
        # If even the root cannot be read (an empty or corrupt clone), the guard
        # cannot degrade: base_ref stays None and the original resolve_last_tag path
        # runs (and would abort). Documents that the guard is best-effort, not a
        # guarantee the cut always proceeds.
        captured = self._run(
            monkeypatch, plan=self._RC2_MISDISPATCH_PLAN,
            base_ref=None, tag_glob="9.1.0-rc*", tag_glob_derived=True,
            prev_release=None, root=None,
        )
        assert captured["base_ref"] is None
        assert captured["tag_glob"] is None


class _StopCut(Exception):
    """Sentinel to abort cut() right after the discovery call in baseline tests."""


class TestContinuingLineRange:
    """The fork-at-freeze range fix: a continuing cut walks the release line.

    Builds a real fork-at-freeze topology and drives _continuing_line_range +
    list_range_commits so the assertions are on the actual commits discovery would
    see, not on mocked refs. This is the regression guard for the high-severity bug
    where a continuing cut walked pre-release..unstable, noting the next minor's
    PRs and missing the RC fixes that live only on the line.
    """

    @staticmethod
    def _fork_at_freeze_repo(tmp_path):
        # A single repo standing in for the clone. The pre-release line is
        # registered under `origin/<name>`, matching how _continuing_line_range /
        # _resolve_base_ref reach it (the real clone is --branch source_ref, so the
        # line exists only as origin/<name>). `run_git fetch origin <line>` is a
        # harmless no-op against a local repo that already has the ref.
        from scripts.common.proc import git_output, run_git
        repo = str(tmp_path / "r")
        os.makedirs(repo)
        run_git(repo, "init", "-q", "-b", "unstable")
        run_git(repo, "config", "user.email", "t@t")
        run_git(repo, "config", "user.name", "t")

        def commit(subject):
            run_git(repo, "commit", "-q", "--allow-empty", "-m", subject)
            return git_output(repo, "rev-parse", "HEAD").strip()

        commit("prev GA 9.0.0 (#100)")
        run_git(repo, "tag", "9.0.0")
        commit("work before freeze (#150)")
        # Freeze: the line forks from unstable here. Build it as a real
        # pre-release-9.1.0 branch first, then expose it as a remote-tracking ref
        # (origin/pre-release-9.1.0) via a self-remote, so _continuing_line_range's
        # `fetch origin <line>` succeeds exactly as it would against the real clone.
        run_git(repo, "branch", "pre-release-9.1.0")
        # unstable advances into the next minor (9.2 dev) after the freeze.
        commit("9.2 dev work (#300)")
        commit("more 9.2 dev (#301)")
        # RC fixes land only on the pre-release line.
        run_git(repo, "checkout", "-q", "pre-release-9.1.0")
        commit("rc fix a (#200)")
        commit("rc fix b (#201)")
        run_git(repo, "checkout", "-q", "unstable")
        # Register the repo as its own origin and fetch, so origin/pre-release-9.1.0
        # exists as a remote-tracking ref (the clone's real shape).
        run_git(repo, "remote", "add", "origin", repo)
        run_git(repo, "fetch", "-q", "origin")
        return repo

    def test_range_covers_rc_fixes_and_excludes_next_minor(self, tmp_path) -> None:
        from scripts.release_notes.discover import list_range_commits

        repo = self._fork_at_freeze_repo(tmp_path)
        plan = BranchPlan("rc2", "pre-release-9.1.0", "pre-release-9.1.0", True, None)
        fork_point, line_ref = rc._continuing_line_range(
            repo, plan, source_ref="unstable", git_env={}
        )
        assert line_ref == "origin/pre-release-9.1.0"

        # The commits discovery would actually resolve over the fixed range.
        commits = list_range_commits(repo, fork_point, line_ref)
        subjects = " ".join(subj for _sha, subj, _body in commits)
        # RC fixes on the line are present...
        assert "#200" in subjects and "#201" in subjects
        # ...and the next minor's post-freeze unstable PRs are not.
        assert "#300" not in subjects and "#301" not in subjects

    def test_old_buggy_range_would_have_been_exactly_wrong(self, tmp_path) -> None:
        # Pin the bug the fix corrects: the prior range (pre-release..unstable) is
        # wrong in both directions. Documents why the fix is needed and fails if
        # someone reverts to walking source_ref as the head.
        from scripts.release_notes.discover import list_range_commits

        repo = self._fork_at_freeze_repo(tmp_path)
        buggy = list_range_commits(repo, "origin/pre-release-9.1.0", "unstable")
        buggy_subjects = " ".join(subj for _sha, subj, _body in buggy)
        assert "#300" in buggy_subjects and "#301" in buggy_subjects  # noted wrongly
        assert "#200" not in buggy_subjects and "#201" not in buggy_subjects  # missed

    def test_guard_rejects_non_ancestor_fork_point(self, tmp_path, monkeypatch) -> None:
        # The ancestor guard fails closed: if merge-base resolves something that is
        # not an ancestor of the line tip (a mis-wire, a rewritten line), refuse
        # rather than cut a wrong/empty range.
        from scripts.common.proc import git_output
        repo = self._fork_at_freeze_repo(tmp_path)
        plan = BranchPlan("rc2", "pre-release-9.1.0", "pre-release-9.1.0", True, None)
        # Force merge-base to return an unrelated commit (the unstable tip), which
        # is not an ancestor of the line tip; the real --is-ancestor check runs.
        unstable_tip = git_output(repo, "rev-parse", "unstable").strip()
        real_git_output = rc.git_output

        def _fake(repo_dir, *a, **k):
            if a[:1] == ("merge-base",) and "--is-ancestor" not in a:
                return unstable_tip + "\n"
            return real_git_output(repo_dir, *a, **k)

        monkeypatch.setattr(rc, "git_output", _fake)
        with pytest.raises(ValueError, match="not an ancestor"):
            rc._continuing_line_range(repo, plan, source_ref="unstable", git_env={})


class TestMisdispatchSurfacesWarning:
    """A mis-dispatched rc2+/GA must reach a human, not abort opaquely.

    Regression for the finding: a non-continuing rc2+ (no pre-release line yet)
    arrives with a derived `<version>-rc*` glob; in valkey's fork-at-freeze model
    unstable carries no such tag, so resolve_last_tag would raise and abort the cut
    before any PR opened, swallowing the rc-out-of-sequence warning resolve_branch_plan
    built. These tests do not mock discovery: real resolve_branch_plan +
    regenerate_unreleased + discover run against a real clone. Only the AI generate
    step and the GitHub API are stubbed.
    """

    @staticmethod
    def _repo_no_prerelease_line(tmp_path):
        # A real clone with the release files the worktree step reads (00-RELEASENOTES,
        # src/version.h), copied from the shared fixture, plus real history: a 9.0.0
        # release tag (so resolve_previous_release_tag resolves for real) and an
        # unstable branch with post-release work, but no pre-release-9.1.0 line.
        # Registered as its own origin so _remote_branch_exists (ls-remote) and the
        # worktree's origin/<name> refs behave as in a real clone.
        from scripts.common.proc import run_git
        repo = str(tmp_path / "r")
        shutil.copytree(_FIXTURE_CLONE, repo)
        run_git(repo, "init", "-q", "-b", "unstable")
        run_git(repo, "config", "user.email", "t@t")
        run_git(repo, "config", "user.name", "t")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "prev GA (#100)")
        run_git(repo, "tag", "9.0.0")
        run_git(repo, "commit", "-q", "--allow-empty", "-m", "post-release work (#150)")
        run_git(repo, "remote", "add", "origin", repo)
        run_git(repo, "fetch", "-q", "origin")
        return repo

    def test_misdispatched_rc2_opens_held_draft_with_warning(self, tmp_path, monkeypatch, capsys):
        # rc2 with no pre-release-9.1.0 line yet: a mis-dispatch. The cut must not
        # abort in resolve_last_tag; it resolves the previous-release baseline, runs
        # discovery for real, and the dry-run shows a held draft naming the rc
        # warning, so a human sees the mis-dispatch.
        from unittest.mock import MagicMock

        from github.GithubException import UnknownObjectException

        repo = self._repo_no_prerelease_line(tmp_path)
        # Real discovery runs (9.0.0..unstable finds the #150 commit). The GitHub
        # API is the only external boundary: 404 the PR lookup so the commit lands
        # in unresolved and the range carries no included PR -> generate is never
        # reached, no model runs. The cut still proceeds to the hold decision.
        gh_repo = MagicMock()
        gh_repo.get_pull.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
        rc.cut(
            gh_repo, repo_full_name="valkey-io/valkey", source_clone_dir=repo,
            valkey_clone_dir=repo, source_ref="unstable", version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob="9.1.0-rc*", tag_glob_derived=True,
            base_ref=None, contrib_base_ref=None, security_fixes=None,
            token="t", git_env={}, dry_run=True,
        )
        out = capsys.readouterr().out
        # The cut proceeded to the hold decision (did not abort), and the draft-hold
        # names the mis-dispatch warning.
        assert "DRAFT (held)" in out
        assert "rc out of sequence" in out
        # Discovery anchored to the previous release, not a doomed rc glob.
        assert "9.0.0" in out

    @staticmethod
    def _labelled_pull(number, *, title, labels):
        # A minimal PyGithub-shaped pull for the range PR: enough fields for
        # hydrate_prs/_build_merged_pr and _is_backport_pull to read real strings
        # (never MagicMock auto-vivified attrs, which crash the regex paths).
        from unittest.mock import MagicMock
        pull = MagicMock()
        pull.number = number
        pull.title = title
        pull.user.login = "alice"
        pull.html_url = f"https://x/{number}"
        pull.body = "the PR description"
        pull.merge_commit_sha = ""
        pull.head.ref = "feature/x"
        label_mocks = []
        for name in labels:
            m = MagicMock()
            m.name = name
            label_mocks.append(m)
        pull.labels = label_mocks
        return pull

    def test_misdispatched_rc2_generates_notes_and_holds(self, tmp_path, monkeypatch, capsys):
        # The full path with real discovery + classify + render: the range PR
        # resolves to a real release-noted PR, only the AI `generate` step is
        # stubbed. The rendered dated section must carry the generated bullet and
        # the cut must still open a held draft naming the mis-dispatch warning, so
        # a real note is produced without swallowing the warning.
        from scripts.release_notes import pipeline as pipeline_mod
        from scripts.release_notes.models import CategorizedBullet, GenerationResult

        repo = self._repo_no_prerelease_line(tmp_path)
        # The range commit (#150) now resolves to a real labelled PR, so classify
        # INCLUDEs it and generation runs. get_pull returns that PR for #150.
        pull = self._labelled_pull(
            150, title="Add a useful cluster feature", labels=("release-notes",)
        )
        gh_repo = self._gh_repo_serving({150: pull})
        # Stub only the AI boundary: return one bullet for #150. Everything below
        # (discover, hydrate, classify) and above (dedup, group, render) is real.
        # author mirrors the factual re-stamp the real generate applies from the PR
        # (never model-supplied), so render appends the "by @alice" credit.
        monkeypatch.setattr(
            pipeline_mod.generate_mod, "generate",
            lambda include, **k: GenerationResult(
                bullets=(CategorizedBullet(
                    pr_number=150, author="alice", category="Bug Fixes",
                    text="Add a useful cluster feature",
                ),),
                skipped=(),
            ),
        )
        rc.cut(
            gh_repo, repo_full_name="valkey-io/valkey", source_clone_dir=repo,
            valkey_clone_dir=repo, source_ref="unstable", version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob="9.1.0-rc*", tag_glob_derived=True,
            base_ref=None, contrib_base_ref=None, security_fixes=None,
            token="t", git_env={}, dry_run=True,
        )
        out = capsys.readouterr().out
        # A real dated section was rendered with the generated bullet (render
        # appends the "(#150)" ref and "by @alice" credit, proving the full
        # generate -> dedup -> group -> render path ran).
        assert "Valkey 9.1.0-rc2" in out
        assert "Add a useful cluster feature by @alice (#150)" in out
        # ...and the mis-dispatch still holds the PR as a draft with its warning.
        assert "DRAFT (held)" in out
        assert "rc out of sequence" in out

    @staticmethod
    def _gh_repo_serving(pulls):
        # A GitHub repo mock whose get_pull serves the given {number: pull} and
        # 404s anything else (an unknown number is a cross-repo / issue ref).
        from unittest.mock import MagicMock

        from github.GithubException import UnknownObjectException

        repo = MagicMock()

        def _get_pull(number):
            if number in pulls:
                return pulls[number]
            raise UnknownObjectException(404, {"message": "Not Found"}, {})

        repo.get_pull.side_effect = _get_pull
        return repo


class TestSecurityOnlyCutNotEmpty:
    """A cut carrying only security_fixes must not be flagged empty or held."""

    _RC_PLAN = BranchPlan("rc1", "9.1", "unstable", False, "unstable")
    _GA_PLAN = BranchPlan("ga", "9.1", "pre-release-9.1.0", True, "pre-release-9.1.0")

    @staticmethod
    def _regen(bullet_count=0, had_prs=False, triage=()):
        from types import SimpleNamespace
        return SimpleNamespace(
            bullet_count=bullet_count, had_prs=had_prs, triage=triage,
            included=0, skipped=(), duplicate_prs=(), uncertain=(),
            unresolved=(), unresolved_backports=(), unresolved_prs=(),
            unresolved_cherry_picks=(), collided=(), base_tag="9.0.0",
        )

    def _meta(self, *, security_fixes=None, already_credited=(), noted_bullet_count=0,
              bullet_count=0, had_prs=False):
        return rc._NotesMeta(
            regen=self._regen(bullet_count=bullet_count, had_prs=had_prs),
            already_credited=already_credited,
            noted_bullet_count=noted_bullet_count,
            urgency="SECURITY",
            security_fixes=security_fixes,
            security_noted_prs=(),
            baseline_unanchored=False,
        )

    def test_hold_reasons_no_empty_when_security_fixes_present(self):
        meta = self._meta(security_fixes=["Fix CVE-2025-1234 (CVSS 9.8)"])
        reasons = rc._hold_reasons(self._RC_PLAN, meta)
        assert "empty release notes" not in reasons

    def test_hold_reasons_empty_when_no_content_at_all(self):
        meta = self._meta(security_fixes=None)
        reasons = rc._hold_reasons(self._RC_PLAN, meta)
        assert "empty release notes" in reasons

    def test_empty_notes_section_suppressed_with_security_fixes(self):
        meta = self._meta(security_fixes=["Fix CVE-2025-1234 (CVSS 9.8)"])
        section = rc._empty_notes_section(meta, self._RC_PLAN)
        assert section == ""

    def test_empty_notes_section_renders_without_security_fixes(self):
        meta = self._meta(security_fixes=None)
        section = rc._empty_notes_section(meta, self._RC_PLAN)
        assert "Empty release notes" in section

    def test_no_new_prs_section_suppressed_with_security_fixes(self):
        meta = self._meta(
            security_fixes=["Fix CVE-2025-9999"],
            already_credited=[44, 45],
            noted_bullet_count=0,
        )
        section = rc._no_new_prs_section(meta, self._GA_PLAN)
        assert section == ""

    def test_no_new_prs_hold_reason_suppressed_with_security_fixes(self):
        meta = self._meta(
            security_fixes=["Fix CVE-2025-9999"],
            already_credited=[44, 45],
            noted_bullet_count=0,
        )
        reasons = rc._hold_reasons(self._GA_PLAN, meta)
        assert "no new release notes (every PR already credited)" not in reasons
