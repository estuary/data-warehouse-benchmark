"""Run provenance: tie every result row to the exact code + queries + config
that produced it.

``gather_provenance`` collects a best-effort snapshot (git SHA/dirty, harness
and tool versions, a content hash of the executed ``queries/<platform>/`` files,
and which engines actually ran an LLM optimization pass). It never raises — any
field it can't resolve comes back ``None`` — so provenance capture can't break a
benchmark run. The returned dict is passed straight to
``ResultsDB.record_run_provenance``.

.. note:: ``optimize_model`` records **observed** optimization, not configured
   intent. Echoing ``get_settings().OPTIMIZE_MODEL`` here would assert that every
   engine's SQL had been LLM-optimized whether or not the pass ran.
   See :func:`optimization_provenance`.
"""

from __future__ import annotations

import hashlib
import logging
import platform as _platform
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

logger = logging.getLogger("benchmark.provenance")

_PACKAGE = "estuary-corrdyn-2026-data-warehouse-benchmark"
_REPO_ROOT = Path(__file__).resolve().parents[2]
_QUERIES_DIR = _REPO_ROOT / "queries"


def _git(*args: str) -> str | None:
    """Run a git command in the repo root; return stripped stdout or None."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("git %s failed", " ".join(args), exc_info=True)
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _pkg_version(name: str) -> str | None:
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def _query_files_hash(platforms: list[str]) -> str | None:
    """SHA-256 over the sorted contents of the executed ``queries/<platform>/``
    .sql files, so a change to any executed query is detectable. Returns None if
    no files are found (e.g. queries not yet generated)."""
    files: list[Path] = []
    for platform in sorted(set(platforms)):
        pdir = _QUERIES_DIR / platform
        if pdir.is_dir():
            files.extend(sorted(pdir.rglob("*.sql")))
    if not files:
        return None
    digest = hashlib.sha256()
    for f in files:
        try:
            digest.update(f.relative_to(_QUERIES_DIR).as_posix().encode())
            digest.update(f.read_bytes())
        except OSError:
            logger.debug("could not hash %s", f, exc_info=True)
    return digest.hexdigest()


def optimization_provenance(platforms: list[str]) -> str | None:
    """Which of ``platforms`` actually ran an LLM optimization pass, and with what.

    Reads reality — the audit log `optimize` writes at
    ``results/optimization_<platform>.json`` — rather than the configured
    ``BENCHMARK_OPTIMIZE_MODEL``. Stamping the setting would make every run assert
    that every engine's SQL had been LLM-optimized, whether or not the pass ran:
    a config value describes intent, and provenance must record what happened.

    Returns:
        ``"<platform>:<model>"`` per optimized platform, comma-joined and sorted,
        or ``None`` when no targeted platform has an optimization log. ``None``
        means "no engine in this run ran the pass" — a positive statement, not a
        missing value.
    """
    found: list[str] = []
    for platform in sorted(set(platforms)):
        log = _REPO_ROOT / "results" / f"optimization_{platform}.json"
        if not log.is_file():
            continue
        model = None
        try:
            import json

            model = json.loads(log.read_text()).get("model")
        except (OSError, ValueError):
            logger.debug("could not read %s", log, exc_info=True)
        found.append(f"{platform}:{model or 'unknown-model'}")
    return ",".join(found) if found else None


def gather_provenance(
    *,
    command: str,
    scale_factor: int | None,
    run_type: str | None,
    platforms: list[str],
    blocks: list[str],
) -> dict[str, Any]:
    """Collect a best-effort provenance snapshot for one benchmark invocation."""
    git_status = _git("status", "--porcelain")
    tool_versions = {
        "python": _platform.python_version(),
        "duckdb": _pkg_version("duckdb"),
        "sqlglot": _pkg_version("sqlglot"),
        "anthropic": _pkg_version("anthropic"),
    }
    return {
        "command": command,
        "scale_factor": scale_factor,
        "run_type": run_type,
        "platforms": ",".join(platforms) if platforms else None,
        "blocks": ",".join(blocks) if blocks else None,
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": (git_status != "") if git_status is not None else None,
        "harness_version": _pkg_version(_PACKAGE),
        "optimize_model": optimization_provenance(platforms),
        "query_files_hash": _query_files_hash(platforms),
        "tool_versions": {k: v for k, v in tool_versions.items() if v is not None},
    }
