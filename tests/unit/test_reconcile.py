"""Reconcile SQL builders (docs-validated) + ResultsDB round-trip."""

from __future__ import annotations

from datetime import datetime, timedelta

from benchmark.analysis.reconcile import (
    _RECONCILE_TZ_BUFFER_HOURS,
    RECONCILERS,
    _widen_lookback,
    bigquery_queue_sql,
    databricks_reconcile_sql,
    fabric_reconcile_sql,
    redshift_cost_sql,
    redshift_server_time_sql,
    snowflake_reconcile_sql,
)


def test_widen_lookback_covers_account_tz_offset():
    # The harness records created_at in local tz (e.g. CDT 23:19), but the engine
    # attributes the query in its account tz (PDT), ~2h earlier in naive terms. A 1h
    # buffer leaves it just outside the window (0 rows matched); the tz buffer must
    # push the start comfortably before any real account-tz offset.
    oldest = datetime.fromisoformat("2026-07-22T23:19:00")
    win = ("2026-07-22T23:19:00", "2026-07-24T00:00:00")
    start, end = _widen_lookback(win, oldest)
    assert datetime.fromisoformat(start) == oldest - timedelta(hours=_RECONCILE_TZ_BUFFER_HOURS)
    # Covers the observed 2h PDT-vs-CDT gap (and any realistic offset) with margin.
    assert datetime.fromisoformat(start) <= oldest - timedelta(hours=2)
    assert end == win[1]  # end unchanged


def test_widen_lookback_noop_when_window_already_earlier():
    oldest = datetime.fromisoformat("2026-07-23T12:00:00")
    win = ("2026-07-20T00:00:00", "2026-07-24T00:00:00")  # start already well before
    assert _widen_lookback(win, oldest) == win


def test_widen_lookback_accepts_iso_string_and_none():
    win = ("2026-07-22T23:19:00", "2026-07-24T00:00:00")
    from_str = _widen_lookback(win, "2026-07-22T23:19:00")
    assert datetime.fromisoformat(from_str[0]) == datetime.fromisoformat(
        "2026-07-22T23:19:00"
    ) - timedelta(hours=_RECONCILE_TZ_BUFFER_HOURS)
    assert _widen_lookback(win, None) == win  # nothing to reconcile → unchanged


def test_snowflake_sql_reads_attribution_history():
    sql = snowflake_reconcile_sql(["sfq-a", "sfq-b"], "2026-06-23T00:00:00")
    assert "QUERY_ATTRIBUTION_HISTORY" in sql
    assert "credits_attributed_compute" in sql and "credits_used_query_acceleration" in sql
    assert "'sfq-a'" in sql and "start_time >= '2026-06-23T00:00:00'" in sql


def test_databricks_sql_has_documented_tables():
    sql = databricks_reconcile_sql("2026-06-23T00:00:00", "2026-06-23T06:00:00", ["sid-a", "sid-b"])
    assert "system.query.history" in sql and "execution_duration_ms" in sql
    assert "system.billing.usage" in sql and "list_prices" in sql
    assert "'sid-a'" in sql


def test_databricks_sql_finds_the_warehouse_from_the_statements():
    # The warehouses come from the statements' own history rows, not the
    # connected warehouse, so a recreated workspace can reconcile statements
    # that ran on a deleted one (the system tables are account-wide per region).
    sql = databricks_reconcile_sql("2026-06-23T00:00:00", "2026-06-23T06:00:00", ["sid-a"])
    bench = sql[sql.index("WITH bench AS") : sql.index("hist AS")]
    assert "compute.warehouse_id AS wh" in bench and "statement_id IN ('sid-a')" in bench
    assert "compute.warehouse_id IN (SELECT wh FROM bench)" in sql
    assert "usage_metadata.warehouse_id IN (SELECT wh FROM bench)" in sql
    assert "compute.warehouse_id = '" not in sql


def test_databricks_share_denominator_covers_all_warehouse_statements():
    # The benchmark-id filter must apply AFTER the per-hour share is computed —
    # an id-filtered denominator attributes co-resident work (validate-sf1,
    # ad-hoc queries) to benchmark rows (enough to charge an SF1 warm pass $82).
    # The ids also pick the warehouses (bench), but hist keeps every statement
    # those warehouses ran.
    sql = databricks_reconcile_sql("2026-06-23T00:00:00", "2026-06-23T06:00:00", ["sid-a"])
    hist = sql[sql.index("hist AS") : sql.index("share AS")]
    assert "statement_id IN" not in hist
    assert sql.rindex("statement_id IN") > sql.index("share AS")


def test_redshift_server_time_sql_is_owner_visible():
    sql = redshift_server_time_sql([101, 102])
    assert "SYS_QUERY_HISTORY" in sql and "execution_time" in sql and "101" in sql
    # server-time query must NOT depend on the superuser-only usage view
    assert "SYS_SERVERLESS_USAGE" not in sql


def test_redshift_cost_sql_uses_serverless_usage():
    sql = redshift_cost_sql([101, 102], "2026-06-23T00:00:00", "2026-06-23T06:00:00")
    assert "SYS_SERVERLESS_USAGE" in sql and "charged_seconds" in sql and "102" in sql
    # Benchmark-id filter must come AFTER the per-minute bucket totals so
    # co-resident queries carry their own share of each charged minute.
    assert sql.index("query_id IN") > sql.index("bucket_total")


def test_fabric_sql_matches_on_the_label_column_not_command_text():
    """Fabric must key on the dedicated ``label`` column.

    Searching ``command`` for the label as a substring can never match: Fabric strips
    a leading block comment before storing ``command``, so every Fabric row would keep
    NULL server time while reconcile reported success. A ``login_name = USER_NAME()``
    filter excludes rows when a service principal runs the queries but USER_NAME()
    resolves to the workspace user.
    """
    sql = fabric_reconcile_sql("2026-09-25T10:00:00-05:00")
    assert "queryinsights.exec_requests_history" in sql
    assert "total_elapsed_time_ms" in sql and "allocated_cpu_time_ms" in sql
    # The lookback is the reconcile's (widened) window, in the view's UTC; a fixed
    # 24-hour lookback would miss rows reconciled two days after their run.
    assert "start_time >= '2026-09-25 15:00:00'" in sql
    assert "GROUP BY label" in sql
    assert "label" in sql
    assert "command" not in sql, "must not re-introduce the command-text scan"
    assert "USER_NAME()" not in sql, "must not re-introduce the login_name filter"


def test_reconcilers_cover_delayed_engines():
    # motherduck reconciles server time (batch from durable MD_QUERY_HISTORY());
    # bigquery reconciles queue time only; clickhouse's metrics are immediate.
    assert set(RECONCILERS) == {
        "bigquery",
        "snowflake",
        "databricks",
        "redshift",
        "fabric",
        "motherduck",
    }


def test_bigquery_queue_sql_reads_creation_to_start():
    sql = bigquery_queue_sql(["job_a", "job_b"], "US", "2026-08-01T00:00:00")
    assert "`region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT" in sql
    assert "TIMESTAMP_DIFF(start_time, creation_time, MILLISECOND)" in sql
    assert "creation_time >= TIMESTAMP('2026-08-01 00:00:00')" in sql  # partition prune
    assert "'job_a', 'job_b'" in sql


def test_bigquery_reconcile_chunks_ids_and_fills_queue_only(monkeypatch):
    from types import SimpleNamespace

    import benchmark.analysis.reconcile as rec
    from benchmark.connectors.base import QueryResult

    monkeypatch.setattr(rec, "BIGQUERY_IDS_PER_LOOKUP", 2)
    seen: list[str] = []

    def _execute(sql: str) -> QueryResult:
        seen.append(sql)
        ids = [part.strip("' ") for part in sql.split("IN (")[1].rstrip(")").split(",")]
        return QueryResult(
            rows=[(j, 1500) for j in ids], columns=[], row_count=len(ids), wall_clock_ms=1.0
        )

    conn = SimpleNamespace(region="us", execute_query=_execute)
    out = rec._reconcile_bigquery(conn, ["a", "b", "c"], window_iso=("2026-08-01", "x"), rates={})
    assert len(seen) == 2  # 3 ids at 2 per lookup
    assert {m.platform_query_id for m in out} == {"a", "b", "c"}
    assert all(m.queue_ms == 1500.0 and m.server_time_ms is None for m in out)
    assert all(m.cost_usd is None for m in out)  # cost stays the inline direct figure


def test_reconcile_motherduck_batches_server_time_and_metadata():
    from types import SimpleNamespace

    from benchmark.analysis.reconcile import _reconcile_motherduck

    captured = {}

    def _lookup(query_ids, *, settle_seconds):
        captured["ids"] = list(query_ids)
        captured["settle"] = settle_seconds
        return {
            "q-a": {
                "server_time_ms": 3.5,
                "start_time": "2026-07-01T00:00:00",
                "end_time": None,
                "bytes_scanned": 1024,
                "bytes_spilled": 512,
                "queue_ms": 1.2,
            },
            "q-b": {"server_time_ms": 7.0, "start_time": None, "end_time": None},
        }

    conn = SimpleNamespace(lookup_server_times=_lookup)
    metrics = _reconcile_motherduck(
        conn, ["q-a", "q-b"], window_iso=("", ""), rates={"motherduck_settle_seconds": 2}
    )
    by_id = {m.platform_query_id: m for m in metrics}
    assert captured["settle"] == 2  # settle override honored
    assert by_id["q-a"].server_time_ms == 3.5
    assert by_id["q-a"].cost_usd is None  # cost stays estimated (not reconciled)
    assert by_id["q-a"].metadata == {"start_time": "2026-07-01T00:00:00"}  # None end dropped
    assert by_id["q-a"].bytes_scanned == 1024  # profiling flows through
    assert by_id["q-a"].bytes_spilled == 512
    assert by_id["q-a"].queue_ms == 1.2
    assert by_id["q-b"].metadata is None  # no start/end -> no metadata patch
    assert by_id["q-b"].bytes_scanned is None  # absent profiling stays None


def test_update_reconciled_metrics_merges_metadata(results_db):
    import json

    results_db.record_sql_result(
        platform="motherduck",
        query_id="m1",
        block="tpcds",
        scale_factor=1,
        run_type="warm",
        run_number=1,
        wall_clock_ms=100.0,
        server_time_ms=None,
        row_count=1,
        cost_usd=0.0,
        cost_confidence="estimated",
        error_message=None,
        platform_query_id="mq-1",
        metadata={"component": "dw"},
    )
    results_db.update_reconciled_metrics(
        "mq-1",
        server_time_ms=3.5,
        cost_usd=None,
        cost_confidence=None,
        bytes_scanned=2048,
        bytes_spilled=64,
        queue_ms=0.9,
        metadata_extra={"start_time": "2026-07-01T00:00:00"},
    )
    with results_db.connect() as con:
        st, cc, meta, bs, sp, qm = con.execute(
            "SELECT server_time_ms, cost_confidence, metadata, bytes_scanned, "
            "bytes_spilled, queue_ms FROM sql_benchmark_results "
            "WHERE platform_query_id = 'mq-1'"
        ).fetchone()
    assert st == 3.5
    assert cc == "estimated"  # cost untouched (None passed → COALESCE keeps it)
    assert (bs, sp, qm) == (2048, 64, 0.9)  # profiling back-filled
    merged = json.loads(meta)
    assert merged == {
        "component": "dw",
        "start_time": "2026-07-01T00:00:00",
    }  # merged, not replaced


def test_reconcile_auto_widens_window_to_cover_old_rows(results_db, monkeypatch):
    """A fixed --window-hours must not silently drop runs older than it — reconcile
    widens the lookback to cover the oldest unreconciled row (a fixed 24h window
    would exclude days-old runs and undercount cost)."""
    from datetime import datetime, timedelta

    import benchmark.analysis.reconcile as rec

    # An 8-day-old snowflake row still needing reconciliation (estimated cost).
    # Naive datetime to match DuckDB's TIMESTAMP column (returned tz-naive).
    old = datetime(2026, 7, 1, 8, 0, 0)
    with results_db.connect() as con:
        con.execute(
            """
            INSERT INTO sql_benchmark_results
              (platform, query_id, block, scale_factor, run_type, run_number,
               cost_confidence, platform_query_id, created_at)
            VALUES ('snowflake','tpcds_q01','tpcds',10,'warm',1,'estimated','sfq-old', ?)
            """,
            [old],
        )

    assert results_db.oldest_reconcile_candidate("snowflake", 10, "warm") == old

    captured = {}

    def _fake(connector, pqids, *, window_iso, rates, collector=None):
        captured["start"] = window_iso[0]
        return []

    monkeypatch.setitem(rec.RECONCILERS, "snowflake", _fake)

    now = datetime(2026, 7, 9)  # 8 days after the run
    narrow = ((now - timedelta(hours=24)).isoformat(), now.isoformat())
    rec.reconcile_platform(
        "snowflake",
        connector=None,
        results_db=results_db,
        scale_factor=10,
        run_type="warm",
        window_iso=narrow,
    )
    # window start was pushed back before the 8-day-old run, not left at now-24h
    assert captured["start"] < narrow[0]
    assert captured["start"] <= old.isoformat()


def test_rows_to_reconcile_and_update_terminates(results_db):
    results_db.record_sql_result(
        platform="databricks",
        query_id="t1",
        block="tpcds",
        scale_factor=1000,
        run_type="warm",
        run_number=1,
        wall_clock_ms=100.0,
        server_time_ms=None,
        row_count=1,
        cost_usd=0.5,
        cost_confidence="estimated",
        error_message=None,
        platform_query_id="sid-1",
    )
    assert results_db.rows_to_reconcile("databricks", 1000, "warm") == ["sid-1"]

    results_db.update_reconciled_metrics(
        "sid-1", server_time_ms=42.0, cost_usd=1.25, cost_confidence="proportional"
    )
    with results_db.connect() as con:
        st, cost = con.execute(
            "SELECT server_time_ms, cost_usd FROM sql_benchmark_results "
            "WHERE platform_query_id = 'sid-1'"
        ).fetchone()
    assert st == 42.0 and cost == 1.25
    # back-filled row no longer needs reconciliation (the filter terminates)
    assert results_db.rows_to_reconcile("databricks", 1000, "warm") == []


# ── reconcile must be able to cover a whole campaign ─────────────────────────
#
# `reconcile`'s --scale/--run-type default to a single (1000, warm) cell, which is
# right for a targeted back-fill and wrong after a multi-cell campaign: a bare
# `reconcile --platforms databricks` would reconcile one cell and report success.
# Databricks does not collect server time inline, so the rest of its sizing curve
# would be missing from the report while nothing failed.


def _row(db, **kw):
    base = dict(
        platform="databricks",
        query_id="tpcds_q01",
        block="tpcds",
        scale_factor=100,
        run_type="warm",
        run_number=1,
        wall_clock_ms=10.0,
        server_time_ms=None,
        row_count=1,
        cost_usd=1.0,
        cost_confidence="proportional",
        error_message=None,
        bytes_scanned=1,
        platform_query_id="stmt-1",
    )
    base.update(kw)
    db.record_sql_result(**base)


def test_unreconciled_cells_finds_every_cell_a_campaign_wrote(results_db):
    _row(results_db, scale_factor=100, run_type="warm", platform_query_id="a")
    _row(results_db, scale_factor=1, run_type="concurrency", platform_query_id="b")
    _row(results_db, scale_factor=1000, run_type="concurrency", platform_query_id="c")

    cells = results_db.unreconciled_cells("databricks")

    assert cells == [(1, "concurrency"), (100, "warm"), (1000, "concurrency")]


def test_a_fully_reconciled_cell_is_not_returned(results_db):
    """Otherwise --all-cells would re-walk settled cells on every pass."""
    _row(
        results_db,
        scale_factor=10,
        run_type="warm",
        platform_query_id="done",
        server_time_ms=5.0,
        cost_usd=2.0,
        cost_confidence="proportional",
    )

    assert results_db.unreconciled_cells("databricks") == []
    # ...until queue time is asked for: that cell has none yet.
    assert results_db.unreconciled_cells("databricks", missing_queue=True) == [(10, "warm")]


def test_rows_without_a_platform_query_id_are_not_cells(results_db):
    """Reconcile joins on the engine-side id; a row without one can never be
    back-filled, so listing its cell would loop forever on an unfixable row."""
    _row(results_db, scale_factor=10, run_type="warm", platform_query_id=None)

    assert results_db.unreconciled_cells("databricks") == []


def test_cells_are_scoped_to_the_platform(results_db):
    _row(results_db, platform="databricks", scale_factor=100, platform_query_id="d")
    _row(results_db, platform="snowflake", scale_factor=1, platform_query_id="s")

    assert results_db.unreconciled_cells("databricks") == [(100, "warm")]
    assert results_db.unreconciled_cells("snowflake") == [(1, "warm")]
