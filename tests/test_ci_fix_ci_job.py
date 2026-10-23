"""The CI job a fix is prepared in, read from the workflow that ran it."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.ci_fix import ci_job
from scripts.ci_fix.ci_job import Checkout, JobCommands, JobSpec, Step, Unsupported

# Valkey's own workflows, as of unstable 0704408 (2026-10).
FIXTURES = Path(__file__).parent / "fixtures" / "valkey_workflows"
SHA = "f" * 40


def _resolve(source: str, name: str, *, scheduled: bool = True) -> JobSpec:
    return ci_job.resolve(source, name, scheduled=scheduled, repository="valkey-io/valkey", sha=SHA,
                          ref="refs/heads/unstable")


# --- expressions --------------------------------------------------------------

_GITHUB = {"event_name": "schedule", "repository": "valkey-io/valkey"}


def _context(**extra):
    github = ci_job._Known({**_GITHUB, "event": ci_job._Known({"inputs": {}})})
    return {"github": github, "inputs": {}, "matrix": {"compiler": "gcc", "server": {"version": "7.2"}},
            "env": {"CC": "gcc"}, **extra}


@pytest.mark.parametrize(("expression", "expected"), [
    ("'it''s'", "it's"),
    ("42", 42),
    ("true", True),
    ("null", None),
    ("github.event_name", "schedule"),
    ("matrix.server.version", "7.2"),
    ("matrix.missing", None),
    ("matrix.server.version.deeper", None),
    ("env.CC", "gcc"),
    ("inputs.skiptests", None),
    ("github.event.inputs.test_args", None),
    # Strings compare without case; other types as numbers.
    ("github.event_name == 'SCHEDULE'", True),
    ("github.event_name != 'pull_request'", True),
    ("null == ''", True),
    ("1 == '1'", True),
    ("true == 1", True),
    ("'a' == 1", False),
    # && and || return an operand, as GitHub does.
    ("github.event_name != 'pull_request' && '--accurate' || ''", "--accurate"),
    ("github.event_name == 'pull_request' && '--accurate' || ''", ""),
    ("inputs.use_repo || github.event.inputs.use_repo || github.repository", "valkey-io/valkey"),
    ("'' && 'x'", ""),
    ("!contains(github.event.inputs.skiptests, 'valkey')", True),
    ("!(true && false)", True),
    ("!true == false", True),
    ("contains('Valgrind,SANITIZER', 'sanitizer')", True),
    ("contains(fromJSON('[\"gcc\", \"clang\"]'), 'GCC')", True),
    ("startsWith('ubuntu-latest', 'Ubuntu')", True),
    ("endsWith('ubuntu-latest', 'arm')", False),
    ("fromJSON(inputs.valgrind_test && '[\"targeted\"]' || '[\"unit\", \"cluster\"]')", ["unit", "cluster"]),
    ("always() && success()", True),
])
def test_an_expression_has_githubs_value(expression, expected):
    assert ci_job.evaluate(expression, _context()) == expected


@pytest.mark.parametrize("expression", [
    "secrets.TOKEN", "steps.build.outputs.diff", "needs.a.result", "runner.os", "vars.REGION",
    "github.token", "github.event.pull_request.base.ref",  # a property the run's context does not know
    "hashFiles('x')", "failure()", "toJSON(matrix)",  # a function the fix job does not evaluate
    "matrix.compiler[0]", "1 < 2", "'unterminated", "(true", "contains('a')",  # outside the grammar
])
def test_an_expression_outside_what_a_fix_job_follows_is_unsupported(expression):
    with pytest.raises(Unsupported):
        ci_job.evaluate(expression, _context())


def test_a_branch_github_skips_is_never_evaluated():
    expression = "github.event_name == 'pull_request_target' && github.event.pull_request.head.sha || 'x'"

    assert ci_job.evaluate(expression, _context()) == "x"


def test_text_renders_each_expression_as_github_writes_it():
    text = "./runtest ${{ github.event_name != 'pull_request' && '--accurate' || '' }} ${{github.event.inputs.x}}"

    assert ci_job.render(text, _context()) == "./runtest --accurate "
    assert ci_job.render("${{ 3 }} ${{ true }} ${{ '}}' }}", _context()) == "3 true }}"
    assert ci_job.value("${{ fromJSON('[1]') }}", _context()) == [1]
    assert ci_job.value(" x ${{ matrix.compiler }}", _context()) == " x gcc"


@pytest.mark.parametrize(("condition", "holds"), [
    (True, True), ("matrix.missing", False), ("${{ matrix.compiler == 'gcc' }}", True),
    ("github.event_name == 'workflow_dispatch' || github.event_name == 'workflow_call'", False),
])
def test_a_steps_condition_is_evaluated(condition, holds):
    assert ci_job.condition(condition, _context()) is holds


# --- a job, its matrix entry and its display name -----------------------------

DAILY = """\
name: Daily
on:
  schedule:
    - cron: "0 0 * * *"
jobs:
  test-ubuntu-jemalloc:
    runs-on: ubuntu-latest
    if: github.event_name == 'schedule' && !contains(github.event.inputs.skipjobs, 'ubuntu')
    timeout-minutes: 1440
    steps:
      - name: prep
        if: github.event_name == 'workflow_dispatch' || github.event_name == 'workflow_call'
        run: echo "GITHUB_HEAD_REF=${{github.event.inputs.use_git_ref}}" >> $GITHUB_ENV
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd
        with:
          repository: ${{ github.event.inputs.use_repo || github.repository }}
          ref: ${{ github.event.inputs.use_git_ref || github.ref }}
      - name: Install libbacktrace
        uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd
        with:
          repository: ianlancetaylor/libbacktrace
          ref: b9e40069c0b47a722286b94eb5231f7f05c08713
          path: libbacktrace
      - run: cd libbacktrace && ./configure && make && sudo make install
      - name: make
        run: make
      - name: test
        if: true && !contains(github.event.inputs.skiptests, 'valkey')
        run: ./runtest ${{ github.event_name != 'pull_request' && '--accurate' || '' }} --dump-logs ${{github.event.inputs.test_args}}
      - name: backward compatibility tests with OSS Redis 7.0
        run: ./runtest --tags compatible-redis
      - name: Upload test failures
        if: always()
        uses: ./.github/actions/upload-test-failures
  notify-about-job-results:
    needs: [test-ubuntu-jemalloc]
    runs-on: ubuntu-latest
    steps:
      - run: echo done
"""


def test_a_job_resolves_to_its_runner_checkouts_setup_and_commands():
    spec = _resolve(DAILY, "test-ubuntu-jemalloc")

    assert spec == JobSpec(
        job_id="test-ubuntu-jemalloc",
        name="test-ubuntu-jemalloc",
        runs_on="ubuntu-latest",
        container="",
        timeout_minutes=ci_job.MAX_JOB_MINUTES,
        env={},
        # The step for dispatched runs does not run; the job's own checkout
        # is the fix job's; the tests, and what follows them, are the agent's.
        steps=(Step("cd libbacktrace && ./configure && make && sudo make install",
                    "cd libbacktrace && ./configure && make && sudo make install"),
               Step("make", "make")),
        checkout=Checkout("ianlancetaylor/libbacktrace", "b9e40069c0b47a722286b94eb5231f7f05c08713", "libbacktrace"),
        commands=JobCommands(build="make", runtest="./runtest --accurate --dump-logs"),
    )


def test_a_pull_requests_run_tests_without_accurate():
    assert _resolve(DAILY, "test-ubuntu-jemalloc", scheduled=False).commands.runtest == "./runtest --dump-logs"


def test_a_name_no_job_runs_as_is_unsupported():
    with pytest.raises(Unsupported):
        _resolve(DAILY, "test-something-new")
    with pytest.raises(Unsupported):  # two jobs run as it
        _resolve(DAILY.replace("  notify-about-job-results:\n", "  other:\n    name: test-ubuntu-jemalloc\n"),
                 "test-ubuntu-jemalloc")


_MATRIX = DAILY.replace(
    "    timeout-minutes: 1440\n",
    "    timeout-minutes: 30\n    strategy:\n      matrix:\n        compiler: [gcc, clang]\n"
    "    env:\n      CC: ${{ matrix.compiler }}\n",
)


def test_a_matrix_entry_is_chosen_by_the_name_github_gives_it():
    spec = _resolve(_MATRIX, "test-ubuntu-jemalloc (clang)")

    # The fix needs its own time, however short the job's tests were.
    assert (spec.env, spec.timeout_minutes) == ({"CC": "clang"}, ci_job.MIN_JOB_MINUTES)
    # Its environment reaches the rebuilds, which run with a clean one.
    assert spec.commands.build == "export CC='clang'\nmake"
    with pytest.raises(Unsupported):
        _resolve(_MATRIX, "test-ubuntu-jemalloc")  # which entry is unknown


def test_a_name_that_uses_the_matrix_is_the_entrys_whole_name():
    source = _MATRIX.replace("    runs-on: ubuntu-latest\n", "    name: sanitizer-${{ matrix.compiler }}\n"
                             "    runs-on: ubuntu-latest\n", 1)

    assert _resolve(source, "sanitizer-gcc").env == {"CC": "gcc"}


def test_a_matrix_from_an_expression_has_one_entry_per_value():
    source = _MATRIX.replace("compiler: [gcc, clang]", "compiler: ${{ fromJSON('[\"unit\", \"cluster\"]') }}")

    assert _resolve(source, "test-ubuntu-jemalloc (cluster)").env == {"CC": "cluster"}


_NAMED_MATRIX = DAILY.replace(
    "  test-ubuntu-jemalloc:\n    runs-on: ubuntu-latest\n",
    "  test-rpm-distros:\n    name: ${{ matrix.name }}\n    runs-on: ubuntu-latest\n"
    "    container: ${{ matrix.container }}\n    strategy:\n      matrix:\n        include:\n"
    "          - {name: test-almalinux8, container: 'almalinux:8', install_epel: true}\n"
    "          - {name: test-fedoralatest, container: 'fedora:latest'}\n",
).replace("      - name: make\n", "      - name: Install EPEL\n        if: matrix.install_epel\n"
          "        run: dnf -y install epel-release\n      - name: make\n")


def test_an_include_matrix_has_one_entry_per_item_with_its_own_conditions():
    alma, fedora = _resolve(_NAMED_MATRIX, "test-almalinux8"), _resolve(_NAMED_MATRIX, "test-fedoralatest")

    assert (alma.job_id, alma.container, fedora.container) == ("test-rpm-distros", "almalinux:8", "fedora:latest")
    assert "Install EPEL" in [step.name for step in alma.steps]
    assert "Install EPEL" not in [step.name for step in fedora.steps]


@pytest.mark.parametrize("matrix", [
    "        compiler: [gcc, clang]\n        os: [a, b]\n",
    "        compiler: [gcc, clang]\n        include:\n          - {compiler: tcc}\n",
    "        compiler: [gcc, clang]\n        exclude:\n          - {compiler: gcc}\n",
    "        compiler: ${{ fromJSON('\"gcc\"') }}\n",
])
def test_a_matrix_that_cannot_be_enumerated_is_unsupported(matrix):
    source = _MATRIX.replace("        compiler: [gcc, clang]\n", matrix)

    with pytest.raises(Unsupported):
        _resolve(source, "test-ubuntu-jemalloc (gcc)")


# --- jobs no fix job can repeat -----------------------------------------------


@pytest.mark.parametrize("change", [
    ("    runs-on: ubuntu-latest\n", "    runs-on: windows-latest\n"),
    ("    runs-on: ubuntu-latest\n", "    runs-on: [self-hosted]\n"),
    ("    runs-on: ubuntu-latest\n", "    runs-on: macos-latest\n    container: alpine:latest\n"),
    ("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    container:\n      image: alpine\n"
                                   "      options: --privileged\n"),
    ("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    services:\n      redis: {image: redis}\n"),
    ("    runs-on: ubuntu-latest\n", "    runs-on: ubuntu-latest\n    defaults:\n      run: {shell: sh}\n"),
    # A VM, or no make step or runtest command to repeat.
    ("        run: make\n", "        uses: cross-platform-actions/action@v1\n        with:\n          run: make\n"),
    ("      - name: make\n        run: make\n", ""),
    ("      - name: make\n        run: make\n", "      - name: make\n        run: make\n"
                                               "      - name: make\n        run: make all\n"),
    ("        run: ./runtest ${{", "        run: ./other-tests ${{"),
    # A setup step it cannot replay exactly.
    ("      - name: make\n", "      - uses: actions/setup-go@v5\n      - name: make\n"),
    ("      - name: make\n", "      - name: x\n        shell: pwsh\n        run: echo\n      - name: make\n"),
    ("      - name: make\n", "      - name: x\n        working-directory: src\n        run: echo\n      - name: make\n"),
    ("      - name: make\n", "      - name: x\n        continue-on-error: true\n        run: echo\n      - name: make\n"),
    ("      - name: make\n", "      - name: x\n        env:\n          T: ${{ secrets.T }}\n        run: echo\n"
                             "      - name: make\n"),
    ("      - name: make\n", "      - name: x\n        if: failure()\n        run: echo\n      - name: make\n"),
    ("          path: libbacktrace\n", "          path: libbacktrace\n          token: ${{ secrets.T }}\n"),
    ("      - name: make\n", "      - uses: actions/checkout@v6\n        with: {repository: a/b, path: b}\n"
                             "      - name: make\n"),
    ("      - name: make\n        run: make\n", "      - name: make\n        if: false\n        run: make\n"),
])
def test_a_job_a_fix_job_cannot_repeat_exactly_is_unsupported(change):
    with pytest.raises(Unsupported):
        _resolve(DAILY.replace(*change), "test-ubuntu-jemalloc")


def test_what_follows_the_tests_does_not_matter():
    source = DAILY.replace("      - name: Upload test failures\n",
                           "      - uses: actions/setup-go@v5\n        with:\n          go: ${{ secrets.X }}\n"
                           "      - name: Upload test failures\n")

    assert _resolve(source, "test-ubuntu-jemalloc").commands.build == "make"


def test_a_script_between_the_tests_that_is_not_a_test_is_unsupported():
    """It is setup the fix would not replay (the compatibility tests' own is skipped with them)."""
    source = DAILY.replace("      - name: backward compatibility tests with OSS Redis 7.0\n",
                           "      - name: install tclx\n        run: sudo apt-get install tclx\n"
                           "      - name: backward compatibility tests with OSS Redis 7.0\n")

    with pytest.raises(Unsupported, match="runs between the job's tests"):
        _resolve(source, "test-ubuntu-jemalloc")


def test_a_script_after_the_last_test_does_not_matter():
    """Daily's large-memory jobs print a memory summary after their tests; it is not setup."""
    source = DAILY.replace("      - name: Upload test failures\n",
                           "      - name: Memory usage summary\n        if: always()\n        run: free -h\n"
                           "      - name: Upload test failures\n")

    assert _resolve(source, "test-ubuntu-jemalloc").commands.build == "make"


def test_the_legacy_unit_tests_are_a_test_step_and_the_make_test_unit_line_is_kept():
    """8.0 runs ``./src/valkey-server test all`` between its test steps; unstable runs ``make test-unit``."""
    source = DAILY.replace(
        "      - name: backward compatibility tests with OSS Redis 7.0\n",
        "      - name: legacy unit tests\n        run: ./src/valkey-server test all --accurate\n"
        "      - name: unittest\n        run: |\n          if [ -f ./src/unit/valkey-unit-gtests ]; then\n"
        "            make test-unit accurate=1 ${{github.event.inputs.test_args}}\n"
        "          elif [ -f ./src/valkey-unit-tests ]; then\n            ./src/valkey-unit-tests --accurate\n          fi\n"
        "      - name: backward compatibility tests with OSS Redis 7.0\n",
    )

    spec = _resolve(source, "test-ubuntu-jemalloc")

    assert spec.commands.unittest == "make test-unit accurate=1"
    assert spec.commands.runtest == "./runtest --accurate --dump-logs"


def test_a_shell_expansion_is_dropped_with_the_option_it_is_the_value_of():
    source = DAILY.replace(" --dump-logs ${{github.event.inputs.test_args}}\n",
                           ' --clients $(nproc) --dump-logs "${extra[@]}"\n')

    assert _resolve(source, "test-ubuntu-jemalloc").commands.runtest == "./runtest --accurate --dump-logs"


def test_a_step_shell_and_env_are_kept_and_rendered():
    source = DAILY.replace(
        "      - name: make\n",
        "      - name: Log\n        shell: bash\n        env:\n          WHO: ${{ github.job }}\n"
        "        run: echo \"$WHO ${{ env.WHO }}\"\n      - name: make\n",
    )

    step = next(step for step in _resolve(source, "test-ubuntu-jemalloc").steps if step.name == "Log")

    assert step == Step("Log", 'echo "$WHO test-ubuntu-jemalloc"', shell="bash", env={"WHO": "test-ubuntu-jemalloc"})


# --- the setup a test step does, and the job's commands ------------------------

CI = """\
name: CI
on: [push, pull_request]
jobs:
  test-ubuntu-latest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd
      - name: make
        run: make SERVER_CFLAGS='-Werror'
      - name: test
        run: |
          sudo apt-get install tcl8.6 tclx
          ./runtest --verbose --other-server-path tests/tmp/old/bin/valkey-server --tags -slow \\
            --failures-output test-failures/valkey.json --dump-logs
      - name: module api test
        run: CFLAGS='-Werror' ./runtest-moduleapi --verbose
"""


def test_a_test_step_that_also_installs_keeps_that_setup():
    spec = _resolve(CI, "test-ubuntu-latest", scheduled=False)

    assert [step.name for step in spec.steps] == ["make", "test setup"]
    assert spec.steps[-1].run == "sudo apt-get install tcl8.6 tclx"
    # The agent runs its own tests: not the job's selection, and not against
    # another server the fix job never downloads.
    assert spec.commands == JobCommands(
        build="make SERVER_CFLAGS='-Werror'",
        runtest="./runtest --verbose --tags -slow --dump-logs",
        moduleapi="CFLAGS='-Werror' ./runtest-moduleapi --verbose",
    )


def test_the_fix_rebuilds_without_the_system_setup_the_job_already_did():
    source = DAILY.replace(
        "      - name: make\n        run: make\n",
        "      - name: make\n        run: |\n"
        "          apt-get update && apt-get install -y make gcc-13\n"
        "          update-alternatives --install /usr/bin/gcc gcc /usr/bin/gcc-13 100\n"
        "          dnf -y install gcc make\n"
        "          apk add build-base\n"
        "          sudo apt-get install -y libssl-dev\n"
        "          make CC=gcc # the build\n",
        1,
    )

    spec = _resolve(source, "test-ubuntu-jemalloc")

    # The fix rebuilds as an unprivileged account; these ran as root already.
    assert spec.commands.build == "make CC=gcc"
    assert "apk add build-base" in spec.steps[-1].run  # the job's own setup runs them


def test_a_dropped_install_takes_its_continuation_lines_with_it():
    source = DAILY.replace(
        "      - name: make\n        run: make\n",
        "      - name: make\n        run: |\n"
        "          sudo apt-get install -y \\\n"
        "            pkg-config libssl-dev\n"
        "          make BUILD_TLS=yes \\\n"
        "            SERVER_CFLAGS='-Werror'\n",
        1,
    )

    spec = _resolve(source, "test-ubuntu-jemalloc")

    assert spec.commands.build == "make BUILD_TLS=yes SERVER_CFLAGS='-Werror'"


def test_a_strategy_that_is_not_a_mapping_is_unsupported_not_a_crash():
    with pytest.raises(Unsupported):
        _resolve(DAILY.replace("    timeout-minutes: 1440\n", "    timeout-minutes: 1440\n    strategy: 3\n", 1),
                 "test-ubuntu-jemalloc")


def test_a_shell_variable_the_test_command_uses_is_not_setup():
    source = CI.replace("          sudo apt-get install tcl8.6 tclx\n",
                        "          shard=unit\n          sudo apt-get install tcl8.6 tclx\n")

    assert _resolve(source, "test-ubuntu-latest").steps[-1].run == "sudo apt-get install tcl8.6 tclx"


def _bash_words(args: str) -> str:
    """How bash itself splits ``args`` into words, one ``<word>`` each."""
    result = subprocess.run(["bash", "-c", f"set -f; printf '<%s>' {args}"],
                            capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else f"error: {result.stderr.strip()}"


@pytest.mark.parametrize("args", [
    "32bit # the script below has no argument",  # a comment Valkey's daily.yml has
    "X=1\t# after a tab", "a;#b", "a #b 'c # d'", "x ##y", "#",
    "CFLAGS=-DX#Y", "a#",  # inside a word
    "${#list[@]}",  # parameter length
    "CFLAGS='-DA#B' X=1", 'CFLAGS="#1" # note', "'it'\\''s' # q",  # quoted
    'A="x \\" # y"', 'x="a\\"#b"', "a\\ #b", "a\\#b # c",  # escaped
])
def test_a_comment_is_dropped_exactly_where_bash_starts_one(args):
    assert _bash_words(ci_job._without_comment(args)) == _bash_words(args)


@pytest.mark.parametrize("line", [
    "export TCLSH=tclsh8.6",  # the next step's fresh shell would not see it
    "cd tests/tmp",
    "TCLSH=tclsh8.6",
    "sudo apt-get install $PACKAGES",
    "sudo apt-get install tclx && rm -rf tests/tmp",
    "if true; then sudo apt-get install tclx; fi",
])
def test_only_an_install_or_a_fixture_build_is_hoisted_out_of_a_test_step(line):
    assert ci_job._setup_of({"name": "test", "run": f"{line}\n./runtest --verbose"}) is None


def test_only_the_lines_before_the_test_command_are_its_setup():
    step = {"name": "test", "run": "sudo apt-get install tclx\n./runtest --verbose\nsudo apt-get install valgrind"}

    assert ci_job._setup_of(step) == {"name": "test setup", "run": "sudo apt-get install tclx"}


def test_a_fixture_build_with_a_comment_is_hoisted_whole():
    step = {"name": "module api test", "if": "x",
            "run": "make -C tests/modules 32bit # build them ahead of time\n./runtest-moduleapi --verbose"}

    assert ci_job._setup_of(step) == {"name": "module api test setup",
                                      "run": "make -C tests/modules 32bit # build them ahead of time"}


def test_a_skipped_test_steps_setup_is_not_hoisted():
    source = CI.replace("      - name: test\n", "      - name: test\n        if: false\n")

    assert [step.name for step in _resolve(source, "test-ubuntu-latest").steps] == ["make"]


# --- the setup driver ---------------------------------------------------------


def _spec(*steps: Step, env: dict[str, str] | None = None) -> JobSpec:
    return JobSpec(job_id="j", name="j", runs_on="ubuntu-latest", container="", timeout_minutes=10,
                   env=env or {}, steps=steps, checkout=None, commands=JobCommands("make", "./runtest"))


def _drive(tmp_path: Path, spec: JobSpec, *, path: str = "") -> subprocess.CompletedProcess[str]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    script = tmp_path / "setup.sh"
    script.write_text(ci_job.setup_script(spec), encoding="utf-8")
    env = {**os.environ, "GITHUB_WORKSPACE": str(workspace), "GITHUB_ENV": str(tmp_path / "github_env"),
           "GITHUB_PATH": str(tmp_path / "github_path"), "TMPDIR": str(tmp_path)}
    if path:
        env["PATH"] = path
    return subprocess.run(["sh", str(script)], cwd=tmp_path, env=env, capture_output=True, text=True, check=False)


def test_the_setup_runs_each_step_from_the_workspace_with_what_earlier_steps_exported(tmp_path):
    (tmp_path / "bin").mkdir()
    spec = _spec(
        Step("export", "\n".join([
            'echo "FROM_FIRST=one=1" >> "$GITHUB_ENV"',
            "printf 'MULTI<<EOF\\nline 1\\nline 2\\nEOF\\n' >> \"$GITHUB_ENV\"",
            f'echo "{tmp_path / "bin"}" >> "$GITHUB_PATH"',
            "printf '#!/bin/sh\\necho tool ran\\n' > \"" + str(tmp_path / "bin" / "tool") + '"',
            "chmod +x \"" + str(tmp_path / "bin" / "tool") + '"',
        ])),
        Step("read", 'echo "$FROM_FIRST|$MULTI|$JOB|$STEP|$(pwd)"; tool', env={"STEP": "it's a step"}),
        Step("own env only", 'echo "STEP=${STEP:-unset}"'),
        env={"JOB": "job env"},
    )

    result = _drive(tmp_path, spec)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "one=1|line 1", f"line 2|job env|it's a step|{tmp_path / 'workspace'}", "tool ran", "STEP=unset",
    ]
    # The workflow's own later steps, which hold credentials, never do: the
    # job's build ran as the workflow user, and could have prepended a path.
    assert not (tmp_path / "github_env").exists()
    assert not (tmp_path / "github_path").exists()


def test_the_setup_stops_at_the_first_failing_step_and_names_it(tmp_path):
    spec = _spec(Step("first", "echo first"), Step("Build libbacktrace", "false\necho after"),
                 Step("never", "echo never"))

    result = _drive(tmp_path, spec)

    assert result.returncode == 1
    assert result.stdout.splitlines() == [
        "first", "::error::The job's setup step 'Build libbacktrace' failed with exit status 1",
    ]


@pytest.mark.parametrize(("shell", "passes"), [
    ("", True),  # bash -e: a failure inside a pipeline does not stop it
    ("bash", False),  # bash -o pipefail
    ("sh", True),
])
def test_a_pipeline_fails_a_step_as_its_shell_says(tmp_path, shell, passes):
    assert (_drive(tmp_path, _spec(Step("pipe", "false | true", shell=shell))).returncode == 0) is passes


# A bash that says how it was called, then runs the step's script with sh.
_FAKE_BASH = """#!/bin/sh
called="bash"
while [ "$#" -gt 1 ]; do called="$called $1"; shift; done
echo "$called"
exec sh -e "$1"
"""


@pytest.mark.parametrize(("shell", "bash", "called"), [
    ("", True, ["bash -e"]),
    ("", False, []),  # GitHub's default is sh -e without bash
    ("bash", True, ["bash --noprofile --norc -eo pipefail"]),
    ("sh", True, []),
])
def test_each_step_runs_in_the_shell_github_gives_it(tmp_path, shell, bash, called):
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("sh", "cat", "mktemp"):
        (tools / tool).symlink_to(shutil.which(tool))
    if bash:
        (tools / "bash").write_text(_FAKE_BASH, encoding="utf-8")
        (tools / "bash").chmod(0o755)

    result = _drive(tmp_path, _spec(Step("which", "echo ran", shell=shell)), path=str(tools))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [*called, "ran"]


def test_a_steps_script_cannot_end_its_heredoc_early(tmp_path):
    result = _drive(tmp_path, _spec(Step("tricky", "echo 'CI_FIX_STEP'\ncat <<'CI_FIX_STEP'\nin\nCI_FIX_STEP")))

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["CI_FIX_STEP", "in"]


# --- Valkey's own workflows ---------------------------------------------------

_DAILY = (FIXTURES / "daily.yml").read_text(encoding="utf-8")
_CI = (FIXTURES / "ci.yml").read_text(encoding="utf-8")
_LIBBACKTRACE = Checkout("ianlancetaylor/libbacktrace", "b9e40069c0b47a722286b94eb5231f7f05c08713", "libbacktrace")


def test_valkeys_ubuntu_job_resolves():
    spec = _resolve(_DAILY, "test-ubuntu-jemalloc")

    assert (spec.runs_on, spec.container, spec.checkout) == ("ubuntu-latest", "", _LIBBACKTRACE)
    assert [step.name for step in spec.steps][:4] == [
        "cd libbacktrace && ./configure && make && sudo make install", "Install gtest", "make", "testprep",
    ]
    assert spec.commands.build == "make all-with-unit-tests SERVER_CFLAGS='-Werror' USE_LIBBACKTRACE=yes"
    assert spec.commands.runtest == "./runtest --accurate --verbose --dump-logs"
    assert spec.commands.moduleapi == "CFLAGS='-Werror' ./runtest-moduleapi --verbose --dump-logs"
    assert spec.commands.unittest == "make test-unit accurate=1"


def test_valkeys_sanitizer_entry_resolves_with_its_compiler():
    spec = _resolve(_DAILY, "test-sanitizer-address (gcc)")

    assert spec.env == {"CC": "gcc"}
    assert spec.commands.build.startswith("export CC='gcc'\nmake all-with-unit-tests OPT=-O3 SANITIZER=address")


def test_valkeys_valgrind_shard_runs_its_own_job_instance():
    spec = _resolve(_DAILY, "test-valgrind-test (unit)")

    assert spec.commands.build == "make valgrind SERVER_CFLAGS='-Werror' BUILD_ZSTD=yes USE_LIBBACKTRACE=yes"
    assert spec.commands.runtest == (
        "./runtest --valgrind --no-latency --verbose --clients 1 --timeout 2400 --dump-logs"
    )


def test_valkeys_rpm_distro_entry_resolves_to_its_container():
    spec = _resolve(_DAILY, "test-fedoralatest-jemalloc")

    assert (spec.job_id, spec.container) == ("test-rpm-distros-jemalloc", "fedora:latest")
    assert [step.name for step in spec.steps] == ["Build libbacktrace", "make", "testprep"]  # no EPEL on Fedora


def test_valkeys_alpine_and_macos_jobs_resolve():
    alpine, macos = _resolve(_DAILY, "test-alpine-jemalloc"), _resolve(_DAILY, "test-macos-latest")

    assert (alpine.runs_on, alpine.container) == ("ubuntu-latest", "alpine:latest")
    assert (macos.runs_on, macos.container, macos.checkout) == ("macos-latest", "", _LIBBACKTRACE)


def test_valkeys_pull_request_job_resolves():
    spec = _resolve(_CI, "test-ubuntu-latest", scheduled=False)

    assert spec.commands.runtest == "./runtest --verbose --tags -slow --dump-logs"
    assert spec.checkout == _LIBBACKTRACE


def test_valkeys_freebsd_job_runs_in_a_vm_and_falls_back():
    with pytest.raises(Unsupported, match="VM"):
        _resolve(_DAILY, "test-freebsd")


def _every_job_name(source: str) -> list[str]:
    """The display name of every job instance ``source`` defines."""
    import yaml

    names = []
    for job_id, job in yaml.safe_load(source)["jobs"].items():
        context = {"github": ci_job._Known({"event_name": "schedule", "repository": "valkey-io/valkey", "job": job_id,
                                            "ref": "refs/heads/unstable", "sha": SHA, "event": ci_job._Known({"inputs": {}})}),
                   "inputs": {}}
        for entry in ci_job._matrix_entries(job, context):
            names.append(ci_job._display_name(job_id, job, entry, context))
    return names


def test_every_valkey_job_resolves_as_expected():
    """Each job of the fixtures, and why the ones a fix job cannot host cannot be: a change here is a change in coverage."""
    expected = {"test-freebsd": "the job runs its tests in a VM"}
    outcomes = {}
    for source, scheduled in ((_DAILY, True), (_CI, False)):
        for name in _every_job_name(source):
            try:
                _resolve(source, name, scheduled=scheduled)
            except Unsupported as exc:
                outcomes[name] = str(exc)

    assert outcomes == expected


def test_every_resolved_valkey_job_has_a_setup_script(tmp_path):
    for name in ("test-ubuntu-jemalloc", "test-fedoralatest-jemalloc", "test-macos-latest"):
        script = tmp_path / "setup.sh"
        script.write_text(ci_job.setup_script(_resolve(_DAILY, name)), encoding="utf-8")

        assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0, name
