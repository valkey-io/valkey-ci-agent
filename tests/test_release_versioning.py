"""Tests for deterministic version derivation from branch + tags."""

from __future__ import annotations

import pytest

from scripts.release.models import ReleaseIntent
from scripts.release.versioning import derive_version, parse_release_branch


class TestParseReleaseBranch:
    def test_accepts_release_lines(self) -> None:
        assert parse_release_branch("9.1") == (9, 1)
        assert parse_release_branch(" 10.0 ") == (10, 0)

    @pytest.mark.parametrize("branch", ["unstable", "main", "9.1.0", "9", "v9.1", ""])
    def test_rejects_non_release_branches(self, branch: str) -> None:
        with pytest.raises(ValueError, match="not a release branch"):
            parse_release_branch(branch)
    @pytest.mark.parametrize("branch", ["9.01", "09.1"])
    def test_rejects_leading_zero_components(self, branch: str) -> None:
        with pytest.raises(ValueError, match="not a release branch"):
            parse_release_branch(branch)


class TestDeriveRC:
    @pytest.mark.parametrize("tags", [["9.0.0", "9.0.1"], []],
                             ids=["fresh-line", "empty-tag-set"])
    def test_first_rc_on_fresh_or_empty_line(self, tags: "list[str]") -> None:
        derived = derive_version("9.1", ReleaseIntent.RC, tags)
        assert (derived.version, derived.stage) == ("9.1.0", "rc1")
        assert derived.tag == "9.1.0-rc1"

    def test_next_rc_follows_existing_rcs(self) -> None:
        tags = ["9.1.0-rc1", "9.1.0-rc2", "9.0.0"]
        derived = derive_version("9.1", ReleaseIntent.RC, tags)
        assert (derived.version, derived.stage) == ("9.1.0", "rc3")

    def test_rc_never_derives_backward_over_a_gap(self) -> None:
        # Only rc3 survives (rc1/rc2 deleted, or the line was seeded there):
        # the next rc must be rc4, never a reissue of the missing rc1.
        derived = derive_version("9.1", ReleaseIntent.RC, ["9.1.0-rc3"])
        assert (derived.version, derived.stage) == ("9.1.0", "rc4")

    def test_deterministic_regardless_of_tag_order(self) -> None:
        tags = ["9.1.0-rc2", "9.0.3", "9.1.0-rc1", "8.1.0"]
        assert derive_version("9.1", ReleaseIntent.RC, tags) == derive_version(
            "9.1", ReleaseIntent.RC, list(reversed(tags))
        )

    def test_malformed_rc_numbers_and_components_are_ignored(self) -> None:
        # rc0, rc01, and zero-padded version components are outside the
        # tag model; counting them would derive over malformed tags.
        malformed = [
            "9.1.0-rc0", "9.1.0-rc01", "09.1.0-rc2",
            "9.01.0-rc2", "9.1.00-rc2",
        ]
        derived = derive_version("9.1", ReleaseIntent.RC, malformed)
        assert (derived.version, derived.stage) == ("9.1.0", "rc1")

    def test_two_digit_major_flows_through_every_regex(self) -> None:
        # Both the branch regex and the tag regexes must survive a
        # two-digit major; a single-digit assumption anywhere would either
        # refuse the branch or silently drop the existing rc tag.
        derived = derive_version("10.0", ReleaseIntent.RC, ["10.0.0-rc1"])
        assert (derived.version, derived.stage) == ("10.0.0", "rc2")


class TestDeriveGA:
    @pytest.mark.parametrize("tags", [["9.1.0-rc1", "9.1.0-rc2"], []],
                             ids=["after-rcs", "empty-tag-set"])
    def test_initial_ga(self, tags: "list[str]") -> None:
        derived = derive_version("9.1", ReleaseIntent.GA, tags)
        assert (derived.version, derived.stage) == ("9.1.0", "ga")
        assert derived.tag == "9.1.0"


@pytest.mark.parametrize("intent", [ReleaseIntent.RC, ReleaseIntent.GA])
@pytest.mark.parametrize("shipped_tag", ["9.1.0", "9.1.1"])
def test_rc_and_ga_refused_after_line_released(
    intent: ReleaseIntent, shipped_tag: str,
) -> None:
    # Any final release on the line closes the rc/ga window. That includes a
    # line without a .0 tag: a deleted .0 tag (or a line seeded at .1) must
    # not reopen the rc window, since deriving 9.1.0-rc1 there would version
    # BELOW the shipped 9.1.1.
    with pytest.raises(ValueError, match="final release"):
        derive_version("9.1", intent, [shipped_tag])


def test_rc_refused_when_the_line_has_both_a_final_and_its_rc() -> None:
    # '9.1.1' and '9.1.1-rc1' together: the rc tag is NOT an initial-release
    # rc (patch != 0) so it must not be counted, and the final must still
    # close the rc window.
    with pytest.raises(ValueError, match="final release"):
        derive_version("9.1", ReleaseIntent.RC, ["9.1.1", "9.1.1-rc1"])


class TestDerivePatch:
    def test_next_patch_after_initial_release(self) -> None:
        derived = derive_version("8.0", ReleaseIntent.PATCH, ["8.0.0", "8.0.1", "8.0.7"])
        assert (derived.version, derived.stage) == ("8.0.8", "ga")

    @pytest.mark.parametrize("tags", [["9.1.0-rc1", "9.0.0"], []],
                             ids=["unreleased-line", "empty-tag-set"])
    def test_patch_refused_without_a_final_release(self, tags: "list[str]") -> None:
        with pytest.raises(ValueError, match="no final release"):
            derive_version("9.1", ReleaseIntent.PATCH, tags)

    def test_other_line_and_malformed_tags_ignored(self) -> None:
        tags = [
            "8.0.0", "8.1.9", "v8.0.5", "8.0.2-rc1", "junk",
            "08.0.4", "8.00.4", "8.0.04", "8.0.1",
        ]
        derived = derive_version("8.0", ReleaseIntent.PATCH, tags)
        # Other lines, prefixed or zero-padded tags, junk, and RCs do not
        # count as final releases. Max valid final patch on 8.0 is 1.
        assert derived.version == "8.0.2"

    def test_patch_numbers_compare_numerically_not_lexically(self) -> None:
        # A string sort would put '9.1.9' above '9.1.10' and re-derive the
        # already-shipped 9.1.10.
        derived = derive_version("9.1", ReleaseIntent.PATCH, ["9.1.9", "9.1.10"])
        assert derived.version == "9.1.11"

    def test_patch_beside_a_nonzero_patch_rc_tag(self) -> None:
        # '9.1.1-rc1' matches the rc tag regex but not the M.m.0 initial-rc
        # shape; it must be ignored while '9.1.1' still counts as final.
        derived = derive_version("9.1", ReleaseIntent.PATCH, ["9.1.1", "9.1.1-rc1"])
        assert derived.version == "9.1.2"

    @pytest.mark.parametrize("stray", ["9.1.1+hotfix", "9.1.0.1", "unstable"])
    def test_tags_outside_the_model_never_count_as_finals(self, stray: str) -> None:
        # Build-metadata tags, four-component tags, and branch-named tags
        # are outside the tag model: they must be ignored, not partially
        # matched into a final release (which would skip or repeat a patch).
        derived = derive_version("9.1", ReleaseIntent.PATCH, ["9.1.0", stray])
        assert derived.version == "9.1.1"
