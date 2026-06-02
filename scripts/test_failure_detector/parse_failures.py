"""Parse and deduplicate test failures from the consolidated artifact"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class JobReference:
    """A reference to a specific CI job where a test failed."""

    job: str
    suite: str
    url: str = ""


@dataclass
class UniqueFailure:
    """A deduplicated test failure that may appear across multiple jobs."""

    test_name: str
    test_file: str
    error: str = ""
    jobs: list[JobReference] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return f"{self.test_name} in {self.test_file}"


def parse_and_deduplicate(
    all_failures: dict[str, Any],
    

) -> list[UniqueFailure]:
    """Parse the all-test-failures JSON and deduplicate by test name + file.

    Args:
        all_failures: The parsed all-test-failures.json content.
            Structure: {job_name: {suite_name: [{test_name, test_file, error}]}}
        job_urls: Mapping of job name -> HTML URL for CI links.

    Returns:
        List of UniqueFailure objects, deduplicated across jobs.
    """
    grouped: dict[str, UniqueFailure] = {}

    for job_name, suites in all_failures.items():
        if not isinstance(suites, dict):
            logger.warning("Unexpected format for job %r: expected dict, got %s", job_name, type(suites).__name__)
            continue

        for suite_name, entries in suites.items():
            if not isinstance(entries, list):
                logger.warning(
                    "Unexpected format for %s/%s: expected list, got %s",
                    job_name, suite_name, type(entries).__name__,
                )
                continue

            for entry in entries:
                if not isinstance(entry, dict):
                    continue

                test_name = entry.get("test_name", "")
                test_file = entry.get("test_file", "")
                if not test_name or not test_file:
                    logger.debug("Skipping entry with missing test_name or test_file: %s", entry)
                    continue

                key = f"{test_name} in {test_file}"

                if key not in grouped:
                    grouped[key] = UniqueFailure(
                        test_name=test_name,
                        test_file=test_file,
                        error=entry.get("error", ""),
                    )

                # Deduplicate: skip if this job is already recorded
                failure = grouped[key]
                if not any(j.job == job_name for j in failure.jobs):
                    failure.jobs.append(
                        JobReference(
                            job=job_name,
                            suite=suite_name,
                            url=job_urls.get(job_name, ""),
                        )
                    )
                    logger.debug("%s in %s/%s", key, job_name, suite_name)

    unique_failures = list(grouped.values())
    logger.info("Total unique failures: %d", len(unique_failures))
    return unique_failures
