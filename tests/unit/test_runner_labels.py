"""The runner must never record rows it cannot attribute to a compute size."""

from __future__ import annotations

import pytest

from benchmark.exceptions import UnlabelledComputeError
from benchmark.sql_benchmark import runner


class _DB:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def record_engine_environment(self, *args, **kwargs) -> None:
        self.rows.append((args, kwargs))


class _Conn:
    """Minimal connector stub; ``compute`` is what the live lookup returns."""

    platform = "fabric"

    def __init__(self, compute: dict) -> None:
        self._compute = compute

    def runtime_environment(self) -> dict:
        return {"platform": "fabric"}

    def runtime_compute(self) -> dict:
        return self._compute

    def provisioned_compute(self) -> dict:
        return {}


def test_missing_compute_label_aborts_instead_of_recording_null():
    """A failed compute lookup must raise, not return None.

    If an empty lookup ({}) were let through, the runner would stamp
    compute_label = NULL on every row of the block and report success. Unlabelled
    rows are invisible to the size-keyed canonical views, so the cell would read as
    absent rather than wrong: silent loss.

    This is deliberately NOT continue-on-failure: that rule protects a run from one
    bad QUERY, whereas this is a property of the entire cell.
    """
    db = _DB()
    with pytest.raises(UnlabelledComputeError) as exc:
        runner.capture_runtime_environment(db, "fabric", 1, "warm", _Conn({}))
    assert "unlabelled" in str(exc.value).lower()


def test_present_compute_label_is_returned():
    db = _DB()
    label = runner.capture_runtime_environment(
        db, "fabric", 1, "warm", _Conn({"capacity_sku": "F8", "state": "Active"})
    )
    assert label == "F8"


def test_unrelated_capture_failure_still_does_not_abort():
    """Instrumentation errors other than a missing label stay non-fatal.

    A connector that cannot answer at all is a different failure from one that
    answers without a size, and it must not take the run down.
    """

    class Broken(_Conn):
        def runtime_environment(self) -> dict:
            raise RuntimeError("boom")

    assert runner.capture_runtime_environment(_DB(), "fabric", 1, "warm", Broken({})) is None


def test_concurrency_refuses_to_record_an_unlabelled_band(monkeypatch):
    """The concurrency path must not swallow the runner's label guard.

    run_concurrency_pass has a Snowflake-only override fallback that returns None for
    every other engine. A bare ``except Exception`` around the capture would catch
    UnlabelledComputeError, fall through to that fallback, and write a whole band
    with compute_label = NULL while reporting success.
    """
    from benchmark.sql_benchmark import concurrency as conc

    monkeypatch.setattr(conc, "connect_with_retry", lambda factory: _Probe())
    monkeypatch.setattr(conc, "create_connector", lambda platform, config=None: None)

    def _raise(*args, **kwargs):
        raise UnlabelledComputeError("no size")

    monkeypatch.setattr("benchmark.sql_benchmark.runner.capture_runtime_environment", _raise)

    with pytest.raises(UnlabelledComputeError):
        conc.run_concurrency_pass(
            platform="fabric",
            connector_config={"schema": "tpcds_sf1"},
            cost_config={},
            results_db=_DB(),
            scale_factor=1,
            blocks=["tpcds"],
            concurrency_levels=[1],
            provenance_id=None,
        )


class _Probe:
    def disconnect(self) -> None: ...
