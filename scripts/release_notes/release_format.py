"""Render the 00-RELEASENOTES dated release sections.

Owns valkey's release-notes format: renders categorized bullets into a dated
section, prepends the release line's prior sections, and appends the cumulative
contributor footer. All helpers are pure (no I/O).
"""

from __future__ import annotations

import datetime
import re
from typing import Dict, List, Optional, Sequence

# Canonical category order; dated sections render in this order.
# Render order. Features lead: the notes open with what the release ADDS, per
# maintainer review of the first 9.2.0-rc1 cut. Behavior Changes sits at the
# bottom (before only the catch-all): important for upgraders, but it reads as
# a warning list, not as the release's story.
CATEGORIES: List[str] = [
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
    "Behavior Changes",
    "Other Changes",        # catch-all: a user-facing change fitting none of the above
]

# Catch-all for bullets with non-canonical categories. Must be in CATEGORIES.
CATCH_ALL_CATEGORY = "Other Changes"

# Renders first, ahead of canonical categories.
SECURITY_CATEGORY = "Security Fixes"

# Generated from commit authors of the release range, deduped and alpha-sorted.
CONTRIBUTORS_SECTION = "Contributors"

# Auto-populated at release time; model-assigned bullets under these are refused.
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

# Canonical patch-release rationale. Historical Valkey patch releases explain
# why a user should upgrade; they do not describe themselves as the Nth stable
# release. SECURITY is canonical for every stage because the security reason is
# more important than GA/RC sequencing.
_PATCH_URGENCY_SENTENCES = {
    "LOW": "No need to upgrade unless there are new features you want to use.",
    "MODERATE": "Program an upgrade of the server, but it's not urgent.",
    "HIGH": "There is a critical bug that may affect a subset of users. Upgrade!",
    "CRITICAL": "There is a critical bug affecting MOST USERS. Upgrade ASAP.",
    "SECURITY": (
        "This release includes security fixes we recommend you apply as soon as possible."
    ),
}

# A rendered note bullet ends with "(#N)" naming the PR it credits. The
# bullet-line guard keeps a "(#N)" in prose or a heading from being read as a
# credit. Shared by the cut's already-credited dedup and by the cross-line
# wording index (see :mod:`scripts.release_notes.prior_notes`); one grammar so
# the two cannot drift apart.
BULLET_LINE_RE = re.compile(r"^\s*[*-]\s+\S")
# Trailing PR group: "(#N)" or a hand-written "(#N, #M)" at end of line,
# tolerating trailing punctuation/closing parens. Generated notes always emit a
# single canonical ref, while existing module changelogs sometimes credit
# several PRs in one trailing group.
TRAILING_PR_GROUP_RE = re.compile(
    r"\((?P<refs>#\d+(?:\s*,\s*#\d+)*)\)[\s.,:;)]*$"
)
PR_NUMBER_RE = re.compile(r"#(\d+)")
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
# rcN, N starting at 1 with no leading zeros: "rc1", "rc12" but not "rc0"/"rc01".
_RC_STAGE_RE = re.compile(r"^rc([1-9]\d*)$")

_ORDINALS = [
    "zeroth", "first", "second", "third", "fourth", "fifth", "sixth",
    "seventh", "eighth", "ninth", "tenth", "eleventh", "twelfth",
]


def parse_version(version: str) -> "tuple[int, int, int]":
    """Split ``"M.m.p"`` into integer ``(major, minor, patch)``.

    All profiles intentionally limit each component to 0-255. Core requires
    one-byte components for ``VALKEY_VERSION_NUM``; module profiles share this
    controller-wide contract so branch, tag, changelog, and version-file
    validation cannot diverge by repository.
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


def unrecognized_categories(
    notes: "Dict[str, List[str]]",
    categories: Sequence[str],
) -> List[str]:
    """Return category names in *notes* that are not in *categories*.

    Reserved sections are excluded. Empty categories are ignored.
    """
    known = set(categories) | set(RESERVED_SECTIONS)
    return [
        category
        for category, bullets in notes.items()
        if bullets and category not in known
    ]


def trailing_pr_numbers(line: str) -> "set[int]":
    """Return every PR number in a line's final ``(#N[, #M...])`` group."""
    match = TRAILING_PR_GROUP_RE.search(line)
    if match is None:
        return set()
    return {int(number) for number in PR_NUMBER_RE.findall(match.group("refs"))}


def credited_pr_numbers(notes_text: str) -> "set[int]":
    """Return the PR numbers a release-line changelog already credits.

    Reads every bullet line's trailing local ``(#N[, #M...])`` reference from
    *notes_text*. GitHub issue and PR numbers are unique within a repository, so
    a trailing local reference is a valid credit regardless of section heading.
    """
    credited: set[int] = set()
    for line in notes_text.splitlines():
        if BULLET_LINE_RE.match(line):
            credited.update(trailing_pr_numbers(line))
    return credited


def _format_date(date: str) -> str:
    """Render *date* as ``"Tue 02 June 2026"``. Passes non-ISO strings unchanged."""
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


def render_header(major: int, minor: int, display_name: str) -> str:
    """Render the file title and urgency legend for a ``M.m`` release line."""
    title = "{} {}.{} release notes".format(display_name, major, minor)
    underline = "=" * len(title)
    return "{}\n{}\n\n{}".format(title, underline, URGENCY_LEGEND)


def stage_heading(version: str, stage: str, display_name: str) -> str:
    """The version part of a dated heading: ``<Name> M.m.p[-rcN| GA]``."""
    if stage == "ga":
        # Valkey uses "GA" for the initial M.m.0 release, while patch release
        # headings are simply "<Name> M.m.p".
        _major, _minor, patch = parse_version(version)
        return "{} {}{}".format(display_name, version, " GA" if patch == 0 else "")
    return "{} {}-{}".format(display_name, version, stage)


def _urgency_sentence(version: str, stage: str, urgency: str, display_name: str) -> str:
    major, minor, patch = parse_version(version)
    if urgency == "SECURITY" or (stage == "ga" and patch > 0):
        return "Upgrade urgency {}: {}".format(
            urgency, _PATCH_URGENCY_SENTENCES[urgency]
        )
    if stage == "ga":
        which = ordinal(patch + 1)  # M.m.0 is the first stable release of M.m
        return (
            "Upgrade urgency {}: This is the {} stable release of {} {}.{}.".format(
                urgency, which, display_name, major, minor
            )
        )
    rc_num = int(_RC_STAGE_RE.match(stage).group(1))  # type: ignore[union-attr]
    which = ordinal(rc_num)
    return (
        "Upgrade urgency {}: This is the {} release candidate of {} {}.".format(
            urgency, which, display_name, version
        )
    )


def render_version_section(
    version: str,
    stage: str,
    urgency: str,
    date: str,
    notes: "Dict[str, List[str]]",
    security_fixes: Optional[Sequence[str]] = None,
    *,
    display_name: str,
    categories: Sequence[str],
) -> str:
    """Render one dated release section in release-branch markdown form.

    Emits Security Fixes (from *security_fixes*) first, then canonical
    categories in order, then any non-canonical categories last. Contributors
    are rendered separately as a cumulative file footer. *categories* is the
    profile's canonical ordering.
    """
    ordered_categories = categories
    stage = _normalize_stage(stage)
    urgency = urgency.strip().upper()
    if urgency not in VALID_URGENCIES:
        raise ValueError(
            "urgency must be one of {}, got {!r}".format(", ".join(VALID_URGENCIES), urgency)
        )

    heading = "{}  -  Released {}".format(
        stage_heading(version, stage, display_name), _format_date(date)
    )
    underline = "-" * len(heading)
    out: List[str] = [
        heading, underline, "",
        _urgency_sentence(version, stage, urgency, display_name), "",
    ]

    def emit_category(name: str, bullets: Sequence[str]) -> None:
        out.append("### {}".format(name))
        for bullet in bullets:
            out.append("* " + _strip_bullet(bullet))
        out.append("")

    if security_fixes:
        emit_category(SECURITY_CATEGORY, list(security_fixes))
    for category in ordered_categories:
        bullets = notes.get(category)
        if bullets:
            emit_category(category, bullets)
    # Non-canonical categories rendered last so nothing is silently dropped.
    for category in unrecognized_categories(notes, ordered_categories):
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
    such header and *contributors* is the list of display names from that
    section. Repositories are expected to normalize historical contributor
    blocks to this canonical format before enabling automated cuts.
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
        if BULLET_LINE_RE.match(line):
            names.append(_strip_bullet(line))
    return body, names


def render_contributors_footer(contributors: Sequence[str]) -> str:
    """Render the cumulative footer, deduped by login/display-name identity.

    Returns ``""`` when the list is empty.
    """
    records: List[tuple[str, str, str]] = []
    for entry in contributors:
        rendered = _strip_bullet(entry)
        if not rendered:
            continue
        # (?:^|\s) accepts a canonical footer entry with either a display name
        # and handle or just a bare handle, keying both forms by login.
        handle_match = re.search(r"(?:^|\s)@([A-Za-z0-9-]+)$", rendered)
        handle = handle_match.group(1).casefold() if handle_match else ""
        display = (
            rendered[:handle_match.start()].strip()
            if handle_match
            else rendered
        )
        display_key = display.casefold()
        # A name-only identity (e.g. a co-author trailer with no resolvable
        # login) merges with a handle it spells: "KarthikSubbarao" is
        # "@KarthikSubbarao". Spaces are dropped so "Karthik Subbarao" also
        # merges with that handle.
        squashed = display_key.replace(" ", "")
        matches = [
            index
            for index, (known_name, known_handle, _known_rendered) in enumerate(records)
            if (handle and known_handle == handle)
            or (display_key and known_name == display_key)
            or (handle and known_name.replace(" ", "") == handle)
            or (squashed and known_handle == squashed)
        ]
        if not matches:
            records.append((display_key, handle, rendered))
            continue

        # A later handled entry is the freshest API identity and replaces stale
        # profile names or name-only fallbacks carried from an older footer.
        winner = (display_key, handle, rendered)
        if not handle:
            winner = next(
                (records[index] for index in matches if records[index][1]),
                records[matches[0]],
            )
        for index in reversed(matches):
            del records[index]
        records.append(winner)

    if not records:
        return ""
    unique = sorted(
        (rendered for _name, _handle, rendered in records),
        key=lambda entry: entry.rsplit(" @", 1)[0].lstrip("@").casefold(),
    )
    out = ["### Contributors"]
    out.extend("* {}".format(name) for name in unique)
    return "\n".join(out)


def _dated_section_re(display_name: str) -> "re.Pattern[str]":
    """Line-anchored ``<display_name> M.m.p`` heading matcher."""
    return re.compile(
        r"^{}\s+\d+\.\d+\.\d+".format(re.escape(display_name)), re.MULTILINE
    )


def has_dated_section(text: str, display_name: str) -> bool:
    """Whether *text* contains any ``<display_name> M.m.p`` dated heading."""
    return dated_section_start(text, display_name) is not None


def dated_section_start(text: str, display_name: str) -> "Optional[int]":
    """Return the offset of the first dated section, or ``None`` when absent."""
    match = _dated_section_re(display_name).search(text)
    return match.start() if match else None


def _dated_release_key_re(display_name: str) -> "re.Pattern[str]":
    """Matcher for a dated heading's version and stage.

    Accepts every stage spelling this module emits or has emitted historically:
    ``M.m.p``, ``M.m.p GA``, ``M.m.p-rcN``, and the older ``M.m.p RCN``.
    """
    return re.compile(
        r"^{}\s+(\d+)\.(\d+)\.(\d+)"
        r"(?:[-\s]+[rR][cC]\s*([1-9]\d*)|\s+GA)?"
        r"(?=\s|$)".format(re.escape(display_name))
    )


def dated_release_key(
    line: str,
    display_name: str,
) -> "Optional[tuple[int, int, int, int, int]]":
    """Parse a dated heading into an ascending publication-order key.

    Returns ``(major, minor, patch, 0 if rc else 1, rc_number)`` for a
    ``<display_name> M.m.p[-rcN| GA]`` heading, or ``None`` when *line* is not
    one. Sorting ascending replays a release line in the order it was published:
    older versions first, and for one version its release candidates before its
    GA. Used to pick the earliest published wording of a note (see
    :mod:`scripts.release_notes.prior_notes`).
    """
    match = _dated_release_key_re(display_name).match(line.strip())
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.group(1, 2, 3))
    rc = match.group(4)
    if rc is None:
        return (major, minor, patch, 1, 0)
    return (major, minor, patch, 0, int(rc))


def _existing_dated_sections(text: str, display_name: str) -> str:
    """Return text from the first ``<display_name> M.m.p`` heading onward.

    The heading name must match the selected profile's ``display_name``. A
    mismatched value would fail to recognize the profile's prior history.
    """
    start = dated_section_start(text, display_name)
    if start is None:
        return ""
    return text[start:].strip()


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
    display_name: str,
    categories: Sequence[str],
) -> str:
    """Render the full changelog with a new dated section prepended.

    Assembles: title + urgency legend, the new section, prior dated sections,
    and a cumulative Contributors footer.
    """
    major, minor, _ = parse_version(version)
    dated = render_version_section(
        version, stage, urgency, date, notes, security_fixes,
        display_name=display_name, categories=categories,
    )

    before_contrib, prior_contributors = _split_contributors_footer(prior_text)
    existing = _existing_dated_sections(before_contrib, display_name)

    parts: List[str] = [render_header(major, minor, display_name), "", dated.rstrip()]
    if existing:
        parts += ["", existing]

    # Merge this cut's contributors with prior ones for the cumulative footer.
    merged = list(prior_contributors) + list(contributors or [])
    footer = render_contributors_footer(merged)
    if footer:
        parts += ["", footer]

    return "\n".join(parts).rstrip() + "\n"
