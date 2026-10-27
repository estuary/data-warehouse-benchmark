"""terraform_outputs: scale-aware provisioned-compute resolution + resilient load.

Snowflake's warehouse is sized per scale, so its slice resolves to the size for
the requested scale; the other engines are fixed / autoscaling and return their
config unchanged. A missing/invalid outputs file degrades to ``{}``.
"""

from __future__ import annotations

import pytest

from benchmark import terraform_outputs as tfo


@pytest.fixture(autouse=True)
def _clear_cache():
    tfo.get_terraform_outputs.cache_clear()
    yield
    tfo.get_terraform_outputs.cache_clear()


def _stub_outputs(monkeypatch, engine_compute):
    monkeypatch.setattr(
        tfo, "get_terraform_outputs", lambda: {"engine_compute": {"value": engine_compute}}
    )


def test_snowflake_resolves_warehouse_size_per_scale(monkeypatch):
    _stub_outputs(
        monkeypatch,
        {"snowflake": {"warehouse_sizes": {"1": "XSMALL", "100": "MEDIUM", "1000": "LARGE"}}},
    )
    assert tfo.provisioned_compute("snowflake", 1) == {"warehouse_size": "XSMALL"}
    assert tfo.provisioned_compute("snowflake", 100) == {"warehouse_size": "MEDIUM"}
    assert tfo.provisioned_compute("snowflake", 1000) == {"warehouse_size": "LARGE"}


def test_fixed_and_autoscaling_engines_are_scale_independent(monkeypatch):
    _stub_outputs(
        monkeypatch,
        {
            "bigquery": {"edition": "ENTERPRISE", "reservation_slots": 100},
            "redshift": {"baseline_rpu": 8, "max_rpu": 512},
        },
    )
    # Same config at every scale (still recorded per-scale by the runner).
    assert tfo.provisioned_compute("bigquery", 1) == tfo.provisioned_compute("bigquery", 1000)
    assert tfo.provisioned_compute("bigquery", 1) == {
        "edition": "ENTERPRISE",
        "reservation_slots": 100,
    }
    assert tfo.provisioned_compute("redshift", 100) == {"baseline_rpu": 8, "max_rpu": 512}


def test_missing_engine_or_outputs_returns_empty(monkeypatch):
    _stub_outputs(monkeypatch, {"snowflake": {"warehouse_sizes": {"1": "XSMALL"}}})
    assert tfo.provisioned_compute("fabric", 1) == {}  # not provisioned
    monkeypatch.setattr(tfo, "get_terraform_outputs", lambda: {})
    assert tfo.provisioned_compute("snowflake", 1) == {}  # no engine_compute block


def test_loader_missing_file_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(
        tfo,
        "get_settings",
        lambda: type("S", (), {"TERRAFORM_OUTPUTS_PATH": tmp_path / "nope.json"}),
    )
    assert tfo.get_terraform_outputs() == {}


def test_loader_reads_file_and_resolves_scale(monkeypatch, tmp_path):
    path = tmp_path / "outputs.json"
    path.write_text(
        '{"engine_compute": {"value": {"snowflake": {"warehouse_sizes": {"1000": "LARGE"}}}}}'
    )
    monkeypatch.setattr(
        tfo, "get_settings", lambda: type("S", (), {"TERRAFORM_OUTPUTS_PATH": path})
    )
    assert tfo.provisioned_compute("snowflake", 1000) == {"warehouse_size": "LARGE"}


# ---- SA-key materialization (export-terraform-outputs) --------------------

import base64  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import types  # noqa: E402


def _fake_settings(tmp_path):
    bq_dest = tmp_path / "runner-sa.json"
    est_dest = tmp_path / "estuary-sa.json"
    return (
        types.SimpleNamespace(
            BIGQUERY=types.SimpleNamespace(GCP_SA_KEY=bq_dest),
            ESTUARY=types.SimpleNamespace(BIGQUERY_SERVICE_ACCOUNT_KEY=est_dest),
        ),
        bq_dest,
        est_dest,
    )


def _b64_sa(email):
    return base64.b64encode(
        json.dumps({"type": "service_account", "client_email": email}).encode()
    ).decode()


def test_materialize_writes_both_keys_0600(monkeypatch, tmp_path):
    settings, bq_dest, est_dest = _fake_settings(tmp_path)
    monkeypatch.setattr(tfo, "get_settings", lambda: settings)
    outputs = {
        "bigquery_runner_sa_key": {"value": _b64_sa("runner@x")},
        "bigquery_estuary_sa_key": {"value": _b64_sa("estuary@x")},
    }
    written = tfo.materialize_credential_files(outputs)
    assert set(written) == {bq_dest, est_dest}
    assert json.loads(bq_dest.read_text())["client_email"] == "runner@x"
    assert json.loads(est_dest.read_text())["client_email"] == "estuary@x"
    assert oct(os.stat(bq_dest).st_mode & 0o777) == "0o600"


def test_materialize_skips_null_output(monkeypatch, tmp_path):
    settings, bq_dest, est_dest = _fake_settings(tmp_path)
    monkeypatch.setattr(tfo, "get_settings", lambda: settings)
    # estuary engine not provisioned -> null value; bigquery present.
    outputs = {
        "bigquery_runner_sa_key": {"value": _b64_sa("runner@x")},
        "bigquery_estuary_sa_key": {"value": None},
    }
    written = tfo.materialize_credential_files(outputs)
    assert written == [bq_dest]
    assert not est_dest.exists()


def test_materialize_skips_when_dest_unset(monkeypatch, tmp_path):
    settings, _bq_dest, _est_dest = _fake_settings(tmp_path)
    settings.BIGQUERY.GCP_SA_KEY = None  # path not configured
    monkeypatch.setattr(tfo, "get_settings", lambda: settings)
    written = tfo.materialize_credential_files(
        {"bigquery_runner_sa_key": {"value": _b64_sa("runner@x")}}
    )
    assert written == []


def test_materialize_skips_non_base64_json(monkeypatch, tmp_path):
    settings, bq_dest, _est = _fake_settings(tmp_path)
    monkeypatch.setattr(tfo, "get_settings", lambda: settings)
    written = tfo.materialize_credential_files(
        {"bigquery_runner_sa_key": {"value": "not-base64-json!!"}}
    )
    assert written == [] and not bq_dest.exists()
