"""Stable per-failure identity shared with the Test Failure Detector.

The detector files two kinds of issue, each keyed by a fingerprint from
:func:`scripts.common.incidents.compute_fingerprint` under
:data:`scripts.test_failure_detector.issue_renderer.MARKER_NAMESPACE`:

* ``[TEST-FAILURE]``, keyed by the ``(test_name, test_file)`` pair runtest
  prints as ``<name> in tests/<file>.tcl``
  (:func:`scripts.test_failure_detector.issue_renderer.fingerprint_for`);
* ``[JOB-FAILURE]``, keyed by its cause: the runtest summary line
  ``[<status>]: <data>`` that names no test, such as an exception, a
  valgrind or sanitizer report's first error and frames, or the shutdown
  leak check. The job and step are not part of the key, so one cause failing
  in several jobs is one issue
  (:func:`scripts.test_failure_detector.issue_renderer.job_fingerprint_for`).

The dashboard names a failure by a display string. An attributed one has the
``<name> in tests/<file>.tcl`` shape; this module splits it with the
detector's own :func:`scripts.common.valkey_ci.test_in_file` and hashes the
pair through the detector's own function, so a scorecard row and its issue
share an id by construction rather than by a copied formula.

Every other display name, a job-level bucket (``"<job>: unattributed
failure"``, ``"<job>: server crash"``) or an exception the parser could name
only by message or file, gets a ci_health-local id under its own namespace.
It is stable for deep links and can never collide with a detector id, but it
is not one: the ``[JOB-FAILURE]`` key is the cause line, which a display name
does not carry. Looking up that issue takes the cause itself
(:func:`fingerprint_for_cause`), which the sync layer takes from the
detector's log reader, ``scripts.test_failure_detector.job_failures``, not
from a display name.
"""

from __future__ import annotations

from typing import Optional, Tuple

from scripts.common.incidents import compute_fingerprint
from scripts.common.valkey_ci import test_in_file
from scripts.test_failure_detector.issue_renderer import MARKER_NAMESPACE, fingerprint_for, job_fingerprint_for
from scripts.test_failure_detector.job_failures import JobFailure
from scripts.test_failure_detector.parse_failures import UniqueFailure

# Namespace element for ids only the dashboard knows. The detector's ids use
# either the bare pair or "job", so this cannot collide with them.
LOCAL_KIND = "ci_health"


def split_display_name(display_name: str) -> Optional[Tuple[str, str]]:
    """Return ``(test_name, test_file)`` for an attributed display name, else None.

    The split is the detector's: it takes the trailing ``in tests/...tcl``
    and folds a test client's state (``pid:NNN``, ``port NNN``) into its
    stable name, so both sides land on the same pair.
    """
    return test_in_file(display_name.strip())


def fingerprint_for_test(test_name: str, test_file: str) -> str:
    """The detector's ``[TEST-FAILURE]`` fingerprint for a ``(test_name, test_file)`` pair."""
    return fingerprint_for(UniqueFailure(test_name=test_name, test_file=test_file))


def fingerprint_for_cause(cause: str) -> str:
    """The detector's ``[JOB-FAILURE]`` fingerprint for a runtest summary line.

    ``cause`` is the ``[<status>]: <data>`` line as the detector's
    ``job_failures`` reader builds it. The job and step are not part of the
    detector's key (see ``JobFailure.identity``), so none are needed here.
    """
    return job_fingerprint_for(JobFailure(job="", url="", summary=(cause,)))


def fingerprint_for_name(display_name: str) -> str:
    """A stable id for a dashboard display name.

    An attributed name gets the detector's ``[TEST-FAILURE]`` fingerprint;
    any other name gets a ci_health-local id (see the module docstring).
    """
    pair = split_display_name(display_name)
    if pair is not None:
        return fingerprint_for_test(*pair)
    return compute_fingerprint(namespace=(MARKER_NAMESPACE, LOCAL_KIND, display_name.strip()), shapes=())
