"""prepare-scale plan pass: data + compute axes, resiliency, board.

Uses fakes — no cloud. Compute axis runs against the real run-plan targets with a
fake connector; data axis stubs validate_engine_loads.
"""

from __future__ import annotations

from benchmark import prepare_scale as ps


class _FakeConnector:
    def __init__(self, *, live=None, raises=False) -> None:
        self._live = live or {}
        self._raises = raises

    def __enter__(self):
        if self._raises:
            raise RuntimeError("Network is unreachable")
        return self

    def __exit__(self, *exc):
        return None

    def runtime_compute(self):
        return self._live


# ---- compute axis (real run-plan targets) --------------------------------


def test_compute_axis_matched(monkeypatch):
    # run-plan snowflake[1000] == LARGE; live warehouse size LARGE -> ready.
    monkeypatch.setattr(
        ps, "create_connector", lambda p, config=None: _FakeConnector(live={"size": "LARGE"})
    )
    r = ps.compute_axis("snowflake", 1000)
    assert r.status == ps.READY


def test_compute_axis_mismatch(monkeypatch):
    # run-plan motherduck[1000] == giga; live tier Standard -> pending.
    monkeypatch.setattr(
        ps, "create_connector", lambda p, config=None: _FakeConnector(live={"size": "Standard"})
    )
    r = ps.compute_axis("motherduck", 1000)
    assert r.status == ps.PENDING
    assert "giga" in r.detail


def test_compute_axis_skip_when_no_cell(monkeypatch):
    # no run-plan cell for the platform -> skip, and no connector is built.
    called = []
    monkeypatch.setattr(ps, "create_connector", lambda *a, **k: called.append(1))
    r = ps.compute_axis("nonexistent", 1000)
    assert r.status == ps.SKIP
    assert not called


def test_compute_axis_unreachable(monkeypatch):
    monkeypatch.setattr(ps, "create_connector", lambda p, config=None: _FakeConnector(raises=True))
    r = ps.compute_axis("motherduck", 1000)
    assert r.status == ps.UNREACHABLE


def test_compute_axis_bigquery_on_demand_allowed_ready(monkeypatch):
    # SF100: on_demand=allow -> on-demand (no reservation) is a valid billing mode, ready.
    monkeypatch.setattr(ps, "create_connector", lambda p, config=None: _FakeConnector(live={}))
    r = ps.compute_axis("bigquery", 100)
    assert r.status == ps.READY
    assert "on-demand" in r.detail and "bigquery_reservation_enabled" in r.detail


def test_compute_axis_bigquery_on_demand_denied_pending(monkeypatch):
    # SF1000: on_demand=deny -> on-demand flagged pending; must reserve slots (500 @ SF1000).
    monkeypatch.setattr(ps, "create_connector", lambda p, config=None: _FakeConnector(live={}))
    r = ps.compute_axis("bigquery", 1000)
    assert r.status == ps.PENDING
    assert "not allowed" in r.detail and "500" in r.detail


def test_compute_axis_bigquery_reservation_verified(monkeypatch):
    # When a reservation IS live, verify its slot count against the run-plan (500 @ SF1000).
    monkeypatch.setattr(
        ps, "create_connector", lambda p, config=None: _FakeConnector(live={"slot_capacity": 500})
    )
    assert ps.compute_axis("bigquery", 1000).status == ps.READY


def test_compute_axis_unverified_when_numeric_target_and_live_empty(monkeypatch):
    # redshift target is numeric (64 @ SF1000); empty live -> not comparable -> unverified.
    monkeypatch.setattr(ps, "create_connector", lambda p, config=None: _FakeConnector(live={}))
    r = ps.compute_axis("redshift", 1000)
    assert r.status == ps.PENDING
    assert "unverified" in r.detail


def test_assess_compute_only_skips_data(monkeypatch):
    monkeypatch.setattr(
        ps, "create_connector", lambda p, config=None: _FakeConnector(live={"size": "giga"})
    )

    def _boom(*a, **k):
        raise AssertionError("data axis must not run under --compute-only")

    monkeypatch.setattr(ps, "validate_engine_loads", _boom)
    [r] = ps.assess(["motherduck"], 1000, check_data=False)
    assert r.data.status == ps.SKIP
    assert r.overall == ps.READY  # compute matched (giga), data intentionally skipped


# ---- data axis -----------------------------------------------------------


def test_data_axis_ready(monkeypatch):
    monkeypatch.setattr(ps, "validate_engine_loads", lambda p, s, suites=None, on_table=None: [])
    assert ps.data_axis("motherduck", 1).status == ps.READY


def test_data_axis_pending(monkeypatch):
    monkeypatch.setattr(
        ps,
        "validate_engine_loads",
        lambda p, s, suites=None, on_table=None: ["motherduck/dsb store: got 0, expected 100"],
    )
    r = ps.data_axis("motherduck", 1000)
    # Full first-failure message preserved (no truncation); no "+N more" for a single issue.
    assert r.status == ps.PENDING
    assert "got 0, expected 100" in r.detail and "more" not in r.detail


def test_data_axis_pending_multiple_shows_more_count(monkeypatch):
    monkeypatch.setattr(
        ps,
        "validate_engine_loads",
        lambda p, s, suites=None, on_table=None: ["first issue", "second", "third"],
    )
    r = ps.data_axis("motherduck", 1000)
    assert r.status == ps.PENDING
    assert r.detail.startswith("first issue") and "(+2 more)" in r.detail


def test_data_axis_unreachable(monkeypatch):
    monkeypatch.setattr(
        ps,
        "validate_engine_loads",
        lambda p, s, suites=None, on_table=None: ["motherduck/tpcds: connect failed — boom"],
    )
    assert ps.data_axis("motherduck", 1).status == ps.UNREACHABLE


def test_data_axis_unknown_when_manifest_missing(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("expected_counts.json")

    monkeypatch.setattr(ps, "validate_engine_loads", boom)
    assert ps.data_axis("motherduck", 1).status == ps.UNKNOWN


# ---- assess + board ------------------------------------------------------


def test_assess_and_board(monkeypatch):
    monkeypatch.setattr(
        ps, "create_connector", lambda p, config=None: _FakeConnector(live={"size": "giga"})
    )
    monkeypatch.setattr(ps, "validate_engine_loads", lambda p, s, suites=None, on_table=None: [])
    results = ps.assess(["motherduck", "fabric"], 1000)

    by = {r.platform: r for r in results}
    assert by["motherduck"].overall == ps.READY  # live giga == target giga, data loaded
    # fabric@1000 has a run-plan cell (F128) but the fake live lacks capacity_sku -> pending.
    assert by["fabric"].overall == ps.PENDING

    from rich.console import Console

    console = Console(width=200, record=True)
    console.print(ps.build_board_table(1000, results))
    board = console.export_text()
    assert "SF1000 readiness" in board
    assert "motherduck" in board and "ready" in board


# ---- remediation + sizing JSON -------------------------------------------


def _pending_compute(platform):
    return ps.PlatformReadiness(
        platform,
        data=ps.AxisResult(ps.READY, "loaded"),
        compute=ps.AxisResult(ps.PENDING, "off-target"),
    )


def test_remediation_tfvars_for_bigquery_denied_scale():
    # SF1000: on_demand=deny -> reserve slots. Two sizing-var edits (capacity + enable),
    # not a manual step. A slot count alone won't take effect without enabling.
    plan = ps.remediation_plan([_pending_compute("bigquery")], 1000, sizing={})
    by_var = {e.tfvar: e for e in plan.tfvar_edits}
    assert set(by_var) == {"bigquery_reservation_slot_capacity", "bigquery_reservation_enabled"}
    assert by_var["bigquery_reservation_slot_capacity"].new == 500  # run-plan bigquery[1000]
    assert by_var["bigquery_reservation_enabled"].new is True
    assert not plan.manual_steps


def test_remediation_no_tfvars_for_bigquery_allowed_scale():
    # SF100: on_demand=allow -> never write reservation tfvars, even if compute is pending.
    plan = ps.remediation_plan([_pending_compute("bigquery")], 100, sizing={})
    assert not plan.tfvar_edits
    assert not plan.manual_steps


def test_remediation_out_of_band_motherduck():
    plan = ps.remediation_plan([_pending_compute("motherduck")], 1000, sizing={})
    assert not plan.tfvar_edits
    assert any("Duckling tier" in s and "giga" in s for s in plan.manual_steps)


def test_remediation_preprovisioned_snowflake():
    plan = ps.remediation_plan([_pending_compute("snowflake")], 1000, sizing={})
    assert not plan.tfvar_edits
    assert any("BENCHMARK_WH_LARGE" in s for s in plan.manual_steps)


def test_remediation_redshift_wired_emits_tfvar_edit():
    # redshift_baseline_rpu is a root tfvar: a pending redshift yields a sizing
    # edit (run_plan SF1000 -> 64 RPU), not a "needs a terraform change" manual
    # step.
    plan = ps.remediation_plan([_pending_compute("redshift")], 1000, sizing={})
    [edit] = plan.tfvar_edits
    assert edit.tfvar == "redshift_baseline_rpu"
    assert edit.new == 64
    assert not any("no root tfvar" in s for s in plan.manual_steps)


def test_remediation_data_missing_suite_load_chain():
    # A "not found" failure -> a tailored load chain for that suite.
    r = ps.PlatformReadiness(
        "bigquery",
        data=ps.AxisResult(
            ps.PENDING,
            "detail",
            failures=["bigquery/clickbench hits: count_table_rows failed: 404 Not found"],
        ),
        compute=ps.AxisResult(ps.READY, "ok"),
    )
    [step] = ps.remediation_plan([r], 1, sizing={}).data_steps
    assert "bigquery / clickbench" in step and "not loaded" in step
    # ClickBench is single-scale: includes the typed-derive step, and no --scale.
    assert "download-clickbench" in step and "derive-clickbench-typed" in step
    assert "validate-loads --suites clickbench" in step and "--scale" not in step


def test_remediation_data_zero_rows_is_backfill():
    # got 0 -> the binding delivered nothing; remedy is to trigger a backfill,
    # distinct from a partial load.
    r = ps.PlatformReadiness(
        "motherduck",
        data=ps.AxisResult(
            ps.PENDING,
            "detail",
            failures=["motherduck/dsb store: got 0, expected 100 (delta -100)"],
        ),
        compute=ps.AxisResult(ps.READY, "ok"),
    )
    [step] = ps.remediation_plan([r], 1000, sizing={}).data_steps
    assert "motherduck / dsb" in step and "0 rows" in step and "backfill" in step


def test_remediation_data_partial_load_reports_pct():
    # 0 < got < expected -> partial/incomplete backfill, with the percentage.
    r = ps.PlatformReadiness(
        "snowflake",
        data=ps.AxisResult(
            ps.PENDING,
            "detail",
            failures=[
                "snowflake/clickbench hits: got 940,032, expected 99,997,497 (delta -99,057,465)"
            ],
        ),
        compute=ps.AxisResult(ps.READY, "ok"),
    )
    [step] = ps.remediation_plan([r], 1, sizing={}).data_steps
    assert "partial load" in step and "0.9%" in step and "backfill" in step


def test_remediation_data_over_count_is_duplicate():
    # got > expected -> duplicate load; remedy is truncate + re-materialize.
    r = ps.PlatformReadiness(
        "redshift",
        data=ps.AxisResult(
            ps.PENDING,
            "detail",
            failures=["redshift/dsb store: got 200, expected 100 (delta 100)"],
        ),
        compute=ps.AxisResult(ps.READY, "ok"),
    )
    [step] = ps.remediation_plan([r], 1, sizing={}).data_steps
    assert "over-count" in step and "duplicate" in step


def test_cross_engine_correlation_leads_data_steps():
    # The same table short on >=2 engines -> one upstream-cause note, first.
    def short(plat):
        return ps.PlatformReadiness(
            plat,
            data=ps.AxisResult(
                ps.PENDING,
                "detail",
                failures=[
                    f"{plat}/clickbench hits: got 0, expected 99,997,497 (delta -99,997,497)"
                ],
            ),
            compute=ps.AxisResult(ps.READY, "ok"),
        )

    results = [short("snowflake"), short("motherduck"), short("bigquery")]
    steps = ps.remediation_plan(results, 1, sizing={}).data_steps
    assert "short on 3/3 engines" in steps[0]
    assert "clickbench/hits" in steps[0] and "data/clickbench/hits.parquet" in steps[0]
    # The correlation note leads; per-engine detail still follows.
    assert any("snowflake / clickbench" in s for s in steps[1:])


def test_cross_engine_note_uses_staged_check():
    # An injected staged_check verdict is appended to the correlation note
    # (prepare-scale checks the source instead of telling the operator to).
    def short(plat):
        return ps.PlatformReadiness(
            plat,
            data=ps.AxisResult(
                ps.PENDING, "d", failures=[f"{plat}/clickbench hits: got 0, expected 99,997,497"]
            ),
            compute=ps.AxisResult(ps.READY, "ok"),
        )

    from benchmark.staging.s3 import StagedVerdict

    calls = []

    def fake_check(suite, table, scale):
        calls.append((suite, table, scale))
        return StagedVerdict(False, "STALE: S3 has 92 MiB but local is 8.2 GiB — re-stage")

    steps = ps.remediation_plan(
        [short("snowflake"), short("bigquery")], 1, sizing={}, staged_check=fake_check
    ).data_steps
    # The S3 read happens once per (suite, table), not once per engine.
    assert calls == [("clickbench", "hits", 1)]
    assert "Staged object: STALE" in steps[0] and "re-stage" in steps[0]
    # No "Check <path> before re-loading" boilerplate when we actually checked.
    assert "before re-loading" not in steps[0]


def test_confirmed_source_trims_not_loaded_to_downstream():
    # When the staged check confirms the source is good, a "not loaded" engine's
    # remedy collapses to downstream-only — no download/derive/stage prefix.
    from benchmark.staging.s3 import StagedVerdict

    r = ps.PlatformReadiness(
        "bigquery",
        data=ps.AxisResult(ps.PENDING, "d", failures=["bigquery/clickbench hits: 404 Not found"]),
        compute=ps.AxisResult(ps.READY, "ok"),
    )

    def ok_check(suite, table, scale):
        return StagedVerdict(True, "matches local within encoding variance — gap is downstream")

    [step] = ps.remediation_plan([r], 1, sizing={}, staged_check=ok_check).data_steps
    assert "not materialized here (source staged OK)" in step
    assert "flowctl apply (bigquery clickbench)" in step
    # The regenerate prefix is gone.
    assert "download-clickbench" not in step and "derive-clickbench-typed" not in step


def test_remediation_data_per_suite():
    # Two suites failing different ways -> one tailored step each.
    r = ps.PlatformReadiness(
        "bigquery",
        data=ps.AxisResult(
            ps.PENDING,
            "detail",
            failures=[
                "bigquery/clickbench hits: 404 Not found",
                "bigquery/dsb store: got 0, expected 100",
            ],
        ),
        compute=ps.AxisResult(ps.READY, "ok"),
    )
    steps = ps.remediation_plan([r], 1, sizing={}).data_steps
    assert len(steps) == 2
    assert any("clickbench" in s and "not loaded" in s for s in steps)
    assert any("dsb" in s and "0 rows" in s for s in steps)


def test_remediation_data_unknown_is_generate():
    r = ps.PlatformReadiness(
        "motherduck",
        data=ps.AxisResult(ps.UNKNOWN, "expected-counts manifest unavailable: no entry for SF1000"),
        compute=ps.AxisResult(ps.READY, "ok"),
    )
    [step] = ps.remediation_plan([r], 1000, sizing={}).data_steps
    assert "not generated" in step and "generate-tpcds --scales 1000" in step


def test_tfvar_edit_is_noop_when_unchanged():
    e = ps.TfvarEdit("bigquery", "bigquery_reservation_slot_capacity", old=500, new=500)
    assert e.is_noop
    assert not ps.TfvarEdit("bigquery", "x", old=100, new=500).is_noop


def test_write_sizing_edits_round_trip(tmp_path):
    import json

    path = tmp_path / "sizing.auto.tfvars.json"
    path.write_text('{"existing_var": "keep"}\n')
    edits = [
        ps.TfvarEdit("bigquery", "bigquery_reservation_slot_capacity", None, 500),
        ps.TfvarEdit("fabric", "fabric_capacity_sku", "F32", "F128"),
    ]
    ps.write_sizing_edits(edits, path)

    on_disk = json.loads(path.read_text())
    assert on_disk == {
        "existing_var": "keep",  # untouched keys preserved
        "bigquery_reservation_slot_capacity": 500,  # int stays int
        "fabric_capacity_sku": "F128",  # string stays string
    }


# ---- Estuary collection axis (fakes; no flowctl) -------------------------

from benchmark.staging import estuary as est_mod  # noqa: E402


def _cstate(name, *, present=True, journal_bytes=100, last_write=1000):
    return est_mod.CollectionState(name, present, journal_bytes, last_write)


def test_estuary_axis_ready(monkeypatch):
    monkeypatch.setattr(ps, "_expected_tables", lambda s, sc, dr=None: ["a", "b"])
    monkeypatch.setattr(est_mod, "collection_name", lambda s, sc, t: f"acct/{s}/sf{sc}/{t}")
    monkeypatch.setattr(est_mod, "collection_states", lambda names: [_cstate(n) for n in names])
    r = ps.estuary_axis("tpcds", 10)
    assert r.status == ps.READY
    assert "2/2 non-empty" in r.detail


def test_estuary_axis_missing_collection(monkeypatch):
    monkeypatch.setattr(ps, "_expected_tables", lambda s, sc, dr=None: ["a", "b"])
    monkeypatch.setattr(est_mod, "collection_name", lambda s, sc, t: f"x/{t}")
    monkeypatch.setattr(
        est_mod,
        "collection_states",
        lambda names: [_cstate(names[0]), _cstate(names[1], present=False, journal_bytes=0)],
    )
    r = ps.estuary_axis("tpcds", 10)
    assert r.status == ps.PENDING and "missing" in r.detail


def test_estuary_axis_empty_collection(monkeypatch):
    monkeypatch.setattr(ps, "_expected_tables", lambda s, sc, dr=None: ["a", "b"])
    monkeypatch.setattr(est_mod, "collection_name", lambda s, sc, t: f"x/{t}")
    monkeypatch.setattr(
        est_mod,
        "collection_states",
        lambda names: [_cstate(names[0]), _cstate(names[1], journal_bytes=0)],
    )
    r = ps.estuary_axis("tpcds", 10)
    assert r.status == ps.PENDING and "empty" in r.detail


def test_estuary_axis_unknown_without_manifest(monkeypatch):
    monkeypatch.setattr(ps, "_expected_tables", lambda s, sc, dr=None: None)
    r = ps.estuary_axis("tpcds", 10)
    assert r.status == ps.UNKNOWN


def test_assess_sources_estuary_auth_failure_is_unreachable(monkeypatch):
    def _boom():
        raise RuntimeError("JWT expired")

    monkeypatch.setattr(est_mod, "auth", _boom)
    rows = ps.assess_sources(("tpcds", "dsb"), 10, check_s3=False, check_estuary=True)
    assert [r.estuary.status for r in rows] == [ps.UNREACHABLE, ps.UNREACHABLE]
    assert all(r.s3 is None for r in rows)  # s3 disabled -> not run


# ---- S3 source axis (footers vs manifest; fakes, no S3) ------------------

import types  # noqa: E402


class _Secret:
    def __init__(self, v):
        self._v = v

    def get_secret_value(self):
        return self._v


def _fake_settings(*, keys=True):
    est = types.SimpleNamespace(
        S3_AWS_ACCESS_KEY_ID=_Secret("k") if keys else None,
        S3_AWS_SECRET_ACCESS_KEY=_Secret("s") if keys else None,
    )
    return types.SimpleNamespace(
        ESTUARY=est, AWS=types.SimpleNamespace(REGION="us-east-1"), S3_BUCKET="bkt"
    )


def _patch_s3(monkeypatch, *, plan, rows, keys=True):
    monkeypatch.setattr("benchmark.staging.load_check._suite_plan", plan)
    monkeypatch.setattr("benchmark.settings.get_settings", lambda: _fake_settings(keys=keys))
    monkeypatch.setattr(
        "benchmark.staging.expected_counts.make_s3_filesystem", lambda r, k, s: object()
    )
    monkeypatch.setattr("benchmark.staging.expected_counts.parquet_footer_rows", rows)


def test_s3_source_axis_ready(monkeypatch):
    _patch_s3(
        monkeypatch,
        plan=lambda suite, scale, dr: ("tpcds_sf10", {"a": 10, "b": 20}),
        rows=lambda s3, path: 10 if path.endswith("a.parquet") else 20,
    )
    r = ps.s3_source_axis("tpcds", 10)
    assert r.status == ps.READY and "match" in r.detail


def test_s3_source_axis_mismatch(monkeypatch):
    _patch_s3(
        monkeypatch,
        plan=lambda suite, scale, dr: ("tpcds_sf10", {"a": 10, "b": 20}),
        rows=lambda s3, path: 10 if path.endswith("a.parquet") else 19,  # b short by 1
    )
    r = ps.s3_source_axis("tpcds", 10)
    assert r.status == ps.PENDING and "mismatch" in r.detail


def test_s3_source_axis_missing_object(monkeypatch):
    def _rows(s3, path):
        if path.endswith("b.parquet"):
            raise FileNotFoundError("nope")
        return 10

    _patch_s3(
        monkeypatch, plan=lambda suite, scale, dr: ("tpcds_sf10", {"a": 10, "b": 20}), rows=_rows
    )
    r = ps.s3_source_axis("tpcds", 10)
    assert r.status == ps.PENDING and "missing" in r.detail


def test_s3_source_axis_unknown_without_manifest(monkeypatch):
    def _boom(suite, scale, dr):
        raise KeyError(f"no entry for SF{scale}")

    _patch_s3(monkeypatch, plan=_boom, rows=lambda s3, path: 0)
    r = ps.s3_source_axis("tpcds", 10)
    assert r.status == ps.UNKNOWN


def test_s3_source_axis_unreachable_without_keys(monkeypatch):
    _patch_s3(
        monkeypatch,
        plan=lambda suite, scale, dr: ("tpcds_sf10", {"a": 10}),
        rows=lambda s3, path: 10,
        keys=False,
    )
    r = ps.s3_source_axis("tpcds", 10)
    assert r.status == ps.UNREACHABLE
