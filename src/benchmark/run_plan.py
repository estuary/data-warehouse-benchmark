"""Run-plan: the harness-owned per-scale compute sizing matrix.

Loaded from ``run_plan.yaml`` shipped beside this module (package data). It is the single source for the
target compute each engine should run at for a given scale, used by
``prepare-scale`` to size/validate infra and by the Snowflake connector to pick
its per-scale warehouse.

YAML shape (one sizing knob per engine, scale -> target):

    snowflake: { warehouse: { 1: XSMALL, 1000: LARGE } }
    redshift:  { baseline_rpu: { 1: 8, 1000: 64 } }

An engine may also carry recognized *policy* keys (currently ``on_demand``)
alongside its single sizing knob — each a scale -> allow|deny map:

    bigquery:
      reservation_slots: { 1: 100, 1000: 500 }
      on_demand:         { 1: allow, 1000: deny }

A missing ``(engine, scale)`` cell means "skip that platform at that scale".
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from .exceptions import BenchmarkError
from .scales import validate_scale

# Shipped beside this module (package data), so it resolves via the package's own
# location rather than a guessed repo root — works under an installed wheel too.
DEFAULT_RUN_PLAN_PATH = Path(__file__).resolve().parent / "run_plan.yaml"

# Per-engine keys that declare policy rather than the sizing target. They sit
# beside the single sizing knob; each maps scale -> one of POLICY_VALUES.
POLICY_KEYS = frozenset({"on_demand"})
#: Keys that are neither the sizing knob nor a policy. ``sweep`` declares the size
#: ladder a sizing campaign walks, which is a different question from ``sizes`` (the
#: ONE designated target per scale). Declaring it makes a sweep's intended extent
#: checkable: otherwise nothing can tell a deliberately short ladder from an
#: unfinished one.
#: ``load_sweep`` is the ladder the concurrency grid walks where it differs from the
#: single-user one: ClickHouse measures single queries on one replica (memory is the
#: knob) and load on three replicas (``3x8GB`` .. ``3x120GB``).
LADDER_KEYS = frozenset({"sweep", "load_sweep"})
POLICY_VALUES = frozenset({"allow", "deny"})


class EnginePlan(BaseModel):
    """One engine's sizing curve: the knob it feeds and the scale->target map,
    plus any per-scale policy maps (e.g. ``on_demand: {1000: deny}``)."""

    knob: str  # tfvar / cost-collector field this value drives, e.g. "baseline_rpu"
    sizes: dict[int, str | int]  # scale -> target value
    policies: dict[str, dict[int, str]] = {}  # policy name -> scale -> allow|deny
    sweep: list[str | int] = []  # the size ladder to sweep, smallest first
    load_sweep: list[str | int] = []  # the concurrency grid's ladder, when it differs


class RunPlan(BaseModel):
    """Per-engine sizing matrix keyed by scale."""

    engines: dict[str, EnginePlan]

    @classmethod
    def load(cls, path: Path = DEFAULT_RUN_PLAN_PATH) -> RunPlan:
        """Parse the run-plan YAML. Raises ``BenchmarkError`` on missing/invalid file."""
        path = Path(path)
        if not path.exists():
            raise BenchmarkError(
                f"run-plan not found at {path}. It defines per-scale compute sizing."
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise BenchmarkError(f"run-plan at {path} must be a mapping of engine -> knob")

        engines: dict[str, EnginePlan] = {}
        for engine, knob_map in raw.items():
            if not isinstance(knob_map, dict) or not knob_map:
                raise BenchmarkError(
                    f"run-plan[{engine}] must be a mapping of knob -> sizes, got {knob_map!r}"
                )
            policy_maps = {k: v for k, v in knob_map.items() if k in POLICY_KEYS}
            ladder = knob_map.get("sweep", [])
            load_ladder = knob_map.get("load_sweep", [])
            sizing = {
                k: v for k, v in knob_map.items() if k not in POLICY_KEYS and k not in LADDER_KEYS
            }
            if len(sizing) != 1:
                raise BenchmarkError(
                    f"run-plan[{engine}] must have exactly one sizing knob "
                    f"(plus optional {sorted(POLICY_KEYS | LADDER_KEYS)}), got {sorted(knob_map)}"
                )
            ((knob, sizes),) = sizing.items()
            if not isinstance(sizes, dict) or not sizes:
                raise BenchmarkError(f"run-plan[{engine}].{knob} must map scales to targets")
            norm: dict[int, str | int] = {}
            for scale, value in sizes.items():
                try:
                    norm[validate_scale(int(scale))] = value
                except ValueError as exc:
                    raise BenchmarkError(f"run-plan[{engine}].{knob}: {exc}") from exc
            policies: dict[str, dict[int, str]] = {}
            for name, pmap in policy_maps.items():
                if not isinstance(pmap, dict) or not pmap:
                    raise BenchmarkError(f"run-plan[{engine}].{name} must map scales to allow|deny")
                pnorm: dict[int, str] = {}
                for scale, value in pmap.items():
                    try:
                        key = validate_scale(int(scale))
                    except ValueError as exc:
                        raise BenchmarkError(f"run-plan[{engine}].{name}: {exc}") from exc
                    if value not in POLICY_VALUES:
                        raise BenchmarkError(
                            f"run-plan[{engine}].{name}[{scale}] must be one of "
                            f"{sorted(POLICY_VALUES)}, got {value!r}"
                        )
                    pnorm[key] = value
                policies[name] = pnorm
            for key, value in (("sweep", ladder), ("load_sweep", load_ladder)):
                if value and not isinstance(value, list):
                    raise BenchmarkError(
                        f"run-plan[{engine}].{key} must be a list of sizes, got {value!r}"
                    )
            engines[engine] = EnginePlan(
                knob=knob,
                sizes=norm,
                policies=policies,
                sweep=list(ladder),
                load_sweep=list(load_ladder),
            )
        return cls(engines=engines)

    def target(self, platform: str, scale: int) -> str | int | None:
        """Target value for ``platform`` at ``scale``; ``None`` => skip this cell."""
        plan = self.engines.get(platform)
        return plan.sizes.get(scale) if plan else None

    def sweep_ladder(self, platform: str) -> list[str | int]:
        """The size ladder a sizing campaign should walk for ``platform``.

        Empty when the engine declares none, which means "sweeping is not part of
        this engine's plan" — distinct from an unfinished sweep. Sourced from the
        vendor's own published sizing where one exists, and from this file's per-scale
        matrix where it does not.
        """
        plan = self.engines.get(platform)
        return list(plan.sweep) if plan else []

    def load_ladder(self, platform: str) -> list[str | int]:
        """The size ladder the concurrency grid walks for ``platform``.

        ``load_sweep`` where the engine declares one (ClickHouse runs load on three
        replicas, single queries on one), otherwise the same ladder as
        :meth:`sweep_ladder`.
        """
        plan = self.engines.get(platform)
        if plan is None:
            return []
        return list(plan.load_sweep or plan.sweep)

    def on_demand_allowed(self, platform: str, scale: int) -> bool | None:
        """Whether on-demand (no reservation) billing is allowed for ``platform``
        at ``scale``. ``None`` when no ``on_demand`` policy is declared (no opinion).
        """
        plan = self.engines.get(platform)
        if plan is None:
            return None
        policy = plan.policies.get("on_demand")
        if not policy or scale not in policy:
            return None
        return policy[scale] == "allow"


@lru_cache(maxsize=8)
def get_run_plan(path: Path | None = None) -> RunPlan:
    """Load and cache the run-plan (per process, keyed by path)."""
    return RunPlan.load(path or DEFAULT_RUN_PLAN_PATH)
