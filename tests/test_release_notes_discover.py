"""Tests for release-range discovery.

Builds real local git repositories in ``tmp_path`` (commits with ``(#N)``
subjects, tags) to exercise tag resolution, range listing, and PR dedup; the
commit->PR API fallback and PR hydration are tested against MagicMock repos.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.common.proc import git_output, run_git
from scripts.release_notes import discover as discover_mod
from scripts.release_notes.discover import (
    _MAX_PR_BODY_CHARS,
    _clean_pr_body,
    _pr_from_commit_api,
    _reconcile_cherry_pick_suspects,
    _resolve_base_ref,
    hydrate_prs,
    list_range_commits,
    resolve_commit_prs,
    resolve_last_tag,
    resolve_previous_release_tag,
)
from scripts.release_notes.models import (
    MergedPR,
    UnresolvedBackport,
    UnresolvedCherryPick,
    UnresolvedPR,
)


def _init_repo(path) -> str:
    repo = str(path)
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    return repo


def _commit(repo: str, subject: str) -> str:
    # Empty commits keep the test fast and the subject is all discovery reads.
    run_git(repo, "commit", "-q", "--allow-empty", "-m", subject)
    return git_output(repo, "rev-parse", "HEAD").strip()


def _commit_with_body(repo: str, subject: str, body: str) -> str:
    # A commit whose body carries a trailer (e.g. a -x cherry-pick), so
    # list_range_commits surfaces the body to resolve_commit_prs.
    run_git(repo, "commit", "-q", "--allow-empty", "-m", subject, "-m", body)
    return git_output(repo, "rev-parse", "HEAD").strip()


class TestResolveLastTag:
    def test_returns_reachable_tag(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "old (#1)")
        run_git(repo, "tag", "9.1.0-rc1")
        _commit(repo, "newer (#2)")
        tag, sha = resolve_last_tag(repo, "main")
        assert tag == "9.1.0-rc1"
        assert sha == git_output(repo, "rev-list", "-n", "1", "9.1.0-rc1").strip()

    def test_picks_highest_version_of_several(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.1.0-rc1")
        _commit(repo, "b (#2)")
        run_git(repo, "tag", "9.1.0-rc2")
        _commit(repo, "c (#3)")
        tag, _ = resolve_last_tag(repo, "main")
        assert tag == "9.1.0-rc2"

    def test_picks_highest_version_not_graph_nearest(self, tmp_path) -> None:
        # The lower-version tag is the graph-nearer ancestor (what `git describe
        # --abbrev=0` would return), but the higher version is the correct
        # baseline. Selection must be by version, not graph distance.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.1.0")
        _commit(repo, "b (#2)")
        run_git(repo, "tag", "9.0.9")  # lower version, but nearer to head
        tag, _ = resolve_last_tag(repo, "main")
        assert tag == "9.1.0"

    def test_ga_ranks_above_its_rc(self, tmp_path) -> None:
        # A bare M.m.p GA outranks any -rcN of the same M.m.p, regardless of the
        # order the tags were created in.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.1.0")
        run_git(repo, "tag", "9.1.0-rc2")
        tag, _ = resolve_last_tag(repo, "main")
        assert tag == "9.1.0"

    def test_cross_line_merge_does_not_pick_sibling_tag(self, tmp_path) -> None:
        # A sibling release line (8.2.0) merged into this line is graph-nearer to
        # head than this line's own 8.1.9 tag. Without a glob, version-max picks
        # the higher 8.2.0; the M.m.* glob a patch GA passes restricts candidates
        # to this line, so 8.1.9 wins and the range never spans the sibling line.
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "8.1.9")
        run_git(repo, "checkout", "-q", "-b", "side")
        _commit(repo, "sibling (#2)")
        run_git(repo, "tag", "8.2.0")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "merge", "-q", "--no-ff", "side", "-m", "merge sibling")
        tag, _ = resolve_last_tag(repo, "main", tag_glob="8.1.*")
        assert tag == "8.1.9"

    def test_glob_restricts_line(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.1.0-rc1")
        _commit(repo, "b (#2)")
        run_git(repo, "tag", "8.0.0")  # different line, higher version
        tag, _ = resolve_last_tag(repo, "main", tag_glob="9.1.*")
        assert tag == "9.1.0-rc1"

    def test_tolerates_v_prefix(self, tmp_path) -> None:
        # A `v`-prefixed tag still parses and is selected on version.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "v9.1.0")
        tag, _ = resolve_last_tag(repo, "main")
        assert tag == "v9.1.0"

    def test_v_prefixed_tag_matches_bare_glob(self, tmp_path) -> None:
        # The GA/rc line globs are bare (`8.1.*`), but _TAG_RE tolerates a `v`
        # prefix. A `v8.1.8` tag must still count as a member of the `8.1.*` line;
        # the glob is applied in Python (not `git tag --list`, which matches the
        # literal name and would silently drop the v-prefixed tag).
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "v8.1.8")
        tag, _ = resolve_last_tag(repo, "main", tag_glob="8.1.*")
        assert tag == "v8.1.8"

    def test_v_prefixed_glob_still_excludes_sibling_line(self, tmp_path) -> None:
        # Tolerating the `v` prefix must not widen the glob: a sibling line's
        # v-prefixed tag (v8.2.0) is still excluded by the `8.1.*` line glob.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "v8.1.8")
        _commit(repo, "b (#2)")
        run_git(repo, "tag", "v8.2.0")  # sibling line, higher version, v-prefixed
        tag, _ = resolve_last_tag(repo, "main", tag_glob="8.1.*")
        assert tag == "v8.1.8"

    def test_skips_unparseable_tags(self, tmp_path) -> None:
        # A tag that is not a release version (a moved marker, a feature tag) must
        # not be selected or mis-ordered by string comparison: it is skipped, and
        # the real release tag wins even though it sorts lower lexically.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.1.0")
        run_git(repo, "tag", "nightly")  # not M.m.p -> skipped
        tag, _ = resolve_last_tag(repo, "main")
        assert tag == "9.1.0"

    def test_no_tag_raises(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "only (#1)")
        with pytest.raises(ValueError):
            resolve_last_tag(repo, "main")

    def test_no_parseable_tag_raises(self, tmp_path) -> None:
        # Reachable tags exist but none is a release version -> no baseline.
        repo = _init_repo(tmp_path)
        _commit(repo, "only (#1)")
        run_git(repo, "tag", "nightly")
        with pytest.raises(ValueError):
            resolve_last_tag(repo, "main")

    def test_timeout_propagates_not_masked_as_no_tag(self, tmp_path, monkeypatch) -> None:
        # A hung `git tag` (TimeoutExpired) is an operational failure, not "no
        # baseline tag": it must propagate, not be disguised as a missing tag that
        # would send the caller to a wrong baseline. Only a non-zero exit
        # (CalledProcessError) maps to the "no tag reachable" ValueError.
        import subprocess

        repo = _init_repo(tmp_path)
        _commit(repo, "only (#1)")

        def _hang(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git tag", timeout=300)

        monkeypatch.setattr(discover_mod, "git_output", _hang)
        with pytest.raises(subprocess.TimeoutExpired):
            resolve_last_tag(repo, "main")


class TestResolvePreviousReleaseTag:
    def test_picks_highest_release_below_target(self, tmp_path) -> None:
        # The previous-release baseline for an rc1: highest release tag strictly
        # below the target version. 9.0.2 wins over 9.0.0/9.0.1.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.0.0")
        run_git(repo, "tag", "9.0.1")
        run_git(repo, "tag", "9.0.2")
        tag, sha = resolve_previous_release_tag(repo, "9.1.0")
        assert tag == "9.0.2"
        assert sha == git_output(repo, "rev-list", "-n", "1", "9.0.2").strip()

    def test_considers_unreachable_tags(self, tmp_path) -> None:
        # valkey's fork-at-freeze model: the previous release is tagged on its own
        # branch, not reachable from the source branch. A --merged-style resolver
        # would miss it; this one lists all tags and still finds it.
        repo = _init_repo(tmp_path)
        _commit(repo, "fork base (#1)")
        # Previous release line, tagged on a side branch (not merged into main).
        run_git(repo, "checkout", "-q", "-b", "rel-9.0")
        _commit(repo, "9.0 ga (#2)")
        run_git(repo, "tag", "9.0.0")
        run_git(repo, "checkout", "-q", "main")
        _commit(repo, "9.1 dev (#3)")
        tag, _ = resolve_previous_release_tag(repo, "9.1.0")
        assert tag == "9.0.0"

    def test_skipped_minor_resolves_prior_line(self, tmp_path) -> None:
        # A minor was skipped (8.2 -> 9.1, no 9.0 line). Arithmetic on the version
        # would guess a non-existent 9.0.0; this resolver finds the real prior
        # release 8.2.0 by version comparison.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "8.2.0")
        tag, _ = resolve_previous_release_tag(repo, "9.1.0")
        assert tag == "8.2.0"

    def test_new_major_resolves_prior_major(self, tmp_path) -> None:
        # rc1 of 9.0.0: no 9.x below it, but the prior major's last release is the
        # correct baseline. Arithmetic returned None (unanchored) for this.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "8.2.5")
        tag, _ = resolve_previous_release_tag(repo, "9.0.0")
        assert tag == "8.2.5"

    def test_excludes_target_own_prereleases(self, tmp_path) -> None:
        # The target's own rc tags share its M.m.p and must not be selected as the
        # baseline (that would make the range empty/negative). Only tags strictly
        # below M.m.p qualify.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.0.0")
        run_git(repo, "tag", "9.1.0-rc1")  # same M.m.p as target -> excluded
        tag, _ = resolve_previous_release_tag(repo, "9.1.0")
        assert tag == "9.0.0"

    def test_patch_rc1_anchors_to_prior_patch(self, tmp_path) -> None:
        # rc1 of a patch (9.2.3) anchors to the highest release below it: 9.2.2,
        # not the previous minor, so the range does not re-credit the 9.2.x line.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.1.0")
        run_git(repo, "tag", "9.2.0")
        run_git(repo, "tag", "9.2.1")
        run_git(repo, "tag", "9.2.2")
        tag, _ = resolve_previous_release_tag(repo, "9.2.3")
        assert tag == "9.2.2"

    def test_ga_ranks_above_rc_of_same_mmp(self, tmp_path) -> None:
        # When both a GA and rcs of the prior M.m.p exist, the GA is the baseline
        # (a pre-release ranks below its release).
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.0.0-rc1")
        run_git(repo, "tag", "9.0.0-rc2")
        run_git(repo, "tag", "9.0.0")
        tag, _ = resolve_previous_release_tag(repo, "9.1.0")
        assert tag == "9.0.0"

    def test_skips_unparseable_tags(self, tmp_path) -> None:
        # Non-release tags are ignored, not string-sorted.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.0.0")
        run_git(repo, "tag", "nightly")
        run_git(repo, "tag", "zzz-marker")
        tag, _ = resolve_previous_release_tag(repo, "9.1.0")
        assert tag == "9.0.0"

    def test_tolerates_v_prefix(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "v9.0.0")
        tag, _ = resolve_previous_release_tag(repo, "9.1.0")
        assert tag == "v9.0.0"

    def test_no_earlier_release_returns_none(self, tmp_path) -> None:
        # First release ever (or a tagless fork): nothing below the target.
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        assert resolve_previous_release_tag(repo, "9.0.0") is None

    def test_only_higher_or_equal_tags_returns_none(self, tmp_path) -> None:
        # Tags exist but none is strictly below the target (all >= M.m.p).
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.1.0")
        run_git(repo, "tag", "9.2.0")
        assert resolve_previous_release_tag(repo, "9.1.0") is None

    def test_malformed_target_returns_none(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "a (#1)")
        run_git(repo, "tag", "9.0.0")
        assert resolve_previous_release_tag(repo, "not-a-version") is None

    def test_range_from_resolved_base_is_new_line_only(self, tmp_path) -> None:
        # End-to-end range property under fork-at-freeze: the resolved previous
        # release tag lives on a side branch and is not reachable from head, yet
        # base..head yields exactly the new line's commits (the side-branch-only
        # release commits drop out). This is why reachability is not required.
        repo = _init_repo(tmp_path)
        _commit(repo, "shared freeze point (#1)")
        run_git(repo, "checkout", "-q", "-b", "rel-9.0")
        _commit(repo, "9.0 branch-only ga commit (#2)")
        run_git(repo, "tag", "9.0.0")
        run_git(repo, "checkout", "-q", "main")
        _commit(repo, "9.1 feat A (#3)")
        _commit(repo, "9.1 feat B (#4)")
        tag, _ = resolve_previous_release_tag(repo, "9.1.0")
        commits = list_range_commits(repo, tag, "main")
        subjects = [s for _, s, _ in commits]
        # Only the 9.1 work; the 9.0 branch-only commit (#2) is excluded.
        assert subjects == ["9.1 feat A (#3)", "9.1 feat B (#4)"]


class TestListRangeCommits:
    def test_lists_range_oldest_first(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        _commit(repo, "first (#2)")
        _commit(repo, "second (#3)")
        commits = list_range_commits(repo, "base", "main")
        subjects = [s for _, s, _ in commits]
        assert subjects == ["first (#2)", "second (#3)"]

    def test_excludes_base(self, tmp_path) -> None:
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        commits = list_range_commits(repo, "base", "main")
        assert commits == []

    def test_captures_multiline_body(self, tmp_path) -> None:
        # The body is carried alongside the subject so discovery can recover the
        # original PR from an ## Applied table or a -x trailer; a multi-line body
        # (the common case) must survive the record split intact.
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        run_git(
            repo, "commit", "-q", "--allow-empty",
            "-m", "squash backport (#50)",
            "-m", "## Applied\n\n| Source PR | Title |\n|---|---|\n| #10 | feat |",
        )
        commits = list_range_commits(repo, "base", "main")
        assert len(commits) == 1
        _sha, subject, body = commits[0]
        assert subject == "squash backport (#50)"
        assert "## Applied" in body and "#10" in body

    def test_merge_merged_pr_intermediates_are_dropped(self, tmp_path) -> None:
        # A PR merged with a merge commit (not squashed) puts its work-in-progress
        # commits on the branch side. The first-parent walk lists only the merge
        # commit, so an intermediate whose subject ends in an unrelated (#N) (here
        # "Revert accidental change from (#111)") cannot leak a phantom note.
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        run_git(repo, "checkout", "-q", "-b", "feature")
        _commit(repo, "implement thing")
        _commit(repo, "Revert accidental change from (#111)")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "merge", "-q", "--no-ff", "feature",
                "-m", "Merge pull request #42 from x/feature")
        subjects = [s for _, s, _ in list_range_commits(repo, "base", "main")]
        assert subjects == ["Merge pull request #42 from x/feature"]
        assert not any("#111" in s for s in subjects)  # phantom never surfaced

    def test_sweep_merge_splices_second_parent_sources(self, tmp_path) -> None:
        # A merge-merged backport sweep carries each source PR's (#N) only on its
        # branch (second-parent) side. Those cherry-picks must survive the
        # first-parent walk, spliced in in place of the sweep merge, or the whole
        # sweep would collapse to its un-attributable merge commit. The merge commit
        # itself is dropped: its sources are now enumerated and the backport-labeled
        # container PR is never a note, so keeping it would mint a phantom note plus
        # a false-positive unresolved-backport flag for a sweep that resolved cleanly.
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        run_git(repo, "checkout", "-q", "-b", "agent/backport/sweep/9.0")
        _commit(repo, "Fix alpha (#201)")
        _commit(repo, "Fix beta (#202)")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "merge", "-q", "--no-ff", "agent/backport/sweep/9.0",
                "-m", "Merge pull request #500 from valkey-io/agent/backport/sweep/9.0")
        subjects = [s for _, s, _ in list_range_commits(repo, "base", "main")]
        # Only the spliced sources remain, oldest first; the sweep merge is dropped.
        assert subjects == [
            "Fix alpha (#201)",
            "Fix beta (#202)",
        ]

    def test_non_sweep_merge_intermediates_stay_excluded(self, tmp_path) -> None:
        # The splice is scoped to sweep merges: an ordinary merge with a clean
        # (#N)-bearing intermediate on its branch keeps only its merge commit, so a
        # feature-branch commit ending in a real (#N) is not double-credited.
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        run_git(repo, "checkout", "-q", "-b", "feature")
        _commit(repo, "earlier work landed as (#77)")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "merge", "-q", "--no-ff", "feature",
                "-m", "Merge pull request #78 from x/feature")
        subjects = [s for _, s, _ in list_range_commits(repo, "base", "main")]
        assert subjects == ["Merge pull request #78 from x/feature"]

    def test_squash_and_merge_commits_coexist_in_order(self, tmp_path) -> None:
        # Mixed history: a merge-merged PR, then a squash PR. First-parent order is
        # preserved and each contributes exactly one mainline commit.
        repo = _init_repo(tmp_path)
        _commit(repo, "base (#1)")
        run_git(repo, "tag", "base")
        run_git(repo, "checkout", "-q", "-b", "feature")
        _commit(repo, "wip")
        run_git(repo, "checkout", "-q", "main")
        run_git(repo, "merge", "-q", "--no-ff", "feature",
                "-m", "Merge pull request #42 from x/feature")
        _commit(repo, "Add g feature (#43)")
        subjects = [s for _, s, _ in list_range_commits(repo, "base", "main")]
        assert subjects == [
            "Merge pull request #42 from x/feature",
            "Add g feature (#43)",
        ]


class TestResolveCommitPrs:
    def test_subject_parse_and_dedup(self, tmp_path) -> None:
        # Two commits carrying the same trailing (#N) for the *same* change (a
        # tool-made backport preserves the source subject verbatim) collapse to one
        # PR and are not flagged as a collision.
        commits = [
            ("sha1", "feature (#10)", ""),
            ("sha2", "feature (#10)", ""),
            ("sha3", "fix (#11)", ""),
        ]
        repo = MagicMock()
        pr_to_sha, unresolved, _suspects, collided = resolve_commit_prs(repo, commits)
        assert set(pr_to_sha) == {10, 11}
        assert pr_to_sha[10] == "sha1"  # first occurrence wins
        assert unresolved == []
        assert collided == []  # same change, correct collapse, no false positive
        repo.get_commit.assert_not_called()  # subject parse never hit the API

    def test_reused_subject_ref_on_distinct_change_is_collided(self, tmp_path) -> None:
        # The #3380 shape: a feature commit and an unrelated follow-up (a comment
        # fixup) both end (#3380) via the ambiguous subject tier. The first wins the
        # credit; the second is a *distinct* change and must be surfaced as collided,
        # not silently dropped (it resolved to a number, so it would otherwise vanish
        # past the label-only gate).
        commits = [
            ("shaFeat", "Add cluster slot migration (#3380)", ""),
            ("shaFix", "Fix a comment typo in cluster.c (#3380)", ""),
        ]
        repo = MagicMock()
        pr_to_sha, unresolved, _suspects, collided = resolve_commit_prs(repo, commits)
        assert pr_to_sha == {3380: "shaFeat"}  # first (feature) wins the credit
        assert unresolved == []
        assert [(c.sha, c.number, c.kept_sha) for c in collided] == [
            ("shaFix", 3380, "shaFeat")
        ]
        repo.get_commit.assert_not_called()  # both resolved offline via subject tier

    def test_api_tier_collision_is_not_flagged(self, tmp_path) -> None:
        # A multi-commit PR the commit->PR API maps to one number (no subject ref on
        # either commit) is a correct collapse, not a reused-ref collision: the API
        # tier is trusted, so nothing is flagged even though two distinct SHAs share
        # the number.
        repo = MagicMock()
        repo.get_commit.return_value.get_pulls.return_value = [MagicMock(number=42)]
        commits = [
            ("shaA", "first commit of PR, no ref", ""),
            ("shaB", "second commit of PR, no ref", ""),
        ]
        pr_to_sha, unresolved, _suspects, collided = resolve_commit_prs(repo, commits)
        assert pr_to_sha == {42: "shaA"}
        assert unresolved == []
        assert collided == []  # API-tier collapse is trusted, never flagged

    def test_applied_table_collision_is_not_flagged(self, tmp_path) -> None:
        # Two sweep commits whose ## Applied tables both name source #10 (e.g. a
        # re-run sweep) collapse on the applied tier, which is trusted; not flagged.
        body = (
            "sweep\n\n## Applied\n\n"
            "| Source PR | Title | Detail |\n|---|---|---|\n| #10 | feat | clean |\n"
        )
        repo = MagicMock()
        commits = [("shaS1", "sweep a (#50)", body), ("shaS2", "sweep b (#51)", body)]
        pr_to_sha, unresolved, _suspects, collided = resolve_commit_prs(repo, commits)
        assert pr_to_sha == {10: "shaS1"}
        assert collided == []  # applied-tier collapse is trusted, never flagged

    def test_non_subject_winner_with_distinct_subject_loser_is_collided(self, tmp_path) -> None:
        # The winner resolves via API (tier 4, no subject (#N)), then a later commit
        # reuses that same number via the ambiguous subject tier on a distinct change.
        # Before the fix, the loser was silently dropped because the collision guard
        # only fired for subject-tier winners.
        repo = MagicMock()
        repo.get_commit.return_value.get_pulls.return_value = [MagicMock(number=42)]
        commits = [
            ("shaAPI", "direct push, no ref", ""),           # wins #42 via API
            ("shaSubj", "Unrelated follow-up fix (#42)", ""),  # distinct change reusing (#42)
        ]
        pr_to_sha, unresolved, _suspects, collided = resolve_commit_prs(repo, commits)
        assert pr_to_sha == {42: "shaAPI"}
        assert unresolved == []
        assert [(c.sha, c.number, c.kept_sha) for c in collided] == [
            ("shaSubj", 42, "shaAPI"),
        ]

    def test_cherry_pick_trailer_winner_with_distinct_subject_loser_is_collided(self) -> None:
        # The winner resolves via -x trailer (tier 2), claiming #42. A later commit
        # reuses #42 via the subject tier with a different change.
        repo = MagicMock()
        repo.get_commit.return_value.get_pulls.return_value = [MagicMock(number=42)]
        body_with_trailer = "(cherry picked from commit abc123def456)\n"
        commits = [
            ("shaTrailer", "port fix, no trailing ref", body_with_trailer),  # wins #42 via -x
            ("shaSubj", "Totally different change (#42)", ""),               # distinct loser
        ]
        pr_to_sha, unresolved, _suspects, collided = resolve_commit_prs(repo, commits)
        assert 42 in pr_to_sha
        assert [(c.sha, c.number) for c in collided] == [("shaSubj", 42)]

    def test_non_subject_winner_same_change_subject_loser_is_not_collided(self) -> None:
        # When the loser is a re-pick of the same change (matching subject), it should
        # collapse silently even though the winner used a non-subject tier.
        repo = MagicMock()
        repo.get_commit.return_value.get_pulls.return_value = [MagicMock(number=42)]
        commits = [
            ("shaAPI", "Add cluster slot migration (#42)", ""),  # wins via subject
            ("shaRepick", "Add cluster slot migration (#42)", ""),  # same change re-picked
        ]
        pr_to_sha, unresolved, _suspects, collided = resolve_commit_prs(repo, commits)
        assert pr_to_sha == {42: "shaAPI"}
        assert collided == []  # same subject -> correct collapse, no flag

    def test_api_fallback_when_no_trailing_ref(self) -> None:
        repo = MagicMock()
        pull = MagicMock(number=77)
        repo.get_commit.return_value.get_pulls.return_value = [pull]
        pr_to_sha, unresolved, _suspects, _collided = resolve_commit_prs(repo, [("shaX", "direct push, no ref", "")])
        assert pr_to_sha == {77: "shaX"}
        assert unresolved == []

    def test_commit_with_no_pr_is_unresolved_not_dropped(self) -> None:
        # A commit that resolves to no PR must surface as unresolved (a shipped
        # change that would otherwise vanish past the label-only gate), not be
        # silently dropped.
        repo = MagicMock()
        repo.get_commit.return_value.get_pulls.return_value = []
        pr_to_sha, unresolved, _suspects, _collided = resolve_commit_prs(repo, [("shaX", "no ref and no pr", "")])
        assert pr_to_sha == {}
        assert [(u.sha, u.subject) for u in unresolved] == [("shaX", "no ref and no pr")]

    def test_revert_uses_trailing_not_inner_ref(self) -> None:
        # 'Revert "X (#3)" (#9)' belongs to PR 9, not 3.
        repo = MagicMock()
        pr_to_sha, _, _suspects, _collided = resolve_commit_prs(repo, [("sha", 'Revert "X (#3)" (#9)', "")])
        assert set(pr_to_sha) == {9}

    def test_api_failure_is_unresolved_without_aborting(self) -> None:
        # The riskiest branch: the commit has no trailing (#N) so the API fallback
        # runs, and the API call itself raises (rate limit / network error surviving
        # retries). A RuntimeError is non-retryable, so retry_github_call re-raises
        # at once (no sleeps). The failure must be swallowed to an unresolved commit
        # (discovery keeps going), not propagated to abort the whole run.
        repo = MagicMock()
        repo.get_commit.side_effect = RuntimeError("500 upstream error")
        commits = [
            ("shaX", "hand-applied cherry-pick, no ref", ""),
            ("sha2", "fix (#11)", ""),
        ]
        pr_to_sha, unresolved, _suspects, _collided = resolve_commit_prs(repo, commits)
        # shaX unresolved; the well-formed commit is still resolved.
        assert pr_to_sha == {11: "sha2"}
        assert [u.sha for u in unresolved] == ["shaX"]

    def test_applied_table_recovers_original_over_backport_subject(self) -> None:
        # A squash-merged sweep: the subject is the backport PR (#50), but the
        # ## Applied table names the original source PRs. The originals must win;
        # the backport number must not appear, and the API is never consulted.
        body = (
            "Backport sweep\n\n"
            "## Applied\n\n"
            "| Source PR | Title | Detail |\n"
            "|---|---|---|\n"
            "| #10 | feat a | clean |\n"
            "| #11 | fix b | clean |\n"
        )
        repo = MagicMock()
        pr_to_sha, unresolved, _suspects, _collided = resolve_commit_prs(repo, [("shaSquash", "sweep (#50)", body)])
        assert set(pr_to_sha) == {10, 11}
        assert 50 not in pr_to_sha
        assert unresolved == []
        repo.get_commit.assert_not_called()

    def test_cherry_pick_trailer_recovers_original(self) -> None:
        # A -x cherry-pick whose subject is the backport PR (#60): the trailer
        # names the source commit, whose PR (#12) is the original. The trailer is
        # tried before the subject, so #12 wins over #60.
        body = "port fix\n\n(cherry picked from commit abcdef1234567890)"
        repo = MagicMock()
        pull = MagicMock(number=12)
        repo.get_commit.return_value.get_pulls.return_value = [pull]
        pr_to_sha, unresolved, _suspects, _collided = resolve_commit_prs(repo, [("shaPick", "port fix (#60)", body)])
        assert set(pr_to_sha) == {12}
        assert 60 not in pr_to_sha
        assert unresolved == []
        repo.get_commit.assert_called_once_with("abcdef1234567890")

    def test_cherry_pick_trailer_prefers_oldest_hop(self) -> None:
        # Picked through several branches (unstable -> 9.0 -> 8.0): git -x appends,
        # so the file lists the oldest hop first, most-recent last. The oldest hop
        # (1111...) is the original commit; the newest (2222...) is an intermediate
        # backport. We must resolve the oldest first and credit the original PR
        # (#13), not the intermediate backport PR (#99).
        body = (
            "port fix (#70)\n\n"
            "(cherry picked from commit 1111111111111111)\n"
            "(cherry picked from commit 2222222222222222)\n"
        )
        per_sha = {
            "1111111111111111": [MagicMock(number=13)],  # original
            "2222222222222222": [MagicMock(number=99)],  # intermediate backport
        }
        repo = MagicMock()
        repo.get_commit.side_effect = lambda sha: MagicMock(
            get_pulls=MagicMock(return_value=per_sha[sha])
        )
        pr_to_sha, _, _suspects, _collided = resolve_commit_prs(repo, [("shaPick", "port fix (#70)", body)])
        assert set(pr_to_sha) == {13}
        assert 99 not in pr_to_sha
        # Oldest hop (first in file) is resolved first and wins on the first hit,
        # so the intermediate hop is never looked up.
        repo.get_commit.assert_called_once_with("1111111111111111")

    def test_falls_back_to_subject_when_trailer_unresolvable(self) -> None:
        # A -x trailer whose source commit the API can't resolve (not in this
        # repo) must fall through to the subject (#80), not become unresolved.
        body = "port fix (#80)\n\n(cherry picked from commit deadbeefdeadbeef)"
        repo = MagicMock()
        repo.get_commit.return_value.get_pulls.return_value = []
        pr_to_sha, unresolved, suspects, _collided = resolve_commit_prs(repo, [("shaPick", "port fix (#80)", body)])
        assert set(pr_to_sha) == {80}
        assert unresolved == []
        # The credit fell through an unresolvable -x trailer: #80 is recorded as a
        # cherry-pick suspect, carrying the source SHA that could not be resolved.
        assert set(suspects) == {80}
        assert suspects[80].sha == "shaPick"
        assert suspects[80].source_shas == ("deadbeefdeadbeef",)

    def test_no_suspect_when_trailer_resolves(self) -> None:
        # A -x trailer that did resolve is credited via the trailer itself (#12),
        # so it is not a suspect: nothing to confirm by hand.
        body = "port fix\n\n(cherry picked from commit abcdef1234567890)"
        repo = MagicMock()
        repo.get_commit.return_value.get_pulls.return_value = [MagicMock(number=12)]
        _pr_to_sha, _unresolved, suspects, _collided = resolve_commit_prs(repo, [("shaPick", "port fix (#60)", body)])
        assert suspects == {}

    def test_no_suspect_without_trailer(self) -> None:
        # A plain merge with no cherry-pick trailer is credited from its subject
        # with full confidence: no suspect.
        _pr_to_sha, _unresolved, suspects, _collided = resolve_commit_prs(
            MagicMock(), [("sha", "feature (#10)", "")]
        )
        assert suspects == {}

    def test_suspect_when_trailer_unresolvable_and_api_credits(self) -> None:
        # An unresolvable -x trailer with no subject (#N): the credit falls all the
        # way to the commit->PR API (#90). The first get_commit (source SHA) yields
        # no pulls; the second (the range SHA) yields #90. Still a suspect: the
        # confident source signal was unreachable.
        body = "port fix\n\n(cherry picked from commit deadbeefdeadbeef)"
        repo = MagicMock()
        repo.get_commit.side_effect = lambda sha: MagicMock(
            get_pulls=MagicMock(
                return_value=[MagicMock(number=90)] if sha == "shaPick" else []
            )
        )
        pr_to_sha, _unresolved, suspects, _collided = resolve_commit_prs(repo, [("shaPick", "no subject ref", body)])
        assert set(pr_to_sha) == {90}
        assert set(suspects) == {90}
        assert suspects[90].source_shas == ("deadbeefdeadbeef",)


class TestPrFromCommitApi:
    def test_returns_none_on_api_error_instead_of_raising(self) -> None:
        # Isolated: a lookup failure returns None (caller drops the commit) rather
        # than letting the exception escape _pr_from_commit_api.
        repo = MagicMock()
        repo.get_commit.side_effect = RuntimeError("network down")
        assert _pr_from_commit_api(repo, "deadbeef") is None


class TestCleanPrBody:
    def test_none_and_empty_become_empty(self) -> None:
        assert _clean_pr_body(None) == ""
        assert _clean_pr_body("") == ""
        assert _clean_pr_body("   \n  ") == ""

    def test_non_string_body_becomes_empty(self) -> None:
        # PyGithub types body as str, but a mis-parsed payload could hand back a
        # non-string; it must degrade to "" rather than crash the cut.
        from unittest.mock import MagicMock
        assert _clean_pr_body(MagicMock()) == ""
        assert _clean_pr_body(123) == ""

    def test_strips_html_comments(self) -> None:
        # PR templates render guidance/checklists as HTML comments; drop them.
        body = "Real summary.\n<!-- please fill this in\nmultiline -->\nMore text."
        cleaned = _clean_pr_body(body)
        assert "please fill this in" not in cleaned
        assert "Real summary." in cleaned
        assert "More text." in cleaned

    def test_strips_dco_trailers(self) -> None:
        body = "Fixes a bug.\n\nSigned-off-by: Jane Dev <jane@example.com>\nCo-authored-by: Bob <bob@x>"
        cleaned = _clean_pr_body(body)
        assert "Fixes a bug." in cleaned
        assert "Signed-off-by" not in cleaned
        assert "Co-authored-by" not in cleaned

    def test_collapses_blank_runs_left_by_removals(self) -> None:
        # Removing a comment between paragraphs must not leave a 3-blank-line gap.
        body = "Para one.\n\n<!-- comment -->\n\nPara two."
        assert _clean_pr_body(body) == "Para one.\n\nPara two."

    def test_short_body_untouched_except_strip(self) -> None:
        assert _clean_pr_body("  Just a summary.  ") == "Just a summary."

    def test_truncates_long_body_on_word_boundary(self) -> None:
        # A long body is clipped to the cap and gets an ellipsis; the cut lands on
        # a space so the last token is whole (no "wor…" split) since whitespace is
        # frequent near the boundary. Every token is "alpha", so a correct
        # boundary cut ends in a complete "alpha…", never a fragment.
        body = ("alpha " * 1000).strip()  # ~6000 chars, spaces throughout
        cleaned = _clean_pr_body(body)
        assert len(cleaned) <= _MAX_PR_BODY_CHARS + 1  # +1 for the ellipsis char
        assert cleaned.endswith("alpha…")  # whole final token, not a split fragment
        # Body (sans ellipsis) is only whole "alpha" tokens joined by spaces.
        assert set(cleaned[:-1].split()) == {"alpha"}

    def test_truncates_hard_when_no_late_whitespace(self) -> None:
        # A single giant token with no nearby space must still be capped, not left
        # far short by chasing a word boundary that isn't there.
        body = "x" * 5000
        cleaned = _clean_pr_body(body)
        # No usable boundary near the cap, so it clips at the cap (plus ellipsis).
        assert len(cleaned) == _MAX_PR_BODY_CHARS + 1
        assert cleaned.endswith("…")


class TestHydratePrs:
    def test_builds_merged_prs(self) -> None:
        repo = MagicMock()
        pull = MagicMock()
        pull.title = "Fix the thing"
        pull.user.login = "octocat"
        pull.html_url = "https://x/10"
        pull.body = "Fixes a crash when the thing overflows."
        pull.merge_commit_sha = "deadbeef"
        pull.labels = [MagicMock(name="lbl")]
        pull.labels[0].name = "release-notes"
        pull.head.ref = ""  # PyGithub returns a string; keep the mock realistic
        repo.get_pull.return_value = pull
        prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, {10: "sha"})
        assert len(prs) == 1
        assert prs[0].number == 10
        assert prs[0].author == "octocat"
        assert prs[0].body == "Fixes a crash when the thing overflows."
        assert prs[0].labels == ("release-notes",)
        assert unresolved_backports == []

    def test_ghost_author_becomes_empty(self) -> None:
        repo = MagicMock()
        pull = MagicMock()
        pull.title = "t"
        pull.user = None
        pull.html_url = "u"
        pull.body = None  # a PR with no description
        pull.merge_commit_sha = ""
        pull.labels = []
        pull.head.ref = ""  # PyGithub returns a string; keep the mock realistic
        repo.get_pull.return_value = pull
        prs, _, _ = hydrate_prs(repo, {5: "sha5"})
        assert prs[0].author == ""
        assert prs[0].body == ""  # None body coerces to ""
        assert prs[0].merge_commit_sha == "sha5"  # falls back to the commit sha

    def test_pr_404_is_surfaced_not_dropped(self) -> None:
        # A 404 (an issue, a moved/deleted PR, or a (#N) from another repo) does
        # not abort the run, but the range commit still shipped a change, so it is
        # recorded in unresolved_prs (with its sha) rather than silently dropped.
        from github.GithubException import UnknownObjectException

        repo = MagicMock()
        repo.get_pull.side_effect = UnknownObjectException(404, {"message": "Not Found"}, {})
        prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, {404: "shaX"})
        assert prs == []
        assert unresolved_backports == []
        assert [(u.number, u.sha) for u in unresolved_prs] == [(404, "shaX")]

    def test_non_404_github_error_is_reraised(self) -> None:
        # A 5xx that outlasts retries must not be swallowed: dropping a real
        # release-noted PR would ship it un-noted, and the label gate won't catch it.
        from github.GithubException import GithubException

        repo = MagicMock()
        repo.get_pull.side_effect = GithubException(500, {"message": "Server Error"}, {})
        with pytest.raises(GithubException):
            hydrate_prs(repo, {7: "sha"})


def _pull(number, *, title="a change", author="dev", url=None, body="",
          labels=(), commit_subjects=None, head_ref="", merge_sha="msha",
          merged=True, base_ref="unstable"):
    """Build a MagicMock PyGithub pull. ``labels`` is a tuple of label-name strings.

    ``merged``/``base_ref`` back the source-PR validation gate (a recovered source
    must be merged, and its title must match the backport's embedded source title).
    They are set explicitly because a bare MagicMock auto-vivifies ``.merged`` and
    ``.base.ref`` to truthy Mocks, which would pass the gate for the wrong reason and
    give the negative tests no teeth. Defaults model a normal merged source PR.
    """
    pull = MagicMock()
    pull.number = number
    pull.title = title
    pull.user = MagicMock() if author is not None else None
    if author is not None:
        pull.user.login = author
    pull.html_url = url if url is not None else f"https://x/{number}"
    pull.body = body
    pull.merge_commit_sha = merge_sha
    pull.merged = merged
    pull.merged_at = "2024-01-01T00:00:00Z" if merged else None
    pull.base.ref = base_ref
    label_mocks = []
    for name in labels:
        m = MagicMock()
        m.name = name
        label_mocks.append(m)
    pull.labels = label_mocks
    pull.head.ref = head_ref
    if commit_subjects is None:
        # A plain PR should never have its commits read; make it explode if it does.
        pull.get_commits.side_effect = AssertionError("get_commits should not be called")
    else:
        commits = []
        for subject in commit_subjects:
            c = MagicMock()
            c.commit.message = subject
            commits.append(c)
        pull.get_commits.return_value = commits
    return pull


class TestHydratePrsBackportRecovery:
    """Per-PR [Backport ...] PRs resolved for a range commit are walked back to
    their original source PR, so the note credits the change's author (and the
    original labels drive classification), never the backport."""

    def _repo(self, pulls):
        # pulls: {number: MagicMock pull}. get_pull raises 404 for unknown numbers.
        from github.GithubException import UnknownObjectException

        repo = MagicMock()

        def _get_pull(number):
            if number in pulls:
                return pulls[number]
            raise UnknownObjectException(404, {"message": "Not Found"}, {})

        repo.get_pull.side_effect = _get_pull
        return repo

    def test_recovers_via_backport_summary_row(self) -> None:
        # The backport PR (#500) carries a ## Backport Summary naming source #7.
        # Identity is taken from #7; the backport PR's own commits are never read.
        # The embedded source title (from the [Backport ..] prefix) matches #7's
        # actual title, so validation trusts the recovery.
        backport = _pull(
            500, title="[Backport 9.1] Fix a memory leak in cluster failover",
            labels=("backport",),
            body=(
                "## Backport Summary\n\nClean.\n\n"
                "| Field | Value |\n|---|---|\n"
                "| Source PR | [#7](https://x/7) |\n"
                "| Source title | Fix a memory leak in cluster failover |\n"
            ),
        )
        source = _pull(7, title="Fix a memory leak in cluster failover", author="alice",
                       body="the real body", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, {500: "shaBackport"})
        assert len(prs) == 1
        assert prs[0].number == 7
        assert prs[0].title == "Fix a memory leak in cluster failover"
        assert prs[0].author == "alice"
        assert prs[0].labels == ("release-notes",)
        assert prs[0].body == "the real body"
        # merge_commit_sha is the backport's range commit on THIS line, not the
        # source's merge on unstable (which is where source.merge_commit_sha points).
        assert prs[0].merge_commit_sha == "shaBackport"
        backport.get_commits.assert_not_called()
        assert unresolved_backports == []  # source recovered -> nothing to flag

    def test_recovers_via_pr_commits_when_no_summary(self) -> None:
        # No ## Backport Summary in the body: fall back to the backport PR's own
        # commits, whose trailing (#N) still names the original. The embedded title
        # (from the [Backport ..] prefix) still validates the recovered #7.
        backport = _pull(
            500, title="[Backport 9.1] Fix a memory leak in cluster failover",
            labels=("backport",),
            body="no summary here", commit_subjects=["Fix a memory leak (#7)"],
        )
        source = _pull(7, title="Fix a memory leak in cluster failover", author="alice",
                       labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        prs, _, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [7]
        assert prs[0].author == "alice"

    def test_recovers_via_branch_name_last(self) -> None:
        # No summary and the PR's commits carry no resolvable (#N): the head branch
        # backport/<n>-to-<x> still names the source. The embedded title still
        # validates the recovered #55.
        backport = _pull(
            500, title="[Backport 9.1] Fix a memory leak in cluster failover",
            labels=("backport",),
            body="", commit_subjects=["Fix a leak with no ref"],
            head_ref="backport/55-to-9.1",
        )
        source = _pull(55, title="Fix a memory leak in cluster failover", author="bob",
                       labels=("release-notes",))
        repo = self._repo({500: backport, 55: source})
        prs, _, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [55]
        assert prs[0].author == "bob"

    def test_unrecoverable_backport_credits_backport_not_dropped(self, caplog) -> None:
        # A backport with no summary, no resolvable PR-commit (#N), no backport
        # branch: keep crediting the backport (never drop), warn, and flag it so
        # the PR body surfaces the suspect credit (not only the CI log).
        backport = _pull(
            500, title="[Backport 9.1] Fix a leak", labels=("backport",),
            body="", commit_subjects=["Fix a leak with no ref"],
            head_ref="some/other-branch",
        )
        repo = self._repo({500: backport})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert any("itself a backport" in r.message for r in caplog.records)
        assert [(b.number, b.title) for b in unresolved_backports] == [
            (500, "[Backport 9.1] Fix a leak")
        ]

    def test_source_404_falls_back_to_backport(self, caplog) -> None:
        # The recovered source PR does not resolve (deleted / cross-repo): keep the
        # backport rather than drop the change, and flag it as unresolved.
        backport = _pull(
            500, title="[Backport 9.1] Fix a leak", labels=("backport",),
            body=(
                "## Backport Summary\n\n| Field | Value |\n|---|---|\n"
                "| Source PR | #7 |\n"
            ),
        )
        repo = self._repo({500: backport})  # #7 absent -> 404
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert [b.number for b in unresolved_backports] == [500]

    def test_two_backports_of_one_source_dedup(self) -> None:
        # Two per-PR backports (#500, #501) both trace to source #7 -> one entry.
        # Distinctive matching titles so validation trusts both recoveries.
        title = "Fix a memory leak in cluster failover"
        b1 = _pull(500, title=f"[Backport 9.1] {title}", labels=("backport",),
                   body="## Backport Summary\n\n| Field | Value |\n|---|---|\n| Source PR | #7 |\n")
        b2 = _pull(501, title=f"[Backport 9.0] {title}", labels=("backport",),
                   body="## Backport Summary\n\n| Field | Value |\n|---|---|\n| Source PR | #7 |\n")
        source = _pull(7, title=title, author="alice", labels=("release-notes",))
        repo = self._repo({500: b1, 501: b2, 7: source})
        prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, {500: "shaA", 501: "shaB"})
        assert [p.number for p in prs] == [7]
        # First-seen backport (#500) wins the dedup; its range sha is recorded.
        assert prs[0].merge_commit_sha == "shaA"
        assert unresolved_backports == []  # both resolved to #7

    def test_source_also_present_as_direct_pr_dedup(self) -> None:
        # #7 is in the range directly AND via backport #500 -> one entry keyed 7.
        title = "Fix a memory leak in cluster failover"
        source = _pull(7, title=title, author="alice", labels=("release-notes",))
        backport = _pull(
            500, title=f"[Backport 9.1] {title}", labels=("backport",),
            body="## Backport Summary\n\n| Field | Value |\n|---|---|\n| Source PR | #7 |\n",
        )
        repo = self._repo({7: source, 500: backport})
        prs, _, _ = hydrate_prs(repo, {7: "shaDirect", 500: "shaBackport"})
        assert [p.number for p in prs] == [7]
        assert prs[0].author == "alice"

    def test_plain_pr_never_reads_commits_or_remaps(self) -> None:
        # A normal (non-backport) PR: exactly one get_pull, commits never read.
        plain = _pull(10, title="A normal change", labels=("release-notes",))
        repo = self._repo({10: plain})
        prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, {10: "sha"})
        assert [p.number for p in prs] == [10]
        assert repo.get_pull.call_count == 1
        plain.get_commits.assert_not_called()
        assert unresolved_backports == []

    def test_backport_titled_pr_with_no_source_not_remapped(self, caplog) -> None:
        # A PR whose title merely starts with "[Backport ..]" but has no summary,
        # no PR-commit (#N), no backport branch is not remapped: credit itself, warn,
        # and flag it (it is still credited to a backport-looking PR).
        odd = _pull(
            10, title="[Backport 9.1] but actually authored here",
            labels=(), body="", commit_subjects=["some work, no ref"],
            head_ref="feature/x",
        )
        repo = self._repo({10: odd})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, {10: "sha"})
        assert [p.number for p in prs] == [10]
        assert any("itself a backport" in r.message for r in caplog.records)
        assert [b.number for b in unresolved_backports] == [10]

    def test_detects_backport_via_summary_without_title_or_label(self) -> None:
        # Gap 1: a backport whose author retitled it off the "[Backport ..]" prefix
        # and dropped the "backport" label is still detected via its ## Backport
        # Summary row, routed into recovery, and credited to the real source author.
        # Before, detection missed it and it was silently credited as an original.
        title = "Fix a memory leak in cluster failover"
        backport = _pull(
            500, title=title, labels=(),  # no [Backport] prefix, no backport label
            body=(
                "## Backport Summary\n\n| Field | Value |\n|---|---|\n"
                "| Source PR | [#7](https://x/7) |\n"
                f"| Source title | {title} |\n"
            ),
        )
        source = _pull(7, title=title, author="alice", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [7]
        assert prs[0].author == "alice"
        assert unresolved_backports == []

    def test_detects_backport_via_branch_name_without_title_or_label(self, caplog) -> None:
        # A backport with no "[Backport ..]" title, no label, and no summary is
        # still detected via its backport/<n>-to-<branch> head branch. With no
        # embedded source title to cross-check, recovery fails closed, so it is
        # credited to itself but flagged as unresolved, no longer a silent
        # mis-credit, as it was before detection consulted the branch name.
        backport = _pull(
            500, title="Fix a leak", labels=(), body="",
            commit_subjects=["Fix a leak with no ref"],
            head_ref="backport/55-to-9.1",
        )
        source = _pull(55, title="Fix a leak", author="bob", labels=("release-notes",))
        repo = self._repo({500: backport, 55: source})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert [b.number for b in unresolved_backports] == [500]

    # One distinctive underlying change carried by every hop of a chain, so each
    # hop's embedded source title matches the recovered source's title core and the
    # validation gate trusts the recovery. The `visited`/`depth` guards (not the
    # title gate) are then what stop the walk in the cyclic/max-depth tests.
    _CHAIN_TITLE = "Fix a memory leak in cluster failover"

    @classmethod
    def _summary_backport(cls, number, source, *, title=None):
        # A backport PR whose ## Backport Summary names `source`, so recovery
        # resolves via the row without ever reading its commits. Every hop embeds
        # the same distinctive source title so validation passes and the guards,
        # not the title check, terminate the walk.
        return _pull(
            number,
            title=title or f"[Backport 9.1] {cls._CHAIN_TITLE}",
            labels=("backport",),
            body=(
                "## Backport Summary\n\n| Field | Value |\n|---|---|\n"
                f"| Source PR | #{source} |\n"
                f"| Source title | {cls._CHAIN_TITLE} |\n"
            ),
        )

    def test_cyclic_backport_summary_terminates(self, caplog) -> None:
        # A malformed pair of ## Backport Summary rows points in a circle:
        # #500's source is #501, #501's source is #500. The `visited` guard must
        # stop the walk (never loop): it reaches #501, sees #501's source (#500)
        # is already visited, halts, and credits #501 as an unresolved backport
        # rather than hanging.
        b500 = self._summary_backport(500, 501)
        b501 = self._summary_backport(501, 500)
        repo = self._repo({500: b500, 501: b501})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, {500: "shaCycle"})
        assert [p.number for p in prs] == [501]  # halted at the last hop before the cycle
        assert [b.number for b in unresolved_backports] == [501]  # still a backport -> flagged
        assert any("itself a backport" in r.message for r in caplog.records)

    def test_backport_chain_bounded_by_max_depth(self, caplog) -> None:
        # A linear chain of per-PR backports #500 -> #501 -> #502 -> #503, each a
        # backport naming the next as its source. _MAX_BACKPORT_DEPTH (2) caps the
        # walk: it advances two hops to #502 and stops before resolving #503, so
        # #502 is credited (and flagged, still a backport). #503 is never fetched.
        b500 = self._summary_backport(500, 501)
        b501 = self._summary_backport(501, 502)
        b502 = self._summary_backport(502, 503)
        b503 = self._summary_backport(503, 504)  # present but must never be fetched
        repo = self._repo({500: b500, 501: b501, 502: b502, 503: b503})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, unresolved_prs = hydrate_prs(repo, {500: "shaChain"})
        assert [p.number for p in prs] == [502]  # two hops from #500
        assert [b.number for b in unresolved_backports] == [502]
        # The depth cap stopped the walk before #503 was ever looked up.
        assert 503 not in [c.args[0] for c in repo.get_pull.call_args_list]

    def test_unfetchable_pr_surfaced_while_others_resolve(self) -> None:
        # A range with two PRs where one 404s: the good PR is still noted, and the
        # 404'd one is recorded in unresolved_prs (with its range sha) rather than
        # vanishing from both the notes and the unresolved surface.
        good = _pull(10, title="A real change", author="alice", labels=("release-notes",))
        repo = self._repo({10: good})  # #99 absent -> 404
        prs, unresolved_backports, unresolved_prs = hydrate_prs(
            repo, {10: "shaGood", 99: "shaGhost"}
        )
        assert [p.number for p in prs] == [10]
        assert unresolved_backports == []
        assert [(u.number, u.sha) for u in unresolved_prs] == [(99, "shaGhost")]


class TestHydratePrsSourceValidation:
    """A recovered source PR is trusted only when it is merged and its title matches
    a distinctive source title the backport embeds. A recovery that fails validation
    keeps the backport credited and flags it under unresolved_backports (never a
    silent wrong-author credit). Fail-closed when there is nothing distinctive to
    check."""

    # A distinctive source title (>= 15 chars, >= 3 words, not generic) that a match
    # on can count as evidence.
    _TITLE = "Fix a memory leak in cluster failover"

    def _repo(self, pulls):
        from github.GithubException import UnknownObjectException

        repo = MagicMock()

        def _get_pull(number):
            if number in pulls:
                return pulls[number]
            raise UnknownObjectException(404, {"message": "Not Found"}, {})

        repo.get_pull.side_effect = _get_pull
        return repo

    def _backport(self, number, source, *, source_title, target="9.1"):
        # A backport whose ## Backport Summary names `source` and embeds `source_title`
        # both in the [Backport ..] prefix and the Source title row.
        return _pull(
            number, title=f"[Backport {target}] {source_title}", labels=("backport",),
            body=(
                "## Backport Summary\n\n| Field | Value |\n|---|---|\n"
                f"| Source PR | #{source} |\n"
                f"| Source title | {source_title} |\n"
            ),
        )

    def test_distinctive_title_match_is_trusted(self) -> None:
        backport = self._backport(500, 7, source_title=self._TITLE)
        source = _pull(7, title=self._TITLE, author="alice", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [7]
        assert prs[0].author == "alice"
        assert unresolved_backports == []

    def test_title_mismatch_demotes_to_unresolved(self, caplog) -> None:
        # The recovered #7 resolves to a real PR whose title is unrelated to what the
        # backport claims it carried (a mistyped Source PR cell, a stray (#N)). Keep
        # the backport credit and flag it; never silently credit #7's author.
        backport = self._backport(500, 7, source_title=self._TITLE)
        wrong = _pull(7, title="Completely unrelated change to logging",
                      author="mallory", labels=("release-notes",))
        repo = self._repo({500: backport, 7: wrong})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]  # credited to the backport, not #7
        assert prs[0].author == "dev"            # not mallory
        assert [b.number for b in unresolved_backports] == [500]
        assert any("failed validation" in r.message for r in caplog.records)

    def test_generic_title_is_not_evidence_demotes(self, caplog) -> None:
        # The embedded source title is a recurring chore title ("Fix CI"): a match on
        # it can't distinguish two PRs, so it is not distinctive and the recovery
        # fails closed even though #7's title matches exactly.
        backport = self._backport(500, 7, source_title="Fix CI")
        source = _pull(7, title="Fix CI", author="alice", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert [b.number for b in unresolved_backports] == [500]

    def test_automated_prefix_title_is_not_evidence_demotes(self, caplog) -> None:
        # An automated dependabot title clears the length and word-count floors and
        # is not in the exact stoplist, so only _GENERIC_TITLE_PREFIX_RE (the "Bump"
        # stem) rejects it as non-distinctive. The same "Bump <dep> ..." stem recurs
        # across many PRs, so a title match is not evidence #7 is the right source:
        # the recovery must fail closed and keep the backport credit. Isolates the
        # automated-prefix guard, which "Fix CI" (short + stoplisted) never exercises.
        auto_title = "Bump lodash from 1.2.3 to 1.2.4 in the web dir"
        backport = self._backport(500, 7, source_title=auto_title)
        source = _pull(7, title=auto_title, author="alice", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]     # credited to the backport, not #7
        assert prs[0].author == "dev"               # not alice
        assert [b.number for b in unresolved_backports] == [500]

    def test_stoplist_title_passing_floors_is_not_evidence_demotes(self, caplog) -> None:
        # "Update copyright year" is 21 chars and 3 words, so it clears both the
        # char and word floors; only its presence in _GENERIC_TITLES rejects it.
        # Isolates the exact-stoplist branch (distinct from the char/word floors and
        # the automated-prefix regex).
        chore_title = "Update copyright year"
        backport = self._backport(500, 7, source_title=chore_title)
        source = _pull(7, title=chore_title, author="alice", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert [b.number for b in unresolved_backports] == [500]

    def test_short_nonlisted_title_below_floors_demotes(self, caplog) -> None:
        # "Fix crash" is 2 words / 9 chars: not in the stoplist and not an automated
        # prefix, so only the char/word floors reject it as non-distinctive. Isolates
        # the length/word-count floors, which the stoplisted "Fix CI" also trips.
        short_title = "Fix crash"
        backport = self._backport(500, 7, source_title=short_title)
        source = _pull(7, title=short_title, author="alice", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert [b.number for b in unresolved_backports] == [500]

    def test_unmerged_source_demotes(self, caplog) -> None:
        # The recovered #7 is a real PR with a matching title but is not merged (an
        # open PR, or an issue-shaped ref). A shipped change's source is always
        # merged, so reject and keep the backport credit.
        backport = self._backport(500, 7, source_title=self._TITLE)
        unmerged = _pull(7, title=self._TITLE, author="alice",
                         labels=("release-notes",), merged=False)
        repo = self._repo({500: backport, 7: unmerged})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert [b.number for b in unresolved_backports] == [500]

    def test_label_only_backport_with_no_embedded_title_demotes(self, caplog) -> None:
        # A backport recognized only by its label, with a free-form title (no
        # [Backport ..] prefix) and no Source title row: nothing to cross-check, so
        # fail closed even though the branch name recovers a real matching source.
        backport = _pull(
            500, title="Fix a memory leak in cluster failover", labels=("backport",),
            body="", commit_subjects=["work with no ref"],
            head_ref="backport/7-to-9.1",
        )
        source = _pull(7, title=self._TITLE, author="alice", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert [b.number for b in unresolved_backports] == [500]

    def test_source_retitled_within_threshold_is_trusted(self) -> None:
        # The source PR was lightly retitled after the backport was cut (added a
        # trailing period). Similarity stays >= 0.90, so the recovery is still trusted.
        backport = self._backport(500, 7, source_title=self._TITLE)
        source = _pull(7, title=self._TITLE + ".", author="alice", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [7]
        assert unresolved_backports == []

    def test_source_retitled_beyond_threshold_demotes(self, caplog) -> None:
        # The source title diverged too far (a rewrite, or a wrong PR that happens to
        # share a few words). Below the 0.90 floor -> demote rather than mis-credit.
        backport = self._backport(500, 7, source_title=self._TITLE)
        source = _pull(7, title="Fix a race in replication handshake retries",
                       author="alice", labels=("release-notes",))
        repo = self._repo({500: backport, 7: source})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaBackport"})
        assert [p.number for p in prs] == [500]
        assert [b.number for b in unresolved_backports] == [500]

    def test_chained_backport_with_matching_titles_walks_through(self) -> None:
        # #500 -> #501 -> source #7, each hop a backport embedding the same distinctive
        # underlying title. Validation compares title cores (prefix stripped), so every
        # hop passes and the walk reaches the true source #7.
        b500 = self._backport(500, 501, source_title=self._TITLE, target="8.0")
        b501 = self._backport(501, 7, source_title=self._TITLE, target="9.0")
        source = _pull(7, title=self._TITLE, author="alice", labels=("release-notes",))
        repo = self._repo({500: b500, 501: b501, 7: source})
        prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaChain"})
        assert [p.number for p in prs] == [7]
        assert prs[0].author == "alice"
        assert unresolved_backports == []

    def test_chain_hop_mismatch_strands_at_intermediate(self, caplog) -> None:
        # #500 -> #501 (both backports of the distinctive title) but #501's recovered
        # source #7 has an unrelated title. The gate rejects hop 2, leaving the walk at
        # the intermediate backport #501 (flagged), never mis-crediting #7's author.
        b500 = self._backport(500, 501, source_title=self._TITLE, target="8.0")
        b501 = self._backport(501, 7, source_title=self._TITLE, target="9.0")
        wrong = _pull(7, title="Unrelated logging tweak here", author="mallory",
                      labels=("release-notes",))
        repo = self._repo({500: b500, 501: b501, 7: wrong})
        with caplog.at_level("WARNING"):
            prs, unresolved_backports, _ = hydrate_prs(repo, {500: "shaChain"})
        assert [p.number for p in prs] == [501]  # stranded at the intermediate
        assert prs[0].author != "mallory"
        assert [b.number for b in unresolved_backports] == [501]
        assert any("failed validation" in r.message for r in caplog.records)


class TestReconcileCherryPickSuspects:
    """`_reconcile_cherry_pick_suspects` keeps only unflagged, still-credited suspects."""

    @staticmethod
    def _suspect(number: int) -> UnresolvedCherryPick:
        return UnresolvedCherryPick(
            number=number, sha=f"sha{number}",
            source_shas=("deadbeefdeadbeef",), subject=f"port fix (#{number})",
        )

    @staticmethod
    def _merged(number: int) -> MergedPR:
        return MergedPR(number=number, title=f"PR {number}", author="dev", url=f"https://x/{number}")

    def test_kept_when_credited_and_unflagged(self) -> None:
        # The hole: #80 is present in prs under its own number and carries no
        # backport markers, so nothing else flags the unconfirmed credit.
        suspects = {80: self._suspect(80)}
        kept = _reconcile_cherry_pick_suspects(suspects, [self._merged(80)], [], [])
        assert [cp.number for cp in kept] == [80]

    def test_dropped_when_remapped_by_hydrate(self) -> None:
        # hydrate_prs walked the credited #80 back to a recovered original #7, so
        # #80 is no longer in prs: the credit was corrected, no flag.
        suspects = {80: self._suspect(80)}
        kept = _reconcile_cherry_pick_suspects(suspects, [self._merged(7)], [], [])
        assert kept == []

    def test_dropped_when_already_flagged_as_backport(self) -> None:
        # The credited PR carried backport markers and is already an
        # UnresolvedBackport; flagging it again as a cherry-pick would double-report.
        suspects = {80: self._suspect(80)}
        backports = [UnresolvedBackport(number=80, title="[Backport 9.1] port fix")]
        kept = _reconcile_cherry_pick_suspects(suspects, [self._merged(80)], backports, [])
        assert kept == []

    def test_dropped_when_pr_fetch_failed(self) -> None:
        # The credited number 404'd into unresolved_prs (the note was never built),
        # so it is surfaced there, not as a miscredit.
        suspects = {80: self._suspect(80)}
        fetch_failed = [UnresolvedPR(number=80, sha="sha80")]
        kept = _reconcile_cherry_pick_suspects(suspects, [], [], fetch_failed)
        assert kept == []

    def test_empty_suspects_short_circuits(self) -> None:
        assert _reconcile_cherry_pick_suspects({}, [self._merged(1)], [], []) == []


class TestDiscover:
    def test_end_to_end_local(self, tmp_path, monkeypatch) -> None:
        repo_dir = _init_repo(tmp_path)
        _commit(repo_dir, "base (#1)")
        run_git(repo_dir, "tag", "9.1.0-rc1")
        _commit(repo_dir, "feat (#2)")
        _commit(repo_dir, "fix (#3)")

        gh_repo = MagicMock()

        def _get_pull(n):
            p = MagicMock()
            p.title = f"PR {n}"
            p.user.login = "dev"
            p.html_url = f"https://x/{n}"
            p.body = f"Body of PR {n}"
            p.merge_commit_sha = ""
            p.labels = []
            p.head.ref = ""  # PyGithub returns a string; keep the mock realistic
            return p

        gh_repo.get_pull.side_effect = _get_pull
        result = discover_mod.discover(gh_repo, repo_dir, "main", tag_glob="9.1.*")
        assert result.base_tag == "9.1.0-rc1"
        assert {p.number for p in result.prs} == {2, 3}

    def test_end_to_end_credits_source_of_per_pr_backport(self, tmp_path) -> None:
        # A per-PR backport squash-merged onto the line: its subject is the backport
        # PR (#500), it carries no ## Applied table / -x trailer, so resolve_commit_prs
        # keys it as #500. hydrate_prs then walks it back to source #7 via the
        # backport PR's ## Backport Summary. Validation trusts #7 because its title
        # matches the distinctive source title the backport embeds. The rendered
        # range must credit #7.
        repo_dir = _init_repo(tmp_path)
        _commit(repo_dir, "base (#1)")
        run_git(repo_dir, "tag", "9.1.0-rc1")
        _commit(repo_dir, "Fix a memory leak (#500)")  # squash subject = the backport PR
        source_title = "Fix a memory leak in cluster failover"

        def _get_pull(n):
            p = MagicMock()
            p.user.login = "dev" if n == 500 else "alice"
            p.html_url = f"https://x/{n}"
            p.merge_commit_sha = ""
            p.head.ref = ""
            p.merged = True
            p.merged_at = "2024-01-01T00:00:00Z"
            p.base.ref = "unstable"
            if n == 500:
                p.title = f"[Backport 9.1] {source_title}"
                m = MagicMock()
                m.name = "backport"
                p.labels = [m]
                p.body = (
                    "## Backport Summary\n\n| Field | Value |\n|---|---|\n"
                    "| Source PR | [#7](https://x/7) |\n"
                    f"| Source title | {source_title} |\n"
                )
                p.get_commits.side_effect = AssertionError("summary row should win")
            else:  # the original source PR
                p.title = source_title
                m = MagicMock()
                m.name = "release-notes"
                p.labels = [m]
                p.body = "the original description"
            return p

        gh_repo = MagicMock()
        gh_repo.get_pull.side_effect = _get_pull
        result = discover_mod.discover(gh_repo, repo_dir, "main", tag_glob="9.1.*")
        assert {p.number for p in result.prs} == {7}
        (pr,) = result.prs
        assert pr.author == "alice"
        assert pr.title == source_title
        assert pr.labels == ("release-notes",)

    def test_end_to_end_merge_merged_sweep_credits_sources_without_flagging_container(
        self, tmp_path
    ) -> None:
        # A merge-merged backport sweep of #201/#202 under container PR #500. The
        # sources are spliced in and credited; the sweep merge commit is dropped, so
        # #500 (the backport-labeled container) is neither noted nor flagged as an
        # unresolved backport. Guards the false positive where the retained merge
        # commit resolved to #500 and, finding no single recoverable source, flagged
        # a sweep that had in fact resolved cleanly.
        repo_dir = _init_repo(tmp_path)
        _commit(repo_dir, "base (#1)")
        run_git(repo_dir, "tag", "9.1.0-rc1")
        run_git(repo_dir, "checkout", "-q", "-b", "agent/backport/sweep/9.0")
        _commit(repo_dir, "Fix alpha (#201)")
        _commit(repo_dir, "Fix beta (#202)")
        run_git(repo_dir, "checkout", "-q", "main")
        run_git(repo_dir, "merge", "-q", "--no-ff", "agent/backport/sweep/9.0",
                "-m", "Merge pull request #500 from valkey-io/agent/backport/sweep/9.0")

        def _get_pull(n):
            p = MagicMock()
            p.user.login = "dev"
            p.html_url = f"https://x/{n}"
            p.merge_commit_sha = ""
            p.head.ref = ""
            p.merged = True
            p.merged_at = "2024-01-01T00:00:00Z"
            p.base.ref = "unstable"
            m = MagicMock()
            m.name = "release-notes"
            p.title = f"Fix {n}"
            p.labels = [m]
            p.body = ""
            return p

        gh_repo = MagicMock()
        gh_repo.get_pull.side_effect = _get_pull
        result = discover_mod.discover(gh_repo, repo_dir, "main", tag_glob="9.1.*")
        assert {p.number for p in result.prs} == {201, 202}  # container #500 not noted
        assert result.unresolved_backports == ()  # and not falsely flagged

    def test_end_to_end_flags_unconfirmed_fork_cherry_pick(self, tmp_path) -> None:
        # A hand-applied -x pick from a fork: the commit body names a source SHA
        # that is not in this repo, so the trailer cannot resolve, and the credit
        # falls to the subject (#80). The credited PR carries no backport markers,
        # so hydrate_prs never flags it. discover() must surface it as an
        # UnresolvedCherryPick so the suspect credit is visible to a maintainer.
        repo_dir = _init_repo(tmp_path)
        _commit(repo_dir, "base (#1)")
        run_git(repo_dir, "tag", "9.1.0-rc1")
        _commit_with_body(
            repo_dir, "port fix (#80)",
            "port fix\n\n(cherry picked from commit deadbeefdeadbeef)",
        )

        def _get_pull(n):
            p = MagicMock()
            p.title = f"PR {n}"
            p.user.login = "dev"
            p.html_url = f"https://x/{n}"
            p.body = ""
            p.merge_commit_sha = ""
            p.labels = []  # no backport marker: hydrate_prs will not flag it
            p.head.ref = ""
            return p

        gh_repo = MagicMock()
        gh_repo.get_pull.side_effect = _get_pull
        # The source SHA is not in this repo: the trailer lookup resolves to no PR.
        gh_repo.get_commit.return_value.get_pulls.return_value = []

        result = discover_mod.discover(gh_repo, repo_dir, "main", tag_glob="9.1.*")
        assert {p.number for p in result.prs} == {80}
        assert result.unresolved_backports == ()  # not caught by the backport flag
        assert [cp.number for cp in result.unresolved_cherry_picks] == [80]
        (cp,) = result.unresolved_cherry_picks
        assert cp.source_shas == ("deadbeefdeadbeef",)

    def test_explicit_base_ref_overrides_tag(self, tmp_path) -> None:
        # A repo with no tags (like a fork): tag resolution would raise, but an
        # explicit base_ref makes the range base_ref..head work directly.
        repo_dir = _init_repo(tmp_path)
        _commit(repo_dir, "root (#1)")
        run_git(repo_dir, "branch", "base")
        _commit(repo_dir, "feat (#2)")

        gh_repo = MagicMock()

        def _get_pull(n):
            p = MagicMock()
            p.title = f"PR {n}"
            p.user.login = "dev"
            p.html_url = f"https://x/{n}"
            p.body = f"Body of PR {n}"
            p.merge_commit_sha = ""
            p.labels = []
            p.head.ref = ""  # PyGithub returns a string; keep the mock realistic
            return p

        gh_repo.get_pull.side_effect = _get_pull
        result = discover_mod.discover(gh_repo, repo_dir, "main", base_ref="base")
        assert result.base_tag == "base"
        assert {p.number for p in result.prs} == {2}  # only commits after base

    def test_base_ref_resolves_via_remote_tracking_ref(self, tmp_path) -> None:
        # Mirror the real cut: `git clone --branch <src>` leaves every OTHER
        # branch reachable only as origin/<name>. A base_ref naming such a branch
        # must resolve via the remote-tracking ref, and the resolved name must
        # carry into the range so base..head still excludes the base commit.
        (tmp_path / "upstream").mkdir()
        upstream = _init_repo(tmp_path / "upstream")
        _commit(upstream, "root (#1)")
        run_git(upstream, "branch", "unstable")  # baseline lives on its own branch
        run_git(upstream, "checkout", "-q", "main")
        _commit(upstream, "feat (#2)")

        clone_dir = str(tmp_path / "clone")
        # Single-branch clone of main only; 'unstable' is now origin/unstable.
        run_git(None, "clone", "-q", "--branch", "main", upstream, clone_dir)
        with pytest.raises(Exception):  # noqa: B017 - bare name does not resolve locally
            git_output(clone_dir, "rev-parse", "--verify", "unstable")

        gh_repo = MagicMock()

        def _get_pull(n):
            p = MagicMock()
            p.title = f"PR {n}"
            p.user.login = "dev"
            p.html_url = f"https://x/{n}"
            p.body = f"Body of PR {n}"
            p.merge_commit_sha = ""
            p.labels = []
            p.head.ref = ""  # PyGithub returns a string; keep the mock realistic
            return p

        gh_repo.get_pull.side_effect = _get_pull
        result = discover_mod.discover(gh_repo, clone_dir, "main", base_ref="unstable")
        assert result.base_tag == "origin/unstable"  # fell back to remote-tracking ref
        assert {p.number for p in result.prs} == {2}  # only commits after the baseline

    def test_unresolvable_base_ref_raises_valueerror_naming_ref(self, tmp_path) -> None:
        # A --base-ref that resolves neither as given nor as origin/<name> (a
        # typo'd branch/tag) must raise a ValueError naming the ref, mirroring
        # resolve_last_tag, not leak a raw CalledProcessError from the fallback
        # rev-parse.
        repo = _init_repo(tmp_path)
        _commit(repo, "root (#1)")
        with pytest.raises(ValueError, match="no-such-ref"):
            _resolve_base_ref(repo, "no-such-ref")
