"""Set the Valkey version macros in src/version.h.

Rewrites three macros in place:

    #define VALKEY_VERSION "M.m.p"
    #define VALKEY_VERSION_NUM 0x00MMmmpp
    #define VALKEY_RELEASE_STAGE "dev"|"rcN"|"ga"

``VALKEY_VERSION_NUM`` packs major/minor/patch into one byte each, matching the
documented ``0x00MMmmpp`` scheme used by ``VM_GetServerVersion`` (src/module.c)
and parsed by ``version2num`` (src/util.c). Other macros (SERVER_NAME,
REDIS_VERSION, ...) are left untouched.

Upstream ``valkey-io/valkey`` ships no equivalent tool, so this module owns the
``src/version.h`` format and :mod:`release_cut` drives it against a clone.
"""

from __future__ import annotations

import re

from scripts.release_notes.release_format import parse_version

_VERSION_DEFINE_RE = re.compile(r'^(#define\s+VALKEY_VERSION\s+)"[^"]*"', re.MULTILINE)
_VERSION_NUM_DEFINE_RE = re.compile(r"^(#define\s+VALKEY_VERSION_NUM\s+)0x[0-9A-Fa-f]+", re.MULTILINE)
_STAGE_DEFINE_RE = re.compile(r'^(#define\s+VALKEY_RELEASE_STAGE\s+)"[^"]*"', re.MULTILINE)

# dev/ga plus rcN, N starting at 1 with no leading zeros ("rc1", "rc12" but not
# "rc0"/"rc01"). The rc sub-pattern matches _RC_STAGE_RE in release_format /
# release_cut; "dev" (the unstable-branch stage) is accepted here but nowhere
# else, so this stays a superset of that regex rather than reusing it.
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
    # parse_version validates the M.m.p range and raises on bad input. Derive the
    # canonical string from the parsed tuple (not the raw input) so VALKEY_VERSION
    # and VALKEY_VERSION_NUM can never disagree: writing the raw string would leave
    # "09.1.0" in VALKEY_VERSION while VALKEY_VERSION_NUM normalized to 0x00090100.
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
    # re.subn returns the substitution count, so count == 1 means the macro was
    # found and rewritten exactly once; count == 0 means it is absent. A count
    # above 1 indicates a duplicated macro definition, which is also a problem.
    missing = [
        name
        for name, count in (
            ("VALKEY_VERSION", n1),
            ("VALKEY_VERSION_NUM", n2),
            ("VALKEY_RELEASE_STAGE", n3),
        )
        if count != 1
    ]
    if missing:
        raise ValueError(
            "expected exactly one definition of each of these macros in version.h, "
            "but they were missing or duplicated: {}".format(", ".join(missing))
        )
    return text
