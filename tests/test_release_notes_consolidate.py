"""Final releases combine RC history without losing source entries or credits."""

from __future__ import annotations

import json

import pytest

from scripts.release_notes import consolidate
from scripts.release_notes import release_format as rn
from scripts.release_notes.projects import VALKEY_PROFILE, profile_for


def _section(stage, notes, *, version="9.2.0", security=None, profile=VALKEY_PROFILE):
    return rn.render_version_section(
        version, stage, "LOW", "2026-11-18", notes, security,
        display_name=profile.display_name, categories=profile.categories,
    )


def _notes(*, ga=None, rc1=None, rc2=None, security=None, profile=VALKEY_PROFILE):
    prior = _section("rc2", rc2 or {}, security=security, profile=profile)
    prior += "\n" + _section("rc1", rc1 or {}, profile=profile)
    prior += "\n### Contributors\n* Amy @amy\n* Bob @bob\n"
    return rn.render_release_notes(
        ga or {}, version="9.2.0", stage="ga", urgency="LOW", date="2026-11-18",
        prior_text=prior, display_name=profile.display_name, categories=profile.categories,
    )


def _run(notes, run_fn, *, version="9.2.0", stage="ga", profile=VALKEY_PROFILE):
    return consolidate.consolidate_release_notes(
        notes, version=version, stage=stage, urgency="LOW", date="2026-11-18",
        profile=profile, repo_dir="/clone", run_fn=run_fn,
    )


def test_combines_duplicates_and_superseded_changes_with_all_credits():
    notes = _notes(
        rc1={"New Features and Enhanced Behavior": [
            "Add experimental `HOTKEYS` INFO fields by @amy (#1)",
            "Add command X by @bob (#2)",
        ]},
        rc2={"New Features and Enhanced Behavior": ["Add command X by @bob (#2)"]},
        ga={"Behavior Changes": ["Remove the RC-only `HOTKEYS` INFO fields by @cara (#3)"]},
    )
    prompts = []

    def run_fn(prompt, **kwargs):
        prompts.append(prompt)
        assert kwargs["allowed_tools"] == ""
        assert kwargs["cwd"] == "/clone"
        return json.dumps({"bullets": [
            {"sources": [0, 3], "category": "Behavior Changes",
             "text": "Remove the experimental `HOTKEYS` INFO fields that only shipped in RCs"},
            {"sources": [1, 2], "category": "New Features and Enhanced Behavior",
             "text": "Add command X"},
        ]}), "", 0

    result = _run(notes, run_fn)
    assert result is not None
    assert len(prompts) == 1
    assert prompts[0].index('"stage": "rc1"') < prompts[0].index('"stage": "rc2"')
    assert prompts[0].index('"stage": "rc2"') < prompts[0].index('"stage": "ga"')
    assert result.rc_stages == ("rc1", "rc2")
    assert result.source_count == 4
    assert "by @amy, @cara (#1, #3)" in result.notes_text
    assert result.notes_text.count("Add command X by @bob (#2)") == 1
    assert "Valkey 9.2.0  -  Released Wed 18 November 2026" in result.notes_text
    assert "Valkey 9.2.0-rc" not in result.notes_text
    assert "### New Features" in result.notes_text
    assert result.notes_text.index("### New Features") < result.notes_text.index("### Behavior Changes")
    assert "all release candidates" in result.notes_text
    assert "diff the `00-RELEASENOTES` files" in result.notes_text
    assert rn.credited_pr_numbers(result.notes_text) == {1, 2, 3}
    assert result.notes_text.endswith("### Contributors\n* Amy @amy\n* Bob @bob\n")


def test_preserves_other_versions_security_and_uncredited_historical_bullets():
    older = _section("ga", {"Bug Fixes": ["Old fix (#90)"]}, version="9.1.0")
    other_rc = _section("rc1", {"Bug Fixes": ["Other version (#91)"]}, version="9.1.1")
    prior = (
        "Valkey 9.2.0 RC1 - Released September 2026\n"
        "------------------------------------------\n\n"
        "Bug fixes\n=========\n"
        "* Fix a historical issue with no PR credit\n"
        "  on 32-bit builds\n\n"
        "### Security Fixes\n* Fix CVE-2026-1234 (#4)\n\n"
        + older + "\n" + other_rc + "\n### Contributors\n* Amy @amy\n"
    )
    notes = rn.render_release_notes(
        {"Bug Fixes": ["New fix by @bob (#5, #6)"]},
        version="9.2.0", stage="ga", urgency="LOW", date="2026-11-18",
        prior_text=prior, display_name="Valkey", categories=VALKEY_PROFILE.categories,
        security_fixes=["Fix CVE-2026-5678 (#7)"],
    )

    def run_fn(prompt, **kwargs):
        assert '"text": "Fix a historical issue with no PR credit on 32-bit builds"' in prompt
        assert "CVE-" not in prompt
        assert "Old fix" not in prompt
        assert "Other version" not in prompt
        assert "Amy @amy" not in prompt
        return json.dumps({"bullets": [
            {"sources": [0], "category": "Bug Fixes",
             "text": "Fix a historical issue with no PR credit on 32-bit builds"},
            {"sources": [1], "category": "Bug Fixes", "text": "New fix"},
        ]}), "", 0

    result = _run(notes, run_fn)
    assert result is not None
    assert older + "\n" + other_rc in result.notes_text
    assert "* Fix CVE-2026-1234 (#4)" in result.notes_text
    assert "* Fix CVE-2026-5678 (#7)" in result.notes_text
    assert "* New fix by @bob (#5, #6)" in result.notes_text
    assert "* Fix a historical issue with no PR credit on 32-bit builds\n" in result.notes_text
    assert result.notes_text.startswith(notes[:notes.index("Valkey 9.2.0  -")])


def test_carries_legacy_per_section_contributors_to_footer():
    notes = _notes(rc1={"Bug Fixes": ["Fix (#1)"]})
    notes = notes.replace(
        "* Fix (#1)", "* Fix (#1)\n\n### Contributors\n* Legacy @legacy",
    )
    result = _run(notes, lambda *a, **k: (
        '{"bullets":[{"sources":[0],"category":"Bug Fixes","text":"Fix"}]}', "", 0,
    ))
    assert result is not None
    assert result.notes_text.count("### Contributors") == 1
    assert "* Amy @amy" in result.notes_text
    assert "* Legacy @legacy" in result.notes_text


@pytest.mark.parametrize("case", ["rc", "patch", "no_rc", "other_version"])
def test_only_consolidates_initial_final_release_with_matching_rcs(case):
    notes = _notes(rc1={"Bug Fixes": ["Fix (#1)"]})
    stage, version = "ga", "9.2.0"
    if case == "rc":
        stage = "rc3"
    elif case == "patch":
        version = "9.2.1"
    elif case == "no_rc":
        notes = _section("ga", {"Bug Fixes": ["Fix (#1)"]})
    else:
        notes = notes.replace("9.2.0-rc", "9.1.0-rc")

    def forbidden(*args, **kwargs):
        pytest.fail("AI must only run for final .0 releases with RC sections")

    assert _run(notes, forbidden, stage=stage, version=version) is None


def test_security_only_rcs_are_combined_without_ai():
    notes = _notes(security=["Fix CVE-2026-1234 (#1)"])

    def forbidden(*args, **kwargs):
        pytest.fail("Security-only notes need no editorial AI call")

    result = _run(notes, forbidden)
    assert result is not None
    assert result.grouped == {}
    assert result.security_fixes == ("Fix CVE-2026-1234 (#1)",)
    assert "* Fix CVE-2026-1234 (#1)" in result.notes_text
    assert "Valkey 9.2.0-rc" not in result.notes_text


def test_final_security_entry_replaces_prior_rc_bug_note_for_same_pr():
    notes = _notes(rc1={"Bug Fixes": ["Fix an overflow by @amy (#1)"]})
    notes = notes.replace(
        "This is the first stable release of Valkey 9.2.",
        "This is the first stable release of Valkey 9.2.\n\n"
        "### Security Fixes\n* Fix CVE-2026-1234 (#1)",
    )
    result = _run(notes, lambda *a, **k: pytest.fail("The security entry covers this PR"))
    assert result is not None
    assert "Fix an overflow" not in result.notes_text
    assert result.notes_text.count("(#1)") == 1
    assert "* Fix CVE-2026-1234 (#1)" in result.notes_text


@pytest.mark.parametrize("profile", [
    profile_for("valkey-io/valkey-search"),
    profile_for("valkey-io/valkey-json"),
    profile_for("valkey-io/valkey-bloom"),
])
def test_module_profile_uses_its_heading_categories_and_notes_file(profile):
    notes = _notes(rc1={profile.categories[0]: ["Add a feature by @amy (#1)"]}, profile=profile)
    result = _run(notes, lambda *a, **k: (
        json.dumps({"bullets": [
            {"sources": [0], "category": profile.categories[0], "text": "Add a feature"},
        ]}), "", 0,
    ), profile=profile)
    assert result is not None
    assert f"{profile.display_name} 9.2.0  -  Released" in result.notes_text
    assert f"`{profile.notes_file}`" in result.notes_text
    assert f"### {profile.categories[0]}" in result.notes_text


@pytest.mark.parametrize("output", [
    "No JSON",
    '{"bullets":{}}',
    '{"bullets":[]}',
    '{"bullets":[false]}',
    '{"bullets":[{"sources":[],"category":"Bug Fixes","text":"Fix"}]}',
    '{"bullets":[{"sources":[false],"category":"Bug Fixes","text":"Fix"}]}',
    '{"bullets":[{"sources":[0.0],"category":"Bug Fixes","text":"Fix"}]}',
    '{"bullets":[{"sources":[1],"category":"Bug Fixes","text":"Fix"}]}',
    '{"bullets":[{"sources":[0,0],"category":"Bug Fixes","text":"Fix"}]}',
    '{"bullets":[{"sources":[0],"category":"Invented","text":"Fix"}]}',
    '{"bullets":[{"sources":[0],"category":"Security Fixes","text":"Fix"}]}',
    '{"bullets":[{"sources":[0],"category":"Bug Fixes","text":"Fix (#99)"}]}',
    '{"bullets":[{"sources":[0],"category":"Bug Fixes","text":"Fix by @invented"}]}',
    '{"bullets":[{"sources":[0],"category":"Bug Fixes","text":"\\n### Fake"}]}',
    '{"bullets":[{"sources":[0],"category":"Bug Fixes","text":"Fix\\nother"}]}',
    '{"bullets":[{"sources":[0],"category":"Bug Fixes","text":"..."}]}',
    '{"bullets":[{"sources":[0],"category":"Bug Fixes","text":""}]}',
    '{"bullets":[],"bullets":[{"sources":[0],"category":"Bug Fixes","text":"Fix"}]}',
])
def test_invalid_or_incomplete_output_aborts(output):
    notes = _notes(rc1={"Bug Fixes": ["Fix (#1)"]})
    attempts = []

    def run_fn(prompt, **kwargs):
        attempts.append(prompt)
        return output, "", 0

    with pytest.raises(ValueError):
        _run(notes, run_fn)
    assert len(attempts) == 2


def test_nonzero_ai_exit_aborts_even_with_complete_json():
    notes = _notes(rc1={"Bug Fixes": ["Fix (#1)"]})
    with pytest.raises(ValueError, match="failed"):
        _run(notes, lambda *a, **k: (
            '{"bullets":[{"sources":[0],"category":"Bug Fixes","text":"Fix"}]}', "timeout", 1,
        ))


@pytest.mark.parametrize("text", [
    "Add `HOTKEYS GET` and `HOTKEYS RESET`",
    "Remove the INFO fields that only shipped in release candidates",
])
def test_source_credit_cannot_replace_rc_only_effect_or_affected_names(text):
    notes = _notes(ga={"Behavior Changes": [
        "Remove the `hotkeys_last_window_samples` INFO field, which only shipped "
        "in the 9.2 release candidates by @amy (#1)",
    ]})
    with pytest.raises(ValueError, match="release-candidate qualifier"):
        _run(notes, lambda *a, **k: (json.dumps({"bullets": [
            {"sources": [0], "category": "Behavior Changes", "text": text},
        ]}), "", 0))


def test_invalid_output_retries_once_with_feedback_and_all_original_entries():
    notes = _notes(ga={"Behavior Changes": [
        "Remove the `hotkeys_last_window_samples` INFO field, which only shipped "
        "in the 9.2 release candidates by @amy (#1)",
    ]})
    prompts = []

    def run_fn(prompt, **kwargs):
        prompts.append(prompt)
        text = "Add hot key detection"
        if len(prompts) == 2:
            assert "previous response failed validation" in prompt
            assert '"id": 0' in prompt
            text = "Remove `hotkeys_last_window_samples`, which only shipped in release candidates"
        return json.dumps({"bullets": [
            {"sources": [0], "category": "Behavior Changes", "text": text},
        ]}), "", 0

    result = _run(notes, run_fn)
    assert result is not None
    assert len(prompts) == 2
    assert "which only shipped in release candidates by @amy (#1)" in result.notes_text


def test_repeated_pr_must_be_merged_into_one_bullet():
    notes = _notes(
        rc1={"Bug Fixes": ["Fix (#1)"]}, rc2={"Bug Fixes": ["Updated fix (#1)"]},
    )
    with pytest.raises(ValueError, match="repeated a PR"):
        _run(notes, lambda *a, **k: (
            json.dumps({"bullets": [
                {"sources": [0], "category": "Bug Fixes", "text": "Fix"},
                {"sources": [1], "category": "Bug Fixes", "text": "Updated fix"},
            ]}), "", 0,
        ))


def test_unrecognized_input_content_aborts_before_ai():
    notes = _notes(rc1={"Bug Fixes": ["Fix (#1)"]})
    notes = notes.replace("* Fix (#1)", "* Fix (#1)\n\nImportant free-form warning")
    with pytest.raises(ValueError, match="Cannot consolidate"):
        _run(notes, lambda *a, **k: pytest.fail("unparsed content must not be discarded"))
