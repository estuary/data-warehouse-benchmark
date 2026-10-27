"""Capture collection injection: define-once publish, ClickBench drop guard.

`_inject_capture_collections` ensures every capture-binding target has a
collection definition. The one binding it can't define — ClickBench when its
local typed `hits.parquet` is absent — is *dropped* rather than left dangling
(flowctl rejects a capture referencing an undefined collection, which would
otherwise block the whole spec). TPC-DS/DSB targets are always definable, so
they're kept. We publish the full spec; presence of a scale's data is a runtime
fact checked in the target, not encoded by pruning bindings here.
"""

from __future__ import annotations

import base64
import json
import time
import types
from pathlib import Path

import pytest
import yaml

import benchmark.staging.estuary as est

ACCT = "CorrDyn"


def test_clean_strips_ansi_and_whitespace():
    # flowctl emits ANSI even when piped; _clean keeps the message, drops codes,
    # so the JSON log file and error strings stay plain text.
    raw = "\x1b[2m2026-06-25T19:29:10Z\x1b[0m \x1b[31mERROR\x1b[0m requires data plane abc, not found\n"
    cleaned = est._clean(raw)
    assert cleaned == "2026-06-25T19:29:10Z ERROR requires data plane abc, not found"
    assert "\x1b" not in cleaned


def test_digest_collapses_identical_data_plane_wall():
    # 3 collections, same root cause -> one group with count 3.
    lines = [
        f"2026-06-25T19:29:10Z ERROR flowctl::local_specs: scope=file:///x.yaml#/collections/CorrDyn~1benchmark~1{s} error=collection CorrDyn/benchmark/{s} requires data plane 129baaf7ca04c400, which was not found"
        for s in ("clickbench~1hits", "dsb~1sf1~1call_center", "tpcds~1sf1~1item")
    ]
    digest = est._digest_flowctl_errors("\n".join(lines))
    assert "3 error(s), 1 distinct" in digest
    assert "3× [collection] requires data plane 129baaf7ca04c400, which was not found" in digest
    # collection name is masked out of the fingerprint, surfaced as an example
    assert "<name>" not in digest.split("e.g.")[0]  # masked phrase has no literal path
    assert "e.g. CorrDyn/benchmark/clickbench/hits" in digest


def test_digest_groups_distinct_causes_and_masks_ips():
    lines = [
        "t ERROR flowctl::draft: scope=flow://capture/CorrDyn/benchmark/captures/source-s3 detail=configured AWS Secret Access Key is not valid",
        "t ERROR flowctl::draft: scope=flow://materialization/CorrDyn/benchmark/materializations/snowflake-clickbench detail=Incoming request with IP/Token 107.20.68.5 is not allowed to access Snowflake",
        "t ERROR flowctl::draft: scope=flow://materialization/CorrDyn/benchmark/materializations/snowflake-dsb-sf1 detail=Incoming request with IP/Token 98.89.112.85 is not allowed to access Snowflake",
    ]
    digest = est._digest_flowctl_errors("\n".join(lines))
    assert "3 error(s), 2 distinct" in digest
    # the two different IPs collapse into one group via <ip> masking
    assert (
        "2× [materialization] Incoming request with IP/Token <ip> is not allowed to access Snowflake"
        in digest
    )
    assert "1× [capture] configured AWS Secret Access Key is not valid" in digest


def test_digest_falls_back_when_unrecognized():
    raw = "some totally unexpected flowctl output\nwith no parseable errors"
    assert est._digest_flowctl_errors(raw) == raw


def _doc() -> dict:
    def b(target: str) -> dict:
        return {"resource": {"stream": "s3/x.parquet"}, "target": target}

    return {
        "captures": {
            f"{ACCT}/benchmark/captures/source-s3": {
                "bindings": [
                    b(f"{ACCT}/benchmark/tpcds/sf1/store"),
                    b(f"{ACCT}/benchmark/dsb/sf1/item"),
                    b(f"{ACCT}/benchmark/clickbench/hits"),
                ]
            }
        }
    }


def _targets(doc: dict) -> set[str]:
    cap = next(iter(doc["captures"].values()))
    return {b["target"] for b in cap["bindings"]}


def test_clickbench_binding_dropped_when_schema_undefinable(monkeypatch):
    # Force the no-typed-schema path: with build returning {}, TPC-DS/DSB fall
    # back to passthrough and ClickBench is dropped — hermetic regardless of
    # ESTUARY_ACCOUNT (account truthy -> patched build; falsy -> build skipped).
    monkeypatch.setattr(
        "benchmark.datagen.collection_schemas.build_collection_schemas",
        lambda *a, **k: {},
    )

    doc = _doc()
    injected, dropped = est._inject_capture_collections(doc)

    assert dropped == 1
    assert f"{ACCT}/benchmark/clickbench/hits" not in _targets(doc)
    # TPC-DS/DSB bindings survive (defined via passthrough here) so the full
    # capture still publishes.
    assert _targets(doc) == {
        f"{ACCT}/benchmark/tpcds/sf1/store",
        f"{ACCT}/benchmark/dsb/sf1/item",
    }
    assert injected == 2  # the two kept targets each got a collection def


def test_existing_collection_targets_are_left_intact(monkeypatch):
    # A target already present in collections is kept without re-injection.
    # Force the no-typed-schema path: with build returning {}, TPC-DS/DSB fall
    # back to passthrough and ClickBench is dropped — hermetic regardless of
    # ESTUARY_ACCOUNT (account truthy -> patched build; falsy -> build skipped).
    monkeypatch.setattr(
        "benchmark.datagen.collection_schemas.build_collection_schemas",
        lambda *a, **k: {},
    )

    doc = _doc()
    doc["collections"] = {f"{ACCT}/benchmark/tpcds/sf1/store": {"schema": {}, "key": ["/x"]}}
    injected, dropped = est._inject_capture_collections(doc)

    # store already defined (not re-injected), dsb injected, clickbench dropped.
    assert injected == 1
    assert dropped == 1
    assert f"{ACCT}/benchmark/tpcds/sf1/store" in _targets(doc)


# ---- Estuary token guard (refresh-token vs access-token JWT) ---------------


def _make_jwt(exp: float) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'HS256', 'typ': 'JWT'})}.{seg({'exp': exp})}.sig"


def test_jwt_expiry_detects_access_token():
    exp = time.time() + 3600
    assert est._jwt_expiry(_make_jwt(exp)) == pytest.approx(exp, abs=1)


def test_jwt_expiry_none_for_opaque_refresh_token():
    # A real Estuary refresh token is an opaque {id,secret} blob, not a JWT.
    blob = base64.b64encode(json.dumps({"id": "x", "secret": "y"}).encode()).decode()
    assert est._jwt_expiry(blob) is None


def test_auth_rejects_expired_access_token(monkeypatch):
    monkeypatch.setattr(est.subprocess, "run", lambda *a, **k: pytest.fail("must not shell out"))
    with pytest.raises(est.FlowctlError, match="EXPIRED access-token JWT"):
        est.auth(token=_make_jwt(time.time() - 60))


def test_auth_warns_but_proceeds_for_valid_access_token(monkeypatch):
    calls = []
    monkeypatch.setattr(
        est.subprocess,
        "run",
        lambda *a, **k: (
            calls.append(1) or types.SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )
    est.auth(token=_make_jwt(time.time() + 3600))
    assert calls  # a still-valid access token works for now, so auth is attempted


def test_auth_missing_token_raises(monkeypatch):
    monkeypatch.setattr(
        est,
        "get_settings",
        lambda: types.SimpleNamespace(ESTUARY=types.SimpleNamespace(REFRESH_TOKEN=None)),
    )
    with pytest.raises(est.FlowctlError, match="ESTUARY_REFRESH_TOKEN missing"):
        est.auth()


def test_flowctl_env_sets_flow_auth_token():
    env = est._flowctl_env(token="rt-abc")
    assert env["FLOW_AUTH_TOKEN"] == "rt-abc"
    assert "PATH" in env  # inherits the process env


def test_auth_uses_flow_auth_token_not_auth_token_subcommand(monkeypatch):
    """Refresh tokens authenticate via the FLOW_AUTH_TOKEN env on a normal command
    (roles list) — NOT `flowctl auth token --token`, which only takes a JWT."""
    seen = {}

    def _fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        seen["token_in_env"] = (k.get("env") or {}).get("FLOW_AUTH_TOKEN")
        return types.SimpleNamespace(returncode=0, stdout="capability: admin", stderr="")

    monkeypatch.setattr(est.subprocess, "run", _fake_run)
    est.auth(token="rt-opaque-refresh")
    assert "auth" in seen["cmd"] and "roles" in seen["cmd"]
    assert "token" not in seen["cmd"]  # no `auth token --token`
    assert seen["token_in_env"] == "rt-opaque-refresh"  # carried via env, not argv


def test_auth_appends_hint_on_flowctl_auth_failure(monkeypatch):
    monkeypatch.setattr(
        est.subprocess,
        "run",
        lambda *a, **k: types.SimpleNamespace(
            returncode=1, stdout="", stderr="Failed to authenticate: JWT token missing payload"
        ),
    )
    with pytest.raises(est.FlowctlError, match="refresh token"):
        est.auth(token="rt-bad")


# ---- Scoped materialization publish (targeted backfills) ------------------

_SCOPED_SPEC = """
materializations:
  Acme/benchmark/materializations/bigquery-tpcds-sf1:
    endpoint:
      connector:
        image: img
        config:
          credentials_json: '{"type":"service_account","client_email":"x@y"}'
    bindings:
      - source: Acme/benchmark/tpcds/sf1/call_center
        resource: {table: call_center, delta_updates: true}
        backfill: 1
  Acme/benchmark/materializations/bigquery-dsb-sf1:
    endpoint: {connector: {image: img, config: {}}}
    bindings:
      - source: Acme/benchmark/dsb/sf1/item
        resource: {table: item, delta_updates: true}
        backfill: 1
  Acme/benchmark/materializations/bigquery-tpcds-sf10:
    endpoint: {connector: {image: img, config: {}}}
    bindings:
      - source: Acme/benchmark/tpcds/sf10/call_center
        resource: {table: call_center, delta_updates: true}
        backfill: 1
  Acme/benchmark/materializations/bigquery-clickbench:
    endpoint: {connector: {image: img, config: {}}}
    bindings:
      - source: Acme/benchmark/clickbench/hits
        resource: {table: hits, delta_updates: true}
        backfill: 1
"""


def test_mat_matches_scale_is_exact():
    assert est._mat_matches("x/bigquery-tpcds-sf1", names=None, scales=[1], suites=None)
    # -sf1 must NOT match -sf10
    assert not est._mat_matches("x/bigquery-tpcds-sf10", names=None, scales=[1], suites=None)
    assert est._mat_matches("x/bigquery-tpcds-sf10", names=None, scales=[10], suites=None)


def test_mat_matches_suite_and_clickbench():
    assert est._mat_matches("x/bigquery-tpcds-sf1", names=None, scales=None, suites=["tpcds"])
    assert not est._mat_matches("x/bigquery-dsb-sf1", names=None, scales=None, suites=["tpcds"])
    # clickbench has no scale suffix -> matched via trailing token
    assert est._mat_matches("x/bigquery-clickbench", names=None, scales=None, suites=["clickbench"])


def test_mat_matches_name_full_or_leaf_and_and_semantics():
    assert est._mat_matches(
        "x/bigquery-tpcds-sf1", names={"bigquery-tpcds-sf1"}, scales=None, suites=None
    )
    assert est._mat_matches(
        "x/bigquery-tpcds-sf1", names={"x/bigquery-tpcds-sf1"}, scales=None, suites=None
    )
    # filters AND together: right name, wrong scale -> no match
    assert not est._mat_matches(
        "x/bigquery-tpcds-sf1", names={"bigquery-tpcds-sf1"}, scales=[10], suites=None
    )


def test_build_scoped_spec_filters_and_bumps_backfill(monkeypatch):
    monkeypatch.setattr(est, "render", lambda p: _SCOPED_SPEC)
    doc, selected = est.build_scoped_spec(Path("x.yaml"), scales=[1], suites=["tpcds"], backfill=2)
    assert selected == ["Acme/benchmark/materializations/bigquery-tpcds-sf1"]
    assert set(doc["materializations"]) == set(selected)
    binding = doc["materializations"][selected[0]]["bindings"][0]
    assert binding["backfill"] == 2  # bumped 1 -> 2


def test_build_scoped_spec_no_match_raises(monkeypatch):
    monkeypatch.setattr(est, "render", lambda p: _SCOPED_SPEC)
    with pytest.raises(est.FlowctlError, match="no materializations"):
        est.build_scoped_spec(Path("x.yaml"), scales=[999])


def test_build_scoped_spec_credentials_survive_reserialization(monkeypatch):
    # After render, credentials_json is a real string; the scoped doc must
    # safe_dump back to a STRING (not a parsed mapping) across the YAML round-trip.
    monkeypatch.setattr(est, "render", lambda p: _SCOPED_SPEC)
    doc, _ = est.build_scoped_spec(Path("x.yaml"), names={"bigquery-tpcds-sf1"})
    reparsed = yaml.safe_load(yaml.safe_dump(doc, width=2**31))
    cfg = reparsed["materializations"]["Acme/benchmark/materializations/bigquery-tpcds-sf1"][
        "endpoint"
    ]["connector"]["config"]
    assert isinstance(cfg["credentials_json"], str)
    assert "service_account" in cfg["credentials_json"]


def test_publish_scoped_publishes_only_selected(monkeypatch):
    monkeypatch.setattr(est, "render", lambda p: _SCOPED_SPEC)
    # Stub the auth-token env so the test is hermetic (CI has no ESTUARY_REFRESH_TOKEN).
    monkeypatch.setattr(est, "_flowctl_env", lambda token=None: {"FLOW_AUTH_TOKEN": "test"})
    captured = {}

    def _fake_run(cmd, *a, **k):
        captured["cmd"] = cmd
        # read back the applied spec that would be published
        src = cmd[cmd.index("--source") + 1]
        captured["doc"] = yaml.safe_load(Path(src).read_text())
        return types.SimpleNamespace(returncode=0, stdout="Publish successful", stderr="")

    monkeypatch.setattr(est.subprocess, "run", _fake_run)
    selected = est.publish_scoped(Path("bigquery.flow.yaml"), scales=[1], backfill=2)
    assert set(selected) == {
        "Acme/benchmark/materializations/bigquery-tpcds-sf1",
        "Acme/benchmark/materializations/bigquery-dsb-sf1",
    }
    # only the 2 sf1 mats were in the published doc — sf10 + clickbench untouched
    assert set(captured["doc"]["materializations"]) == set(selected)
    assert "--auto-approve" in captured["cmd"]


# ── pausing materializations before a measured run ──────────────────────────


def _fake_flowctl(monkeypatch, live: set[str]) -> dict:
    """Stub flowctl: ``catalog list`` returns ``live`` tasks; ``publish`` is captured."""
    monkeypatch.setattr(est, "render", lambda p: _SCOPED_SPEC)
    monkeypatch.setattr(est, "_flowctl_env", lambda token=None: {"FLOW_AUTH_TOKEN": "test"})
    captured: dict = {}

    def _fake_run(cmd, *a, **k):
        if cmd[:3] == ["flowctl", "catalog", "list"]:
            rows = [
                json.dumps({"catalogName": n, "liveSpec": {"catalogType": "materialization"}})
                for n in sorted(live)
            ]
            rows.append(
                json.dumps({"catalogName": "Acme/c", "liveSpec": {"catalogType": "collection"}})
            )
            return types.SimpleNamespace(returncode=0, stdout="\n".join(rows), stderr="")
        src = cmd[cmd.index("--source") + 1]
        captured["doc"] = yaml.safe_load(Path(src).read_text())
        return types.SimpleNamespace(returncode=0, stdout="Publish successful", stderr="")

    monkeypatch.setattr(est.subprocess, "run", _fake_run)
    return captured


def test_disable_pauses_only_live_tasks_without_a_backfill_bump(monkeypatch):
    tpcds = "Acme/benchmark/materializations/bigquery-tpcds-sf1"
    captured = _fake_flowctl(monkeypatch, live={tpcds})
    done = est.set_materializations_disabled(Path("bigquery.flow.yaml"), disabled=True, scales=[1])
    # dsb-sf1 matched the scope but Estuary does not hold it: publishing it would create it
    assert done == [tpcds]
    spec = captured["doc"]["materializations"]
    assert set(spec) == {tpcds}
    assert spec[tpcds]["shards"]["disable"] is True
    assert spec[tpcds]["bindings"][0]["backfill"] == 1


def test_enable_publishes_the_spec_without_the_disable_flag(monkeypatch):
    tpcds = "Acme/benchmark/materializations/bigquery-tpcds-sf1"
    captured = _fake_flowctl(monkeypatch, live={tpcds})
    est.set_materializations_disabled(
        Path("bigquery.flow.yaml"), disabled=False, names={"bigquery-tpcds-sf1"}
    )
    assert "disable" not in captured["doc"]["materializations"][tpcds].get("shards", {})


def test_disable_refuses_when_nothing_selected_exists(monkeypatch):
    captured = _fake_flowctl(monkeypatch, live=set())
    with pytest.raises(est.FlowctlError, match="none of the selected"):
        est.set_materializations_disabled(Path("bigquery.flow.yaml"), disabled=True, scales=[1])
    assert "doc" not in captured  # nothing published
