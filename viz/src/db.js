/**
 * DuckDB-WASM bootstrap.
 *
 * Everything here exists to keep the app deployable to a dumb static host:
 *
 *  - The wasm module, the worker AND the Parquet reader extension are
 *    SELF-HOSTED out of `public/duckdb/`, not pulled from jsDelivr or
 *    extensions.duckdb.org. That is what makes the app work offline after the
 *    first load, and it is why the URLs are resolved against `document.baseURI`
 *    — Vite's `base: './'` means the bundle can sit under any path prefix.
 *  - Only the `eh` (exception-handling) bundle is shipped. It is single
 *    threaded, so nothing here needs SharedArrayBuffer and the host never has
 *    to send COOP/COEP headers.
 *  - Parquet is fetched by the page and handed to DuckDB as a registered file
 *    buffer. Relative paths do not resolve inside the wasm worker, and this
 *    also keeps every byte accounted for.
 */
import * as duckdb from '@duckdb/duckdb-wasm';
import { coordinator, wasmConnector } from '@uwdata/mosaic-core';

/** Resolve an app-relative path against the page, honouring Vite `base: './'`. */
const asset = path => new URL(path, document.baseURI).href;

/**
 * The Parquet reader. Since DuckDB 1.5 it is a loadable extension rather than
 * part of the wasm module, and the first `read_parquet` would otherwise
 * autoload it from https://extensions.duckdb.org — a runtime CDN dependency
 * and an offline failure. Loading it explicitly from our own copy pre-empts
 * the autoload. The file must match the module's DuckDB version; the version
 * is logged at startup and the refresh recipe is in `scripts/prepare-data.mjs`.
 */
const PARQUET_EXTENSION = 'duckdb/parquet.duckdb_extension.wasm';

/**
 * Instantiate a single-threaded DuckDB-WASM database from self-hosted assets.
 * @returns {Promise<duckdb.AsyncDuckDB>}
 */
async function initDatabase() {
  const bundle = {
    mainModule: asset('duckdb/duckdb-eh.wasm'),
    mainWorker: asset('duckdb/duckdb-browser-eh.worker.js')
  };

  // The worker script must be same-origin; a blob shim importing the absolute
  // URL is the standard way to load it without an origin mismatch.
  const shim = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker}");`], { type: 'text/javascript' })
  );
  try {
    const worker = new Worker(shim);
    const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), worker);
    await db.instantiate(bundle.mainModule);
    return db;
  } finally {
    URL.revokeObjectURL(shim);
  }
}

/**
 * Fetch a parquet file and register it with DuckDB under its own name.
 * @param {duckdb.AsyncDuckDB} db
 * @param {string} file Filename under `data/`.
 * @returns {Promise<number>} Bytes fetched.
 */
async function registerParquet(db, file) {
  const res = await fetch(asset(`data/${file}`));
  if (!res.ok) throw new Error(`failed to fetch data/${file}: ${res.status} ${res.statusText}`);
  const bytes = new Uint8Array(await res.arrayBuffer());
  // Read the size first: registerFileBuffer transfers the ArrayBuffer to the
  // worker, which detaches it here and leaves byteLength at 0.
  const size = bytes.byteLength;
  await db.registerFileBuffer(file, bytes);
  return size;
}

/**
 * Start DuckDB-WASM, load the Parquet reader from our own copy, register the
 * parquet payload, and wire up the Mosaic coordinator against it.
 *
 * @param {string[]} files Parquet filenames to register.
 * @returns {Promise<{bytes: number, version: string}>} Total bytes of data
 *   fetched and the DuckDB version running in the worker.
 */
export async function connectDatabase(files) {
  const db = await initDatabase();
  const con = await db.connect();

  const [{ v: version }] = (await con.query('SELECT version() AS v')).toArray().map(r => r.toJSON());
  try {
    await con.query(`LOAD '${asset(PARQUET_EXTENSION)}'`);
  } catch (err) {
    throw new Error(
      `could not load the Parquet extension for DuckDB ${version} from ${PARQUET_EXTENSION}: ` +
      `${err.message}. See scripts/prepare-data.mjs for how to refresh it.`
    );
  }

  const sizes = await Promise.all(files.map(f => registerParquet(db, f)));

  coordinator().databaseConnector(wasmConnector({ duckdb: db, connection: con }));
  return { bytes: sizes.reduce((a, b) => a + b, 0), version };
}
