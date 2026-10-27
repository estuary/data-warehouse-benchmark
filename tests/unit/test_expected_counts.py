"""expected_counts manifest writer — merge safety (no clobber across scales)."""

from __future__ import annotations

import json

from benchmark.staging.expected_counts import merge_into_manifest


def test_merge_preserves_other_scales(tmp_path):
    m = tmp_path / "expected_counts.json"
    m.write_text(json.dumps({"1": {"store_sales": 100}}), encoding="utf-8")
    merge_into_manifest({"10": {"store_sales": 1000}}, m)
    d = json.loads(m.read_text(encoding="utf-8"))
    assert d["1"]["store_sales"] == 100  # untouched
    assert d["10"]["store_sales"] == 1000  # added


def test_merge_replaces_same_scale_wholesale(tmp_path):
    m = tmp_path / "expected_counts.json"
    m.write_text(json.dumps({"10": {"a": 1, "b": 2}}), encoding="utf-8")
    merge_into_manifest({"10": {"a": 5}}, m)
    # Scale-level replace: the new map wins for that scale (no per-table merge).
    assert json.loads(m.read_text(encoding="utf-8"))["10"] == {"a": 5}


def test_merge_creates_when_absent(tmp_path):
    m = tmp_path / "expected_counts.json"
    merge_into_manifest({"1": {"a": 1}}, m)
    assert json.loads(m.read_text(encoding="utf-8")) == {"1": {"a": 1}}
