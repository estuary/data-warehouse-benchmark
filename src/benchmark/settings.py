"""Typed settings for the benchmark harness.

One root :class:`BenchmarkSettings` composes nested per-engine and
cross-cutting settings models. Every model carries an ``env_prefix`` and
``extra="forbid"`` so a typo in ``.env`` fails fast rather than silently
falling back to ``None``. Field names are upper-case to mirror the
on-disk env var names; ruff's pep8-naming rule is disabled for this file
in ``pyproject.toml``.

Access via :func:`get_settings` (process-cached). Tests can reset with
``get_settings.cache_clear()``.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import BeforeValidator, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

logger = logging.getLogger("benchmark.settings")


def _expand_path(value: object) -> object:
    """``pathlib.Path('~/x')`` does not auto-expand — bring back the tilde."""
    if value is None or isinstance(value, Path):
        return Path(str(value)).expanduser() if value is not None else None
    if isinstance(value, str):
        return Path(value).expanduser()
    return value


# Apply ``~`` expansion before pydantic constructs the Path. Used by any
# settings field that holds a filesystem path the operator might write with
# a leading tilde.
UserPath = Annotated[Path, BeforeValidator(_expand_path)]

# Per-section env files: each settings model reads ONLY its own
# ``<ENV_DIR>/<section>.env`` (default dir "env"; override with BENCHMARK_ENV_DIR).
# This is what lets ``extra="forbid"`` work cleanly: a model sees only its own
# prefix's vars, so a typo under that prefix fails fast, while another engine's
# vars can never trip the forbid check. (A single shared .env cannot do this on
# pydantic-settings >=2.2, which feeds every dotenv key to every nested model.)
ENV_DIR = Path(os.environ.get("BENCHMARK_ENV_DIR", "env"))

# Strictness lever. "forbid" (default) makes a typo'd or renamed var in a
# section's env file fail fast; the `settings` diagnostic CLI sets "ignore" so
# `show` / `generate` keep working against a dirty file.
_EXTRA = os.environ.get("BENCHMARK_SETTINGS_EXTRA", "forbid")


def _cfg(env_prefix: str, section: str) -> SettingsConfigDict:
    """Settings config for one section: its env prefix + its own env file."""
    return SettingsConfigDict(
        env_prefix=env_prefix,
        env_file=str(ENV_DIR / f"{section}.env"),
        env_file_encoding="utf-8",
        extra=_EXTRA,
        case_sensitive=True,
        env_ignore_empty=True,  # blank `VAR=` → treat as unset → field default (None)
    )


# ---------------------------------------------------------------------------
# Shared base — every settings model inherits from this so .env always wins
# over the parent shell (avoids surprises like AWS_PROFILE leaking in from
# ~/.bashrc).
# ---------------------------------------------------------------------------


class BenchmarkBaseSettings(BaseSettings):
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Default order: init > env > dotenv > file_secret.
        # Swap env <-> dotenv so .env always wins over os.environ.
        return (init_settings, dotenv_settings, env_settings, file_secret_settings)


# ---------------------------------------------------------------------------
# Shared / cross-cutting
# ---------------------------------------------------------------------------


class AWSSettings(BenchmarkBaseSettings):
    """Shared AWS credentials. Used by Redshift IAM auth (when REDSHIFT_PASSWORD
    is blank) and by `benchmark stage-data` uploads to the staging bucket."""

    model_config = _cfg("AWS_", "aws")

    REGION: str = Field(
        default="us-east-1",
        description="AWS region for Redshift Serverless and the S3 staging bucket.",
    )
    PROFILE: str = Field(
        default="benchmark",
        description="Local AWS credentials profile name (read by boto3).",
    )


class EstuarySettings(BenchmarkBaseSettings):
    """Estuary Flow control-plane credentials and per-engine connector secrets
    injected into materialization YAML."""

    model_config = _cfg("ESTUARY_", "estuary")

    REFRESH_TOKEN: SecretStr | None = Field(
        default=None,
        description="flowctl auth token; required to apply captures/materializations.",
    )
    ACCOUNT: str = Field(
        default="",
        description="Estuary account/tenant prefix (e.g. 'CorrDyn'); namespaces collection paths.",
    )
    WSL_DISTRO: str = Field(
        default="Ubuntu-24.04",
        description="On Windows, the WSL distro flowctl runs in (flowctl ships Linux-only). Must be installed (`wsl -l -v`) and have flowctl on PATH.",
    )
    OPENMETRICS_URL: str = Field(
        default="https://api.estuary.dev/api/v1/metrics",
        description="Base URL of Estuary's OpenMetrics API (docs.estuary.dev/reference/openmetrics-api); '/{prefix}/' is appended per request. Auth is REFRESH_TOKEN as a Bearer header.",
    )
    S3_AWS_ACCESS_KEY_ID: SecretStr | None = Field(
        default=None,
        description="Least-privilege reader key for Estuary captures pulling from the staging bucket.",
    )
    S3_AWS_SECRET_ACCESS_KEY: SecretStr | None = Field(
        default=None,
        description="Secret half of the Estuary S3 reader credentials.",
    )
    BIGQUERY_SERVICE_ACCOUNT_KEY: UserPath | None = Field(
        default=None,
        description="Filesystem path to the JSON SA key for the Estuary materialize-bigquery connector.",
    )

    @property
    def BIGQUERY_SERVICE_ACCOUNT_JSON(self) -> str | None:
        """Compact single-line JSON content of the configured SA key file.

        Treated as a "computed field" by :meth:`BenchmarkSettings.env_dict`, so
        materialization YAMLs can reference ``${ESTUARY_BIGQUERY_SERVICE_ACCOUNT_JSON}``
        directly. Parses + re-dumps the file (instead of passing the raw text)
        because GCP service-account JSON tends to be pretty-printed with
        newlines, which would break ``credentials_json: '...'`` in single-quoted
        YAML.
        """
        if self.BIGQUERY_SERVICE_ACCOUNT_KEY is None:
            return None
        parsed = json.loads(self.BIGQUERY_SERVICE_ACCOUNT_KEY.read_text())
        return json.dumps(parsed, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Per-engine
# ---------------------------------------------------------------------------


class SnowflakeSettings(BenchmarkBaseSettings):
    """Snowflake (Gen2 Standard) credentials and warehouse defaults."""

    model_config = _cfg("SNOWFLAKE_", "snowflake")

    ACCOUNT: str | None = Field(
        default=None,
        description="Snowflake account identifier (e.g. 'xy12345.us-east-1.aws').",
    )
    BENCHMARK_USER: str | None = Field(
        default=None,
        description="Service user the harness + Estuary authenticate as (created by terraform; distinct from the admin user terraform itself runs as).",
    )
    BENCHMARK_PRIVATE_KEY_FILE: UserPath | None = Field(
        default=None,
        description="Filesystem path to the PKCS#8 PEM private key for BENCHMARK_USER. Operator generates the RSA keypair locally with openssl (private key never enters terraform state); terraform consumes only the public half via snowflake_benchmark_user_rsa_public_key. Consumed by the harness via snowflake-connector-python's private_key_file= arg and by Estuary's materialize-snowflake credentials block (JWT mode) via the BENCHMARK_PRIVATE_KEY computed property.",
    )
    ROLE: str = Field(
        default="BENCHMARK_ROLE", description="Role granted on the warehouse and database."
    )
    # No WAREHOUSE field: the per-scale warehouse is resolved from the run-plan
    # (src/benchmark/run_plan.yaml) by the SnowflakeConnector, not from env.
    DATABASE: str = Field(
        default="BENCHMARK_DB",
        description="Database holding TPC-DS/DSB/ClickBench tables and benchmark staging.",
    )
    REGION: str = Field(
        default="us-east-1",
        description="Snowflake region; cost telemetry uses this to resolve credit rates.",
    )
    CREDIT_RATE_USD: float = Field(
        default=4.00,
        description="Region-specific credit price (USD/credit) for cost attribution.",
    )

    @property
    def BENCHMARK_PRIVATE_KEY(self) -> str | None:
        """PEM content of the BENCHMARK_PRIVATE_KEY_FILE.

        Treated as a "computed field" by :meth:`BenchmarkSettings.env_dict`,
        so Estuary materialization YAMLs can reference
        ``${SNOWFLAKE_BENCHMARK_PRIVATE_KEY}`` directly and get the full PEM
        body inlined into the materialize-snowflake `credentials.private_key`
        block (JWT auth mode).
        """
        if self.BENCHMARK_PRIVATE_KEY_FILE is None:
            return None
        return self.BENCHMARK_PRIVATE_KEY_FILE.read_text()


class DatabricksSettings(BenchmarkBaseSettings):
    """Databricks SQL Warehouse (serverless) credentials."""

    model_config = _cfg("DATABRICKS_", "databricks")

    HOST: str | None = Field(
        default=None,
        description="Workspace URL host (e.g. 'adb-1234.5.azuredatabricks.net').",
    )
    TOKEN: SecretStr | None = Field(
        default=None, description="Personal access token for the benchmark service principal."
    )
    SQL_HTTP_PATH: str | None = Field(
        default=None, description="HTTP path of the SQL warehouse endpoint."
    )
    LOADER_SQL_HTTP_PATH: str | None = Field(
        default=None,
        description="HTTP path of the SQL warehouse Estuary's materializations write through; "
        "unset means the benchmark warehouse (SQL_HTTP_PATH). A separate loader keeps a long "
        "load off the warehouse being measured.",
    )
    CATALOG: str = Field(
        default="benchmark_catalog",
        description="Unity Catalog name holding the benchmark tables.",
    )

    @model_validator(mode="after")
    def _loader_defaults_to_benchmark_warehouse(self) -> DatabricksSettings:
        if self.LOADER_SQL_HTTP_PATH is None:
            self.LOADER_SQL_HTTP_PATH = self.SQL_HTTP_PATH
        return self


class BigQuerySettings(BenchmarkBaseSettings):
    """BigQuery (Enterprise reservation) project and dataset, plus the Estuary
    GCS staging bucket and the harness service-account key."""

    model_config = _cfg("BIGQUERY_", "bigquery")

    PROJECT: str | None = Field(
        default=None, description="GCP project ID hosting the benchmark dataset."
    )
    REGION: str = Field(
        default="us-central1",
        description="BigQuery region; pins slot reservation + Estuary staging location.",
    )
    DATASET: str = Field(
        default="tpcds", description="Dataset holding TPC-DS/DSB/ClickBench tables."
    )
    GCP_SA_KEY: UserPath | None = Field(
        default=None,
        description="Path to the harness runner SA JSON (BigQuery jobUser + dataEditor).",
    )
    GCS_STAGING_BUCKET: str | None = Field(
        default=None,
        description="GCS bucket the Estuary materialize-bigquery connector stages into.",
    )


class RedshiftSettings(BenchmarkBaseSettings):
    """Amazon Redshift Serverless workgroup."""

    model_config = _cfg("REDSHIFT_", "redshift")

    WORKGROUP: str | None = Field(
        default=None,
        description="Serverless workgroup name; resolved via boto3 when ENDPOINT is blank.",
    )
    DATABASE: str = Field(default="benchmark_db", description="Database name on the workgroup.")
    USER: str | None = Field(
        default=None,
        description="Required for Estuary loads (password-only); optional for the Python connector (falls back to IAM).",
    )
    PASSWORD: SecretStr | None = Field(default=None, description="Password for USER.")
    ENDPOINT: str | None = Field(
        default=None,
        description="Optional explicit endpoint override; otherwise looked up from the workgroup.",
    )


class FabricSettings(BenchmarkBaseSettings):
    """Microsoft Fabric tenant credentials. Drives the Data Warehouse
    component (the SQL benchmark)."""

    model_config = _cfg("FABRIC_", "fabric")

    TENANT_ID: str | None = Field(
        default=None, description="Azure AD tenant ID for the Fabric workspace."
    )
    CLIENT_ID: str | None = Field(
        default=None, description="Service principal app ID with Fabric workspace access."
    )
    CLIENT_SECRET: SecretStr | None = Field(
        default=None, description="Secret for the service principal."
    )
    DW_ENDPOINT: str | None = Field(
        default=None,
        description="Fabric Data Warehouse SQL endpoint.",
    )
    DW_DATABASE: str = Field(
        default="BENCHMARK_DW", description="Database name on the DW endpoint."
    )
    STAGING_STORAGE_ACCOUNT: str | None = Field(
        default=None,
        description="Azure Storage account the materialize-azure-fabric-warehouse connector stages Parquet into before COPY INTO.",
    )
    STAGING_STORAGE_KEY: SecretStr | None = Field(
        default=None,
        description="Account key for STAGING_STORAGE_ACCOUNT (Estuary writes/reads the staging container).",
    )
    STAGING_CONTAINER: str | None = Field(
        default=None,
        description="Container name inside STAGING_STORAGE_ACCOUNT used as the Estuary staging area.",
    )
    CAPACITY_SKU: str = Field(
        default="f64",
        description="Provisioned Fabric capacity SKU (f2/f4/f8/f16/f32/f64/f128); drives PROPORTIONAL cost attribution.",
    )
    SUBSCRIPTION_ID: str | None = Field(
        default=None,
        description="Azure subscription holding the Fabric capacity — for the ARM lookup that validates the running SKU at runtime.",
    )
    RESOURCE_GROUP: str | None = Field(
        default=None,
        description="Resource group of the Fabric capacity (ARM capacity lookup).",
    )
    CAPACITY_NAME: str | None = Field(
        default=None,
        description="Fabric capacity resource name (ARM lookup; validates actual SKU vs CAPACITY_SKU).",
    )


class ClickHouseSettings(BenchmarkBaseSettings):
    """ClickHouse Cloud credentials. Single service ('Estuary Benchmark')
    provisioned out-of-band; terraform only records its UUID. HOST and PORT
    come from the service overview in the Cloud UI; PASSWORD must be copied
    from the Cloud UI too (the API doesn't expose it)."""

    model_config = _cfg("CLICKHOUSE_", "clickhouse")

    HOST: str | None = Field(
        default=None,
        description="Service hostname, from the service overview in the ClickHouse Cloud UI.",
    )
    PORT: int = Field(
        default=8443,
        description="HTTPS port for clickhouse-connect; 8443 secure, 8123 plain.",
    )
    USER: str = Field(
        default="default",
        description="Username for query traffic; the Cloud-provisioned default user is 'default'.",
    )
    PASSWORD: SecretStr | None = Field(
        default=None,
        description="Password for USER. Copy from the ClickHouse Cloud UI — the API does not expose it.",
    )
    ORG_ID: str | None = Field(
        default=None,
        description="ClickHouse Cloud organization ID — for the Cloud API call that validates the running service size.",
    )
    SERVICE_ID: str | None = Field(
        default=None,
        description="ClickHouse Cloud service ID (Cloud API service lookup).",
    )
    API_KEY_ID: str | None = Field(
        default=None,
        description="ClickHouse Cloud API key ID (Basic-auth user for the Cloud API).",
    )
    API_KEY_SECRET: SecretStr | None = Field(
        default=None,
        description="ClickHouse Cloud API key secret (Basic-auth password for the Cloud API).",
    )


class MotherDuckSettings(BenchmarkBaseSettings):
    """MotherDuck token. Hosts the benchmark database; DuckDB-compatible connector."""

    model_config = _cfg("MOTHERDUCK_", "motherduck")

    TOKEN: SecretStr | None = Field(
        default=None,
        description="MotherDuck service token (read/write on the benchmark database).",
    )
    ACCOUNT_USER: str | None = Field(
        default=None,
        description=(
            "MotherDuck username that owns TOKEN; the instances REST API used by "
            "scripts/resize/motherduck_resize.py is keyed by it."
        ),
    )


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------


class BenchmarkSettings(BenchmarkBaseSettings):
    """Top-level harness settings. Cross-cutting BENCHMARK_* vars (staging
    bucket, optimize-pass API key) plus nested per-engine and shared
    sub-trees. Instantiate once via :func:`get_settings`."""

    model_config = _cfg("BENCHMARK_", "benchmark")

    # Root-level vars (BENCHMARK_*).
    S3_BUCKET: str | None = Field(
        default=None,
        description="Shared S3 bucket Estuary captures stage Parquet to before materialization.",
    )
    ANTHROPIC_API_KEY: SecretStr | None = Field(
        default=None,
        description="Anthropic API key for the optimize pass's `--via sdk` backend.",
    )
    OPTIMIZE_MODEL: str = Field(
        default="claude-opus-4-8",
        description=(
            "Anthropic model id for the per-engine query optimization pass "
            "(`benchmark optimize`); override if a different model is preferred."
        ),
    )

    TERRAFORM_OUTPUTS_PATH: Path = Field(
        default=Path("terraform/outputs.json"),
        description=(
            "Path to exported `terraform output -json` (written by "
            "`benchmark export-terraform-outputs`). Read at runtime to record each "
            "engine's provisioned compute (warehouse size, RPUs, slots, capacity SKU) "
            "into engine_environment, per scale. Missing/invalid file -> omitted."
        ),
    )

    # Shared sub-trees.
    AWS: AWSSettings = Field(default_factory=AWSSettings)
    ESTUARY: EstuarySettings = Field(default_factory=EstuarySettings)

    # Per-engine sub-trees.
    SNOWFLAKE: SnowflakeSettings = Field(default_factory=SnowflakeSettings)
    DATABRICKS: DatabricksSettings = Field(default_factory=DatabricksSettings)
    BIGQUERY: BigQuerySettings = Field(default_factory=BigQuerySettings)
    REDSHIFT: RedshiftSettings = Field(default_factory=RedshiftSettings)
    FABRIC: FabricSettings = Field(default_factory=FabricSettings)
    CLICKHOUSE: ClickHouseSettings = Field(default_factory=ClickHouseSettings)
    MOTHERDUCK: MotherDuckSettings = Field(default_factory=MotherDuckSettings)

    def env_dict(self) -> dict[str, str]:
        """Flatten the settings tree into ``{ENV_VAR_NAME: value}`` for YAML
        substitution (e.g. Estuary flow specs that use ``${VAR}`` placeholders).

        - SecretStr values are unwrapped.
        - Path values are rendered with ``str()``.
        - list[str] values are joined with commas.
        - ``None`` fields are *omitted* so the placeholder substitution can
          detect them as missing rather than silently inserting an empty string.
        """

        def _stringify(value: object) -> str:
            if isinstance(value, SecretStr):
                return value.get_secret_value()
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, list):
                return ",".join(str(v) for v in value)
            return str(value)

        out: dict[str, str] = {}

        def _add(model: BaseSettings, prefix: str) -> None:
            for fname in model.__class__.model_fields:
                value = getattr(model, fname)
                if isinstance(value, BaseSettings):
                    continue
                if value is None:
                    continue
                out[f"{prefix}{fname}"] = _stringify(value)
            # @property-defined computed fields on this class (named in ALL CAPS
            # so they read like ordinary env vars at lookup time).
            for pname, attr in vars(model.__class__).items():
                if not isinstance(attr, property):
                    continue
                # A computed field may read a backing file (e.g. a private key
                # or SA-JSON path). If that file is missing/unreadable we OMIT
                # the var rather than failing the whole map — unrelated specs
                # still render, and `_substitute` reports it as missing only if
                # a spec actually references this placeholder.
                try:
                    value = getattr(model, pname)
                except (
                    Exception
                ) as exc:  # tolerate any computed-field error (e.g. missing key file)
                    logger.debug(
                        "omitting computed setting %s%s from env_dict: %s",
                        prefix,
                        pname,
                        exc,
                    )
                    continue
                if value is None:
                    continue
                out[f"{prefix}{pname}"] = _stringify(value)

        _add(self, self.model_config.get("env_prefix", "") or "")
        for attr in (
            "AWS",
            "ESTUARY",
            "SNOWFLAKE",
            "DATABRICKS",
            "BIGQUERY",
            "REDSHIFT",
            "FABRIC",
            "CLICKHOUSE",
            "MOTHERDUCK",
        ):
            nested: BaseSettings = getattr(self, attr)
            _add(nested, nested.model_config.get("env_prefix", "") or "")

        return out


@lru_cache(maxsize=1)
def get_settings() -> BenchmarkSettings:
    return BenchmarkSettings()
