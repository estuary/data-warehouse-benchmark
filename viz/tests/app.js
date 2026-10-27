/**
 * The page most tests run on: one per worker, opened once and kept running.
 *
 * Opening a page starts DuckDB-WASM, about 3 s (compiling the 36 MB module,
 * loading the Parquet extension, building the tables), and most tests' own
 * work takes a fraction of that. The site switches pages in place over the
 * database it already has (src/site/router.js), and so do the tests: `app` is
 * a page opened once per worker, and `visit` reaches a page on it by a link,
 * as a reader does. Every visit mounts its page afresh, as the site does for
 * each reader who comes back to a page, so anything a page fails to clean up
 * when it is left shows up in the next test, as it would for that reader.
 *
 * A test about starting up (what a fresh load logs or requests, going offline,
 * a context of its own) takes Playwright's own `page`, a fresh one per test.
 * `visit` works on either.
 */
import { test as base, expect } from '@playwright/test';
import { settled, switchTo } from './wait.js';

export const test = base.extend({
  // One running page per worker. Playwright starts a new worker after a
  // failed test, so a page a failure left broken is not reused.
  site: [async ({ browser }, use, workerInfo) => {
    const { baseURL, viewport } = workerInfo.project.use;
    const context = await browser.newContext({ baseURL, viewport });
    await use(await context.newPage());
    await context.close();
  }, { scope: 'worker' }],

  /** The shared page, handed back as the next test expects to find it. */
  app: async ({ site }, use, testInfo) => {
    await use(site);
    // What a test attached ends with it.
    for (const type of ['pageerror', 'console', 'request', 'download', 'dialog']) site.removeAllListeners(type);
    await site.unrouteAll({ behavior: 'ignoreErrors' });
    const { viewport } = testInfo.project.use;
    const size = site.viewportSize();
    if (viewport && (size?.width !== viewport.width || size?.height !== viewport.height)) await site.setViewportSize(viewport);
  }
});

export { expect };

/** The page id a page file names: `engine.html#engine=x` -> `engine`, `` -> `index`. */
const pageId = href => href.replace(/^\//, '').replace(/(\.html)?(#.*)?$/, '') || 'index';

/**
 * Open `href` (a page file, with or without a hash: `fit.html#sf=10`) on
 * `page` and wait until it has rendered and settled, or shown a startup
 * error. On a page the site is already running on, follow a link to it,
 * answered in place; when that page is the one on screen, go to another page
 * first, so it mounts afresh and reads the hash as a load would. On a page
 * with nothing running yet, load it.
 * @param {import('@playwright/test').Page} page
 * @param {string} href
 */
export async function visit(page, href) {
  const target = href.replace(/^\//, '');
  const current = await page.evaluate(() => (typeof window.__vizIdle === 'function' && document.body?.classList.contains('ready')
    ? /\bpage-(\w+)/.exec(document.body.className)?.[1] ?? null
    : null));
  if (!current) {
    await page.goto(target);
    await page.waitForFunction(() => document.body.classList.contains('ready') || document.querySelector('.error'));
    if (await page.locator('.error').count() === 0) await settled(page);
    return;
  }
  if (current === pageId(target)) await switchTo(page, current === 'method' ? 'index.html' : 'method.html');
  await switchTo(page, target);
}

/**
 * Open the page on screen again from its address, as a reload or a shared
 * link would, without starting the database again.
 * @param {import('@playwright/test').Page} page
 */
export async function reopen(page) {
  const url = new URL(page.url());
  await visit(page, `${url.pathname.split('/').pop() || 'index.html'}${url.hash}`);
}

/**
 * Words a page shows only when a value went wrong on its way to the screen: a
 * column read that came back undefined (renamed in the exports), a division by
 * zero, an object printed whole. None of them appears in the site's copy.
 */
const BROKEN_VALUE = /\b(?:NaN|undefined|null|Infinity)\b|\[object Object\]/;

/**
 * Fail if any text on the page, chart labels included, is a broken value.
 * @param {import('@playwright/test').Page} page
 * @param {string} what Names the page and state in the failure.
 */
export async function expectNoBrokenValues(page, what) {
  const text = await page.evaluate(() => document.body.textContent.replace(/\s+/g, ' '));
  const at = text.search(BROKEN_VALUE);
  expect(at < 0 ? null : text.slice(Math.max(0, at - 60), at + 60), `${what}: a value rendered as NaN, undefined, null or Infinity`).toBeNull();
}
