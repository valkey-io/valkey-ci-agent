"""Shared helpers for pollers that can run once or on an in-run cadence."""

from __future__ import annotations

import argparse
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any, TypeVar

T = TypeVar("T")


def nonnegative_int(value: str) -> int:
    """Argparse type for integer knobs where 0 is a meaningful value."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer >= 0") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return parsed


def env_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Parse an environment variable as a bounded integer.

    Invalid or empty values fall back to ``default``. This keeps scheduled
    workflows from failing open because of a typo in an optional tuning knob.
    """
    source = os.environ if environ is None else environ
    raw = source.get(name, "").strip()
    if not raw:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError:
            value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


def env_seconds(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Parse an environment variable as bounded seconds."""
    return env_int(
        name,
        default,
        minimum=minimum,
        maximum=maximum,
        environ=environ,
    )


def run_poll_loop(
    poll_once: Callable[[], T],
    *,
    interval_seconds: int = 0,
    duration_seconds: int = 0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> list[T]:
    """Run ``poll_once`` once, or repeatedly on a fixed in-process cadence.

    ``interval_seconds <= 0`` or ``duration_seconds <= 0`` preserves one-shot
    behavior. In loop mode, the first poll starts immediately and later polls
    target ``start + N * interval`` until the deadline. If a poll takes longer
    than an interval, missed starts are skipped instead of burst-catching up.
    """
    if interval_seconds <= 0 or duration_seconds <= 0:
        return [poll_once()]

    start = clock()
    deadline = start + duration_seconds
    next_start = start
    results: list[T] = []
    iteration = 0

    while True:
        now = clock()
        if results and now > deadline:
            break
        if now < next_start:
            sleep(next_start - now)
            now = clock()
            if results and now > deadline:
                break

        iteration += 1
        if logger is not None:
            logger.info("Starting poll iteration %d", iteration)
        results.append(poll_once())

        next_start += interval_seconds
        if next_start > deadline:
            break

        now = clock()
        if now > next_start:
            missed = int((now - next_start) // interval_seconds) + 1
            next_start += missed * interval_seconds
            if logger is not None:
                logger.warning("Poll iteration overran cadence; skipped %d interval(s)", missed)
            if next_start > deadline:
                break

    return results


def add_poll_loop_args(parser: argparse.ArgumentParser) -> None:
    """Add standard sustained-poll options to a poller CLI."""
    parser.add_argument(
        "--poll-interval-seconds",
        type=nonnegative_int,
        default=0,
        help="Run repeatedly at this interval (0 = one-shot)",
    )
    parser.add_argument(
        "--poll-duration-seconds",
        type=nonnegative_int,
        default=0,
        help="Maximum sustained polling duration (0 = one-shot)",
    )


def run_poll_loop_from_args(
    poll_once: Callable[[], T],
    args: argparse.Namespace,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    logger: logging.Logger | None = None,
) -> list[T]:
    """Run a poll loop using CLI args added by ``add_poll_loop_args``."""
    return run_poll_loop(
        poll_once,
        interval_seconds=args.poll_interval_seconds,
        duration_seconds=args.poll_duration_seconds,
        clock=clock,
        sleep=sleep,
        logger=logger,
    )


def format_poll_results(results: list[Any]) -> Any:
    """Preserve old one-shot JSON while wrapping sustained runs."""
    return results[0] if len(results) == 1 else {"runs": results}
