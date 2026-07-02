from __future__ import annotations

import argparse

import pytest

from scripts.common.polling import (
    PollLoopError,
    add_poll_loop_args,
    env_int,
    env_seconds,
    format_poll_results,
    nonnegative_int,
    run_poll_loop,
    run_poll_loop_from_args,
)


def test_nonnegative_int_for_argparse():
    assert nonnegative_int("0") == 0
    assert nonnegative_int("3") == 3
    with pytest.raises(argparse.ArgumentTypeError):
        nonnegative_int("-1")
    with pytest.raises(argparse.ArgumentTypeError):
        nonnegative_int("bad")


def test_env_int_defaults_and_bounds():
    env = {
        "EMPTY": "",
        "BAD": "garbage",
        "LOW": "-5",
        "HIGH": "999",
        "OK": "30",
    }
    assert env_int("MISSING", 7, environ=env) == 7
    assert env_int("EMPTY", 7, environ=env) == 7
    assert env_int("BAD", 7, environ=env) == 7
    assert env_int("LOW", 7, minimum=1, environ=env) == 1
    assert env_int("HIGH", 7, maximum=60, environ=env) == 60
    assert env_int("OK", 7, environ=env) == 30


def test_env_seconds_uses_same_bounds():
    assert env_seconds("INTERVAL", 5, minimum=1, maximum=10, environ={"INTERVAL": "30"}) == 10


def test_run_poll_loop_single_when_not_configured():
    calls = []

    def poll():
        calls.append("poll")
        return len(calls)

    assert run_poll_loop(poll) == [1]
    assert calls == ["poll"]


def test_run_poll_loop_single_propagates_exception_when_not_configured():
    def poll():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_poll_loop(poll)


def test_add_poll_loop_args_and_run_from_args():
    parser = argparse.ArgumentParser()
    add_poll_loop_args(parser)
    args = parser.parse_args(["--poll-interval-seconds", "5", "--poll-duration-seconds", "10"])
    assert args.poll_interval_seconds == 5
    assert args.poll_duration_seconds == 10

    calls = []

    def poll():
        calls.append("poll")
        return len(calls)

    assert run_poll_loop_from_args(
        poll,
        args,
        clock=lambda: 0,
        sleep=lambda _seconds: None,
    ) == [1, 2, 3]


def test_run_poll_loop_runs_on_interval_until_deadline():
    now = 0.0
    starts = []
    sleeps = []

    def clock():
        return now

    def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    def poll():
        starts.append(now)
        return int(now)

    result = run_poll_loop(
        poll, interval_seconds=10, duration_seconds=25,
        clock=clock, sleep=sleep,
    )
    assert result == [0, 10, 20]
    assert starts == [0, 10, 20]
    assert sleeps == [10, 10]


def test_run_poll_loop_continues_after_iteration_exception():
    now = 0.0
    attempts = []
    sleeps = []

    def clock():
        return now

    def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    def poll():
        attempts.append(now)
        if len(attempts) == 1:
            raise RuntimeError("transient")
        return int(now)

    with pytest.raises(PollLoopError) as raised:
        run_poll_loop(
            poll, interval_seconds=10, duration_seconds=25,
            clock=clock, sleep=sleep,
        )
    assert str(raised.value.last_error) == "transient"
    assert raised.value.results == [10, 20]
    assert attempts == [0, 10, 20]
    assert sleeps == [10, 10]


def test_run_poll_loop_reraises_when_every_iteration_fails():
    now = 0.0
    attempts = []

    def clock():
        return now

    def sleep(seconds):
        nonlocal now
        now += seconds

    def poll():
        attempts.append(now)
        raise RuntimeError(f"boom at {now:g}")

    with pytest.raises(PollLoopError) as raised:
        run_poll_loop(
            poll, interval_seconds=10, duration_seconds=25,
            clock=clock, sleep=sleep,
        )
    assert str(raised.value.last_error) == "boom at 20"
    assert raised.value.results == []
    assert attempts == [0, 10, 20]


def test_run_poll_loop_skips_missed_intervals_instead_of_bursting():
    now = 0.0
    starts = []

    def clock():
        return now

    def sleep(seconds):
        nonlocal now
        now += seconds

    def poll():
        nonlocal now
        starts.append(now)
        now += 13
        return int(now)

    run_poll_loop(
        poll, interval_seconds=10, duration_seconds=25,
        clock=clock, sleep=sleep,
    )
    assert starts == [0, 20]


def test_format_poll_results_preserves_one_shot_shape():
    assert format_poll_results([{"action": "swept"}]) == {"action": "swept"}
    assert format_poll_results([{"n": 1}, {"n": 2}]) == {"runs": [{"n": 1}, {"n": 2}]}
