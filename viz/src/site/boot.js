/**
 * Startup, once per visit: the site chrome, the offline worker, DuckDB-WASM,
 * the parquet exports and the derived views, then the first page's own code.
 * After that the pages switch in place over the same database
 * (src/site/router.js). Any failure lands in a visible panel rather than only
 * the console: a wasm or asset 404 is the failure mode this app is most likely
 * to hit on an unfamiliar host.
 *
 * Every page ends by putting `ready` on <body>. The browser tests and the
 * post-deploy smoke test wait for that marker, so a page that never sets it
 * fails loudly instead of passing on a half-rendered DOM.
 */
import { coordinator } from '@uwdata/vgplot';
import { connectDatabase } from '../db.js';
import { loadTables, PARQUET_FILES } from '../data.js';
import { mountSite } from './layout.js';
import { focusableScrollAreas, stripPlotMarkLabels } from './a11y.js';
import { idle, loadPage, openFirst } from './router.js';
import { stackTables } from './stack-tables.js';
import { sortTables } from './sort-tables.js';

/** How long startup waits for the service worker to take control, in ms. */
const SW_CLAIM_TIMEOUT_MS = 1500;

/**
 * Register the offline service worker and give it a moment to claim this page,
 * so the large DuckDB fetches that follow are cached on their way through.
 * Production only: in dev, caching Vite's module graph would fight hot reload.
 * Never blocks startup for long, and never fails it.
 */
async function registerOffline() {
  if (!import.meta.env.PROD || !('serviceWorker' in navigator)) return;
  try {
    await navigator.serviceWorker.register(new URL('sw.js', document.baseURI));
    if (navigator.serviceWorker.controller) return;
    await Promise.race([
      new Promise(res => navigator.serviceWorker.addEventListener('controllerchange', res, { once: true })),
      new Promise(res => setTimeout(res, SW_CLAIM_TIMEOUT_MS))
    ]);
  } catch (err) {
    console.warn('offline cache unavailable:', err);
  }
}

/** Show a startup failure where the page content would have been. */
function fail(err) {
  console.error(err);
  const status = document.getElementById('status');
  const html = `<div class="error"><strong>Failed to start.</strong><br>${String(err.message || err)}
     <p>This app needs a static HTTP server. DuckDB-WASM cannot load from
     <code>file://</code>. Try <code>npx serve dist</code>.</p></div>`;
  if (status) status.innerHTML = html;
  else document.body.insertAdjacentHTML('afterbegin', html);
}

/**
 * Start the site on the page the browser loaded.
 *
 * The page is named by its body class (`page-compare`), which is right however
 * the host spells the URL. Its code loads while the database starts.
 */
export function start() {
  const page = /\bpage-(\w+)/.exec(document.body.className)?.[1] ?? 'index';
  const code = loadPage(page);
  mountSite(page);
  stripPlotMarkLabels();
  focusableScrollAreas();
  stackTables();
  sortTables();
  const status = document.getElementById('status');
  const t0 = performance.now();
  (async () => {
    await registerOffline();
    if (status) status.textContent = 'Starting the in-browser database';
    const t1 = performance.now();
    const { version } = await connectDatabase(PARQUET_FILES);
    const t2 = performance.now();
    if (status) status.textContent = 'Loading benchmark results';
    await loadTables();
    const t3 = performance.now();
    // Mosaic logs every query at debug level. Quiet it in production so anyone
    // debugging their own page is not wading through ours.
    if (import.meta.env.PROD) coordinator().logger(null);
    // For the browser tests (tests/wait.js): true when Mosaic has no query
    // queued or running, so a test waits for the page instead of sleeping.
    window.__vizIdle = idle;
    await openFirst(page, code);
    const t4 = performance.now();
    // The phases, so a slow start can be placed: worker and offline cache,
    // DuckDB and the parquet fetch, tables and views, the page's own queries.
    const ms = (a, b) => Math.round(b - a);
    console.info(`${page} ready in ${ms(t0, t4)} ms on DuckDB ${version} `
      + `(start ${ms(t0, t1)}, database ${ms(t1, t2)}, tables ${ms(t2, t3)}, page ${ms(t3, t4)})`);
  })().catch(fail);
}
