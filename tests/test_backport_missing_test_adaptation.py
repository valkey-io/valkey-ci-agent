"""Security and repository-policy tests for missing-test adaptation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from scripts.backport.missing_test_adaptation import (
    adapt_target_missing_tests_with_claude,
    build_test_adaptation_prompt,
    is_test_path,
)
from scripts.backport.models import BackportCandidate

SEARCH_TEST_PATTERNS = (
    "testing/*.cc",
    "vmsdk/testing/*.cc",
    "integration/test_*.py",
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_valkey_search_test_layout_is_registry_extensible() -> None:
    assert is_test_path("testing/acl_test.cc", SEARCH_TEST_PATTERNS)
    assert is_test_path(
        "testing/coordinator/client_test.cc",
        SEARCH_TEST_PATTERNS,
    )
    assert is_test_path("vmsdk/testing/module_test.cc", SEARCH_TEST_PATTERNS)
    assert is_test_path("integration/test_cancel.py", SEARCH_TEST_PATTERNS)
    assert not is_test_path("src/module.cc", SEARCH_TEST_PATTERNS)


def test_adaptation_rejects_repository_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    tests_dir = repo / "testing"
    tests_dir.mkdir()
    victim = tmp_path / "outside.cc"
    victim.write_text("outside original\n", encoding="utf-8")
    link = tests_dir / "acl_test.cc"
    link.symlink_to(victim)
    _git(repo, "add", "testing/acl_test.cc")
    _git(repo, "commit", "-q", "-m", "tracked test symlink")

    def fake_agent(_profile, _prompt, *, cwd):
        sandbox_path = Path(cwd, "testing", "acl_test.cc")
        assert not sandbox_path.exists()
        sandbox_path.write_text("sandbox adaptation\n", encoding="utf-8")
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"result","result":"adapted"}\n',
            stderr="",
        )

    result = adapt_target_missing_tests_with_claude(
        str(repo),
        BackportCandidate(
            source_pr_number=42,
            source_pr_title="test",
            source_pr_url="https://example.test/pull/42",
            target_branch="1.2",
        ),
        {"testing/new_test.cc": "TEST(NewTest, Works) {}\n"},
        language="c++",
        test_path_patterns=SEARCH_TEST_PATTERNS,
        run_agent_func=fake_agent,
    )

    assert result.fatal is True
    assert "invalid generated test path" in result.summary
    assert victim.read_text(encoding="utf-8") == "outside original\n"


def test_adaptation_reports_removed_existing_test_without_deleting_it(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    test_path = repo / "testing" / "acl_test.cc"
    test_path.parent.mkdir()
    test_path.write_text("TEST(ACL, Existing) {}\n", encoding="utf-8")
    _git(repo, "add", "testing/acl_test.cc")
    _git(repo, "commit", "-q", "-m", "existing test")

    def fake_agent(_profile, _prompt, *, cwd):
        Path(cwd, "testing", "acl_test.cc").unlink()
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"result","result":"adapted"}\n',
            stderr="",
        )

    result = adapt_target_missing_tests_with_claude(
        str(repo),
        BackportCandidate(
            source_pr_number=42,
            source_pr_title="test",
            source_pr_url="https://example.test/pull/42",
            target_branch="1.2",
        ),
        {"testing/new_test.cc": "TEST(NewTest, Works) {}\n"},
        language="c++",
        test_path_patterns=SEARCH_TEST_PATTERNS,
        run_agent_func=fake_agent,
    )

    assert result.fatal is True
    assert result.summary == (
        "test adaptation not applied: removed existing test path(s): "
        "testing/acl_test.cc"
    )
    assert test_path.read_text(encoding="utf-8") == "TEST(ACL, Existing) {}\n"


def test_a_long_upstream_test_is_given_whole_as_a_file_not_cut(tmp_path: Path) -> None:
    """Its first 12,000 characters were all the agent saw, with nothing to say there was more."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    candidate = BackportCandidate(
        source_pr_number=42, source_pr_title="test", source_pr_url="https://example.test/pull/42",
        target_branch="1.2",
    )
    long_test = "TEST(New, Start) {}\n" + "// filler\n" * 2_000 + "TEST(New, AtTheEnd) {}\n"
    sources = {"testing/long_test.cc": long_test, "testing/short_test.cc": "TEST(Short, Works) {}\n"}

    prompt = build_test_adaptation_prompt(
        str(repo), candidate, sources, language="c++", context_dir=tmp_path / "upstream",
    )

    whole = tmp_path / "upstream" / "00-long_test.cc"
    assert whole.read_text(encoding="utf-8") == long_test
    assert f"Read the whole text at {whole}" in prompt and "TEST(New, AtTheEnd)" not in prompt
    # A short one is inline, as before, and nothing is written for it.
    assert "TEST(Short, Works) {}" in prompt
    assert [path.name for path in (tmp_path / "upstream").iterdir()] == ["00-long_test.cc"]

    # With nowhere to write it, the cut is at least said.
    assert "more characters are not shown" in build_test_adaptation_prompt(
        str(repo), candidate, sources, language="c++",
    )


def test_adaptation_hides_cleanly_added_upstream_test_from_editable_sandbox(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    native = repo / "testing/acl_test.cc"
    native.parent.mkdir()
    native.write_text("TEST(ACL, Existing) {}\n", encoding="utf-8")
    added = repo / "testing/new_test.cc"
    added.write_text("TEST(NewTest, Works) {}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "candidate")

    def fake_agent(_profile, _prompt, *, cwd):
        assert not Path(cwd, "testing/new_test.cc").exists()
        Path(cwd, "testing/acl_test.cc").write_text(
            "TEST(ACL, Existing) {}\nTEST(ACL, PortedRegression) {}\n",
            encoding="utf-8",
        )
        return SimpleNamespace(
            returncode=0,
            stdout='{"type":"result","result":"ported regression"}\n',
            stderr="",
        )

    result = adapt_target_missing_tests_with_claude(
        str(repo),
        BackportCandidate(
            source_pr_number=42,
            source_pr_title="test",
            source_pr_url="https://example.test/pull/42",
            target_branch="1.2",
        ),
        {"testing/new_test.cc": "TEST(NewTest, Works) {}\n"},
        language="c++",
        test_path_patterns=SEARCH_TEST_PATTERNS,
        excluded_test_paths=("testing/new_test.cc",),
        run_agent_func=fake_agent,
    )

    assert result.fatal is False
    assert result.adapted_paths == ["testing/acl_test.cc"]
    assert "PortedRegression" in native.read_text(encoding="utf-8")
    assert added.exists()
