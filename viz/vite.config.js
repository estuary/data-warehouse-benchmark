import { basename, resolve } from 'node:path';
import { defineConfig } from 'vite';
import { PREVIEW_IMAGE, SITE_URL, pageUrl } from './src/site/published.js';

/**
 * Canonical address and link-preview tags for every page, built from the
 * page's own <title> and description so the two cannot drift apart. Crawlers
 * and chat apps read the HTML as served, before any script runs, so the tags
 * are written into each file here rather than by the page's code.
 */
function linkPreviews() {
  return {
    name: 'link-previews',
    transformIndexHtml(html, ctx) {
      const page = basename(ctx.filename, '.html');
      const title = /<title>([^<]+)<\/title>/.exec(html)?.[1];
      const description = /<meta name="description" content="([^"]+)">/.exec(html)?.[1];
      if (!title || !description) throw new Error(`${page}.html needs a <title> and a meta description for its link preview`);
      const meta = (key, content) => ({ tag: 'meta', attrs: { [key.startsWith('og:') ? 'property' : 'name']: key, content }, injectTo: 'head' });
      const image = new URL(PREVIEW_IMAGE.path, SITE_URL).href;
      return [
        { tag: 'link', attrs: { rel: 'canonical', href: pageUrl(page) }, injectTo: 'head' },
        meta('og:type', 'website'),
        meta('og:site_name', '2026 Cloud Data Warehouse Benchmark'),
        meta('og:url', pageUrl(page)),
        meta('og:title', title),
        meta('og:description', description),
        meta('og:image', image),
        meta('og:image:width', String(PREVIEW_IMAGE.width)),
        meta('og:image:height', String(PREVIEW_IMAGE.height)),
        meta('og:image:alt', PREVIEW_IMAGE.alt),
        meta('twitter:card', 'summary_large_image'),
        meta('twitter:image:alt', PREVIEW_IMAGE.alt)
      ];
    }
  };
}

/**
 * One HTML entry per page, so every page has its own URL, opens directly and
 * reloads offline. All six load the same script (src/site/app.js): after the
 * first page, a link to another page swaps that page's content in place over
 * the database already running (src/site/router.js), and each page's own code
 * is a chunk loaded the first time it is shown. Keep this list in step with
 * `PAGES` in src/site/layout.js and public/sw.js.
 */
const PAGES = ['index', 'fit', 'engine', 'compare', 'queries', 'method'];

export default defineConfig({
  // Relative asset URLs so the bundle runs from any path prefix on any static
  // host (GitHub Pages project sites and their /preview/ path, S3 key prefixes,
  // Cloudflare Pages).
  base: './',
  plugins: [linkPreviews()],
  build: {
    outDir: process.env.VIZ_OUT_DIR || 'dist',
    // The DuckDB-WASM module in public/ is ~36 MB; it is copied, not bundled.
    chunkSizeWarningLimit: 2000,
    // The shared chunks load through each page's own imports. Preload hints
    // add nothing on a warm cache and draw a console warning per chunk once
    // the service worker answers the page (a preload/fetch credentials mismatch).
    modulePreload: false,
    rollupOptions: {
      input: Object.fromEntries(PAGES.map(p => [p, resolve(import.meta.dirname, `${p}.html`)]))
    }
  },
  server: { open: true }
});
