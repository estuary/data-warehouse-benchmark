/**
 * Post-deploy smoke test: load the published Compare page and require its own
 * ready marker and a rendered frontier.
 *
 * Pages can report a successful deploy and still serve a broken page (a stale
 * asset, a wasm that failed to load), so the workflow runs this against the
 * live URL after `deploy-pages` and fails when a reader would. The CDN can lag
 * the deploy by a minute; the loop retries before giving up.
 *
 *   PAGE_URL=https://host/path/ node scripts/smoke.mjs
 */
import { chromium } from 'playwright';

const url = process.env.PAGE_URL;
if (!url) throw new Error('PAGE_URL is required');

const ATTEMPTS = 6;
const RETRY_DELAY_MS = 15_000;
const READY_TIMEOUT_MS = 60_000;

/** The frontier's dots; src/site/a11y.js moves Plot's mark name to data-mark. */
const FRONTIER_DOTS = '#view-cfrontier g[data-mark="dot"] > *';

const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  const target = new URL('compare.html', url.endsWith('/') ? url : `${url}/`).href;

  for (let attempt = 1; ; attempt++) {
    await page.goto(target, { waitUntil: 'domcontentloaded' });
    try {
      await page.waitForFunction(() => document.body.classList.contains('ready'), null, { timeout: READY_TIMEOUT_MS });
      break;
    } catch {
      if (attempt === ATTEMPTS) throw new Error(`published page never became ready: ${errors.join('; ') || 'no page errors'}`);
      console.log(`attempt ${attempt}: not ready yet, retrying in ${RETRY_DELAY_MS / 1000}s`);
      await new Promise(r => setTimeout(r, RETRY_DELAY_MS));
    }
  }

  // `ready` is set before Mosaic's first animation-frame render; give the
  // frontier a moment to draw before counting.
  await page.waitForFunction(
    sel => document.querySelectorAll(sel).length > 0,
    FRONTIER_DOTS, { timeout: READY_TIMEOUT_MS }
  ).catch(() => {});
  const dots = await page.locator(FRONTIER_DOTS).count();
  if (dots === 0) throw new Error('frontier rendered no marks');
  if (errors.length) throw new Error(`page errors: ${errors.join('; ')}`);
  console.log(`ok: ${target} ready with ${dots} frontier marks`);
} finally {
  await browser.close();
}
