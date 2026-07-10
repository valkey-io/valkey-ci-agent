"""Auto-populate the ``### Security Fixes`` section from GitHub Security Advisories.

Security fixes are the one release-notes section the AI pipeline never authors:
they are factual, embargoed, and must not be hallucinated (see
:mod:`render`'s reserved-section handling). Historically a maintainer supplied
each bullet by hand via ``--security-fix``. This module lets the cut also pull
published repository advisories and render the ones fixed by the version being
cut, keeping the AI out of it entirely: every field here is read from the
advisory verbatim.

Why version-match and not the PR graph-walk (:mod:`discover`):
a repository advisory carries no structured pointer to the fixing commit or PR
(no ``references``, no commit SHA, no PR number). The only structured link to a
release is a per-vulnerability version string (``patched_versions`` /
``first_patched_version``), which is author-typed metadata. So an advisory is
tied to this cut only by matching that string against the version being cut; the
tag..head PR set has no join key to intersect with. Because the match is against
unverified metadata, and embargoed or draft advisories are invisible to the
token, the caller surfaces a disclaimer telling a maintainer to add any others by
hand.

Two PyGithub 2.9.1 quirks this module works around:

* ``AdvisoryVulnerability.first_patched_version`` is declared ``str`` but the REST
  API returns an object ``{"identifier": "9.1.0"}``, so reading the *property*
  raises ``BadAttributeException``. We read version tokens from ``raw_data``
  instead, which keeps the JSON shape.
* ``Repository.get_repository_advisories()`` takes no ``state`` filter, so we
  fetch all and keep only ``state == "published"`` (and not withdrawn) ourselves.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from scripts.common.github_client import retry_github_call

logger = logging.getLogger(__name__)

# A canonical M.m.p version token. We read only the patched-version fields
# (patched_versions / first_patched_version), which name the fixed version(s):
# a bare version ("9.0.5") or a comma list of backport targets ("8.0.4, 9.0.5"),
# NOT the vulnerable_version_range field (which we never read). We extract every
# M.m.p token and test exact membership (never range math), so a fix maps only
# to the versions that actually shipped it.
#
# The lookarounds reject an adjacent digit or dot on either side, so a
# 4-component version ("9.1.0.5") or an extra leading component ("1.9.1.0") is
# not truncated into a bogus 3-component token. A word boundary (\b) would not
# help here: "." is a non-word char, so \b sits happily between "9.1.0" and the
# trailing ".5", matching a version this release never shipped.
_VERSION_TOKEN_RE = re.compile(r"(?<![\d.])(\d+\.\d+\.\d+)(?![\d.])")
# A range operator directly governing a token marks it as a range bound, not a
# discrete fixed version. patched_versions is contractually a bare version or comma
# list of fixed backport targets (a range lives in vulnerable_version_range, which
# we never read), but an author typo or an unexpected payload could put a range
# here. Because we do exact membership and never range math, a bound would falsely
# mark a version as fixed: "< 9.1.0" names the first unaffected release and
# ">= 9.0.0" names the vulnerable floor, neither of which shipped the fix. Drop a
# token preceded by a comparison operator ("<"/">"/"<="/">=") or a spaced range
# dash/tilde/caret. This catches the upper (right-hand) end of a spaced range,
# e.g. the "9.1.0" in "8.0.0 - 9.1.0"; the lower "8.0.0" has nothing before it and
# is kept (a residual gap, alongside an unspaced "9.0.0-9.1.0"). A version with an
# attached pre-release suffix ("9.1.0-rc1") keeps its token: the dash there is not
# whitespace-surrounded, so it is not read as a range. patched_versions never
# legitimately holds a range, and the PR-body disclaimer asks a maintainer to add
# anything missed, so the residual gaps are harmless.
_RANGE_BOUND_RE = re.compile(r"(?:[<>]=?|\s[-~^])\s*$")
# CVE identifier. Scrapes CVE ids out of a manual --security-fix entry so it can
# dedup against an advisory bearing the same CVE (manual wins). The advisory's own
# display id comes from its structured fields via _cve_id(), not this regex.
_CVE_ID_RE = re.compile(r"CVE-\d{4}-\d{4,}", re.IGNORECASE)
# GHSA identifier (GitHub's own advisory id: "GHSA-" + three 4-char groups). An
# advisory without a CVE yet is rendered as "(GHSA-...) ...", so a manual
# --security-fix that names the same GHSA must dedup on it too (CVE alone would
# miss it). Matched on the structural "GHSA-####-####-####" shape rather than the
# exact base32 alphabet: the prefix + grouping is already specific enough never to
# collide with prose, and staying charset-agnostic avoids dropping a real id if
# GitHub's alphabet ever shifts.
_GHSA_ID_RE = re.compile(r"GHSA(?:-[0-9A-Za-z]{4}){3}", re.IGNORECASE)


@dataclass(frozen=True)
class AdvisoryFix:
    """One published advisory that this cut's version fixes. Entirely factual."""

    display_id: str        # the id shown in parens: CVE if present, else GHSA
    cve_id: str            # "" when the advisory has no CVE assigned yet
    ghsa_id: str
    summary: str           # advisory summary, rendered verbatim as the note text
    html_url: str


@dataclass(frozen=True)
class AdvisorySelection:
    """Outcome of scanning published advisories for the version being cut.

    ``matched`` carries the selected fixes; the caller renders them via
    :func:`merge_with_manual` (the single render path, so nothing here can drift
    from what ships). The counts and ``unmatched_ids`` feed the PR-body disclaimer
    so a maintainer can see what was and was not auto-included.
    """

    matched: tuple[AdvisoryFix, ...] = ()
    considered: int = 0                  # published, non-withdrawn advisories examined
    unmatched_ids: tuple[str, ...] = ()  # published advisories read cleanly that did not match this version
    unreadable_ids: tuple[str, ...] = ()  # published advisories whose patched versions could not be read (MIGHT match)
    fetch_failed: bool = False           # True if the advisory API call failed (e.g. no permission)
    fetch_error: str = ""


def _string_attr(advisory: Any, name: str) -> str:
    """Return a string advisory attribute, tolerating None / a broken property.

    Some advisory fields (e.g. ``cve_id``) are legitimately ``None``; others can
    raise if PyGithub mis-parses the payload. Either way we want a plain string,
    never an exception aborting the whole cut, so fall back to ``""``.
    """
    try:
        value = getattr(advisory, name)
    except Exception:  # noqa: BLE001 - a mis-parsed attribute must not abort the cut
        return ""
    return value if isinstance(value, str) else ""


def _cve_id(advisory: Any) -> str:
    """Return the advisory's CVE id, from ``cve_id`` or the ``identifiers`` list.

    ``cve_id`` is often ``None`` on a freshly published advisory whose CVE is
    still propagating, but the structured ``identifiers`` list carries it, so
    consult that as a fallback before giving up.
    """
    direct = _string_attr(advisory, "cve_id")
    if direct:
        return direct
    try:
        identifiers = advisory.identifiers
    except Exception:  # noqa: BLE001
        identifiers = None
    # A mis-parsed payload can hand back a non-list here; iterating it would raise
    # and abort the cut, so treat anything but a list as "no identifiers".
    if not isinstance(identifiers, list):
        return ""
    for ident in identifiers:
        if isinstance(ident, dict) and ident.get("type") == "CVE" and ident.get("value"):
            return str(ident["value"])
    return ""


def _fixed_version_tokens(text: str) -> set[str]:
    """Extract discrete fixed-version tokens from *text*, dropping range bounds.

    A token immediately preceded by a comparison operator ("< 9.1.0", ">= 9.0.0")
    is a range bound, not a version that shipped the fix, so it is discarded (see
    :data:`_RANGE_BOUND_RE`). A bare or comma-listed version ("9.0.5",
    "8.0.4, 9.0.5") is kept. The operator is checked against the text right before
    each match rather than a whole-string "contains an operator" test, so a comma
    list mixing a bound and a discrete version keeps only the discrete one.
    """
    tokens: set[str] = set()
    for m in _VERSION_TOKEN_RE.finditer(text):
        if _RANGE_BOUND_RE.search(text[: m.start()]):
            continue  # governed by a comparison operator -> a range bound, not a fix
        tokens.add(m.group(1))
    return tokens


def patched_version_tokens(raw_vulnerabilities: Sequence[Any]) -> set[str]:
    """Collect every discrete fixed M.m.p token from an advisory's raw vulnerabilities.

    Reads both ``patched_versions`` (a plain string) and
    ``first_patched_version`` (an object ``{"identifier": ...}`` in the REST
    payload, read here from the raw dict, never the broken property). Operates
    on the raw JSON dicts (``advisory.raw_data["vulnerabilities"]``) so it is pure
    and unit-testable without a live advisory object. Range bounds ("< 9.1.0") are
    dropped (see :func:`_fixed_version_tokens`), so a field that (wrongly) holds a
    range never marks a boundary version as fixed.
    """
    tokens: set[str] = set()
    # A mis-parsed payload can put a non-list here; iterating a scalar would raise
    # and abort the cut, so ignore anything that is not a list.
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
    """Return the advisory summary as a single line, for the note text.

    ``summary`` is already a one-line field. When it is empty, falls back to the
    first non-blank line of the (often multi-paragraph) ``description`` rather
    than the whole thing, so the fallback bullet stays a sentence, not the entire
    write-up. Then to a placeholder, so a bullet is never emitted empty. Any
    embedded newline is dropped either way: it would otherwise split the bullet
    or inject a stray heading into the changelog (the hazard :mod:`render` guards
    against).
    """
    summary = _string_attr(advisory, "summary")
    if summary:
        return " ".join(summary.splitlines()).strip() or "(no summary provided)"
    description = _string_attr(advisory, "description")
    for line in description.splitlines():
        if line.strip():
            return line.strip()
    return "(no summary provided)"


# Sentinel distinguishing "read the advisory, it does not fix this version" (None)
# from "could not read the advisory's patched versions at all" (_UNREADABLE). The
# two must not collapse: a real non-match is safely reported as "did not match
# this version", but an unread advisory MIGHT fix this version and is surfaced as
# a separate "could not read" warning so a maintainer checks it by hand.
_UNREADABLE = object()


def _extract_fix(advisory: Any, version: str) -> "Optional[AdvisoryFix] | object":
    """Return an :class:`AdvisoryFix` if *advisory* is fixed by *version*.

    Returns ``None`` when the advisory was read but *version* is not among its
    patched-version tokens (exact membership, never a range comparison), or when it
    carries no CVE/GHSA id to put in the parens. Returns :data:`_UNREADABLE` when
    the advisory's ``raw_data`` could not be read at all, a case the caller must
    not treat as a non-match, since an unread advisory might still fix this version.
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

    No leading ``* `` is added here: valkey's ``emit_category`` prepends the
    marker. This matches the hand-written form maintainers use, e.g.
    ``(CVE-2026-23479) Use-After-Free in unblock client flow``.

    The summary is rendered verbatim; if it happens to end in a ``(#N)`` we do not
    strip it (it is the advisory author's text). That is safe because the promoted
    Security Fixes section is excluded from the credited-PR dedup
    (:func:`release_cut._credited_pr_numbers`), so a CVE summary's incidental
    ``(#N)`` is never mistaken for a PR credit.
    """
    return f"({fix.display_id}) {fix.summary}"


def collect_advisory_fixes(repo: Any, version: str) -> AdvisorySelection:
    """Fetch published advisories and select those fixed by *version*.

    Never raises: an advisory API failure (most often the token lacking advisory
    read permission) is captured in ``fetch_failed``/``fetch_error`` and returned
    as an empty selection, so a permission gap degrades to "no fixes found, add
    them by hand" rather than aborting the cut. Only ``published`` (non-withdrawn)
    advisories are considered; drafts and embargoed advisories are invisible to
    the token and are exactly what the caller's disclaimer asks a human to add.
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
            # Could not read this advisory's patched versions: it MIGHT fix this
            # version, so it must not be reported as a clean non-match. Bucket it
            # separately (id if we have one, else a placeholder) for its own warning.
            unreadable_ids.append(_cve_id(advisory) or _string_attr(advisory, "ghsa_id") or "(unknown advisory)")
        else:
            ident = _cve_id(advisory) or _string_attr(advisory, "ghsa_id")
            if ident:
                unmatched_ids.append(ident)

    # Deterministic order (by display id) and dedup: two advisories should not
    # share a CVE, but a re-published/duplicated GHSA could, and a stable order
    # keeps the rendered notes reproducible across re-cuts.
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
    """Merge advisory-derived fixes with hand-supplied ``--security-fix`` entries.

    Manual entries win on CVE collision: if a maintainer typed a bullet naming a
    CVE that an advisory also fixes, the advisory copy is dropped so the change is
    listed once, with the human's wording. Manual entries come first (a maintainer
    who bothered to type one likely wants it prominent), then the remaining
    advisory bullets. Returns ``None`` when nothing remains, so the caller omits
    the Security Fixes header entirely.

    Collision is matched on each advisory's own structured ``cve_id`` and
    ``ghsa_id``, never on ids scraped from the rendered bullet summary. An
    advisory summary routinely cites a related-but-distinct id in prose ("same
    root cause as CVE-..."), so scanning the summary would silently drop that
    advisory whenever a manual fix happened to name the cited id, dropping a real
    published fix. Matching a GHSA (not just a CVE) is what lets a GHSA-only
    advisory (one with no CVE assigned yet, rendered as ``(GHSA-...) ...``) dedup
    against a maintainer who hand-wrote the same fix by its GHSA id.
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
