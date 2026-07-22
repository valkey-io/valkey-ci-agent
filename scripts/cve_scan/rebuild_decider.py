"""Rebuild-fixability decider for CVE scan findings.

Classifies each Finding as fixable (a plain rebuild on the current base would
pick up the patched package) or not-fixable (no upstream fix exists, or the
image already has the fix). This module is the load-bearing safety piece of the
CVE workflow: a wrong "fixable" triggers a needless rebuild, so the logic is
intentionally conservative and deterministic.

Security model: pure code, no network, no subprocess, no AI in the decision path.

Note on version comparison: this module provides a pure-Python comparator used
for the CLASSIFICATION stage (pre-filter, advisory only). It is intentionally
conservative: ambiguous comparisons return None and are routed to not-fixable.
The actual safety gate before dispatch is in base_precheck.py, which uses native
dpkg/apk comparison semantics via docker.
"""

from __future__ import annotations

import re

from scripts.cve_scan.models import Classification, Finding

# ---------------------------------------------------------------------------
# Version comparison helper
# ---------------------------------------------------------------------------

# Regex that splits a version string into segments of digits or non-digit text.
_SEGMENT_RE = re.compile(r"(\d+|[a-zA-Z]+|[.+\-]|~)")


def _parse_segments(version: str) -> list[int | str]:
    """Split a version string into comparable segments.

    Separators (dots, hyphens, plus) are kept to preserve positional alignment.
    Tilde (~) is kept as a distinct token for Debian tilde ordering.
    Numeric segments become ints; alpha and separator segments remain strings.
    """
    segments: list[int | str] = []
    for token in _SEGMENT_RE.findall(version):
        if token.isdigit():
            segments.append(int(token))
        else:
            segments.append(token)
    return segments


def _strip_epoch(version: str) -> "tuple[int, str]":
    """Strip a Debian-style epoch prefix (N:rest).

    Returns (epoch, remainder). Missing epoch = 0.
    """
    if ":" in version:
        epoch_str, _, remainder = version.partition(":")
        try:
            return int(epoch_str), remainder
        except ValueError:
            # Malformed epoch: treat as epoch 0 with full string
            return 0, version
    return 0, version


def _split_debian_revision(version: str) -> "tuple[str, str]":
    """Split a Debian version string into (upstream_version, debian_revision).

    The debian_revision is the part after the LAST hyphen.
    If no hyphen is present, debian_revision is '0'.

    Examples:
      '1.0-1'         -> ('1.0', '1')
      '1.0+deb12u1'   -> ('1.0+deb12u1', '0')
      '3.0.13-1~deb12' -> ('3.0.13', '1~deb12')
      '3.0.12-r0'     -> ('3.0.12', 'r0')   (Alpine uses this)
    """
    if "-" in version:
        idx = version.rfind("-")
        return version[:idx], version[idx + 1:]
    return version, "0"


def _compare_version_part(a: str, b: str) -> "int | None":
    """Compare a single version part (upstream or revision) segment by segment.

    Implements the Debian comparison algorithm for a single version part:
    - Tilde sorts before everything (including end-of-string).
    - Plus suffix: when one string has run out and the other starts with '+',
      the longer one is greater.
    - Returns None on genuine ambiguity (int vs alpha non-separator tokens).
    """
    seg_a = _parse_segments(a)
    seg_b = _parse_segments(b)

    max_len = max(len(seg_a), len(seg_b))
    for i in range(max_len):
        if i >= len(seg_a) and i < len(seg_b):
            next_b = seg_b[i]
            if next_b == "~":
                return 1   # a (shorter) > b (has tilde suffix)
            if next_b == "+":
                return -1  # a (shorter) < b (has + suffix = debian patch)
            return -1  # shorter is older
        if i >= len(seg_b) and i < len(seg_a):
            next_a = seg_a[i]
            if next_a == "~":
                return -1  # a (has tilde suffix) < b (shorter)
            if next_a == "+":
                return 1   # a (has + suffix) > b (shorter)
            return 1   # a has more segments -> newer

        a_tok, b_tok = seg_a[i], seg_b[i]

        # Tilde handling (Debian rule: ~ sorts before everything)
        if a_tok == "~" and b_tok != "~":
            return -1
        if b_tok == "~" and a_tok != "~":
            return 1

        if type(a_tok) is type(b_tok):
            if a_tok < b_tok:  # type: ignore[operator]
                return -1
            if a_tok > b_tok:  # type: ignore[operator]
                return 1
        else:
            a_is_sep = isinstance(a_tok, str) and a_tok in ".+-"
            b_is_sep = isinstance(b_tok, str) and b_tok in ".+-"
            if a_is_sep or b_is_sep:
                continue
            return None  # Genuine ambiguity

    return 0


def _compare_versions(installed: str, fixed: str) -> "int | None":
    """Compare two version strings using Debian-aware semantics.

    Returns:
        -1 if installed < fixed (installed is older, upgrade available)
         0 if installed == fixed
         1 if installed > fixed (installed is newer)
        None if comparison is ambiguous (mixed int-vs-alpha at the same
             position that cannot be resolved by standard rules)

    Handles:
    - Debian epoch: ``N:`` prefix compared numerically first.
    - Debian version structure: splits into (upstream_version, debian_revision)
      on the LAST hyphen, then compares upstream versions and revisions
      independently. This correctly orders:
        * 1.0-1 < 1.0+deb12u1 (upstream 1.0 < upstream 1.0+deb12u1)
        * 3.0.13-1~deb12u1 == 3.0.13-1~deb12u1 (exact match)
    - Tilde ordering: ``~`` sorts before everything including end-of-string
      (Debian policy: 1.0~rc1 < 1.0).
    - Debian ``+`` suffix: ``1.0+deb12u1`` is GREATER than bare ``1.0``
      (Debian policy: 1.0 < 1.0+deb12u1).
    - Alpine ``X.Y.Z-rN`` revisions: treated as upstream=X.Y.Z, rev=rN.
    - Common ``X.Y.Z`` numeric comparisons.

    Conservative on ambiguity: returns None when a mixed int-vs-alpha segment
    pair cannot be resolved, which the caller routes to fixable=False.

    Note: this is the CLASSIFICATION stage comparator (pre-filter, advisory).
    The actual safety gate uses native dpkg/apk via version_compare.py.
    """
    # Epoch comparison
    epoch_a, ver_a = _strip_epoch(installed)
    epoch_b, ver_b = _strip_epoch(fixed)

    if epoch_a != epoch_b:
        return -1 if epoch_a < epoch_b else 1

    # Split into upstream_version + debian_revision on last hyphen
    upstream_a, rev_a = _split_debian_revision(ver_a)
    upstream_b, rev_b = _split_debian_revision(ver_b)

    # Compare upstream versions first
    upstream_cmp = _compare_version_part(upstream_a, upstream_b)
    if upstream_cmp is None:
        return None
    if upstream_cmp != 0:
        return upstream_cmp

    # Upstream versions are equal: compare debian revisions
    return _compare_version_part(rev_a, rev_b)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(finding: Finding) -> Classification:
    """Classify a single finding as rebuild-fixable or not.

    Decision rules (evaluated in order):
    1. No fixed_version available: not fixable (no upstream fix yet).
    2. Version comparison is ambiguous (None): not fixable (conservative).
    3. installed_version >= fixed_version: not fixable (already patched or
       stale finding).
    4. installed_version < fixed_version: fixable (a rebuild on the current
       base would pick up the fix).
    """
    if finding.fixed_version is None:
        return Classification(
            finding=finding,
            fixable=False,
            rationale="No upstream fix yet.",
        )

    cmp = _compare_versions(finding.installed_version, finding.fixed_version)

    if cmp is None:
        # Ambiguous comparison: fail closed (not fixable)
        return Classification(
            finding=finding,
            fixable=False,
            rationale=(
                f"Version comparison ambiguous between "
                f"{finding.installed_version} and {finding.fixed_version} "
                f"for {finding.cve_id}; treating conservatively (not auto-fixable)."
            ),
        )

    if cmp >= 0:
        # installed >= fixed: nothing to do
        return Classification(
            finding=finding,
            fixable=False,
            rationale=(
                f"Installed version {finding.installed_version} is already "
                f"at or above fixed version {finding.fixed_version} for "
                f"{finding.cve_id}. Nothing to do (stale or resolved finding)."
            ),
        )

    # installed < fixed: a rebuild would pick up the fix
    return Classification(
        finding=finding,
        fixable=True,
        rationale=(
            f"A rebuild would upgrade {finding.package} from "
            f"{finding.installed_version} to {finding.fixed_version}, "
            f"resolving {finding.cve_id}."
        ),
    )


def classify_all(findings: list[Finding]) -> list[Classification]:
    """Classify a list of findings. Returns one Classification per Finding."""
    return [classify(f) for f in findings]
