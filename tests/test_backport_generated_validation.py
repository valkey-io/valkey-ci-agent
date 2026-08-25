from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.backport.registry import GeneratedFileRule
from scripts.backport.sweep_validation import prepare_generated_files


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
