"""Tests for the release-cut entry point (orchestration mocked).

main is now cut-only: it always dispatches to release_cut.cut(). The cut
internals are tested in test_release_notes_release_cut.py; here we cover
argument validation, the baseline-glob/base-ref resolution, and that the parsed
inputs reach cut().
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.release_notes import main as main_mod
from scripts.release_notes.main import main

# Every RELEASE_NOTES_* env var main() reads as an argparse default. The
# validation tests assert that a *missing* CLI flag triggers a usage error, which
# only holds if the corresponding env default is empty. main() reads these at
# call time (not import time), so clearing them here reaches the real defaults.
# An ambient value (CI, a dev shell, another test) would otherwise supply the
# "missing" argument and make a validation test pass for the wrong reason.
_RELEASE_NOTES_ENV = (
    "RELEASE_NOTES_REPO", "RELEASE_NOTES_HEAD_REF", "RELEASE_NOTES_VERSION",
    "RELEASE_NOTES_STAGE", "RELEASE_NOTES_URGENCY", "RELEASE_NOTES_DATE",
    "RELEASE_NOTES_TAG_GLOB", "RELEASE_NOTES_BASE_REF", "RELEASE_NOTES_CONTRIB_BASE",
    "RELEASE_NOTES_SECURITY_FROM_ADVISORIES",
    "RELEASE_NOTES_GITHUB_TOKEN", "TARGET_TOKEN", "GITHUB_TOKEN",
)


@pytest.fixture(autouse=True)
def _clear_release_notes_env(monkeypatch):
    """Give every test a clean env so argparse defaults are the real defaults."""
    for name in _RELEASE_NOTES_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def patched(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "Github", MagicMock())
    monkeypatch.setattr(main_mod, "retry_github_call", lambda op, **k: op())
    auth = MagicMock()
    auth.__enter__.return_value.env.return_value = {"GIT_PASSWORD": "x"}
    monkeypatch.setattr(main_mod, "GitAuth", lambda *a, **k: auth)
    monkeypatch.setattr(main_mod, "github_https_url", lambda name: f"https://github.com/{name}.git")
    monkeypatch.setattr(main_mod, "run_git", lambda *a, **k: MagicMock())
    monkeypatch.setattr(main_mod.tempfile, "mkdtemp", lambda *a, **k: str(tmp_path / "clone"))
    monkeypatch.setattr(main_mod.shutil, "rmtree", lambda *a, **k: None)
    return monkeypatch


def _capture_cut(patched):
    captured = {}

    def _cut(repo, **kwargs):
        captured.update(kwargs)
        return 0

    patched.setattr(main_mod.cut_mod, "cut", _cut)
    return captured


# --- argument validation ---

def test_missing_token_is_usage_error(monkeypatch):
    monkeypatch.delenv("RELEASE_NOTES_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("TARGET_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        main(["--head-ref", "unstable", "--version", "9.1.0", "--stage", "rc1", "--urgency", "LOW"])
    assert exc.value.code == 2


def test_missing_head_ref_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main(["--token", "t", "--version", "9.1.0", "--stage", "rc1", "--urgency", "LOW"])
    assert exc.value.code == 2


def test_missing_version_stage_urgency_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main(["--token", "t", "--head-ref", "unstable"])
    assert exc.value.code == 2


@pytest.mark.parametrize("bad_version", ["9.1", "v9.1.0", "9.1.0-rc1", "9.256.0", "nope"])
def test_malformed_version_is_usage_error(bad_version):
    # Fail fast (exit 2) at argparse, before any clone, rather than deep in promote().
    with pytest.raises(SystemExit) as exc:
        main(["--token", "t", "--head-ref", "unstable",
              "--version", bad_version, "--stage", "rc1", "--urgency", "LOW"])
    assert exc.value.code == 2


def test_version_canonicalized_before_cut(patched):
    # Leading zeros / trailing space must not leak past the boundary: the cut sees
    # the canonical M.m.p so version.h, headings, and branch names all agree.
    captured = _capture_cut(patched)
    main(["--token", "t", "--head-ref", "unstable",
          "--version", "09.1.0 ", "--stage", "rc2", "--urgency", "LOW"])
    assert captured["version"] == "9.1.0"


@pytest.mark.parametrize("bad_stage", ["beta", "rc0", "rc01", "ga1", ""])
def test_malformed_stage_is_usage_error(bad_stage):
    with pytest.raises(SystemExit) as exc:
        main(["--token", "t", "--head-ref", "unstable",
              "--version", "9.1.0", "--stage", bad_stage, "--urgency", "LOW"])
    assert exc.value.code == 2


def test_stage_normalized_before_cut(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--head-ref", "unstable",
          "--version", "9.1.0", "--stage", "RC2", "--urgency", "LOW"])
    assert captured["stage"] == "rc2"


@pytest.mark.parametrize("bad_urgency", ["URGENT", "medium-ish", "none"])
def test_bogus_urgency_is_usage_error(bad_urgency):
    with pytest.raises(SystemExit) as exc:
        main(["--token", "t", "--head-ref", "unstable",
              "--version", "9.1.0", "--stage", "rc1", "--urgency", bad_urgency])
    assert exc.value.code == 2


def test_urgency_uppercased_before_cut(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--head-ref", "unstable",
          "--version", "9.1.0", "--stage", "rc2", "--urgency", "high"])
    assert captured["urgency"] == "HIGH"


@pytest.mark.parametrize("bad_date", [
    "06/30/2026", "2026-13-45", "Jun 30 2026",
    # Rejected only if the format is checked explicitly: date.fromisoformat is
    # lenient on Python 3.11+ and would accept these, shipping a wrong/raw date
    # into the release heading (2026-W01-1 resolves to 2025-12-29).
    "20260630", "2026-W01-1", "2026-6-3",
])
def test_malformed_date_is_usage_error(bad_date):
    with pytest.raises(SystemExit) as exc:
        main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
              "--stage", "rc2", "--urgency", "LOW", "--date", bad_date])
    assert exc.value.code == 2


def test_valid_iso_date_accepted(patched):
    captured = _capture_cut(patched)
    rc = main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
               "--stage", "rc2", "--urgency", "LOW", "--date", "2026-06-30"])
    assert rc == 0


def test_rc1_first_minor_marks_baseline_unanchored(patched, caplog):
    # rc1 of M.0.0 with no derivable previous minor: the flag reaches cut() so the
    # PR body can warn the baseline is unanchored.
    captured = _capture_cut(patched)
    import logging
    with caplog.at_level(logging.WARNING):
        main(["--token", "t", "--head-ref", "unstable",
              "--version", "9.0.0", "--stage", "rc1", "--urgency", "LOW"])
    assert captured["baseline_unanchored"] is True


def test_baseline_anchored_when_base_ref_derived(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--head-ref", "unstable",
          "--version", "9.1.0", "--stage", "rc1", "--urgency", "LOW"])
    assert captured["baseline_unanchored"] is False


def test_missing_base_ref_aborts_before_cut(patched):
    # An explicit base_ref that resolves to nothing must abort with a clear error
    # before cut() runs (not deep in discovery). run_git is stubbed to a generic
    # MagicMock in `patched`, so make rev-parse fail to simulate an absent ref.
    import subprocess
    captured = _capture_cut(patched)

    def _run_git(repo_dir, *args, **kwargs):
        if args[:1] == ("rev-parse",):
            raise subprocess.CalledProcessError(1, ["git", *args])
        return MagicMock()

    patched.setattr(main_mod, "run_git", _run_git)
    rc = main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
               "--stage", "rc2", "--urgency", "LOW", "--base-ref", "no-such-ref"])
    assert rc == 1
    assert captured == {}  # cut() never reached


# --- dispatch + arg threading ---

def test_dispatches_to_cut_with_parsed_args(patched):
    captured = _capture_cut(patched)
    rc = main(["--token", "t", "--head-ref", "unstable",
               "--version", "9.1.0", "--stage", "rc2", "--urgency", "HIGH"])
    assert rc == 0
    assert captured["version"] == "9.1.0"
    assert captured["stage"] == "rc2"
    assert captured["urgency"] == "HIGH"
    assert captured["source_ref"] == "unstable"


def test_dry_run_threads_through(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
          "--stage", "rc1", "--urgency", "LOW", "--dry-run"])
    assert captured["dry_run"] is True


def test_security_from_advisories_defaults_false(patched):
    # Absent the flag/env, the cut must not attempt the advisory fetch.
    captured = _capture_cut(patched)
    main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW"])
    assert captured["security_from_advisories"] is False


def test_security_from_advisories_flag_threads_through(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW", "--security-from-advisories"])
    assert captured["security_from_advisories"] is True


def test_security_from_advisories_env_default_reaches_cut(patched, monkeypatch):
    # The workflow passes this input only as RELEASE_NOTES_SECURITY_FROM_ADVISORIES
    # ('true'/'false'), never as a CLI flag, so the env default must reach cut().
    captured = _capture_cut(patched)
    monkeypatch.setenv("RELEASE_NOTES_SECURITY_FROM_ADVISORIES", "true")
    main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW"])
    assert captured["security_from_advisories"] is True


def test_security_from_advisories_env_false_is_false(patched, monkeypatch):
    # 'false' is the literal string GitHub Actions exports for an unchecked box;
    # a bare bool(os.environ.get(...)) would misread it as truthy.
    captured = _capture_cut(patched)
    monkeypatch.setenv("RELEASE_NOTES_SECURITY_FROM_ADVISORIES", "false")
    main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW"])
    assert captured["security_from_advisories"] is False


def test_cut_failure_returns_one(patched):
    def _cut(repo, **kwargs):
        raise RuntimeError("boom")

    patched.setattr(main_mod.cut_mod, "cut", _cut)
    rc = main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
               "--stage", "rc1", "--urgency", "LOW"])
    assert rc == 1


def test_valueerror_logged_without_traceback(patched, caplog):
    # A validation ValueError from cut() carries a message written to stand on its
    # own; it is logged as an error line (not a traceback) and exits 1.
    def _cut(repo, **kwargs):
        raise ValueError("--base-ref 'nope' not found in the clone")

    patched.setattr(main_mod.cut_mod, "cut", _cut)
    with caplog.at_level("ERROR"):
        rc = main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
                   "--stage", "rc2", "--urgency", "LOW"])
    assert rc == 1
    msgs = [r.message for r in caplog.records]
    assert any("Release cut failed: --base-ref 'nope' not found" in m for m in msgs)
    # An exc_info traceback would attach the exception to the record; a clean
    # logger.error(...) does not.
    assert all(r.exc_info is None for r in caplog.records if "Release cut failed:" in r.message)


def test_calledprocesserror_stderr_logged(patched, caplog):
    import subprocess

    def _cut(repo, **kwargs):
        raise subprocess.CalledProcessError(128, ["git", "push"], stderr="protected ref")

    patched.setattr(main_mod.cut_mod, "cut", _cut)
    with caplog.at_level("ERROR"):
        rc = main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
                   "--stage", "rc2", "--urgency", "LOW"])
    assert rc == 1
    assert any("protected ref" in r.message for r in caplog.records)


# --- baseline glob / base-ref resolution ---

class TestDefaultTagGlob:
    def test_rc2_makes_rc_glob(self) -> None:
        # rc2+ anchors to the prior RC of this version.
        assert main_mod._default_tag_glob("9.1.0", "rc2") == "9.1.0-rc*"
        assert main_mod._default_tag_glob("9.1.0", "rc10") == "9.1.0-rc*"

    def test_rc1_has_no_glob(self) -> None:
        # rc1 has no rc0 to anchor to -> no glob (uses base_ref instead).
        assert main_mod._default_tag_glob("9.1.0", "rc1") is None

    def test_ga_has_no_glob(self) -> None:
        # GA continues an existing line; the no-glob nearest tag is correct.
        assert main_mod._default_tag_glob("9.1.0", "ga") is None

    def test_non_version_is_none(self) -> None:
        assert main_mod._default_tag_glob("9.1", "rc2") is None

    def test_case_insensitive(self) -> None:
        # A maintainer dispatching "RC2" must still get the rc glob; "RC1" still
        # has no glob (rc1 has no rc0 to anchor to, regardless of case).
        assert main_mod._default_tag_glob("9.1.0", "RC2") == "9.1.0-rc*"
        assert main_mod._default_tag_glob("9.1.0", "Rc10") == "9.1.0-rc*"
        assert main_mod._default_tag_glob("9.1.0", "RC1") is None


class TestDefaultBaseRefForRc1:
    def test_new_minor_uses_previous_minor_ga(self) -> None:
        # A new minor (patch 0) covers changes since the previous minor's GA.
        assert main_mod._default_base_ref_for_rc1("9.1.0") == "9.0.0"

    def test_patch_uses_prior_patch_not_previous_minor(self) -> None:
        # A patch cut covers only changes since the prior patch, so the baseline is
        # M.m.(p-1), NOT the previous minor. Deriving 9.1.0 here would re-credit
        # the entire 9.2.0/.1/.2 patch line.
        assert main_mod._default_base_ref_for_rc1("9.2.3") == "9.2.2"
        assert main_mod._default_base_ref_for_rc1("9.2.1") == "9.2.0"

    def test_patch_of_first_minor_uses_prior_patch(self) -> None:
        # 9.0.5 has patch>0, so its baseline is the prior patch 9.0.4. The old
        # minor==0 guard wrongly returned None (unanchored) for this.
        assert main_mod._default_base_ref_for_rc1("9.0.5") == "9.0.4"
        assert main_mod._default_base_ref_for_rc1("9.0.1") == "9.0.0"

    def test_first_release_of_major_has_none(self) -> None:
        # 9.0.0 has no previous release on this major to derive.
        assert main_mod._default_base_ref_for_rc1("9.0.0") is None

    def test_non_version_is_none(self) -> None:
        assert main_mod._default_base_ref_for_rc1("9.1") is None


def test_rc2_default_glob_passed_to_cut(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--head-ref", "unstable",
          "--version", "9.1.0", "--stage", "rc2", "--urgency", "LOW"])
    assert captured["tag_glob"] == "9.1.0-rc*"
    assert captured["base_ref"] is None


def test_rc1_without_base_ref_warns_and_defaults(patched, caplog):
    captured = _capture_cut(patched)
    import logging
    with caplog.at_level(logging.WARNING):
        main(["--token", "t", "--head-ref", "unstable",
              "--version", "9.1.0", "--stage", "rc1", "--urgency", "LOW"])
    # Defaults base_ref to the previous release, and suppresses the doomed glob.
    assert captured["base_ref"] == "9.0.0"
    assert captured["tag_glob"] is None
    assert any("rc1" in r.message and "9.0.0" in r.message for r in caplog.records)


def test_rc1_patch_defaults_base_ref_to_prior_patch(patched, caplog):
    # rc1 of a patch (9.2.3) must anchor to the prior patch 9.2.2, not the previous
    # minor 9.1.0, which silently re-credits the whole 9.2.x patch line. The
    # anchored value must reach cut() (baseline stays anchored, not unanchored).
    captured = _capture_cut(patched)
    import logging
    with caplog.at_level(logging.WARNING):
        main(["--token", "t", "--head-ref", "unstable",
              "--version", "9.2.3", "--stage", "rc1", "--urgency", "LOW"])
    assert captured["base_ref"] == "9.2.2"
    assert captured["tag_glob"] is None
    assert captured["baseline_unanchored"] is False


def test_rc1_uppercase_stage_still_defaults_base_ref(patched, caplog):
    # The rc1 base-ref default keys on a lowercased stage, so "RC1" must trigger
    # the previous-release default and suppress the doomed glob just like "rc1".
    captured = _capture_cut(patched)
    import logging
    with caplog.at_level(logging.WARNING):
        main(["--token", "t", "--head-ref", "unstable",
              "--version", "9.1.0", "--stage", "RC1", "--urgency", "LOW"])
    assert captured["base_ref"] == "9.0.0"
    assert captured["tag_glob"] is None


def test_rc1_with_explicit_base_ref_no_override(patched, caplog):
    captured = _capture_cut(patched)
    import logging
    with caplog.at_level(logging.WARNING):
        main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
              "--stage", "rc1", "--urgency", "LOW", "--base-ref", "9.0.4"])
    assert captured["base_ref"] == "9.0.4"  # user value wins, not the derived default
    assert captured["tag_glob"] is None
    # No rc1 baseline warning when the user supplied one.
    assert not any("Defaulting --base-ref" in r.message for r in caplog.records)


def test_rc1_first_minor_warns_without_default(patched, caplog):
    captured = _capture_cut(patched)
    import logging
    with caplog.at_level(logging.WARNING):
        main(["--token", "t", "--head-ref", "unstable",
              "--version", "9.0.0", "--stage", "rc1", "--urgency", "LOW"])
    # No previous-minor could be derived -> base_ref stays None, loud warning.
    assert captured["base_ref"] is None
    assert any("no previous-minor release" in r.message for r in caplog.records)


def test_explicit_base_ref_overrides_glob(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--head-ref", "feature/release-notes-automation",
          "--version", "9.1.0", "--stage", "rc2", "--urgency", "LOW", "--base-ref", "unstable"])
    assert captured["base_ref"] == "unstable"
    assert captured["tag_glob"] is None


def test_rc1_explicit_tag_glob_not_overridden_by_derived_base(patched):
    # An explicit --tag-glob means the user chose glob-based resolution; rc1's
    # derived base must not preempt it (which would set base_ref and discard the
    # glob). The glob reaches the cut and base_ref stays None.
    captured = _capture_cut(patched)
    main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
          "--stage", "rc1", "--urgency", "LOW", "--tag-glob", "9.1.*"])
    assert captured["base_ref"] is None
    assert captured["tag_glob"] == "9.1.*"


def test_rc1_derived_base_absent_degrades_not_aborts(patched, caplog):
    # On a tagless fork, rc1 of 9.1.0 derives 9.0.0, which is absent. A *derived*
    # (not user-supplied) base that resolves to nothing must degrade to the
    # nearest-tag fallback (like the M.0.0 path), not hard-fail the cut.
    import logging
    import subprocess

    captured = _capture_cut(patched)

    def _run_git(repo_dir, *args, **kwargs):
        if args[:1] == ("rev-parse",):
            raise subprocess.CalledProcessError(1, ["git", *args])  # derived tag absent
        return MagicMock()

    patched.setattr(main_mod, "run_git", _run_git)
    with caplog.at_level(logging.WARNING):
        rc = main(["--token", "t", "--head-ref", "unstable",
                   "--version", "9.1.0", "--stage", "rc1", "--urgency", "LOW"])
    assert rc == 0                              # cut ran; did not abort
    assert captured["base_ref"] is None         # derived value dropped
    assert captured["baseline_unanchored"] is True
    assert any("falling back to the nearest" in r.message for r in caplog.records)


def test_rc2_explicit_missing_base_still_aborts(patched):
    # Contrast with the derived case: an *explicit* --base-ref that is missing must
    # still hard-fail (the user asked for it), not silently fall back.
    import subprocess

    captured = _capture_cut(patched)

    def _run_git(repo_dir, *args, **kwargs):
        if args[:1] == ("rev-parse",):
            raise subprocess.CalledProcessError(1, ["git", *args])
        return MagicMock()

    patched.setattr(main_mod, "run_git", _run_git)
    rc = main(["--token", "t", "--head-ref", "unstable", "--version", "9.1.0",
               "--stage", "rc2", "--urgency", "LOW", "--base-ref", "no-such-ref"])
    assert rc == 1
    assert captured == {}  # cut() never reached
