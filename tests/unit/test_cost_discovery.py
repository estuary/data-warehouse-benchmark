"""Cost-config capture/load/build behaviors.

Exercises the connector-introspection cost-config flow with fakes — no cloud:
  * capture writes a VALID file even when nothing is reachable
  * load yells when the file is missing
  * collector_for builds from captured config (filtering extra keys) and warns
    + falls back to defaults for a platform absent from the file.
"""

from __future__ import annotations

import json

import pytest

from benchmark.cost import discovery
from benchmark.cost.motherduck import MotherDuckCostCollector
from benchmark.exceptions import BenchmarkError


class _FakeConnector:
    def __init__(self, *, compute=None, raises=False) -> None:
        self._compute = compute
        self._raises = raises

    def __enter__(self) -> _FakeConnector:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def runtime_compute(self):
        if self._raises:
            raise RuntimeError("Network is unreachable")
        return self._compute or {}

    @staticmethod
    def cost_kwargs_from_compute(compute):
        # Identity translator for the fake: the captured compute IS the kwargs.
        return compute or None


def test_capture_writes_valid_file_even_when_nothing_reachable(tmp_path, monkeypatch):
    def fake_create(platform):
        return {
            "motherduck": _FakeConnector(compute={"tier": "jumbo"}),
            "clickhouse": _FakeConnector(raises=True),  # unreachable
            "bigquery": _FakeConnector(compute=None),  # doesn't self-report
        }[platform]

    monkeypatch.setattr(discovery, "create_connector", fake_create)
    out = tmp_path / "cost_config.json"
    doc = discovery.capture_cost_config(["motherduck", "clickhouse", "bigquery"], out)

    # File exists and is valid JSON regardless of failures.
    on_disk = json.loads(out.read_text())
    assert on_disk == doc
    assert doc["platforms"] == {"motherduck": {"tier": "jumbo"}}
    assert set(doc["errors"]) == {"clickhouse", "bigquery"}
    assert "unreachable" in doc["errors"]["clickhouse"]


def test_capture_valid_file_with_zero_reachable(tmp_path, monkeypatch):
    monkeypatch.setattr(discovery, "create_connector", lambda p: _FakeConnector(raises=True))
    out = tmp_path / "cost_config.json"
    doc = discovery.capture_cost_config(["motherduck"], out)
    assert json.loads(out.read_text()) == doc
    assert doc["platforms"] == {}
    assert "motherduck" in doc["errors"]


def test_load_yells_when_missing(tmp_path):
    with pytest.raises(BenchmarkError, match="capture-cost-config"):
        discovery.load_cost_config(tmp_path / "nope.json")


def test_collector_for_builds_from_config_and_filters_extra_keys():
    # 'note' is informational and must not reach the constructor.
    cfg = {"platforms": {"motherduck": {"tier": "jumbo", "note": "from recent_queries"}}}
    collector = discovery.collector_for("motherduck", cfg)
    assert isinstance(collector, MotherDuckCostCollector)
    assert collector.tier == "jumbo"


def test_collector_for_warns_and_defaults_when_absent(caplog):
    collector = discovery.collector_for("motherduck", {"platforms": {}})
    assert isinstance(collector, MotherDuckCostCollector)
    assert collector.tier == "standard"  # constructor default
    assert any("no captured cost config" in r.message for r in caplog.records)


def test_collector_for_selects_size_specific_config():
    # A run prices by the size it actually ran on — no hand-editing between sizes.
    cfg = {
        "platforms": {
            "motherduck": {
                "tier": "standard",  # flat default
                "sizes": {"Pulse": {"tier": "pulse"}, "Standard": {"tier": "standard"}},
            }
        }
    }
    assert discovery.collector_for("motherduck", cfg).tier == "standard"  # no label -> flat
    assert discovery.collector_for("motherduck", cfg, compute_label="Pulse").tier == "pulse"
    # unknown label falls back to flat (never errors)
    assert discovery.collector_for("motherduck", cfg, compute_label="Nope").tier == "standard"


def test_capture_merges_and_accumulates_per_size(tmp_path, monkeypatch):
    # pre-existing file with another platform must survive the merge.
    out = tmp_path / "cost_config.json"
    out.write_text(
        json.dumps({"version": 1, "platforms": {"bigquery": {"x": 1}}, "compute": {}, "errors": {}})
    )
    monkeypatch.setattr(
        discovery, "create_connector", lambda p: _FakeConnector(compute={"size": "jumbo"})
    )
    doc = discovery.capture_cost_config(["motherduck"], out)
    # bigquery preserved (merge, not overwrite)
    assert doc["platforms"]["bigquery"] == {"x": 1}
    # motherduck recorded flat + per-size, keyed by the derived label ('jumbo')
    md = doc["platforms"]["motherduck"]
    assert md["size"] == "jumbo"
    assert md["sizes"] == {"jumbo": {"size": "jumbo"}}
