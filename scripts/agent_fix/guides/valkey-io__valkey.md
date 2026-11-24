# Making a change to Valkey

Valkey's own guidelines for code and tests are at the end of this file, as the
default branch has them (`AGENTS.md`, `DEVELOPMENT_GUIDE.md`, `tests/README.md`
and `src/unit/README.md`). Read them: they are the authority on style and
conventions.

Before you use a `wait_for_*` or `verify_*` helper or a runtest option, read
`tests/support/util.tcl`, `tests/support/test.tcl` and `tests/test_helper.tcl`
in your checkout for what it actually does, and whether this branch has it.
Helpers and options differ between release lines, so use only what this
branch's `tests/` defines.

What follows first is the bot's own policy, not a summary of those files.

## Writing the test

- Assert an exact value only while the state it reads cannot move. While the
  server is still doing work in the background, assert a bound
  (`assert_range`, `assert_morethan`) or wait for the state first.
- Fix a race by holding the moving part still, not by waiting longer:
  `pause_process` and `resume_process` stop and resume a server, and
  `tests/support/util.tcl` has more.
- Give every wait a failure message that prints the state it was waiting
  for, in `wait_for_condition`'s `else` clause, so the next failure explains
  itself.
- Check a result by reading the data back. That the server did not crash is
  not a check.
- Use an existing helper before writing a new one.

## Comments

A comment says what the code does or why, briefly. No narration of the fix,
no history ("previously", "now"), and no PR or issue numbers.

## Scope

- Keep the change minimal and easy to backport: release branches take the
  same change.
- Do not reorder or rename tests; other tests in a file may depend on state
  that earlier ones leave behind.
- A tag that skips a test is not a fix for a race. Skipping is right only
  where the test cannot apply (the platform lacks the feature, or a tool the
  test needs is missing), with the reason next to the skip.
- Do not make a test or the harness ignore what it reports (an error, a
  warning, a log line, a sanitizer or valgrind finding) or accept an outcome
  it rejected. Find why the report happens and remove that: a harness that
  signals a server during its exit-time leak check is fixed by waiting for
  the server to exit, not by ignoring the warning that results. A test may
  accept more outcomes, or a looser limit, only when every outcome it would
  then accept is correct server behaviour, and the fix says why.
- Do not change `.github/` unless your policy says a CI change is allowed.
- Do not patch `deps/`. It is vendored from other projects (libvalkey,
  jemalloc, Lua and others), and Valkey takes a change there only as an
  update from that project: say which project needs the fix and what the fix
  is. Change Valkey's own code or test only where it is wrong whatever the
  dependency does.

## Running a test

The verification section of your briefing names the exact command the bot
checks your change with, with the job's own build; prefer it. In general:

```sh
make -j"$(getconf _NPROCESSORS_ONLN)"
./runtest --single unit/type/incr --only 'exact test name' --loops 20 --fastfail --dump-logs
```

`--only` is an exact name match (a leading `/` makes it a regexp). With
several clients (the default) loop copies run in parallel, and a `run_solo`
block runs inline, so loop a file with `run_solo` with `--clients 1`. Add
`--accurate` for what scheduled CI runs, `--valgrind --clients 1` after
`make valgrind`, `--tls` after `make BUILD_TLS=yes && ./utils/gen-test-certs.sh`.

A C unit test is a Google Test in `src/unit/test_*.cpp`, not a Tcl test:

```sh
make -j"$(getconf _NPROCESSORS_ONLN)" all-with-unit-tests
./src/unit/valkey-unit-gtests --gtest_filter='Suite.Test' --gtest_repeat=20
```

Release branches before the Google Test harness run their unit tests through
`./src/valkey-unit-tests` (its `--help` lists how to select one); older ones
through `./src/valkey-server test <name>`.
