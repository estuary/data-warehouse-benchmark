/**
 * Stage the static payload for the client-side app.
 *
 *  1. Copy the benchmark export parquets from ../results/exports into public/data.
 *  2. Copy the single-threaded DuckDB-WASM assets into public/duckdb so the app
 *     self-hosts them (no jsDelivr CDN, works offline, no COOP/COEP needed).
 *  3. Check that the Parquet reader extension is beside them AND was built for
 *     the DuckDB version inside the module. It is committed (Git LFS), not copied
 *     from node_modules, because the npm package does not ship it; see
 *     PARQUET_EXTENSION below for how to refresh it.
 *  4. Check every export still has the columns and types the explorer was
 *     written against (SCHEMA below). `--write-schema` records the current ones.
 *
 * Regenerate the source parquets with `uv run benchmark analyze` at the repo root.
 */
import { copyFile, mkdir, open, readFile, rm, stat, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const app = resolve(here, '..');
const repo = resolve(app, '..');

const EXPORTS = resolve(repo, 'results/exports');
const DATA_OUT = resolve(app, 'public/data');
const WASM_OUT = resolve(app, 'public/duckdb');

/** Parquet files the app queries. Missing ones are a hard error. */
const TABLES = [
  'cost_curves.parquet',
  'clickbench_summary.parquet',
  'concurrency_grid.parquet',
  'pattern_load.parquet',
  'skew_load.parquet',
  'skew_impact.parquet',
  'row_count_agreement.parquet',
  'size_ladder.parquet',
  'rate_card.parquet',
  'query_catalog.parquet',
  'scale_skips.parquet'
];

/**
 * The single-threaded `eh` (exception-handling) DuckDB-WASM bundle. It needs
 * no SharedArrayBuffer, so the app runs on hosts that cannot send COOP/COEP
 * headers. The larger `mvp` fallback is deliberately not shipped: every
 * browser since Chrome 95 / Firefox 100 / Safari 15.2 handles `eh`, and
 * carrying both would double the deployed artifact for no live user.
 */
const WASM_ASSETS = [
  'duckdb-eh.wasm',
  'duckdb-browser-eh.worker.js'
];
const WASM_SRC = resolve(app, 'node_modules/@duckdb/duckdb-wasm/dist');

/**
 * The Parquet reader. Since DuckDB 1.5 it is a loadable extension, and a DuckDB
 * that has not been handed one autoloads it from https://extensions.duckdb.org
 * on the first `read_parquet` — a runtime CDN fetch this app must not make.
 * `src/db.js` loads this copy explicitly instead.
 *
 * It is version-locked to the wasm module. When `@duckdb/duckdb-wasm` is
 * upgraded, this script fails with the version the module now needs; refresh the
 * file from
 *   https://extensions.duckdb.org/<version>/wasm_eh/parquet.duckdb_extension.wasm
 * and commit it. The check compares the version DuckDB reports when the
 * installed module is instantiated here in Node against the version stamped in
 * the extension's metadata footer, so a mismatch fails the build rather than
 * the page.
 */
const PARQUET_EXTENSION = 'parquet.duckdb_extension.wasm';

/**
 * DuckDB extensions end in a fixed-size metadata footer of 32-byte fields; one
 * of them is the DuckDB version the extension was built against.
 */
const EXTENSION_FOOTER_BYTES = 512;
const EXTENSION_FIELD_BYTES = 32;

/** Version stamped in an extension file's footer, e.g. `v1.5.4`. */
async function extensionVersion(file) {
  const fh = await open(file, 'r');
  try {
    const { size } = await fh.stat();
    const buf = Buffer.alloc(EXTENSION_FOOTER_BYTES);
    await fh.read(buf, 0, EXTENSION_FOOTER_BYTES, size - EXTENSION_FOOTER_BYTES);
    for (let i = 0; i < EXTENSION_FOOTER_BYTES; i += EXTENSION_FIELD_BYTES) {
      const field = buf.subarray(i, i + EXTENSION_FIELD_BYTES).toString('utf8').replace(/\0+$/, '');
      if (/^v\d+\.\d+\.\d+/.test(field)) return field;
    }
    return null;
  } finally {
    await fh.close();
  }
}

/**
 * The columns and DuckDB types of every export, as the explorer was written
 * against them. The pages read columns by name from query rows, so a column
 * that `benchmark analyze` drops or renames comes back `undefined` and renders
 * as a blank or "NaN" rather than failing; a type change can break the
 * arithmetic (DuckDB hands a BIGINT to JavaScript as a BigInt, and mixing a
 * BigInt with a number throws). So a column missing or retyped here fails the
 * build. A new column is only reported: record it with `--write-schema` once
 * the explorer reads it. tests/integration/test_explorer_export_contract.py
 * holds the Python side to the same file, on every pull request.
 */
const SCHEMA = resolve(here, 'export-schema.json');

/** Each export's columns, `{ file: { column: type } }`, as DuckDB reads them. */
function exportColumns(con) {
  return Object.fromEntries(TABLES.map(f => [f, Object.fromEntries(
    con.query(`DESCRIBE SELECT * FROM read_parquet('${join(EXPORTS, f)}')`).toArray()
      .map(r => r.toJSON()).map(r => [r.column_name, r.column_type])
  )]));
}

/**
 * Compare the exports with SCHEMA: the broken columns (gone or retyped) and
 * the new ones.
 */
function schemaDrift(want, got) {
  const broken = [];
  const added = [];
  for (const [file, columns] of Object.entries(want)) {
    for (const [column, type] of Object.entries(columns)) {
      const now = got[file]?.[column];
      if (now == null) broken.push(`${file}: ${column} (${type}) is gone`);
      else if (now !== type) broken.push(`${file}: ${column} is ${now}, was ${type}`);
    }
  }
  for (const [file, columns] of Object.entries(got)) {
    for (const column of Object.keys(columns)) if (!want[file]?.[column]) added.push(`${file}: ${column}`);
  }
  return { broken, added };
}

/** A DuckDB in Node, from the installed wasm module (the same engine the page runs). */
async function nodeDuckDB() {
  const require = createRequire(import.meta.url);
  const duckdb = require('@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs');
  const dist = dirname(require.resolve('@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs'));
  const bundles = {
    mvp: { mainModule: join(dist, 'duckdb-mvp.wasm'), mainWorker: join(dist, 'duckdb-node-mvp.worker.cjs') },
    eh: { mainModule: join(dist, 'duckdb-eh.wasm'), mainWorker: join(dist, 'duckdb-node-eh.worker.cjs') }
  };
  const db = await duckdb.createDuckDB(bundles, new duckdb.VoidLogger(), duckdb.NODE_RUNTIME);
  await db.instantiate();
  return db;
}

const kb = n => `${(n / 1024).toFixed(0)} KB`;

async function copyAll(files, from, to, { clean = false } = {}) {
  // Vite copies all of public/ into dist/, so an export dropped from TABLES would
  // keep shipping forever if it were merely left behind. Clearing first makes what
  // is staged exactly what this script names.
  //
  // Only the data directory is cleared. public/duckdb/ is git-tracked (the wasm
  // module ships via LFS), so wiping and re-copying it every build would make the
  // build depend on node_modules to restore committed files, and would silently
  // replace a 35 MB LFS object whenever the installed duckdb-wasm version moved.
  if (clean) await rm(to, { recursive: true, force: true });
  await mkdir(to, { recursive: true });
  let total = 0;
  for (const f of files) {
    const src = join(from, f);
    if (!existsSync(src)) {
      throw new Error(
        `missing required input: ${src}\n` +
        `  regenerate the exports with:  uv run benchmark analyze`
      );
    }
    await copyFile(src, join(to, f));
    total += (await stat(src)).size;
  }
  return total;
}

const dataBytes = await copyAll(TABLES, EXPORTS, DATA_OUT, { clean: true });
const wasmBytes = await copyAll(WASM_ASSETS, WASM_SRC, WASM_OUT);

const extension = join(WASM_OUT, PARQUET_EXTENSION);
if (!existsSync(extension) || (await stat(extension)).size < 100_000) {
  throw new Error(
    `missing or unfetched Parquet extension: ${extension}\n` +
    `  it ships via Git LFS (run \`git lfs pull\`); to refresh it for a new DuckDB version see the note in this script`
  );
}
const extBytes = (await stat(extension)).size;
const db = await nodeDuckDB();
const [needed, built] = await Promise.all([db.getVersion(), extensionVersion(extension)]);
if (needed !== built) {
  throw new Error(
    `Parquet extension version mismatch: the DuckDB module is ${needed} but ${PARQUET_EXTENSION} was built for ${built ?? 'an unknown version'}\n` +
    `  refresh it: curl -fsSL -o ${extension} https://extensions.duckdb.org/${needed}/wasm_eh/${PARQUET_EXTENSION}`
  );
}

const columns = exportColumns(db.connect());
if (process.argv.includes('--write-schema')) {
  await writeFile(SCHEMA, `${JSON.stringify(columns, null, 2)}\n`);
  console.log(`schema: recorded the columns of ${TABLES.length} exports in scripts/export-schema.json`);
} else {
  const { broken, added } = schemaDrift(JSON.parse(await readFile(SCHEMA, 'utf8')), columns);
  if (broken.length) {
    throw new Error(
      `the exports no longer match the columns the explorer reads (scripts/export-schema.json):\n` +
      broken.map(b => `  ${b}`).join('\n') + '\n' +
      `  if the change is intended, update the pages that read these columns, then record it:  npm run data -- --write-schema`
    );
  }
  if (added.length) {
    console.log(`schema: ${added.length} export column(s) not in scripts/export-schema.json yet (${added.join(', ')}); ` +
      'record them with `npm run data -- --write-schema` once the explorer reads them');
  }
}

console.log(`data:  ${TABLES.length} parquet files -> public/data (${kb(dataBytes)})`);
console.log(`wasm:  ${WASM_ASSETS.length} duckdb assets -> public/duckdb (${kb(wasmBytes)}) + parquet extension (${kb(extBytes)}, both DuckDB ${needed})`);
console.log(`       browser fetches ${kb(dataBytes)} of data + the ${kb(wasmBytes + extBytes)} wasm bundle (once, then cached)`);
