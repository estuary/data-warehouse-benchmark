/**
 * Where the explorer is published, for what other sites see of it: each
 * page's canonical address and its link preview (Open Graph and Twitter card
 * tags, added at build by vite.config.js). These name the published site
 * whatever host serves a given build, so a preview or a mirror does not
 * compete with it.
 *
 * No imports and no document access, so the build and the tests can read it.
 */

/** The published site's root. */
export const SITE_URL = 'https://warehouse-benchmark.estuary.dev/';

/** The card a shared link shows; scripts/og-image.mjs renders it into public/. */
export const PREVIEW_IMAGE = {
  path: 'og-image.png',
  width: 1200,
  height: 630,
  alt: 'The 2026 Cloud Data Warehouse Benchmark by Estuary and CorrDyn: interactive results for seven cloud warehouses'
};

/** A page's canonical address: `index` is the site root, the rest `<page>.html`. */
export const pageUrl = page => new URL(page === 'index' ? '' : `${page}.html`, SITE_URL).href;
