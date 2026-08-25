from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.backport import sweep_validation
from scripts.backport.registry import GeneratedFileRule
from scripts.backport.sweep_validation import (
    ValidationOutcome,
    prepare_generated_files,
    validate_branch_with_optional_repair,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "src/unit").mkdir(parents=True)
    (repo / "src/unit/test_example.c").write_text("int test_new(void) {}\n", encoding="utf-8")
    (repo / "src/unit/test_files.h").write_text("stale\n", encoding="utf-8")
    (repo / "generate.py").write_text(
        "from pathlib import Path\n"
        "Path('src/unit/test_files.h').write_text('generated\\n')\n",
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "candidate")


def test_generated_output_is_amended_into_candidate_and_converges(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    starting_count = _git(tmp_path, "rev-list", "--count", "HEAD")

    outcome = prepare_generated_files(
        str(tmp_path),
        ("src/unit/test_example.c",),
        [
            GeneratedFileRule(
                paths=("src/unit/*.c",),
                command="python3 generate.py",
                outputs=("src/unit/test_files.h",),
            )
        ],
    )

    assert outcome.ok is True
    assert outcome.generated_paths == ("src/unit/test_files.h",)
    assert outcome.amended_commit_sha == _git(tmp_path, "rev-parse", "HEAD")
    assert _git(tmp_path, "rev-list", "--count", "HEAD") == starting_count
    assert (tmp_path / "src/unit/test_files.h").read_text() == "generated\n"
    assert _git(tmp_path, "status", "--porcelain") == ""


def test_generated_command_fails_closed_on_unexpected_path(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "unexpected.txt").write_text("before\n", encoding="utf-8")
    _git(tmp_path, "add", "unexpected.txt")
    _git(tmp_path, "commit", "-q", "-m", "track unexpected")
    (tmp_path / "generate.py").write_text(
        "from pathlib import Path\n"
        "Path('src/unit/test_files.h').write_text('generated\\n')\n"
        "Path('unexpected.txt').write_text('changed\\n')\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", "generate.py")
    _git(tmp_path, "commit", "-q", "-m", "bad generator")

    outcome = prepare_generated_files(
        str(tmp_path),
        ("src/unit/test_example.c",),
        [
            GeneratedFileRule(
                paths=("src/unit/*.c",),
                command="python3 generate.py",
                outputs=("src/unit/test_files.h",),
            )
        ],
    )

    assert outcome.ok is False
    assert "unexpected path" in outcome.output
    assert (tmp_path / "src/unit/test_files.h").read_text() == "stale\n"
    assert (tmp_path / "unexpected.txt").read_text() == "before\n"
    assert _git(tmp_path, "status", "--porcelain") == ""


def test_generated_rule_rejects_output_not_tracked_on_target(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    outcome = prepare_generated_files(
        str(tmp_path),
        ("src/unit/test_example.c",),
        [
            GeneratedFileRule(
                paths=("src/unit/*.c",),
                command="python3 generate.py",
                outputs=("src/unit/not-on-target.h",),
            )
        ],
    )

    assert outcome.ok is False
    assert "not tracked on the target branch" in outcome.output
    assert _git(tmp_path, "status", "--porcelain") == ""


def test_successful_repair_regenerates_and_revalidates_final_tree(monkeypatch) -> None:
    generated_calls: list[tuple[str, ...]] = []
    validation_calls: list[str] = []

    def fake_prepare(_repo_dir, changed_paths, _rules, **_kwargs):
        generated_calls.append(changed_paths)
        if len(generated_calls) == 1:
            return ValidationOutcome(True, "")
        return ValidationOutcome(
            True,
            "",
            generated_paths=("src/unit/test_files.h",),
            amended_commit_sha="c" * 40,
        )

    def fake_validate(*_args, **_kwargs):
        validation_calls.append("validate")
        if len(validation_calls) == 1:
            return False, "initial failure"
        return True, "final tree passed"

    monkeypatch.setattr(sweep_validation, "prepare_generated_files", fake_prepare)
    monkeypatch.setattr(sweep_validation, "validate_backport_branch", fake_validate)
    monkeypatch.setattr(
        sweep_validation,
        "repair_validation_failure_with_claude",
        lambda *_args, **_kwargs: ValidationOutcome(
            True,
            "repaired tree passed",
            ai_summary="updated generator input",
        ),
    )
    monkeypatch.setattr(
        sweep_validation,
        "changed_paths_since_base",
        lambda *_args: ("src/unit/test_example.c", "generate.py"),
    )
    monkeypatch.setattr(sweep_validation, "head_sha", lambda *_args: "b" * 40)

    outcome = validate_branch_with_optional_repair(
        "/repo",
        "9.0",
        ["make"],
        [],
        repair=True,
        generated_file_rules=[object()],
    )

    assert outcome.ok is True
    assert outcome.output == "final tree passed"
    assert outcome.generated_paths == ("src/unit/test_files.h",)
    assert outcome.amended_commit_sha == "c" * 40
    assert generated_calls == [
        ("src/unit/test_example.c", "generate.py"),
        ("src/unit/test_example.c", "generate.py"),
    ]
    assert len(validation_calls) == 2


def test_failed_post_repair_generation_rolls_back_exact_repair_base(monkeypatch) -> None:
    generated_calls = 0
    git_calls: list[tuple[str, ...]] = []

    def fake_prepare(*_args, **_kwargs):
        nonlocal generated_calls
        generated_calls += 1
        if generated_calls == 1:
            return ValidationOutcome(True, "")
        return ValidationOutcome(False, "generated-file command did not converge")

    monkeypatch.setattr(sweep_validation, "prepare_generated_files", fake_prepare)
    monkeypatch.setattr(
        sweep_validation,
        "validate_backport_branch",
        lambda *_args, **_kwargs: (False, "initial failure"),
    )
    monkeypatch.setattr(
        sweep_validation,
        "repair_validation_failure_with_claude",
        lambda *_args, **_kwargs: ValidationOutcome(True, "repaired tree passed"),
    )
    monkeypatch.setattr(
        sweep_validation,
        "changed_paths_since_base",
        lambda *_args: ("generate.py",),
    )
    monkeypatch.setattr(sweep_validation, "head_sha", lambda *_args: "b" * 40)

    outcome = validate_branch_with_optional_repair(
        "/repo",
        "9.0",
        ["make"],
        [],
        repair=True,
        generated_file_rules=[object()],
        run_git=lambda _repo_dir, *args, **_kwargs: git_calls.append(args),
    )

    assert outcome.ok is False
    assert "did not converge" in outcome.output
    assert git_calls == [("reset", "--hard", "b" * 40)]
