/**
 * Waits the browser tests share, so no test sleeps for a guessed time.
 *
 * Once DuckDB is up, every page answers an input in milliseconds; a fixed
 * sleep either wastes most of its time or, on a busy runner, ends too soon.
 * These wait on the thing itself: the page's queries, or the offline cache.
 */

/**
 * Wait until the page has finished reacting to the last input: no Mosaic
 * query queued or running (`window.__vizIdle`, set in src/site/boot.js) for
 * three animation frames in a row. Inputs reach Mosaic on a frame, and one
 * query's result can start another, so a single idle reading is not enough.
 * @param {import('@playwright/test').Page} page
 */
export async function settled(page) {
  await page.evaluate(() => new Promise(resolve => {
    let quiet = 0;
    const tick = () => {
      quiet = window.__vizIdle?.() ? quiet + 1 : 0;
      if (quiet >= 3) resolve();
      else requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }));
}

/** DuckDB files the worker fetches; they are not in the page's resource timing. */
const DUCKDB_FILES = ['duckdb-eh.wasm', 'duckdb-browser-eh.worker.js', 'parquet.duckdb_extension.wasm'];

/**
 * Wait until the offline cache (public/sw.js) holds every page, every asset
 * those pages load, the DuckDB files and every data file this page fetched,
 * so going offline next cannot race the worker's background caching.
 * @param {import('@playwright/test').Page} page
 */
export async function offlineReady(page) {
  await page.waitForFunction(async duckdbFiles => {
    if (!navigator.serviceWorker.controller) return false;
    const names = await caches.keys();
    if (!names.length) return false;
    const cache = await caches.open(names[0]);
    const cached = new Set((await cache.keys()).map(r => r.url));
    const root = new URL('./', document.baseURI);
    const pages = ['index.html', 'fit.html', 'engine.html', 'compare.html', 'queries.html', 'method.html']
      .map(p => new URL(p, root).href);
    if (!pages.every(p => cached.has(p))) return false;
    const want = new Set(duckdbFiles.map(f => new URL(`duckdb/${f}`, root).href));
    for (const p of pages) {
      const html = await (await cache.match(p)).text();
      for (const m of html.matchAll(/(?:src|href)="(\.\/assets\/[^"]+)"/g)) want.add(new URL(m[1], root).href);
    }
    for (const e of performance.getEntriesByType('resource')) {
      if (e.name.includes('/data/')) want.add(e.name);
    }
    // The chunks each script imports, followed the way the worker follows them.
    const queue = [...want].filter(u => u.includes('/assets/') && u.endsWith('.js'));
    while (queue.length) {
      const url = queue.shift();
      const res = await cache.match(url);
      if (!res) return false;
      for (const m of (await res.text()).matchAll(/["'`](\.\/[\w.-]+\.(?:js|css))["'`]/g)) {
        const dep = new URL(m[1], new URL('./', url)).href;
        if (!want.has(dep) && dep.includes('/assets/')) { want.add(dep); queue.push(dep); }
      }
    }
    return [...want].every(u => cached.has(u));
  }, DUCKDB_FILES, { polling: 100, timeout: 30_000 });
}

/**
 * Follow a link to another page, as a reader does, and wait until that page
 * has rendered and settled. The site answers such a link in place, over the
 * database already running (src/site/router.js).
 * @param {import('@playwright/test').Page} page
 * @param {string} href A page file, with or without a hash: `fit.html#sf=10`.
 */
export async function switchTo(page, href) {
  await page.evaluate(h => {
    const a = document.createElement('a');
    a.href = h;
    document.body.append(a);
    a.click();
    a.remove();
  }, href);
  await shown(page, href.replace(/\.html.*$/, ''));
}

/**
 * Wait until page `id` (its body class, `page-<id>`) is on screen, rendered
 * (`ready`, or a startup error) and settled.
 * @param {import('@playwright/test').Page} page
 * @param {string} id A page id: `fit`, `compare`, ...
 */
export async function shown(page, id) {
  await page.waitForFunction(p => document.body.classList.contains(`page-${p}`)
    && (document.body.classList.contains('ready') || !!document.querySelector('.error')), id);
  await settled(page);
}
