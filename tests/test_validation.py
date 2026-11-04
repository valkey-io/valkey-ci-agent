"""Tests for path-based validation command selection."""

from __future__ import annotations

import subprocess

from scripts.backport.registry import ValidationRule
from scripts.backport.validation import (
    UNMAPPED_TEST_PATHS_PREFIX,
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
    (tmp_path / "src").mkdir()
    (tmp_path / "src/.clang-format").write_text("BasedOnStyle: LLVM\n", encoding="utf-8")
    workflow = tmp_path / ".github/workflows/clang-format.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
run: |
  python3 -m venv "$RUNNER_TEMP/clang-format"
  "$RUNNER_TEMP/clang-format/bin/pip" install clang-format==18.1.8
  echo "$RUNNER_TEMP/clang-format/bin" >> "$GITHUB_PATH"
  clang-format --version
  clang-format -i **/*.c **/*.h
""",
        encoding="utf-8",
    )
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
    assert "clang-format --dry-run --Werror -- src/rdb.c" in commands
    assert "make -j4 BUILD_TLS=yes" in commands
    assert "rdb-smoke" in commands
    assert "./runtest --single integration/corrupt-dump --clients 1" in commands
    assert "./runtest --single unit/cluster/packet --clients 1" in commands
    assert any("-DLOG_REQ_RES" in command for command in commands)
    assert any("--log-req-res" in command for command in commands)
    assert commands[-1].startswith("./utils/req-res-log-validator.py")


def test_valkey_profile_only_clang_formats_paths_upstream_formats(tmp_path) -> None:
    """Upstream runs clang-format inside src/ only, using src/.clang-format.

    Checking a C file outside src/ falls back to clang's built-in LLVM style and
    fails a candidate whose upstream CI is green.
    """
    for path in (
        "src/.clang-format",
        "src/rdb.c",
        "tests/modules/basics.c",
        "deps/lua/src/lapi.c",
    ):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("BasedOnStyle: LLVM\n", encoding="utf-8")
    workflow = tmp_path / ".github/workflows/clang-format.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "run: clang-format-18 -i **/*.c **/*.h\n",
        encoding="utf-8",
    )

    commands = select_validation_commands(
        [],
        [],
        ["src/rdb.c", "tests/modules/basics.c", "deps/lua/src/lapi.c"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    formatting = [command for command in commands if command.startswith("clang-format")]
    assert formatting == ["clang-format --dry-run --Werror -- src/rdb.c"]


def test_valkey_profile_does_not_treat_formatter_setup_as_a_format_job(
    tmp_path,
) -> None:
    """Setup, comments and echoed examples do not say which suffixes CI formats."""
    for path in ("src/.clang-format", "src/server.c"):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("source\n", encoding="utf-8")
    workflow = tmp_path / ".github/workflows/clang-format.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        """\
# clang-format -i **/*.c **/*.h
run: echo 'clang-format -i **/*.c **/*.h'
other: python3 -m pip install clang-format -i https://packages.example.invalid/simple
run: python3 -m pip install clang-format==18.1.8
""",
        encoding="utf-8",
    )

    commands = select_validation_commands(
        [],
        [],
        ["src/server.c"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert not any(command.startswith("clang-format") for command in commands)


def test_valkey_profile_skips_clang_format_when_release_has_no_style(tmp_path) -> None:
    """Valkey 7.2 has no src/.clang-format, so clang's LLVM default is unrelated."""
    source = tmp_path / "src/server.c"
    source.parent.mkdir(parents=True)
    source.write_text("void f(void) {}\n", encoding="utf-8")

    commands = select_validation_commands(
        [],
        [],
        ["src/server.c"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert not any(command.startswith("clang-format") for command in commands)


def test_valkey_profile_skips_clang_format_when_release_has_style_but_no_ci(
    tmp_path,
) -> None:
    """Valkey 8.0 carries a style file but does not enforce it in CI."""
    source = tmp_path / "src/server.c"
    source.parent.mkdir(parents=True)
    source.write_text("void f(void) {}\n", encoding="utf-8")
    (tmp_path / "src/.clang-format").write_text(
        "BasedOnStyle: LLVM\n",
        encoding="utf-8",
    )

    commands = select_validation_commands(
        [],
        [],
        ["src/server.c"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert not any(command.startswith("clang-format") for command in commands)


def test_valkey_profile_only_formats_suffixes_enforced_by_release_ci(
    tmp_path,
) -> None:
    """Valkey 8.1/9.0 format C but not the C++ suffixes added in 9.1."""
    for path in ("src/.clang-format", "src/server.c", "src/unit/test.cpp"):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("source\n", encoding="utf-8")
    workflow = tmp_path / ".github/workflows/clang-format.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "run: clang-format-18 -i **/*.c **/*.h\n",
        encoding="utf-8",
    )

    commands = select_validation_commands(
        [],
        [],
        ["src/server.c", "src/unit/test.cpp"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    formatting = [command for command in commands if command.startswith("clang-format")]
    assert formatting == ["clang-format --dry-run --Werror -- src/server.c"]


def test_valkey_profile_only_formats_candidate_line_ranges(tmp_path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    source = tmp_path / "src/server.c"
    source.parent.mkdir(parents=True)
    source.write_text(
        "void preexisting( ){ }\nvoid changed(void) { }\n",
        encoding="utf-8",
    )
    (tmp_path / "src/.clang-format").write_text(
        "BasedOnStyle: LLVM\n",
        encoding="utf-8",
    )
    workflow = tmp_path / ".github/workflows/clang-format.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "run: clang-format-18 -i **/*.c **/*.h\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "branch", "base"], cwd=tmp_path, check=True)

    source.write_text(
        "void preexisting( ){ }\nvoid changed(int value) { }\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/server.c"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "candidate"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    commands = select_validation_commands(
        [],
        [],
        ["src/server.c"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
        base_ref="base",
    )

    formatting = [command for command in commands if command.startswith("clang-format")]
    assert formatting == ["clang-format --dry-run --Werror --lines=2:2 -- src/server.c"]


def test_valkey_profile_runs_tls_test_in_tls_mode_for_both_validation_legs(
    tmp_path,
) -> None:
    """tests/unit/tls.tcl otherwise passes while skipping its entire body."""
    test_path = tmp_path / "tests/unit/tls.tcl"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        'start_server {tags {"tls"}} { if {$::tls} { test body } }\n',
        encoding="utf-8",
    )

    commands = select_validation_commands(
        [],
        [],
        ["tests/unit/tls.tcl"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "./runtest --single unit/tls --clients 1 --tls" in commands
    reply = [command for command in commands if "--log-req-res" in command]
    assert len(reply) == 1
    assert "--single unit/tls" in reply[0]
    assert reply[0].endswith("--force-resp3 --tls")


def test_valkey_profile_preserves_reply_logs_when_tls_and_plain_tests_change(
    tmp_path,
) -> None:
    for path in ("tests/unit/tls.tcl", "tests/unit/type/string.tcl"):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("test body\n", encoding="utf-8")

    commands = select_validation_commands(
        [],
        [],
        ["tests/unit/tls.tcl", "tests/unit/type/string.tcl"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    reply = [command for command in commands if "--log-req-res" in command]
    assert len(reply) == 2
    assert "--single unit/type/string" in reply[0]
    assert "--dont-pre-clean" not in reply[0]
    assert "--single unit/tls" in reply[1]
    assert "--dont-pre-clean" in reply[1]
    assert reply[1].endswith("--force-resp3 --tls")


def test_valkey_profile_runs_a_smoke_set_for_harness_changes(tmp_path) -> None:
    """A whole serialized suite cannot finish inside the per-command timeout.

    Upstream budgets 24 hours and three shards for that run, so a tests/support
    change is proven with a fixed smoke set instead.
    """
    commands = select_validation_commands(
        [],
        [],
        ["tests/support/util.tcl"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert commands == [
        "git diff --check",
        "./runtest --single unit/other --single unit/keyspace --single unit/dump "
        "--single unit/protocol --single unit/type/incr --clients 1 --tags -slow",
    ]
    assert "./runtest --clients 1" not in commands


def test_valkey_profile_maps_all_release_branch_harness_helpers(tmp_path) -> None:
    changed_paths = [
        "tests/helpers/fake_redis_node.tcl",
        "tests/instances.tcl",
        "tests/support/server.tcl",
        "tests/test_helper.tcl",
    ]
    for path in changed_paths:
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("helper body\n", encoding="utf-8")

    commands = select_validation_commands(
        [],
        [],
        changed_paths,
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert sum("--single unit/other" in command for command in commands) == 1
    assert not any(UNMAPPED_TEST_PATHS_PREFIX in command for command in commands)


def test_valkey_profile_targets_moduleapi_units_that_load_the_module(tmp_path) -> None:
    """Module sources map to units by the .so path they name, not by basename.

    tests/modules/defragtest.c is only exercised by defrag.tcl, so a basename
    rule would silently skip the one unit that covers the change.
    """
    modules_dir = tmp_path / "tests/unit/moduleapi"
    modules_dir.mkdir(parents=True)
    (modules_dir / "defrag.tcl").write_text(
        "set testmodule [file normalize tests/modules/defragtest.so]\n",
        encoding="utf-8",
    )
    (modules_dir / "hash.tcl").write_text(
        "set testmodule [file normalize tests/modules/hash.so]\n",
        encoding="utf-8",
    )
    (tmp_path / "tests/modules").mkdir(parents=True)

    commands = select_validation_commands(
        [],
        [],
        ["tests/modules/defragtest.c"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "./runtest-moduleapi --single unit/moduleapi/defrag --clients 1" in commands
    assert not any(command.rstrip() == "./runtest-moduleapi --clients 1" for command in commands)
    assert any(
        command.startswith("CFLAGS='-Werror' ./runtest-moduleapi --single unit/moduleapi/defrag ")
        for command in commands
    )


def test_valkey_profile_falls_back_to_whole_moduleapi_suite_when_unmappable(tmp_path) -> None:
    """Module build infrastructure cannot be narrowed to a unit, so run everything.

    The fallback drops --clients 1 as well: the whole suite serialized at one
    client is exactly the shape that cannot finish inside the timeout.
    """
    (tmp_path / "tests/unit/moduleapi").mkdir(parents=True)

    commands = select_validation_commands(
        [],
        [],
        ["tests/modules/Makefile"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "./runtest-moduleapi" in commands
    assert "./runtest-moduleapi --clients 1" not in commands


def test_valkey_profile_pre_cleans_when_no_runtest_leg_wrote_logs(tmp_path) -> None:
    """--dont-pre-clean only exists to preserve logs a preceding ./runtest wrote.

    Kept unconditionally, a previous candidate's tests/tmp reqres files survive
    into this run and get validated against this branch's schemas.
    """
    (tmp_path / "tests/unit/moduleapi").mkdir(parents=True)
    (tmp_path / "tests/unit/moduleapi/hash.tcl").write_text(
        "set testmodule [file normalize tests/modules/hash.so]\n",
        encoding="utf-8",
    )

    commands = select_validation_commands(
        [],
        [],
        ["tests/modules/hash.c"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    reply = [command for command in commands if "--log-req-res" in command]
    assert len(reply) == 1
    assert "--dont-pre-clean" not in reply[0]


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


def test_valkey_profile_can_temporarily_skip_req_res_validator(
    tmp_path,
    monkeypatch,
) -> None:
    test_path = tmp_path / "tests/integration/replication.tcl"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("test body\n", encoding="utf-8")
    monkeypatch.setenv("BACKPORT_SKIP_REQ_RES_VALIDATION", "1")

    commands = select_validation_commands(
        [],
        [],
        ["tests/integration/replication.tcl"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert any("--log-req-res" in command for command in commands)
    assert not any(
        command.startswith("./utils/req-res-log-validator.py")
        for command in commands
    )


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


def test_valkey_profile_targets_moduleapi_directly_with_legacy_wrapper(tmp_path) -> None:
    """The 7.2/8.0 wrapper appends selection after a hard-coded full suite."""
    test_path = tmp_path / "tests/unit/moduleapi/blockonkeys.tcl"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("test body\n", encoding="utf-8")
    (tmp_path / "runtest-moduleapi").write_text(
        "$MAKE -C tests/modules && tclsh tests/test_helper.tcl "
        "--single unit/moduleapi/basics \"${@}\"\n",
        encoding="utf-8",
    )

    commands = select_validation_commands(
        [],
        [],
        ["tests/unit/moduleapi/blockonkeys.tcl"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert (
        "make -C tests/modules && ./runtest "
        "--single unit/moduleapi/blockonkeys --clients 1"
    ) in commands
    assert any(
        command.startswith(
            "CFLAGS='-Werror' make -C tests/modules && ./runtest "
            "--single unit/moduleapi/blockonkeys --clients 1 "
        )
        for command in commands
    )
    assert not any(
        command.startswith("./runtest-moduleapi --single") for command in commands
    )


def test_valkey_profile_uses_existing_cluster_smoke_on_legacy_branch(tmp_path) -> None:
    """7.2's cluster runner rejects --clients and has a different test namespace."""
    source = tmp_path / "src/cluster.c"
    source.parent.mkdir(parents=True)
    source.write_text("void cluster(void) {}\n", encoding="utf-8")
    smoke = tmp_path / "tests/unit/cluster/misc.tcl"
    smoke.parent.mkdir(parents=True)
    smoke.write_text("test body\n", encoding="utf-8")

    commands = select_validation_commands(
        [],
        [],
        ["src/cluster.c"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "./runtest --single unit/cluster/misc --clients 1" in commands
    assert not any(command.startswith("./runtest-cluster") for command in commands)


def test_valkey_profile_runs_changed_legacy_cluster_test_directly(tmp_path) -> None:
    test_path = tmp_path / "tests/cluster/tests/03-failover-loop.tcl"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("test body\n", encoding="utf-8")
    smoke = tmp_path / "tests/unit/cluster/misc.tcl"
    smoke.parent.mkdir(parents=True)
    smoke.write_text("test body\n", encoding="utf-8")

    commands = select_validation_commands(
        [],
        [],
        ["tests/cluster/tests/03-failover-loop.tcl"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "./runtest-cluster --single 03-failover-loop" in commands
    assert not any("no explicit validation mapping" in command for command in commands)


def test_valkey_profile_maps_legacy_cluster_runner_files(tmp_path) -> None:
    runner = tmp_path / "tests/cluster/run.tcl"
    runner.parent.mkdir(parents=True)
    runner.write_text("runner body\n", encoding="utf-8")
    smoke = tmp_path / "tests/unit/cluster/misc.tcl"
    smoke.parent.mkdir(parents=True)
    smoke.write_text("test body\n", encoding="utf-8")

    commands = select_validation_commands(
        [],
        [],
        ["tests/cluster/run.tcl"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "./runtest --single unit/cluster/misc --clients 1" in commands
    assert not any(UNMAPPED_TEST_PATHS_PREFIX in command for command in commands)


def test_valkey_profile_rejects_unit_test_suffix_target_does_not_build(
    tmp_path,
) -> None:
    makefile = tmp_path / "src/Makefile"
    makefile.parent.mkdir(parents=True)
    makefile.write_text(
        "ENGINE_TEST_FILES := $(wildcard unit/*.c)\n"
        "test-unit:\n\t@echo tests\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "src/unit/test_networking.cpp"
    test_path.parent.mkdir()
    test_path.write_text("TEST(Networking, Regression) {}\n", encoding="utf-8")

    commands = select_validation_commands(
        ["make"],
        [],
        ["src/unit/test_networking.cpp"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "make -C src test-unit" in commands
    unmapped_command = next(
        command
        for command in commands
        if UNMAPPED_TEST_PATHS_PREFIX in command
        and "src/unit/test_networking.cpp" in command
    )
    assert commands.index("make") < commands.index(unmapped_command)
    assert commands.index("make -C src test-unit") < commands.index(unmapped_command)


def test_valkey_profile_accepts_unit_test_suffix_target_builds(tmp_path) -> None:
    makefile = tmp_path / "src/unit/Makefile"
    makefile.parent.mkdir(parents=True)
    makefile.write_text(
        "SOURCES := $(wildcard *.cpp)\n"
        "test-unit:\n\t@echo tests\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "src/unit/test_networking.cpp"
    test_path.write_text("TEST(Networking, Regression) {}\n", encoding="utf-8")

    commands = select_validation_commands(
        [],
        [],
        ["src/unit/test_networking.cpp"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "make -C src test-unit" in commands
    assert not any(UNMAPPED_TEST_PATHS_PREFIX in command for command in commands)


def test_valkey_profile_treats_unit_test_header_as_harness_input(tmp_path) -> None:
    header = tmp_path / "src/unit/test_help.h"
    header.parent.mkdir(parents=True)
    header.write_text("#define TEST_HELP 1\n", encoding="utf-8")

    commands = select_validation_commands(
        [],
        [],
        ["src/unit/test_help.h"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert "make -C src test-unit" in commands
    assert not any(UNMAPPED_TEST_PATHS_PREFIX in command for command in commands)


def test_valkey_profile_fails_closed_for_unmapped_changed_test(tmp_path) -> None:
    test_path = tmp_path / "tests/new-harness/case.tcl"
    test_path.parent.mkdir(parents=True)
    test_path.write_text("test body\n", encoding="utf-8")

    commands = select_validation_commands(
        [],
        [],
        ["tests/new-harness/case.tcl"],
        validation_profile="valkey-core",
        repo_dir=str(tmp_path),
    )

    assert any(
        UNMAPPED_TEST_PATHS_PREFIX in command
        and "no explicit validation mapping" in command
        and "tests/new-harness/case.tcl" in command
        for command in commands
    )
