"""--platforms: every command that takes it resolves `all` and engine names the same way.

`all` is the seven engines in canonical order (``connectors.registry.ALL_PLATFORMS``);
a command that acts on only some engines narrows it by its own rule (``reconcile``:
the engines with a reconciler). Each command's worker is replaced with a recorder,
so no engine, credential or data is needed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from benchmark import cli
from benchmark.cli import _resolve_platforms
from benchmark.connectors.registry import ALL_PLATFORMS

CANONICAL = [
    "snowflake",
    "databricks",
    "bigquery",
    "redshift",
    "fabric",
    "clickhouse",
    "motherduck",
]

_PROVENANCE = {
    "command": "test",
    "scale_factor": None,
    "run_type": None,
    "platforms": None,
    "blocks": None,
    "git_sha": None,
    "git_dirty": None,
    "harness_version": None,
    "optimize_model": None,
    "query_files_hash": None,
    "tool_versions": None,
}


@pytest.fixture(autouse=True)
def _no_logging_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_configure", lambda verbose=False: None)


# ── the resolver ─────────────────────────────────────────────────────────────


def test_all_is_the_seven_engines_in_canonical_order() -> None:
    assert list(ALL_PLATFORMS) == CANONICAL
    assert _resolve_platforms("all") == CANONICAL


def test_all_narrows_to_the_eligible_engines_in_canonical_order() -> None:
    assert _resolve_platforms("all", eligible={"motherduck", "bigquery"}) == [
        "bigquery",
        "motherduck",
    ]


def test_a_comma_list_keeps_the_order_given() -> None:
    assert _resolve_platforms("motherduck, snowflake") == ["motherduck", "snowflake"]


# ── every command expands `all` ──────────────────────────────────────────────


def _record_translate(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    def translate_all(engines: list[str], **_: Any) -> dict:
        seen.extend(engines)
        return {}

    mp.setattr("benchmark.translator.translate_all", translate_all)
    return ["translate"]


def _record_validate_dialects(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    _record_translate(mp, seen, tmp)
    mp.setattr(
        "benchmark.translator.dialect_success_rates",
        lambda matrix: {e: {"ok": 1, "total": 1, "rate": 1.0} for e in CANONICAL},
    )
    return ["validate-dialects"]


def _record_validate_loads(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    def validate_engine_loads(platforms: list[str], *_: Any, **__: Any) -> list[str]:
        seen.extend(platforms)
        return []

    mp.setattr("benchmark.staging.load_check.validate_engine_loads", validate_engine_loads)
    return ["validate-loads"]


def _record_apply_physical(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    mp.setattr(
        "benchmark.physical_design.apply_design",
        lambda platforms, scale, path: seen.extend(platforms),
    )
    return ["apply-physical-design"]


def _record_capture_cost_config(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    def capture(platforms: list[str], out: Path) -> dict:
        seen.extend(platforms)
        return {"platforms": {}, "errors": {}}

    mp.setattr("benchmark.cost.discovery.capture_cost_config", capture)
    return ["capture-cost-config", "--out", str(tmp / "cost_config.json")]


def _record_e2e(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    def run_e2e(platforms: list[str], *_: Any, **__: Any) -> list[tuple[str, str]]:
        seen.extend(platforms)
        return []

    mp.setattr("benchmark.e2e.run_e2e", run_e2e)
    return ["e2e"]


def _record_skip_scale(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    mp.setattr(
        "benchmark.results.ResultsDB.record_scale_skip",
        lambda self, scale, platform, *a, **k: seen.append(platform),
    )
    return ["skip-scale", "--scale", "1000", "--results-db", str(tmp / "r.duckdb")]


def _connector_recorder(seen: list[str]) -> Callable[..., Any]:
    """create_connector double: note the platform, then refuse (a config error)."""

    def create_connector(platform: str, config: dict | None = None) -> Any:
        seen.append(platform)
        raise ValueError(f"{platform} not configured in this test")

    return create_connector


def _record_validate_correctness(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    mp.setattr("benchmark.connectors.registry.create_connector", _connector_recorder(seen))
    return ["validate-correctness", "--blocks", "tpcds", "--results-db", str(tmp / "r.duckdb")]


def _record_validate_sf1(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    _record_validate_correctness(mp, seen, tmp)
    return ["validate-sf1", "--blocks", "tpcds", "--results-db", str(tmp / "r.duckdb")]


def _single_user_pass(command: str) -> Callable[[pytest.MonkeyPatch, list[str], Path], list[str]]:
    """sql / cold: the per-platform loop starts by building the cost collector."""

    def setup(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
        mp.setattr("benchmark.cost.discovery.load_cost_config", lambda *a, **k: {})
        mp.setattr("benchmark.provenance.gather_provenance", lambda **kw: dict(_PROVENANCE))

        def collector_for(platform: str, *_: Any, **__: Any) -> Any:
            seen.append(platform)
            raise KeyError(platform)

        mp.setattr("benchmark.cost.discovery.collector_for", collector_for)
        mp.setattr(cli, "_price_single_user", lambda *a, **k: None)
        return [command, "--blocks", "tpcds", "--results-db", str(tmp / "r.duckdb")]

    return setup


def _shared_load_pass(command: str, worker: str) -> Callable[..., list[str]]:
    """concurrency / sustained: one pass per platform × block."""

    def setup(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
        mp.setattr("benchmark.cost.discovery.load_cost_config", lambda *a, **k: {})
        mp.setattr("benchmark.provenance.gather_provenance", lambda **kw: dict(_PROVENANCE))

        def run_pass(*, platform: str, **_: Any) -> int:
            seen.append(platform)
            return 0

        mp.setattr(f"benchmark.sql_benchmark.concurrency.{worker}", run_pass)
        mp.setattr(cli, "_price_shared_windows", lambda *a, **k: None)
        return [command, "--blocks", "tpcds", "--results-db", str(tmp / "r.duckdb")]

    return setup


def _record_reconcile(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    mp.setattr("benchmark.connectors.registry.create_connector", _connector_recorder(seen))
    return ["reconcile", "--results-db", str(tmp / "r.duckdb")]


def _record_connectivity(mp: pytest.MonkeyPatch, seen: list[str], tmp: Path) -> list[str]:
    mp.setattr("benchmark.connectors.registry.create_connector", _connector_recorder(seen))
    return ["connectivity-check", "--timeout", "5"]


COMMANDS: dict[str, tuple[Callable[..., list[str]], list[str]]] = {
    "translate": (_record_translate, CANONICAL),
    "validate-dialects": (_record_validate_dialects, CANONICAL),
    "validate-loads": (_record_validate_loads, CANONICAL),
    "apply-physical-design": (_record_apply_physical, CANONICAL),
    "capture-cost-config": (_record_capture_cost_config, CANONICAL),
    "e2e": (_record_e2e, CANONICAL),
    "skip-scale": (_record_skip_scale, CANONICAL),
    "validate-correctness": (_record_validate_correctness, CANONICAL),
    "validate-sf1": (_record_validate_sf1, CANONICAL),
    "sql": (_single_user_pass("sql"), CANONICAL),
    "cold": (_single_user_pass("cold"), CANONICAL),
    "concurrency": (_shared_load_pass("concurrency", "run_concurrency_pass"), CANONICAL),
    "sustained": (_shared_load_pass("sustained", "run_sustained_pass"), CANONICAL),
    # ClickHouse records its metrics inline and has no reconciler.
    "reconcile": (_record_reconcile, [p for p in CANONICAL if p != "clickhouse"]),
}


@pytest.mark.parametrize("command", sorted(COMMANDS))
def test_platforms_all_expands_in_canonical_order(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    setup, expected = COMMANDS[command]
    seen: list[str] = []
    argv = setup(monkeypatch, seen, tmp_path)

    result = CliRunner().invoke(cli.app, [*argv, "--platforms", "all"])

    assert result.exit_code in (0, 1), result.output  # 1: the recorder refused every engine
    assert "unknown platform" not in result.output
    assert seen == expected


def test_connectivity_check_all_probes_every_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The probes run on parallel threads, so only the printed order is canonical."""
    seen: list[str] = []
    argv = _record_connectivity(monkeypatch, seen, tmp_path)

    result = CliRunner().invoke(cli.app, [*argv, "--platforms", "all"])

    assert result.exit_code == 0, result.output
    assert sorted(seen) == sorted(CANONICAL)
    printed = [line.split("|")[0].strip() for line in result.output.splitlines() if "|" in line]
    assert printed[1:] == CANONICAL  # after the header row


def test_recost_capacity_all_prices_every_engine(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    priced: list[str] = []
    empty = {"cells": [], "total_before_usd": 0.0, "total_after_usd": 0.0}

    def pricer(name: str) -> Callable[..., dict]:
        def run(*args: Any, **kwargs: Any) -> dict:
            priced.extend(kwargs.get("platforms") or [name])
            return empty

        return run

    for fn, name in (
        ("recost_fabric_windows", "fabric"),
        ("recost_rate_priced_rows", "rate"),
        ("resplit_databricks_hours", "databricks"),
        ("cap_redshift_idle", "redshift"),
        ("recost_bigquery_capacity", "bigquery"),
        ("price_snowflake_on_engine_time", "snowflake"),
    ):
        monkeypatch.setattr(f"benchmark.analysis.recost.{fn}", pricer(name))
    monkeypatch.setattr("benchmark.cost.discovery.load_cost_config", lambda *a, **k: {})

    result = CliRunner().invoke(
        cli.app,
        ["recost-capacity", "--platforms", "all", "--results-db", str(tmp_path / "r.duckdb")],
    )

    assert result.exit_code == 0, result.output
    assert sorted(priced) == sorted(CANONICAL)


# ── a misspelled engine fails before anything runs ──────────────────────────


@pytest.mark.parametrize("command", ["sql", "translate", "concurrency", "reconcile"])
def test_a_misspelled_engine_fails_with_a_suggestion(
    command: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A typo is a usage error naming the engine it was probably meant to be, not
    an engine skipped with a log line (or, on translate, a KeyError)."""
    monkeypatch.chdir(tmp_path)  # nothing is written into the repo if it got past

    result = CliRunner().invoke(cli.app, [command, "--platforms", "snowflak"])

    assert result.exit_code == 2
    message = " ".join(result.output.replace("│", " ").split())  # undo the error box
    assert "did you mean 'snowflake'" in message
