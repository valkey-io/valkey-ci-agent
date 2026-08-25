from __future__ import annotations

import subprocess

from scripts.backport.registry import ValidationRule
from scripts.backport.validation import (
    changed_paths_since_base,
    select_validation_commands,
)


def test_select_validation_commands_appends_matching_rules_once() -> None:
    commands = select_validation_commands(
        ["make"],
        [
            ValidationRule(paths=("src/cluster_legacy.c",), commands=("cluster-smoke",)),
            ValidationRule(paths=("tests/unit/cluster/*.tcl",), commands=("cluster-smoke", "tcl-smoke")),
            ValidationRule(paths=("src/networking.c",), commands=("network-smoke",)),
        ],
        ["tests/unit/cluster/cli.tcl", "README.md"],
    )

    assert commands == ["make", "cluster-smoke", "tcl-smoke"]


def test_changed_paths_since_base_uses_merge_base(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "base.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "branch", "base"], cwd=tmp_path, check=True)

    (tmp_path / "changed.txt").write_text("changed\n", encoding="utf-8")
    subprocess.run(["git", "add", "changed.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "changed"], cwd=tmp_path, check=True, capture_output=True)

    assert changed_paths_since_base(str(tmp_path), "base") == ("changed.txt",)


def test_valkey_profile_runs_changed_tests_format_and_subsystem_checks(tmp_path) -> None:
    for path in (
        "src/rdb.c",
        "tests/integration/corrupt-dump.tcl",
        "tests/unit/cluster/packet.tcl",
    ):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("test body\n", encoding="utf-8")

    commands = select_validation_commands(
        ["make -j4 BUILD_TLS=yes"],
        [ValidationRule(paths=("src/rdb.c",), commands=("rdb-smoke",))],
        [
            "src/rdb.c",
            "tests/integration/corrupt-dump.tcl",
            "tests/unit/cluster/packet.tcl",
        ],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert commands[0] == "git diff --check"
    assert "clang-format-18 --dry-run --Werror -- src/rdb.c" in commands
    assert "make -j4 BUILD_TLS=yes" in commands
    assert "rdb-smoke" in commands
    assert "./runtest --single integration/corrupt-dump --clients 1" in commands
    assert "./runtest --single unit/cluster/packet --clients 1" in commands
    assert any("-DLOG_REQ_RES" in command for command in commands)
    assert any("--log-req-res" in command for command in commands)
    assert commands[-1].startswith("./utils/req-res-log-validator.py")


def test_valkey_profile_does_not_reply_log_top_level_skipped_test(tmp_path) -> None:
    test_path = tmp_path / "tests/integration/corrupt-dump-fuzzer.tcl"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        'tags {"dump" "logreqres:skip"} {\n    test body\n}\n',
        encoding="utf-8",
    )

    commands = select_validation_commands(
        [],
        [],
        ["tests/integration/corrupt-dump-fuzzer.tcl"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "./runtest --single integration/corrupt-dump-fuzzer --clients 1" in commands
    assert not any("--log-req-res" in command for command in commands)


def test_valkey_profile_uses_module_runner_for_moduleapi_test(tmp_path) -> None:
    test_path = tmp_path / "tests/unit/moduleapi/blockonkeys.tcl"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("test body\n", encoding="utf-8")

    commands = select_validation_commands(
        [],
        [],
        ["tests/unit/moduleapi/blockonkeys.tcl"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "./runtest-moduleapi --single unit/moduleapi/blockonkeys --clients 1" in commands
    assert "./runtest --single unit/moduleapi/blockonkeys --clients 1" not in commands
    assert any(
        command.startswith("CFLAGS='-Werror' ./runtest-moduleapi")
        and "--log-req-res" in command
        for command in commands
    )
