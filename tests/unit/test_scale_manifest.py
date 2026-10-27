"""Scale-keyed manifest: record/load round-trip + manifest-aware cost collector.

Covers persistence (atomic upsert, int-key round-trip, missing-file error,
readiness flags) and building a cost collector from the manifest's recorded
cost_kwargs (filtering extras, warn+default when absent).
"""

from __future__ import annotations

import pytest

from benchmark.cost.discovery import collector_for_scale
from benchmark.cost.motherduck import MotherDuckCostCollector
from benchmark.exceptions import BenchmarkError
from benchmark.scale_manifest import (
    ScaleEntry,
    ScaleManifest,
    load_scale_manifest,
    record_scale,
)


def test_record_then_load_round_trip(tmp_path):
    path = tmp_path / "scale_manifest.json"
    entry = ScaleEntry(
        target="giga",
        live={"size": "Giga"},
        cost_kwargs={"tier": "giga"},
        compute_validated=True,
        data_ready=True,
    )
    record_scale(1000, "motherduck", entry, path)

    loaded = load_scale_manifest(path)
    assert loaded.version == 2
    got = loaded.entry(1000, "motherduck")  # int key survives the JSON string-key trip
    assert got is not None
    assert got.cost_kwargs == {"tier": "giga"}
    assert got.captured_at is not None  # stamped on record
    assert got.ready is True


def test_ready_requires_both_axes():
    assert ScaleEntry(compute_validated=True, data_ready=False).ready is False
    assert ScaleEntry(compute_validated=False, data_ready=True).ready is False
    assert ScaleEntry(compute_validated=True, data_ready=True).ready is True


def test_record_upserts_and_coexists(tmp_path):
    path = tmp_path / "scale_manifest.json"
    record_scale(1000, "motherduck", ScaleEntry(cost_kwargs={"tier": "giga"}), path)
    record_scale(1000, "bigquery", ScaleEntry(cost_kwargs={"tier": "enterprise"}), path)
    record_scale(1, "motherduck", ScaleEntry(cost_kwargs={"tier": "standard"}), path)
    # same (scale, platform) overwrites
    record_scale(1000, "motherduck", ScaleEntry(cost_kwargs={"tier": "mega"}), path)

    m = load_scale_manifest(path)
    assert m.entry(1000, "motherduck").cost_kwargs == {"tier": "mega"}
    assert m.entry(1000, "bigquery").cost_kwargs == {"tier": "enterprise"}
    assert m.entry(1, "motherduck").cost_kwargs == {"tier": "standard"}


def test_load_missing_raises(tmp_path):
    with pytest.raises(BenchmarkError, match="prepare-scale"):
        load_scale_manifest(tmp_path / "nope.json")


def test_entry_missing_returns_none():
    m = ScaleManifest()
    assert m.entry(1000, "snowflake") is None


def test_collector_for_scale_builds_and_filters():
    m = ScaleManifest(
        scales={1000: {"motherduck": ScaleEntry(cost_kwargs={"tier": "jumbo", "note": "x"})}}
    )
    collector = collector_for_scale("motherduck", 1000, m)
    assert isinstance(collector, MotherDuckCostCollector)
    assert collector.tier == "jumbo"  # 'note' filtered out before the ctor


def test_collector_for_scale_warns_and_defaults_when_absent(caplog):
    collector = collector_for_scale("motherduck", 1000, ScaleManifest())
    assert isinstance(collector, MotherDuckCostCollector)
    assert collector.tier == "standard"  # ctor default
    assert any("no recorded cost config" in r.message for r in caplog.records)
