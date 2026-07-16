"""Recover the original PR of a backported commit for release-note attribution.

Reads the ``## Applied`` table from sweep commits, the ``(cherry picked from
commit <sha>)`` trailer from -x picks, and the backport branch name to trace a
range commit back to its source PR.
"""

from __future__ import annotations

import re

# Matches the ``(cherry picked from commit <sha>)`` trailer git appends with -x.
_CHERRY_PICK_TRAILER_RE = re.compile(
    r"(?im)^[ \t]*\(cherry picked from commit ([0-9a-f]{7,40})\)[ \t]*$"
)

# Matches a "[Backport <branch>] ..." PR title prefix.
_BACKPORT_TITLE_RE = re.compile(r"^\s*\[Backport\b", re.IGNORECASE)

# Captures and strips the "[Backport <branch>] " prefix to reveal the source title.
_BACKPORT_TITLE_PREFIX_RE = re.compile(r"^\s*\[Backport\b[^\]]*\]\s*", re.IGNORECASE)

# Matches a PR reference cell: "#123" or "[#123](url)".
_PR_CELL_RE = re.compile(r"^(?:\[)?#(\d+)(?:\]\([^)]*\))?$")

# Matches "backport/<source_pr>-to-<branch>" (optionally "agent/" prefixed).
_BACKPORT_BRANCH_RE = re.compile(r"^(?:agent/)?backport/(\d+)-to-")


def is_backport_title(title: str) -> bool:
    """True if *title* has a ``[Backport ...]`` prefix."""
    return bool(_BACKPORT_TITLE_RE.match(title))


def source_title_from_backport_title(title: str) -> str | None:
    """Strip the ``[Backport <branch>] `` prefix and return the source title, or None."""
    stripped = _BACKPORT_TITLE_PREFIX_RE.sub("", title or "")
    if stripped == (title or ""):  # no prefix matched
        return None
    stripped = stripped.strip()
    return stripped or None


def cherry_pick_source_shas(commit_message: str) -> list[str]:
    """Return source SHAs from ``(cherry picked from commit <sha>)`` trailers.

    Returned in file order (oldest hop first). Empty list if no trailer present.
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
    """Extract source PR numbers from the ``## Applied`` markdown table in *body*."""
    applied = _markdown_section(body, "Applied")
    if not applied:
        return set()
    rows: list[str] = []
    for line in applied.splitlines():
        if line.lstrip().startswith("|"):
            rows.append(line)
        elif rows:
            rows[-1] += " " + line.strip()  # fold wrapped cell

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
                column = 0  # fallback: sweep lists source PR first
            if any(cell.strip().lower() == "source pr" for cell in cells):
                continue  # skip the header row
        if all(set(cell) <= set("-: ") for cell in cells if cell):
            continue  # separator row (|---|---|)
        if column < len(cells):
            match = _PR_CELL_RE.match(cells[column])
            if match:
                numbers.add(int(match.group(1)))
    return numbers


def summary_source_pr_from_body(body: str) -> int | None:
    """Return the source PR number from the ``## Backport Summary`` table, or None."""
    cell = _summary_value_cell(body, "source pr")
    if cell is None:
        return None
    match = _PR_CELL_RE.match(cell)
    return int(match.group(1)) if match else None


def summary_source_title_from_body(body: str) -> str | None:
    """Return the source title from the ``## Backport Summary`` table, or None."""
    cell = _summary_value_cell(body, "source title")
    return cell or None


def _summary_value_cell(body: str, label: str) -> str | None:
    """Return the value cell for *label* in the ``## Backport Summary`` table, or None."""
    summary = _markdown_section(body, "Backport Summary")
    if not summary:
        return None
    rows: list[str] = []
    for line in summary.splitlines():
        if line.lstrip().startswith("|"):
            rows.append(line)
        elif rows:
            rows[-1] += " " + line.strip()  # fold wrapped cell
    for row in rows:
        cells = _split_table_row(row)
        if len(cells) < 2 or cells[0].strip().lower() != label:
            continue
        return cells[1]
    return None


def _split_table_row(row: str) -> list[str]:
    """Split a markdown table row on unescaped ``|`` and unescape ``\\|`` in cells."""
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
    """Extract the source PR number from a per-PR backport branch name, or None."""
    if not head_ref:
        return None
    match = _BACKPORT_BRANCH_RE.match(head_ref)
    return int(match.group(1)) if match else None
