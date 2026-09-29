"""Stable per-failure identity shared with the Test Failure Detector.

The dashboard names a failure by a display string, ``"<test> in <file>"`` for
an attributed Tcl failure or a job-level label such as
``"test-valgrind-test (unit): unattributed failure"`` for anything the log
parser could not pin to a test. The detector names the same failure by the
``(test_name, test_file)`` pair from the ``all-test-failures`` artifact and
hashes it with :func:`scripts.common.incidents.compute_fingerprint` under
:data:`scripts.test_failure_detector.issue_renderer.MARKER_NAMESPACE`.

This module maps a display name onto that pair and applies the same hash, so
a scorecard row and a ``[TEST-FAILURE]`` issue for the same failure carry the
same fingerprint and either side can look the other up.

Failures the artifact never carries (spawn timeouts, valgrind/sanitizer job
failures with no attributed test; ``tests/test_helper.tcl`` skips them in
``write_test_failures``) get ``test_file="unknown"``, the same fallback that
file uses for a line it cannot parse, so a future detector fed from log-parser
output lands on the identical key.
"""

from __future__ import annotations

import re
from typing import Tuple

from scripts.common.incidents import compute_fingerprint
from scripts.test_failure_detector.issue_renderer import MARKER_NAMESPACE

UNKNOWN_FILE = "unknown"

# "<test name> in tests/<path>.tcl" -- the shape both the Tcl runner's summary
# lines and the dashboard's display names use for an attributed failure.
_ATTRIBUTED_RE = re.compile(r"^(?P<name>.+?) in (?P<file>tests/\S+\.tcl)$")


def split_display_name(display_name: str) -> Tuple[str, str]:
    """Return ``(test_name, test_file)`` for a dashboard display name.

    Attributed failures split on the trailing ``in tests/...tcl``; anything
    else is the whole string with ``test_file=UNKNOWN_FILE``.
    """
    m = _ATTRIBUTED_RE.match(display_name.strip())
    if m:
        return m.group("name"), m.group("file")
    return display_name.strip(), UNKNOWN_FILE


def fingerprint_for_pair(test_name: str, test_file: str) -> str:
    """The detector's fingerprint for a ``(test_name, test_file)`` pair."""
    return compute_fingerprint(namespace=(MARKER_NAMESPACE, test_name, test_file), shapes=())


def fingerprint_for_name(display_name: str) -> str:
    """The detector's fingerprint for a dashboard display name."""
    return fingerprint_for_pair(*split_display_name(display_name))
