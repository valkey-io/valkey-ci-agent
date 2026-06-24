"""Render the 00-RELEASENOTES dated release sections.

Owns valkey's release-notes format. A cut hands this module the categorized
bullets for the range (a ``{category: [bullet, ...]}`` map produced by
:mod:`render`) and it renders a dated release section, prepends the release
line's prior dated sections, and appends the cumulative contributor footer.
Upstream ``valkey-io/valkey`` ships no release tooling of its own, so this is
the single authoritative place the dated-section format lives.

The bullets are always carried as an in-memory map and rendered straight into a
dated section: nothing is ever written to a branch as an "unreleased" block.
The render/measure helpers are pure (no I/O, no network) so they are cheap to
unit test.
"""

from __future__ import annotations

import datetime
import re
from typing import Dict, List, Optional, Sequence

# Canonical category order. The generator assigns each bullet to one of these;
# dated sections render them in this order. The set is intentionally exhaustive:
# every release-notes-labelled PR should have a natural home, and "Other Changes"
# is the catch-all so a change that fits none of the specific buckets still lands
# somewhere rather than forcing the model to invent a header. The specific
# categories beyond the original eight (Cluster and Replication, Configuration,
# CLI and Tools) cover surfaces valkey release notes have historically used that
# the eight did not (cluster/replication changes, config option changes, and the
# valkey-cli / valkey-benchmark family), so the model rarely needs the catch-all.
CATEGORIES: List[str] = [
    "Behavior Changes",
    "New Features and Enhanced Behavior",
    "Performance and Efficiency Improvements",
    "Bug Fixes",
    "Command and API Updates",
    "Cluster and Replication",
    "Configuration",
    "Module API Changes",
    "Observability and Logging",
    "CLI and Tools",        # user-facing CLI programs: valkey-cli, valkey-benchmark, etc.
    "Build and Tooling",    # build system, packaging, CI, developer tooling
    "Other Changes",        # catch-all: a user-facing change fitting none of the above
]

# The catch-all bucket. A category the generator returns that is not in
# CATEGORIES is treated as a suggestion, not a new header: the bullet lands here
# (see render.group_bullets) and the suggestion is surfaced in the PR body. Must
# be one of CATEGORIES.
CATCH_ALL_CATEGORY = "Other Changes"

# Security fixes are supplied at release-cut time from the embargo CVE list
# (--security-fix) and render first, ahead of the canonical categories.
SECURITY_CATEGORY = "Security Fixes"

# The contributor list is generated from the commit authors of the release
# range (contributors.py), deduplicated and alpha-sorted, not hand-edited.
CONTRIBUTORS_SECTION = "Contributors"

# Sections that are populated automatically at release time from a factual
# source (the CVE list / the range's commit authors), so a bullet the generator
# assigns to one of these is refused rather than rendered (:mod:`render`'s
# ``group_bullets`` drops it and warns), keeping them the sole source of truth.
RESERVED_SECTIONS = (SECURITY_CATEGORY, CONTRIBUTORS_SECTION)

# Upgrade urgency legend rendered at the top of a release-branch notes file.
URGENCY_LEGEND = """Upgrade urgency levels:

| Level    | Meaning                                                             |
|----------|---------------------------------------------------------------------|
| LOW      | No need to upgrade unless there are new features you want to use.   |
| MODERATE | Program an upgrade of the server, but it's not urgent.              |
| HIGH     | There is a critical bug that may affect a subset of users. Upgrade! |
| CRITICAL | There is a critical bug affecting MOST USERS. Upgrade ASAP.         |
| SECURITY | There are security fixes in the release.                            |"""

VALID_URGENCIES = ("LOW", "MODERATE", "HIGH", "CRITICAL", "SECURITY")

_BULLET_RE = re.compile(r"^\s*[*-]\s+\S")
_DATED_SECTION_RE = re.compile(r"^Valkey\s+\d+\.\d+\.\d+", re.MULTILINE)
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# rcN, N starting at 1 with no leading zeros: "rc1", "rc12" but not "rc0"/"rc01".
_RC_STAGE_RE = re.compile(r"^rc([1-9]\d*)$")

_ORDINALS = [
    "zeroth", "first", "second", "third", "fourth", "fifth", "sixth",
    "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
]


def parse_version(version: str) -> "tuple[int, int, int]":
    """Split ``"M.m.p"`` into integer ``(major, minor, patch)``.

    Each component must be an integer in the inclusive range 0-255 so it fits
    a single byte of ``VALKEY_VERSION_NUM`` (see version_bump.py).
    """
    match = _VERSION_RE.match(version.strip())
    if not match:
        raise ValueError(
            "version must be in the form MAJOR.MINOR.PATCH (e.g. 9.1.0), got {!r}".format(version)
        )
    parts = tuple(int(p) for p in match.groups())
    for component, value in zip(("major", "minor", "patch"), parts):
        if not 0 <= value <= 255:
            raise ValueError(
                "{} version {} is out of range 0-255".format(component, value)
            )
    return parts  # type: ignore[return-value]


def ordinal(n: int) -> str:
    """Return a small ordinal word ("first", "second", ...) or "Nth" fallback."""
    if 0 <= n < len(_ORDINALS):
        return _ORDINALS[n]
    return "{}th".format(n)


def unrecognized_categories(notes: "Dict[str, List[str]]") -> List[str]:
    """Return the names of bullet-bearing categories that are not canonical.

    The generator may assign a bullet to a typo'd (``Bug Fix`` for ``Bug Fixes``)
    or invented (``Networking``) category. Such bullets are still rendered
    verbatim in the dated section (nothing is dropped), but they fall outside
    :data:`CATEGORIES`, so callers warn on them and ask a maintainer to
    recategorize. Reserved sections (:data:`RESERVED_SECTIONS`) are excluded --
    ``group_bullets`` already refuses those. Categories with no bullets are
    ignored. Order follows *notes*.
    """
    known = set(CATEGORIES) | set(RESERVED_SECTIONS)
    return [
        category
        for category, bullets in notes.items()
        if bullets and category not in known
    ]


def _format_date(date: str) -> str:
    """Render *date* as ``"Tue 02 June 2026"``.

    Accepts an ISO ``YYYY-MM-DD`` string (reformatted) or any other string
    (returned unchanged, so callers may pass a pre-formatted display date).
    """
    try:
        parsed = datetime.date.fromisoformat(date.strip())
    except ValueError:
        return date.strip()
    return parsed.strftime("%a %d %B %Y")


def _normalize_stage(stage: str) -> str:
    s = stage.strip().lower()
    if s == "ga":
        return "ga"
    if _RC_STAGE_RE.match(s):
        return s
    raise ValueError("release stage must be 'ga' or 'rcN' (e.g. rc1), got {!r}".format(stage))


def render_header(major: int, minor: int) -> str:
    """Render the file title and urgency legend for a ``M.m`` release line."""
    title = "Valkey {}.{} release notes".format(major, minor)
    underline = "=" * len(title)
    return "{}\n{}\n\n{}".format(title, underline, URGENCY_LEGEND)


def _stage_heading(version: str, stage: str) -> str:
    if stage == "ga":
        return "Valkey {} GA".format(version)
    return "Valkey {}-{}".format(version, stage)


def _urgency_sentence(version: str, stage: str, urgency: str) -> str:
    major, minor, patch = parse_version(version)
    if stage == "ga":
        which = ordinal(patch + 1)  # M.m.0 is the first stable release of M.m
        return (
            "Upgrade urgency {}: This is the {} stable release of Valkey {}.{}.".format(
                urgency, which, major, minor
            )
        )
    rc_num = int(_RC_STAGE_RE.match(stage).group(1))  # type: ignore[union-attr]
    which = ordinal(rc_num)
    return (
        "Upgrade urgency {}: This is the {} release candidate of Valkey {}.".format(
            urgency, which, version
        )
    )


def render_version_section(
    version: str,
    stage: str,
    urgency: str,
    date: str,
    notes: "Dict[str, List[str]]",
    security_fixes: Optional[Sequence[str]] = None,
) -> str:
    """Render one dated release section in release-branch markdown form.

    *notes* maps category name to a list of bullet strings (already including
    the leading ``* ``). Only non-empty categories are emitted, in
    :data:`CATEGORIES` order, with ``Security Fixes`` (from *security_fixes*)
    rendered first. Any non-canonical category (a typo'd or invented header) is
    rendered verbatim *after* the canonical ones so its bullets are never dropped;
    callers warn on them via :func:`unrecognized_categories`. The reserved
    sections (:data:`RESERVED_SECTIONS`) are never read from *notes*: a
    ``Security Fixes`` or ``Contributors`` section a contributor hand-added to the
    block is ignored here, since *security_fixes* is the source of truth for the
    former and the latter is rendered once for the whole file (not per section).
    *security_fixes* is an optional list of CVE bullet strings.

    Contributors are deliberately *not* rendered here: a single cumulative
    ``### Contributors`` footer for the whole file is rendered by
    :func:`render_contributors_footer` and assembled in
    :func:`render_release_notes`.
    """
    stage = _normalize_stage(stage)
    urgency = urgency.strip().upper()
    if urgency not in VALID_URGENCIES:
        raise ValueError(
            "urgency must be one of {}, got {!r}".format(", ".join(VALID_URGENCIES), urgency)
        )

    heading = "{}  -  Released {}".format(_stage_heading(version, stage), _format_date(date))
    underline = "-" * len(heading)
    out: List[str] = [heading, underline, "", _urgency_sentence(version, stage, urgency), ""]

    def emit_category(name: str, bullets: Sequence[str]) -> None:
        out.append("### {}".format(name))
        for bullet in bullets:
            bullet = bullet.strip()
            if not bullet.startswith(("* ", "- ")):
                bullet = "* " + bullet
            out.append(bullet)
        out.append("")

    # Security Fixes come only from *security_fixes* (the embargo CVE list), never
    # from *notes*: group_bullets refuses a bullet the model assigned to a reserved
    # section, so this header cannot be duplicated.
    if security_fixes:
        emit_category(SECURITY_CATEGORY, list(security_fixes))
    for category in CATEGORIES:
        bullets = notes.get(category)
        if bullets:
            emit_category(category, bullets)
    # Non-canonical categories (typo'd or invented headers) are rendered last,
    # verbatim and in their original order, so a miscategorized note is never
    # silently dropped; unrecognized_categories() lets callers warn about them.
    for category in unrecognized_categories(notes):
        emit_category(category, notes[category])

    return "\n".join(out).rstrip() + "\n"


_CONTRIBUTORS_HEADER_RE = re.compile(r"^###\s+Contributors\s*$", re.MULTILINE)


def _strip_bullet(line: str) -> str:
    """Return *line* trimmed of a leading ``* ``/``- `` bullet marker."""
    s = line.strip()
    if s.startswith(("* ", "- ")):
        return s[2:].strip()
    return s


def _split_contributors_footer(text: str) -> "tuple[str, List[str]]":
    """Split *text* at its trailing ``### Contributors`` section.

    Returns ``(body, contributors)`` where *body* is everything before the last
    ``### Contributors`` header (right-stripped) and *contributors* is the list of
    display names parsed from that section (bullet markers removed). When no such
    header exists, returns ``(text, [])``. Using the *last* header means a legacy
    per-section ``### Contributors`` is folded into the cumulative footer on the
    next cut, migrating old files to the single-footer layout.
    """
    matches = list(_CONTRIBUTORS_HEADER_RE.finditer(text))
    if not matches:
        return text, []
    last = matches[-1]
    body = text[: last.start()].rstrip()
    names: List[str] = []
    for line in text[last.end():].splitlines():
        # The footer's bullets run until the next header.
        if line.lstrip().startswith("#"):
            break
        if _BULLET_RE.match(line):
            names.append(_strip_bullet(line))
    return body, names


def render_contributors_footer(contributors: Sequence[str]) -> str:
    """Render the cumulative ``### Contributors`` footer, deduped and alpha-sorted.

    *contributors* is a list of display strings (``"Jane Doe @jdoe"``), possibly
    with duplicates carried across cuts. They are de-duplicated case-insensitively
    (first spelling wins) and sorted by the display-name portion before ``@``,
    matching :mod:`contributors`. Returns ``""`` when the list is empty.
    """
    seen: set = set()
    unique: List[str] = []
    for entry in contributors:
        name = _strip_bullet(entry)
        if not name:
            continue
        key = name.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(name)
    if not unique:
        return ""
    unique.sort(key=lambda e: e.split(" @", 1)[0].casefold())
    out = ["### Contributors"]
    out.extend("* {}".format(name) for name in unique)
    return "\n".join(out)


def _existing_dated_sections(text: str) -> str:
    """Return the dated-section region of a prior changelog (from the first
    ``Valkey M.m.p`` heading onward)."""
    match = _DATED_SECTION_RE.search(text)
    if not match:
        return ""
    return text[match.start():].strip()


def render_release_notes(
    notes: "Dict[str, List[str]]",
    *,
    version: str,
    stage: str,
    urgency: str,
    date: str,
    prior_text: str,
    contributors: Optional[Sequence[str]] = None,
    security_fixes: Optional[Sequence[str]] = None,
) -> str:
    """Render the release line's frozen changelog with a new dated section on top.

    *notes* is the ``{category: [bullet, ...]}`` map for this cut (from
    :func:`render.group_bullets`); it is rendered straight into a dated section --
    there is no intermediate "unreleased" block. *prior_text* is the destination
    release line's existing changelog (the ``pre-release-M.m.p`` / ``M.m`` branch,
    which carries the earlier dated sections); an empty string on a first cut.

    The result is: title + urgency legend, the new dated section, then
    *prior_text*'s previously dated sections, then one cumulative
    ``### Contributors`` footer, and never an "unreleased" block, because the
    release line only ever holds frozen dated sections.
    """
    major, minor, _ = parse_version(version)
    dated = render_version_section(version, stage, urgency, date, notes, security_fixes)

    # Peel off any existing ``### Contributors`` footer from the prior changelog so
    # (a) it is not swept into the dated region below, and (b) its names roll into
    # the new cumulative footer, which is what dedups the roll-up across
    # rc1..rcN..GA.
    before_contrib, prior_contributors = _split_contributors_footer(prior_text)
    existing = _existing_dated_sections(before_contrib)

    parts: List[str] = [render_header(major, minor), "", dated.rstrip()]
    if existing:
        parts += ["", existing]

    # One cumulative ``### Contributors`` footer for the whole file: this cut's
    # contributors (commit authors over the release range, so everyone whose code
    # shipped is credited even without a release-note bullet) merged with those
    # carried on the prior changelog, deduped and alpha-sorted.
    merged = list(prior_contributors) + list(contributors or [])
    footer = render_contributors_footer(merged)
    if footer:
        parts += ["", footer]

    return "\n".join(parts).rstrip() + "\n"
