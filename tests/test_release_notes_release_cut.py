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

    def test_commit_title_patch_ga_omits_suffix(self) -> None:
        assert commit_title("9.1.1", "ga") == "Add release notes entry for Valkey 9.1.1"


class TestResolveBranchPlan:
    def _exists(self, monkeypatch, present):
        monkeypatch.setattr(rc, "_remote_branch_exists",
                            lambda repo_dir, branch: branch in present)

    def test_rc1_targets_minor_branch(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"9.1"})
        plan = resolve_branch_plan("/d", version="9.1.0", stage="rc1")
        assert plan == BranchPlan("rc1", "9.1", "9.1")

    def test_rcN_targets_minor_branch(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"9.1"})
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: None)
        plan = resolve_branch_plan("/d", version="9.1.0", stage="rc2")
        assert plan == BranchPlan("rc2", "9.1", "9.1")

    def test_ga_targets_minor_branch(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"9.1"})
        plan = resolve_branch_plan("/d", version="9.1.0", stage="ga")
        assert plan == BranchPlan("ga", "9.1", "9.1")

    def test_uppercase_rc1_normalizes(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"9.1"})
        plan = resolve_branch_plan("/d", version="9.1.0", stage="RC1")
        assert plan.stage == "rc1"
        assert plan.target == "9.1"

    def test_uppercase_ga_normalizes(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"9.1"})
        plan = resolve_branch_plan("/d", version="9.1.0", stage="GA")
        assert plan.stage == "ga"
        assert plan.target == "9.1"

    def test_missing_branch_raises(self, monkeypatch) -> None:
        self._exists(monkeypatch, set())
        with pytest.raises(ValueError, match="does not exist"):
            resolve_branch_plan("/d", version="9.1.0", stage="rc1")

    def test_bad_stage_raises(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"9.1"})
        with pytest.raises(ValueError):
            resolve_branch_plan("/d", version="9.1.0", stage="beta")

    def test_bad_version_raises(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"9.1"})
        with pytest.raises(ValueError):
            resolve_branch_plan("/d", version="9.1", stage="rc1")

    def test_rc_sequence_warning_plumbed(self, monkeypatch) -> None:
        self._exists(monkeypatch, {"9.1"})
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: "out-of-seq detail")
        plan = resolve_branch_plan("/d", version="9.1.0", stage="rc3")
        assert plan.rc_warning == "out-of-seq detail"


class TestRcSequenceWarning:
    """The out-of-sequence rc detection feeding BranchPlan.rc_warning."""

    def _stub_notes(self, monkeypatch, notes):
        # _warn_rc_sequence fetches the branch then reads 00-RELEASENOTES from it.
        monkeypatch.setattr(rc, "run_git", lambda *a, **k: None)
        monkeypatch.setattr(rc, "git_output", lambda *a, **k: notes)

    def _warn(self, monkeypatch, stage_lc, notes):
        self._stub_notes(monkeypatch, notes)
        return rc._warn_rc_sequence("/d", "9.1", stage_lc, 9, 1, 0)

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
        assert rc._warn_rc_sequence("/d", "9.1", "rc2", 9, 1, 0) is None





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


class TestValidateReleaseProgression:
    @staticmethod
    def _version_h(version: str, stage: str | None = "ga") -> str:
        text = (
            f'#define VALKEY_VERSION "{version}"\n'
            "#define VALKEY_VERSION_NUM 0x00000000\n"
        )
        if stage is not None:
            text += f'#define VALKEY_RELEASE_STAGE "{stage}"\n'
        return text

    @pytest.mark.parametrize(
        ("current", "stage", "target"),
        [
            ("7.2.13", None, "7.2.14"),
            ("8.0.9", None, "8.0.10"),
            ("8.1.8", "ga", "8.1.9"),
            ("9.0.4", "ga", "9.0.5"),
            ("9.1.0", "ga", "9.1.1"),
        ],
    )
    def test_live_patch_lines_advance(self, current, stage, target) -> None:
        rc.validate_release_progression(self._version_h(current, stage), target, "ga")

    def test_rc_and_ga_advance_same_version(self) -> None:
        rc.validate_release_progression(self._version_h("9.2.0", "rc1"), "9.2.0", "rc2")
        rc.validate_release_progression(self._version_h("9.2.0", "rc2"), "9.2.0", "ga")

    def test_unstable_sentinel_can_start_release(self) -> None:
        rc.validate_release_progression(
            self._version_h("255.255.255", "dev"), "9.2.0", "rc1"
        )

    @pytest.mark.parametrize(("target", "stage"), [("8.1.8", "ga"), ("8.1.7", "ga")])
    def test_same_or_older_release_rejected(self, target, stage) -> None:
        with pytest.raises(ValueError, match="already-released or backward"):
            rc.validate_release_progression(
                self._version_h("8.1.8", "ga"), target, stage
            )




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
            contrib_base=None, contrib_head="9.1",
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

        def _list(repo, base, head, token, *, repo_dir=None, pr_logins=None):
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
                "origin/unstable": "base_sha", "origin/9.1": "head_sha"
            }[ref],
        )
        promote_and_bump(
            clone, grouped=grouped, dest_notes_text="",
            dest_version_text=version_text, version="9.1.0", stage_lc="rc2",
            urgency="LOW", date="2026-06-25", repo_full_name="valkey-io/valkey",
            contrib_base="origin/unstable",
            contrib_head="origin/9.1", token="t", security_fixes=None,
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


class TestOriginGuard:
    def test_rejects_non_github_origin(self, monkeypatch) -> None:
        monkeypatch.setattr(
            rc, "git_output", lambda *a, **k: "/tmp/attacker-controlled-repo"
        )

        with pytest.raises(RuntimeError, match="origin URL was modified"):
            rc._assert_origin_url("/repo", "valkey-io/valkey")

    def test_freshness_check_revalidates_before_fetch(self, monkeypatch) -> None:
        events = []
        monkeypatch.setattr(
            rc, "_assert_origin_url",
            lambda repo_dir, repo_full_name: events.append("origin"),
        )
        monkeypatch.setattr(
            rc, "_fetch_remote_branch_tip",
            lambda repo_dir, branch, git_env: events.append("fetch") or "a" * 40,
        )

        rc._assert_remote_branch_unchanged(
            "/repo", "9.1", "a" * 40, {}, "valkey-io/valkey"
        )

        assert events == ["origin", "fetch"]


class TestCutOrchestration:
    """End-to-end cut() with git + GitHub + pipeline mocked, real fixture worktree."""

    def _setup(
        self,
        monkeypatch,
        clone,
        *,
        line_exists,
        bullets=True,
        triage=(),
        had_prs=True,
        skipped=(),
        duplicate_prs=(),
        uncertain=(),
        unresolved=(),
        unresolved_backports=(),
        unresolved_prs=(),
        ai_included=(),
        guardrail_included=(),
        ai_excluded=(),
        label_excluded=(),
        impact_review=(),
        stub_contrib_base=True,
        writes=None,
    ):
        from scripts.release_notes import pipeline as pipeline_mod
        from scripts.release_notes import render as render_mod
        from scripts.release_notes.models import CategorizedBullet
        from scripts.release_notes.pipeline import RegenResult

        bl = ([CategorizedBullet(pr_number=40, author="a", category="Bug Fixes", text="fix")]
              if bullets else [])
        grouped = render_mod.group_bullets(bl)
        # Included counts every PR sent to generation, including ones for which
        # generation returned no bullet.
        included = (
            (1 if bullets else 0)
            + len(skipped)
            + len(ai_included)
            + len(guardrail_included)
        )
        monkeypatch.setattr(
            pipeline_mod, "regenerate_unreleased",
            lambda *a, **k: RegenResult(
                base_tag="9.0.0", grouped=grouped,
                included=included,
                bullet_count=sum(len(v) for v in grouped.values()),
                skipped=tuple(skipped),
                triage=tuple(triage), had_prs=had_prs,
                ai_included=tuple(ai_included),
                guardrail_included=tuple(guardrail_included),
                ai_excluded=tuple(ai_excluded),
                label_excluded=tuple(label_excluded),
                impact_review=tuple(impact_review),
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
        def _fake_git_output(repo_dir, *args, **kwargs):
            if args == ("remote", "get-url", "origin"):
                return "https://github.com/valkey-io/valkey.git"
            return "a" * 40

        monkeypatch.setattr(rc, "git_output", _fake_git_output)
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

    def test_rc1_prs_prep_branch_into_target(self, monkeypatch, clone):
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True})
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        # Exactly one PR is opened: the prep branch into the target M.m branch.
        assert len(created) == 1
        assert created[0]["head"].startswith("agent/release-cut/")
        assert created[0]["base"] == "9.1"

    def test_included_prs_but_no_bullets_aborts_without_pr(self, monkeypatch, clone):
        # The cut()-level guard: PRs were included but generation produced no
        # renderable bullets. cut() must return 1 and open no PR / push nothing,
        # rather than commit empty notes. Override _setup's RegenResult with a
        # guard-tripping one (included=1, bullet_count=0, empty grouped).
        from unittest.mock import MagicMock

        from scripts.release_notes import pipeline as pipeline_mod
        from scripts.release_notes.pipeline import RegenResult

        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True})
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
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



    def test_uppercase_ga_cuts_minor_line_with_lowercased_names(self, monkeypatch, clone):
        # End-to-end: a dispatch of "GA" must route to the M.m line and emit the
        # GA-titled commit, with the prep branch lowercased. commit_title and
        # stage_release_name do not normalize their own argument, so this proves
        # resolve_branch_plan's normalization holds across the whole cut() path.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True})
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
            valkey_clone_dir=clone, version="9.1.0", stage="GA",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        assert created[0]["base"] == "9.1"                          # M.m line, not pre-release
        assert created[0]["head"] == "agent/release-cut/9.1.0-ga"   # prep branch lowercased
        assert created[0]["title"] == "Add release notes entry for Valkey 9.1.0 GA"
        # No raw "-GA" leaks into the prep-branch push refspec.
        assert not any("9.1.0-GA" in " ".join(c) for c in calls if c[:1] == ("push",))




    def test_triage_listed_in_release_pr_body(self, monkeypatch, clone):
        from unittest.mock import MagicMock

        from scripts.release_notes.models import MergedPR
        triage = (MergedPR(number=7, title="Untagged | thing", author="bob", url="https://x/7"),)
        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True}, triage=triage)
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc2",
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
        self._setup(monkeypatch, clone, line_exists={"9.1": True},
                    unresolved=unresolved)
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc2",
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
        self._setup(monkeypatch, clone, line_exists={"9.1": True},
                    unresolved_backports=unresolved_backports)
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc2",
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
        self._setup(monkeypatch, clone, line_exists={"9.1": True},
                    unresolved_prs=unresolved_prs)
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        body = created[0]["body"]
        assert "Commits whose PR could not be fetched" in body
        assert "abcdef123456" in body  # sha truncated to 12 in the table
        assert "#777" in body          # the PR number as referenced

    def test_rc_out_of_sequence_warned_in_pr_body(self, monkeypatch, clone):
        # rc2 dispatched: the sequence warning must surface in the PR body.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True})
        monkeypatch.setattr(rc, "_warn_rc_sequence", lambda *a, **k: "rc2 out-of-seq: expected rc1")
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        body = created[0]["body"]
        assert "Release candidate out of sequence" in body
        assert "rc1" in body

    def test_in_sequence_rc_has_no_warning_in_pr_body(self, monkeypatch, clone):
        # rc1 first cut is in sequence: no warning section in the body.
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True})
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        assert "out of sequence" not in created[0]["body"]

    def _cut_body(
        self,
        monkeypatch,
        clone,
        *,
        line_exists,
        cut_kwargs,
        bullets=True,
        triage=(),
        had_prs=True,
        skipped=(),
        duplicate_prs=(),
        uncertain=(),
        ai_included=(),
        guardrail_included=(),
        ai_excluded=(),
        label_excluded=(),
        impact_review=(),
    ):
        """Run cut() with GitHub mocked and return the created PR's body."""
        from unittest.mock import MagicMock
        self._setup(monkeypatch, clone, line_exists=line_exists, bullets=bullets,
                    triage=triage, had_prs=had_prs, skipped=skipped,
                    duplicate_prs=duplicate_prs, uncertain=uncertain,
                    ai_included=ai_included, guardrail_included=guardrail_included,
                    ai_excluded=ai_excluded, label_excluded=label_excluded,
                    impact_review=impact_review)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []
        repo.create_pull.side_effect = lambda **kw: created.append(kw) or MagicMock(
            number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())
        base = dict(
            repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
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
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={})
        assert "computed over the range below" in body
        assert "mode: rc1" in body
        assert "target_branch: 9.1" in body
        assert "base: 9.0.0 @ aaaaaaaaaaaa" in body



    def test_baseline_unanchored_warned_in_body(self, monkeypatch, clone):
        body = self._cut_body(monkeypatch, clone, line_exists={"9.0": True},
                              cut_kwargs={"version": "9.0.0", "baseline_unanchored": True})
        assert "baseline is unanchored" in body

    def test_empty_range_explained_in_body(self, monkeypatch, clone):
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True},
                              cut_kwargs={}, bullets=False, had_prs=False)
        assert "Empty release notes" in body
        assert "No merged PRs were found" in body

    def test_all_undecided_empty_notes_explained_in_body(self, monkeypatch, clone):
        # No PR labelled release-notes and AI triage decided nothing (all undecided):
        # nothing was included, so the empty-notes section explains why and the
        # Needs-triage table still lists the undecided PRs.
        from scripts.release_notes.models import MergedPR
        triage = (MergedPR(number=7, title="thing", author="bob", url="https://x/7"),)
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={},
                              bullets=False, had_prs=True, triage=triage)
        assert "Empty release notes" in body
        assert "1 candidate PR(s) need human triage" in body
        assert "Needs triage" in body  # the table is still rendered

    def test_all_label_excluded_empty_notes_explained_in_body(self, monkeypatch, clone):
        from scripts.release_notes.models import TriagedPR

        label_excluded = (
            TriagedPR(
                number=9, title="internal refactor", author="dev",
                url="https://x/9", included=False,
                reason="labelled `no-release-notes`",
            ),
        )
        body = self._cut_body(
            monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={},
            bullets=False, had_prs=True, label_excluded=label_excluded,
        )

        assert "1 PR(s) were labelled `no-release-notes`" in body
        assert "0 candidate PR(s) were judged internal-only" in body
        assert "Excluded by `no-release-notes`" in body
        assert "(or add `release-notes`)" not in body

    def test_duplicate_pr_warned_in_body(self, monkeypatch, clone):
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={},
                              duplicate_prs=(40,))
        assert "noted more than once" in body
        assert "#40" in body

    def test_ai_included_listed_in_body(self, monkeypatch, clone):
        # A label-less PR AI triage added to the notes is listed with its reason so
        # a maintainer can confirm the include.
        from scripts.release_notes.models import TriagedPR
        ai_included = (TriagedPR(number=8, title="adds a config", author="dev",
                                 url="https://x/8", included=True, reason="adds CONFIG foo"),)
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={},
                              ai_included=ai_included)
        assert "AI-triaged into the notes" in body
        assert "[#8](https://x/8)" in body
        assert "adds CONFIG foo" in body

    def test_ai_excluded_listed_in_body(self, monkeypatch, clone):
        # A label-less PR AI triage dropped is listed so a maintainer can catch a
        # wrongly-dropped user-facing change; an uncertain call is marked.
        from scripts.release_notes.models import TriagedPR
        ai_excluded = (TriagedPR(number=9, title="refactor", author="dev",
                                 url="https://x/9", included=False,
                                 reason="internal refactor", uncertain=True),)
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={},
                              ai_excluded=ai_excluded)
        assert "AI-triaged out of the notes" in body
        assert "[#9](https://x/9)" in body
        assert "⚠️ internal refactor" in body  # uncertain calls are flagged

    def test_ai_triage_holds_pr_as_draft(self, monkeypatch, clone):
        # Any AI include/exclude decision holds the PR as a draft for confirmation.
        from scripts.release_notes.models import TriagedPR
        ai_included = (TriagedPR(number=8, title="t", author="d", url="https://x/8",
                                 included=True, reason="user-facing"),)
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={},
                              ai_included=ai_included)
        assert "AI triaged PRs without release-notes (confirm include/exclude)" in body

    def test_skipped_section_does_not_claim_every_pr_was_labelled(
        self, monkeypatch, clone
    ):
        body = self._cut_body(
            monkeypatch,
            clone,
            line_exists={"9.1": True},
            cut_kwargs={},
            skipped=(41,),
        )

        assert "Selected for the notes" in body
        assert "carried the `release-notes` label" not in body

    def test_uncertain_notes_flagged_in_body(self, monkeypatch, clone):
        from scripts.release_notes.models import UncertainNote
        body = self._cut_body(
            monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={},
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
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True},
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
        self._setup(monkeypatch, clone, line_exists={"9.1": True}, writes=writes)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = MagicMock(number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
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
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True},
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
        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True}, writes=writes)
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
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
        assert "embargoed or missed CVEs" in created[0]["body"]
        # cut() cleaned up its throwaway worktree (no leak).
        self._assert_worktree_removed(calls, clone)

    def test_advisory_fetch_failure_disclaimed_in_body(self, monkeypatch, clone):
        repo, created, _writes, _calls = self._advisory_repo(monkeypatch, clone, advisories=None)
        repo.get_repository_advisories.side_effect = RuntimeError("no advisory permission")
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
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
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
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
        body = self._cut_body(monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={})
        assert "⚠️" not in body
        assert "Empty release notes" not in body

    def _cut_created(
        self,
        monkeypatch,
        clone,
        *,
        line_exists,
        cut_kwargs,
        bullets=True,
        triage=(),
        had_prs=True,
        duplicate_prs=(),
        uncertain=(),
        guardrail_included=(),
        impact_review=(),
    ):
        """Run cut() with GitHub mocked and return the created PR's full kwargs.

        Like :meth:`_cut_body` but returns the whole ``create_pull`` kwargs dict so
        a test can assert on ``draft`` (the hold decision), not just the body.
        """
        from unittest.mock import MagicMock
        self._setup(monkeypatch, clone, line_exists=line_exists, bullets=bullets,
                    triage=triage, had_prs=had_prs, duplicate_prs=duplicate_prs,
                    uncertain=uncertain, guardrail_included=guardrail_included,
                    impact_review=impact_review)
        repo = MagicMock()
        repo.get_pulls.return_value = []
        created = []
        repo.create_pull.side_effect = lambda **kw: created.append(kw) or MagicMock(
            number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())
        base = dict(
            repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None,
            contrib_base_ref=None, security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        base.update(cut_kwargs)
        rc.cut(repo, **base)
        return created[0] if created else None

    def test_clean_cut_opens_ready(self, monkeypatch, clone):
        # No flagged signals: the PR opens ready (not a draft) with no hold banner.
        kw = self._cut_created(monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={})
        assert kw["draft"] is False
        assert "Held as a draft" not in kw["body"]

    def test_flagged_cut_held_as_draft(self, monkeypatch, clone):
        # A cut with a flagged signal (unanchored baseline) opens as a draft and
        # leads the body with the hold banner naming the reason.
        kw = self._cut_created(monkeypatch, clone, line_exists={"9.0": True},
                              cut_kwargs={"version": "9.0.0", "baseline_unanchored": True})
        assert kw["draft"] is True
        assert "Held as a draft" in kw["body"]
        assert "baseline is unanchored" in kw["body"]
        # The banner leads the body, before the summary line.
        assert kw["body"].index("Held as a draft") < kw["body"].index("Cuts **")

    def test_force_ready_opens_flagged_cut_ready(self, monkeypatch, clone):
        # force_ready overrides the hold: the same flagged cut opens ready, and the
        # banner records that the flags were overridden rather than held.
        kw = self._cut_created(monkeypatch, clone, line_exists={"9.0": True},
                              cut_kwargs={"version": "9.0.0", "baseline_unanchored": True,
                                           "force_ready": True})
        assert kw["draft"] is False
        assert "Opened ready despite" in kw["body"]
        assert "force_ready" in kw["body"]
        assert "Held as a draft" not in kw["body"]
        # The warning section itself still renders below the banner.
        assert "baseline is unanchored" in kw["body"]

    def test_triage_only_cut_is_held(self, monkeypatch, clone):
        # An advisory-tier signal (AI-undecided PRs, notes still non-empty) also
        # holds: any reviewer-facing signal opens the PR as a draft.
        from scripts.release_notes.models import MergedPR
        triage = (MergedPR(number=7, title="thing", author="bob", url="https://x/7"),)
        kw = self._cut_created(monkeypatch, clone, line_exists={"9.1": True}, cut_kwargs={},
                               triage=triage)
        assert kw["draft"] is True
        assert "AI triage could not decide some PRs" in kw["body"]

    def test_guardrail_inclusion_is_listed_and_held(self, monkeypatch, clone):
        from scripts.release_notes.models import TriagedPR

        guarded = (
            TriagedPR(
                number=3921,
                title="Fix crash on crafted RESTORE payload",
                author="dev",
                url="https://x/3921",
                included=True,
                reason="availability impact: crash",
                uncertain=True,
                guardrail=True,
            ),
        )
        kw = self._cut_created(
            monkeypatch,
            clone,
            line_exists={"9.1": True},
            cut_kwargs={},
            guardrail_included=guarded,
        )

        assert kw["draft"] is True
        assert "release-safety guardrail overrode AI triage" in kw["body"]
        assert "[#3921](https://x/3921)" in kw["body"]
        assert "availability impact: crash" in kw["body"]

    def test_low_urgency_impact_is_listed_and_held(self, monkeypatch, clone):
        from scripts.release_notes.models import ReleaseImpact

        impacts = (
            ReleaseImpact(
                number=4073,
                title="Fix use-after-free while loading corrupt RDB",
                url="https://x/4073",
                reason="memory-safety impact: use-after-free",
            ),
        )
        kw = self._cut_created(
            monkeypatch,
            clone,
            line_exists={"9.1": True},
            cut_kwargs={"urgency": "LOW"},
            impact_review=impacts,
        )

        assert kw["draft"] is True
        assert "release impact may require higher urgency or security treatment" in kw["body"]
        assert "Release impact and urgency need review" in kw["body"]
        assert "[#4073](https://x/4073)" in kw["body"]
        assert "requested urgency is **LOW**" in kw["body"]

    def test_high_urgency_impact_is_listed_without_urgency_hold(
        self, monkeypatch, clone
    ):
        from scripts.release_notes.models import ReleaseImpact

        impacts = (
            ReleaseImpact(
                number=4073,
                title="Fix use-after-free while loading corrupt RDB",
                url="https://x/4073",
                reason="memory-safety impact: use-after-free",
            ),
        )
        kw = self._cut_created(
            monkeypatch,
            clone,
            line_exists={"9.1": True},
            cut_kwargs={"urgency": "HIGH"},
            impact_review=impacts,
        )

        assert kw["draft"] is False
        assert "Release-impact review" in kw["body"]
        assert "requested urgency is **HIGH**" in kw["body"]
        assert "release impact may require higher urgency" not in kw["body"]

    def test_dry_run_previews_hold(self, monkeypatch, clone, capsys):
        # --dry-run shows the hold decision the real cut would make.
        from unittest.mock import MagicMock
        self._setup(monkeypatch, clone, line_exists={"9.0": True})
        repo = MagicMock()
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, version="9.0.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=True, baseline_unanchored=True,
        )
        out = capsys.readouterr().out
        assert "PR would open: DRAFT (held)" in out
        assert "baseline is unanchored" in out


    def test_recut_fetches_prep_branch_before_force_with_lease(self, monkeypatch, clone):
        # A re-cut of the same stage finds the agent-namespaced prep branch already
        # on the remote. The fresh clone never fetched it, so --force-with-lease has
        # no basis and would reject with "stale info". Assert the prep branch is
        # fetched (populating the tracking ref) immediately before the lease push.
        from unittest.mock import MagicMock
        prep = "agent/release-cut/9.1.0-rc1"
        calls = self._setup(
            monkeypatch, clone,
            line_exists={"9.1": True, prep: True},
        )
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = MagicMock(number=2, html_url="https://x/2")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
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
        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True})
        repo = MagicMock()
        repo.get_pulls.return_value = []
        repo.create_pull.return_value = MagicMock(number=1, html_url="https://x/1")
        monkeypatch.setattr(rc.publish_mod, "retry_github_call", lambda op, **k: op())

        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        prep_fetch = [c for c in calls
                      if c[:2] == ("fetch", "origin") and "refs/remotes/origin/agent/release-cut" in " ".join(c)]
        assert prep_fetch == [], prep_fetch

    def test_dry_run_pushes_nothing(self, monkeypatch, clone):
        from unittest.mock import MagicMock
        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True})
        repo = MagicMock()
        rc.cut(
            repo, repo_full_name="valkey-io/valkey", source_clone_dir=clone,
            valkey_clone_dir=clone, version="9.1.0", stage="rc1",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=True,
        )
        assert [c for c in calls if c[:1] == ("push",)] == []
        repo.create_pull.assert_not_called()

    def test_worktree_is_based_on_pinned_sha(self, monkeypatch, clone):
        from unittest.mock import MagicMock

        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True})
        rc.cut(
            MagicMock(), repo_full_name="valkey-io/valkey",
            source_clone_dir=clone, valkey_clone_dir=clone,
            version="9.1.0", stage="rc1", urgency="LOW",
            date="2026-06-25", tag_glob=None, base_ref=None,
            contrib_base_ref=None, security_fixes=None, token="t",
            git_env={}, dry_run=True,
        )
        worktree_add = next(c for c in calls if c[:2] == ("worktree", "add"))
        assert worktree_add[-1] == "a" * 40
        assert not worktree_add[-1].startswith("origin/")

    def test_dry_run_fails_if_target_advances(self, monkeypatch, clone):
        from unittest.mock import MagicMock

        self._setup(monkeypatch, clone, line_exists={"9.1": True})
        tips = iter(("a" * 40, "b" * 40))
        monkeypatch.setattr(rc, "_fetch_remote_branch_tip", lambda *a, **k: next(tips))
        with pytest.raises(RuntimeError, match="advanced during generation"):
            rc.cut(
                MagicMock(), repo_full_name="valkey-io/valkey",
                source_clone_dir=clone, valkey_clone_dir=clone,
                version="9.1.0", stage="rc1", urgency="LOW",
                date="2026-06-25", tag_glob=None, base_ref=None,
                contrib_base_ref=None, security_fixes=None, token="t",
                git_env={}, dry_run=True,
            )

    def test_real_cut_fails_before_remote_mutation_if_target_advances(
        self, monkeypatch, clone
    ):
        from unittest.mock import MagicMock

        calls = self._setup(monkeypatch, clone, line_exists={"9.1": True})
        tips = iter(("a" * 40, "b" * 40))
        monkeypatch.setattr(rc, "_fetch_remote_branch_tip", lambda *a, **k: next(tips))
        repo = MagicMock()

        with pytest.raises(RuntimeError, match="advanced during generation"):
            rc.cut(
                repo, repo_full_name="valkey-io/valkey",
                source_clone_dir=clone, valkey_clone_dir=clone,
                version="9.1.0", stage="rc1", urgency="LOW",
                date="2026-06-25", tag_glob=None, base_ref=None,
                contrib_base_ref=None, security_fixes=None, token="t",
                git_env={}, dry_run=False,
            )

        assert [c for c in calls if c[:1] == ("push",)] == []
        repo.get_pulls.assert_not_called()
        repo.create_pull.assert_not_called()

    def test_contrib_base_matches_notes_baseline(self, monkeypatch, clone):
        # The credits must span the same range as the bullets: the contributor
        # base passed to promote_and_bump equals regen.base_tag (9.0.0 here),
        # not whatever `git describe` would return from the source branch. _setup
        # leaves the real _contrib_base in place here so the wiring is exercised;
        # promote_and_bump is captured to read what it received.
        from unittest.mock import MagicMock
        self._setup(monkeypatch, clone, line_exists={"9.1": True},
                    stub_contrib_base=False)

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
            valkey_clone_dir=clone, version="9.1.0", stage="rc2",
            urgency="LOW", date="2026-06-25", tag_glob=None, base_ref=None, contrib_base_ref=None,
            security_fixes=None, token="t", git_env={}, dry_run=False,
        )
        # regen.base_tag is 9.0.0; the real _contrib_base must return it (via the
        # notes_base_ref branch), never reaching git describe.
        assert captured["contrib_base"] == "9.0.0"





class TestNotesRange:
    """The precise base/head-ref + SHA range surfaced in the PR body / dry-run."""

    _RANGE = rc._NotesRange(
        mode="rc2", source_ref="9.1",
        target_branch="9.1", base_ref="9.1.0-rc1",
        base_sha="a" * 40, head_ref="9.1", head_sha="b" * 40,
    )

    def test_plan_mode_fresh_rc1(self) -> None:
        plan = BranchPlan("rc1", "9.1", "9.1")
        assert rc._plan_mode(plan) == "rc1"



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
            "mode: rc2",
            "source_ref: 9.1",
            "target_branch: 9.1",
            "base: 9.1.0-rc1 @ aaaaaaaaaaaa",
            "head: 9.1 @ bbbbbbbbbbbb",
        ]

    def test_body_section_renders_fenced_block(self) -> None:
        section = rc._notes_range_body_section(self._RANGE, regen=None)
        assert "`9.1.0-rc1..9.1`" in section
        assert "mode: rc2" in section
        assert "base: 9.1.0-rc1 @ aaaaaaaaaaaa" in section
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
        plan = BranchPlan("rc2", "9.1", "9.1")
        regen = MagicMock(base_tag="unstable")  # a resolvable ref for this fixture repo
        # head_ref is the ref discovery actually walked (the line tip on a
        # continuing cut); here both ends point at the fixture's single commit.
        rng = rc._resolve_notes_range(
            repo, plan, head_ref="unstable", regen=regen
        )
        assert rng.mode == "rc2"
        assert rng.target_branch == "9.1"
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

    _GA_PLAN = BranchPlan("ga", "9.1", "9.1")

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
        monkeypatch.setattr(rcmod, "_remote_branch_exists", lambda d, b: b == "9.1")
        monkeypatch.setattr(rcmod, "run_git", lambda *a, **k: None)
        monkeypatch.setattr(
            rcmod,
            "git_output",
            lambda _repo, *args, **k: (
                "https://github.com/valkey-io/valkey.git"
                if args == ("remote", "get-url", "origin")
                else "a" * 40
            ),
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
            valkey_clone_dir=clone, version="9.1.0",
            stage="ga", urgency="LOW", date="2026-06-29", tag_glob=None,
            base_ref=None, contrib_base_ref=None, security_fixes=None,
            token="t", git_env={}, dry_run=True,
        )
        # #44 was dropped before render saw the grouped bullets.
        all_lines = [line for lines in captured["grouped"].values() for line in lines]
        assert not any("(#44)" in line for line in all_lines)
        assert captured["already"] == [44]









class TestSecurityOnlyCutNotEmpty:
    """A cut carrying only security_fixes must not be flagged empty or held."""

    _RC_PLAN = BranchPlan("rc1", "9.1", "9.1")
    _GA_PLAN = BranchPlan("ga", "9.1", "9.1")

    @staticmethod
    def _regen(bullet_count=0, had_prs=False, triage=()):
        from types import SimpleNamespace
        return SimpleNamespace(
            bullet_count=bullet_count, had_prs=had_prs, triage=triage,
            included=0, skipped=(), duplicate_prs=(), uncertain=(),
            ai_included=(), guardrail_included=(), ai_excluded=(),
            label_excluded=(), impact_review=(),
            unresolved=(), unresolved_backports=(), unresolved_prs=(),
            unresolved_cherry_picks=(), collided=(), reverted=(), base_tag="9.0.0",
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
