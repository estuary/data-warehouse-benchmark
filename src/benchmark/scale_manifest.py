"""Scale-keyed readiness + cost manifest (``results/scale_manifest.json``).

One entry per ``(scale, platform)`` recording the compute target, the live
introspection snapshot, the cost-collector kwargs derived from it, and the two
readiness flags. :func:`benchmark.cost.discovery.collector_for_scale` builds a
cost collector from an entry. The CLI neither writes nor reads this file: its
passes price from ``results/cost_config.json`` (``capture-cost-config``).

JSON object keys are strings, so scale keys round-trip as ``"1000"`` on disk;
pydantic coerces them back to ``int`` on load.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .exceptions import BenchmarkError

logger = logging.getLogger("benchmark.scale_manifest")

SCALE_MANIFEST_VERSION = 2
DEFAULT_SCALE_MANIFEST_PATH = Path("results/scale_manifest.json")


class ScaleEntry(BaseModel):
    """Readiness + cost facts for one platform at one scale."""

    target: str | int | None = None  # run-plan target the live infra was checked against
    live: dict[str, Any] = Field(default_factory=dict)  # runtime_compute() snapshot (provenance)
    cost_kwargs: dict[str, Any] = Field(default_factory=dict)  # -> cost collector ctor
    compute_validated: bool = False  # live compute matched the run-plan target
    data_ready: bool = False  # scale's data is loaded for this platform
    captured_at: str | None = None

    @property
    def ready(self) -> bool:
        """A platform may run at this scale only when both axes pass."""
        return self.compute_validated and self.data_ready


class ScaleManifest(BaseModel):
    """All recorded ``(scale -> platform -> entry)`` readiness facts."""

    version: int = SCALE_MANIFEST_VERSION
    scales: dict[int, dict[str, ScaleEntry]] = Field(default_factory=dict)

    def entry(self, scale: int, platform: str) -> ScaleEntry | None:
        """The recorded entry for ``(scale, platform)``, or ``None`` if absent."""
        return self.scales.get(scale, {}).get(platform)


def load_scale_manifest(path: Path = DEFAULT_SCALE_MANIFEST_PATH) -> ScaleManifest:
    """Load the manifest. Raises ``BenchmarkError`` if it does not exist."""
    path = Path(path)
    if not path.exists():
        raise BenchmarkError(
            f"scale manifest not found at {path}. Run "
            "`benchmark prepare-scale --scale N` first — the benchmark prices off "
            "and gates on per-scale readiness recorded there."
        )
    return ScaleManifest.model_validate_json(path.read_text(encoding="utf-8"))


def record_scale(
    scale: int,
    platform: str,
    entry: ScaleEntry,
    path: Path = DEFAULT_SCALE_MANIFEST_PATH,
    *,
    when: datetime | None = None,
) -> ScaleManifest:
    """Upsert one ``(scale, platform)`` entry and write the manifest atomically.

    Loads the existing manifest (or starts empty), stamps ``captured_at`` when
    unset, replaces the entry for this ``(scale, platform)``, and writes via a
    temp file + rename so a crash never leaves a half-written manifest.
    """
    path = Path(path)
    manifest = load_scale_manifest(path) if path.exists() else ScaleManifest()
    if entry.captured_at is None:
        stamp = (when or datetime.now(UTC)).isoformat()
        entry = entry.model_copy(update={"captured_at": stamp})
    manifest.scales.setdefault(scale, {})[platform] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    logger.info(
        "recorded scale manifest entry", extra={"platform": platform, "scale_factor": scale}
    )
    return manifest
