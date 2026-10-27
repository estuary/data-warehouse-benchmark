/**
 * Offline support: a same-origin service worker with no build step.
 *
 * The browser's HTTP cache alone does not make the page reload offline: static
 * hosts send `must-revalidate` (or nothing) for the HTML, so the navigation
 * itself fails without a network. This worker keeps one copy of everything the
 * app fetched — the shell, the hashed JS/CSS, the DuckDB module, worker and
 * Parquet extension, and the data files — and answers from it when the network
 * is gone.
 *
 * Strategies, by path under the worker's scope (which is the app's base):
 *   assets/…        cache-first: Vite content-hashes these, so a hit is exact.
 *   everything else network-first, cache as fallback: the DuckDB files and the
 *                   parquet exports keep fixed names across deploys, so a fresh
 *                   copy must win whenever one is reachable. Online, this costs
 *                   one conditional request per file (the fetch goes through
 *                   the HTTP cache, so an ETag match is a 304, not a download).
 *
 * The shell and its hashed assets are precached on install because the page
 * that registers the worker has already fetched them before it is controlled;
 * the worker claims that page as soon as it activates, so the large DuckDB
 * files it fetches afterwards are cached as they stream past. This file must
 * stay dependency-free: it runs before the app and must never reach a CDN.
 */

const CACHE = 'benchmark-explorer-v2';

/** The site's pages. Keep in step with PAGES in vite.config.js. */
const PAGES = ['index.html', 'fit.html', 'engine.html', 'compare.html', 'queries.html', 'method.html'];

/** Scope root, e.g. `https://host/some/prefix/`. Honors Vite `base: './'`. */
const ROOT = new URL('./', self.location.href);

/** Hashed asset URLs referenced by an HTML shell. */
function assetsIn(html) {
  const found = new Set();
  for (const m of html.matchAll(/(?:src|href)="(\.\/assets\/[^"]+)"/g)) {
    found.add(new URL(m[1], ROOT).href);
  }
  return [...found];
}

/**
 * The cache key for a page. The site has several pages (index.html, fit.html,
 * ...), each its own shell, so a navigation is keyed by its path without query
 * or hash. The root and `index.html` are one page, and a static host may serve
 * `compare.html` at `compare` too, so both spellings map to the file name.
 */
function pageKey(url) {
  const u = new URL(url);
  u.search = '';
  u.hash = '';
  if (u.href === ROOT.href) u.pathname += 'index.html';
  else if (!/\.[a-z]+$/.test(u.pathname) && !u.pathname.endsWith('/')) u.pathname += '.html';
  return u.href;
}

/**
 * A copy of a response that is safe to answer a navigation with. Hosts with
 * clean URLs redirect `compare.html` to `compare`, and the browser refuses to
 * use a response marked as redirected for a navigation, which made the page
 * fail offline. Rebuilding the response drops the flag.
 */
async function unredirected(res) {
  if (!res.redirected) return res;
  return new Response(await res.blob(), { status: res.status, statusText: res.statusText, headers: res.headers });
}

/** Cached page shells: every cached entry directly under the root ending in .html. */
async function cachedShells(cache) {
  const out = [];
  for (const req of await cache.keys()) {
    const rest = req.url.slice(ROOT.href.length);
    if (req.url.startsWith(ROOT.href) && /^[^/]+\.html$/.test(rest)) out.push(req);
  }
  return out;
}

/**
 * Drop cached `assets/` entries that no cached page references. The pages
 * share chunks, so a file is live while any shell still names it.
 */
async function pruneAssets(cache) {
  // Live: what any cached shell names, and every chunk those import, read
  // from the cached copies (a chunk is named only inside another script).
  const live = new Set();
  let queue = [];
  for (const req of await cachedShells(cache)) {
    const res = await cache.match(req);
    if (res) queue.push(...assetsIn(await res.text()));
  }
  while (queue.length) {
    const url = queue.pop();
    if (live.has(url)) continue;
    live.add(url);
    const res = url.endsWith('.js') ? await cache.match(url) : null;
    if (res) {
      const base = new URL('./', url);
      for (const m of (await res.text()).matchAll(/["'`](\.\/[\w.-]+\.(?:js|css))["'`]/g)) queue.push(new URL(m[1], base).href);
    }
  }
  for (const req of await cache.keys()) {
    if (req.url.startsWith(`${ROOT.href}assets/`) && !live.has(req.url)) await cache.delete(req);
  }
}

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    const shell = await fetch(ROOT.href, { cache: 'no-cache' });
    if (!shell.ok) return;
    const html = await shell.clone().text();
    await cache.put(pageKey(ROOT.href), await unredirected(shell));
    await cacheClosure(cache, assetsIn(html));
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key !== CACHE) await caches.delete(key);
    }
    await self.clients.claim();
    // The other pages are cached after the worker takes control, not during
    // install: install holds up the claim, and the page that registered the
    // worker fetches the 36 MB DuckDB module as soon as it is claimed, which
    // is what must pass through the cache. Not awaited for the same reason.
    warmPages();
  })());
});

/**
 * Cache every page and its assets, not only the one that registered the
 * worker: a reader who opened compare.html first must be able to reload it
 * offline, and the worker cannot tell which page that was.
 */
async function warmPages() {
  const cache = await caches.open(CACHE);
  const pages = await Promise.all(PAGES.map(async name => {
    const url = new URL(name, ROOT).href;
    const res = await fetch(url, { cache: 'no-cache' }).catch(() => null);
    if (!res || !res.ok) return '';
    const text = await res.clone().text();
    await cache.put(pageKey(url), await unredirected(res));
    return text;
  }));
  await cacheClosure(cache, pages.flatMap(assetsIn));
}

/**
 * Cache `urls` and every chunk they import. A page's HTML names only its entry
 * script and stylesheet; the shared chunks and each page's own code are
 * imported from inside the JS (`import ... from "./format-abc123.js"`, and
 * import(`./fit-abc123.js`) for a page, with backticks), so they are found by
 * reading each cached script in turn.
 */
async function cacheClosure(cache, urls) {
  const seen = new Set();
  let queue = [...urls];
  while (queue.length) {
    const next = [];
    for (const url of queue) {
      if (seen.has(url)) continue;
      seen.add(url);
      let res = await cache.match(url);
      if (!res) {
        await cache.add(url).catch(() => undefined);
        res = await cache.match(url);
      }
      if (res && url.endsWith('.js')) {
        const base = new URL('./', url);
        for (const m of (await res.text()).matchAll(/["'`](\.\/[\w.-]+\.(?:js|css))["'`]/g)) next.push(new URL(m[1], base).href);
      }
    }
    queue = next;
  }
}

self.addEventListener('fetch', event => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== ROOT.origin || !url.href.startsWith(ROOT.href)) return;

  const isNavigation = request.mode === 'navigate';
  // The app switches pages in place by fetching the next page's HTML
  // (src/site/router.js); that fetch is a page too, cached as one.
  const isPage = isNavigation || PAGES.includes(url.pathname.slice(ROOT.pathname.length));
  const isAsset = url.href.startsWith(`${ROOT.href}assets/`);
  // A page is keyed by its path, whatever the URL's query or hash, so a
  // bookmarked `compare.html#sf=1000` still opens offline.
  const key = isPage ? pageKey(url.href) : url.href.split('#')[0];

  event.respondWith((async () => {
    const cache = await caches.open(CACHE);
    if (isAsset) {
      const hit = await cache.match(key);
      if (hit) return hit;
    }
    try {
      const res = await fetch(request);
      if (res.ok) {
        await cache.put(key, isPage ? await unredirected(res.clone()) : res.clone());
        if (isNavigation) pruneAssets(cache);
      }
      return res;
    } catch (err) {
      const hit = await cache.match(key);
      if (hit) return hit;
      throw err;
    }
  })());
});
