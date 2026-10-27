"""Cost-config capture & load — connector self-introspection (no UI, no env).

The cost-driving knob for each engine (MotherDuck Duckling tier, ClickHouse
vCPU×replicas, BigQuery edition, …) is read from the *live engine* via
``WarehouseConnector.runtime_compute()`` — the SAME introspection the SQL
benchmark uses for its drift check (actual-vs-provisioned compute) — and mapped
to cost-collector kwargs by the connector's pure
``cost_kwargs_from_compute()``. One query path feeds both verification and
cost, so the priced compute can never diverge from the ran compute. No web-UI
copy-paste, no duplication into ``env/*.env``.

Flow:
  1. ``benchmark capture-cost-config`` calls :func:`capture_cost_config`, which
     introspects each reachable platform (``runtime_compute()`` →
     ``cost_kwargs_from_compute()``) and writes ``results/cost_config.json``.
     The file is ALWAYS valid JSON: unreachable / non-self-reporting platforms
     are recorded under ``errors`` rather than aborting the capture. The raw
     ``runtime_compute()`` snapshot is kept under ``compute`` for provenance.
  2. At run time the benchmark calls :func:`load_cost_config` (raises if the file
     is missing — capture is a required pre-step) and :func:`collector_for`,
     which builds each platform's cost collector from the captured config and
     WARNS + falls back to collector defaults for any platform absent from the
     file.
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..connectors.base import WarehouseConnector, compute_label_from_environment
from ..connectors.registry import create_connector
from ..exceptions import BenchmarkError
from .registry import _load, create_cost_collector

if TYPE_CHECKING:
    from ..scale_manifest import ScaleManifest
    from .base import CostCollector

logger = logging.getLogger("benchmark.cost.discovery")

COST_CONFIG_VERSION = 1
DEFAULT_COST_CONFIG_PATH = Path("results/cost_config.json")


def capture_cost_config(
    platforms: list[str], out_path: Path = DEFAULT_COST_CONFIG_PATH
) -> dict[str, Any]:
    """Introspect each platform's cost driver and write a cost-config file.

    **Merges** into any existing file rather than overwriting: platforms not in this
    sweep are preserved, and each captured platform accumulates a per-**compute-size**
    entry under ``platforms[<p>]["sizes"][<label>]`` (keyed by the auto-derived
    ``compute_label``, e.g. ``"Pulse"`` / ``"Standard"``) in addition to the flat
    latest kwargs. So switching a Duckling/warehouse size and re-running
    ``capture-cost-config`` records the new size alongside the old — no hand-editing,
    and ``collector_for(..., compute_label=...)`` prices each run by its actual size.

    Always writes a valid JSON document, even if no platform is reachable —
    failures (with their reason) go under ``errors``. Returns the document written.
    """
    # Merge onto the existing file so other platforms + previously-captured sizes survive.
    existing: dict[str, Any] = {}
    if Path(out_path).exists():
        try:
            existing = json.loads(Path(out_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("could not read existing cost-config; starting fresh", exc_info=True)
    platforms_doc: dict[str, dict] = dict(existing.get("platforms", {}))
    compute_snapshots: dict[str, dict] = dict(existing.get("compute", {}))
    errors: dict[str, str] = {}
    captured = 0

    for platform in platforms:
        try:
            connector = create_connector(platform)
        except Exception as exc:  # record, never abort the sweep
            errors[platform] = f"connector build failed: {exc}"
            logger.warning("cost-config: %s connector build failed: %s", platform, exc)
            continue
        try:
            with connector:
                compute = connector.runtime_compute() or {}
                # env is only used to derive the size label — best-effort, never
                # let its absence/failure fail the cost capture.
                try:
                    env = connector.runtime_environment() or {}
                except Exception:
                    env = {}
            if compute:
                compute_snapshots[platform] = compute
            config = type(connector).cost_kwargs_from_compute(compute)
            if config is None:
                # Distinguish "the engine genuinely exposes no cost driver"
                # (connector overrides the translator but it returned None — e.g.
                # runtime_compute came back empty) from "we never wrote a
                # translator" (still the base default). Same None, very different
                # cause — don't blame the engine for our missing code.
                implemented = (
                    type(connector).cost_kwargs_from_compute
                    is not WarehouseConnector.cost_kwargs_from_compute
                )
                errors[platform] = (
                    "engine reports no cost driver via introspection"
                    if implemented
                    else "no cost-config introspection implemented for this platform"
                )
            else:
                # Same label derivation as the runner stamps on rows, so the key here
                # matches the compute_label used to select the collector at run time.
                label = compute_label_from_environment(
                    {**env, "platform": platform, "actual": compute}
                )
                entry = dict(platforms_doc.get(platform, {}))
                sizes = dict(entry.get("sizes", {}))
                if label:
                    sizes[label] = config
                entry = {**entry, **config}  # flat kwargs = latest capture
                if sizes:  # accumulate per-size only when we have a label to record
                    entry["sizes"] = sizes
                platforms_doc[platform] = entry
                captured += 1
                logger.info(
                    "cost-config: %s [%s] -> %s (from %s)", platform, label or "?", config, compute
                )
        except Exception as exc:  # unreachable / auth / perms — record, continue
            errors[platform] = str(exc)
            logger.warning("cost-config: %s introspection failed: %s", platform, exc)

    doc = {
        "version": COST_CONFIG_VERSION,
        "platforms": platforms_doc,
        "compute": compute_snapshots,
        "errors": errors,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
    logger.info(
        "cost-config written to %s (%d captured this run, %d errors)",
        out_path,
        captured,
        len(errors),
    )
    return doc


def load_cost_config(path: Path = DEFAULT_COST_CONFIG_PATH) -> dict[str, Any]:
    """Load the captured cost-config file. Raises if it does not exist."""
    path = Path(path)
    if not path.exists():
        raise BenchmarkError(
            f"cost-config file not found at {path}. Run "
            "`benchmark capture-cost-config` first — cost rates are sourced by "
            "introspecting each live engine, not from defaults."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _filter_to_ctor_kwargs(platform: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Keep only keys the collector constructor accepts (drops informational keys)."""
    sig = inspect.signature(_load(platform).__init__)
    accepted = {name for name in sig.parameters if name != "self"}
    return {k: v for k, v in raw.items() if k in accepted}


def collector_for(
    platform: str, cost_config: dict[str, Any], compute_label: str | None = None
) -> CostCollector:
    """Build ``platform``'s cost collector from captured config.

    When ``compute_label`` is given and the platform has a matching per-size entry
    under ``sizes`` (captured by :func:`capture_cost_config`), that size's kwargs are
    used — so a run prices by the compute size it actually ran on (e.g. MotherDuck
    Pulse vs Standard) with no hand-editing. Otherwise the flat latest kwargs are
    used. Falls back to collector defaults (with a WARNING) when the platform is
    absent from the file — e.g. it was unreachable at capture time.

    .. note:: :func:`collector_for_scale` builds the same collector from the
       scale-keyed manifest instead.
    """
    raw = (cost_config or {}).get("platforms", {}).get(platform)
    if raw is None:
        logger.warning(
            "no captured cost config for %s — using collector defaults; cost "
            "figures may be inaccurate. Re-run `capture-cost-config` once %s is "
            "reachable.",
            platform,
            platform,
        )
        return create_cost_collector(platform)
    kwargs = raw
    if compute_label:
        sized = (raw.get("sizes") or {}).get(compute_label)
        if sized is not None:
            kwargs = sized
            logger.info("cost: %s using size-specific config for '%s'", platform, compute_label)
        else:
            logger.info(
                "cost: %s has no size-specific config for '%s'; using flat kwargs",
                platform,
                compute_label,
            )
    return create_cost_collector(platform, _filter_to_ctor_kwargs(platform, kwargs))


def collector_for_scale(platform: str, scale: int, manifest: ScaleManifest) -> CostCollector:
    """Build ``platform``'s cost collector from the scale manifest's recorded kwargs.

    Reads ``cost_kwargs`` for ``(scale, platform)`` from the manifest and filters
    them to the collector constructor. Falls back to collector defaults (with a
    WARNING) when there is no recorded entry — e.g. the platform was unreachable
    when ``prepare-scale`` ran.
    """
    entry = manifest.entry(scale, platform)
    if entry is None or not entry.cost_kwargs:
        logger.warning(
            "no recorded cost config for %s at SF%s — using collector defaults; "
            "cost figures may be inaccurate. Re-run `prepare-scale --scale %s` "
            "once %s is reachable.",
            platform,
            scale,
            scale,
            platform,
        )
        return create_cost_collector(platform)
    return create_cost_collector(platform, _filter_to_ctor_kwargs(platform, entry.cost_kwargs))
