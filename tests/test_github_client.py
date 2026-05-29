"""Tests for GitHub API retry helpers."""

from __future__ import annotations

import pytest
from github.GithubException import GithubException

from scripts.common.github_client import retry_github_call


def test_retry_github_call_retries_retryable_errors(monkeypatch) -> None:
    calls = {"count": 0}

    def operation() -> str:
        calls["count"] += 1
        if calls["count"] < 3:
            raise GithubException(429, {"message": "rate limit"})
        return "ok"

    monkeypatch.setattr("scripts.common.github_client.time.sleep", lambda _seconds: None)
    monkeypatch.setattr("scripts.common.github_client.random.uniform", lambda _a, _b: 0.0)

    result = retry_github_call(operation, retries=3, description="test call")

    assert result == "ok"
    assert calls["count"] == 3


@pytest.mark.parametrize("status", [401, 404])
def test_retry_github_call_does_not_retry_permanent_errors(status: int) -> None:
    calls = {"count": 0}

    def operation() -> str:
        calls["count"] += 1
        raise GithubException(status, {"message": "permanent"})

    with pytest.raises(GithubException):
        retry_github_call(operation, retries=3, description="test call")

    assert calls["count"] == 1
