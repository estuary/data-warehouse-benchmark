"""bytes_scanned + platform_query_id persistence, and scale-skip keys."""

from __future__ import annotations

from benchmark.results import ResultsDB


def _record(db: ResultsDB, **over: object) -> None:
    base = {
        "platform": "bigquery",
        "query_id": "q",
        "block": "tpcds",
        "scale_factor": 1,
        "run_type": "warm",
        "run_number": 1,
        "wall_clock_ms": 10.0,
        "server_time_ms": 5.0,
        "row_count": 1,
        "cost_usd": 0.0,
        "cost_confidence": "direct",
        "error_message": None,
    }
    base.update(over)
    db.record_sql_result(**base)


def _scalar(db: ResultsDB, col: str, qid: str):
    with db.connect() as con:
        return con.execute(
            f"SELECT {col} FROM sql_benchmark_results WHERE query_id = ?", [qid]
        ).fetchone()[0]


def test_bytes_scanned_persists_when_reported(results_db):
    _record(results_db, query_id="t1", bytes_scanned=123456)
    assert _scalar(results_db, "bytes_scanned", "t1") == 123456


def test_bytes_scanned_null_when_absent(results_db):
    _record(results_db, query_id="t2")
    assert _scalar(results_db, "bytes_scanned", "t2") is None


def test_platform_query_id_persists(results_db):
    _record(results_db, query_id="t3", platform_query_id="job-xyz")
    assert _scalar(results_db, "platform_query_id", "t3") == "job-xyz"


# ── block-granularity skips ──────────────────────────────────────────────────
#
# A skip is often per-suite rather than whole-scale: ClickHouse's run plan skips DSB
# at 1 TB while TPC-DS runs there. A (scale, platform) key alone cannot express
# that, so stray DSB queries would render as thin coverage of a cell nobody
# intended to measure.


def test_a_skip_can_name_one_block(tmp_path):
    from benchmark.results import ResultsDB

    db = ResultsDB(tmp_path / "r.duckdb")
    db.record_scale_skip(1000, "clickhouse", "tpcds established the profile", block="dsb")

    with db.connect() as con:
        rows = con.execute("SELECT scale_factor, platform, block FROM scale_skips").fetchall()
    assert rows == [(1000, "clickhouse", "dsb")]


def test_per_block_skips_coexist_at_the_same_cell(tmp_path):
    """A (scale, platform) primary key would make these collide, keeping only the last."""
    from benchmark.results import ResultsDB

    db = ResultsDB(tmp_path / "r.duckdb")
    db.record_scale_skip(1000, "clickhouse", "reason A", block="dsb")
    db.record_scale_skip(1000, "clickhouse", "reason B", block="clickbench")

    with db.connect() as con:
        blocks = {r[0] for r in con.execute("SELECT block FROM scale_skips").fetchall()}
    assert blocks == {"dsb", "clickbench"}


def test_re_recording_the_same_cell_updates_rather_than_duplicates(tmp_path):
    from benchmark.results import ResultsDB

    db = ResultsDB(tmp_path / "r.duckdb")
    db.record_scale_skip(1000, "clickhouse", "first", block="dsb")
    db.record_scale_skip(1000, "clickhouse", "revised", block="dsb")

    with db.connect() as con:
        rows = con.execute("SELECT reason FROM scale_skips").fetchall()
    assert rows == [("revised",)]


def test_the_default_is_a_whole_scale_skip(tmp_path):
    from benchmark.results import ResultsDB

    db = ResultsDB(tmp_path / "r.duckdb")
    db.record_scale_skip(1, "fabric", "never benchmarked")

    with db.connect() as con:
        assert con.execute("SELECT block FROM scale_skips").fetchone() == ("*",)


def test_migration_widens_an_existing_table_and_preserves_its_rows(tmp_path):
    """Databases predating the column must migrate in place. Existing rows become
    whole-scale skips, which is what they meant — the key could not say otherwise."""
    import duckdb

    from benchmark.results import ResultsDB

    path = tmp_path / "old.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE scale_skips (
            scale_factor INTEGER NOT NULL, platform VARCHAR NOT NULL,
            reason VARCHAR NOT NULL, skipped_by VARCHAR,
            skipped_at TIMESTAMP DEFAULT now(),
            PRIMARY KEY (scale_factor, platform))
    """)
    con.execute("INSERT INTO scale_skips VALUES (1000, 'fabric', 'legacy', 'operator', now())")
    con.close()

    db = ResultsDB(path)

    with db.connect() as c:
        cols = {r[0] for r in c.execute("DESCRIBE scale_skips").fetchall()}
        rows = c.execute("SELECT scale_factor, platform, block, reason FROM scale_skips").fetchall()
    assert "block" in cols
    assert rows == [(1000, "fabric", "*", "legacy")]
    # And it must be a no-op the second time.
    ResultsDB(path)
    with db.connect() as c:
        assert c.execute("SELECT count(*) FROM scale_skips").fetchone() == (1,)
