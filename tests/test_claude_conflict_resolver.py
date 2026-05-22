"""Tests for Claude Code-based conflict resolver."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.backport.conflict_resolver import resolve_conflicts_with_claude
from scripts.backport.models import BackportPRContext, ConflictedFile


def _pr_context() -> BackportPRContext:
    return BackportPRContext(
        source_pr_number=1234,
        source_pr_title="Fix memory leak in cluster.c",
        source_pr_url="https://github.com/valkey-io/valkey/pull/1234",
        source_pr_diff="",
        target_branch="8.1",
        commits=["abc123"],
    )


def _agent_result(stdout: str, stderr: str = "", rc: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=rc)


def test_whitespace_only_conflict_skips_claude(tmp_path: Path) -> None:
    cf = ConflictedFile(
        path="src/server.c",
        target_branch_content="foo  ",
        source_branch_content="foo",
    )
    results = resolve_conflicts_with_claude(str(tmp_path), [cf], _pr_context())
    assert len(results) == 1
    assert results[0].resolved_content == "foo"
    assert "whitespace" in results[0].resolution_summary
    assert results[0].source == "automatic"


def test_claude_resolves_conflict(tmp_path: Path) -> None:
    # Write a conflicted file to disk
    src = tmp_path / "src"
    src.mkdir()
    conflicted = src / "cluster.c"
    conflicted.write_text("<<<<<<< HEAD\nold code\n=======\nnew code\n>>>>>>> abc123\n")

    cf = ConflictedFile(
        path="src/cluster.c",
        target_branch_content="old code",
        source_branch_content="new code",
    )

    # Mock Claude Code to edit the file (simulate resolution)
    captured = {}

    def mock_agent(_profile, prompt, **kw):
        captured["prompt"] = prompt
        # Simulate Claude editing the file
        conflicted.write_text("new code\n")
        result_event = json.dumps({"type": "result", "result": "Resolved conflict in src/cluster.c"})
        return _agent_result(f'{{"type":"system","subtype":"init"}}\n{result_event}')

    with patch("scripts.backport.conflict_resolver.run_agent", side_effect=mock_agent):
        results = resolve_conflicts_with_claude(str(tmp_path), [cf], _pr_context())

    assert len(results) == 1
    assert results[0].resolved_content == "new code\n"
    assert "Claude Code" in results[0].resolution_summary
    assert "untrusted data" in captured["prompt"]


def test_unresolved_conflict_returns_none(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    conflicted = src / "cluster.c"
    conflicted.write_text("<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc\n")

    cf = ConflictedFile(
        path="src/cluster.c",
        target_branch_content="old",
        source_branch_content="new",
    )

    # Mock Claude Code that does nothing (file unchanged)
    def mock_agent(_profile, prompt, **kw):
        # Claude did not edit the file
        return _agent_result('{"type":"result","result":"I could not resolve this"}')

    with patch("scripts.backport.conflict_resolver.run_agent", side_effect=mock_agent):
        results = resolve_conflicts_with_claude(str(tmp_path), [cf], _pr_context())

    assert len(results) == 1
    assert results[0].resolved_content is None
    assert "file unchanged" in results[0].resolution_summary


def test_result_event_with_non_string_payload_is_logged_safely(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    conflicted = src / "cluster.c"
    conflicted.write_text("<<<<<<< HEAD\nold code\n=======\nnew code\n>>>>>>> abc123\n")

    cf = ConflictedFile(
        path="src/cluster.c",
        target_branch_content="old code",
        source_branch_content="new code",
    )

    def mock_agent(_profile, prompt, **kw):
        conflicted.write_text("new code\n")
        result_event = json.dumps({"type": "result", "result": {"summary": "resolved"}})
        return _agent_result(result_event)

    with patch("scripts.backport.conflict_resolver.run_agent", side_effect=mock_agent):
        results = resolve_conflicts_with_claude(str(tmp_path), [cf], _pr_context())

    assert len(results) == 1
    assert results[0].resolved_content == "new code\n"


def test_claude_nonzero_exit_returns_unresolved(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    conflicted = src / "cluster.c"
    conflicted.write_text("<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc\n")

    cf = ConflictedFile(
        path="src/cluster.c",
        target_branch_content="old",
        source_branch_content="new",
    )

    with patch(
        "scripts.backport.conflict_resolver.run_agent",
        return_value=_agent_result("", stderr="bedrock failed", rc=1),
    ):
        results = resolve_conflicts_with_claude(str(tmp_path), [cf], _pr_context())

    assert len(results) == 1
    assert results[0].resolved_content is None
    assert "Claude Code failed" in results[0].resolution_summary
    assert "bedrock failed" in results[0].resolution_summary


def test_validation_failure_retries_claude_once(tmp_path: Path) -> None:
    """If Claude leaves conflict markers in the file, the resolver retries once."""
    src = tmp_path / "tests" / "unit"
    src.mkdir(parents=True)
    conflicted = src / "cluster.tcl"
    conflicted.write_text("<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc\n")

    cf = ConflictedFile(
        path="tests/unit/cluster.tcl",
        target_branch_content="old",
        source_branch_content="new",
    )
    prompts: list[str] = []

    def mock_agent(_profile, prompt, **kw):
        prompts.append(prompt)
        if len(prompts) == 1:
            # Pass 1: leave conflict markers in the file → triggers retry.
            conflicted.write_text(
                "<<<<<<< HEAD\nold\n=======\nproc f {} { return ok }\n>>>>>>> abc\n"
            )
        else:
            # Pass 2: actually resolve.
            conflicted.write_text("proc f {} { return ok }\n")
        return _agent_result('{"type":"result","result":"Resolved"}')

    with patch("scripts.backport.conflict_resolver.run_agent", side_effect=mock_agent):
        results = resolve_conflicts_with_claude(str(tmp_path), [cf], _pr_context())

    assert len(prompts) == 2
    assert "failed validation" in prompts[1]
    assert len(results) == 1
    assert results[0].resolved_content == "proc f {} { return ok }\n"


def test_mixed_whitespace_and_real_conflicts(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    real_conflict = src / "cluster.c"
    real_conflict.write_text("<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc\n")

    ws_file = ConflictedFile(
        path="src/server.c",
        target_branch_content="foo  \n",
        source_branch_content="foo\n",
    )
    real_file = ConflictedFile(
        path="src/cluster.c",
        target_branch_content="old",
        source_branch_content="new",
    )

    def mock_agent(_profile, prompt, **kw):
        real_conflict.write_text("new\n")
        return _agent_result('{"type":"result","result":"Resolved"}')

    with patch("scripts.backport.conflict_resolver.run_agent", side_effect=mock_agent):
        results = resolve_conflicts_with_claude(str(tmp_path), [ws_file, real_file], _pr_context())

    assert len(results) == 2
    ws_result = next(r for r in results if r.path == "src/server.c")
    real_result = next(r for r in results if r.path == "src/cluster.c")
    assert "whitespace" in ws_result.resolution_summary
    assert real_result.resolved_content == "new\n"


def test_unchanged_file_without_markers_detected(tmp_path: Path) -> None:
    """If Claude doesn't edit the file and it has no conflict markers
    (e.g., add/add conflict where git leaves one side), the pre-hash
    check catches it as unresolved."""
    src = tmp_path / "src"
    src.mkdir()
    # File without conflict markers (git left target side)
    target_file = src / "server.c"
    target_file.write_text("int main() { return 0; }\n")

    cf = ConflictedFile(
        path="src/server.c",
        target_branch_content="int main() { return 0; }\n",
        source_branch_content="int main() { return 1; }\n",
    )

    def mock_agent(_profile, prompt, **kw):
        # Claude does nothing to the file
        return _agent_result('{"type":"result","result":"Could not resolve"}')

    with patch("scripts.backport.conflict_resolver.run_agent", side_effect=mock_agent):
        results = resolve_conflicts_with_claude(str(tmp_path), [cf], _pr_context())

    assert len(results) == 1
    assert results[0].resolved_content is None
    assert "file unchanged" in results[0].resolution_summary


def test_claude_editing_unlisted_file_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    src = repo / "src"
    src.mkdir()
    conflicted = src / "cluster.c"
    other = src / "server.c"
    conflicted.write_text("<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> abc\n")
    other.write_text("unchanged\n")
    subprocess.run(["git", "add", "src/server.c"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    cf = ConflictedFile(
        path="src/cluster.c",
        target_branch_content="old",
        source_branch_content="new",
    )

    def mock_agent(_profile, prompt, **kw):
        conflicted.write_text("new\n")
        other.write_text("unexpected edit\n")
        return _agent_result('{"type":"result","result":"Resolved"}')

    with patch("scripts.backport.conflict_resolver.run_agent", side_effect=mock_agent):
        results = resolve_conflicts_with_claude(str(repo), [cf], _pr_context())

    assert len(results) == 1
    assert results[0].resolved_content is None
    assert "outside the conflict set" in results[0].resolution_summary
    assert "src/server.c" in results[0].resolution_summary
