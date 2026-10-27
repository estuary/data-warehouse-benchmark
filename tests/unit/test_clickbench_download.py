"""ClickBench download: resume-from-partial + auto-retry on network drops.

A ~100 GB download can't restart from scratch every time the network drops, so
the downloader uses HTTP Range to resume and retries transient failures with
backoff. These tests fake ``requests.get`` so no network is touched.
"""

from __future__ import annotations

import benchmark.datagen.clickbench as cb


class _FakeResp:
    """Minimal stand-in for a streaming ``requests`` response."""

    def __init__(self, status_code: int, chunks: list[bytes]):
        self.status_code = status_code
        self._chunks = chunks

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def iter_content(self, chunk_size: int):
        yield from self._chunks


def test_download_retries_then_succeeds(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_get(url, headers=None, stream=True, timeout=60):
        calls["n"] += 1
        if calls["n"] == 1:
            raise cb.requests.ConnectionError("vpn dropped mid-stream")
        return _FakeResp(200, [b"hello ", b"world"])

    monkeypatch.setattr(cb.requests, "get", fake_get)
    monkeypatch.setattr(cb.time, "sleep", lambda *_: None)  # no real backoff wait

    out = tmp_path / "hits.parquet.raw"
    cb.download_clickbench(out)

    assert calls["n"] == 2  # failed once, retried, succeeded
    assert out.read_bytes() == b"hello world"


def test_download_resumes_from_partial_with_range(tmp_path, monkeypatch):
    out = tmp_path / "hits.parquet.raw"
    out.write_bytes(b"hello ")  # a partial file from an earlier interrupted run
    seen = {}

    def fake_get(url, headers=None, stream=True, timeout=60):
        seen["headers"] = headers or {}
        return _FakeResp(206, [b"world"])  # 206 Partial Content -> append

    monkeypatch.setattr(cb.requests, "get", fake_get)
    monkeypatch.setattr(cb.time, "sleep", lambda *_: None)

    cb.download_clickbench(out)

    assert seen["headers"].get("Range") == "bytes=6-"  # resumed from byte 6
    assert out.read_bytes() == b"hello world"


def test_download_gives_up_after_max_retries(tmp_path, monkeypatch):
    def always_fail(url, headers=None, stream=True, timeout=60):
        raise cb.requests.ConnectionError("network down")

    monkeypatch.setattr(cb.requests, "get", always_fail)
    monkeypatch.setattr(cb.time, "sleep", lambda *_: None)

    out = tmp_path / "hits.parquet.raw"
    try:
        cb.download_clickbench(out, max_retries=2)
    except RuntimeError as exc:
        assert "after 2 retries" in str(exc)
    else:
        raise AssertionError("expected RuntimeError after exhausting retries")


def test_download_treats_416_as_complete(tmp_path, monkeypatch):
    out = tmp_path / "hits.parquet.raw"
    out.write_bytes(b"already complete")

    def fake_get(url, headers=None, stream=True, timeout=60):
        return _FakeResp(416, [])  # Range Not Satisfiable -> whole file present

    monkeypatch.setattr(cb.requests, "get", fake_get)
    monkeypatch.setattr(cb.time, "sleep", lambda *_: None)

    cb.download_clickbench(out)
    assert out.read_bytes() == b"already complete"  # untouched
