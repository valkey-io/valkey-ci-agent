"""Shared test fixtures."""

import socket

import pytest
import urllib3.util.connection


@pytest.fixture(autouse=True)
def block_unstubbed_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make accidental live API calls fail instead of being hidden by fail-soft CLIs."""

    def denied(*args: object, **kwargs: object) -> None:
        raise AssertionError("tests must stub external network access")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(urllib3.util.connection, "create_connection", denied)


@pytest.fixture
def temp_db_path(tmp_path):
    """Path to a fresh SQLite file for ``scripts.ci_health.store`` tests."""
    return str(tmp_path / "ci_health.db")
