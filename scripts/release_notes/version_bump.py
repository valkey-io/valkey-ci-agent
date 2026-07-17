"""Set the Valkey version macros in src/version.h.

Rewrites VALKEY_VERSION, VALKEY_VERSION_NUM (0x00MMmmpp), and
VALKEY_RELEASE_STAGE in place. Other macros are left untouched.
"""

from __future__ import annotations

import re

from scripts.release_notes.release_format import parse_version

_VERSION_DEFINE_RE = re.compile(r'^(#define\s+VALKEY_VERSION\s+)"[^"]*"', re.MULTILINE)
_VERSION_NUM_DEFINE_RE = re.compile(r"^(#define\s+VALKEY_VERSION_NUM\s+)0x[0-9A-Fa-f]+", re.MULTILINE)
_STAGE_DEFINE_RE = re.compile(r'^(#define\s+VALKEY_RELEASE_STAGE\s+)"[^"]*"', re.MULTILINE)

# dev/ga/rcN. Superset of release_format's _RC_STAGE_RE (also accepts "dev").
_STAGE_RE = re.compile(r"^(dev|ga|rc[1-9]\d*)$")


def version_num(version: str) -> str:
    """Return the ``0x00MMmmpp`` hex literal for a ``"M.m.p"`` version string."""
    major, minor, patch = parse_version(version)
    return "0x00{:02x}{:02x}{:02x}".format(major, minor, patch)


def _validate_stage(stage: str) -> str:
    stage = stage.strip().lower()
    if not _STAGE_RE.match(stage):
        raise ValueError(
            "release stage must be 'dev', 'ga', or 'rcN' (e.g. rc1), got {!r}".format(stage)
        )
    return stage


def set_version(version_h_text: str, version: str, stage: str) -> str:
    """Return *version_h_text* with the three Valkey version macros updated."""
    # Derive canonical string from parsed tuple so VERSION and VERSION_NUM agree.
    major, minor, patch = parse_version(version)
    canonical = "{}.{}.{}".format(major, minor, patch)
    stage = _validate_stage(stage)

    text, n1 = _VERSION_DEFINE_RE.subn(
        lambda m: '{}"{}"'.format(m.group(1), canonical), version_h_text
    )
    text, n2 = _VERSION_NUM_DEFINE_RE.subn(
        lambda m: "{}{}".format(m.group(1), version_num(canonical)), text
    )
    text, n3 = _STAGE_DEFINE_RE.subn(
        lambda m: '{}"{}"'.format(m.group(1), stage), text
    )
    # VERSION and VERSION_NUM are required.
    # RELEASE_STAGE is optional: older branches (up to 8.0 inclusive) predate the macro.
    missing = [
        name
        for name, count in (
            ("VALKEY_VERSION", n1),
            ("VALKEY_VERSION_NUM", n2),
        )
        if count != 1
    ]
    if missing:
        raise ValueError(
            "expected exactly one definition of each of these macros in version.h, "
            "but they were missing or duplicated: {}".format(", ".join(missing))
        )
    return text
