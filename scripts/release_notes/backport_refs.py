"""Recover the *original* PR of a backported commit, for release-note attribution.

A change reaches a release line as a backport: a squash-merged sweep whose
commit subject is the *backport* PR, or a ``-x`` cherry-pick whose subject was
rewritten. In both cases the trailing ``(#N)`` no longer names the PR that
introduced the change. These helpers read the two artifacts that still point at
the original: the ``## Applied`` table a sweep writes into its commit body, and
the ``(cherry picked from commit <sha>)`` trailer ``git cherry-pick -x`` appends,
so :mod:`scripts.release_notes.discover` can credit the original PR and its
author rather than the backport.

Note on the trailer: only ``git cherry-pick -x`` writes it, and this repo's
*backport* tooling (:mod:`scripts.backport`) picks without ``-x`` (so a tool-made
backport keeps its source subject ``(#N)`` and is recovered by that instead). The
trailer therefore comes from a ``-x`` pick made elsewhere: a maintainer's
hand-applied pick, or the ci-fix port path (:mod:`scripts.ci_fix.push`).

The ``## Applied`` parser is a second copy of the one in
:mod:`scripts.backport.mark_done`; keep it in step with the table
:mod:`scripts.backport.sweep_reporting` emits (a ``Source PR`` column) if that
format changes.
"""

from __future__ import annotations

import re

# ``git cherry-pick -x`` appends this trailer naming the commit it was picked
# from. It is the one artifact that survives a subject rewrite, so it lets
# discovery walk a hand-applied cherry-pick back to its original commit (and
# thus the original PR) even when the subject no longer carries the source
# ``(#N)``. Case-insensitive, tolerant of surrounding whitespace; git writes the
# full 40-hex id, but 7+ is accepted for hand-written or abbreviated trailers.
_CHERRY_PICK_TRAILER_RE = re.compile(
    r"(?im)^[ \t]*\(cherry picked from commit ([0-9a-f]{7,40})\)[ \t]*$"
)

# A leading "[Backport <branch>] ..." title, as scripts.backport builds it. Used
# to flag when the PR resolved for a range commit is itself a backport rather
# than the original.
_BACKPORT_TITLE_RE = re.compile(r"^\s*\[Backport\b", re.IGNORECASE)

# The same "[Backport <branch>] " prefix, but matched through the closing bracket
# and trailing space so the *source title* that follows can be captured. Anchored
# with the same "^\s*\[Backport\b" as _BACKPORT_TITLE_RE so the two never disagree
# on what a backport title looks like; "[^\]]*" spans the "<branch>" part up to the
# first "]". scripts.backport.utils.build_pr_title embeds the source PR's title
# verbatim after this prefix, so what remains is the source title.
_BACKPORT_TITLE_PREFIX_RE = re.compile(r"^\s*\[Backport\b[^\]]*\]\s*", re.IGNORECASE)

# A table cell holding a PR reference: "#123" or a markdown link "[#123](url)".
_PR_CELL_RE = re.compile(r"^(?:\[)?#(\d+)(?:\]\([^)]*\))?$")

# A per-PR backport branch, as scripts.backport.utils.build_branch_name builds it
# ("backport/<source_pr>-to-<branch>", optionally under an "agent/" namespace).
# The captured group is the *source* (original) PR number, so a per-PR backport
# whose commit subject was rewritten to the backport PR can still be walked back
# to its origin from the head-branch name alone.
_BACKPORT_BRANCH_RE = re.compile(r"^(?:agent/)?backport/(\d+)-to-")


def is_backport_title(title: str) -> bool:
    """Return ``True`` if *title* is a ``[Backport ...]`` PR title.

    Signals that discovery resolved a range commit to a backport PR (a
    squash-merged sweep or a per-PR backport) rather than the original: no
    ``## Applied`` table, ``-x`` trailer, or original ``(#N)`` was available, so
    the note would credit the backport, not the change's author.
    """
    return bool(_BACKPORT_TITLE_RE.match(title))


def source_title_from_backport_title(title: str) -> str | None:
    """The source PR title embedded after a ``[Backport <branch>] `` prefix, or ``None``.

    :func:`scripts.backport.utils.build_pr_title` builds a backport title as
    ``[Backport <branch>] <source_pr_title>``, copying the source title verbatim.
    Stripping the prefix therefore recovers the title the backport *claims* it
    carried, which discovery cross-checks against the actual title of the recovered
    source PR (an independent witness to the recovered ``#N``). Returns ``None`` for
    a title with no ``[Backport ...]`` prefix, or one whose remainder is empty (a
    bare ``[Backport 9.1]`` with no title after it).
    """
    stripped = _BACKPORT_TITLE_PREFIX_RE.sub("", title or "")
    if stripped == (title or ""):  # no prefix matched: not a "[Backport ...]" title
        return None
    stripped = stripped.strip()
    return stripped or None


def cherry_pick_source_shas(commit_message: str) -> list[str]:
    """Return the source SHAs named by ``(cherry picked from commit <sha>)`` trailers.

    A commit picked through several branches (e.g. unstable -> 9.0 -> 8.0)
    accumulates one trailer per hop. ``git cherry-pick -x`` *appends* its trailer,
    so an earlier hop's inherited trailer stays above the one the latest hop adds:
    file order is oldest-hop-first, most-recent-hop-last. Returned in that file
    order; an empty list means the message carried no such trailer (a plain
    cherry-pick made without ``-x``).
    """
    return _CHERRY_PICK_TRAILER_RE.findall(commit_message)


def _markdown_section(body: str, heading: str) -> str:
    """Return the body of the ``## <heading>`` section, or ``""`` if absent."""
    pattern = re.compile(
        rf"(?ims)^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)"
    )
    match = pattern.search(body)
    return match.group(1) if match else ""


def applied_source_prs_from_body(body: str) -> set[int]:
    """Source PR numbers listed in the ``## Applied`` table of a backport commit body.

    A squash-merged backport sweep records the original source PRs it carried
    only in an ``## Applied`` markdown table; the squash commit's *subject* is
    the backport PR, so the source PRs are recoverable only from this table.
    Only the ``Source PR`` column is read (falling back to the first column when
    no header names it, since the sweep always lists the source PR first), so a
    ``#N`` in a Title/Detail cell or in a ``## Needs attention`` row is never
    counted. Cells wrapped across newlines are reassembled first.
    """
    applied = _markdown_section(body, "Applied")
    if not applied:
        return set()
    rows: list[str] = []
    for line in applied.splitlines():
        if line.lstrip().startswith("|"):
            rows.append(line)
        elif rows:
            # A cell wrapped across newlines: fold the continuation into the row.
            rows[-1] += " " + line.strip()

    column: int | None = None
    numbers: set[int] = set()
    for row in rows:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if column is None:
            for index, cell in enumerate(cells):
                if cell.strip().lower() == "source pr":
                    column = index
                    break
            else:
                column = 0  # no header row; sweep lists the source PR first
            if any(cell.strip().lower() == "source pr" for cell in cells):
                continue  # consumed the header row itself
        if all(set(cell) <= set("-: ") for cell in cells if cell):
            continue  # separator row (|---|---|)
        if column < len(cells):
            match = _PR_CELL_RE.match(cells[column])
            if match:
                numbers.add(int(match.group(1)))
    return numbers


def summary_source_pr_from_body(body: str) -> int | None:
    """Original PR named by the ``## Backport Summary`` table of a *per-PR* backport.

    A per-PR backport PR (as :mod:`scripts.backport.pr_creator` opens it) carries a
    ``## Backport Summary`` table that is *transposed* relative to the sweep's
    ``## Applied`` table: it is a ``| Field | Value |`` table whose rows are
    ``| Source PR | [#N](url) |``, ``| Source title | ... |``, etc. The source
    PR is a **row label** in the first column, with the ``#N`` in the second, not a
    column of its own, so it needs a row scan, not the column scan
    :func:`applied_source_prs_from_body` does.

    Returns the source PR number, or ``None`` when there is no ``## Backport
    Summary`` section or no parseable ``Source PR`` row (e.g. a non-backport PR, or
    a body whose format has drifted). Only the ``Source PR`` row's value cell is
    read, so a ``#N`` in the ``Source title`` cell or elsewhere is never counted.
    """
    cell = _summary_value_cell(body, "source pr")
    if cell is None:
        return None
    match = _PR_CELL_RE.match(cell)
    return int(match.group(1)) if match else None


def summary_source_title_from_body(body: str) -> str | None:
    """Source title named by the ``## Backport Summary`` table's ``Source title`` row.

    :mod:`scripts.backport.pr_creator` writes a ``| Source title | <title> |`` row
    beside the ``Source PR`` row, copying the source PR's title verbatim. It is a
    witness to the recovered source PR that is independent of the ``#N`` in the
    ``Source PR`` row, so discovery can cross-check the recovered PR's actual title
    against it. Returns the value cell verbatim (not parsed), or ``None`` when there
    is no ``## Backport Summary`` section or no ``Source title`` row.
    """
    cell = _summary_value_cell(body, "source title")
    return cell or None


def _summary_value_cell(body: str, label: str) -> str | None:
    """The value cell of the ``## Backport Summary`` row whose label cell is *label*.

    The summary is a transposed ``| Field | Value |`` table, so a field is a row
    label in the first cell with its value in the second. Returns ``cells[1]``
    (stripped) for the first row whose first cell equals *label* (case-insensitive),
    or ``None`` when there is no ``## Backport Summary`` section or no such row.
    Cells wrapped across newlines are reassembled first. Pipe characters inside
    cell content are escaped as ``\\|`` by the emitter
    (:func:`scripts.backport.pr_creator._escape_table_cell`), so splitting must
    honour that escape and unescape after extraction.
    """
    summary = _markdown_section(body, "Backport Summary")
    if not summary:
        return None
    rows: list[str] = []
    for line in summary.splitlines():
        if line.lstrip().startswith("|"):
            rows.append(line)
        elif rows:
            # A cell wrapped across newlines: fold the continuation into the row.
            rows[-1] += " " + line.strip()
    for row in rows:
        cells = _split_table_row(row)
        if len(cells) < 2 or cells[0].strip().lower() != label:
            continue
        return cells[1]
    return None


def _split_table_row(row: str) -> list[str]:
    """Split a markdown table row on unescaped ``|`` delimiters and unescape cells.

    A literal pipe inside cell content is stored as ``\\|`` (the emitter's escape).
    A naive ``.split("|")`` truncates such cells. This splits character-by-character,
    skipping escaped pipes, then unescapes each cell. Mirrors the approach in
    :func:`scripts.backport.sweep_reporting._split_markdown_table_row`.
    """
    text = row.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in text:
        if char == "\\" and not escaped:
            escaped = True
            current.append(char)
            continue
        if char == "|" and not escaped:
            cells.append("".join(current).strip().replace("\\|", "|"))
            current = []
            continue
        current.append(char)
        escaped = False
    cells.append("".join(current).strip().replace("\\|", "|"))
    return cells


def source_pr_from_branch(head_ref: str) -> int | None:
    """Original PR number encoded in a per-PR backport's head branch, or ``None``.

    :func:`scripts.backport.utils.build_branch_name` names a per-PR backport branch
    ``backport/<source_pr>-to-<branch>`` (optionally under ``agent/``), so the head
    ref carries the original PR number even when the squash-merge subject was
    rewritten to the backport PR. Returns ``None`` for any other branch (including
    the sweep's ``agent/backport/sweep-...`` namespace, which batches many sources
    and encodes none in the name).
    """
    if not head_ref:
        return None
    match = _BACKPORT_BRANCH_RE.match(head_ref)
    return int(match.group(1)) if match else None
