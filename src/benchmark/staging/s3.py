"""Stage Parquet data to S3 for Estuary capture.

Uploads all benchmark Parquet under ``data/`` to S3:
  - data/tpcds/sfN/*.parquet     -> s3://<bucket>/data/tpcds/sfN/
  - data/dsb/sfN/*.parquet       -> s3://<bucket>/data/dsb/sfN/
  - data/clickbench/hits.parquet -> s3://<bucket>/data/clickbench/hits.parquet

Empty / missing suite directories are silently skipped, so it's safe to run
before any generator has produced data. The ClickBench branch uses a single
multipart upload (the file is ~14 GiB; boto3's default TransferConfig already
multiparts at 8 MiB chunks).

This module ASSUMES the bucket already exists — provisioning lives in
``terraform/modules/shared``. If the bucket is missing the upload fails
fast with a "go provision shared infra" message rather than auto-creating
it here.
"""

from __future__ import annotations

import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, ProfileNotFound

from ..exceptions import BenchmarkError
from ..settings import get_settings

logger = logging.getLogger("benchmark.staging.s3")


class StagingError(BenchmarkError):
    """Failed to stage data to cloud storage."""


def _client(region: str, profile: str | None = None):
    profile = profile or get_settings().AWS.PROFILE
    try:
        session = boto3.Session(profile_name=profile)
        return session.client("s3", region_name=region)
    except ProfileNotFound as e:
        raise StagingError(
            f"AWS profile '{profile}' not found in ~/.aws/credentials — "
            f"run `aws configure --profile {profile}` or set AWS_PROFILE to an existing profile"
        ) from e
    except NoCredentialsError as e:
        raise StagingError("AWS credentials not configured: fill AWS_* in env/aws.env") from e


def _check_bucket(client, bucket: str) -> None:
    try:
        client.head_bucket(Bucket=bucket)
        logger.info("bucket %s reachable", bucket)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchBucket"}:
            raise StagingError(
                f"S3 bucket '{bucket}' does not exist. Provision it via "
                f"`terraform -chdir=terraform apply -target=module.shared` "
                f"before staging data."
            ) from e
        raise StagingError(f"unable to access S3 bucket '{bucket}': {e}") from e


def _fmt_size(n: int) -> str:
    gib = n / (1024**3)
    return f"{gib:.1f} GiB" if gib >= 1 else f"{n / (1024**2):.0f} MiB"


# Parquet derive is row-deterministic but NOT byte-deterministic (compression,
# row-group boundaries, dictionary encoding vary between runs), so two valid
# full copies of the same corpus can differ by a small fraction. The diagnostic
# verdict compares staged vs local within this relative tolerance; a stale
# sample / partial stage is orders of magnitude off, well outside it.
_STAGED_SIZE_TOLERANCE = 0.10


@dataclass(frozen=True)
class StagedVerdict:
    """Outcome of comparing a staged S3 object to its local source.

    ``ok``: True = staged object is the full source (matches within tolerance);
    False = stale/partial/missing (re-stage); None = couldn't determine (no
    creds, access error, or nothing local to compare against). ``detail`` is the
    human-readable one-liner shown to the operator.
    """

    ok: bool | None
    detail: str


def staged_object_verdict(
    key: str,
    local: Path,
    *,
    bucket: str | None = None,
    region: str | None = None,
    profile: str | None = None,
) -> StagedVerdict:
    """Verdict comparing the staged S3 object at ``key`` to ``local``.

    Best-effort and side-effect-only-reads: returns ``ok=None`` with an advisory
    detail on missing creds / access errors rather than raising, since this is a
    diagnostic aid, not a gate. Authenticates as the configured AWS profile
    (the same identity ``stage-data`` writes with), so it sees what was staged.
    """
    settings = get_settings()
    bucket = bucket or settings.S3_BUCKET
    if not bucket:
        return StagedVerdict(None, "S3 bucket not configured — can't verify the staged object")
    try:
        client = _client(region or settings.AWS.REGION, profile)
    except StagingError as e:
        return StagedVerdict(None, f"could not check S3 ({e})")
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return StagedVerdict(False, f"NOT staged in S3 (s3://{bucket}/{key}) — run stage-data")
        return StagedVerdict(None, f"could not check S3 (S3 {code or 'error'})")
    except BotoCoreError as e:
        return StagedVerdict(None, f"could not check S3 ({type(e).__name__})")

    staged = head.get("ContentLength") or 0
    when = head.get("LastModified")
    when_s = f", staged {when.date().isoformat()}" if when is not None else ""
    if not local.exists():
        return StagedVerdict(
            None, f"S3 has {_fmt_size(staged)}{when_s}; no local artifact to compare against"
        )
    local_size = local.stat().st_size
    if abs(staged - local_size) <= _STAGED_SIZE_TOLERANCE * local_size:
        # Same corpus; byte size differs only by parquet encoding variance.
        return StagedVerdict(
            True,
            f"matches local within encoding variance ({_fmt_size(staged)}{when_s}) — "
            f"the source is fine; the gap is downstream (capture / materialization / backfill)",
        )
    if staged < local_size:
        pct = 100.0 * staged / local_size if local_size else 0.0
        return StagedVerdict(
            False,
            f"STALE/partial: S3 has {_fmt_size(staged)}{when_s} = {pct:.0f}% of local "
            f"{_fmt_size(local_size)} — re-stage (stage-data)",
        )
    return StagedVerdict(
        None,
        f"larger than local: S3 has {_fmt_size(staged)}{when_s} vs local "
        f"{_fmt_size(local_size)} — verify the staged file",
    )


def verify_estuary_s3_credentials(
    bucket: str | None = None, region: str | None = None
) -> str | None:
    """Check the Estuary capture's S3 reader key (``ESTUARY_S3_AWS_*``) can reach
    the bucket — the same credentials the source-s3 connector authenticates with.

    Returns an error string if the key is missing / malformed / rejected, else
    ``None``. Best-effort (returns a message rather than raising); intended as a
    publish pre-flight so a bad key fails fast with a clear cause instead of
    surfacing deep in a flowctl draft error.
    """
    settings = get_settings()
    bucket = bucket or settings.S3_BUCKET
    if not bucket:
        return "S3 bucket not configured (BENCHMARK_S3_BUCKET)"
    est = settings.ESTUARY
    kid = est.S3_AWS_ACCESS_KEY_ID.get_secret_value() if est.S3_AWS_ACCESS_KEY_ID else None
    sec = est.S3_AWS_SECRET_ACCESS_KEY.get_secret_value() if est.S3_AWS_SECRET_ACCESS_KEY else None
    if not kid or not sec:
        return "ESTUARY_S3_AWS_ACCESS_KEY_ID / ESTUARY_S3_AWS_SECRET_ACCESS_KEY not set"
    # AWS secret access keys are exactly 40 chars; a wrong length is a truncated
    # paste — catch it before spending a network round-trip.
    if len(sec) != 40:
        return (
            f"ESTUARY_S3_AWS_SECRET_ACCESS_KEY is {len(sec)} chars (AWS secrets are 40) "
            "— likely truncated; re-copy from `terraform output -raw estuary_secret_access_key`"
        )
    try:
        client = boto3.Session(
            aws_access_key_id=kid,
            aws_secret_access_key=sec,
            region_name=region or settings.AWS.REGION,
        ).client("s3")
        client.head_bucket(Bucket=bucket)
    except (BotoCoreError, ClientError) as e:
        code = e.response.get("Error", {}).get("Code", "") if isinstance(e, ClientError) else ""
        return f"Estuary S3 reader key rejected by s3://{bucket} ({code or type(e).__name__})"
    return None


def _already_staged(client, bucket: str, key: str, local: Path) -> bool:
    """Whether ``key`` already exists in ``bucket`` with the exact same byte
    size as ``local`` — the skip-upload signal.

    Exact match is deliberately conservative: a re-derive can change the byte
    size slightly (parquet encoding isn't byte-deterministic), and in that case
    we re-upload rather than risk leaving a divergent object staged. So this
    skips the common "re-run stage-data without re-deriving" case, not a
    re-derive. Multipart ETags aren't plain MD5s, so size is the pragmatic
    check. A 404 means not staged.
    """
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
    return head.get("ContentLength") == local.stat().st_size


def _upload(client, local: Path, bucket: str, key: str, callback=None) -> bool:
    """Upload ``local`` to ``key`` unless an identically-sized object is already
    there. Returns True if uploaded, False if skipped (already staged)."""
    if _already_staged(client, bucket, key, local):
        return False
    client.upload_file(str(local), bucket, key, Callback=callback)
    return True


def _upload_progress(enabled: bool):
    """A rich upload progress bar (bytes/total, %, speed, ETA), or a no-op
    context when disabled / not a TTY. Renders to stderr so stdout stays clean.
    Mirrors ``datagen.clickbench._download_progress``."""
    if not (enabled and sys.stderr.isatty()):
        return nullcontext(None)
    from rich.console import Console
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TaskProgressColumn,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )

    return Progress(
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=Console(stderr=True),
    )


class _UploadCallback:
    """Thread-safe boto3 ``upload_file`` callback that advances a rich task.

    boto3's multipart transfer invokes the callback from multiple worker
    threads, so the byte counter is advanced under a lock."""

    def __init__(self, progress, task) -> None:
        self._progress = progress
        self._task = task
        self._lock = threading.Lock()

    def __call__(self, bytes_amount: int) -> None:
        with self._lock:
            self._progress.update(self._task, advance=bytes_amount)


ALL_SUITES: tuple[str, ...] = ("tpcds", "dsb", "clickbench")


def stage_all_suites(
    bucket: str | None = None,
    data_root: Path = Path("data"),
    max_workers: int = 4,
    suites: tuple[str, ...] = ALL_SUITES,
    show_progress: bool = False,
) -> dict[str, list[str]]:
    """Upload Parquet for the requested suites under ``data_root`` to S3.

    ``suites`` scopes which blocks are staged (default all three). This matters:
    ClickBench's ``hits.parquet`` is multi-GiB, so a TPC-DS/DSB-only run must
    pass ``suites=("tpcds", "dsb")`` to avoid a long, irrelevant upload. Returns
    mapping of {suite: [s3_keys_uploaded]}. Missing suite dirs are skipped
    silently — re-running after generating only a subset is safe.

    ClickBench is a single multi-GiB hits.parquet; it's uploaded sequentially
    after the smaller TPC-DS/DSB shards finish so the thread pool isn't tied
    up while the big file uploads. boto3's TransferConfig multiparts at
    8 MiB chunks by default, which is what we want.
    """
    settings = get_settings()
    bucket = bucket or settings.S3_BUCKET
    if not bucket:
        raise StagingError(
            "S3 bucket not configured — set BENCHMARK_S3_BUCKET env var or pass --bucket"
        )
    region = settings.AWS.REGION

    try:
        client = _client(region)
        _check_bucket(client, bucket)
    except (BotoCoreError, ClientError) as e:
        raise StagingError(f"S3 bucket check failed: {e}") from e

    uploaded: dict[str, list[str]] = {"tpcds": [], "dsb": [], "clickbench": []}
    futures: list[tuple[str, str, object]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        for suite in ("tpcds", "dsb"):
            if suite not in suites:
                continue
            for sf_dir in sorted((data_root / suite).glob("sf*")):
                if not sf_dir.is_dir():
                    continue
                for parquet in sorted(sf_dir.glob("*.parquet")):
                    key = f"data/{suite}/{sf_dir.name}/{parquet.name}"
                    futures.append((suite, key, ex.submit(_upload, client, parquet, bucket, key)))
        for suite, key, fut in futures:
            did_upload = fut.result()
            uploaded[suite].append(key)
            logger.info(
                "%s s3://%s/%s",
                "uploaded" if did_upload else "skipped (already staged)",
                bucket,
                key,
            )

    # ClickBench: single multi-GiB file, uploaded after the small files settle.
    if "clickbench" in suites:
        cb_path = data_root / "clickbench" / "hits.parquet"
        if cb_path.exists():
            cb_key = "data/clickbench/hits.parquet"
            cb_size = cb_path.stat().st_size
            cb_gib = cb_size / (1024**3)
            if _already_staged(client, bucket, cb_key, cb_path):
                logger.info(
                    "skipped (already staged) s3://%s/%s (%.1f GiB)", bucket, cb_key, cb_gib
                )
                uploaded["clickbench"].append(cb_key)
            else:
                logger.info("uploading ClickBench hits.parquet (%.1f GiB)", cb_gib)
                with _upload_progress(show_progress) as progress:
                    callback = None
                    if progress is not None:
                        task = progress.add_task("ClickBench hits", total=cb_size)
                        callback = _UploadCallback(progress, task)
                    _upload(client, cb_path, bucket, cb_key, callback=callback)
                uploaded["clickbench"].append(cb_key)
                logger.info("uploaded s3://%s/%s", bucket, cb_key)
        else:
            logger.info("ClickBench hits.parquet missing at %s; skipping", cb_path)

    total = sum(len(v) for v in uploaded.values())
    logger.info("staged %d files to s3://%s", total, bucket)
    return uploaded
