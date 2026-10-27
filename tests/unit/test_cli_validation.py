"""CLI input validation: bad --platforms / --suites, a missing render-status
template and a missing cost config fail loudly, before anything runs."""

from __future__ import annotations

import re

import pytest
import typer

from benchmark.cli import _resolve_platforms, _resolve_suites


def test_resolve_platforms_all_expands():
    plats = _resolve_platforms("all")
    assert "bigquery" in plats and "snowflake" in plats


def test_resolve_platforms_valid_subset():
    assert _resolve_platforms("bigquery,motherduck") == ["bigquery", "motherduck"]


def test_resolve_platforms_rejects_unknown_with_suggestion():
    with pytest.raises(typer.BadParameter) as exc:
        _resolve_platforms("biquery")  # a one-letter typo
    msg = str(exc.value)
    assert "unknown platform" in msg and "did you mean 'bigquery'" in msg


def test_resolve_platforms_rejects_unknown_no_close_match():
    with pytest.raises(typer.BadParameter):
        _resolve_platforms("zzzznotreal")


def test_resolve_suites_valid():
    assert _resolve_suites("tpcds,clickbench") == ("tpcds", "clickbench")


def test_resolve_suites_rejects_unknown():
    with pytest.raises(typer.BadParameter) as exc:
        _resolve_suites("tpcds,clikbench")
    assert "unknown suite" in str(exc.value)


# ── validate-correctness: the same gate at a chosen scale ────────────────────


def test_schema_map_follows_the_scale_but_clickbench_stays_pinned():
    """TPC-DS and DSB live in per-scale schemas; ClickBench is ONE ~100 GB dataset
    whose ground truth is keyed at SF100 regardless of the run tier. Deriving its
    truth scale from --scale would look up a key that does not exist."""
    import inspect
    import re

    from benchmark import cli

    body = inspect.getsource(cli.validate_correctness)
    spec = re.search(r"block_specs: dict\[str, tuple\[str, int\]\] = \{(.+?)\n    \}", body, re.S)
    assert spec, "block_specs map not found — did the command get restructured?"
    text = spec.group(1)
    assert 'f"tpcds_sf{scale}"' in text
    assert 'f"dsb_sf{scale}"' in text
    assert '"clickbench": ("clickbench", 100)' in text, (
        "ClickBench ground truth is keyed at SF100; it must not follow --scale"
    )


def test_revert_on_regression_is_refused_above_sf1():
    """The optimize baselines were equivalence-gated at SF1. Reverting a rewrite
    because it diverges at another scale would be a decision taken against evidence
    the baseline never saw, so the combination is refused rather than honoured."""
    from typer.testing import CliRunner

    from benchmark.cli import app

    result = CliRunner().invoke(
        app,
        [
            "validate-correctness",
            "--scale",
            "100",
            "--revert-on-regression",
            "--platforms",
            "motherduck",
        ],
    )

    assert result.exit_code == 2
    assert "--scale 1 only" in result.output


def test_validate_sf1_alias_still_exists():
    """`validate-sf1` is the name the README, CLAUDE.md and the operator guide use
    for the correctness gate, so it stays registered beside `validate-correctness`."""
    from benchmark.cli import app

    names = {c.name for c in app.registered_commands}
    assert "validate-sf1" in names
    assert "validate-correctness" in names


# ── render-status: the template is the operator's to supply ──────────────────

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(output: str) -> str:
    """Typer renders errors with rich, and colors them under CI (GITHUB_ACTIONS);
    drop the escapes and collapse the panel's line wrapping before matching."""
    return " ".join(ANSI_ESCAPE.sub("", output).split())


def test_render_status_requires_a_template():
    """No template ships with the repository, so there is no default to fall back on."""
    from typer.testing import CliRunner

    from benchmark.cli import app

    result = CliRunner().invoke(app, ["render-status"])

    assert result.exit_code == 2
    assert "Missing option '--template'" in _plain(result.output)


def test_render_status_names_a_missing_template(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from benchmark import cli

    monkeypatch.setattr(cli, "_configure", lambda verbose=False: None)
    missing = tmp_path / "status.html"

    result = CliRunner().invoke(cli.app, ["render-status", "--template", str(missing)])

    assert result.exit_code == 2
    assert f"no status template at {missing}" in _plain(result.output)


def test_render_status_resolves_a_supplied_template(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from benchmark import cli
    from benchmark.results import ResultsDB

    monkeypatch.setattr(cli, "_configure", lambda verbose=False: None)
    db_path = tmp_path / "r.duckdb"
    ResultsDB(db_path)  # creates the schema
    template = tmp_path / "status.html"
    template.write_text("<p>no facts here</p>", encoding="utf-8")
    out = tmp_path / "page.html"

    result = CliRunner().invoke(
        cli.app,
        [
            "render-status",
            "--template",
            str(template),
            "--results-db",
            str(db_path),
            "--output",
            str(out),
        ],
    )

    assert result.exit_code == 0, result.output
    assert out.read_text(encoding="utf-8") == "<p>no facts here</p>"


# ── the measured passes need results/cost_config.json ───────────────────────


@pytest.mark.parametrize("command", ["sql", "cold", "concurrency", "sustained"])
def test_measured_pass_without_cost_config_exits_2(command, tmp_path, monkeypatch):
    """Every measured pass refuses to run unpriced, with a message rather than a
    traceback: exit 2, pointing at capture-cost-config."""
    from typer.testing import CliRunner

    from benchmark import cli

    monkeypatch.setattr(cli, "_configure", lambda verbose=False: None)
    monkeypatch.chdir(tmp_path)  # results/cost_config.json resolves against the cwd

    result = CliRunner().invoke(
        cli.app, [command, "--platforms", "motherduck", "--results-db", str(tmp_path / "r.db")]
    )

    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "capture-cost-config" in result.output
