"""Tests for the src/version.h version-macro rewriter (version_bump)."""

from __future__ import annotations

import pytest

from scripts.release_notes.version_bump import set_version, version_num

_SAMPLE = (
    '#define SERVER_NAME "valkey"\n'
    '#define VALKEY_VERSION "255.255.255"\n'
    "#define VALKEY_VERSION_NUM 0x00ffffff\n"
    '#define VALKEY_RELEASE_STAGE "dev"\n'
    '#define REDIS_VERSION "7.2.4"\n'
)


def _macro(text: str, name: str) -> str:
    """Return the value written for ``#define <name> <value>`` (first match)."""
    for line in text.splitlines():
        if line.startswith(f"#define {name} "):
            return line.split(None, 2)[2]
    raise AssertionError(f"{name} not found")


class TestVersionNum:
    def test_packs_bytes(self) -> None:
        assert version_num("9.1.0") == "0x00090100"
        assert version_num("255.255.255") == "0x00ffffff"
        assert version_num("1.2.3") == "0x00010203"


class TestSetVersion:
    def test_rewrites_all_three_macros(self) -> None:
        out = set_version(_SAMPLE, "9.1.0", "rc2")
        assert _macro(out, "VALKEY_VERSION") == '"9.1.0"'
        assert _macro(out, "VALKEY_VERSION_NUM") == "0x00090100"
        assert _macro(out, "VALKEY_RELEASE_STAGE") == '"rc2"'

    def test_leaves_unrelated_macros_untouched(self) -> None:
        out = set_version(_SAMPLE, "9.1.0", "ga")
        assert _macro(out, "SERVER_NAME") == '"valkey"'
        assert _macro(out, "REDIS_VERSION") == '"7.2.4"'

    def test_string_and_num_agree_on_leading_zero_input(self) -> None:
        # Regression: set_version used to write the raw version string into
        # VALKEY_VERSION while deriving VALKEY_VERSION_NUM from the parsed tuple,
        # so "09.1.0" produced VALKEY_VERSION "09.1.0" but NUM 0x00090100, a
        # self-inconsistent version.h. The string is now derived from the parsed
        # tuple too, so both macros always agree (and carry the canonical form).
        out = set_version(_SAMPLE, "09.1.0", "rc1")
        assert _macro(out, "VALKEY_VERSION") == '"9.1.0"'
        assert _macro(out, "VALKEY_VERSION_NUM") == "0x00090100"

    def test_stage_normalized_lowercase(self) -> None:
        out = set_version(_SAMPLE, "9.1.0", "RC1")
        assert _macro(out, "VALKEY_RELEASE_STAGE") == '"rc1"'

    @pytest.mark.parametrize("bad", ["9.1", "v9.1.0", "9.1.0-rc1", "9.256.0", "nope"])
    def test_rejects_malformed_version(self, bad: str) -> None:
        with pytest.raises(ValueError):
            set_version(_SAMPLE, bad, "rc1")

    @pytest.mark.parametrize("bad", ["beta", "", "ga1", "release", "rc0", "rc01", "rc00"])
    def test_rejects_malformed_stage(self, bad: str) -> None:
        # The rc sub-pattern matches _RC_STAGE_RE elsewhere: N starts at 1 with no
        # leading zeros, so "rc0"/"rc01"/"rc00" are rejected, not just non-rc junk.
        with pytest.raises(ValueError):
            set_version(_SAMPLE, "9.1.0", bad)

    def test_accepts_dev_and_ga_and_valid_rc(self) -> None:
        # dev/ga and a normal rcN still pass (the tighten only removed rc0/leading zeros).
        for stage in ("dev", "ga", "rc1", "rc12"):
            set_version(_SAMPLE, "9.1.0", stage)  # must not raise

    def test_missing_required_macro_raises(self) -> None:
        # VERSION and VERSION_NUM are required; a version.h lacking either is a
        # hard error.
        without_num = '#define VALKEY_VERSION "1.0.0"\n#define VALKEY_RELEASE_STAGE "dev"\n'
        with pytest.raises(ValueError, match="VALKEY_VERSION_NUM"):
            set_version(without_num, "9.1.0", "rc1")

    def test_missing_stage_macro_is_tolerated(self) -> None:
        # Older branches (7.2) predate VALKEY_RELEASE_STAGE. The version bump
        # must succeed without it, updating only VERSION and VERSION_NUM.
        legacy = (
            '#define SERVER_NAME "valkey"\n'
            '#define VALKEY_VERSION "7.2.13"\n'
            "#define VALKEY_VERSION_NUM 0x0007020d\n"
        )
        out = set_version(legacy, "7.2.14", "rc1")
        assert _macro(out, "VALKEY_VERSION") == '"7.2.14"'
        assert _macro(out, "VALKEY_VERSION_NUM") == "0x0007020e"
        assert "VALKEY_RELEASE_STAGE" not in out

    def test_duplicated_required_macro_raises(self) -> None:
        # Two definitions of a required macro (count > 1) is a hard error.
        doubled = (
            '#define VALKEY_VERSION "1.0.0"\n'
            '#define VALKEY_VERSION "2.0.0"\n'
            "#define VALKEY_VERSION_NUM 0x00010000\n"
            '#define VALKEY_RELEASE_STAGE "dev"\n'
        )
        with pytest.raises(ValueError, match="VALKEY_VERSION"):
            set_version(doubled, "9.1.0", "rc1")
