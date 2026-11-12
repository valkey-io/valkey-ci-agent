# Fixing a failing Valkey test

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

## First decide what failed

- Test bug: the test assumes a timing, an ordering, or a state the server does
  not guarantee. Fix the test, with the polling helpers the repository's docs
  describe rather than a fixed `after N`.
- Harness bug: the test framework itself races or mishandles a server
  (`tests/support/`, `tests/helpers/`, `tests/test_helper.tcl`). Fix it there.
- Environment: a port clash, an out-of-disk runner, a missing tool. Refuse;
  that is not a code fix.
- Product bug: the server broke something it promises. Fix `src/`. Never make
  a test pass by weakening what it checks.

Look for the test or harness fix first, and change a C file only when the fix
has to be there. Of eleven fixes people merged for failures like these, eight
changed only tests or the harness and two changed `src/`.

A C change is necessary when the log shows the server breaking a promise a
user could see (a crash, a wrong or lost reply, lost data, a state it never
leaves) and no correct test could pass without it. A server that is briefly
in a state the test did not expect, and gets out of it on its own, is not
that: fix the test to wait for the state it depends on, and say in
`validation` what the server did, so a maintainer can decide whether it needs
a change of its own.

When a C change is necessary, make the smallest one, the way earlier fixes to
that code were made: the test's history and the existing fixes you are given
show them. Say why no test change would be right.

Code under `deps/` is vendored from other projects (libvalkey, jemalloc, Lua
and others), and Valkey takes a change there only as an update from that
project. Do not patch `deps/`: say which project needs the fix and what the
fix is. Change Valkey's own code or test only where it is wrong whatever the
dependency does.

A test that fails once in many runs is still a bug. Find the race in the log
(`--dump-logs` prints each server's log after a failure) before editing.
Valgrind and sanitizer builds are many times slower; scale a wait with
`$::valgrind` rather than hard-coding a longer one everywhere.

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
- A tag that skips a test is not a fix for a race.
- Do not change `.github/` unless your policy says a CI change is allowed.

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
