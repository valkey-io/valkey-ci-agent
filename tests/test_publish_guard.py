"""Tests for scripts/publish_guard.py - the registry-backed write guard.

The guard blocks writes to configured protected repos unless
VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH=1. Entry points configure this set from
repos.yml and the guard fails closed if used before configuration.
"""

from __future__ import annotations

import pytest

from scripts.common import publish_guard
from scripts.common.publish_guard import check_publish_allowed, configure_publish_guard

# All tests here opt out of the autouse fixture in conftest — the fixture
# sets env vars that would otherwise mask the guard's default behavior.
pytestmark = pytest.mark.disable_publish_autouse


@pytest.fixture
def clean_env(monkeypatch):
    """Clear the upstream-opt-in env var for a clean test."""
    monkeypatch.delenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", raising=False)
    return monkeypatch



def test_fork_write_allowed_by_default(clean_env):
    check_publish_allowed("sarthakaggarwal97/valkey", action="create_pull")


def test_any_non_upstream_repo_allowed(clean_env):
    check_publish_allowed("some-org/some-repo", action="create_issue")


def test_fork_write_still_allowed_with_opt_in_set(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", "1")
    check_publish_allowed("sarthakaggarwal97/valkey", action="create_pull")



def test_upstream_valkey_write_blocked_by_default(clean_env):
    with pytest.raises(RuntimeError, match="ALLOW_VALKEY_IO_PUBLISH"):
        check_publish_allowed("valkey-io/valkey", action="create_pull")


def test_upstream_fuzzer_write_allowed_by_default(clean_env):
    """Fuzzer upstream is intentionally allowed without opt-in."""
    check_publish_allowed("valkey-io/valkey-fuzzer", action="create_issue")


def test_configured_module_repo_blocked_by_default(clean_env):
    configure_publish_guard({"valkey-io/valkey-bloom"})
    with pytest.raises(RuntimeError, match="ALLOW_VALKEY_IO_PUBLISH"):
        check_publish_allowed("valkey-io/valkey-bloom", action="create_pull")


def test_unconfigured_guard_fails_closed(monkeypatch):
    monkeypatch.setattr(publish_guard, "_configured", False)
    with pytest.raises(RuntimeError, match="not configured"):
        check_publish_allowed("some-org/some-repo", action="create_pull")



def test_upstream_write_allowed_with_opt_in(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", "1")
    check_publish_allowed("valkey-io/valkey", action="create_pull")


def test_opt_in_case_insensitive(clean_env):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", "TRUE")
    check_publish_allowed("valkey-io/valkey", action="create_pull")


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_values_accepted(clean_env, value):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", value)
    check_publish_allowed("valkey-io/valkey", action="create_pull")


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_falsy_values_reject(clean_env, value):
    clean_env.setenv("VALKEY_CI_AGENT_ALLOW_VALKEY_IO_PUBLISH", value)
    with pytest.raises(RuntimeError):
        check_publish_allowed("valkey-io/valkey", action="create_pull")



def test_error_message_includes_action_and_repo(clean_env):
    try:
        check_publish_allowed(
            "valkey-io/valkey", action="create_issue", context="issue #42",
        )
    except RuntimeError as exc:
        msg = str(exc)
        assert "create_issue" in msg
        assert "valkey-io/valkey" in msg
        assert "issue #42" in msg
    else:
        pytest.fail("Expected RuntimeError")
