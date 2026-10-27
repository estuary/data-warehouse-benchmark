"""redshift_sweep preflight: an old AWS CLI or a missing S3 Tables export stops the sweep.

SYS_QUERY_HISTORY keeps 7 days, so a sweep whose queue time is not exported to S3
Tables cannot be rechecked later. The preflight reads the export status through the
AWS CLI, which only knows the option from 2.35.22.
"""

from __future__ import annotations

import importlib.util
import json
import types
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "sweeps" / "redshift_sweep.py"


@pytest.fixture(scope="module")
def sweep():
    spec = importlib.util.spec_from_file_location("redshift_sweep", _PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_aws(monkeypatch, sweep, *, version: str, s3_status: dict | None) -> None:
    def run(cmd, *a, **k):
        if cmd[:2] == ["aws", "--version"]:
            return types.SimpleNamespace(
                returncode=0, stdout=f"aws-cli/{version} Python/3", stderr=""
            )
        if "get-workgroup" in cmd:
            body = {"workgroup": {"namespaceName": "ns-1"}}
        else:
            body = {"namespace": {"namespaceName": "ns-1", "s3TablePublishStatus": s3_status}}
        return types.SimpleNamespace(returncode=0, stdout=json.dumps(body), stderr="")

    monkeypatch.setattr(sweep.subprocess, "run", run)


def test_ready_when_the_cli_is_new_and_the_table_is_exported(monkeypatch, sweep):
    _fake_aws(monkeypatch, sweep, version="2.37.2", s3_status={"s3Tables": ["sys_query_history"]})
    assert sweep.export_preflight("wg", "benchmark") == []


def test_an_old_cli_is_refused_before_any_aws_call(monkeypatch, sweep):
    _fake_aws(monkeypatch, sweep, version="2.34.20", s3_status=None)
    (problem,) = sweep.export_preflight("wg", "benchmark")
    assert "2.34.20 predates" in problem and "2.35.22" in problem


def test_a_namespace_without_the_export_is_refused(monkeypatch, sweep):
    _fake_aws(monkeypatch, sweep, version="2.37.2", s3_status={"s3Tables": ["sys_userlog"]})
    (problem,) = sweep.export_preflight("wg", "benchmark")
    assert "ns-1 does not export sys_query_history" in problem


def test_exporting_all_tables_counts(monkeypatch, sweep):
    _fake_aws(monkeypatch, sweep, version="2.35.22", s3_status={"enabledAll": True})
    assert sweep.export_preflight("wg", "benchmark") == []
