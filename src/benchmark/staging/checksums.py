"""SHA-256 checksums for staged data files.

Every Parquet under data/ gets hashed; the manifest goes next to the data files
for downstream integrity checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger("benchmark.staging.checksums")


def _sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksum_manifest(data_root: Path, output: Path) -> dict[str, dict[str, str]]:
    """Hash every *.parquet under `data_root` and write a JSON manifest.

    Schema: {"<relative/path/to/file.parquet>": "<sha256>", ...}
    Keys are POSIX-style relative paths so the manifest is portable across OS.
    """
    manifest: dict[str, str] = {}
    files = sorted(data_root.rglob("*.parquet"))
    logger.info("hashing %d parquet files under %s", len(files), data_root)
    for path in files:
        rel = path.relative_to(data_root).as_posix()
        digest = _sha256_file(path)
        manifest[rel] = digest
        logger.debug("%s %s", digest[:16], rel)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("wrote checksum manifest to %s (%d entries)", output, len(manifest))
    return {data_root.name: manifest}
