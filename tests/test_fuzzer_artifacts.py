"""Tests for fuzzer artifact client."""
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import pytest

from scripts.fuzzer.artifacts import ArtifactClient, _extract_zip


def test_extract_zip_valid():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("file.txt", "hello")
    assert _extract_zip(buf.getvalue()) == {"file.txt": b"hello"}


@pytest.mark.parametrize("blob", [b"", b"not a zip"])
def test_extract_zip_returns_empty_on_bad_input(blob):
    assert _extract_zip(blob) == {}


def test_extract_zip_refuses_oversized_archive(monkeypatch):
    """A buggy fuzzer dumping a multi-GB log shouldn't OOM the monitor."""
    from scripts.fuzzer import artifacts as artifacts_mod
    monkeypatch.setattr(artifacts_mod, "_MAX_UNCOMPRESSED_BYTES", 512)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", b"a" * 1024)
    assert _extract_zip(buf.getvalue()) == {}


def test_client_requires_token():
    with pytest.raises(ValueError, match="token is required"):
        ArtifactClient(MagicMock(), token="")


def test_list_run_artifacts():
    mock_repo = MagicMock()
    mock_repo._requester.requestJsonAndCheck.return_value = (
        {}, {"artifacts": [{"id": 1, "name": "fuzzer-run-artifacts-123",
                            "size_in_bytes": 1024, "expired": False}]},
    )
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    client = ArtifactClient(mock_gh, token="t")
    arts = client.list_run_artifacts("r", 99)
    assert len(arts) == 1
    assert arts[0].name == "fuzzer-run-artifacts-123"
