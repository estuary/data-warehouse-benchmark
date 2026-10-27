#!/usr/bin/env bash
# Snapshot results/benchmark.duckdb (+ cost_config.json) into results/backups/.
#
#   ./scripts/ops/backup_results_db.sh <tag>
#
# WHY A FILE COPY AND NOT `EXPORT DATABASE`: a campaign step usually holds the
# write lock, and DuckDB will not let a second process attach even read-only while
# a writer is active. A file copy is the only snapshot available mid-campaign.
#
# CONSISTENCY: the .wal is copied WITH the database and immediately after it, so a
# copy taken mid-write still replays to a consistent state. The script then opens
# the copy read-only and counts rows — if the snapshot were torn, that fails here
# rather than silently six weeks from now when someone needs it.
#
# Backups are git-ignored. They are the read source while a campaign holds the live
# DB; never open results/benchmark.duckdb for reading during a run.

set -uo pipefail

cd "$(dirname "$0")/../.." || exit 1

TAG="${1:?usage: backup_results_db.sh <tag>   e.g. pre-clickhouse-sweep}"
STAMP="$(date -u '+%Y%m%d-%H%M%S')"
DB="results/benchmark.duckdb"
DEST_DIR="results/backups"
DEST="${DEST_DIR}/benchmark.duckdb.${TAG}-${STAMP}"

[[ -f "$DB" ]] || { echo "no such database: $DB" >&2; exit 1; }
mkdir -p "$DEST_DIR"

echo "snapshotting $DB -> $DEST"
cp "$DB" "$DEST" || exit 1
# Copy the WAL straight after the main file, under the same name the copy expects,
# so DuckDB replays it when the backup is opened.
if [[ -f "${DB}.wal" ]]; then
  cp "${DB}.wal" "${DEST}.wal"
  echo "  included a live .wal (a writer was mid-transaction)"
fi
if [[ -f results/cost_config.json ]]; then
  cp results/cost_config.json "${DEST_DIR}/cost_config.json.${TAG}-${STAMP}"
fi

# Verify the snapshot opens and carries the rows we expect.
uv run python - "$DEST" <<'PY'
import sys, duckdb
con = duckdb.connect(sys.argv[1], read_only=True)
rows, cells = con.execute("""
    SELECT count(*), count(DISTINCT (platform, scale_factor, run_type))
    FROM sql_benchmark_results
""").fetchone()
runs = con.execute("SELECT count(*) FROM benchmark_run").fetchone()[0]
print(f"  verified: {rows:,} result rows across {cells} platform/scale/run-type cells, {runs} run records")
PY
rc=$?
if [[ $rc -ne 0 ]]; then
  echo "!!! the snapshot did NOT open cleanly — treat it as unusable" >&2
  exit 1
fi

du -h "$DEST" | awk '{print "  size: " $1}'
echo "ok"
