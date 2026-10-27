"""Settings: per-section env files + extra=forbid typo detection.

Each settings model reads only its own ``env/<section>.env`` so ``extra="forbid"``
catches a typo under that prefix while another engine's vars never trip it. This
is what lets the harness run current pydantic-settings (>=2.2), which feeds every
dotenv key to every nested model — a single shared .env would falsely forbid.
"""

from __future__ import annotations

import importlib

import pytest

import benchmark.settings as settings_mod


def test_each_section_reads_its_own_env_file():
    cfg = settings_mod.SnowflakeSettings.model_config
    assert cfg["env_file"].endswith("snowflake.env")
    assert cfg["env_prefix"] == "SNOWFLAKE_"
    assert cfg["extra"] == "forbid"  # strict by default
    assert settings_mod.AWSSettings.model_config["env_file"].endswith("aws.env")
    assert settings_mod.BenchmarkSettings.model_config["env_file"].endswith("benchmark.env")


@pytest.fixture
def settings_with_env_dir(monkeypatch, tmp_path):
    """Reload settings pointing ENV_DIR at a fresh tmp dir; restore after."""
    monkeypatch.setenv("BENCHMARK_ENV_DIR", str(tmp_path))
    monkeypatch.setenv("BENCHMARK_SETTINGS_EXTRA", "forbid")
    importlib.reload(settings_mod)
    settings_mod.get_settings.cache_clear()
    yield settings_mod, tmp_path
    monkeypatch.undo()
    importlib.reload(settings_mod)
    settings_mod.get_settings.cache_clear()


def test_clean_multi_engine_load(settings_with_env_dir):
    s_mod, env_dir = settings_with_env_dir
    (env_dir / "aws.env").write_text("AWS_REGION=eu-west-1\n")
    (env_dir / "snowflake.env").write_text("SNOWFLAKE_ACCOUNT=acct123\n")
    s_mod.get_settings.cache_clear()
    s = s_mod.get_settings()
    # both sections load under forbid; neither trips on the other's var
    assert s.AWS.REGION == "eu-west-1"
    assert s.SNOWFLAKE.ACCOUNT == "acct123"


def test_typo_in_section_file_is_forbidden(settings_with_env_dir):
    s_mod, env_dir = settings_with_env_dir
    (env_dir / "snowflake.env").write_text("SNOWFLAKE_ACCOUNT=acct123\nSNOWFLAKE_TYPOO=oops\n")
    s_mod.get_settings.cache_clear()
    with pytest.raises(Exception, match=r"TYPOO|[Ee]xtra"):
        s_mod.get_settings()


def test_databricks_loader_defaults_to_the_benchmark_warehouse(settings_with_env_dir, monkeypatch):
    s_mod, env_dir = settings_with_env_dir
    monkeypatch.delenv("DATABRICKS_SQL_HTTP_PATH", raising=False)
    monkeypatch.delenv("DATABRICKS_LOADER_SQL_HTTP_PATH", raising=False)
    (env_dir / "databricks.env").write_text("DATABRICKS_SQL_HTTP_PATH=/sql/1.0/warehouses/bench\n")
    s_mod.get_settings.cache_clear()
    s = s_mod.get_settings()
    assert s.DATABRICKS.LOADER_SQL_HTTP_PATH == "/sql/1.0/warehouses/bench"
    assert s.env_dict()["DATABRICKS_LOADER_SQL_HTTP_PATH"] == "/sql/1.0/warehouses/bench"


def test_databricks_loader_can_point_elsewhere(settings_with_env_dir, monkeypatch):
    s_mod, env_dir = settings_with_env_dir
    monkeypatch.delenv("DATABRICKS_SQL_HTTP_PATH", raising=False)
    monkeypatch.delenv("DATABRICKS_LOADER_SQL_HTTP_PATH", raising=False)
    (env_dir / "databricks.env").write_text(
        "DATABRICKS_SQL_HTTP_PATH=/sql/1.0/warehouses/bench\n"
        "DATABRICKS_LOADER_SQL_HTTP_PATH=/sql/1.0/warehouses/loader\n"
    )
    s_mod.get_settings.cache_clear()
    d = s_mod.get_settings().DATABRICKS
    assert (d.SQL_HTTP_PATH, d.LOADER_SQL_HTTP_PATH) == (
        "/sql/1.0/warehouses/bench",
        "/sql/1.0/warehouses/loader",
    )
