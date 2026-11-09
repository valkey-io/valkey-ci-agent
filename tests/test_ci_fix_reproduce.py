from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from scripts.ci_fix.models import FailedTest
from scripts.ci_fix.reproduce import (
    LOOPS,
    REPRO_LOOPS,
    JobCommands,
    ReproPlan,
    ReproStatus,
    TestRun,
    build_plan,
    failure_report,
    plan_for,
    ran_tests,
    reproduce,
    shows_failure_of,
)
from scripts.common.text_utils import strip_ansi

TEST = FailedTest("INCREX BYFLOAT overflow preserves existing TTL", "tests/unit/type/incr.tcl")
UNIT_TEST = FailedTest("Crc64Test.TestCrc64", "src/unit/test_crc64.cpp")


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "runtest").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_helper.tcl").write_text("} elseif {$opt eq {--fastfail}} {\n", encoding="utf-8")
    return str(tmp_path)


def test_without_the_jobs_commands_plain_make_and_runtest_loop_only_the_failing_test(checkout):
    plan = plan_for(checkout, (TEST,), scheduled=True)

    assert plan.command(20) == (
        'make -j"$(nproc)" && ./runtest --dump-logs --accurate --single unit/type/incr '
        "--only 'INCREX BYFLOAT overflow preserves existing TTL' --fastfail --loops 20 --timeout 600"
    )
    assert "plain make and runtest" in plan.notes[-1]


def test_a_pr_run_does_not_use_accurate(checkout):
    assert "--accurate" not in plan_for(checkout, (TEST,), scheduled=False).test


def test_a_timed_out_client_without_a_test_name_runs_its_whole_file(checkout):
    plan = plan_for(checkout, (FailedTest("", TEST.file),), scheduled=False)

    assert "--only" not in plan.test and "--single unit/type/incr" in plan.test
    assert "whole files run" in plan.notes[0]


@pytest.mark.parametrize("tests", [
    (),
    (FailedTest("failover", "tests/sentinel/tests/05-manual.tcl"),),  # another harness
    (FailedTest("x", "tests/unit/../../../../tmp/e.tcl"),),  # leaves the suite
])
def test_only_main_suite_tests_are_planned(checkout, tests):
    assert plan_for(checkout, tests, scheduled=True) is None

    plan = plan_for(checkout, (TEST, *tests), scheduled=True)
    assert plan.tests == (TEST,) and "sentinel" not in plan.test and "../" not in plan.test


def _gtest_checkout(checkout, *, parallel=True):
    (Path(checkout) / "src" / "unit").mkdir(parents=True)
    (Path(checkout) / "src" / "unit" / "Makefile").write_text("test-unit:\n", encoding="utf-8")
    if parallel:
        (Path(checkout) / "deps" / "gtest-parallel").mkdir(parents=True)
        (Path(checkout) / "deps" / "gtest-parallel" / "gtest_parallel.py").write_text("", encoding="utf-8")


def test_a_c_unit_test_is_repeated_by_name_the_way_the_job_ran_it(checkout):
    """Through gtest-parallel, one process per test, with the job's own test arguments."""
    _gtest_checkout(checkout)
    job = JobCommands(build="make all-with-unit-tests SERVER_CFLAGS='-Werror'", runtest="./runtest --dump-logs",
                      unittest="make test-unit accurate=1")

    plan = plan_for(checkout, (UNIT_TEST, TEST), scheduled=True, job=job)

    assert plan.command(20) == (
        "make -j\"$(nproc)\" all-with-unit-tests SERVER_CFLAGS='-Werror' && "
        '{ test -x ./src/unit/valkey-unit-gtests || make -j"$(nproc)" all-with-unit-tests; } && '
        "(cd src/unit && python3 ../../deps/gtest-parallel/gtest_parallel.py ./valkey-unit-gtests "
        "--timeout_per_test 300 --repeat=20 --gtest_filter=Crc64Test.TestCrc64 -- --accurate)"
    )
    assert plan.tests == (UNIT_TEST,) and plan.notes == () and not plan.valgrind
    # gtest-parallel's own lines, and the binary's for a failed test, are what its runs report.
    assert plan.failed(TestRun(passed=False, output="", report="[  FAILED  ] Crc64Test.TestCrc64 (0 ms)"))
    assert plan.failed(TestRun(passed=False, output="", report="[1/20] Crc64Test.TestCrc64 killed by signal SIGSEGV (3 ms)"))
    assert plan.ran(TestRun(passed=True, output="", report="[20/20] Crc64Test.TestCrc64 (1 ms)"))
    assert not plan.ran(TestRun(passed=True, output="", report="[1/1] Other.Test (0 ms)"))
    # Several unit tests: one filter.
    two = plan_for(checkout, (UNIT_TEST, FailedTest("Sizes/HashtableTest.Grow/2", "test_hashtable.cpp")), scheduled=True)
    assert "--gtest_filter=Crc64Test.TestCrc64:Sizes/HashtableTest.Grow/2 " in two.test


def test_the_jobs_unit_test_arguments_and_make_variables_are_kept_apart(checkout):
    """A large-memory test skips itself without --large-memory; a 32-bit gtest needs the job's libraries."""
    _gtest_checkout(checkout)
    job = JobCommands(
        build="make 32bit SERVER_CFLAGS='-Werror'", runtest="./runtest",
        unittest='make test-unit large_memory=1 seed=7 GTEST_LIBS="/usr/lib32/libgtest.a /usr/lib32/libgmock.a"',
    )

    plan = plan_for(checkout, (UNIT_TEST,), scheduled=True, job=job)

    # The job's build names no unit tests, but makes them: nothing is rebuilt as 64-bit.
    assert plan.build == (
        "make -j\"$(nproc)\" 32bit SERVER_CFLAGS='-Werror' && { test -x ./src/unit/valkey-unit-gtests || "
        "make -j\"$(nproc)\" all-with-unit-tests 'GTEST_LIBS=/usr/lib32/libgtest.a /usr/lib32/libgmock.a'; }"
    )
    assert plan.test.endswith("-- --large-memory --seed 7)")
    without = plan_for(checkout, (UNIT_TEST,), scheduled=True, job=JobCommands(build="make", runtest="./runtest"))
    assert "--" not in without.test.split("--gtest_filter")[1] and "was not found" in without.notes[0]


def test_a_gtest_runs_output_is_read_the_way_runtests_is(checkout):
    """What run_test_command keeps of the output (failure_report) is what the plan judges."""
    _gtest_checkout(checkout)
    plan = plan_for(checkout, (UNIT_TEST,), scheduled=True)
    failing = (
        "[==========] Running 1 test from 1 test suite.\n[ RUN      ] Crc64Test.TestCrc64\n"
        "src/unit/test_crc64.cpp:29: Failure\nExpected equality of these values:\n"
        "[  FAILED  ] Crc64Test.TestCrc64 (0 ms)\n[  FAILED  ] 1 test, listed below:\n"
        "[  FAILED  ] Crc64Test.TestCrc64\n 1 FAILED TEST\n"
        "[1/1] Crc64Test.TestCrc64 returned with exit code 1 (2 ms)\nFAILED TESTS (1/1):\n       2 ms: Crc64Test.TestCrc64\n"
    )
    passing = "[1/2] Other.Test (3 ms)\n[2/2] Crc64Test.TestCrc64 (1 ms)\n"
    hung = "\x1b[31m[  TIMEOUT ]\x1b[0m Crc64Test.TestCrc64 timed out after 300 s\n"
    unrelated = "[ RUN      ] Other.Test\n[       OK ] Other.Test (1 ms)\n"

    assert plan.failed(TestRun(passed=False, output=failing, report=failure_report(failing)))
    assert plan.ran(TestRun(passed=True, output=passing, report=failure_report(passing)))
    assert plan.failed(TestRun(passed=False, output=hung, report=failure_report(strip_ansi(hung))))
    assert not plan.ran(TestRun(passed=True, output=unrelated, report=failure_report(unrelated)))
    assert not plan.failed(TestRun(passed=False, output="make: *** No rule", report=failure_report("make: *** No rule")))


def test_without_gtest_parallel_the_binary_repeats_in_one_process_and_no_gtest_means_no_plan(checkout):
    assert plan_for(checkout, (UNIT_TEST,), scheduled=True) is None  # no src/unit/Makefile: an older harness
    _gtest_checkout(checkout, parallel=False)

    plan = plan_for(checkout, (UNIT_TEST,), scheduled=False, job=JobCommands(build="make", runtest="./runtest"))

    assert plan.build == 'make -j"$(nproc)" && { test -x ./src/unit/valkey-unit-gtests || make -j"$(nproc)" all-with-unit-tests; }'
    assert plan.test == "./src/unit/valkey-unit-gtests --gtest_filter=Crc64Test.TestCrc64 --gtest_repeat=\0loops\0"
    assert any("no gtest-parallel" in note for note in plan.notes)


def test_the_jobs_own_runtest_watchdog_is_kept_up_to_the_cap(checkout):
    """A valgrind job's 2400s is kept: lowered to 600s, slow tests timed out instead of failing as CI did."""
    def plan(runtest):
        return plan_for(checkout, (TEST,), scheduled=True, job=JobCommands(build="make", runtest=runtest))

    assert plan("./runtest --timeout 300 --dump-logs").test.endswith("--timeout 300")
    assert plan("./runtest --dump-logs").test.endswith("--timeout 600")
    valgrind = plan("./runtest --valgrind --clients 1 --timeout 2400 --dump-logs")
    assert valgrind.test.endswith("--timeout 2400") and valgrind.notes == ()
    longer = plan("./runtest --timeout 3600 --dump-logs")
    assert longer.test.endswith("--timeout 2400")
    assert longer.notes == ("The job's --timeout 3600 was lowered to 2400s for the reproduction: "
                            "a test that times out at 2400s may be slow here, not hung.",)


def test_there_is_no_plan_without_runtest(tmp_path):
    assert plan_for(str(tmp_path / "missing"), (TEST,), scheduled=True) is None


# --- reproduce ---------------------------------------------------------------

PLAN = ReproPlan(build="make", test=f"./runtest --loops {LOOPS}", tests=(TEST,))
PASS = TestRun(passed=True, output="all passed", report=f"[ok]: {TEST.name} (5 ms)")
FAIL = TestRun(passed=False, output="", report=f"[err]: {TEST.name} in {TEST.file}")
BROKEN = TestRun(passed=False, output="tclsh: command not found")


def _runner(*results):
    calls = []
    queue = iter(results)

    def run(command, timeout):
        calls.append(command)
        return next(queue)

    run.calls = calls
    return run


def _deadline(seconds=3600):
    return time.monotonic() + seconds


def test_a_failure_on_two_plain_runs_is_deterministic():
    run = _runner(FAIL, FAIL)
    result = reproduce(PLAN, run, deadline=_deadline())

    assert result.status is ReproStatus.REPRODUCED and result.deterministic
    assert run.calls == ["make && ./runtest --loops 1"] * 2
    assert PLAN.verify_loops(result.deterministic) == 10


def test_one_failure_then_a_pass_is_a_flake():
    result = reproduce(PLAN, _runner(FAIL, PASS), deadline=_deadline())

    assert result.reproduced and not result.deterministic
    assert PLAN.verify_loops(result.deterministic) == 40


def test_a_flaky_failure_reproduces_under_loops():
    run = _runner(PASS, FAIL)
    result = reproduce(PLAN, run, deadline=_deadline())

    assert result.status is ReproStatus.REPRODUCED and not result.deterministic
    assert run.calls[1] == f"make && ./runtest --loops {REPRO_LOOPS}"
    assert f"failed within {REPRO_LOOPS} loops" in result.describe()
    assert "intermittently" in result.describe()


def test_a_timing_flake_reproduces_with_every_core_busy():
    run = _runner(PASS, PASS, FAIL)
    result = reproduce(PLAN, run, deadline=_deadline())

    assert result.reproduced
    assert "while :; do :; done" in run.calls[2] and run.calls[1] in run.calls[2]
    assert "with every core busy" in result.describe()


def test_a_failure_that_never_shows_is_not_reproduced():
    result = reproduce(PLAN, _runner(PASS, PASS, PASS), deadline=_deadline())

    assert result.status is ReproStatus.NOT_REPRODUCED
    assert result.all_attempts_passed
    assert result.describe().startswith("Did not reproduce")


def test_a_build_break_is_not_a_reproduction():
    result = reproduce(PLAN, _runner(BROKEN), deadline=_deadline())

    assert result.status is ReproStatus.UNRUNNABLE
    assert "tclsh" in result.output


def test_looped_attempts_stop_at_the_deadline():
    run = _runner(PASS)
    result = reproduce(PLAN, run, deadline=_deadline(60))

    assert result.status is ReproStatus.NOT_REPRODUCED
    assert not result.all_attempts_passed
    assert len(run.calls) == 1 and "no time left" in result.describe()


def test_a_loop_that_does_not_finish_is_an_incomplete_nonreproduction():
    timed_out = TestRun(False, "timed out after 60s", timed_out=True)
    result = reproduce(PLAN, _runner(PASS, timed_out, PASS), deadline=_deadline())

    assert result.status is ReproStatus.NOT_REPRODUCED
    assert not result.all_attempts_passed
    assert "20 loops did not finish" in result.describe()


HARNESS_EXCEPTION = """\
# Failed opening Unix socket: unix socket path too long (289), must be under 104
[exception]: Executing test client: error flushing "sock820d74890": broken pipe.
    invoked from within
"send_data_packet $::test_server_fd done "$__testname""
"""


def test_a_harness_exception_outside_the_test_is_not_a_reproduction():
    report = failure_report(HARNESS_EXCEPTION)

    assert not shows_failure_of(report, (TEST,))
    run = _runner(TestRun(False, HARNESS_EXCEPTION, report))
    assert reproduce(PLAN, run, deadline=_deadline()).status is ReproStatus.UNRUNNABLE


def test_an_exception_inside_the_test_is_a_reproduction():
    output = (
        "[exception]: Executing test client: I/O error reading reply.\n"
        f'"test "{TEST.name}" {{\n'
    )
    assert shows_failure_of(failure_report(output), (TEST,))


def test_only_the_planned_tests_failing_counts():
    other = "[err]: Some other test in tests/unit/type/incr.tcl"
    mine = f"[err]: {TEST.name} in {TEST.file}"
    assert not shows_failure_of(failure_report(other), (TEST,))
    assert shows_failure_of(failure_report(mine), (TEST,))
    # A client timeout outside any named test is a timeout in its file, not
    # any other test failing there.
    whole_file = (FailedTest("", TEST.file),)
    assert not shows_failure_of(failure_report(other), whole_file)
    assert shows_failure_of(failure_report(f"[TIMEOUT]: pid:4242 in {TEST.file}"), whole_file)
    assert shows_failure_of("*** [err]: Valgrind error in tests/unit/type/incr.tcl", (TEST,))
    assert not shows_failure_of(failure_report("make: *** [Makefile:6: all] Error 2"), (TEST,))


def test_a_pass_that_ran_no_failing_test_is_not_evidence():
    ok_other = "[ok]: Some other test (3 ms)"

    result = reproduce(PLAN, _runner(TestRun(True, "", ok_other)), deadline=_deadline())

    assert result.status is ReproStatus.UNRUNNABLE
    assert "without running the failing test" in result.describe()
    assert ran_tests(f"[ok]: {TEST.name} (12 ms)", (TEST,))
    assert ran_tests(ok_other, (FailedTest("", TEST.file),))
    assert not ran_tests("", (FailedTest("", TEST.file),))
    # A client timeout in the list does not excuse a named test from running.
    mixed = (TEST, FailedTest("", "tests/unit/other.tcl"))
    assert not ran_tests(ok_other, mixed)
    assert ran_tests(f"{ok_other}\n[ok]: {TEST.name} (12 ms)", mixed)


def test_runtest_reports_a_hang_well_inside_the_reproduction_budget(checkout):
    from scripts.ci_fix.engine import REPRO_BUDGET_S

    valgrind = JobCommands(build="make valgrind", runtest="./runtest --valgrind --timeout 2400")
    for job in (None, valgrind):
        test = plan_for(checkout, (TEST,), scheduled=True, job=job).test
        watchdog = int(test.rsplit("--timeout ", 1)[1].split()[0])  # runtest takes the last
        assert watchdog < REPRO_BUDGET_S - 5 * 60, job


def test_a_renamed_or_sibling_test_is_not_the_failing_test():
    renamed = f"[ok]: {TEST.name} (deflaked) (12 ms)"
    sibling = f"[err]: {TEST.name} again in {TEST.file}"

    assert not ran_tests(renamed, (TEST,))
    assert not shows_failure_of(sibling, (TEST,))
    assert shows_failure_of(f"[TIMEOUT]: {TEST.name} in {TEST.file}", (TEST,))


def test_a_branch_without_fastfail_runs_its_loops_to_the_end(checkout):
    (Path(checkout) / "tests" / "test_helper.tcl").write_text("} elseif {$opt eq {--loops}} {\n", encoding="utf-8")

    plan = plan_for(checkout, (TEST,), scheduled=False)

    assert "--fastfail" not in plan.test
    assert "no --fastfail" in " ".join(plan.notes)


def test_a_rejected_option_is_reported_as_the_reason():
    broken = TestRun(False, "Wrong argument: --fastfail\n")

    result = reproduce(PLAN, _runner(broken), deadline=_deadline())

    assert result.status is ReproStatus.UNRUNNABLE
    assert "Wrong argument: --fastfail" in result.describe()


def test_an_ok_line_from_an_earlier_loop_does_not_make_a_harness_exception_a_reproduction():
    report = failure_report(f"[ok]: {TEST.name} (12 ms)\n" + HARNESS_EXCEPTION)

    assert not shows_failure_of(report, (TEST,))


def test_a_flake_seen_only_under_load_is_verified_under_load():
    result = reproduce(PLAN, _runner(PASS, PASS, FAIL), deadline=_deadline())

    assert result.under_load
    assert "while :; do :; done" in PLAN.command(40, under_load=result.under_load)


def test_a_test_name_with_braces_is_quoted_not_formatted(checkout):
    braced = FailedTest("HSET with {tag} key and {loops}", TEST.file)

    command = plan_for(checkout, (braced,), scheduled=False).command(7)

    assert "--only 'HSET with {tag} key and {loops}'" in command
    assert "--loops 7" in command


def test_a_file_with_run_solo_loops_one_client_at_a_time(checkout):
    (Path(checkout) / "tests" / "unit" / "type").mkdir(parents=True)
    (Path(checkout) / TEST.file).write_text('run_solo {defrag} {\n    test "x" {}\n}\n', encoding="utf-8")

    plan = plan_for(checkout, (TEST,), scheduled=False)

    assert plan.test.endswith("--clients 1")
    assert "run_solo" in " ".join(plan.notes)


def test_valgrind_verification_stays_short_even_after_a_deterministic_failure():
    valgrind = ReproPlan(build="make valgrind", test=f"./runtest --loops {LOOPS}", valgrind=True, tests=(TEST,))

    assert valgrind.verify_loops(True) == valgrind.verify_loops(False) == 4


# --- inside the failed job: its own commands ------------------------------------

JOB = JobCommands(
    build="export CC='clang'\nmake all-with-unit-tests SANITIZER=address \\\n  USE_LIBBACKTRACE=yes",
    runtest="./runtest --accurate --verbose --dump-logs --tls",
    moduleapi="CFLAGS='-Werror' ./runtest-moduleapi --verbose --dump-logs",
)


def test_in_the_job_its_own_build_and_runtest_run_the_failing_test(checkout):
    plan = plan_for(checkout, (TEST,), scheduled=True, job=JOB)

    # One line: every report quotes the command, and a code span holds no newline.
    assert plan.build == (
        "export CC='clang' && make -j\"$(nproc)\" all-with-unit-tests SANITIZER=address USE_LIBBACKTRACE=yes"
    )
    assert plan.test.replace(LOOPS, "20") == (
        "./runtest --accurate --verbose --dump-logs --tls --single unit/type/incr "
        "--only 'INCREX BYFLOAT overflow preserves existing TTL' --fastfail --loops 20 --timeout 600"
    )
    assert not plan.valgrind and plan.notes == ()


def test_a_jobs_multi_line_build_becomes_one_command(checkout):
    """A report quotes the command in a code span, which holds no newline."""
    job = JobCommands(
        build="apt-get update && apt-get install -y make gcc-13\nupdate-alternatives --install x\nmake CC=gcc",
        runtest="./runtest --dump-logs",
    )

    plan = plan_for(checkout, (TEST,), scheduled=True, job=job)

    assert plan.build == (
        "apt-get update && apt-get install -y make gcc-13 && update-alternatives --install x"
        " && make -j\"$(nproc)\" CC=gcc"
    )
    assert "\n" not in plan.command(3) and "\n" not in plan.command(3, under_load=True)


def test_in_the_job_a_module_api_test_runs_through_runtest_moduleapi(checkout):
    module = FailedTest("blocking", "tests/unit/moduleapi/blockonkeys.tcl")

    plan = plan_for(checkout, (module, TEST), scheduled=True, job=JOB)

    assert plan.test.startswith("CFLAGS='-Werror' ./runtest-moduleapi --verbose --dump-logs --single unit/moduleapi/")
    assert plan.tests == (module,)


def test_in_a_valgrind_job_its_own_options_are_kept(checkout):
    job = JobCommands(build="make valgrind", runtest="./runtest --valgrind --no-latency --clients 1 --dump-logs")
    (Path(checkout) / "tests" / "unit" / "type").mkdir(parents=True)
    (Path(checkout) / TEST.file).write_text("run_solo {x} {}\n", encoding="utf-8")

    plan = plan_for(checkout, (TEST,), scheduled=True, job=job)

    assert plan.valgrind and plan.test.count("--clients") == 1


@pytest.mark.parametrize(("result", "status"), [
    (TestRun(passed=False, output="hyperloglog.c:1224:5: error: unknown type name 'bool'"), ReproStatus.REPRODUCED),
    (TestRun(passed=True, output="built"), ReproStatus.NOT_REPRODUCED),
    (TestRun(passed=False, output="killed", timed_out=True), ReproStatus.UNRUNNABLE),
])
def test_a_build_break_is_reproduced_by_the_jobs_build_alone_and_once(result, status):
    plan = build_plan(JobCommands(build="export CC=gcc\nmake MALLOC=libc", runtest="./runtest"))
    commands = []

    reproduction = reproduce(plan, lambda command, _timeout: commands.append(command) or result,
                             deadline=time.monotonic() + 60)

    assert commands == ['export CC=gcc && make -j"$(nproc)" MALLOC=libc']  # no runtest, no loops
    assert reproduction.status is status
    assert plan.verify_loops(True) == 1 and plan.command(10) == commands[0]
    assert plan.ran(TestRun(passed=True, output="")) and not plan.tests


_FILE_WITH_SETUP_BETWEEN_TESTS = """\
proc helper {} { return helped }
set shared 1

start_server {tags {"repl"}} {
    test {Earlier test that starts replication} { r slaveof 127.0.0.1 1 }
    wait_for_sync r
}

start_server {tags {"repl"}} {
    foreach mode {disabled swapdb} {
        test "[string toupper flushdb] during sync replays (load=$mode)" { helper }
    }
}
"""


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="needs tclsh")
def test_a_test_that_only_cannot_reach_runs_alone_in_its_own_block(checkout):
    """--only skips the earlier test but still runs wait_for_sync, which then never syncs (#5301)."""
    path = Path(checkout) / "tests" / "integration" / "replication.tcl"
    path.parent.mkdir()
    path.write_text(_FILE_WITH_SETUP_BETWEEN_TESTS, encoding="utf-8")
    test = FailedTest("FLUSHDB during sync replays (load=swapdb)", "tests/integration/replication.tcl")

    plan = plan_for(checkout, (test,), scheduled=False,
                    job=JobCommands(build="true", runtest="./runtest --valgrind --clients 1"))
    isolated = plan.fallback

    assert isolated is not None and isolated.valgrind
    assert isolated.tests == (FailedTest(test.name, "tests/integration/replication.ci-fix-isolated.tcl"),)
    # Generate the copy the way a run does, then stop before runtest.
    command = isolated.command(1).replace("./runtest", "cat tests/integration/replication.ci-fix-isolated.tcl; true")
    out = subprocess.run(["bash", "-c", command], cwd=checkout,
                         capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    assert "proc helper" in out.stdout and "set shared 1" in out.stdout
    assert "foreach mode" in out.stdout and "wait_for_sync" not in out.stdout
    assert "--single integration/replication.ci-fix-isolated --fastfail --loops 1" in isolated.command(1)
    assert not (path.parent / "replication.ci-fix-isolated.tcl").exists()  # removed after the run


def test_the_fallbacks_are_the_tests_own_block_then_its_whole_file_once(checkout):
    (Path(checkout) / "tests" / "unit").mkdir()
    (Path(checkout) / "tests" / "unit" / "a.tcl").write_text("", encoding="utf-8")
    (Path(checkout) / "tests" / "unit" / "b.tcl").write_text("", encoding="utf-8")
    one = FailedTest("first named test", "tests/unit/a.tcl")
    other = FailedTest("second named test", "tests/unit/b.tcl")

    isolated = plan_for(checkout, (one,), scheduled=False).fallback
    assert ".ci-fix-isolated" in isolated.test
    whole = isolated.fallback
    assert whole.single_run and whole.fallback is None and whole.tests == (one,)
    assert whole.command(20).endswith("--single unit/a --loops 1 --timeout 600")
    assert "--only" not in whole.command(20) and whole.verify_loops(False) == 1

    # Two files have no single block: their whole files run, both at once.
    both = plan_for(checkout, (one, other), scheduled=False).fallback
    assert both.single_run and "--single unit/a --single unit/b --loops 1" in both.command(1)


@pytest.mark.parametrize(("result", "status"), [
    (TestRun(passed=False, output="", report="[err]: first named test in tests/unit/a.tcl"), ReproStatus.REPRODUCED),
    (TestRun(passed=True, output="", report="[ok]: first named test (2 ms)"), ReproStatus.NOT_REPRODUCED),
    (TestRun(passed=True, output="", report=""), ReproStatus.UNRUNNABLE),  # never ran the test
])
def test_a_whole_file_run_happens_once(checkout, result, status):
    (Path(checkout) / "tests" / "unit").mkdir(exist_ok=True)
    (Path(checkout) / "tests" / "unit" / "a.tcl").write_text("", encoding="utf-8")
    one = FailedTest("first named test", "tests/unit/a.tcl")
    whole = plan_for(checkout, (one,), scheduled=False).fallback.fallback
    commands = []

    reproduction = reproduce(whole, lambda command, _t: commands.append(command) or result,
                             deadline=time.monotonic() + 60)

    assert len(commands) == 1 and reproduction.status is status


def test_a_plan_that_already_runs_whole_files_has_no_fallback(checkout):
    """A client timeout outside any test runs its whole file; rerunning it would repeat it."""
    (Path(checkout) / "tests" / "unit").mkdir(exist_ok=True)
    (Path(checkout) / "tests" / "unit" / "a.tcl").write_text("", encoding="utf-8")
    timed_out = FailedTest("", "tests/unit/a.tcl")

    assert plan_for(checkout, (timed_out,), scheduled=False).fallback is None


@pytest.mark.skipif(shutil.which("tclsh") is None, reason="needs tclsh")
def test_top_level_setup_in_an_if_or_foreach_is_kept_in_the_copy(checkout):
    """Review of #147: dropping setup made the copy fail where the file passes."""
    from scripts.ci_fix.reproduce import _ISOLATE_TCL

    path = Path(checkout) / "tests" / "unit" / "setup.tcl"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        "if {1} { set ::needed 42 }\n"
        "foreach v {a b} { set ::seen($v) 1 }\n"
        "start_server {} { test {other test} { r ping } }\n"
        "start_server {} { test {target} { assert_equal 42 $::needed } }\n",
        encoding="utf-8",
    )
    out = subprocess.run(["tclsh", "/dev/stdin", "tests/unit/setup.tcl", "target"], input=_ISOLATE_TCL,
                         cwd=checkout, capture_output=True, text=True, check=True).stdout

    assert "set ::needed 42" in out and "set ::seen" in out
    assert "target" in out and "other test" not in out


def test_a_fallback_counts_only_the_named_tests_own_failure(checkout):
    """Review of #147: a sibling's valgrind report is not the target failing."""
    (Path(checkout) / "tests" / "unit").mkdir(exist_ok=True)
    (Path(checkout) / "tests" / "unit" / "a.tcl").write_text("", encoding="utf-8")
    target = FailedTest("first named test", "tests/unit/a.tcl")
    plan = plan_for(checkout, (target,), scheduled=False)
    sibling = TestRun(passed=False, output="", report=failure_report("[err]: Valgrind error: Invalid read\n"))

    assert plan.failed(sibling)  # --only ran the target alone: the report is its
    for fallback in (plan.fallback, plan.fallback.fallback):
        assert fallback.attributed and not fallback.failed(sibling)
    own = TestRun(passed=False, output="", report=f"[err]: first named test in {plan.fallback.tests[0].file}")
    assert plan.fallback.failed(own)


def test_an_isolated_failure_must_be_the_one_ci_saw(checkout):
    from scripts.ci_fix.reproduce import matches_ci

    (Path(checkout) / "tests" / "unit").mkdir(exist_ok=True)
    (Path(checkout) / "tests" / "unit" / "a.tcl").write_text("", encoding="utf-8")
    isolated = plan_for(checkout, (FailedTest("first named test", "tests/unit/a.tcl"),), scheduled=False).fallback
    output = "[err]: first named test in tests/unit/a.ci-fix-isolated.tcl\nExpected '5' to be equal to '7'\n"

    assert matches_ci(isolated, output, "*** [err]: first named test in tests/unit/a.tcl\nExpected '3' to be equal to '9'")
    assert not matches_ci(isolated, output, "*** [err]: first named test in tests/unit/a.tcl\nReplica didn't sync")
    assert matches_ci(isolated.fallback, output, "")  # a whole-file run is the file itself
