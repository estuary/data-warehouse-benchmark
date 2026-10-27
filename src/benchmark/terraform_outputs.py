"""Read provisioned compute config from exported terraform outputs.

``benchmark export-terraform-outputs`` (``terraform output -json``) writes the
file; the harness reads it at runtime to record each engine's provisioned
compute — warehouse size, RPUs, reservation slots, capacity SKU — into the
``engine_environment`` table, keyed per scale. Resilient by design: a missing or
invalid file yields ``{}`` so a benchmark run never depends on terraform being
reachable.

The terraform root exposes a single ``engine_compute`` output keyed by platform
(see ``terraform/main.tf``). Snowflake is the one engine that sizes compute by
scale, so its slice carries a scale->size map (``warehouse_sizes``);
:func:`provisioned_compute` resolves it to the size for the requested scale. The
other engines provision a single size or an autoscaling range that doesn't vary
by scale, so their slice is returned as-is.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
from functools import lru_cache
from pathlib import Path

from .settings import get_settings

logger = logging.getLogger("benchmark.terraform_outputs")

# Terraform creates the GCP service-account keys but can't write them to the
# operator's disk; the connectors read them from a file path (BIGQUERY_GCP_SA_KEY,
# ESTUARY_BIGQUERY_SERVICE_ACCOUNT_KEY). This maps each sensitive root output
# (a base64-encoded SA-key JSON, from google_service_account_key.private_key) to
# the settings field holding its destination path, so `export-terraform-outputs`
# can materialize them without a manual "decode + write the key file" step.
# (output_name, settings_path_getter)
_CREDENTIAL_FILE_OUTPUTS = (
    ("bigquery_runner_sa_key", lambda s: s.BIGQUERY.GCP_SA_KEY),
    ("bigquery_estuary_sa_key", lambda s: s.ESTUARY.BIGQUERY_SERVICE_ACCOUNT_KEY),
)


def _output_value(outputs: dict, name: str) -> str | None:
    """Pull a scalar value from a ``terraform output -json`` block, tolerating
    both the ``{"name": {"value": ...}}`` wrapper and a flat ``{"name": ...}``."""
    entry = outputs.get(name)
    if isinstance(entry, dict) and "value" in entry:
        entry = entry["value"]
    return entry if isinstance(entry, str) and entry else None


def materialize_credential_files(outputs: dict) -> list[Path]:
    """Write terraform-managed SA-key files to the paths the connectors expect.

    For each ``(_CREDENTIAL_FILE_OUTPUTS)`` entry whose destination path is
    configured and whose output is present, base64-decode the SA-key JSON, verify
    it parses, and write it ``0600``. Idempotent (re-running rewrites identical
    content). Returns the paths written. An output that's null/absent (engine not
    provisioned) or a destination that's unset is skipped, not an error.
    """
    settings = get_settings()
    written: list[Path] = []
    for name, get_path in _CREDENTIAL_FILE_OUTPUTS:
        dest = get_path(settings)
        if dest is None:
            continue
        raw = _output_value(outputs, name)
        if raw is None:
            logger.debug("output %s absent/null — skipping key materialization", name)
            continue
        try:
            decoded = base64.b64decode(raw)
            json.loads(decoded)  # validate it's well-formed SA-key JSON
        except (ValueError, json.JSONDecodeError):
            logger.warning("output %s is not base64-encoded JSON — skipping", name, exc_info=True)
            continue
        dest_path = Path(os.path.expanduser(str(dest)))
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(decoded)
        dest_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — it's a secret
        logger.info("materialized %s -> %s (0600)", name, dest_path)
        written.append(dest_path)
    return written


@lru_cache(maxsize=1)
def get_terraform_outputs() -> dict:
    """Parse the exported ``terraform output -json`` file (cached for the process).

    Returns ``{}`` if the file is absent or unparseable — provisioned compute is
    optional enrichment, never a hard dependency.
    """
    path = get_settings().TERRAFORM_OUTPUTS_PATH
    if not path.exists():
        logger.debug("terraform outputs file not found at %s", path)
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        logger.warning("could not read terraform outputs at %s", path, exc_info=True)
        return {}
    return data if isinstance(data, dict) else {}


def provisioned_compute(platform: str, scale_factor: int) -> dict:
    """Terraform-provisioned compute for one engine *at one scale*, or ``{}``.

    Snowflake picks its warehouse per scale, so its terraform slice carries a
    ``warehouse_sizes`` scale->size map; this resolves it to ``warehouse_size``
    for ``scale_factor``. Other engines provision a single size / autoscaling
    range that doesn't vary by scale, so their slice is returned unchanged (still
    recorded per-scale in ``engine_environment``).

    Accepts both the ``terraform output -json`` wrapper shape
    (``{"engine_compute": {"value": {...}}}``) and a flat
    (``{"engine_compute": {...}}``) shape.
    """
    block = get_terraform_outputs().get("engine_compute")
    if isinstance(block, dict) and "value" in block:
        block = block["value"]
    if not isinstance(block, dict):
        return {}
    cfg = block.get(platform)
    if not isinstance(cfg, dict):
        return {}

    cfg = dict(cfg)  # copy before mutating
    warehouse_sizes = cfg.pop("warehouse_sizes", None)
    if isinstance(warehouse_sizes, dict) and warehouse_sizes:
        # terraform JSON keys are strings; tolerate int keys too.
        size = warehouse_sizes.get(str(scale_factor), warehouse_sizes.get(scale_factor))
        if size is not None:
            cfg["warehouse_size"] = size
    return cfg
