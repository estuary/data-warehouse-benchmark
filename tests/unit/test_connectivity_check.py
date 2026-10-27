"""connectivity-check: a hung engine is reported at the deadline, not waited on."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from benchmark import cli
from benchmark.exceptions import ConnectorError

_RELEASE = threading.Event()

# --platforms takes real engine ids only, so each behavior is played by one engine.
_HANGS, _ANSWERS, _REFUSES = "snowflake", "databricks", "redshift"


class _FakeConnector:
    """Context-manager connector whose behavior is picked by platform name."""

    def __init__(self, platform: str) -> None:
        self.platform = platform

    def __enter__(self) -> _FakeConnector:
        if self.platform == _HANGS:
            _RELEASE.wait(30)  # a dead host the driver keeps retrying
        if self.platform == _REFUSES:
            raise ConnectorError("connection refused")
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute_query(self, sql: str) -> Any:
        return SimpleNamespace(error_message=None)


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    import benchmark.connectors.registry as registry

    monkeypatch.setattr(
        registry, "create_connector", lambda p, config=None: _FakeConnector(p), raising=True
    )
    monkeypatch.setattr(cli, "_configure", lambda verbose=False: None)


def test_hung_engine_fails_at_deadline_and_others_still_report(fake_registry: None) -> None:
    _RELEASE.clear()
    start = time.monotonic()
    result = CliRunner().invoke(
        cli.app,
        [
            "connectivity-check",
            "--platforms",
            f"{_HANGS},{_ANSWERS},{_REFUSES}",
            "--timeout",
            "0.5",
        ],
    )
    elapsed = time.monotonic() - start
    _RELEASE.set()

    assert result.exit_code == 0, result.output
    assert elapsed < 5, f"check waited {elapsed:.1f}s on the hung engine"
    lines = {line.split("|")[0].strip(): line for line in result.output.splitlines() if "|" in line}
    assert "fail" in lines[_HANGS] and "no answer within 0.5 s" in lines[_HANGS]
    assert "| ok" in lines[_ANSWERS]
    assert "fail" in lines[_REFUSES] and "connection refused" in lines[_REFUSES]
