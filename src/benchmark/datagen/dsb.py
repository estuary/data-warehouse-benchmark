"""DSB skewed-data generation.

DSB shares the TPC-DS schema but introduces value skew via modified
distributions. Microsoft's reference impl (github.com/microsoft/dsb) ships the
TPC-DS C toolkit with skewed distribution files (``tpcds.idx`` + ``*.dst``); its
``dsdgen`` emits skewed ``.dat`` files (the skew is the default — no special
flag — and shows up as ``<table>_<col>.meta`` skew sidecars next to the data).

The C generator builds cleanly on Linux but needs porting on macOS, so we build
and run it in a Docker container (``docker/dsb/Dockerfile``). dsdgen writes to a
container-internal dir and we ``docker cp`` the ``.dat`` out — NOT a bind mount,
whose per-write virtiofs cost dominates runtime on Docker Desktop for macOS. We
then convert to Parquet so the rest of the pipeline (S3 stage, Estuary captures,
engine materializations) is uniform with TPC-DS.

Generator precedence:
  1. a native binary (``vendor/dsb/code/tools/dsdgen`` or ``dsdgen-dsb``) — any OS;
  2. otherwise, on NON-macOS, build the native generator (clone + ``make``);
  3. otherwise, on macOS, the Docker generator (auto-builds the ``benchmark-dsb``
     image on first use — the C toolkit doesn't compile cleanly on macOS).
We do NOT silently fall back to uniform TPC-DS data: if no real skew generator
can be obtained we raise ``DataGenerationError`` (the DSB block is only
meaningful with skew). A ``.dsb_fallback`` marker (left by uniform fallback
data) forces regeneration.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from pathlib import Path

import duckdb

from ..exceptions import DataGenerationError
from .tpcds import TPCDS_TABLES, existing_table_counts, tpcds_table_schema

logger = logging.getLogger("benchmark.datagen.dsb")

DSB_IMAGE = "benchmark-dsb"
# Dockerfile lives at <repo>/docker/dsb; resolve relative to CWD (the harness
# runs from the repo root), mirroring how _dsb_binary() locates vendor/.
DSB_DOCKERFILE_DIR = Path("docker/dsb")
# Native build (non-macOS): clone microsoft/dsb into vendor/ and compile dsdgen.
DSB_REPO_URL = "https://github.com/microsoft/dsb.git"
DSB_VENDOR_DIR = Path("vendor/dsb")


def _dsb_binary() -> Path | None:
    """Locate a locally-built ``microsoft/dsb`` dsdgen binary, if any."""
    candidate = shutil.which("dsdgen-dsb")
    if candidate:
        return Path(candidate)
    for repo_path in (Path("vendor/dsb/code/tools/dsdgen"), Path("vendor/dsb/tools/dsdgen")):
        if repo_path.exists():
            return repo_path
    return None


def _is_darwin() -> bool:
    """True on macOS, where the microsoft/dsb C toolkit doesn't build cleanly so
    we use the Docker generator. On Linux we build/run a native dsdgen instead
    and never shell to Docker."""
    return platform.system() == "Darwin"


def _docker_available() -> bool:
    """True if a docker CLI + reachable daemon are present."""
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        subprocess.run([docker, "version"], check=True, capture_output=True, timeout=30)
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False


def _ensure_dsb_image() -> bool:
    """Build the ``benchmark-dsb`` image if absent. Returns True if it's ready."""
    docker = shutil.which("docker")
    if docker is None:
        return False
    if subprocess.run([docker, "image", "inspect", DSB_IMAGE], capture_output=True).returncode == 0:
        return True
    if not DSB_DOCKERFILE_DIR.exists():
        logger.warning("DSB Dockerfile dir not found at %s", DSB_DOCKERFILE_DIR.resolve())
        return False
    logger.info("building %s image (one-time; clones + compiles microsoft/dsb)…", DSB_IMAGE)
    build = subprocess.run(
        [docker, "build", "-t", DSB_IMAGE, str(DSB_DOCKERFILE_DIR)],
        capture_output=True,
        text=True,
    )
    if build.returncode != 0:
        logger.error("DSB image build failed:\n%s", build.stderr[-2000:])
        return False
    return True


def _generate_raw_local(binary: Path, scale_factor: int, output_dir: Path) -> None:
    """Run a locally-built dsdgen serially. Skew is the default (DSB's tpcds.idx).

    Runs with the binary's own directory as CWD — dsdgen resolves its
    ``tpcds.idx`` distributions file relative to CWD (exit 1 otherwise), which
    is also why the Docker path keeps WORKDIR at the tools dir. ``-DIR`` is
    made absolute so the output still lands in ``output_dir``.

    Serial on purpose: both of dsdgen's parallelism avenues are broken in the
    DSB fork; see the NOTE on parallel dsdgen below.
    """
    binary = binary.resolve()
    subprocess.run(
        [str(binary), "-SCALE", str(scale_factor), "-DIR", str(output_dir.resolve()), "-FORCE"],
        check=True,
        cwd=binary.parent,
    )


# Above this scale, dsdgen's raw .dat output no longer reliably fits in the
# Docker Desktop VM's disk alongside images/cache (raw output is ~1.1 GB per
# SF; the default VM disk is ~100 GB, mostly occupied). A filled VM disk is
# silent data loss: dsdgen never checks fprintf's return and writes 0-byte
# tables for everything after the disk fills. Large scales therefore
# bind-mount the host output dir instead: virtiofs write overhead is
# irrelevant there because dsdgen is CPU-bound (~35 MB/s of output at 100% of
# one core), while small scales keep the fast internal-fs + docker-cp path.
_DSB_BIND_MOUNT_MIN_SF = 20

# NOTE: parallel dsdgen is a dead end in the microsoft/dsb fork (short of
# patching the C).
#   * `-PARALLEL N -CHILD i` SIGSEGVs instantly for any CHILD >= 2, every N
#     and scale (child 1 alone works).
#   * `-TABLE <t>` SIGSEGVs for the sales parents (store_sales,
#     catalog_sales) even run solo. Dimension tables (e.g. inventory) run
#     fine, but without the sales tables that's useless.
# Only the full serial all-tables invocation is reliable; at ~1 core it puts
# DSB SF1000 raw generation in the several-hours range.


def _generate_raw_docker(scale_factor: int, output_dir: Path) -> None:
    """Generate DSB ``.dat`` via the Docker generator.

    Small scales (< ``_DSB_BIND_MOUNT_MIN_SF``): dsdgen writes to a
    container-internal dir (the Docker VM's native fs) rather than a
    bind-mounted host dir — on Docker Desktop for macOS, bind-mount writes
    cross virtiofs and dominate runtime at small SF (SF1 raw ``.dat`` is
    ~1.3 GB — ~7 min bind-mounted vs ~1 min internal). The output is then
    ``docker cp``'d out in one tar stream and converted on the host; the
    container isn't ``--rm``'d until after the copy.

    Large scales: the output cannot fit in the VM disk (see
    ``_DSB_BIND_MOUNT_MIN_SF``), so ``/out`` is a bind mount of the host
    ``output_dir`` and the copy step is skipped.
    """
    docker = shutil.which("docker")
    container = f"benchmark-dsb-gen-sf{scale_factor}"
    # Clear any leftover container from a previous interrupted run, then ensure
    # the host target exists for the copy / mount.
    subprocess.run([docker, "rm", "-f", container], capture_output=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    bind_mount = scale_factor >= _DSB_BIND_MOUNT_MIN_SF
    mount_args = ["-v", f"{output_dir.resolve()}:/out"] if bind_mount else []
    if bind_mount:
        logger.info(
            "dsb sf%d: bind-mounting %s (raw output too large for the Docker VM disk)",
            scale_factor,
            output_dir,
        )
    try:
        # --entrypoint sh so we can mkdir the internal /out first; WORKDIR stays
        # /opt/dsb/code/tools (set in the image) so ./dsdgen finds tpcds.idx.
        subprocess.run(
            [
                docker,
                "run",
                "--name",
                container,
                *mount_args,
                "--entrypoint",
                "sh",
                DSB_IMAGE,
                "-c",
                f"mkdir -p /out && ./dsdgen -SCALE {scale_factor} -DIR /out -FORCE",
            ],
            check=True,
        )
        if not bind_mount:
            subprocess.run(
                [docker, "cp", f"{container}:/out/.", str(output_dir.resolve())],
                check=True,
            )
    finally:
        subprocess.run([docker, "rm", "-f", container], capture_output=True)


def _convert_dat_to_parquet(output_dir: Path) -> dict[str, int]:
    """Convert each dsdgen ``<table>.dat`` to Parquet with canonical names + types.

    dsdgen writes headerless pipe-delimited rows with a trailing delimiter. We
    read every field as text (``all_varchar`` — no type inference, since
    auto-detect mis-types e.g. zip codes as BIGINT, which breaks string ops like
    SUBSTRING and silently drops leading zeros), then CAST each of the first N
    columns to its canonical DuckDB type and alias it to the real TPC-DS name,
    dropping the trailing empty column. DSB shares the TPC-DS schema. Without
    this every downstream consumer breaks — ground-truth hashing, the Estuary
    collection mapping (by name), and the engine tables. ``*.meta`` skew sidecars
    are cleaned up (distribution metadata, not data).
    """
    schema = tpcds_table_schema()
    con = duckdb.connect(":memory:")
    counts: dict[str, int] = {}
    try:
        for table in TPCDS_TABLES:
            # Serial runs write <table>.dat; -PARALLEL runs write
            # <table>_<child>_<N>.dat chunks. Anchored regex, NOT a glob:
            # table names prefix each other (store / store_sales /
            # store_returns), so `store_*` would swallow other tables' chunks.
            chunk_re = re.compile(rf"^{re.escape(table)}(_\d+_\d+)?\.dat$")
            dats = sorted(p for p in output_dir.glob(f"{table}*.dat") if chunk_re.match(p.name))
            if not dats:
                logger.warning("DSB table file missing: %s/%s.dat", output_dir, table)
                continue
            parquet = output_dir / f"{table}.parquet"
            file_list = ", ".join(f"'{p.as_posix()}'" for p in dats)
            read_expr = (
                f"read_csv([{file_list}], delim='|', header=false, "
                "all_varchar=true, null_padding=true)"
            )
            generic = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {read_expr}").fetchall()]
            cols = schema[table]
            if len(generic) < len(cols):
                logger.error(
                    "%s: parsed %d columns but TPC-DS schema expects %d — skipping",
                    table,
                    len(generic),
                    len(cols),
                )
                continue
            # zip stops at len(cols): first N text columns -> CAST to canonical
            # type + real name; the trailing field (after dsdgen's final
            # delimiter) is dropped.
            select_list = ", ".join(
                f'CAST("{g}" AS {duck_type}) AS "{name}"'
                for g, (name, duck_type) in zip(generic, cols, strict=False)
            )
            con.execute(
                f"COPY (SELECT {select_list} FROM {read_expr}) "
                f"TO '{parquet.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            n = con.execute(f"SELECT COUNT(*) FROM read_parquet('{parquet.as_posix()}')").fetchone()
            counts[table] = int(n[0]) if n else 0
            for dat in dats:
                dat.unlink()
    finally:
        con.close()
    for meta in output_dir.glob("*.meta"):
        meta.unlink()
    return counts


def _build_native_binary() -> Path:
    """Clone + compile microsoft/dsb natively (non-macOS); return the dsdgen path.

    Raises ``DataGenerationError`` if any step fails — the caller turns that into
    a hard stop rather than silently producing non-skewed data. Mirrors
    docker/dsb/Dockerfile: shallow clone, pin ``OS = LINUX`` in Makefile.suite,
    ``make dsdgen``. Needs git + a C toolchain (gcc, make, bison, flex). The
    result is cached under ``vendor/`` so later runs reuse it via ``_dsb_binary``.
    """
    tools_dir = DSB_VENDOR_DIR / "code" / "tools"
    binary = tools_dir / "dsdgen"
    try:
        if not (tools_dir / "Makefile.suite").exists():
            DSB_VENDOR_DIR.parent.mkdir(parents=True, exist_ok=True)
            logger.info("building native DSB generator: cloning %s", DSB_REPO_URL)
            subprocess.run(
                ["git", "clone", "--depth", "1", DSB_REPO_URL, str(DSB_VENDOR_DIR)],
                check=True,
                capture_output=True,
                text=True,
            )
        logger.info("compiling dsdgen (make -f Makefile.suite OS=LINUX)…")
        subprocess.run(
            ["sed", "-i", "s/^OS[[:space:]]*=.*/OS = LINUX/", "Makefile.suite"],
            cwd=tools_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["make", "-f", "Makefile.suite", "OS=LINUX", "dsdgen"],
            cwd=tools_dir,
            check=True,
            capture_output=True,
            text=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        detail = stderr.strip()[-1500:] if stderr else str(exc)
        raise DataGenerationError(
            "failed to build the native DSB generator (microsoft/dsb). Install "
            "git + a C toolchain (e.g. `apt install build-essential git bison "
            f"flex`) and retry. Underlying error:\n{detail}"
        ) from exc
    if not binary.exists():
        raise DataGenerationError(f"DSB build reported success but dsdgen is missing at {binary}")
    return binary


def generate_dsb_sf(
    scale_factor: int,
    output_dir: Path,
    *,
    allow_docker: bool = True,
    force: bool = False,
) -> dict[str, int]:
    """Generate skewed DSB data at the given scale.

    Idempotent: if a complete set of table parquets is already present (and the
    prior run wasn't a uniform fallback), regeneration is skipped unless
    ``force=True``.

    Generator selection (there is NO silent uniform fallback: we raise
    ``DataGenerationError`` instead, since the DSB block is only meaningful with
    skew):
      * a native dsdgen (``dsdgen-dsb`` / ``vendor/dsb/...``) if present — any OS;
      * else, on non-macOS, build the native generator (clone + ``make``);
      * else, on macOS, the Docker generator (builds the image on first use).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    marker = output_dir / ".dsb_fallback"

    # Skip if the scale's data already exists, unless forced or a
    # uniform-fallback marker is present (regenerate then).
    if not force and not marker.exists():
        existing = existing_table_counts(output_dir)
        if existing is not None:
            logger.info(
                "DSB SF%d already present (%d tables) — skipping regeneration "
                "(pass force=True / --force to overwrite)",
                scale_factor,
                len(existing),
            )
            return existing

    binary = _dsb_binary()
    if binary is None and not _is_darwin():
        # Non-macOS without a native binary: build it (raises on failure).
        binary = _build_native_binary()

    if binary is not None:
        logger.info("DSB: native dsdgen at SF%d", scale_factor)
        _generate_raw_local(binary, scale_factor, output_dir)
    elif allow_docker:
        # macOS path (non-macOS already built-or-raised above).
        if not _docker_available():
            raise DataGenerationError(
                "DSB on macOS needs Docker, but no Docker daemon is reachable — "
                "start Docker Desktop (or build a native dsdgen) and retry."
            )
        if not _ensure_dsb_image():
            raise DataGenerationError(
                "DSB on macOS needs the benchmark-dsb image, but it failed to "
                "build (see logs above). Fix Docker and retry."
            )
        logger.info("DSB: dsdgen via Docker image %s at SF%d", DSB_IMAGE, scale_factor)
        _generate_raw_docker(scale_factor, output_dir)
    else:
        raise DataGenerationError(
            "no DSB generator available: no native dsdgen, and Docker is "
            "disabled (allow_docker=False). Build a native dsdgen and retry."
        )

    # Real skew generated: clear any uniform-fallback marker.
    marker.unlink(missing_ok=True)
    return _convert_dat_to_parquet(output_dir)
