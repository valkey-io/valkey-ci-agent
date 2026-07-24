"""Rebuild-fixability decider for CVE scan findings.

Conservative, deterministic classification: pure code, no network, no
subprocess, no AI in the decision path. Ambiguous version comparisons fail
closed to not-fixable. This comparator is CLASSIFICATION-stage only
(advisory pre-filter); the actual safety gate before dispatch is
base_precheck.py using native dpkg/apk semantics via docker.
"""

from __future__ import annotations

import re

from scripts.cve_scan.models import Classification, Finding

# ---------------------------------------------------------------------------
# Version comparison helper
# ---------------------------------------------------------------------------

# Splits a version string into digit, alpha, separator, and tilde tokens.
_SEGMENT_RE = re.compile(r"(\d+|[a-zA-Z]+|[.+\-]|~)")


def _parse_segments(version: str) -> list[int | str]:
    """Split a version into comparable segments (ints for digits, strings otherwise)."""
    segments: list[int | str] = []
    for token in _SEGMENT_RE.findall(version):
        if token.isdigit():
            segments.append(int(token))
        else:
            segments.append(token)
    return segments


def _strip_epoch(version: str) -> "tuple[int, str]":
    """Strip a Debian-style epoch prefix (N:rest); returns (epoch, remainder), missing epoch = 0."""
    if ":" in version:
        epoch_str, _, remainder = version.partition(":")
        try:
            return int(epoch_str), remainder
        except ValueError:
            # Malformed epoch: treat as epoch 0 with full string
            return 0, version
    return 0, version


def _split_debian_revision(version: str) -> "tuple[str, str]":
    """Split into (upstream_version, debian_revision) on the LAST hyphen; no hyphen -> rev '0'.

    Alpine '3.0.12-r0' -> ('3.0.12', 'r0'); '1.0+deb12u1' -> ('1.0+deb12u1', '0').
    """
    if "-" in version:
        idx = version.rfind("-")
        return version[:idx], version[idx + 1:]
    return version, "0"


def _compare_version_part(a: str, b: str) -> "int | None":
    """Compare one version part segment by segment (Debian rules).

    Tilde sorts before everything (including end-of-string); a '+' suffix
    beats end-of-string. Returns None on genuine ambiguity (int vs alpha).
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
    """Compare two versions with Debian-aware semantics.

    Returns -1/0/1 (installed vs fixed), or None on ambiguity (caller routes
    None to not-fixable). Handles epoch, last-hyphen revision split, tilde
    ordering (1.0~rc1 < 1.0), and '+' suffix (1.0-1 < 1.0+deb12u1). Alpine
    X.Y.Z-rN treated as upstream=X.Y.Z, rev=rN. Classification-stage only;
    the safety gate uses native dpkg/apk via version_compare.py.
    """
    # Epoch comparison
    epoch_a, ver_a = _strip_epoch(installed)
    epoch_b, ver_b = _strip_epoch(fixed)

    if epoch_a != epoch_b:
        return -1 if epoch_a < epoch_b else 1

    upstream_a, rev_a = _split_debian_revision(ver_a)
    upstream_b, rev_b = _split_debian_revision(ver_b)

    upstream_cmp = _compare_version_part(upstream_a, upstream_b)
    if upstream_cmp is None:
        return None
    if upstream_cmp != 0:
        return upstream_cmp

    return _compare_version_part(rev_a, rev_b)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(finding: Finding) -> Classification:
    """Classify a finding as rebuild-fixable or not.

    Rules in order: no fixed_version -> not fixable; ambiguous comparison ->
    not fixable (conservative); installed >= fixed -> not fixable;
    installed < fixed -> fixable.
    """
    if finding.fixed_version is None:
        return Classification(
            finding=finding,
            fixable=False,
            rationale="No upstream fix yet.",
        )

    cmp = _compare_versions(finding.installed_version, finding.fixed_version)

    if cmp is None:
        # Fail closed on ambiguity
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
        return Classification(
            finding=finding,
            fixable=False,
            rationale=(
                f"Installed version {finding.installed_version} is already "
                f"at or above fixed version {finding.fixed_version} for "
                f"{finding.cve_id}. Nothing to do (stale or resolved finding)."
            ),
        )

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
