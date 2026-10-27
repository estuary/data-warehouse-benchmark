"""flowctl wrapper — env substitution + cross-platform invocation.

The Estuary YAMLs under ``estuary/`` use ``${VAR}`` placeholders so they
stay credential-free in version control. flowctl itself does NOT do env
substitution; the caller has to do it. This module:

1. Reads each YAML, substitutes ``${VAR}`` from
   :meth:`BenchmarkSettings.env_dict` (typed settings flattened to a
   ``{KEY: value}`` view), writes the substituted text to
   ``estuary/.applied/<name>.yaml``.
2. Invokes ``flowctl`` natively on macOS/Linux, or through WSL on Windows
   (Ubuntu 22.04 — flowctl ships Linux-only, no native Windows binary).

Authentication is carried per-invocation via the ``FLOW_AUTH_TOKEN`` environment
variable (see :func:`_flowctl_env`), set to the Estuary **refresh token**
(``ESTUARY_REFRESH_TOKEN``). A refresh token is opaque and long-lived — flowctl
exchanges it for a fresh access token on every call — so this doesn't expire.
``flowctl auth token --token`` accepts only a short-lived (~1h) access-token JWT,
so auth through it breaks hourly; it is not used here.

CLI surface in ``cli.py``: ``benchmark flow-auth`` and
``benchmark flow-publish <yaml-name>``.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..exceptions import BenchmarkError
from ..settings import get_settings

logger = logging.getLogger("benchmark.staging.estuary")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
ESTUARY_DIR = REPO_ROOT / "estuary"
APPLIED_DIR = ESTUARY_DIR / ".applied"
PLACEHOLDER_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")

# flowctl emits ANSI color/style codes even when its output is captured to a
# pipe; left in, they leak raw escapes into the JSON log file and error messages
# (and render as garbage on a non-TTY). Strip them from all captured output.
_ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _clean(text: str) -> str:
    """Strip ANSI escape codes and surrounding whitespace from captured output."""
    return _ANSI_PATTERN.sub("", text).strip()


# flowctl emits one structured ERROR line per failed entity:
#   <ts> ERROR flowctl::<mod>: scope=<scope> error|detail=<message>
# A failed publish can be hundreds of these, near-identical. We parse, mask the
# volatile bits (catalog paths, IPs) into a fingerprint, and group+count so the
# wall reads as its few root causes.
_FLOWCTL_ERROR_PATTERN = re.compile(
    r"\bERROR\b\s+\S+:\s+scope=(?P<scope>\S+)\s+(?:error|detail)=(?P<msg>.*)"
)
_IP_PATTERN = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_CATALOG_PATH_PATTERN = re.compile(r"[A-Za-z0-9_]+/benchmark/\S+")


def _scope_kind(scope: str) -> str:
    """The entity class a flowctl scope refers to (for the digest label)."""
    for kind in ("collection", "materialization", "capture"):
        if kind in scope:
            return kind
    return "spec"


def _scope_example(scope: str) -> str:
    """A human-readable entity name from a flowctl scope (trailing segment,
    ``~1`` un-escaped back to ``/``)."""
    tail = scope.rsplit("/", 1)[-1] if "/" in scope else scope
    return tail.replace("~1", "/")


def _digest_flowctl_errors(text: str) -> str:
    """Collapse flowctl's per-entity ERROR lines into distinct (message, count)
    groups, so a wall of N near-identical failures reads as the root causes.

    Heuristic on flowctl's line format: a line that doesn't match is passed
    through only if it still looks like an error, and if *nothing* matches the
    original text is returned unchanged — so the digest only adds clarity, it
    never hides an unrecognized error.
    """
    groups: dict[tuple[str, str], dict] = {}
    passthrough: list[str] = []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        m = _FLOWCTL_ERROR_PATTERN.search(ln)
        if not m:
            if "ERROR" in ln:  # an error we didn't recognize — keep it visible
                passthrough.append(ln.strip())
            continue
        kind = _scope_kind(m.group("scope"))
        fingerprint = _IP_PATTERN.sub(
            "<ip>", _CATALOG_PATH_PATTERN.sub("<name>", m.group("msg").strip())
        )
        # Drop a leading "collection <name> " / "materialization <name> " — it's
        # redundant with the [kind] label and the masked path.
        fingerprint = re.sub(
            r"^(?:collection|materialization|capture)\s+<name>\s+", "", fingerprint
        )
        fingerprint = re.sub(r"\s+", " ", fingerprint).strip()
        g = groups.setdefault((kind, fingerprint), {"count": 0, "example": None})
        g["count"] += 1
        if g["example"] is None:
            g["example"] = _scope_example(m.group("scope"))

    if not groups:
        return text  # nothing recognized — fall back to the raw (cleaned) output

    total = sum(g["count"] for g in groups.values())
    lines = [f"{total} error(s), {len(groups)} distinct:"]
    for (kind, fingerprint), g in sorted(groups.items(), key=lambda kv: -kv[1]["count"]):
        example = f" (e.g. {g['example']})" if g["example"] else ""
        lines.append(f"  {g['count']}× [{kind}] {fingerprint}{example}")
    if passthrough:
        lines.append("other:")
        lines.extend(f"  {ln}" for ln in dict.fromkeys(passthrough))  # dedup, keep order
    return "\n".join(lines)


def _wsl_distro() -> str:
    """WSL distro flowctl runs in (Windows only). Configurable via ESTUARY_WSL_DISTRO."""
    return get_settings().ESTUARY.WSL_DISTRO


class FlowctlError(BenchmarkError):
    """flowctl invocation failed."""


_IS_WINDOWS = platform.system() == "Windows"


def _resolve_path(path: Path) -> str:
    """Return a path string usable by flowctl.

    On Windows, converts to a WSL mount path (``/mnt/c/…``).
    On macOS/Linux, returns the resolved POSIX path.
    """
    resolved = path.resolve()
    if not _IS_WINDOWS:
        return resolved.as_posix()
    drive = resolved.drive.rstrip(":").lower()
    if not drive:
        return resolved.as_posix()
    return f"/mnt/{drive}{resolved.as_posix()[len(resolved.drive) :]}"


def _shell_cmd(cmd: list[str]) -> list[str]:
    """Wrap a command for the current platform.

    On macOS/Linux, returns the command as-is. On Windows, routes through
    WSL with the configured distro, resolving ``~/.local/bin/flowctl``
    when present (overrides older system-wide installs).
    """
    if not _IS_WINDOWS:
        return cmd
    wsl = shutil.which("wsl.exe") or "wsl.exe"
    distro = _wsl_distro()
    if cmd and cmd[0] == "flowctl":
        local = _resolve_wsl_flowctl_path()
        if local:
            return [wsl, "-d", distro, "--", local, *cmd[1:]]
    return [wsl, "-d", distro, "--", *cmd]


def _resolve_wsl_flowctl_path() -> str | None:
    """Probe WSL for ``~/.local/bin/flowctl``; return its absolute Linux path
    if executable, else None (caller falls back to PATH)."""
    wsl = shutil.which("wsl.exe") or "wsl.exe"
    distro = _wsl_distro()
    try:
        echo = subprocess.run(
            [wsl, "-d", distro, "--", "bash", "-lc", "echo $HOME/.local/bin/flowctl"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        path = echo.stdout.strip()
        if path and echo.returncode == 0:
            check = subprocess.run(
                [wsl, "-d", distro, "--", "test", "-x", path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if check.returncode == 0:
                return path
    except Exception:
        pass
    return None


def _substitute(yaml_text: str) -> tuple[str, list[str]]:
    """Expand ``${VAR}`` placeholders from :meth:`BenchmarkSettings.env_dict`.

    Hand-rolled rather than ``os.path.expandvars`` because the Windows
    implementation in ``ntpath.expandvars`` gets confused after substituting
    long values that contain regex/path-significant characters (specifically:
    ``MOTHERDUCK_TOKEN`` is a multi-hundred-character JWT with ``.``/``_``
    which causes later ``${VAR}`` matches in the same string to be skipped).

    Returns (substituted_text, missing_vars). Vars that aren't in the settings
    tree (or whose field resolved to ``None``) are left as literal ``${VAR}``
    and reported in the missing list so the caller can surface a clear error
    instead of letting flowctl reject them with an opaque parse failure.
    """
    env = get_settings().env_dict()
    missing: set[str] = set()

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        value = env.get(name)
        if value is None:
            missing.add(name)
            return match.group(0)
        if "\n" in value:
            # Multi-line values (e.g. a PEM private key under a YAML `|` block
            # scalar) must have every continuation line carry the placeholder
            # line's leading prefix, or the substituted text is invalid YAML.
            # We reuse the *literal* prefix (everything before the placeholder
            # on its line) rather than bare spaces: for a normal indented line
            # that's just whitespace, but for a commented-out line (e.g. a
            # disabled materialization block) it's the `#  ` comment marker —
            # so the whole expanded value stays commented instead of leaking
            # raw continuation lines into the document. The first line stays in
            # place (its prefix is already literal text in yaml_text).
            line_start = yaml_text.rfind("\n", 0, match.start()) + 1
            prefix = yaml_text[line_start : match.start()]
            value = ("\n" + prefix).join(value.splitlines())
        return value

    expanded = PLACEHOLDER_PATTERN.sub(repl, yaml_text)
    return expanded, sorted(missing)


def _write_applied(name: str, text: str) -> Path:
    APPLIED_DIR.mkdir(parents=True, exist_ok=True)
    out = APPLIED_DIR / name
    out.write_text(text, encoding="utf-8")
    logger.debug("substituted YAML written to %s", out)
    return out


# Permissive collection schema that accepts whatever source-s3 + the auto
# parquet parser emit (column values plus the connector's _meta envelope).
# source-s3 guarantees /_meta/file + /_meta/offset, which makes a stable
# composite key.
def _passthrough_collection_spec() -> dict[str, Any]:
    return {
        "schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "_meta": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string"},
                        "offset": {"type": "integer"},
                    },
                    "required": ["file", "offset"],
                }
            },
            "required": ["_meta"],
        },
        "key": ["/_meta/file", "/_meta/offset"],
    }


def _inject_capture_collections(doc: dict[str, Any]) -> tuple[int, int]:
    """Make sure every capture-binding target has a collection definition.

    flowctl requires each ``target`` referenced by a capture binding to be a
    real collection — either pre-existing in the catalog OR declared inline in
    the same YAML under ``collections:``. We inject typed schemas (when we know
    them — see ``benchmark.datagen.collection_schemas``) so the downstream
    materializer creates native columns instead of a single ``flow_document``
    JSON blob. Targets we don't have an explicit schema for fall back to the
    permissive passthrough.

    A target we can neither define nor pass through — currently only ClickBench
    when its local typed ``hits.parquet`` is absent — has its *binding dropped*
    rather than left dangling: a capture that references an undefined collection
    is rejected wholesale by flowctl, which would otherwise block every
    TPC-DS/DSB load in the same spec too. Run
    ``benchmark derive-clickbench-typed`` (and include ``clickbench`` in the
    scope) to load it.

    Returns ``(collections_injected, bindings_dropped)``.
    """
    from ..datagen.collection_schemas import build_collection_schemas

    captures = doc.get("captures") or {}
    collections = doc.setdefault("collections", {})

    # ESTUARY_ACCOUNT is already substituted into the targets, so it's stable;
    # the schema map is keyed by full collection name. The namespace segment
    # after the account can vary by pipeline, so build typed schemas for every
    # namespace the capture's targets actually reference, else a target outside
    # the default namespace would miss its typed schema and fall back to a
    # flow_document JSON blob.
    account = get_settings().ESTUARY.ACCOUNT
    typed_schemas: dict[str, Any] = {}
    if account:
        prefix = f"{account}/"
        namespaces: set[str] = set()
        for capture in captures.values():
            for binding in capture.get("bindings") or []:
                target = binding.get("target")
                if target and target.startswith(prefix):
                    rest = target[len(prefix) :]
                    if "/" in rest:
                        namespaces.add(rest.split("/", 1)[0])
        for ns in namespaces:
            typed_schemas.update(build_collection_schemas(account, namespace=ns))

    injected = 0
    dropped = 0
    for capture in captures.values():
        kept: list[dict[str, Any]] = []
        for binding in capture.get("bindings") or []:
            target = binding.get("target")
            if not target or target in collections:
                kept.append(binding)
                continue
            if target in typed_schemas:
                collections[target] = copy.deepcopy(typed_schemas[target])
                injected += 1
                kept.append(binding)
            elif "/clickbench/" in target:
                # No local typed hits.parquet -> no schema to inject. Drop the
                # binding instead of referencing an undefined collection (which
                # makes flowctl reject the whole capture).
                logger.warning(
                    "dropping ClickBench capture binding %s: no local typed "
                    "hits.parquet to derive its schema (run "
                    "`benchmark derive-clickbench-typed`)",
                    target,
                )
                dropped += 1
            else:
                logger.warning(
                    "no typed schema for collection %s; using passthrough "
                    "(materializes as flow_document JSON)",
                    target,
                )
                collections[target] = copy.deepcopy(_passthrough_collection_spec())
                injected += 1
                kept.append(binding)
        capture["bindings"] = kept
    return injected, dropped


# Actionable guidance whenever the wrong kind of token is configured. Estuary
# *refresh* tokens (dashboard → Admin → CLI-API → "Generate Token") are opaque
# {id,secret} blobs that flowctl exchanges for a fresh access token on every call
# and do not expire. A bare access-token JWT (e.g. copied from the browser/app
# session) lasts ~1 hour, so auth then breaks hourly.
_REFRESH_TOKEN_HINT = (
    "Estuary REFRESH tokens are opaque {id,secret} blobs that never expire; this is "
    "a short-lived (~1h) access-token JWT. Generate a refresh token at "
    "dashboard.estuary.dev → Admin → CLI-API → 'Generate Token' and set it as "
    "ESTUARY_REFRESH_TOKEN in env/estuary.env."
)


def _jwt_expiry(token: str) -> float | None:
    """If ``token`` is a JWT (an access token), return its ``exp`` epoch (or 0 when
    absent); ``None`` when it isn't a JWT — i.e. it's a proper opaque refresh token.

    Estuary refresh tokens are not JWTs, so a decodable ``typ=JWT`` here flags the
    common mistake of pasting an access token into ``ESTUARY_REFRESH_TOKEN``.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        header = json.loads(base64.urlsafe_b64decode(pad(parts[0])))
        if header.get("typ") != "JWT":
            return None
        payload = json.loads(base64.urlsafe_b64decode(pad(parts[1])))
    except (ValueError, json.JSONDecodeError):
        return None
    return float(payload.get("exp", 0))


def _resolve_token(token: str | None) -> str:
    """The Estuary token to authenticate flowctl with (arg overrides settings)."""
    if not token:
        secret = get_settings().ESTUARY.REFRESH_TOKEN
        token = secret.get_secret_value() if secret else None
    if not token:
        raise FlowctlError(
            "ESTUARY_REFRESH_TOKEN missing: set it in env/estuary.env or pass --token"
        )
    return token


def _flowctl_env(token: str | None = None) -> dict[str, str]:
    """Process env for a flowctl call, with ``FLOW_AUTH_TOKEN`` set.

    flowctl authenticates non-interactively from ``FLOW_AUTH_TOKEN`` (an Estuary
    **refresh token** — long-lived, exchanged for a fresh access token on every
    invocation). This is the supported automation path; ``flowctl auth token
    --token`` accepts only a short-lived access-token JWT, so auth through it
    breaks hourly. See docs.estuary.dev/guides/flowctl/ci-cd.
    """
    return {**os.environ, "FLOW_AUTH_TOKEN": _resolve_token(token)}


def _run_flowctl(
    cmd: list[str], *, token: str | None = None, timeout: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Run a flowctl command authenticated via ``FLOW_AUTH_TOKEN`` (+ WSL wrap)."""
    return subprocess.run(
        _shell_cmd(cmd),
        capture_output=True,
        text=True,
        env=_flowctl_env(token),
        timeout=timeout,
    )


def _guard_token_type(token: str) -> None:
    """Warn/fail when ``token`` is an access-token JWT instead of a refresh token.

    A refresh token is opaque and long-lived; an access-token JWT lasts ~1h, so
    even via ``FLOW_AUTH_TOKEN`` it stops working within the hour. Expired → fail
    fast with the fix; still-valid → warn."""
    exp = _jwt_expiry(token)
    if exp is None:
        return  # opaque refresh token — the correct kind
    if exp and exp < time.time():
        raise FlowctlError(
            f"ESTUARY_REFRESH_TOKEN is an EXPIRED access-token JWT, not a refresh token. "
            f"{_REFRESH_TOKEN_HINT}"
        )
    logger.warning(
        "ESTUARY_REFRESH_TOKEN looks like an access-token JWT, not a refresh token — "
        "it will stop working within the hour. %s",
        _REFRESH_TOKEN_HINT,
    )


def auth(token: str | None = None) -> None:
    """Verify flowctl can authenticate with the Estuary refresh token.

    Auth is carried per-invocation via ``FLOW_AUTH_TOKEN`` (see
    :func:`_flowctl_env`), so there's no persistent login step; this just
    fail-fast validates the token now (cheap ``flowctl auth roles list``) so a bad
    token surfaces here with clear guidance instead of failing each later command.
    """
    token = _resolve_token(token)
    _guard_token_type(token)
    logger.info("verifying flowctl auth (token len=%d)", len(token))
    result = _run_flowctl(["flowctl", "auth", "roles", "list"], token=token, timeout=60)
    if result.returncode != 0:
        detail = _clean(result.stderr) or _clean(result.stdout)
        if "JWT" in detail or "PGRST301" in detail or "authenticate" in detail:
            detail = f"{detail}\n{_REFRESH_TOKEN_HINT}"
        raise FlowctlError(f"flowctl auth failed (exit {result.returncode}): {detail}")
    logger.info("flowctl auth ok")


def render(yaml_path: Path) -> str:
    """Substitute ${VAR}s in a Flow spec and return the final YAML text.

    The full multi-scale spec is always rendered — captures + materializations
    are declarative and stateful, so we define them once with every suite ×
    scale binding and let source-s3 / the materializer pick up each scale's data
    as it lands in S3 (no per-run republish). Data presence is validated in the
    target at runtime (``validate-loads``), not encoded into the spec.

    The only content the renderer drops is a capture binding whose collection it
    can't define (ClickBench with no local typed parquet) — see
    :func:`_inject_capture_collections`.
    """
    yaml_path = yaml_path.resolve()
    if not yaml_path.exists():
        raise FlowctlError(f"YAML not found: {yaml_path}")

    original = yaml_path.read_text(encoding="utf-8")
    substituted, missing = _substitute(original)
    if missing:
        raise FlowctlError(
            f"env vars missing for {yaml_path.name}: {missing}. Set them in env/<section>.env and retry."
        )

    doc = yaml.safe_load(substituted) or {}
    modified = False

    if doc.get("captures"):
        injected, dropped = _inject_capture_collections(doc)
        if injected:
            logger.info("auto-injected %d collection defs for %s", injected, yaml_path.name)
        if dropped:
            logger.warning("dropped %d undefined-target binding(s) in %s", dropped, yaml_path.name)
        if injected or dropped:
            modified = True

    if modified:
        return yaml.safe_dump(doc, sort_keys=False, width=2**31)
    return substituted


def publish(yaml_path: Path, auto_approve: bool = True) -> None:
    """Substitute ${VAR}s in ``yaml_path``, then publish via flowctl.

    Always publishes the full spec (see :func:`render`): captures and
    materializations are define-once and stateful, so the full spec is published
    once and each scale's data flows as it lands in S3. A subset goes through
    :func:`publish_scoped`, which creates or updates only the named specs and never
    prunes the rest; only a bumped ``backfill`` counter makes Estuary re-read a
    collection's history. Raises FlowctlError if any required env vars are missing
    OR if flowctl returns non-zero.
    """
    final_yaml = render(yaml_path)

    # Pre-flight: a capture spec authenticates to S3 with the Estuary reader key.
    # Verify it here so a missing/truncated/rejected key fails fast with a clear
    # cause, instead of surfacing deep in flowctl's draft validation.
    if (yaml.safe_load(final_yaml) or {}).get("captures"):
        from .s3 import verify_estuary_s3_credentials

        s3_err = verify_estuary_s3_credentials()
        if s3_err:
            raise FlowctlError(f"pre-flight: {yaml_path.name} capture would fail — {s3_err}")

    applied = _write_applied(yaml_path.name, final_yaml)
    source_path = _resolve_path(applied)

    cmd = ["flowctl", "catalog", "publish", "--source", source_path]
    if auto_approve:
        cmd.append("--auto-approve")
    logger.info("flowctl catalog publish --source %s", source_path)
    result = _run_flowctl(cmd)
    if result.returncode != 0:
        digest = _digest_flowctl_errors(_clean(result.stderr)) or _clean(result.stdout)
        raise FlowctlError(
            f"flowctl publish failed for {yaml_path.name} (exit {result.returncode}):\n{digest}"
        )
    # flowctl writes a summary to stdout; surface it at INFO so operators see
    # the published spec list without needing --verbose.
    if _clean(result.stdout):
        logger.info("flowctl stdout:\n%s", _clean(result.stdout))
    if _clean(result.stderr):
        logger.debug("flowctl stderr:\n%s", _clean(result.stderr))


def publish_doc(
    doc: dict[str, Any],
    label: str,
    *,
    auto_approve: bool = True,
    init_data_plane: str | None = None,
) -> None:
    """Publish an already-rendered spec dict via flowctl.

    ``doc`` must be fully substituted (no ``${VAR}`` placeholders) — e.g. the
    output of :func:`build_scoped_spec`, possibly further mutated (a
    ``shards.disable`` toggle, a backfill bump). The dict is serialized to
    ``estuary/.applied/<label>`` and published as-is. ``init_data_plane`` names
    the data plane for specs this publication NEWLY CREATES (flowctl's
    ``--init-data-plane``; it must be listed on the covering prefix's storage
    mapping) — existing specs keep their plane regardless. Raises FlowctlError
    on a non-zero flowctl exit.
    """
    applied = _write_applied(label, yaml.safe_dump(doc, sort_keys=False, width=2**31))
    cmd = ["flowctl", "catalog", "publish", "--source", _resolve_path(applied)]
    if init_data_plane:
        cmd.extend(["--init-data-plane", init_data_plane])
    if auto_approve:
        cmd.append("--auto-approve")
    result = _run_flowctl(cmd)
    if result.returncode != 0:
        digest = _digest_flowctl_errors(_clean(result.stderr)) or _clean(result.stdout)
        raise FlowctlError(f"publish of {label} failed (exit {result.returncode}):\n{digest}")
    if _clean(result.stdout):
        logger.info("flowctl stdout:\n%s", _clean(result.stdout))
    if _clean(result.stderr):
        logger.debug("flowctl stderr:\n%s", _clean(result.stderr))


def task_shards(name: str) -> list[dict[str, Any]]:
    """Live shard rows for a task via ``flowctl raw list-shards`` (JSON output).

    Returns one dict per shard (id, status, ...). Shard *splitting* is performed
    by Estuary on the service side (this flowctl build has no split command), so
    this probe is how the harness observes a task's current parallelism. Raises
    FlowctlError on a non-zero exit — callers probing a task that may not exist
    yet should catch it.
    """
    result = _run_flowctl(
        ["flowctl", "raw", "list-shards", "--task", name, "-o", "json"], timeout=60
    )
    if result.returncode != 0:
        detail = _clean(result.stderr) or _clean(result.stdout)
        raise FlowctlError(f"list-shards failed for {name}: {detail}")
    shards: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            shards.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("unparseable list-shards line skipped: %s", line[:200])
    return shards


def raw_stats(name: str, since: str = "6h") -> list[dict[str, Any]]:
    """Per-transaction ops stats documents for a task via ``flowctl raw stats``.

    Returns one dict per stats document — per-transaction ``ts``,
    ``openSecondsTotal``, ``txnCount``, ``shard`` identity, and per-collection
    ``capture``/``materialize`` in/out docs+bytes totals — the ground truth for
    throughput breakdowns (docs.estuary.dev/reference/working-logs-stats).
    flowctl v0.6.10 nests the payload under a ``raw`` key; this unwraps it.
    ``since`` is a humantime duration (the read starts at the previous fragment
    boundary, so expect documents from before the requested window too).
    """
    result = _run_flowctl(
        ["flowctl", "raw", "stats", "--task", name, "--since", since, "-o", "json"],
        timeout=300,
    )
    if result.returncode != 0:
        detail = _clean(result.stderr) or _clean(result.stdout)
        raise FlowctlError(f"raw stats failed for {name}: {detail}")
    docs: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("unparseable raw-stats line skipped: %s", line[:200])
            continue
        payload = doc.get("raw") or doc.get("stats") or doc
        if isinstance(payload, dict):
            docs.append(payload)
    return docs


def task_logs(name: str, since: str = "6h") -> list[dict[str, Any]]:
    """Structured log lines for a task via ``flowctl logs`` (JSON output).

    Each dict carries ``ts``, ``message``, and ``fields``. The runtime/connector
    phase timings live here — e.g. ``finished reading store requests`` /
    ``finished materialization commit`` with a Go-duration ``took`` field —
    which is what makes a store/commit wall-time breakdown possible.
    ``since`` is a humantime duration (reads start at the previous fragment
    boundary, so expect lines from before the requested window too).
    """
    result = _run_flowctl(
        ["flowctl", "logs", "--task", name, "--since", since, "-o", "json"],
        timeout=300,
    )
    if result.returncode != 0:
        detail = _clean(result.stderr) or _clean(result.stdout)
        raise FlowctlError(f"logs read failed for {name}: {detail}")
    lines: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:
            logger.debug("unparseable log line skipped: %s", line[:200])
    return lines


# ─── Scoped materialization publish (targeted backfills) ────────────────────
# The full spec is define-once (see :func:`publish`), but recovering a single
# scale/suite (e.g. re-backfilling one engine's tpcds+dsb at SF1) must NOT
# republish the whole spec (that would re-touch every scale and
# risk re-backfilling ClickBench's 100M rows). These helpers publish a filtered
# subset of materializations, optionally bumping the per-binding ``backfill``
# counter, which is how Estuary forces a re-read of a collection's history
# (docs.estuary.dev/reference/backfilling-data). flowctl publish of a subset only
# creates/updates the named specs — it never prunes the ones left out.


def _mat_matches(
    name: str,
    *,
    names: set[str] | None,
    scales: list[int] | None,
    suites: list[str] | None,
) -> bool:
    """Whether a materialization catalog name passes the scope filters.

    ``names`` matches either the full catalog name or its trailing segment
    (e.g. ``bigquery-tpcds-sf1``). ``scales`` matches the ``-sf<N>`` suffix
    exactly (``-sf1`` never matches ``-sf10``). ``suites`` matches the suite
    token (``-tpcds-``; ClickBench's un-scaled ``-clickbench`` via suffix).
    All provided filters must pass (AND)."""
    leaf = name.split("/")[-1]
    if names and name not in names and leaf not in names:
        return False
    if scales and not any(leaf.endswith(f"-sf{s}") for s in scales):
        return False
    return not suites or any((f"-{s}-" in leaf or leaf.endswith(f"-{s}")) for s in suites)


def build_scoped_spec(
    yaml_path: Path,
    *,
    names: set[str] | None = None,
    scales: list[int] | None = None,
    suites: list[str] | None = None,
    backfill: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Render ``yaml_path`` and return a spec dict containing only the
    materializations that match the scope filters, plus the selected names.

    Renders FIRST (``${VAR}`` substituted) so re-serializing is safe: after
    substitution ``credentials_json`` is a real JSON *string* that ``safe_dump``
    re-quotes correctly — filtering the raw (un-rendered) spec instead would emit
    the ``${…}`` placeholder unquoted and it'd parse back as a mapping. When
    ``backfill`` is given, every selected binding's ``backfill`` counter is set to
    it (forces a re-backfill on next publish)."""
    doc = yaml.safe_load(render(yaml_path)) or {}
    mats = doc.get("materializations") or {}
    selected = {
        n: spec
        for n, spec in mats.items()
        if _mat_matches(n, names=names, scales=scales, suites=suites)
    }
    if not selected:
        raise FlowctlError(
            f"no materializations in {yaml_path.name} matched "
            f"(names={sorted(names) if names else None}, scales={scales}, suites={suites})"
        )
    if backfill is not None:
        for spec in selected.values():
            for binding in spec.get("bindings") or []:
                binding["backfill"] = backfill
    doc = {k: v for k, v in doc.items() if k != "materializations"}
    doc["materializations"] = selected
    return doc, sorted(selected)


def publish_scoped(
    yaml_path: Path,
    *,
    names: set[str] | None = None,
    scales: list[int] | None = None,
    suites: list[str] | None = None,
    backfill: int | None = None,
    auto_approve: bool = True,
) -> list[str]:
    """Publish only the scope-matching materializations of ``yaml_path``.

    The doc is already rendered (see :func:`build_scoped_spec`), so it's published
    as-is without a second substitution. Returns the published materialization
    names. Raises FlowctlError on no-match or a non-zero flowctl exit."""
    doc, selected = build_scoped_spec(
        yaml_path, names=names, scales=scales, suites=suites, backfill=backfill
    )
    logger.info(
        "scoped publish: %d materialization(s)%s",
        len(selected),
        f" (backfill={backfill})" if backfill is not None else "",
    )
    publish_doc(doc, f"{yaml_path.stem}.scoped.yaml", auto_approve=auto_approve)
    return selected


def live_task_names(prefix: str) -> set[str]:
    """Names of the captures and materializations Estuary holds under ``prefix``.

    Reads ``flowctl catalog list --prefix <prefix> --output json`` (one JSON object
    per line). Collections are left out. Raises FlowctlError on a non-zero exit.
    """
    result = _run_flowctl(
        ["flowctl", "catalog", "list", "--prefix", prefix, "--output", "json"], timeout=120
    )
    if result.returncode != 0:
        detail = _clean(result.stderr) or _clean(result.stdout)
        raise FlowctlError(f"catalog list failed for {prefix}: {detail}")
    names: set[str] = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("unparseable catalog list line skipped: %s", line[:200])
            continue
        if (row.get("liveSpec") or {}).get("catalogType") in ("capture", "materialization"):
            names.add(str(row.get("catalogName")))
    return names


def set_materializations_disabled(
    yaml_path: Path,
    *,
    disabled: bool,
    names: set[str] | None = None,
    scales: list[int] | None = None,
    suites: list[str] | None = None,
    auto_approve: bool = True,
) -> list[str]:
    """Pause (``disabled=True``) or resume the scope-matching materializations.

    A live materialization keeps running commit rounds against its warehouse,
    which contends with a measured run on the same warehouse and shares the
    billing hour reconcile splits, so an engine's materializations are paused
    after its load and before any ``sql`` / ``concurrency`` / ``sustained`` run.
    Pausing publishes the scoped spec with ``shards.disable: true``; resuming
    publishes it as the YAML has it. Neither bumps a backfill counter, and a
    paused task keeps the data it loaded.

    Only materializations Estuary already holds are published: a scoped publish of
    a name it does not hold would create that task. Returns the names published;
    raises FlowctlError when none of the selected materializations exists.
    """
    doc, selected = build_scoped_spec(yaml_path, names=names, scales=scales, suites=suites)
    prefix = selected[0].rsplit("/", 1)[0] + "/"
    live = live_task_names(prefix)
    keep = [n for n in selected if n in live]
    for n in selected:
        if n not in live:
            logger.info("%s does not exist in Estuary; left alone", n)
    if not keep:
        raise FlowctlError(f"none of the selected materializations exists: {', '.join(selected)}")
    doc["materializations"] = {n: doc["materializations"][n] for n in keep}
    for spec in doc["materializations"].values():
        shards = spec.setdefault("shards", {})
        if disabled:
            shards["disable"] = True
        else:
            shards.pop("disable", None)
    state = "disabled" if disabled else "enabled"
    logger.info("%s %d materialization(s)", state, len(keep))
    publish_doc(doc, f"{yaml_path.stem}.{state}.yaml", auto_approve=auto_approve)
    return keep


# ─── Scoped data-flow reset (collection-level) ───────────────────────────────
# A materialization backfill (above) only re-reads the EXISTING collection
# journal — data as the capture originally wrote it. When the capture itself
# must re-read S3 and rewrite the collection (e.g. after a capture-runtime
# change like `enable-runtime-v2`, whose storage schema lives in the journal),
# the unit of work is Estuary's *data-flow reset*: publish the collection spec
# with `reset: true` and Estuary regenerates the journal AND auto-bumps the
# backfill counter on the capture binding and on EVERY subscribing
# materialization binding (docs.estuary.dev/reference/backfilling-data).
#
# Blast radius: collections are shared upstream of all 7 engines, so resetting
# `benchmark/tpcds/sf1/*` truncates and reloads tpcds-sf1 destination tables on
# every engine — not just the one being validated. Scope filters are therefore
# mandatory (an unscoped call would reset all 241 collections, multi-TB).

_COLLECTION_NAME_RE = re.compile(
    r"^[^/]+/benchmark/(?P<suite>[^/]+)/(?:sf(?P<scale>\d+)/)?(?P<table>[^/]+)$"
)


def _collection_matches(
    name: str,
    *,
    suites: list[str] | None,
    scales: list[int] | None,
    tables: list[str] | None,
) -> bool:
    """Whether a collection catalog name passes the scope filters (AND).

    Names look like ``<acct>/benchmark/<suite>/sf<N>/<table>`` (tpcds/dsb) or
    ``<acct>/benchmark/clickbench/hits`` (no scale segment). A ``scales`` filter
    only ever matches scaled suites — un-scaled ClickBench must be selected via
    ``suites=['clickbench']`` explicitly, never swept up by a scale filter."""
    m = _COLLECTION_NAME_RE.match(name)
    if not m:
        return False
    if suites and m["suite"] not in suites:
        return False
    if scales and (m["scale"] is None or int(m["scale"]) not in scales):
        return False
    return not tables or m["table"] in tables


def build_reset_spec(
    yaml_path: Path,
    *,
    suites: list[str] | None = None,
    scales: list[int] | None = None,
    tables: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Render capture spec ``yaml_path`` and return a collections-only spec with
    ``reset: true`` on every scope-matching collection, plus the matched names.

    At least one scope filter is required — an unscoped reset would regenerate
    every collection in the capture. The collection models are taken verbatim
    from the render (same injected schemas the capture publish used), so the
    publish changes nothing but the reset flag."""
    if not (suites or scales or tables):
        raise FlowctlError("collection reset requires a scope filter (suites/scales/tables)")
    doc = yaml.safe_load(render(yaml_path)) or {}
    collections = doc.get("collections") or {}
    selected = {
        name: {**spec, "reset": True}
        for name, spec in collections.items()
        if _collection_matches(name, suites=suites, scales=scales, tables=tables)
    }
    if not selected:
        raise FlowctlError(
            f"no collections in {yaml_path.name} matched "
            f"(suites={suites}, scales={scales}, tables={tables})"
        )
    return {"collections": selected}, sorted(selected)


def reset_collections(
    yaml_path: Path,
    *,
    suites: list[str] | None = None,
    scales: list[int] | None = None,
    tables: list[str] | None = None,
    auto_approve: bool = True,
) -> list[str]:
    """Publish a scoped data-flow reset for the scope-matching collections of
    capture spec ``yaml_path``. Returns the reset collection names.

    Estuary regenerates each collection's journal and auto-bumps the capture
    and all subscribing materializations' backfill counters — destination
    tables on EVERY engine bound to these collections are truncated and
    reloaded. Raises FlowctlError on no-match, missing scope, or flowctl error."""
    doc, selected = build_reset_spec(yaml_path, suites=suites, scales=scales, tables=tables)
    label = f"{yaml_path.stem}.reset.yaml"
    applied = _write_applied(label, yaml.safe_dump(doc, sort_keys=False, width=2**31))
    cmd = ["flowctl", "catalog", "publish", "--source", _resolve_path(applied)]
    if auto_approve:
        cmd.append("--auto-approve")
    logger.info("data-flow reset: %d collection(s)", len(selected))
    result = _run_flowctl(cmd)
    if result.returncode != 0:
        digest = _digest_flowctl_errors(_clean(result.stderr)) or _clean(result.stdout)
        raise FlowctlError(f"collection reset failed (exit {result.returncode}):\n{digest}")
    if _clean(result.stdout):
        logger.info("flowctl stdout:\n%s", _clean(result.stdout))
    return selected


# Ordered list for the MotherDuck end-to-end: capture first (so its 49
# collections exist), then the multi-block materialization that subscribes to
# them. One source-s3 capture with 49 bindings covers all three suites.
MOTHERDUCK_PIPELINE: tuple[Path, ...] = (
    ESTUARY_DIR / "captures" / "source-s3.flow.yaml",
    ESTUARY_DIR / "materializations" / "motherduck.flow.yaml",
)


def publish_motherduck_pipeline() -> None:
    """Convenience: auth then publish the 2 specs in MOTHERDUCK_PIPELINE."""
    auth()
    for spec in MOTHERDUCK_PIPELINE:
        logger.info("publishing %s", spec.relative_to(REPO_ROOT))
        publish(spec)


def delete(name: str) -> None:
    """Delete a catalog spec from Estuary by name (capture / materialization /
    collection). Wraps `flowctl catalog delete --name <name>`.

    Passes ``--dangerous-auto-approve`` because the harness runs flowctl
    non-interactively (no TTY to answer the confirmation prompt); the
    ``flow-delete`` command is itself the explicit, operator-invoked confirmation.
    """
    logger.info("flowctl catalog delete --name %s", name)
    result = _run_flowctl(
        ["flowctl", "catalog", "delete", "--dangerous-auto-approve", "--name", name]
    )
    if result.returncode != 0:
        raise FlowctlError(
            f"flowctl delete failed for {name} (exit {result.returncode}): "
            f"{_clean(result.stderr) or _clean(result.stdout)}"
        )
    if _clean(result.stdout):
        logger.info("flowctl stdout:\n%s", _clean(result.stdout))


# ─── Collection state (read-only) ───────────────────────────────────────────
# Used by `prepare-scale` to validate that the Estuary collections feeding the
# materializations actually exist and hold data, independently of whether any
# engine has materialized them yet. Collections are shared upstream of all 7
# engines, so this is a per-suite/scale check, not per-platform.


@dataclass
class CollectionState:
    """Fragment-level state of one Flow collection (read-only probe).

    ``journal_bytes`` is the max journal end offset across fragments (0 = the
    collection exists but holds no data — never backfilled or empty source).
    ``last_write`` is the max fragment modTime (epoch seconds; 0 if none).
    """

    name: str
    present: bool
    journal_bytes: int
    last_write: int


def collection_state(name: str, *, timeout: int = 60) -> CollectionState:
    """Probe one collection's fragments via ``flowctl collections list-fragments``.

    A non-zero exit (collection unknown to the catalog) → ``present=False``. A
    clean exit with no fragments → present but ``journal_bytes=0`` (empty).
    Authenticates per-call via ``FLOW_AUTH_TOKEN`` (:func:`_run_flowctl`).
    """
    try:
        proc = _run_flowctl(
            ["flowctl", "collections", "list-fragments", "--collection", name, "-o", "json"],
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("list-fragments timed out for %s", name)
        return CollectionState(name, present=False, journal_bytes=0, last_write=0)
    if proc.returncode != 0:
        return CollectionState(name, present=False, journal_bytes=0, last_write=0)
    max_end = 0
    max_mod = 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            spec = json.loads(line).get("spec", {})
        except json.JSONDecodeError:
            continue
        max_end = max(max_end, int(spec.get("end", 0)))
        max_mod = max(max_mod, int(spec.get("modTime", 0)))
    return CollectionState(name, present=True, journal_bytes=max_end, last_write=max_mod)


def collection_name(suite: str, scale: int, table: str) -> str:
    """Catalog name of a benchmark collection (``<account>/benchmark/...``).

    ClickBench is a single, un-scaled ``hits`` collection; TPC-DS/DSB are
    ``<suite>/sf<scale>/<table>``.
    """
    account = get_settings().ESTUARY.ACCOUNT
    if suite == "clickbench":
        return f"{account}/benchmark/clickbench/hits"
    return f"{account}/benchmark/{suite}/sf{scale}/{table}"


def collection_states(
    names: list[str], *, max_workers: int = 8, timeout: int = 60
) -> list[CollectionState]:
    """Probe many collections concurrently (flowctl calls are I/O-bound)."""
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(lambda n: collection_state(n, timeout=timeout), names))
