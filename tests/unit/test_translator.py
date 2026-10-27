"""Per-engine dialect success rates over a translate_all matrix."""

from __future__ import annotations

from benchmark.translator import dialect_success_rates


def test_dialect_success_rates_per_engine():
    matrix = {
        "q1": {"engines": {"bigquery": {"status": "success"}, "redshift": {"status": "success"}}},
        "q2": {"engines": {"bigquery": {"status": "success"}, "redshift": {"status": "error"}}},
    }
    rates = dialect_success_rates(matrix)
    assert rates["bigquery"]["ok"] == 2 and rates["bigquery"]["rate"] == 1.0
    assert rates["redshift"]["ok"] == 1 and rates["redshift"]["total"] == 2
    assert rates["redshift"]["rate"] == 0.5
