"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def allow_upstream_publish_in_tests(request: pytest.FixtureRequest) -> None:
    """No-op fixture kept for the ``disable_publish_autouse`` marker.

    The legacy publish guard has been removed; this fixture is retained so
    tests that explicitly opt out via the marker continue to work without
    requiring per-test changes.
    """
    if "disable_publish_autouse" in request.keywords:
        return
