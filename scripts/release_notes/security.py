"""Auto-populate the Security Fixes section from GitHub Security Advisories.

Fetches published repository advisories and selects those whose patched_versions
match the version being cut. All fields are read verbatim from the advisory;
the AI pipeline never authors this section. Reads from raw_data to work around
PyGithub 2.9.1 quirks with first_patched_version and missing state filters.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)

# M.m.p token with lookarounds to reject 4-component versions.
_VERSION_TOKEN_RE = re.compile(r"(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])")
# Tokens preceded by a comparison operator are range bounds, not fixed versions.
_RANGE_BOUND_RE = re.compile(r"(?:[<>]=?|\s[-~^])\s*$")
# CVE identifier scraped from manual --security-fix entries for dedup.
_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)
# GHSA identifier for dedup when an advisory has no CVE yet.
_GHSA_ID_RE = re.compile(r"GHSA(?:-[0-9A-Za-z]{4}){3}", re.IGNORECASE)


@dataclass(frozen=True)
class AdvisoryFix:
    """One published advisory fixed by this cut's version."""

    display_id: str        # the id shown in parens: CVE if present, else GHSA
    cve_id: str            # "" when the advisory has no CVE assigned yet
    ghsa_id: str
    summary: str           # advisory summary, rendered verbatim as the note text
    html_url: str


@dataclass(frozen=True)
class AdvisorySelection:
    """Outcome of scanning published advisories for the version being cut."""

    matched: tuple[AdvisoryFix, ...] = ()
    considered: int = 0                  # published, non-withdrawn advisories examined
    unmatched_ids: tuple[str, ...] = ()  # published advisories read cleanly that did not match this version
    unreadable_ids: tuple[str, ...] = ()  # published advisories whose patched versions could not be read (MIGHT match)
    fetch_failed: bool = False           # True if the advisory API call failed (e.g. no permission)
    fetch_error: str = ""


def _string_attr(advisory: Any, name: str) -> str:
    """Return a string advisory attribute, falling back to "" on None or error."""
    try:
        value = getattr(advisory, name)
    except Exception:  # noqa: BLE001 - a mis-parsed attribute must not abort the cut
        return ""
    return value if isinstance(value, str) else ""


def _cve_id(advisory: Any) -> str:
    """Return the advisory's CVE id, falling back to the identifiers list."""
    direct = _string_attr(advisory, "cve_id")
    if direct:
        return direct
    try:
        identifiers = advisory.identifiers
    except Exception:  # noqa: BLE001
        identifiers = None
    if not isinstance(identifiers, list):
        return ""
    for ident in identifiers:
        if isinstance(ident, dict) and ident.get("type") == "CVE" and ident.get("value"):
            return str(ident["value"])
    return ""


def _fixed_version_tokens(text: str) -> set[str]:
    """Extract discrete fixed-version tokens from *text*, dropping range bounds."""
    tokens: set[str] = set()
    for m in _VERSION_TOKEN_RE.finditer(text):
        if _RANGE_BOUND_RE.search(text[: m.start()]):
            continue
        tokens.add(m.group(1))
    return tokens


def patched_version_tokens(raw_vulnerabilities: Sequence[Any]) -> set[str]:
    """Collect every discrete fixed M.m.p token from an advisory's raw vulnerabilities.

    Reads both patched_versions and first_patched_version from raw JSON dicts.
    Range bounds are dropped.
    """
    tokens: set[str] = set()
    if not isinstance(raw_vulnerabilities, list):
        return tokens
    for vuln in raw_vulnerabilities:
        if not isinstance(vuln, dict):
            continue
        patched = vuln.get("patched_versions")
        if isinstance(patched, str):
            tokens.update(_fixed_version_tokens(patched))
        fpv = vuln.get("first_patched_version")
        if isinstance(fpv, dict):
            identifier = fpv.get("identifier")
            if isinstance(identifier, str):
                tokens.update(_fixed_version_tokens(identifier))
        elif isinstance(fpv, str):
            tokens.update(_fixed_version_tokens(fpv))
    return tokens


def _render_summary(advisory: Any) -> str:
    """Return the advisory summary as a single line.

    Falls back to the first non-blank line of description, then a placeholder.
    """
    summary = _string_attr(advisory, "summary")
    if summary:
        return " ".join(summary.splitlines()).strip() or "(no summary provided)"
    description = _string_attr(advisory, "description")
    for line in description.splitlines():
        if line.strip():
            return line.strip()
    return "(no summary provided)"


# Sentinel: advisory patched versions could not be read (distinct from non-match).
_UNREADABLE = object()


def _extract_fix(advisory: Any, version: str) -> "Optional[AdvisoryFix] | object":
    """Return an AdvisoryFix if *advisory* is fixed by *version*.

    Returns None on non-match, or _UNREADABLE if raw_data could not be read.
    """
    try:
        raw = advisory.raw_data
    except Exception as exc:  # noqa: BLE001 - a single bad advisory must not abort the cut
        logger.warning("Could not read advisory raw_data: %s", exc)
        return _UNREADABLE
    tokens = patched_version_tokens(raw.get("vulnerabilities", []) if isinstance(raw, dict) else [])
    if version not in tokens:
        return None
    cve_id = _cve_id(advisory)
    ghsa_id = _string_attr(advisory, "ghsa_id")
    display_id = cve_id or ghsa_id
    if not display_id:
        logger.warning("Skipping advisory with no CVE or GHSA id (patched %s)", sorted(tokens))
        return None
    return AdvisoryFix(
        display_id=display_id,
        cve_id=cve_id,
        ghsa_id=ghsa_id,
        summary=_render_summary(advisory),
        html_url=_string_attr(advisory, "html_url"),
    )


def render_bullet(fix: AdvisoryFix) -> str:
    """Render one Security Fixes bullet body: ``(CVE-...) <summary>``.

    No leading ``* `` is added; emit_category prepends the marker.
    """
    return f"({fix.display_id}) {fix.summary}"


def collect_advisory_fixes(repo: Any, version: str) -> AdvisorySelection:
    """Fetch published advisories and select those fixed by *version*.

    Never raises; API failures are captured in the returned selection.
    """
    try:
        advisories = retry_github_call(
            lambda: list(repo.get_repository_advisories()),
            retries=3,
            description="list repository advisories",
        )
    except Exception as exc:  # noqa: BLE001 - degrade, never abort the cut on a fetch failure
        logger.warning("Could not fetch repository advisories: %s", exc)
        return AdvisorySelection(fetch_failed=True, fetch_error=str(exc))

    matched: list[AdvisoryFix] = []
    unmatched_ids: list[str] = []
    unreadable_ids: list[str] = []
    considered = 0
    for advisory in advisories:
        if _string_attr(advisory, "state") != "published":
            continue
        try:
            withdrawn = advisory.withdrawn_at
        except Exception:  # noqa: BLE001
            withdrawn = None
        if withdrawn is not None:
            continue
        considered += 1
        fix = _extract_fix(advisory, version)
        if isinstance(fix, AdvisoryFix):
            matched.append(fix)
        elif fix is _UNREADABLE:
            unreadable_ids.append(_cve_id(advisory) or _string_attr(advisory, "ghsa_id") or "(unknown advisory)")
        else:
            ident = _cve_id(advisory) or _string_attr(advisory, "ghsa_id")
            if ident:
                unmatched_ids.append(ident)

    # Deterministic order and dedup.
    matched.sort(key=lambda f: f.display_id)
    seen: set[str] = set()
    deduped: list[AdvisoryFix] = []
    for fix in matched:
        if fix.display_id in seen:
            continue
        seen.add(fix.display_id)
        deduped.append(fix)

    logger.info(
        "Advisories: %d published, %d fixed by %s, %d other, %d unreadable",
        considered, len(deduped), version, len(unmatched_ids), len(unreadable_ids),
    )
    return AdvisorySelection(
        matched=tuple(deduped),
        considered=considered,
        unmatched_ids=tuple(sorted(unmatched_ids)),
        unreadable_ids=tuple(sorted(unreadable_ids)),
    )


def merge_with_manual(
    matched: Sequence[AdvisoryFix], manual_fixes: Optional[Sequence[str]]
) -> Optional[list[str]]:
    """Merge advisory-derived fixes with hand-supplied --security-fix entries.

    Manual entries win on CVE/GHSA collision. Returns None when nothing remains.
    """
    manual = list(manual_fixes or [])
    manual_cves = {
        cve.upper()
        for entry in manual
        for cve in _CVE_ID_RE.findall(entry)
    }
    manual_ghsas = {
        ghsa.upper()
        for entry in manual
        for ghsa in _GHSA_ID_RE.findall(entry)
    }
    merged = list(manual)
    for fix in matched:
        cve_collision = bool(fix.cve_id) and fix.cve_id.upper() in manual_cves
        ghsa_collision = bool(fix.ghsa_id) and fix.ghsa_id.upper() in manual_ghsas
        if cve_collision or ghsa_collision:
            logger.info("Advisory %s superseded by a manual --security-fix", fix.display_id)
            continue
        merged.append(render_bullet(fix))
    return merged or None
