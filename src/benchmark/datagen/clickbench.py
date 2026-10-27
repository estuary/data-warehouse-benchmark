"""ClickBench hits.parquet download + ground-truth hashing + typed-parquet derive.

The ClickBench ``hits`` table is a single ~100 GB Parquet hosted by
ClickHouse. Pipeline:

  download_clickbench()      ->  data/clickbench/hits.parquet.raw   (raw upstream, ints)
  derive_typed_parquet()     ->  data/clickbench/hits.parquet       (typed, what Estuary sees)
  hash_clickbench_ground_truth()  reads hits.parquet.raw via casting view

The typed derive casts EventDate to Date32 and EventTime / ClientEventTime /
LocalEventTime to RFC 3339 ISO 8601 strings — sidesteps source-s3's serializer
of parquet Timestamp columns which Estuary's `format: date-time` validator
rejects.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sys
import time
import urllib.request
from contextlib import nullcontext
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
import requests

from ..queries.registry import QUERIES_ROOT, QueryRegistry
from ..results import ResultsDB
from ..sql_benchmark.validator import _hash_result, serialize_columns, serialize_rows

logger = logging.getLogger("benchmark.datagen.clickbench")

CLICKBENCH_URL = "https://datasets.clickhouse.com/hits_compatible/hits.parquet"
CLICKBENCH_QUERIES_URL = (
    "https://raw.githubusercontent.com/ClickHouse/ClickBench/main/duckdb/queries.sql"
)


def download_clickbench_queries(
    output_dir: Path | None = None,
    url: str = CLICKBENCH_QUERIES_URL,
) -> list[Path]:
    """Fetch the 43 canonical ClickBench queries and write them to disk.

    Upstream ships them as a single file with one SQL statement per line.
    We split on newlines and write ``query_NN.sql`` (1-padded to 2 digits) so
    they pick up the same QueryRegistry conventions as TPC-DS and DSB.

    Args:
        output_dir: Where to write the .sql files. Defaults to
            ``queries/duckdb/clickbench/`` resolved from the project root.
        url: Source URL for queries.sql (override for an offline mirror).

    Returns:
        List of written file paths in query-number order.
    """
    if output_dir is None:
        output_dir = QUERIES_ROOT / "duckdb" / "clickbench"
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("fetching ClickBench queries from %s", url)
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = resp.read().decode("utf-8")

    queries = [line.strip().rstrip(";") for line in body.splitlines() if line.strip()]
    if not queries:
        raise RuntimeError(f"upstream queries.sql at {url} returned no statements")
    logger.info("parsed %d ClickBench queries", len(queries))

    written: list[Path] = []
    for i, sql in enumerate(queries, start=1):
        target = output_dir / f"query_{i:02d}.sql"
        target.write_text(sql + "\n", encoding="utf-8")
        written.append(target)
    logger.info("wrote %d query files to %s", len(written), output_dir)
    return written


def _content_total(resp: requests.Response, current_size: int) -> int | None:
    """Full file size in bytes from a download response, or None if unknown.

    206: ``Content-Range: bytes start-end/total`` carries the full size. 200:
    ``Content-Length`` is the full size. 206 without Content-Range: Content-Length
    is the *remaining* bytes, so add what's already on disk.
    """
    cr = resp.headers.get("Content-Range")
    if cr and "/" in cr:
        total = cr.rsplit("/", 1)[-1].strip()
        if total.isdigit():
            return int(total)
    cl = resp.headers.get("Content-Length")
    if cl and cl.isdigit():
        return current_size + int(cl) if resp.status_code == 206 else int(cl)
    return None


def _download_progress(enabled: bool):
    """A rich download progress bar (bytes/total, %, speed, ETA), or a no-op
    context when disabled / not a TTY. Renders to stderr so stdout stays clean."""
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


def download_clickbench(
    output_path: Path = Path("data/clickbench/hits.parquet"),
    *,
    expected_sha256: str | None = None,
    chunk_size: int = 8 * 1024 * 1024,
    max_retries: int = 10,
    show_progress: bool = True,
) -> Path:
    """Download hits.parquet with resume + auto-retry.

    Uses HTTP ``Range`` so a partial file at ``output_path`` resumes from its
    current size. Transient network failures (e.g. a dropped connection) are
    retried with exponential backoff, each attempt resuming from the bytes
    already on disk, so a flaky connection doesn't force a manual restart of a
    ~100 GB download.

    With ``show_progress`` (and an interactive terminal) a rich progress bar shows
    bytes/total, %, speed, and ETA; it resumes mid-bar from what's already on disk.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    on_disk_gb = (output_path.stat().st_size / 1e9) if output_path.exists() else 0.0
    logger.info(
        "downloading ClickBench hits parquet (~14 GiB on disk) → %s%s",
        output_path,
        f" — resuming from {on_disk_gb:.1f} GB on disk" if on_disk_gb else "",
    )
    started = time.perf_counter()

    with _download_progress(show_progress) as progress:
        task = progress.add_task("ClickBench hits", total=None) if progress else None
        attempt = 0
        while True:
            current_size = output_path.stat().st_size if output_path.exists() else 0
            if progress is not None:
                progress.update(task, completed=current_size)
            headers = {}
            if current_size:
                headers["Range"] = f"bytes={current_size}-"
                logger.info("resuming ClickBench download from byte %d", current_size)
            try:
                with requests.get(CLICKBENCH_URL, headers=headers, stream=True, timeout=60) as r:
                    # 416 = server says the requested range is already satisfied —
                    # i.e. we already have the whole file.
                    if r.status_code == 416 and current_size:
                        logger.info("ClickBench already complete (%d bytes)", current_size)
                        break
                    if r.status_code not in (200, 206):
                        raise RuntimeError(
                            f"unexpected status {r.status_code} from {CLICKBENCH_URL}"
                        )
                    if progress is not None:
                        total = _content_total(r, current_size)
                        if total is not None:
                            progress.update(task, total=total)
                    # 200 (not 206) => server ignored Range; restart the file clean.
                    mode = "ab" if r.status_code == 206 else "wb"
                    with output_path.open(mode) as f:
                        for chunk in r.iter_content(chunk_size=chunk_size):
                            if chunk:
                                f.write(chunk)
                                if progress is not None:
                                    progress.update(task, advance=len(chunk))
                break  # finished without a network error
            except (requests.RequestException, OSError) as exc:
                attempt += 1
                on_disk = output_path.stat().st_size if output_path.exists() else 0
                if attempt > max_retries:
                    raise RuntimeError(
                        f"ClickBench download failed after {max_retries} retries "
                        f"({on_disk} bytes on disk; re-run to resume): {exc}"
                    ) from exc
                backoff = min(60.0, 2.0**attempt)
                logger.warning(
                    "ClickBench download interrupted (attempt %d/%d, %d bytes on disk): "
                    "%s — retrying in %.0fs (resuming)",
                    attempt,
                    max_retries,
                    on_disk,
                    exc,
                    backoff,
                )
                time.sleep(backoff)

    final_size = output_path.stat().st_size
    elapsed = time.perf_counter() - started
    logger.info(
        "ClickBench download complete",
        extra={
            "path": str(output_path),
            "bytes": final_size,
            "elapsed_s": elapsed,
            "mbps": (final_size / max(elapsed, 1e-9)) / 1e6,
        },
    )

    if expected_sha256:
        actual = _sha256_file(output_path)
        if actual != expected_sha256:
            raise RuntimeError(
                f"ClickBench SHA256 mismatch — expected {expected_sha256}, got {actual}"
            )

    return output_path


def _sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def _typed_parquet_rows(path: Path) -> int | None:
    """Row count of an existing, readable typed parquet, else None.

    Returns None when the file is absent OR can't be read as parquet (a partial
    write from an interrupted derive) — both mean "re-derive". Footer read, no
    scan.
    """
    if not path.exists():
        return None
    try:
        return pq.ParquetFile(path).metadata.num_rows
    except Exception:
        logger.warning("existing %s is not a readable parquet — will re-derive", path)
        return None


def derive_typed_parquet(
    input_path: Path = Path("data/clickbench/hits.parquet.raw"),
    output_path: Path = Path("data/clickbench/hits.parquet"),
    *,
    force: bool = False,
) -> int:
    """Read the raw upstream ClickBench parquet and write a typed copy (the full
    corpus — there is no sampling; a sample doesn't represent the data and would
    break the row-count + ground-truth checks, which are keyed to all ~100M rows).

    The upstream parquet stores ``EventDate`` as int16 (days since 1970-01-01)
    and ``EventTime`` / ``ClientEventTime`` / ``LocalEventTime`` as int64
    (Unix epoch seconds). This function:

    - casts ``EventDate`` to ``Date32`` (DATE logical type).
    - writes the three timestamp columns as RFC 3339 ISO 8601 strings
      (``YYYY-MM-DDTHH:MM:SSZ``) — VARCHAR in the parquet, but the Estuary
      collection schema's ``format: date-time`` declaration drives the
      materializer to create ``TIMESTAMP WITH TIME ZONE`` at the destination.

    Native parquet ``Timestamp`` would be cleaner in principle but source-s3
    serializes those as ``"2013-07-14 22:52:59.000000 +00:00"`` (space
    separator, microseconds, ``+00:00`` offset) which Estuary's date-time
    validator rejects (requires RFC 3339 ``T`` separator), hence the strings.

    Idempotent: if a readable typed parquet already exists at ``output_path`` it
    is left as-is unless ``force=True`` (the derive is a ~14 GiB DuckDB COPY).

    Args:
        input_path: Raw upstream parquet (defaults to
            ``data/clickbench/hits.parquet.raw``).
        output_path: Where to write the typed copy (defaults to
            ``data/clickbench/hits.parquet`` — what ``stage-data`` uploads
            and Estuary captures).
        force: Re-derive even if a typed parquet already exists.

    Returns:
        Row count of ``output_path``.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not force:
        existing = _typed_parquet_rows(output_path)
        if existing is not None:
            logger.info(
                "typed ClickBench parquet already present at %s (%d rows) — "
                "skipping derive (pass force=True / --force to re-derive)",
                output_path,
                existing,
            )
            return existing

    if not input_path.exists():
        raise FileNotFoundError(
            f"raw ClickBench parquet missing: {input_path}. "
            "Run `uv run benchmark download-clickbench` first."
        )

    logger.info("deriving typed ClickBench parquet from %s -> %s", input_path, output_path)
    started = time.perf_counter()
    con = duckdb.connect(":memory:")
    try:
        con.execute(
            f"""
            COPY (
                SELECT * REPLACE (
                    (DATE '1970-01-01' + INTERVAL (EventDate) DAY)::DATE       AS EventDate,
                    strftime(to_timestamp(EventTime),       '%Y-%m-%dT%H:%M:%SZ') AS EventTime,
                    strftime(to_timestamp(ClientEventTime), '%Y-%m-%dT%H:%M:%SZ') AS ClientEventTime,
                    strftime(to_timestamp(LocalEventTime),  '%Y-%m-%dT%H:%M:%SZ') AS LocalEventTime
                )
                FROM read_parquet('{input_path.as_posix()}')
            )
            TO '{output_path.as_posix()}'
            (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        row_count = con.execute(
            f"SELECT COUNT(*) FROM read_parquet('{output_path.as_posix()}')"
        ).fetchone()[0]
    finally:
        con.close()

    elapsed = time.perf_counter() - started
    size_gib = output_path.stat().st_size / (1024**3)
    logger.info(
        "typed parquet written: %d rows, %.2f GiB, %.1fs",
        row_count,
        size_gib,
        elapsed,
    )

    # Commit the Estuary collection schema from the freshly typed parquet so the
    # capture stays define-once — `_build_clickbench_schema` then loads the JSON
    # even after the local parquet is freed (free_local_copy_after_stage).
    # Non-fatal: a persist failure must not fail the (expensive) derive.
    try:
        from .collection_schemas import persist_clickbench_schema

        schema_path = persist_clickbench_schema(parquet_path=output_path)
        logger.info("committed ClickBench collection schema to %s", schema_path)
    except Exception:
        logger.warning("failed to persist ClickBench collection schema", exc_info=True)

    return int(row_count)


def create_clickbench_hits_view(duck: duckdb.DuckDBPyConnection, parquet_path: Path) -> None:
    """Create a DuckDB ``hits`` view over ``parquet_path`` with normalized types.

    The canonical queries need true DATE / TIMESTAMP types for ``EventDate`` and
    the ``*EventTime`` columns. The source parquet comes in two shapes and we
    normalize whichever is present, so this works for both the raw upstream and
    the typed Estuary export:

      * raw upstream:  ``EventDate`` UINT16 (days since 1970-01-01),
                       ``*EventTime`` BIGINT (Unix seconds)
      * typed export:  ``EventDate`` DATE (keep), ``*EventTime`` VARCHAR ISO-8601
                       (``YYYY-MM-DDThh:mm:ssZ``) — cast to a naive TIMESTAMP to
                       match Snowflake's TIMESTAMP_NTZ and the tz-naive DuckDB
                       reference (drop the ``Z`` UTC marker).

    The ``* REPLACE`` keeps every other column intact. Shared by ground-truth
    generation and the equivalence checker so both build an identical view.
    """
    col_types = {
        name.upper(): dtype.upper()
        for name, dtype, *_ in duck.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{parquet_path.as_posix()}')"
        ).fetchall()
    }
    replacements: list[str] = []
    eventdate = col_types.get("EVENTDATE")
    if eventdate is not None and "DATE" not in eventdate:
        replacements.append("(DATE '1970-01-01' + INTERVAL (EventDate) DAY) AS EventDate")
    for col in ("EventTime", "ClientEventTime", "LocalEventTime"):
        dtype = col_types.get(col.upper())
        if dtype is None or "TIMESTAMP" in dtype:
            continue  # absent, or already a timestamp — leave as-is
        if "CHAR" in dtype or "STRING" in dtype:
            replacements.append(f"replace({col}, 'Z', '')::TIMESTAMP AS {col}")  # ISO-8601 string
        else:
            replacements.append(f"to_timestamp({col}) AS {col}")  # BIGINT epoch seconds
    replace_clause = f" REPLACE ({', '.join(replacements)})" if replacements else ""
    duck.execute(
        f"CREATE VIEW hits AS SELECT *{replace_clause} "
        f"FROM read_parquet('{parquet_path.as_posix()}')"
    )


def hash_clickbench_ground_truth(
    parquet_path: Path,
    results_db: ResultsDB,
) -> dict[str, dict]:
    """Run the 43 ClickBench canonical queries in DuckDB and persist hashes."""
    if not parquet_path.exists():
        raise FileNotFoundError(f"hits.parquet missing: {parquet_path}")

    queries = [q for q in QueryRegistry().all() if q.block == "clickbench"]
    if not queries:
        logger.warning("no clickbench queries registered; nothing to hash")
        return {}

    duck = duckdb.connect(":memory:")
    try:
        create_clickbench_hits_view(duck, parquet_path)
        duckdb_version = duck.execute("SELECT version()").fetchone()[0]

        summary: dict[str, dict] = {}
        rows_to_insert: list[tuple] = []
        for q in queries:
            err: str | None = None
            result_hash: str | None = None
            row_count: int | None = None
            serialized_rows: str | None = None
            serialized_columns: str | None = None
            started = time.perf_counter()
            try:
                cur = duck.execute(q.sql)
                rows = cur.fetchall()
                columns = [d[0] for d in cur.description] if cur.description else []
                row_count = len(rows)
                result_hash = _hash_result(rows)
                serialized_rows = serialize_rows(rows)
                serialized_columns = serialize_columns(columns)
            except Exception as e:
                err = str(e)
                logger.error("clickbench query failed", extra={"query_id": q.id})
            elapsed = (time.perf_counter() - started) * 1000

            summary[q.id] = {
                "result_hash": result_hash,
                "row_count": row_count,
                "execution_ms": elapsed,
                "error_message": err,
            }
            rows_to_insert.append(
                (
                    q.id,
                    "clickbench",
                    # ClickBench is the single ~100 GB scale; keyed as SF100
                    # consistently across data, recorded results, ground truth,
                    # and validate-sf1 (block_specs), so the key matches the scale
                    # under which ClickBench results are recorded.
                    100,
                    result_hash,
                    row_count,
                    elapsed,
                    err,
                    duckdb_version,
                    serialized_rows,
                    serialized_columns,
                )
            )

        results_db.record_ground_truth(rows_to_insert)
        ok = sum(1 for v in summary.values() if v["error_message"] is None)
        logger.info(
            "clickbench ground-truth complete: %d/%d ok",
            ok,
            len(summary),
        )
        return summary
    finally:
        duck.close()


def free_local_copy_after_stage(path: Path = Path("data/clickbench")) -> None:
    """Optionally delete the local 100 GB after staging to S3 (disk reclaim)."""
    if path.exists():
        shutil.rmtree(path)
        logger.info("removed local ClickBench dir %s", path)
