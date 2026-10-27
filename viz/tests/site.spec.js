/**
 * Browser tests for what the pages share and for the pages without a spec of
 * their own: the Overview, the Method page, the sizing and scan views on the
 * Compare page, the site navigation, the glossary and the chart downloads.
 *
 * As in explorer.spec.js, expectations are computed by a DuckDB in Node from
 * the same parquet files the pages load, so a page that draws a number the data
 * does not support fails here.
 */
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { readFileSync, statSync } from 'node:fs';
import { expect, expectNoBrokenValues, test, visit } from './app.js';
import { offlineReady, settled, shown, switchTo } from './wait.js';

const require = createRequire(import.meta.url);
const duckdb = require('@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs');

const here = dirname(fileURLToPath(import.meta.url));
const DATA = join(here, '..', 'public', 'data');
const ORIGIN = `http://localhost:${process.env.VIZ_PORT || 4173}`;
const file = name => `read_parquet('${join(DATA, name)}')`;

/** Every page and its file. */
const PAGES = ['index.html', 'fit.html', 'engine.html', 'compare.html', 'queries.html', 'method.html'];

let db;
let con;

test.beforeAll(async () => {
  const dist = dirname(require.resolve('@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs'));
  const bundles = {
    mvp: { mainModule: join(dist, 'duckdb-mvp.wasm'), mainWorker: join(dist, 'duckdb-node-mvp.worker.cjs') },
    eh: { mainModule: join(dist, 'duckdb-eh.wasm'), mainWorker: join(dist, 'duckdb-node-eh.worker.cjs') }
  };
  db = await duckdb.createDuckDB(bundles, new duckdb.VoidLogger(), duckdb.NODE_RUNTIME);
  await db.instantiate();
  con = db.connect();
});

test.afterAll(() => {
  con?.close();
});

const sql = query => con.query(query).toArray().map(r => r.toJSON());
const scalar = query => Number(Object.values(sql(query)[0])[0]);

/** Open a page (see `visit` in tests/app.js), collecting errors, warnings and requests. */
async function open(page, path) {
  const errors = [];
  const warnings = [];
  const requests = [];
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
  page.on('console', m => {
    if (m.type() === 'error') errors.push(`console: ${m.text()}`);
    if (m.type() === 'warning') warnings.push(m.text());
  });
  page.on('request', r => requests.push(r.url()));
  await visit(page, path);
  await expect(page.locator('.error')).toHaveCount(0);
  return { errors, warnings, requests };
}

test('every page starts clean, stays on its origin and knows every glossary term', async ({ page }) => {
  test.setTimeout(240_000);
  for (const path of PAGES) {
    // A direct load of each page, the way a shared link arrives.
    await page.goto('about:blank');
    const { errors, warnings, requests } = await open(page, `/${path}`);
    expect(errors, `${path}: no errors`).toEqual([]);
    expect(warnings.filter(w => w.includes('unknown glossary term')), `${path}: every data-term is defined`).toEqual([]);
    const external = requests.filter(u => !u.startsWith(ORIGIN) && !u.startsWith('blob:') && !u.startsWith('data:'));
    expect(external, `${path}: every request stays on the page origin`).toEqual([]);
    // Site chrome: the navigation lists every page and marks this one.
    await expect(page.locator('#site-nav [aria-current="page"]'), `${path}: current page marked`).toHaveCount(1);
    await expect(page.locator('#site-footer'), `${path}: footer rendered`).toContainText('Run jointly by');
    await expectNoBrokenValues(page, path);
  }
});

/** Something each page draws only once its code has run against the data. */
const DRAWN = {
  index: '#facts li',
  fit: '#fit-results tbody tr',
  engine: '#eng-sizes-table tbody tr',
  compare: '#view-frontier g[data-mark="dot"] > *',
  queries: '#q-table-body tr.qrow',
  method: '#glossary-body .gloss-item'
};

test('a link to another page switches in place: DuckDB starts once and every page renders', async ({ page }) => {
  const { errors, warnings, requests } = await open(page, '/index.html');
  // A full page load would drop this.
  await page.evaluate(() => { window.__sameDocument = true; });
  // Through the site navigation, as a reader goes; Engines is a menu.
  for (const id of ['fit', 'compare', 'queries', 'method', 'engine', 'index', 'compare']) {
    if (id === 'engine') {
      await page.locator('#site-nav .has-menu summary').click();
      await page.locator('#site-nav .has-menu a', { hasText: 'Redshift' }).click();
    } else {
      await page.locator(`#site-nav a[data-page="${id}"]`).click();
    }
    await shown(page, id);
    await expect(page.locator('.error'), `${id}: no startup error`).toHaveCount(0);
    expect(new URL(page.url()).pathname, `${id}: address`).toMatch(new RegExp(`/${id}\\.html$`));
    await expect(page.locator('#site-nav [aria-current="page"]'), `${id}: marked current`).toHaveAttribute('data-page', id);
    expect(await page.locator(DRAWN[id]).count(), `${id}: rendered`).toBeGreaterThan(0);
  }
  await expect(page.locator('h1')).toHaveText('All seven engines on the same charts');
  expect(await page.evaluate(() => window.__sameDocument), 'no page load between pages').toBe(true);
  expect(requests.filter(u => u.endsWith('/duckdb-eh.wasm')), 'the DuckDB module is fetched once').toHaveLength(1);
  expect(errors).toEqual([]);
  expect(warnings).toEqual([]);
});

test('back and forward move between pages and keep each page\'s controls and place', async ({ app: page }) => {
  const { errors } = await open(page, '/compare.html#sf=1000&sla=3000&axis=linear');
  await page.evaluate(() => { window.__sameDocument = true; });
  const selects = () => page.evaluate(() => [...document.querySelectorAll('#controls select')].slice(0, 2).map(s => s.value));
  expect(await selects()).toEqual(['1000', 'linear']);
  // Leave from the foot of the page, so there is a place to come back to.
  const link = page.locator('#site-footer a[href="method.html"]').first();
  await link.scrollIntoViewIfNeeded();
  const y = await page.evaluate(() => window.scrollY);
  expect(y).toBeGreaterThan(1000);
  await link.click();
  await shown(page, 'method');
  expect(await page.evaluate(() => window.scrollY), 'a new page opens at its top').toBe(0);

  await page.goBack();
  await shown(page, 'compare');
  expect(new URL(page.url()).hash).toContain('sf=1000');
  expect(await selects(), 'the controls the reader left').toEqual(['1000', 'linear']);
  await expect.poll(() => page.evaluate(() => window.scrollY), { message: 'the place the reader left' }).toBeGreaterThan(y - 50);

  await page.goForward();
  await shown(page, 'method');
  await page.goBack();
  await shown(page, 'compare');
  expect(await page.evaluate(() => window.__sameDocument), 'no page load').toBe(true);
  expect(errors).toEqual([]);
});

test('a page from a newer build than the one running loads the ordinary way', async ({ browser }) => {
  // No service worker, so the route below sees the page's own fetch.
  const context = await browser.newContext({ serviceWorkers: 'block' });
  const page = await context.newPage();
  const { errors, warnings } = await open(page, '/index.html');
  await page.evaluate(() => { window.__sameDocument = true; });
  // A deploy since the tab opened: the HTML the switch fetches names other
  // scripts. A navigation to the page gets the real file.
  await page.route('**/fit.html', async route => {
    if (route.request().isNavigationRequest()) return route.continue();
    const res = await route.fetch();
    const body = (await res.text()).replace(/assets\/app-[\w-]+\.js/, 'assets/app-from-a-later-build.js');
    return route.fulfill({ response: res, body });
  });
  await page.locator('#site-nav a[data-page="fit"]').click();
  await shown(page, 'fit');
  await expect(page.locator('.error')).toHaveCount(0);
  expect(await page.evaluate(() => window.__sameDocument), 'a full page load').toBeUndefined();
  expect(warnings.some(w => w.includes('another build')), 'the switch says why it fell back').toBe(true);
  expect(errors).toEqual([]);
  await context.close();
});

test('the Overview computes its findings from the data', async ({ app: page }) => {
  await open(page, '/index.html');
  // Finding 2: the cheapest size serving 50 people at 100 GB, per engine, at 5 s.
  const cheapest50 = sql(`
    WITH c AS (
      SELECT platform, compute_label, any_value(usd_per_hour) AS usd,
             coalesce(max(concurrency) FILTER (WHERE p50_ms <= 5000 AND shed_pct < sla_max_shed_pct), 0) AS people
      FROM ${file('concurrency_grid.parquet')}
      WHERE publishable AND usd_per_hour IS NOT NULL AND workload = 'decision_support' AND scale_factor = 100
      GROUP BY 1, 2)
    SELECT platform, min(usd) AS usd FROM c WHERE people >= 50 GROUP BY 1`);
  const text = await page.locator('#f2-body').textContent();
  for (const r of cheapest50) expect(text).toContain(`$${Number(r.usd).toFixed(2)}`);
  const lo = Math.min(...cheapest50.map(r => Number(r.usd)));
  const hi = Math.max(...cheapest50.map(r => Number(r.usd)));
  expect(text).toContain(`from $${lo.toFixed(2)} to $${hi.toFixed(2)} an hour`);
  // Finding 1: the Snowflake ladder table has one row per size measured at 100 GB.
  expect(await page.locator('#f1-body tbody tr').count()).toBe(
    scalar(`SELECT count(DISTINCT compute_label) FROM ${file('size_ladder.parquet')} WHERE platform = 'snowflake' AND scale_factor IN (100, 1000)`));
  // Finding 4: one row per engine.
  expect(await page.locator('#f4-body tbody tr').count()).toBe(7);
});

test('the Method page lists every measured size\'s price, the agreement check and the glossary', async ({ app: page }) => {
  await open(page, '/method.html');
  // Every rate-card size with a published result (a size no export has is left out).
  expect(await page.locator('#prices-body tbody tr').count()).toBe(scalar(`SELECT count(*) FROM ${file('rate_card.parquet')} WHERE (platform, compute_label) IN (SELECT (platform, compute_label) FROM ${file('size_ladder.parquet')} UNION SELECT (platform, compute_label) FROM ${file('concurrency_grid.parquet')} UNION SELECT (platform, compute_label) FROM ${file('clickbench_summary.parquet')})`));
  const combos = scalar(`SELECT count(*) FROM (SELECT DISTINCT query_id, scale_factor FROM ${file('row_count_agreement.parquet')})`);
  await expect(page.locator('#correctness-body')).toContainText(`of ${combos.toLocaleString('en-US')} query and data-volume combinations`);
  expect(await page.locator('.gloss-item').count()).toBeGreaterThan(30);
  // Every download link resolves.
  for (const href of await page.locator('#downloads-body a').evaluateAll(as => as.map(a => a.getAttribute('href')))) {
    const res = await page.request.get(href);
    expect(res.ok(), `${href} downloads`).toBe(true);
  }
});

test('a glossary term shows its definition and links to the glossary', async ({ app: page }) => {
  await open(page, '/index.html');
  // The page is shared with earlier tests: park the mouse where no term or
  // note sits, so a stray hover cannot swap the card this test reads.
  await page.mouse.move(1, 1);
  const term = page.locator('[data-term="session"]').first();
  await term.focus();
  await expect(page.locator('#term-tip')).toBeVisible();
  await expect(page.locator('#term-tip')).toContainText('One connection running one query at a time');
  await page.locator('#term-tip a', { hasText: 'Glossary' }).click();
  await shown(page, 'method');
  expect(new URL(page.url()).hash).toBe('#term-session');
  await expect(page.locator('#term-session')).toBeVisible();
  await expect(page.locator('#term-tip')).toBeHidden();
});

test('the sizing and scan views draw what their SQL returns', async ({ app: page }) => {
  await open(page, '/compare.html');
  // Sizing at 100 GB: one speedup point per size on ladders of two or more
  // sizes, and one cost point per size with a cost multiple.
  const ladders = sql(`
    WITH d AS (SELECT * FROM ${file('size_ladder.parquet')} l WHERE scale_factor = 100 AND compute_label <> 'on-demand' AND block <> 'clickbench'
      -- a cell recorded as skipped in the single-user pass is left out of the ladders
      AND NOT EXISTS (SELECT 1 FROM ${file('scale_skips.parquet')} k WHERE k.platform = l.platform
        AND k.scale_factor IS NOT DISTINCT FROM l.scale_factor AND k.run_type IN ('*', 'warm')
        AND k.block IN ('*', l.block) AND k.compute_label IN ('*', l.compute_label)))
    SELECT platform, count(DISTINCT compute_label) AS sizes FROM d GROUP BY 1 HAVING sizes > 1`);
  const speedPoints = ladders.reduce((a, r) => a + Number(r.sizes), 0);
  const costPoints = speedPoints;
  // The sizing and scan plots sit far down the page and render after `ready`
  // once their queries return; on a slower runner that is seconds later.
  await expect.poll(() => page.locator('#view-sizing g[data-mark="dot"] circle').count(), { timeout: 30_000 })
    .toBe(speedPoints + costPoints);
  // Scan: one dot per priced ClickBench size.
  const scanSizes = scalar(`
    SELECT count(*) FROM (SELECT DISTINCT l.platform, l.compute_label FROM ${file('size_ladder.parquet')} l
      JOIN ${file('rate_card.parquet')} r USING (platform, compute_label)
      WHERE l.block = 'clickbench' AND r.usd_per_hour IS NOT NULL)`);
  // Plus one caution ring per size in SEPARATE_SESSION (constants.js).
  const { SEPARATE_SESSION } = await import('../src/constants.js');
  const rings = SEPARATE_SESSION.filter(a => a.kind === 'scan').reduce((n, a) => n + a.labels.length, 0);
  await expect.poll(() => page.locator('#view-scan g[data-mark="dot"] circle').count(), { timeout: 30_000 }).toBe(scanSizes + rings);
  // The fastest-size table names each engine once, fastest first.
  const fastest = sql(`
    WITH t AS (
      SELECT l.platform, l.compute_label, exp(avg(ln(median_server_ms)) FILTER (WHERE median_server_ms > 0)) AS ms
      FROM ${file('size_ladder.parquet')} l JOIN ${file('rate_card.parquet')} r USING (platform, compute_label)
      WHERE l.block = 'clickbench' AND r.usd_per_hour IS NOT NULL GROUP BY 1, 2)
    SELECT platform, min(ms) AS ms FROM t GROUP BY 1 ORDER BY ms`);
  await expect.poll(() => page.locator('#view-scan-fastest tbody tr').count(), { timeout: 30_000 }).toBe(fastest.length);
});

test('a chart downloads as a PNG with its title and selection', async ({ app: page }) => {
  await open(page, '/compare.html');
  const download = page.waitForEvent('download');
  await page.locator('#sec-frontier .chart-tools button', { hasText: 'PNG' }).click();
  const png = await download;
  expect(png.suggestedFilename()).toBe('sec-frontier.png');
  const bytes = statSync(await png.path()).size;
  expect(bytes, 'a PNG with content').toBeGreaterThan(20_000);
  // The CSV is the frontier's rows at the current selection.
  const csv = page.waitForEvent('download');
  await page.locator('#sec-frontier .chart-tools button', { hasText: 'Data (CSV)' }).click();
  const text = readFileSync(await (await csv).path(), 'utf8');
  const lines = text.trim().split('\n');
  expect(lines[0]).toContain('people_served');
  expect(lines.length - 1).toBe(scalar(`SELECT count(*) FROM (SELECT DISTINCT platform, compute_label
    FROM ${file('concurrency_grid.parquet')} WHERE publishable AND usd_per_hour IS NOT NULL AND scale_factor = 100)`));
});

test('the Compare filter bar folds to one line once it sticks', async ({ app: page }) => {
  await open(page, '/compare.html');
  // Scroll the fold's own trigger, the sentinel above the bar, well past the
  // top of the window (offsetTop is relative to a positioned parent, not the page).
  await page.evaluate(() => {
    const top = document.getElementById('controls-sentinel').getBoundingClientRect().top + scrollY;
    window.scrollTo(0, top + 600);
  });
  await expect(page.locator('#controls')).toHaveClass(/compact/);
  const h = await page.locator('#controls').evaluate(el => el.getBoundingClientRect().height);
  expect(h, 'the folded bar is one line').toBeLessThan(70);
  await expect(page.locator('#controls .ctl-summary-text')).toContainText('100 GB');
  await page.locator('#controls .ctl-expand').click();
  await expect(page.locator('#controls select').first()).toBeVisible();
});

test('chart tooltips show every value in full, with no line cut off', async ({ page }) => {
  test.setTimeout(180_000);
  for (const path of ['/engine.html#engine=fabric&sf=100', '/compare.html']) {
    await page.goto(path);
    await page.waitForFunction(() => document.body.classList.contains('ready'));
    // The dot charts draw after `ready` as their queries return; under a full
    // suite's load that can take seconds, so wait for them rather than a fixed pause.
    const dotCharts = () => page.evaluate(() => [...document.querySelectorAll('svg')]
      .filter(svg => svg.querySelector('g[data-mark="dot"] circle') && svg.closest('[id]'))
      .map(svg => svg.closest('[id]').id));
    await expect.poll(async () => (await dotCharts()).length, { timeout: 30_000 }).toBeGreaterThan(2);
    const charts = await dotCharts();
    // Some dot marks carry no tooltip (the red rings), so try each until one shows.
    let shown = 0;
    for (const id of new Set(charts)) {
      const groups = page.locator(`#${id} svg g[data-mark="dot"]`);
      for (let g = 0; g < await groups.count(); g++) {
        const dots = groups.nth(g).locator('circle');
        const dot = dots.nth(Math.floor((await dots.count()) / 2));
        await dot.scrollIntoViewIfNeeded();
        const b = await dot.boundingBox();
        if (!b) continue;
        await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2);
        await page.waitForTimeout(150);
        const text = await page.evaluate(i => [...document.querySelectorAll(`#${i} g[data-mark="tip"]`)].map(t => t.textContent).join(''), id);
        if (!text) continue;
        expect(text, `tooltip in #${id}`).not.toContain('…');
        shown += 1;
        break;
      }
    }
    expect(shown, `charts with a tooltip on ${path}`).toBeGreaterThan(2);
  }
});

test('every page uses the site favicon', async ({ page }) => {
  const { BRAND_MARK } = await import('../src/site/brand.js');
  const norm = svg => svg.replace(/"/g, "'").replace(/\s+/g, ' ').trim();
  for (const name of ['index', 'fit', 'engine', 'compare', 'queries', 'method']) {
    const html = readFileSync(join(here, '..', `${name}.html`), 'utf8');
    const href = html.match(/<link rel="icon" type="image\/svg\+xml" href="([^"]+)">/)?.[1];
    expect(href, `${name}.html has an SVG favicon`).toBeTruthy();
    const favicon = decodeURIComponent(href.replace(/^data:image\/svg\+xml,/, ''));
    expect(norm(favicon), `${name}.html favicon matches src/site/brand.js`).toContain(norm(BRAND_MARK));
  }
});

/** Tick labels per axis of every visible chart; facet panels repeat an axis, so a label is a repeat only past that count. */
const repeatedTicks = page => page.evaluate(() => [...document.querySelectorAll('svg')]
  .filter(svg => svg.getClientRects().length)
  .flatMap(svg => [...svg.querySelectorAll('g[data-mark="x-axis tick label"], g[data-mark="y-axis tick label"]')].map(g => {
    const labels = [...g.querySelectorAll('text')].map(t => t.textContent).filter(Boolean);
    const counts = labels.reduce((m, l) => m.set(l, (m.get(l) ?? 0) + 1), new Map());
    const panels = Math.min(...counts.values());
    return [...counts].filter(([, n]) => n > panels).map(([l]) => `#${svg.closest('[id]')?.id} ${g.dataset.mark}: ${l}`);
  }))
  .flat());

test('no chart axis repeats a tick label, and the scan chart keeps one range across engines', async ({ page }) => {
  test.setTimeout(240_000);
  const scanRanges = new Set();
  for (const engine of ['snowflake', 'databricks', 'bigquery', 'redshift', 'fabric', 'motherduck', 'clickhouse']) {
    await page.goto(`/engine.html#engine=${engine}`);
    await page.waitForFunction(() => document.body.classList.contains('ready'));
    await settled(page);
    expect(await repeatedTicks(page), `${engine} page`).toEqual([]);
    scanRanges.add(await page.evaluate(() =>
      [...document.querySelectorAll('#eng-scan-chart g[data-mark="y-axis tick label"] text')].map(t => t.textContent).join('|')));
  }
  expect(scanRanges.size, 'every engine page draws the scan chart on the same y ticks').toBe(1);
  for (const sf of ['100', '1000']) {
    await page.goto(`/compare.html#sf=${sf}`);
    await page.waitForFunction(() => document.body.classList.contains('ready'));
    await settled(page);
    expect(await repeatedTicks(page), `Compare at ${sf}`).toEqual([]);
  }
});

test('choosing scan queries shows the one fixed data volume, not greyed-out choices', async ({ page }) => {
  await page.goto('/fit.html');
  await page.waitForFunction(() => document.body.classList.contains('ready'));
  await page.locator('#in-kind button[data-kind="scan"]').click();
  await expect(page.locator('#in-sf button')).toHaveCount(1);
  await expect(page.locator('#in-sf button')).toHaveText('About 100 GB (ClickBench)');
  await expect(page.locator('#in-sf button')).toHaveAttribute('aria-pressed', 'true');
  await page.locator('#in-kind button[data-kind="join"]').click();
  await expect(page.locator('#in-sf button')).toHaveCount(4);
  await expect(page.locator('#in-sf button[aria-pressed="true"]')).toHaveText('100 GB');
});

test('notes on table cells open in the site card, not the browser tooltip', async ({ page }) => {
  await page.goto('/fit.html');
  await page.waitForFunction(() => document.body.classList.contains('ready'));
  const badge = page.locator('#fit-results .fit-idle').first();
  await badge.hover();
  await expect(page.locator('#term-tip')).toBeVisible();
  await expect(page.locator('#term-tip b')).toHaveText('When the warehouse is quiet');
  await page.mouse.move(0, 0);
  await expect(page.locator('#term-tip')).toBeHidden();
  // No data table carries a browser tooltip, headings included.
  for (const path of ['/fit.html', '/engine.html#engine=fabric', '/queries.html']) {
    await page.goto(path);
    await page.waitForFunction(() => document.body.classList.contains('ready'));
    expect(await page.locator('table [title]').count(), path).toBe(0);
  }
});

test('tables that sort themselves in SQL keep their own sorting', async ({ page }) => {
  for (const [path, sel] of [['/compare.html', '#view-configs table'], ['/queries.html', 'table.qtable']]) {
    await page.goto(path);
    await page.waitForFunction(() => document.body.classList.contains('ready'));
    await expect(page.locator(sel).first()).toBeAttached();
    await expect(page.locator(`${sel} th.can-sort`)).toHaveCount(0);
  }
  // Every other data table on a page with several gets sortable headings.
  await page.goto('/method.html');
  await page.waitForFunction(() => document.body.classList.contains('ready'));
  expect(await page.locator('table.data-table th.can-sort').count()).toBeGreaterThan(0);
});

test('every page fits a phone without sideways scrolling', async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  // A direct visit to the first page, then the others as a reader reaches them.
  await open(page, `/${PAGES[0]}`);
  for (const path of PAGES) {
    if (path !== PAGES[0]) await switchTo(page, path);
    const { scroll, viewport } = await page.evaluate(() => ({
      scroll: document.documentElement.scrollWidth, viewport: document.documentElement.clientWidth
    }));
    expect(scroll, `${path} at 390 px`).toBeLessThanOrEqual(viewport);
  }
  // The phone menu opens, switches the page and closes behind it.
  await page.locator('#site-header .nav-toggle').click();
  await page.locator('#site-nav a[data-page="fit"]').click();
  await shown(page, 'fit');
  await expect(page.locator('#site-header')).not.toHaveClass(/nav-open/);
  await expect(page.locator('#site-header .nav-toggle')).toHaveAttribute('aria-expanded', 'false');
  await context.close();
});

test('every page opens offline after a visit to any one of them', async ({ page, context }) => {
  // The worker caches every page and the chunks they import; a navigation also
  // prunes stale assets, which must not drop a chunk only another script names.
  await open(page, '/compare.html');
  await page.waitForFunction(() => !!navigator.serviceWorker.controller);
  // A load, not a switch in place: the prune runs on a navigation.
  await page.goto('about:blank');
  await open(page, '/index.html');
  await offlineReady(page);
  await context.setOffline(true);
  try {
    for (const path of ['fit.html', 'engine.html', 'compare.html', 'method.html']) {
      await page.goto(`/${path}`);
      await page.waitForFunction(() => document.body.classList.contains('ready') || document.querySelector('.error'), null, { timeout: 30_000 });
      await expect(page.locator('.error'), `${path} offline`).toHaveCount(0);
    }
    // A link switches pages offline too, to one this tab never opened: its
    // HTML and its code both come from the cache.
    await switchTo(page, 'queries.html');
    await expect(page.locator('.error'), 'queries.html by link, offline').toHaveCount(0);
    expect(await page.locator(DRAWN.queries).count()).toBeGreaterThan(0);
  } finally {
    await context.setOffline(false);
  }
});

test('the separate-session caution matches the run dates in the data', async ({ app: page }) => {
  // SEPARATE_SESSION (constants.js) is a claim about when sizes ran; hold it to
  // the export, and check both scan charts ring exactly those sizes.
  const { SEPARATE_SESSION } = await import('../src/constants.js');
  for (const a of SEPARATE_SESSION) {
    const block = a.kind === 'scan' ? "block = 'clickbench'" : "block IN ('tpcds', 'dsb')";
    const rows = sql(`SELECT compute_label, strftime(min(first_run_at), '%Y-%m-%d') AS d
      FROM ${file('size_ladder.parquet')} WHERE platform = '${a.platform}' AND ${block} AND compute_label <> 'on-demand' GROUP BY 1`);
    for (const r of rows) {
      expect(r.d, `${a.platform} ${r.compute_label} first ran`).toBe(a.labels.includes(r.compute_label) ? a.ranOn : a.restRanOn);
    }
  }
  const rings = SEPARATE_SESSION.filter(a => a.kind === 'scan').reduce((n, a) => n + a.labels.length, 0);
  await open(page, '/compare.html');
  await expect.poll(() => page.locator('#view-scan g[data-mark="dot"] circle[fill="white"], #view-scan g[data-mark="dot"][fill="white"] circle').count(), { timeout: 30_000 }).toBe(rings);
  await open(page, '/engine.html#engine=bigquery');
  await expect(page.locator('#eng-scan-separate')).toContainText('August 28');
});

// ── What the copy claims, held to the data ──────────────────────────────────

test('BigQuery is graded measured exactly where its cost figure is on-demand', async () => {
  // engines.js grades BigQuery's cost by data set: on-demand is the vendor's metered
  // bill (measured), a reservation is allocated. Hold the list to the exports.
  const { BIGQUERY_ON_DEMAND_DATASETS } = await import('../src/engines.js');
  const onDemand = sql(`
    WITH c AS (SELECT scale_factor, query_id, mean_cost_usd FROM ${file('cost_curves.parquet')}
               WHERE platform = 'bigquery' AND block IN ('tpcds', 'dsb')),
         l AS (SELECT scale_factor, query_id, mean_cost_usd FROM ${file('size_ladder.parquet')}
               WHERE platform = 'bigquery' AND block IN ('tpcds', 'dsb') AND compute_label = 'on-demand')
    SELECT CAST(scale_factor AS VARCHAR) AS d FROM c LEFT JOIN l USING (scale_factor, query_id)
    GROUP BY 1 HAVING bool_and(c.mean_cost_usd IS NOT DISTINCT FROM l.mean_cost_usd) ORDER BY 1`).map(r => r.d);
  const scan = sql(`SELECT DISTINCT compute_label FROM ${file('clickbench_summary.parquet')}
    WHERE platform = 'bigquery' AND is_entry_tier`).map(r => r.compute_label);
  expect(scan).toEqual(['on-demand']);
  expect([...onDemand, 'clickbench']).toEqual(BIGQUERY_ON_DEMAND_DATASETS);
});

test('the measurement period the copy states is the span of the runs', () => {
  const MONTH = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  const [{ a, b, y }] = sql(`SELECT month(min(first_run_at)) AS a, month(max(last_run_at)) AS b, year(max(last_run_at)) AS y
    FROM ${file('size_ladder.parquet')}`);
  const period = `${MONTH[Number(a) - 1]} to ${MONTH[Number(b) - 1]} ${Number(y)}`;
  const between = `between ${MONTH[Number(a) - 1]} and ${MONTH[Number(b) - 1]} ${Number(y)}`;
  const stated = new RegExp(`(?:${MONTH.join('|')}) (?:to|and) (?:early |late |mid-)?(?:${MONTH.join('|')}) 20\\d\\d`, 'g');
  for (const f of ['index.html', 'method.html', 'src/pages/engine.js', 'src/pages/method.js', 'src/site/chart-tools.js', 'src/site/glossary.js', 'src/site/layout.js']) {
    const text = readFileSync(join(here, '..', f), 'utf8');
    const found = [...text.matchAll(stated)].map(m => m[0]);
    expect(found.length, `${f} states a measurement period`).toBeGreaterThan(0);
    for (const p of found) expect(`between ${p}`.endsWith(between) || p === period, `${f}: "${p}", the runs span ${period}`).toBe(true);
  }
});

test('the scan-suite ceiling and the entry tiers the copy names match the data', async ({ app: page }) => {
  const { SCAN_SUITE_QUERIES } = await import('../src/constants.js');
  const { TERMS } = await import('../src/site/glossary.js');
  const { ENGINE_LABEL } = await import('../src/engines.js');
  // The suite size, and the scan grid's top level: each pass queues the suite
  // once, so 50 sessions ran all of it at once, and the pages show that level
  // as 43. (A cell's runs can exceed 43 where two passes are pooled.)
  expect(scalar(`SELECT count(*) FROM ${file('query_catalog.parquet')} WHERE block = 'clickbench'`)).toBe(SCAN_SUITE_QUERIES);
  expect(scalar(`SELECT max(concurrency) FROM ${file('concurrency_grid.parquet')} WHERE workload = 'clickbench'`)).toBe(50);
  // The entry-tier definition lists each engine's tier as the data flags it.
  const tiers = sql(`SELECT DISTINCT platform, compute_label FROM ${file('clickbench_summary.parquet')} WHERE is_entry_tier`);
  expect(tiers.length).toBe(7);
  for (const t of tiers) expect(TERMS['entry-tier'].def).toContain(`${ENGINE_LABEL[t.platform]} ${t.compute_label.replace(/ \(Gen2\)$/, '')}`);
  // ...and says which four are not the cheapest scan size measured.
  const notSmallest = sql(`
    WITH cheapest AS (
      SELECT l.platform, arg_min(l.compute_label, r.usd_per_hour) AS label
      FROM (SELECT DISTINCT platform, compute_label FROM ${file('size_ladder.parquet')} WHERE block = 'clickbench') l
      JOIN ${file('rate_card.parquet')} r USING (platform, compute_label) WHERE r.usd_per_hour IS NOT NULL GROUP BY 1)
    SELECT t.platform FROM (SELECT DISTINCT platform, compute_label FROM ${file('clickbench_summary.parquet')} WHERE is_entry_tier) t
    JOIN cheapest c USING (platform) WHERE t.compute_label <> c.label ORDER BY 1`).map(r => ENGINE_LABEL[r.platform]);
  expect(TERMS['entry-tier'].def).toContain(`For ${notSmallest.slice(0, -1).join(', ')} and ${notSmallest.at(-1)} it is not the smallest`);
  // The Fit page says so on the scan example.
  await open(page, '/fit.html#kind=scan&people=100&sla=5000');
  await expect(page.locator('.fit-scan-cap')).toContainText(`the most the study ran at once on scan queries is ${SCAN_SUITE_QUERIES}`);
  await expect(page.locator('#fit-results h2')).toContainText(`served ${SCAN_SUITE_QUERIES} people at once`);
});

test('the Overview headline figures follow the data', async ({ app: page }) => {
  await open(page, '/index.html');
  // Finding 3's headline: an engine that finished under 90% of the join suite (the
  // report's floor) is not ranked on joins, and if it places in the top three on
  // scans it is the headline; otherwise the biggest mover between the two ranks.
  const joinAll = sql(`SELECT platform, exp(avg(ln(median_server_ms)) FILTER (WHERE median_server_ms > 0)) AS ms,
      count(median_server_ms) AS done, count(*) AS n
    FROM ${file('cost_curves.parquet')} WHERE scale_factor = 100 AND block IN ('tpcds', 'dsb') GROUP BY 1 ORDER BY ms`);
  const unfinished = joinAll.filter(r => Number(r.done) < 0.9 * Number(r.n)).map(r => r.platform);
  const join = joinAll.map(r => r.platform).filter(p => !unfinished.includes(p));
  const scan = sql(`
    WITH t AS (SELECT l.platform, l.compute_label, exp(avg(ln(median_server_ms)) FILTER (WHERE median_server_ms > 0)) AS ms
      FROM ${file('size_ladder.parquet')} l JOIN ${file('rate_card.parquet')} r USING (platform, compute_label)
      WHERE l.block = 'clickbench' AND r.usd_per_hour IS NOT NULL GROUP BY 1, 2)
    SELECT platform, min(ms) AS ms FROM t GROUP BY 1 ORDER BY ms`).map(r => r.platform);
  const scanRanked = scan.filter(p => join.includes(p));
  const moves = join.map(p => ({ p, from: join.indexOf(p) + 1, to: scanRanked.indexOf(p) + 1 }))
    .sort((a, b) => Math.abs(b.from - b.to) - Math.abs(a.from - a.to));
  const ORD = ['first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh'];
  const place = n => (n === 7 ? 'last' : ORD[n - 1]);
  const { ENGINE_LABEL } = await import('../src/engines.js');
  const climber = unfinished.map(p => ({ p, to: scan.indexOf(p) + 1 })).filter(c => c.to && c.to <= 3).sort((a, b) => a.to - b.to)[0];
  await expect(page.locator('#f3-title')).toContainText(climber
    ? `${ENGINE_LABEL[climber.p]} did not finish the join queries at 100 GB but placed ${place(climber.to)} on scan queries`
    : `Scan queries move ${ENGINE_LABEL[moves[0].p]} from ${place(moves[0].from)} to ${place(moves[0].to)}`);
  // Skew at 1 TB is the report's clean-pair median (no *_returns pairs).
  const slower = sql(`
    SELECT s.platform, median(s.dsb_server_ms / s.tpcds_server_ms) AS r
    FROM ${file('skew_impact.parquet')} s
    JOIN ${file('query_catalog.parquet')} q ON q.block = 'tpcds' AND q.number = s.qnum
    WHERE s.scale_factor = 1000 AND NOT q.returns_pair GROUP BY 1 HAVING r > 1`);
  for (const r of slower) {
    await expect(page.locator('#further-body')).toContainText(`took ${Number(r.r).toFixed(1)} times as long`);
  }
  // Row-count agreement is ACROSS engines, per query and data volume.
  const [a] = sql(`SELECT count(*) FILTER (WHERE n = 1) AS agreed, count(*) AS compared
    FROM (SELECT count(DISTINCT row_count) AS n FROM ${file('row_count_agreement.parquet')} GROUP BY query_id, scale_factor)`);
  await expect(page.locator('#agreement')).toContainText(`${Number(a.agreed).toLocaleString('en-US')} of ${Number(a.compared).toLocaleString('en-US')} join-query results`);
  // No link to the report until it is published; once it is, it is the page's
  // primary action and the header's call to action, and the footer links it.
  // Each link carries the campaign tags and names its own placement.
  const { REPORT_LIVE, REPORT_URL, reportHref } = await import('../src/site/report.js');
  if (REPORT_LIVE === false) {
    await expect(page.locator(`a[href^="${REPORT_URL}"]`)).toHaveCount(0);
    await expect(page.locator('#report-link')).toHaveCount(0);
    await expect(page.locator('#site-header .site-cta')).toHaveCount(0);
  } else {
    await expect(page.locator('#report-link')).toHaveAttribute('href', reportHref('home'));
    await expect(page.locator('#report-link')).toHaveClass(/btn-primary/);
    await expect(page.locator('#site-header .site-cta')).toHaveAttribute('href', reportHref('header'));
    await expect(page.locator(`footer a[href="${reportHref('footer')}"]`)).toHaveCount(1);
    const tags = new URL(reportHref('header')).searchParams;
    expect([tags.get('utm_source'), tags.get('utm_content')]).toEqual(['benchmark-explorer', 'header']);
  }
});

test('ClickHouse join-suite cost is shown at its list-rate price', async ({ app: page }) => {
  // Every ClickHouse size's stored cost implies its list rate, so the site
  // shows it like every other engine's (no "$0", nothing withheld).
  const cost = scalar(`SELECT avg(mean_cost_usd) FROM ${file('cost_curves.parquet')} WHERE platform = 'clickhouse' AND scale_factor = 100`);
  expect(cost).toBeGreaterThan(0);
  await open(page, '/compare.html#sf=100&engines=ClickHouse');
  await expect(page.locator('#syn-glance p')).toContainText(`at $${Number(cost.toFixed(5))} per query`);
});

test('on a phone the tables show their numbers and Compare works by touch', async ({ browser }) => {
  test.setTimeout(240_000);
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const page = await context.newPage();
  // Every data table's first number sits inside the screen (a card per row).
  for (const [path, sel] of [['/index.html', '#f3-body'], ['/index.html', '#f1-body'], ['/fit.html', '#fit-results-body'],
    ['/fit.html', '#fit-speed-body'], ['/engine.html#engine=snowflake', '#eng-ladder-table']]) {
    await open(page, path);
    const cell = page.locator(`${sel} tbody tr:not([hidden]) td.num`).first();
    await cell.scrollIntoViewIfNeeded();
    const box = await cell.boundingBox();
    expect(box, `${path} ${sel}: a number is drawn`).not.toBeNull();
    expect(box.x + box.width, `${path} ${sel}: its first number is on screen`).toBeLessThanOrEqual(390);
  }
  // The Compare filter bar does not fold before the reader reaches it, and a
  // second tap on an engine adds it rather than replacing the first.
  await open(page, '/compare.html');
  await expect(page.locator('#controls')).not.toHaveClass(/compact/);
  await page.locator('#controls').scrollIntoViewIfNeeded();
  const swatch = name => page.locator('#controls .legend [class*="-swatches"] > div', { hasText: name });
  await swatch('Snowflake').tap();
  await expect(page.locator('#controls .ctl-summary-text')).toContainText('Snowflake');
  await swatch('Databricks').tap();
  await expect(page.locator('#controls .ctl-summary-text')).toContainText('Snowflake, Databricks');
  // A tapped example brings its results into view.
  await open(page, '/fit.html');
  await page.locator('[data-preset="product"]').tap();
  await expect.poll(() => page.locator('#fit-results').evaluate(el => el.getBoundingClientRect().top), { timeout: 5_000 })
    .toBeLessThan(844 * 0.6);
  await context.close();
});
