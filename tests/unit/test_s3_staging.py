"""Unit tests for S3 staging skip-if-already-staged behavior (no network)."""

from __future__ import annotations

from pathlib import Path

from botocore.exceptions import ClientError

from benchmark.staging import s3


class _FakeClient:
    """Minimal stand-in for a boto3 S3 client.

    ``head_sizes`` maps key -> ContentLength for objects that "exist"; a key
    not present raises a 404 ClientError. Records ``uploaded`` keys.
    """

    def __init__(self, head_sizes: dict[str, int]) -> None:
        self._head_sizes = head_sizes
        self.uploaded: list[str] = []

    def head_object(self, Bucket: str, Key: str) -> dict:  # noqa: N803 (boto3 kwarg names)
        if Key in self._head_sizes:
            return {"ContentLength": self._head_sizes[Key]}
        raise ClientError({"Error": {"Code": "404"}}, "HeadObject")

    def upload_file(self, filename, bucket, key, Callback=None) -> None:  # noqa: N803
        self.uploaded.append(key)


def _write(tmp_path: Path, n: int) -> Path:
    # Sparse file: set the size without writing n bytes (some sizes are multi-GiB;
    # only stat().st_size is read by the code under test).
    p = tmp_path / "f.parquet"
    with open(p, "wb") as fh:
        if n:
            fh.truncate(n)
    return p


def test_already_staged_size_match(tmp_path):
    local = _write(tmp_path, 100)
    client = _FakeClient({"data/x": 100})
    assert s3._already_staged(client, "b", "data/x", local) is True


def test_already_staged_size_mismatch(tmp_path):
    local = _write(tmp_path, 100)
    client = _FakeClient({"data/x": 99})
    assert s3._already_staged(client, "b", "data/x", local) is False


def test_already_staged_missing_object(tmp_path):
    local = _write(tmp_path, 100)
    client = _FakeClient({})  # 404 on head
    assert s3._already_staged(client, "b", "data/x", local) is False


def test_upload_skips_when_staged(tmp_path):
    local = _write(tmp_path, 100)
    client = _FakeClient({"data/x": 100})
    assert s3._upload(client, local, "b", "data/x") is False
    assert client.uploaded == []  # no transfer issued


def test_upload_runs_when_not_staged(tmp_path):
    local = _write(tmp_path, 100)
    client = _FakeClient({})  # not present
    assert s3._upload(client, local, "b", "data/x") is True
    assert client.uploaded == ["data/x"]


# --- staged_object_verdict (monkeypatch _client + settings.S3_BUCKET) ---


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(s3, "_client", lambda region, profile=None: client)

    class _S:
        S3_BUCKET = "b"

        class AWS:
            REGION = "us-east-1"
            PROFILE = "benchmark"

    monkeypatch.setattr(s3, "get_settings", lambda: _S())


def test_verdict_stale_when_sizes_differ(tmp_path, monkeypatch):
    # 92 MiB staged vs 8 GiB local (~1%) — far outside tolerance => STALE/partial.
    local = _write(tmp_path, 8 * 1024**3)  # 8 GiB local
    _patch_client(
        monkeypatch, _FakeClient({"data/clickbench/hits.parquet": 92 * 1024**2})
    )  # 92 MiB
    v = s3.staged_object_verdict("data/clickbench/hits.parquet", local)
    assert v.ok is False and "STALE" in v.detail and "re-stage" in v.detail


def test_verdict_match_points_downstream(tmp_path, monkeypatch):
    local = _write(tmp_path, 100)
    _patch_client(monkeypatch, _FakeClient({"data/clickbench/hits.parquet": 100}))
    v = s3.staged_object_verdict("data/clickbench/hits.parquet", local)
    assert v.ok is True and "matches local" in v.detail and "downstream" in v.detail


def test_verdict_within_tolerance_is_match(tmp_path, monkeypatch):
    # The real-world case: two valid full-corpus derives differ by ~0.03%.
    # Byte sizes differ but well within tolerance => NOT stale.
    local = _write(tmp_path, 8_792_356_629)
    _patch_client(monkeypatch, _FakeClient({"data/clickbench/hits.parquet": 8_794_658_868}))
    v = s3.staged_object_verdict("data/clickbench/hits.parquet", local)
    assert v.ok is True and "matches local" in v.detail and "STALE" not in v.detail


def test_verdict_not_staged_on_404(tmp_path, monkeypatch):
    local = _write(tmp_path, 100)
    _patch_client(monkeypatch, _FakeClient({}))  # 404
    v = s3.staged_object_verdict("data/clickbench/hits.parquet", local)
    assert v.ok is False and "NOT staged" in v.detail


# --- verify_estuary_s3_credentials (no network; length + presence checks) ---


def _patch_estuary_creds(monkeypatch, key_id, secret):
    class _Est:
        S3_AWS_ACCESS_KEY_ID = (
            type("S", (), {"get_secret_value": lambda self: key_id})() if key_id else None
        )
        S3_AWS_SECRET_ACCESS_KEY = (
            type("S", (), {"get_secret_value": lambda self: secret})() if secret else None
        )

    class _S:
        S3_BUCKET = "b"
        ESTUARY = _Est()

        class AWS:
            REGION = "us-east-1"

    monkeypatch.setattr(s3, "get_settings", lambda: _S())


def test_verify_creds_flags_truncated_secret(monkeypatch):
    # A truncated 30-char secret is caught by the length check, no network call.
    _patch_estuary_creds(monkeypatch, "AKIA" + "X" * 16, "x" * 30)
    err = s3.verify_estuary_s3_credentials()
    assert err is not None and "30 chars" in err and "truncated" in err


def test_verify_creds_flags_missing(monkeypatch):
    _patch_estuary_creds(monkeypatch, None, None)
    err = s3.verify_estuary_s3_credentials()
    assert err is not None and "not set" in err
