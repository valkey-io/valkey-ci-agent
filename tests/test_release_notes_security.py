"""Tests for auto-populating Security Fixes from GitHub repository advisories.

The advisory objects are faked to mirror PyGithub 2.9.1's shape, including the
two quirks the module works around: ``get_repository_advisories()`` takes no
state filter (so we fetch all and filter published ourselves), and
``AdvisoryVulnerability.first_patched_version`` is exposed only via ``raw_data``
because the typed property mis-parses the ``{"identifier": ...}`` payload.
"""

from __future__ import annotations

import pytest

from scripts.release_notes import security as sec


class _FakeAdvisory:
    """Minimal stand-in for github.RepositoryAdvisory.RepositoryAdvisory.

    Mirrors only the attributes the module reads. ``raw_data`` carries the
    ``vulnerabilities`` dicts (the real object exposes patched versions reliably
    only there), while the scalar fields are plain attributes as PyGithub exposes
    them. ``raise_on`` lets a test force an attribute to raise, matching the
    ``BadAttributeException`` PyGithub throws on a mis-parsed field.
    """

    def __init__(
        self, *, state="published", cve_id=None, ghsa_id="", summary="",
        description="", html_url="", withdrawn_at=None, identifiers=None,
        vulnerabilities=None, raise_on=(), overrides=None,
    ):
        self._state = state
        self._cve_id = cve_id
        self._ghsa_id = ghsa_id
        self._summary = summary
        self._description = description
        self._html_url = html_url
        self._withdrawn_at = withdrawn_at
        self._identifiers = identifiers or []
        self._raw = {"vulnerabilities": vulnerabilities or []}
        self._raise_on = set(raise_on)
        # Force an attribute to return an arbitrary, wrong-typed value (e.g.
        # ghsa_id -> 123, raw_data -> a list). The real GitHub payload is not
        # guaranteed to match the typed shape, so this exercises the code's
        # malformed-but-NON-raising defenses, which raise_on cannot reach.
        self._overrides = dict(overrides or {})

    def _get(self, name, value):
        if name in self._raise_on:
            raise ValueError(f"BadAttribute: {name}")
        if name in self._overrides:
            return self._overrides[name]
        return value

    @property
    def state(self):
        return self._get("state", self._state)

    @property
    def cve_id(self):
        return self._get("cve_id", self._cve_id)

    @property
    def ghsa_id(self):
        return self._get("ghsa_id", self._ghsa_id)

    @property
    def summary(self):
        return self._get("summary", self._summary)

    @property
    def description(self):
        return self._get("description", self._description)

    @property
    def html_url(self):
        return self._get("html_url", self._html_url)

    @property
    def withdrawn_at(self):
        return self._get("withdrawn_at", self._withdrawn_at)

    @property
    def identifiers(self):
        return self._get("identifiers", self._identifiers)

    @property
    def raw_data(self):
        return self._get("raw_data", self._raw)


def _vuln(patched=None, first_patched=None):
    """Build a raw vulnerability dict as the REST payload shapes it."""
    v = {"package": {"ecosystem": "other", "name": "valkey"}}
    if patched is not None:
        v["patched_versions"] = patched
    if first_patched is not None:
        # The REST API returns an object, which is exactly what breaks the typed
        # property; the module must read the identifier out of the dict.
        v["first_patched_version"] = {"identifier": first_patched}
    return v


def _advisory(**kw):
    return _FakeAdvisory(**kw)


class _FakeRepo:
    def __init__(self, advisories, *, raises=None):
        self._advisories = advisories
        self._raises = raises

    def get_repository_advisories(self):
        if self._raises is not None:
            raise self._raises
        return list(self._advisories)


class TestPatchedVersionTokens:
    def test_reads_patched_versions_string(self):
        assert sec.patched_version_tokens([_vuln(patched="9.1.0")]) == {"9.1.0"}

    def test_reads_first_patched_version_object(self):
        # This is the shape that breaks the PyGithub property; via raw dict it works.
        assert sec.patched_version_tokens([_vuln(first_patched="9.1.0")]) == {"9.1.0"}

    def test_reads_comma_list_of_fixed_versions(self):
        # A comma list names discrete backport targets that each shipped the fix.
        toks = sec.patched_version_tokens([_vuln(patched="8.0.4, 9.0.5")])
        assert toks == {"8.0.4", "9.0.5"}

    def test_range_bounds_are_not_treated_as_fixed_versions(self):
        # patched_versions is contractually a discrete fixed version / list, never a
        # range (that lives in vulnerable_version_range, which we never read). But if
        # a range leaks in, its bounds must NOT be read as fixed versions: with exact
        # membership, "< 9.1.0" would falsely mark 9.1.0 (the first UNaffected release
        # under an exclusive bound) as a version that shipped the fix, and ">= 9.0.0"
        # would mark the vulnerable floor. Both bounds are dropped.
        assert sec.patched_version_tokens([_vuln(patched=">= 9.0.0, < 9.1.0")]) == set()
        assert sec.patched_version_tokens([_vuln(patched="< 9.1.0")]) == set()
        assert sec.patched_version_tokens([_vuln(patched="<= 9.1.0")]) == set()
        assert sec.patched_version_tokens([_vuln(patched=">= 9.0.0")]) == set()

    def test_range_bound_dropped_but_discrete_sibling_kept(self):
        # In a mixed list, only the operator-governed token is a bound; a bare
        # sibling entry is a real fixed version and must survive.
        assert sec.patched_version_tokens([_vuln(patched="< 9.1.0, 9.0.5")]) == {"9.0.5"}

    def test_ignores_non_dict_and_missing(self):
        assert sec.patched_version_tokens([None, {}, "x", _vuln()]) == set()

    def test_multiple_vulns_union(self):
        toks = sec.patched_version_tokens([_vuln(patched="9.1.0"), _vuln(first_patched="8.0.4")])
        assert toks == {"9.1.0", "8.0.4"}

    def test_four_component_version_not_truncated(self):
        # Regression: "." is a non-word char, so a \b-anchored regex would extract
        # a bogus 3-component "9.1.0" from a 4-component "9.1.0.5", matching a cut
        # (9.1.0) that never shipped this fix. The lookarounds reject an adjacent
        # digit/dot, so a 4-component version yields no 3-component token.
        assert sec.patched_version_tokens([_vuln(patched="9.1.0.5")]) == set()
        assert sec.patched_version_tokens([_vuln(patched="1.9.1.0")]) == set()

    def test_leading_digit_does_not_false_match_shorter_version(self):
        # "19.1.0" is its own token, NOT "9.1.0"; the lookbehind rejects the
        # adjacent leading digit. So a cut of 9.1.0 must not be considered fixed by
        # an advisory that patched 19.1.0.
        assert sec.patched_version_tokens([_vuln(patched="19.1.0")]) == {"19.1.0"}
        assert "9.1.0" not in sec.patched_version_tokens([_vuln(patched="19.1.0")])

    def test_trailing_digit_does_not_false_match_shorter_version(self):
        # "9.1.00" is its own token, NOT "9.1.0"; the lookahead rejects the
        # adjacent trailing digit. A cut of 9.1.0 is not fixed by a 9.1.00 patch.
        assert sec.patched_version_tokens([_vuln(patched="9.1.00")]) == {"9.1.00"}
        assert "9.1.0" not in sec.patched_version_tokens([_vuln(patched="9.1.00")])

    def test_valid_token_with_suffix_still_matches(self):
        # A trailing "-rc1" (non-digit, non-dot after the token) must still match.
        assert sec.patched_version_tokens([_vuln(patched="9.1.0-rc1")]) == {"9.1.0"}


class TestRenderBullet:
    def test_matches_hand_written_form(self):
        fix = sec.AdvisoryFix(
            display_id="CVE-2026-23479", cve_id="CVE-2026-23479", ghsa_id="GHSA-x",
            summary="Use-After-Free in unblock client flow", html_url="",
        )
        assert sec.render_bullet(fix) == "(CVE-2026-23479) Use-After-Free in unblock client flow"

    def test_no_leading_marker_or_pr_ref(self):
        fix = sec.AdvisoryFix(
            display_id="GHSA-y", cve_id="", ghsa_id="GHSA-y", summary="x", html_url="",
        )
        rendered = sec.render_bullet(fix)
        assert not rendered.startswith("*")
        assert "(#" not in rendered


class TestCollectAdvisoryFixes:
    def test_matches_version_and_renders(self):
        repo = _FakeRepo([
            _advisory(cve_id="CVE-2026-23479", ghsa_id="GHSA-a",
                      summary="Use-After-Free in unblock client flow",
                      vulnerabilities=[_vuln(patched="9.1.0")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [sec.render_bullet(f) for f in sel.matched] == [
            "(CVE-2026-23479) Use-After-Free in unblock client flow"
        ]
        assert sel.considered == 1
        assert [f.display_id for f in sel.matched] == ["CVE-2026-23479"]
        assert sel.unmatched_ids == ()

    def test_non_matching_version_goes_to_unmatched(self):
        repo = _FakeRepo([
            _advisory(cve_id="CVE-2026-1", ghsa_id="GHSA-a", summary="s",
                      vulnerabilities=[_vuln(patched="8.0.4")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert sel.matched == ()
        assert sel.considered == 1
        assert sel.unmatched_ids == ("CVE-2026-1",)

    def test_skips_draft_and_withdrawn(self):
        repo = _FakeRepo([
            _advisory(state="draft", cve_id="CVE-2026-1", ghsa_id="GHSA-a", summary="s",
                      vulnerabilities=[_vuln(patched="9.1.0")]),
            _advisory(state="published", cve_id="CVE-2026-2", ghsa_id="GHSA-b", summary="s2",
                      withdrawn_at="2026-01-01T00:00:00Z",
                      vulnerabilities=[_vuln(patched="9.1.0")]),
            _advisory(state="published", cve_id="CVE-2026-3", ghsa_id="GHSA-c", summary="ok",
                      vulnerabilities=[_vuln(patched="9.1.0")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        # Only the published, non-withdrawn one is considered and matched.
        assert sel.considered == 1
        assert [f.display_id for f in sel.matched] == ["CVE-2026-3"]

    def test_falls_back_to_ghsa_when_no_cve(self):
        repo = _FakeRepo([
            _advisory(cve_id=None, ghsa_id="GHSA-zzzz", summary="pending CVE",
                      identifiers=[{"type": "GHSA", "value": "GHSA-zzzz"}],
                      vulnerabilities=[_vuln(patched="9.1.0")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [sec.render_bullet(f) for f in sel.matched] == ["(GHSA-zzzz) pending CVE"]

    def test_extracts_cve_from_identifiers_when_field_null(self):
        repo = _FakeRepo([
            _advisory(cve_id=None, ghsa_id="GHSA-a", summary="s",
                      identifiers=[{"type": "CVE", "value": "CVE-2026-999"},
                                   {"type": "GHSA", "value": "GHSA-a"}],
                      vulnerabilities=[_vuln(patched="9.1.0")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [sec.render_bullet(f) for f in sel.matched] == ["(CVE-2026-999) s"]

    def test_fetch_failure_degrades(self):
        repo = _FakeRepo([], raises=RuntimeError("no advisory permission"))
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert sel.fetch_failed is True
        assert "permission" in sel.fetch_error
        assert sel.matched == ()

    def test_one_bad_advisory_does_not_abort(self):
        repo = _FakeRepo([
            _advisory(cve_id="CVE-2026-1", ghsa_id="GHSA-a", summary="s",
                      raise_on={"raw_data"}, vulnerabilities=[_vuln(patched="9.1.0")]),
            _advisory(cve_id="CVE-2026-2", ghsa_id="GHSA-b", summary="good",
                      vulnerabilities=[_vuln(patched="9.1.0")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [f.display_id for f in sel.matched] == ["CVE-2026-2"]

    def test_unreadable_advisory_is_not_reported_as_non_match(self):
        # An advisory whose raw_data can't be read MIGHT fix this version, so it must
        # be bucketed as unreadable (a "check by hand" warning), NOT as unmatched
        # ("did not match this version"); the latter falsely assures the maintainer
        # it is irrelevant.
        repo = _FakeRepo([
            _advisory(cve_id="CVE-2026-9", ghsa_id="GHSA-z", summary="s",
                      raise_on={"raw_data"}, vulnerabilities=[_vuln(patched="9.1.0")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert sel.matched == ()
        assert sel.unmatched_ids == ()
        assert sel.unreadable_ids == ("CVE-2026-9",)
        assert sel.considered == 1

    def test_unreadable_advisory_without_id_uses_placeholder(self):
        # No CVE/GHSA to name it, but it still must not vanish: a placeholder keeps
        # the "one advisory could not be read" signal in unreadable_ids.
        repo = _FakeRepo([
            _advisory(cve_id=None, ghsa_id="", summary="s", raise_on={"raw_data"},
                      vulnerabilities=[_vuln(patched="9.1.0")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert sel.unreadable_ids == ("(unknown advisory)",)
        assert sel.unmatched_ids == ()

    # The GitHub payload is not guaranteed to match the typed shape, so the code
    # tolerates malformed-but-non-raising values too (a field returning the wrong
    # type). raise_on only covers attributes that *raise*; these cover the rest.

    def test_raw_data_not_a_dict_is_tolerated(self):
        # raw_data returning a list (not a dict) yields no version tokens: the
        # advisory simply does not match, rather than crashing on raw.get(...).
        repo = _FakeRepo([_advisory(cve_id="CVE-2026-1", overrides={"raw_data": ["oops"]})])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert sel.matched == ()
        assert sel.unmatched_ids == ("CVE-2026-1",)

    def test_vulnerabilities_not_a_list_is_tolerated(self):
        # vulnerabilities as a dict (not a list) contributes no tokens; no crash.
        repo = _FakeRepo([_advisory(
            cve_id="CVE-2026-2",
            overrides={"raw_data": {"vulnerabilities": {"patched_versions": "9.1.0"}}},
        )])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert sel.matched == ()

    def test_non_dict_vuln_entry_skipped_good_one_kept(self):
        # A junk (non-dict) entry alongside a real vuln: the real one still matches.
        repo = _FakeRepo([_advisory(
            cve_id="CVE-2026-3",
            overrides={"raw_data": {"vulnerabilities": ["junk", _vuln(patched="9.1.0")]}},
        )])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [f.display_id for f in sel.matched] == ["CVE-2026-3"]

    def test_vulnerabilities_scalar_is_tolerated(self):
        # A truthy non-list (an int) must not be iterated: `for vuln in 5` would
        # raise TypeError and abort the whole cut. It contributes no tokens.
        repo = _FakeRepo([_advisory(
            cve_id="CVE-2026-11",
            overrides={"raw_data": {"vulnerabilities": 5}},
        )])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert sel.matched == ()
        assert sel.unmatched_ids == ("CVE-2026-11",)

    def test_identifiers_scalar_does_not_abort(self):
        # identifiers as a truthy scalar (int) must not be iterated; _cve_id finds
        # nothing there and falls back to the GHSA, no TypeError aborting the cut.
        repo = _FakeRepo([_advisory(
            cve_id=None, ghsa_id="GHSA-b", overrides={"identifiers": 42},
            vulnerabilities=[_vuln(patched="9.1.0")],
        )])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [f.display_id for f in sel.matched] == ["GHSA-b"]

    def test_non_string_patched_version_skipped(self):
        # patched_versions returning a number (not a string) is skipped, not matched.
        repo = _FakeRepo([_advisory(
            cve_id="CVE-2026-7",
            overrides={"raw_data": {"vulnerabilities": [{"patched_versions": 910}]}},
        )])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert sel.matched == ()
        assert sel.unmatched_ids == ("CVE-2026-7",)

    def test_non_string_ghsa_id_with_no_cve_is_skipped(self):
        # A version-matched advisory whose only id field is wrong-typed (ghsa_id
        # is an int) has no usable display id, so it is skipped: not rendered as
        # "(123) ..." and not crashing.
        repo = _FakeRepo([_advisory(
            cve_id=None, ghsa_id=None, overrides={"ghsa_id": 123},
            vulnerabilities=[_vuln(patched="9.1.0")],
        )])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert sel.matched == ()

    def test_identifiers_not_a_list_does_not_char_iterate(self):
        # identifiers as a bare string must not be iterated per character into
        # bogus CVE dicts; _cve_id finds nothing there and falls back to the GHSA.
        repo = _FakeRepo([_advisory(
            cve_id=None, ghsa_id="GHSA-a", overrides={"identifiers": "CVE-2026-9"},
            vulnerabilities=[_vuln(patched="9.1.0")],
        )])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [f.display_id for f in sel.matched] == ["GHSA-a"]

    def test_non_dict_identifier_entries_tolerated(self):
        # identifiers entries that are not dicts are skipped when scanning for a CVE.
        repo = _FakeRepo([_advisory(
            cve_id=None, ghsa_id="GHSA-a", overrides={"identifiers": ["x", 5, None]},
            vulnerabilities=[_vuln(patched="9.1.0")],
        )])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [f.display_id for f in sel.matched] == ["GHSA-a"]

    def test_deterministic_order_and_dedup(self):
        repo = _FakeRepo([
            _advisory(cve_id="CVE-2026-9", ghsa_id="GHSA-b", summary="nine",
                      vulnerabilities=[_vuln(patched="9.1.0")]),
            _advisory(cve_id="CVE-2026-1", ghsa_id="GHSA-a", summary="one",
                      vulnerabilities=[_vuln(patched="9.1.0")]),
            # duplicate display id -> deduped
            _advisory(cve_id="CVE-2026-1", ghsa_id="GHSA-a2", summary="one-dup",
                      vulnerabilities=[_vuln(patched="9.1.0")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [f.display_id for f in sel.matched] == ["CVE-2026-1", "CVE-2026-9"]

    def test_summary_collapses_newlines(self):
        repo = _FakeRepo([
            _advisory(cve_id="CVE-2026-1", ghsa_id="GHSA-a", summary="line one\nline two",
                      vulnerabilities=[_vuln(patched="9.1.0")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [sec.render_bullet(f) for f in sel.matched] == ["(CVE-2026-1) line one line two"]

    def test_falls_back_to_description_when_no_summary(self):
        repo = _FakeRepo([
            _advisory(cve_id="CVE-2026-1", ghsa_id="GHSA-a", summary="",
                      description="from the description",
                      vulnerabilities=[_vuln(patched="9.1.0")]),
        ])
        sel = sec.collect_advisory_fixes(repo, "9.1.0")
        assert [sec.render_bullet(f) for f in sel.matched] == ["(CVE-2026-1) from the description"]


def _fix(display_id, *, cve_id=None, ghsa_id="GHSA-x", summary="s"):
    """Build an AdvisoryFix for merge_with_manual tests.

    ``cve_id`` defaults to ``display_id`` when it looks like a CVE, else "".
    ``ghsa_id`` defaults to a placeholder that is not a real GHSA shape (so it
    never accidentally dedups); a GHSA-collision test passes a real-shaped id.
    """
    if cve_id is None:
        cve_id = display_id if display_id.upper().startswith("CVE-") else ""
    return sec.AdvisoryFix(
        display_id=display_id, cve_id=cve_id, ghsa_id=ghsa_id, summary=summary, html_url="",
    )


class TestMergeWithManual:
    def test_manual_first_then_advisory(self):
        merged = sec.merge_with_manual(
            [_fix("CVE-2026-0002", summary="auto")], ["Hand-written fix (#7)"],
        )
        assert merged == ["Hand-written fix (#7)", "(CVE-2026-0002) auto"]

    def test_manual_wins_on_cve_collision(self):
        merged = sec.merge_with_manual(
            [_fix("CVE-2026-0002", summary="auto version")],
            ["CVE-2026-0002 hand-written wording"],
        )
        # The advisory copy is dropped; only the manual entry survives.
        assert merged == ["CVE-2026-0002 hand-written wording"]

    def test_collision_is_case_insensitive(self):
        merged = sec.merge_with_manual(
            [_fix("CVE-2026-0002", summary="auto")],
            ["fixes cve-2026-0002 lowercased"],
        )
        assert merged == ["fixes cve-2026-0002 lowercased"]

    def test_collision_matches_own_cve_not_summary_prose(self):
        # Regression: a GHSA-only advisory whose SUMMARY cites an unrelated CVE
        # must NOT be dropped just because a manual --security-fix names that CVE.
        # Dedup keys on the advisory's own cve_id (empty here), never summary text.
        merged = sec.merge_with_manual(
            [_fix("GHSA-abcd", summary="DoS, same root cause as CVE-2026-0002 Lua fix")],
            ["CVE-2026-0002: the actual, separate Lua RCE"],
        )
        assert merged == [
            "CVE-2026-0002: the actual, separate Lua RCE",
            "(GHSA-abcd) DoS, same root cause as CVE-2026-0002 Lua fix",
        ]

    def test_cve_advisory_kept_when_summary_cites_a_different_cve(self):
        # An advisory with its OWN cve_id whose summary also mentions a second,
        # unrelated CVE the manual fix names: still kept (its own id didn't collide).
        merged = sec.merge_with_manual(
            [_fix("CVE-2026-0009", summary="unrelated to CVE-2026-0002")],
            ["CVE-2026-0002 hand"],
        )
        assert merged == ["CVE-2026-0002 hand", "(CVE-2026-0009) unrelated to CVE-2026-0002"]

    def test_manual_wins_on_ghsa_collision_when_advisory_has_no_cve(self):
        # A GHSA-only advisory (no CVE yet) that a maintainer also hand-wrote,
        # naming its GHSA id, must dedup on the GHSA; CVE-only matching missed it
        # and shipped the fix twice.
        merged = sec.merge_with_manual(
            [_fix("GHSA-abcd-1234-wxyz", ghsa_id="GHSA-abcd-1234-wxyz", summary="auto wording")],
            ["(GHSA-abcd-1234-wxyz) hand-written wording"],
        )
        assert merged == ["(GHSA-abcd-1234-wxyz) hand-written wording"]

    def test_ghsa_collision_is_case_insensitive(self):
        merged = sec.merge_with_manual(
            [_fix("GHSA-abcd-1234-wxyz", ghsa_id="GHSA-abcd-1234-wxyz", summary="auto")],
            ["fixes ghsa-ABCD-1234-wxyz lowercased-prefix"],
        )
        assert merged == ["fixes ghsa-ABCD-1234-wxyz lowercased-prefix"]

    def test_ghsa_advisory_kept_when_manual_names_a_different_ghsa(self):
        # No collision: the manual entry names a different GHSA, so the advisory
        # is still rendered.
        merged = sec.merge_with_manual(
            [_fix("GHSA-aaaa-1111-bbbb", ghsa_id="GHSA-aaaa-1111-bbbb", summary="auto")],
            ["(GHSA-cccc-2222-dddd) an unrelated fix"],
        )
        assert merged == [
            "(GHSA-cccc-2222-dddd) an unrelated fix",
            "(GHSA-aaaa-1111-bbbb) auto",
        ]

    def test_ghsa_collision_matches_own_id_not_summary_prose(self):
        # Mirror of the CVE prose-guard: an advisory whose SUMMARY cites an
        # unrelated GHSA must not be dropped when a manual fix names that cited id.
        merged = sec.merge_with_manual(
            [_fix("GHSA-1111-2222-3333", ghsa_id="GHSA-1111-2222-3333",
                  summary="same root cause as GHSA-9999-8888-7777")],
            ["(GHSA-9999-8888-7777) a different real fix"],
        )
        assert merged == [
            "(GHSA-9999-8888-7777) a different real fix",
            "(GHSA-1111-2222-3333) same root cause as GHSA-9999-8888-7777",
        ]

    def test_none_when_empty(self):
        assert sec.merge_with_manual([], None) is None
        assert sec.merge_with_manual([], []) is None

    def test_advisory_only(self):
        assert sec.merge_with_manual([_fix("CVE-2026-0002", summary="auto")], None) == [
            "(CVE-2026-0002) auto"
        ]

    def test_manual_only_passthrough(self):
        assert sec.merge_with_manual([], ["a", "b"]) == ["a", "b"]
