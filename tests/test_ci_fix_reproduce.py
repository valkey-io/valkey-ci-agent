from __future__ import annotations

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


def test_a_jobs_shorter_runtest_watchdog_is_kept_and_a_longer_one_is_not(checkout):
    """runtest takes the last --timeout; a hang must still be reported inside the reproduction budget."""
    shorter = JobCommands(build="make", runtest="./runtest --timeout 300 --dump-logs")
    longer = JobCommands(build="make valgrind", runtest="./runtest --valgrind --clients 1 --timeout 2400 --dump-logs")

    kept, lowered = plan_for(checkout, (TEST,), scheduled=True, job=shorter), plan_for(checkout, (TEST,), scheduled=True, job=longer)
    assert kept.test.endswith("--timeout 300") and kept.notes == ()
    assert lowered.test.endswith("--timeout 600")
    # So that a [TIMEOUT] at 600s is not read as the failure the job saw.
    assert lowered.notes == ("The job's --timeout 2400 was lowered to 600s for the reproduction: "
                             "a test that times out at 600s may be slow here, not hung.",)


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
    assert result.describe().startswith("Did not reproduce")


def test_a_build_break_is_not_a_reproduction():
    result = reproduce(PLAN, _runner(BROKEN), deadline=_deadline())

    assert result.status is ReproStatus.UNRUNNABLE
    assert "tclsh" in result.output


def test_looped_attempts_stop_at_the_deadline():
    run = _runner(PASS)
    result = reproduce(PLAN, run, deadline=_deadline(60))

    assert result.status is ReproStatus.NOT_REPRODUCED
    assert len(run.calls) == 1 and "no time left" in result.describe()




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
