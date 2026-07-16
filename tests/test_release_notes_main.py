"""Tests for the release-cut entry point (orchestration mocked).

main is now cut-only: it always dispatches to release_cut.cut(). The cut
internals are tested in test_release_notes_release_cut.py; here we cover
argument validation, the baseline-glob/base-ref resolution, and that the parsed
inputs reach cut().
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from scripts.common.proc import git_output, run_git
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
    "RELEASE_NOTES_SECURITY_FROM_ADVISORIES", "RELEASE_NOTES_FORCE_READY",
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
    # Default the rc1 previous-release resolver to a no-op (no earlier release), so
    # a generic rc1 test does not shell out to real git against the fake clone dir.
    # Tests that exercise the resolver override this with their own stub.
    monkeypatch.setattr(main_mod.discover_mod, "resolve_previous_release_tag",
                        lambda clone_dir, version: None)
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
        main(["--version", "9.1.0", "--stage", "rc1", "--urgency", "LOW"])
    assert exc.value.code == 2


def test_missing_version_stage_urgency_is_usage_error():
    with pytest.raises(SystemExit) as exc:
        main(["--token", "t", ])
    assert exc.value.code == 2


@pytest.mark.parametrize("bad_version", ["9.1", "v9.1.0", "9.1.0-rc1", "9.256.0", "nope"])
def test_malformed_version_is_usage_error(bad_version):
    # Fail fast (exit 2) at argparse, before any clone, rather than deep in promote().
    with pytest.raises(SystemExit) as exc:
        main(["--token", "t", "--version", bad_version, "--stage", "rc1", "--urgency", "LOW"])
    assert exc.value.code == 2


def test_version_canonicalized_before_cut(patched):
    # Leading zeros / trailing space must not leak past the boundary: the cut sees
    # the canonical M.m.p so version.h, headings, and branch names all agree.
    captured = _capture_cut(patched)
    main(["--token", "t",
          "--version", "09.1.0 ", "--stage", "rc2", "--urgency", "LOW"])
    assert captured["version"] == "9.1.0"


@pytest.mark.parametrize("bad_stage", ["beta", "rc0", "rc01", "ga1", ""])
def test_malformed_stage_is_usage_error(bad_stage):
    with pytest.raises(SystemExit) as exc:
        main(["--token", "t", "--version", "9.1.0", "--stage", bad_stage, "--urgency", "LOW"])
    assert exc.value.code == 2


def test_stage_normalized_before_cut(patched):
    captured = _capture_cut(patched)
    main(["--token", "t",
          "--version", "9.1.0", "--stage", "RC2", "--urgency", "LOW"])
    assert captured["stage"] == "rc2"


@pytest.mark.parametrize("bad_urgency", ["URGENT", "medium-ish", "none"])
def test_bogus_urgency_is_usage_error(bad_urgency):
    with pytest.raises(SystemExit) as exc:
        main(["--token", "t", "--version", "9.1.0", "--stage", "rc1", "--urgency", bad_urgency])
    assert exc.value.code == 2


def test_urgency_uppercased_before_cut(patched):
    captured = _capture_cut(patched)
    main(["--token", "t",
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
        main(["--token", "t", "--version", "9.1.0",
              "--stage", "rc2", "--urgency", "LOW", "--date", bad_date])
    assert exc.value.code == 2


def test_valid_iso_date_accepted(patched):
    captured = _capture_cut(patched)
    rc = main(["--token", "t", "--version", "9.1.0",
               "--stage", "rc2", "--urgency", "LOW", "--date", "2026-06-30"])
    assert rc == 0


def test_rc1_no_prior_release_marks_baseline_unanchored(patched, caplog):
    # rc1 whose repo carries no earlier release tag (first release ever, or a
    # tagless fork): resolve_previous_release_tag returns None, so the cut falls
    # back to nearest-tag resolution and the flag reaches cut() so the PR body can
    # warn the baseline is unanchored. base_ref stays None (tag resolution).
    captured = _capture_cut(patched)
    patched.setattr(main_mod.discover_mod, "resolve_previous_release_tag",
                    lambda clone_dir, version: None)
    import logging
    with caplog.at_level(logging.WARNING):
        main(["--token", "t", "--version", "9.0.0", "--stage", "rc1", "--urgency", "LOW"])
    assert captured["baseline_unanchored"] is True
    assert captured["base_ref"] is None


def test_rc1_anchors_to_resolved_previous_release(patched):
    # rc1 with an earlier release tag in the repo: the resolver picks it, it reaches
    # cut() as base_ref, and the baseline is anchored (not flagged). The resolver is
    # repo-driven, so a skipped-minor jump (9.1.0 -> the 8.2 line's tag) threads the
    # same way; here we just prove the resolved tag is what cut() receives.
    captured = _capture_cut(patched)
    patched.setattr(main_mod.discover_mod, "resolve_previous_release_tag",
                    lambda clone_dir, version: ("8.2.0", "a" * 40))
    main(["--token", "t",
          "--version", "9.1.0", "--stage", "rc1", "--urgency", "LOW"])
    assert captured["base_ref"] == "8.2.0"
    assert captured["baseline_unanchored"] is False
    # rc1 never resolves a same-version glob; it uses the previous-release base.
    assert captured["tag_glob"] is None


def test_rc1_explicit_base_ref_not_overridden_by_resolver(patched):
    # An explicit --base-ref for rc1 wins: the previous-release resolver must never
    # override the maintainer's choice. The resolver may now be *read* by the
    # range-widening guard, so we don't forbid the call; instead we return a
    # sentinel tag and assert it never becomes the base. base_ref stays as given.
    captured = _capture_cut(patched)

    patched.setattr(main_mod.discover_mod, "resolve_previous_release_tag",
                    lambda clone_dir, version: ("SENTINEL-NOT-THE-BASE", "deadbeef"))
    # The range-widening guard shells out via git_output against the (fake) clone;
    # neutralize it so this test stays about "explicit base wins", not git behavior.
    patched.setattr(main_mod, "_recredited_commit_count",
                    lambda *a, **k: None)
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc1", "--urgency", "LOW", "--base-ref", "my-baseline"])
    assert captured["base_ref"] == "my-baseline"  # explicit value wins, not the sentinel
    assert captured["baseline_unanchored"] is False


def test_rc1_explicit_tag_glob_skips_resolver(patched):
    # An explicit --tag-glob for rc1 means the maintainer chose glob-based tag
    # resolution; the previous-release resolver must not run and base_ref stays None.
    captured = _capture_cut(patched)

    def _boom(*a, **k):
        raise AssertionError("resolver must not run when --tag-glob is explicit")

    patched.setattr(main_mod.discover_mod, "resolve_previous_release_tag", _boom)
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc1", "--urgency", "LOW", "--tag-glob", "9.0.*"])
    assert captured["base_ref"] is None
    assert captured["tag_glob"] == "9.0.*"


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
    rc = main(["--token", "t", "--version", "9.1.0",
               "--stage", "rc2", "--urgency", "LOW", "--base-ref", "no-such-ref"])
    assert rc == 1
    assert captured == {}  # cut() never reached


# --- dispatch + arg threading ---

def test_dispatches_to_cut_with_parsed_args(patched):
    captured = _capture_cut(patched)
    rc = main(["--token", "t",  "--version", "9.1.0", "--stage", "rc2", "--urgency", "HIGH"])
    assert rc == 0
    assert captured["version"] == "9.1.0"
    assert captured["stage"] == "rc2"
    assert captured["urgency"] == "HIGH"


def test_dry_run_threads_through(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc1", "--urgency", "LOW", "--dry-run"])
    assert captured["dry_run"] is True


def test_force_ready_defaults_false(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW"])
    assert captured["force_ready"] is False


def test_force_ready_flag_threads_through(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW", "--force-ready"])
    assert captured["force_ready"] is True


def test_force_ready_env_default_reaches_cut(patched, monkeypatch):
    # The workflow passes this input only as RELEASE_NOTES_FORCE_READY
    # ('true'/'false'), never as a CLI flag, so the env default must reach cut().
    captured = _capture_cut(patched)
    monkeypatch.setenv("RELEASE_NOTES_FORCE_READY", "true")
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW"])
    assert captured["force_ready"] is True


def test_force_ready_env_false_is_false(patched, monkeypatch):
    captured = _capture_cut(patched)
    monkeypatch.setenv("RELEASE_NOTES_FORCE_READY", "false")
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW"])
    assert captured["force_ready"] is False


def test_security_from_advisories_defaults_false(patched):
    # Absent the flag/env, the cut must not attempt the advisory fetch.
    captured = _capture_cut(patched)
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW"])
    assert captured["security_from_advisories"] is False


def test_security_from_advisories_flag_threads_through(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW", "--security-from-advisories"])
    assert captured["security_from_advisories"] is True


def test_security_from_advisories_env_default_reaches_cut(patched, monkeypatch):
    # The workflow passes this input only as RELEASE_NOTES_SECURITY_FROM_ADVISORIES
    # ('true'/'false'), never as a CLI flag, so the env default must reach cut().
    captured = _capture_cut(patched)
    monkeypatch.setenv("RELEASE_NOTES_SECURITY_FROM_ADVISORIES", "true")
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW"])
    assert captured["security_from_advisories"] is True


def test_security_from_advisories_env_false_is_false(patched, monkeypatch):
    # 'false' is the literal string GitHub Actions exports for an unchecked box;
    # a bare bool(os.environ.get(...)) would misread it as truthy.
    captured = _capture_cut(patched)
    monkeypatch.setenv("RELEASE_NOTES_SECURITY_FROM_ADVISORIES", "false")
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc2", "--urgency", "LOW"])
    assert captured["security_from_advisories"] is False


def test_cut_failure_returns_one(patched):
    def _cut(repo, **kwargs):
        raise RuntimeError("boom")

    patched.setattr(main_mod.cut_mod, "cut", _cut)
    rc = main(["--token", "t", "--version", "9.1.0",
               "--stage", "rc1", "--urgency", "LOW"])
    assert rc == 1


def test_valueerror_logged_without_traceback(patched, caplog):
    # A validation ValueError from cut() carries a message written to stand on its
    # own; it is logged as an error line (not a traceback) and exits 1.
    def _cut(repo, **kwargs):
        raise ValueError("--base-ref 'nope' not found in the clone")

    patched.setattr(main_mod.cut_mod, "cut", _cut)
    with caplog.at_level("ERROR"):
        rc = main(["--token", "t", "--version", "9.1.0",
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
        rc = main(["--token", "t", "--version", "9.1.0",
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

    def test_ga_scopes_glob_to_line(self) -> None:
        # A patch GA resolves its baseline by tag; the M.m.* glob keeps a
        # concurrent sibling line's tag out of the candidate set. (A first GA of a
        # new minor anchors to its pre-release branch and drops this glob.)
        assert main_mod._default_tag_glob("8.1.9", "ga") == "8.1.*"
        assert main_mod._default_tag_glob("9.1.0", "ga") == "9.1.*"

    def test_ga_case_insensitive(self) -> None:
        assert main_mod._default_tag_glob("8.1.9", "GA") == "8.1.*"

    def test_non_version_is_none(self) -> None:
        assert main_mod._default_tag_glob("9.1", "rc2") is None
        assert main_mod._default_tag_glob("9.1", "ga") is None

    def test_case_insensitive(self) -> None:
        # A maintainer dispatching "RC2" must still get the rc glob; "RC1" still
        # has no glob (rc1 has no rc0 to anchor to, regardless of case).
        assert main_mod._default_tag_glob("9.1.0", "RC2") == "9.1.0-rc*"
        assert main_mod._default_tag_glob("9.1.0", "Rc10") == "9.1.0-rc*"
        assert main_mod._default_tag_glob("9.1.0", "RC1") is None


def test_rc2_default_glob_passed_to_cut(patched):
    captured = _capture_cut(patched)
    main(["--token", "t",
          "--version", "9.1.0", "--stage", "rc2", "--urgency", "LOW"])
    assert captured["tag_glob"] == "9.1.0-rc*"
    assert captured["base_ref"] is None


def test_rc1_defers_baseline_to_post_clone_resolver(patched):
    # rc1 with no --base-ref / --tag-glob does not compute a baseline at parse time
    # (the old arithmetic guess). It flags resolve_rc1_baseline and lets _run_cut
    # resolve the previous release from the repo's tags after the clone. Here the
    # resolver is stubbed; assert the resolved value reaches cut() and no glob is set.
    captured = _capture_cut(patched)
    patched.setattr(main_mod.discover_mod, "resolve_previous_release_tag",
                    lambda clone_dir, version: ("9.0.0", "a" * 40))
    main(["--token", "t",
          "--version", "9.1.0", "--stage", "rc1", "--urgency", "LOW"])
    assert captured["base_ref"] == "9.0.0"
    assert captured["tag_glob"] is None


def test_rc1_uppercase_stage_still_resolves_previous_release(patched):
    # resolve_rc1_baseline keys on the normalized stage, so "RC1" defers to the
    # post-clone resolver just like "rc1".
    captured = _capture_cut(patched)
    patched.setattr(main_mod.discover_mod, "resolve_previous_release_tag",
                    lambda clone_dir, version: ("9.0.0", "a" * 40))
    main(["--token", "t",
          "--version", "9.1.0", "--stage", "RC1", "--urgency", "LOW"])
    assert captured["base_ref"] == "9.0.0"
    assert captured["tag_glob"] is None


def test_explicit_base_ref_overrides_glob(patched):
    captured = _capture_cut(patched)
    main(["--token", "t", "--version", "9.1.0", "--stage", "rc2", "--urgency", "LOW", "--base-ref", "unstable"])
    assert captured["base_ref"] == "unstable"
    assert captured["tag_glob"] is None


def test_rc1_explicit_tag_glob_not_overridden_by_derived_base(patched):
    # An explicit --tag-glob means the user chose glob-based resolution; rc1's
    # derived base must not preempt it (which would set base_ref and discard the
    # glob). The glob reaches the cut and base_ref stays None.
    captured = _capture_cut(patched)
    main(["--token", "t", "--version", "9.1.0",
          "--stage", "rc1", "--urgency", "LOW", "--tag-glob", "9.1.*"])
    assert captured["base_ref"] is None
    assert captured["tag_glob"] == "9.1.*"


def test_rc1_no_previous_release_degrades_not_aborts(patched, caplog):
    # On a tagless fork (or the very first release), the previous-release resolver
    # returns None. rc1 must degrade to nearest-tag resolution and flag the baseline
    # unanchored, not hard-fail the cut.
    import logging

    captured = _capture_cut(patched)
    patched.setattr(main_mod.discover_mod, "resolve_previous_release_tag",
                    lambda clone_dir, version: None)
    with caplog.at_level(logging.WARNING):
        rc = main(["--token", "t",      "--version", "9.1.0", "--stage", "rc1", "--urgency", "LOW"])
    assert rc == 0                              # cut ran; did not abort
    assert captured["base_ref"] is None         # no anchor; falls to tag resolution
    assert captured["baseline_unanchored"] is True
    assert any("no earlier release tag" in r.message for r in caplog.records)


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
    rc = main(["--token", "t", "--version", "9.1.0",
               "--stage", "rc2", "--urgency", "LOW", "--base-ref", "no-such-ref"])
    assert rc == 1
    assert captured == {}  # cut() never reached


# --- explicit --base-ref range-widening guard (real git graphs) ---

def _init_repo(path) -> str:
    repo = str(path)
    run_git(repo, "init", "-q", "-b", "unstable")
    run_git(repo, "config", "user.email", "t@t")
    run_git(repo, "config", "user.name", "t")
    return repo


def _commit(repo: str, subject: str) -> str:
    run_git(repo, "commit", "-q", "--allow-empty", "-m", subject)
    return git_output(repo, "rev-parse", "HEAD").strip()


def _fork_at_freeze_repo(tmp_path):
    """A repo mirroring valkey's fork-at-freeze shape.

    ``unstable`` advances past a freeze point; the previous release ``9.0.0`` is
    tagged on its own branch off that freeze point (so it is not an ancestor of
    ``unstable``). An older release ``8.1.0`` is tagged further back on unstable's
    own history. Returns the repo path.
    """
    repo = _init_repo(tmp_path)
    _commit(repo, "old base (#1)")
    run_git(repo, "tag", "8.1.0")              # older release, on unstable's history
    _commit(repo, "shipped in 9.0 line (#2)")  # freeze point for the 9.0 line
    _commit(repo, "more pre-freeze (#3)")
    # 9.0.0 tagged on its own branch off the freeze point (fork-at-freeze).
    run_git(repo, "checkout", "-q", "-b", "pre-release-9.0.0")
    _commit(repo, "9.0.0 release commit (#4)")
    run_git(repo, "tag", "9.0.0")
    run_git(repo, "checkout", "-q", "unstable")
    _commit(repo, "new work A (#5)")
    _commit(repo, "new work B (#6)")
    return repo


class TestBaseRefRangeGuard:
    def test_legit_previous_release_base_no_warning(self, tmp_path, caplog):
        # The correct base (previous release 9.0.0) is not an ancestor of unstable
        # under fork-at-freeze, yet its range re-credits nothing. The guard must
        # stay silent (an is_ancestor(base, head) guard would wrongly fire here).
        import logging
        repo = _fork_at_freeze_repo(tmp_path)
        with caplog.at_level(logging.WARNING):
            main_mod._warn_if_base_ref_reaches_past_previous_release(
                repo, "9.0.0", "unstable", "9.1.0"
            )
        assert not any("reaches back past" in r.message for r in caplog.records)

    def test_too_old_base_warns(self, tmp_path, caplog):
        # A base older than the previous release (8.1.0 instead of 9.0.0) drags the
        # range back and re-credits already-shipped commits -> loud warning.
        import logging
        repo = _fork_at_freeze_repo(tmp_path)
        with caplog.at_level(logging.WARNING):
            main_mod._warn_if_base_ref_reaches_past_previous_release(
                repo, "8.1.0", "unstable", "9.1.0"
            )
        assert any("reaches back past" in r.message for r in caplog.records)

    def test_recredited_count_zero_for_clean_base(self, tmp_path):
        repo = _fork_at_freeze_repo(tmp_path)
        # base=9.0.0, prev=9.0.0: nothing already-released is re-included.
        assert main_mod._recredited_commit_count(repo, "9.0.0", "unstable", "9.0.0") == 0

    def test_recredited_count_positive_for_old_base(self, tmp_path):
        repo = _fork_at_freeze_repo(tmp_path)
        # base=8.1.0 with prev=9.0.0: the 9.0-line pre-freeze commits (#2,#3) are
        # reachable from 9.0.0 but re-appear in 8.1.0..unstable.
        assert main_mod._recredited_commit_count(repo, "8.1.0", "unstable", "9.0.0") > 0

    def test_unresolvable_ref_returns_none(self, tmp_path):
        repo = _fork_at_freeze_repo(tmp_path)
        assert main_mod._recredited_commit_count(repo, "no-such-ref", "unstable", "9.0.0") is None

    def test_no_previous_release_skips_guard(self, tmp_path, caplog):
        # First release ever (no tag below target): nothing to compare, no warning.
        import logging
        repo = _init_repo(tmp_path)
        _commit(repo, "only (#1)")
        _commit(repo, "two (#2)")
        with caplog.at_level(logging.WARNING):
            main_mod._warn_if_base_ref_reaches_past_previous_release(
                repo, "HEAD~1", "unstable", "9.1.0"
            )
        assert not any("reaches back past" in r.message for r in caplog.records)
