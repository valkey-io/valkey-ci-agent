"""Tests for fuzzer artifact client."""
from __future__ import annotations

import io
import zipfile
from unittest.mock import MagicMock

import pytest

from scripts.common.workflow_artifacts import ArtifactClient, _extract_zip


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
    from scripts.common import workflow_artifacts as artifacts_mod
    monkeypatch.setattr(artifacts_mod, "_MAX_UNCOMPRESSED_BYTES", 512)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.bin", b"a" * 1024)
    assert _extract_zip(buf.getvalue()) == {}


def test_download_keeps_token_off_cross_host_redirects(monkeypatch):
    """The GitHub token must not be forwarded to the signed S3 redirect host."""
    from scripts.common import workflow_artifacts as artifacts_mod

    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"zipbytes"

    def fake_urlopen(req, timeout=0):
        captured["req"] = req
        return _Resp()

    monkeypatch.setattr(artifacts_mod, "urlopen", fake_urlopen)
    client = ArtifactClient(MagicMock(), token="secret")
    client._download("/repos/x/actions/artifacts/1/zip")

    req = captured["req"]
    # has_header() checks both header sets, so assert on the dicts directly:
    # normal headers are forwarded on redirect, unredirected ones are not.
    assert "Authorization" not in req.headers
    assert req.unredirected_hdrs.get("Authorization") == "Bearer secret"


def test_client_requires_token():
    with pytest.raises(ValueError, match="token is required"):
        ArtifactClient(MagicMock(), token="")


def test_download_run_logs_extracts_per_step_logs(monkeypatch):
    """Run logs come back as a zip of per-step text files."""
    from scripts.common import workflow_artifacts as artifacts_mod

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("1_build.txt", "make output")
        zf.writestr("2_test.txt", "[err]: NAN score")

    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return buf.getvalue()

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        return _Resp()

    monkeypatch.setattr(artifacts_mod, "urlopen", fake_urlopen)
    client = ArtifactClient(MagicMock(), token="t")
    logs = client.download_run_logs("valkey-io/valkey", 27559908167)

    assert logs == {"1_build.txt": b"make output", "2_test.txt": b"[err]: NAN score"}
    assert captured["url"].endswith("/actions/runs/27559908167/logs")


def test_download_run_logs_empty_on_expired(monkeypatch):
    """Expired logs (404) yield an empty map, not an exception."""
    from scripts.common import workflow_artifacts as artifacts_mod

    client = ArtifactClient(MagicMock(), token="t")
    monkeypatch.setattr(client, "_download", lambda path: b"")
    assert client.download_run_logs("r", 1) == {}


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


def test_list_run_artifacts_skips_malformed_entries():
    """An artifact entry missing id or name is skipped, not a hard error."""
    mock_repo = MagicMock()
    mock_repo._requester.requestJsonAndCheck.return_value = (
        {}, {"artifacts": [
            {"id": 1, "name": "good"},
            {"id": 2},                       # missing name
            {"name": "no-id"},               # missing id
            "not-a-dict",
        ]},
    )
    mock_gh = MagicMock()
    mock_gh.get_repo.return_value = mock_repo

    client = ArtifactClient(mock_gh, token="t")
    arts = client.list_run_artifacts("r", 99)
    assert [a.name for a in arts] == ["good"]


def _zip_with_unsupported_member(*, include_good: bool):
    """A zip whose first member declares compression method 99."""
    import struct
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bad.bin", b'{"x":1}' * 100)
        if include_good:
            zf.writestr("all-test-failures.json", b'{"real":"data"}')
    raw = bytearray(buf.getvalue())
    struct.pack_into("<H", raw, raw.find(b"PK\x03\x04") + 8, 99)
    struct.pack_into("<H", raw, raw.find(b"PK\x01\x02") + 10, 99)
    return bytes(raw)


def test_extract_zip_survives_unsupported_compression():
    """zipfile raises NotImplementedError for an unknown method. Left uncaught
    it aborts the sweep before any issue is filed."""
    assert _extract_zip(_zip_with_unsupported_member(include_good=False)) == {}


def test_extract_zip_keeps_readable_members_alongside_a_bad_one():
    """One unreadable member must not discard the rest of the bundle."""
    files = _extract_zip(_zip_with_unsupported_member(include_good=True))
    assert files == {"all-test-failures.json": b'{"real":"data"}'}


def test_extract_zip_survives_corrupt_deflate_stream():
    """A bit-flip inside the compressed payload raises zlib.error on read."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("corrupt.bin", bytes(range(256)) * 40)
        zf.writestr("all-test-failures.json", b'{"real":"data"}')
    raw = bytearray(buf.getvalue())
    start = raw.find(b"PK\x03\x04")
    for off in range(start + 60, start + 120):
        raw[off] ^= 0xA5

    files = _extract_zip(bytes(raw))
    assert files == {"all-test-failures.json": b'{"real":"data"}'}


def test_extract_zip_reports_damaged_members():
    """Callers that pass a damaged list learn which members were skipped, so
    they can report that the extraction was incomplete."""
    damaged: list[str] = []
    files = _extract_zip(_zip_with_unsupported_member(include_good=True), damaged)

    assert files == {"all-test-failures.json": b'{"real":"data"}'}
    assert len(damaged) == 1
    assert damaged[0].startswith("bad.bin: NotImplementedError")


def test_extract_zip_reports_unopenable_archive():
    """A zip that cannot be opened at all is reported as whole-archive damage."""
    damaged: list[str] = []
    assert _extract_zip(b"not a zip", damaged) == {}
    assert len(damaged) == 1
    assert damaged[0].startswith("whole archive:")


def test_extract_zip_leaves_damaged_empty_when_intact():
    """An intact archive records no damage."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("all-test-failures.json", b"[]")
    damaged: list[str] = []
    assert _extract_zip(buf.getvalue(), damaged) == {"all-test-failures.json": b"[]"}
    assert damaged == []
