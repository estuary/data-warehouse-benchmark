"""Build the ``validate-loads`` row-count manifest from Parquet metadata (local or S3).

Reads parquet metadata only (no full scan) and emits
`data/<suite>/expected_counts.json` keyed by scale factor and table.

Two sources, one merge-safe writer:

  * **local** — walk ``data_dir/sf*/*.parquet`` footers (what ``generate-*``
    produced on this machine).
  * **S3** — read the staged parquet footers directly from the staging bucket
    (``data/<suite>/sf<N>/<table>.parquet``). Lets us backfill a manifest entry
    for a scale whose data was generated elsewhere and only exists in S3 — the
    footer is a range read, so even multi-GB facts cost a few KB.

Both write through :func:`merge_into_manifest`, which **preserves scales not in
the new counts** (so deriving SF10 never clobbers a committed SF1/SF100). This
matches ``datagen.tpcds.write_expected_counts_manifest``; the two are kept
behaviourally identical on purpose.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pyarrow.parquet as pq

logger = logging.getLogger("benchmark.staging.expected_counts")


def merge_into_manifest(
    counts: dict[str, dict[str, int]], output: Path
) -> dict[str, dict[str, int]]:
    """Merge per-scale ``counts`` into the manifest at ``output`` and write it.

    Scale-level merge: a scale present in ``counts`` replaces that scale's map;
    scales only in the existing file are preserved. Trailing newline + sorted
    keys so regenerating is a no-op diff against the committed manifest.
    """
    output = Path(output)
    existing: dict[str, dict[str, int]] = {}
    if output.exists():
        existing = json.loads(output.read_text(encoding="utf-8"))
    existing.update(counts)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(existing, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    logger.info(
        "expected_counts written to %s (scales: %s)",
        output,
        sorted(int(k) for k in existing),
    )
    return existing


def counts_from_local(data_dir: Path) -> dict[str, dict[str, int]]:
    """Row counts per scale from local ``data_dir/sf*/<table>.parquet`` footers."""
    counts: dict[str, dict[str, int]] = {}
    for sf_dir in sorted(Path(data_dir).glob("sf*")):
        if not sf_dir.is_dir():
            continue
        sf_label = sf_dir.name.removeprefix("sf")
        scale_counts: dict[str, int] = {}
        for parquet in sorted(sf_dir.glob("*.parquet")):
            n = pq.ParquetFile(parquet).metadata.num_rows
            scale_counts[parquet.stem] = n
            logger.info("sf%s %s: %d rows", sf_label, parquet.stem, n)
        if scale_counts:
            counts[sf_label] = scale_counts
    return counts


def make_s3_filesystem(region: str, access_key: str, secret_key: str):
    """A pyarrow ``S3FileSystem`` bound to explicit keys (the long-lived staging
    keys Estuary uses), independent of the ambient AWS credential chain."""
    import pyarrow.fs as fs

    return fs.S3FileSystem(access_key=access_key, secret_key=secret_key, region=region)


def parquet_footer_rows(s3, path: str) -> int:
    """Row count from a single S3 parquet **footer** (a range read — no full
    download, so size-independent). ``path`` is ``bucket/key`` (no ``s3://``)."""
    return pq.ParquetFile(s3.open_input_file(path)).metadata.num_rows


def counts_from_s3(
    bucket: str,
    suite: str,
    scales: list[int],
    *,
    region: str,
    access_key: str,
    secret_key: str,
) -> dict[str, dict[str, int]]:
    """Row counts per scale from staged S3 parquet footers.

    Reads ``s3://{bucket}/data/{suite}/sf{N}/<table>.parquet`` footers (range
    reads — no full download) for each requested scale. A scale with no objects
    under its prefix is skipped (not recorded as empty).
    """
    import pyarrow.fs as fs

    s3 = make_s3_filesystem(region, access_key, secret_key)
    counts: dict[str, dict[str, int]] = {}
    for sf in scales:
        prefix = f"{bucket}/data/{suite}/sf{sf}"
        selector = fs.FileSelector(prefix, allow_not_found=True)
        infos = [i for i in s3.get_file_info(selector) if i.path.endswith(".parquet")]
        scale_counts: dict[str, int] = {}
        for info in sorted(infos, key=lambda i: i.path):
            table = Path(info.path).stem
            scale_counts[table] = parquet_footer_rows(s3, info.path)
            logger.info("s3 %s/sf%d %s: %d rows", suite, sf, table, scale_counts[table])
        if scale_counts:
            counts[str(sf)] = scale_counts
        else:
            logger.warning("no parquet found under s3://%s — skipping SF%d", prefix, sf)
    return counts


def write_from_parquet_metadata(data_dir: Path, output: Path) -> dict[str, dict[str, int]]:
    """Derive counts from local parquet footers and merge into the manifest.

    Merge-safe: scales whose local parquet is absent are preserved from the
    existing manifest rather than dropped.
    """
    return merge_into_manifest(counts_from_local(Path(data_dir)), output)


def write_from_s3_metadata(
    bucket: str,
    suite: str,
    scales: list[int],
    output: Path,
    *,
    region: str,
    access_key: str,
    secret_key: str,
) -> dict[str, dict[str, int]]:
    """Derive counts from staged S3 parquet footers and merge into the manifest."""
    counts = counts_from_s3(
        bucket, suite, scales, region=region, access_key=access_key, secret_key=secret_key
    )
    return merge_into_manifest(counts, output)
