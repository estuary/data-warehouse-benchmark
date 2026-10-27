/**
 * Browser tests for the built explorer.
 *
 * The invariant these guard: what the page draws is what the SQL says. Every
 * expected count and ordering below is computed from the same parquet files
 * the page loads, with the same filters, by a DuckDB running in Node, and then
 * compared with the rendered DOM. A view that silently drops rows, a Selection
 * that stops re-filtering, or a bundle that reaches for a CDN fails here.
 */
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { CLICKBENCH, COMPARE_PRICE_LABELS, COMPUTE_PRICE_RANGE, DEFAULT_SCALE, DEFAULT_SLA_MS, MEASURED_LEVELS, SCAN_LEVELS, SCAN_SUITE_QUERIES, WARM_MEASURED_RUNS } from '../src/constants.js';
import { roundDollars, roundTickSet } from '../src/format.js';
import { expect, expectNoBrokenValues, test, visit } from './app.js';
import { offlineReady, settled } from './wait.js';

const require = createRequire(import.meta.url);
const duckdb = require('@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs');

const here = dirname(fileURLToPath(import.meta.url));
const DATA = join(here, '..', 'public', 'data');
const ORIGIN = `http://localhost:${process.env.VIZ_PORT || 4173}`;

/** Same expression as src/views/concurrency.js and the Python reducer. */
const useful = sla =>
  `coalesce(max(concurrency) FILTER (WHERE p50_ms <= ${sla} AND shed_pct < sla_max_shed_pct), 0)`;

/** The published grid, as the page's `concurrency` view filters it, both workloads. */
const GRID_ALL = `(SELECT * FROM read_parquet('${join(DATA, 'concurrency_grid.parquet')}')
               WHERE publishable AND usd_per_hour IS NOT NULL)`;
/** The TPC-DS + DSB cells: what the scale-factor data sets show. */
const GRID = `(SELECT * FROM ${GRID_ALL} WHERE workload = 'decision_support')`;
/** The ClickBench cells: what the ClickBench data set shows. */
const GRID_CB = `(SELECT * FROM ${GRID_ALL} WHERE workload = 'clickbench')`;
const CLICKBENCH_SUMMARY = `read_parquet('${join(DATA, 'clickbench_summary.parquet')}')`;

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

/** Run SQL in Node's DuckDB and return plain rows. */
function sql(query) {
  return con.query(query).toArray().map(r => r.toJSON());
}
const scalar = query => Number(Object.values(sql(query)[0])[0]);

/** Open the Compare page (see `visit` in tests/app.js) and wait for every view to render. */
async function openExplorer(page) {
  const errors = [];
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
  page.on('console', m => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  const requests = [];
  page.on('request', r => requests.push(r.url()));

  await visit(page, 'compare.html');
  await expect(page.locator('.error')).toHaveCount(0);
  // The frontier's table is collapsed; open it so its rows render and can be counted.
  await page.locator('#sec-frontier details.table-details > summary').click();
  await page.waitForFunction(() =>
    [...document.querySelectorAll('[id^=view-]')].every(v => v.querySelector('svg, table tbody tr')));
  await settled(page);
  return { errors, requests };
}

const count = (page, selector) => page.locator(selector).count();

const setSelect = (page, index, value) => page.evaluate(([i, v]) => {
  const s = document.querySelectorAll('#controls select')[i];
  s.value = v;
  s.dispatchEvent(new Event('input', { bubbles: true }));
  s.dispatchEvent(new Event('change', { bubbles: true }));
}, [index, value]);

const setSlider = (page, value) => page.evaluate(v => {
  const s = document.querySelector('#controls input[type=range]');
  s.value = String(v);
  s.dispatchEvent(new Event('input', { bubbles: true }));
}, value);

/** Table body as an array of cell-text arrays. */
const tableRows = page => page.evaluate(() =>
  [...document.querySelectorAll('#view-configs tbody tr')].map(tr => [...tr.children].map(td => td.textContent.trim())));

test('starts clean: no errors, no external hosts, DuckDB owns the Parquet reader', async ({ page }) => {
  const { errors, requests } = await openExplorer(page);
  expect(errors).toEqual([]);
  const external = requests.filter(u => !u.startsWith(ORIGIN) && !u.startsWith('blob:'));
  expect(external, 'every request must stay on the page origin').toEqual([]);
  expect(requests.some(u => u.endsWith('/duckdb/parquet.duckdb_extension.wasm')),
    'the Parquet extension must be loaded from our own copy').toBe(true);
});

test('each view draws exactly the rows its SQL returns', async ({ app: page }) => {
  await openExplorer(page);
  const sf = DEFAULT_SCALE;

  expect(await count(page, '#view-cfrontier g[data-mark="dot"] > *'))
    .toBe(scalar(`SELECT count(*) FROM (SELECT DISTINCT platform, compute_label FROM ${GRID} WHERE scale_factor = ${sf})`));

  // Plot puts a constant fill on the per-facet <g>, so the white rings of the
  // shed mark are told apart from the filled points by their parent group.
  expect(await count(page, '#view-degradation g[data-mark="dot"] > g:not([fill="white"]) circle'))
    .toBe(scalar(`SELECT count(*) FROM ${GRID} WHERE scale_factor = ${sf}`));
  expect(await count(page, '#view-degradation g[data-mark="dot"] > g[fill="white"] circle'))
    .toBe(scalar(`SELECT count(*) FROM ${GRID} WHERE scale_factor = ${sf} AND shed_pct >= sla_max_shed_pct`));

  expect(await count(page, '#view-shed g[data-mark="dot"] circle'))
    .toBe(scalar(`SELECT count(*) FROM ${GRID} WHERE scale_factor = ${sf}`));
  expect(await count(page, '#view-shed g[data-mark="rule"] line'), 'the 5% threshold rule repeats in every panel')
    .toBe(scalar(`SELECT count(DISTINCT platform) FROM ${GRID} WHERE scale_factor = ${sf}`));

  expect(await count(page, '#view-skewload g[data-mark="dot"] circle'))
    .toBe(scalar(`SELECT count(*) FROM read_parquet('${join(DATA, 'skew_load.parquet')}') s
                  JOIN ${GRID} g USING (platform, scale_factor, compute_label, concurrency)
                  WHERE s.scale_factor = ${sf}`));

  expect(await count(page, '#view-patternload g[data-mark="dot"] circle'))
    .toBe(scalar(`SELECT count(*) FROM (SELECT DISTINCT platform, concurrency, pattern
                  FROM read_parquet('${join(DATA, 'pattern_load.parquet')}') WHERE scale_factor = ${sf})`));

  expect(await count(page, '#view-latency g[data-mark="dot"] circle'))
    .toBe(scalar(`SELECT count(*) FROM (SELECT DISTINCT platform, block, scale_factor
                  FROM read_parquet('${join(DATA, 'cost_curves.parquet')}'))`));

  // Quadrant dots are shaped by cost confidence (circle / square / triangle),
  // so count marks of any shape.
  expect(await count(page, '#view-frontier g[data-mark="dot"] > *'))
    .toBe(scalar(`SELECT count(DISTINCT platform) FROM read_parquet('${join(DATA, 'cost_curves.parquet')}') WHERE scale_factor = ${sf}`));

  // At a glance: one bar per engine, from the skew export and the warm curves.
  expect(await count(page, '#view-skewtax g[data-mark="bar"] rect'))
    .toBe(scalar(`SELECT count(DISTINCT platform) FROM read_parquet('${join(DATA, 'skew_impact.parquet')}') WHERE scale_factor = ${sf}`));
  expect(await count(page, '#view-reliability g[data-mark="bar"] rect'))
    .toBe(scalar(`SELECT count(DISTINCT platform) FROM read_parquet('${join(DATA, 'cost_curves.parquet')}') WHERE scale_factor = ${sf}`));
  // The reliability bar divides failures by WARM_MEASURED_RUNS; no query may exceed it.
  expect(scalar(`SELECT max(failures) FROM read_parquet('${join(DATA, 'cost_curves.parquet')}')`))
    .toBeLessThanOrEqual(WARM_MEASURED_RUNS);

  expect(await count(page, '#view-clickbench g[data-mark="dot"] circle'))
    .toBe(scalar(`SELECT count(*) FROM read_parquet('${join(DATA, 'clickbench_summary.parquet')}') WHERE is_entry_tier`));
});

test('the compute price ramp brackets every size in the grid', async ({ app: page }) => {
  // The ladders colour by list price on a pinned log domain; a size outside it
  // would be clamped to an end colour and read as a different price.
  await openExplorer(page);
  const [lo, hi] = Object.values(sql(`SELECT min(usd_per_hour), max(usd_per_hour) FROM ${GRID}`)[0]).map(Number);
  const legend = await page.evaluate(() => [...document.querySelectorAll('#legend-degradation text')].map(t => t.textContent));
  expect(legend.length, 'ramp legend rendered').toBeGreaterThan(0);
  expect(COMPUTE_PRICE_RANGE[0]).toBeLessThanOrEqual(lo);
  expect(COMPUTE_PRICE_RANGE[1]).toBeGreaterThanOrEqual(hi);
});

test('the frontier axis is exactly the ladder the campaign ran', async ({ app: page }) => {
  // MEASURED_LEVELS is hard-coded in the explorer on the argument that useful
  // concurrency can take no other value; the parquet is the authority.
  await openExplorer(page);
  const ticks = () => page.evaluate(() => [...document.querySelectorAll('#view-cfrontier g[data-mark="x-axis tick label"] text')]
    .map(t => t.textContent).filter(Boolean).map(Number));
  const levels = sql(`SELECT DISTINCT concurrency FROM ${GRID} ORDER BY 1`).map(r => Number(r.concurrency));
  expect(await ticks()).toEqual([0, ...levels]);
  expect(MEASURED_LEVELS).toEqual([0, ...levels]);
  // Scan queries share the axis: the export stops at 50 sessions, which ran the
  // whole 43-query suite at once, so that rung reads 43 and there is no 100.
  const scanLevels = sql(`SELECT DISTINCT concurrency FROM ${GRID_CB} ORDER BY 1`).map(r => Number(r.concurrency));
  expect(scanLevels).toEqual([1, 5, 10, 20, 50]);
  expect(SCAN_LEVELS).toEqual([0, ...scanLevels.slice(0, -1), SCAN_SUITE_QUERIES]);
  await page.goto(`/compare.html#sf=${CLICKBENCH}`);
  await page.reload();
  await page.waitForFunction(() => document.body.classList.contains('ready'));
  await expect.poll(ticks).toEqual(SCAN_LEVELS);
});

test('the URL hash carries the controls both ways', async ({ app: page }) => {
  await visit(page, 'compare.html#sf=1000&sla=2000&axis=linear&engines=Snowflake,MotherDuck&config=motherduck:Mega');
  expect(await page.evaluate(() => document.querySelectorAll('#controls select')[0].value)).toBe('1000');
  expect(await page.evaluate(() => document.querySelector('#controls input[type=range]').value)).toBe('2000');
  expect(await page.evaluate(() => document.querySelectorAll('#controls select')[1].value)).toBe('linear');
  // Polled: the linked engines are restored one legend press at a time.
  await expect.poll(() => count(page, '#view-cfrontier g[data-mark="dot"] > *'), { timeout: 15_000 })
    .toBe(scalar(`SELECT count(*) FROM (SELECT DISTINCT platform, compute_label FROM ${GRID}
    WHERE scale_factor = 1000 AND platform IN ('snowflake', 'motherduck'))`));
  await page.locator('#sec-frontier details.table-details > summary').click();
  await expect(page.locator('#view-configs tr.selected')).toHaveAttribute('data-compute', 'Mega');
  // And back: a change on the page lands in the address bar.
  await setSlider(page, 3000);
  await expect(page).toHaveURL(/sla=3000/);
  expect(await page.evaluate(() => location.hash)).toContain('sla=3000');
  expect(await page.evaluate(() => location.hash)).toContain('engines=Snowflake%2CMotherDuck');
});

test('ClickBench is a data set beside the scale factors', async ({ app: page }) => {
  await openExplorer(page);
  const visible = sel => page.locator(sel).first().isVisible();
  // Before: the pair-only views are on the page.
  expect(await visible('#sec-patterns')).toBe(true);
  expect(await visible('#view-skewtax')).toBe(true);

  await setSelect(page, 0, CLICKBENCH);
  await settled(page);
  expect(await page.evaluate(() => document.body.classList.contains('clickbench'))).toBe(true);
  expect(await page.evaluate(() => location.hash)).toContain(`sf=${CLICKBENCH}`);
  // The frontier's rungs follow the menu: scan queries end at 43, join queries at 100.
  const rungs = () => page.evaluate(() => [...document.querySelectorAll('#view-cfrontier g[data-mark="x-axis tick label"] text')].map(t => Number(t.textContent)));
  await expect.poll(rungs).toEqual(SCAN_LEVELS);
  await setSelect(page, 0, String(DEFAULT_SCALE));
  await settled(page);
  await expect.poll(rungs).toEqual(MEASURED_LEVELS);
  await setSelect(page, 0, CLICKBENCH);
  await settled(page);

  // The concurrency views are cut from the ClickBench cells of the same grid.
  const dots = await count(page, '#view-cfrontier g[data-mark="dot"] > *');
  expect(dots).toBe(scalar(`SELECT count(*) FROM (SELECT DISTINCT platform, compute_label FROM ${GRID_CB})`));
  expect((await tableRows(page)).length).toBe(dots);
  expect(await count(page, '#view-degradation g[data-mark="dot"] > g:not([fill="white"]) circle'))
    .toBe(scalar(`SELECT count(*) FROM ${GRID_CB}`));
  expect(await count(page, '#view-shed g[data-mark="dot"] circle')).toBe(scalar(`SELECT count(*) FROM ${GRID_CB}`));

  // At a glance: one point and one bar per engine at its entry tier.
  const engines = scalar(`SELECT count(DISTINCT platform) FROM ${CLICKBENCH_SUMMARY} WHERE is_entry_tier`);
  expect(await count(page, '#view-frontier g[data-mark="dot"] > *')).toBe(engines);
  expect(await count(page, '#view-reliability g[data-mark="bar"] rect')).toBe(engines);

  // What needs the TPC-DS/DSB pair is hidden, and the nav follows.
  for (const sel of ['#sec-patterns', '#sec-skew', '#view-skewtax', '#view-sweep']) {
    expect(await visible(sel), `${sel} hidden for ClickBench`).toBe(false);
  }
  expect(await page.getByRole('button', { name: /Query patterns/ }).isVisible()).toBe(false);
  await expectNoBrokenValues(page, 'Compare on ClickBench');
  expect(await count(page, '#view-patternload g[data-mark="dot"] circle'), 'no pattern rows for ClickBench').toBe(0);

  // And back.
  await setSelect(page, 0, String(DEFAULT_SCALE));
  await settled(page);
  expect(await page.evaluate(() => document.body.classList.contains('clickbench'))).toBe(false);
  expect(await visible('#sec-patterns')).toBe(true);
  expect(await count(page, '#view-cfrontier g[data-mark="dot"] > *'))
    .toBe(scalar(`SELECT count(*) FROM (SELECT DISTINCT platform, compute_label FROM ${GRID} WHERE scale_factor = ${DEFAULT_SCALE})`));

  // A link to the ClickBench view opens in it.
  await visit(page, `compare.html#sf=${CLICKBENCH}`);
  expect(await page.evaluate(() => document.querySelectorAll('#controls select')[0].value)).toBe(CLICKBENCH);
  expect(await page.evaluate(() => document.body.classList.contains('clickbench'))).toBe(true);
});

test('bottom lines are computed from the data and follow the SLA', async ({ app: page }) => {
  await openExplorer(page);
  // Every box gets a real sentence.
  await page.waitForFunction(() =>
    [...document.querySelectorAll('.synth p')].every(p => p.textContent.trim().length > 20));
  const boxes = await count(page, '.synth p');
  expect(boxes).toBe(9);
  const before = await page.locator('#syn-frontier p').textContent();
  expect(before).toContain(`${DEFAULT_SLA_MS / 1000} s latency target`);
  // The frontier's leader is the same aggregate the table shows: the row with
  // the most sessions (ties to the cheaper size) must be named.
  const top = sql(`
    SELECT platform, compute_label, ${useful(DEFAULT_SLA_MS)} AS sessions, any_value(usd_per_hour) AS usd
    FROM ${GRID} WHERE scale_factor = ${DEFAULT_SCALE}
    GROUP BY platform, compute_label ORDER BY sessions DESC, usd ASC LIMIT 1`)[0];
  expect(before).toContain(String(top.compute_label));
  expect(before).toContain(`${Number(top.sessions)},`);
  // Re-targets with the slider, like the chart it summarises.
  await setSlider(page, 1000);
  await page.waitForFunction(() => document.querySelector('#syn-frontier p').textContent.includes('1 s latency target'));
});

test('ladder lines connect their points in session order', async ({ app: page }) => {
  // Mosaic adds an ORDER BY on x only for lineY/lineX. A plain `line` connects
  // points in GROUP BY result order, which draws loops in these views.
  await openExplorer(page);
  const notMonotone = await page.evaluate(() => {
    const bad = [];
    for (const view of ['view-degradation', 'view-shed', 'view-skewload', 'view-patternload']) {
      for (const path of document.querySelectorAll(`#${view} g[data-mark="line"] path`)) {
        const xs = [];
        for (const m of (path.getAttribute('d') || '').matchAll(/([MLC])([^MLC]*)/g)) {
          const nums = m[2].trim().split(/[\s,]+/).map(Number);
          xs.push(m[1] === 'C' ? nums[4] : nums[0]);
        }
        if (xs.some((x, i) => i > 0 && x < xs[i - 1] - 1e-6)) bad.push(view);
      }
    }
    return bad;
  });
  expect(notMonotone).toEqual([]);
});

test('the frontier table reproduces the reducer at the current SLA and re-targets with the slider', async ({ app: page }) => {
  await openExplorer(page);
  for (const sla of [DEFAULT_SLA_MS, 1000, 20000]) {
    await setSlider(page, sla);
    await settled(page);
    const expected = sql(`
      SELECT platform, compute_label, ${useful(sla)} AS sessions
      FROM ${GRID} WHERE scale_factor = ${DEFAULT_SCALE}
      GROUP BY platform, compute_label ORDER BY 1, 2`);
    const rows = await tableRows(page);
    expect(rows.length, `rows at SLA ${sla}`).toBe(expected.length);
    const zero = expected.filter(r => Number(r.sessions) === 0).length;
    expect(await count(page, '#view-configs tr.zero'), `zero-session rows at SLA ${sla}`).toBe(zero);
    // Sessions column, compared as a multiset (the table's default order is by
    // engine label, the query's by platform id).
    const drawn = rows.map(r => Number(r[3])).sort((a, b) => a - b);
    const truth = expected.map(r => Number(r.sessions)).sort((a, b) => a - b);
    expect(drawn, `sessions at SLA ${sla}`).toEqual(truth);
  }
});

test('the scale menu and the engine legend re-filter through the coordinator', async ({ app: page }) => {
  await openExplorer(page);
  const dots = () => count(page, '#view-cfrontier g[data-mark="dot"] > *');

  await setSelect(page, 0, '1000');
  await settled(page);
  expect(await dots()).toBe(scalar(`SELECT count(*) FROM (SELECT DISTINCT platform, compute_label FROM ${GRID} WHERE scale_factor = 1000)`));
  expect((await tableRows(page)).length).toBe(await dots());
  await setSelect(page, 0, String(DEFAULT_SCALE));
  await settled(page);

  // The filter bar folds once it sticks to the top of the window; the engine
  // swatches are in its full form, at the top of the page.
  await page.evaluate(() => window.scrollTo(0, 0));
  await expect(page.locator('#controls')).not.toHaveClass(/compact/);
  const swatch = page.locator('#controls .legend [role=button]').first();
  await expect(swatch).toHaveAttribute('aria-label', 'Toggle Snowflake');
  await swatch.focus();
  await page.keyboard.press('Enter');
  await settled(page);
  expect(await dots()).toBe(scalar(`SELECT count(DISTINCT compute_label) FROM ${GRID} WHERE scale_factor = ${DEFAULT_SCALE} AND platform = 'snowflake'`));
  expect(await count(page, '#view-latency g[data-mark="dot"] circle')).toBe(
    scalar(`SELECT count(*) FROM (SELECT DISTINCT block, scale_factor FROM read_parquet('${join(DATA, 'cost_curves.parquet')}') WHERE platform = 'snowflake')`));
  // Focus survives Mosaic rebuilding the legend, so a second press toggles back.
  await expect(page.locator(':focus')).toHaveAttribute('aria-label', 'Toggle Snowflake');
  await page.keyboard.press('Enter');
  await settled(page);
  expect(await dots()).toBe(scalar(`SELECT count(*) FROM (SELECT DISTINCT platform, compute_label FROM ${GRID} WHERE scale_factor = ${DEFAULT_SCALE})`));
});

test('table sort is an ORDER BY the database runs', async ({ app: page }) => {
  await openExplorer(page);
  // Exact match: a second heading starts with the same words, and the sorted
  // heading grows an arrow.
  const heading = page.getByRole('columnheader', { name: /^USD per hour( [▴▾])?$/ });
  await heading.click();
  await settled(page);
  const asc = (await tableRows(page)).map(r => r[2]);
  const truth = sql(`SELECT DISTINCT usd_per_hour FROM ${GRID} WHERE scale_factor = ${DEFAULT_SCALE} ORDER BY 1`)
    .map(r => `$${Number(r.usd_per_hour).toFixed(2)}`);
  expect([...new Set(asc)]).toEqual(truth);
  await expect(heading).toHaveAttribute('aria-sort', 'ascending');
  await heading.click();
  await settled(page);
  expect([...new Set((await tableRows(page)).map(r => r[2]))]).toEqual([...truth].reverse());
});

test('a table row drives the scale sweep for that configuration', async ({ app: page }) => {
  await openExplorer(page);
  const sweepDots = () => count(page, '#view-sweep g[data-mark="dot"] circle');
  // Seeded selection: Snowflake Medium (Gen2), which ran at every scale.
  await expect(page.locator('#view-configs tr.selected')).toHaveCount(1);
  expect(await sweepDots()).toBe(scalar(`SELECT count(DISTINCT scale_factor) FROM ${GRID}
    WHERE platform = 'snowflake' AND compute_label = 'Medium (Gen2)'`));
  // Click a Fabric row; F2 exists at SF1, SF10 and SF100 only.
  const row = page.locator('#view-configs tbody tr[data-platform="fabric"][data-compute="F2"]');
  await row.click();
  await settled(page);
  await expect(row).toHaveClass(/selected/);
  await expect(page.locator('#view-configs tr.selected')).toHaveCount(1);
  expect(await sweepDots()).toBe(scalar(`SELECT count(DISTINCT scale_factor) FROM ${GRID}
    WHERE platform = 'fabric' AND compute_label = 'F2'`));
  // The sweep spans scales: the scale menu must not narrow it.
  await setSelect(page, 0, '1000');
  await settled(page);
  expect(await sweepDots()).toBe(scalar(`SELECT count(DISTINCT scale_factor) FROM ${GRID}
    WHERE platform = 'fabric' AND compute_label = 'F2'`));
});

test('charts drawing above the reader do not push the page on a browser without scroll anchoring', async ({ browser }) => {
  // Safari has no CSS scroll anchoring: while Compare loads, each chart that
  // draws above the reader pushes what is on screen down, which reads as being
  // kicked back up while scrolling down. site/scroll-anchor.js holds the content
  // in place. Turning anchoring off here stands in for Safari; the content at
  // the anchor point must never move down.
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  await context.addInitScript(() => {
    const s = document.createElement('style');
    s.textContent = 'html, body, * { overflow-anchor: none !important; }';
    (document.head || document.documentElement).append(s);
  });
  // Served locally, every chart has drawn before the first scroll; stagger the
  // data files as a real network does, so charts draw while the reader scrolls.
  let n = 0;
  await context.route('**/*.parquet', async route => {
    await new Promise(r => setTimeout(r, 800 + 400 * n++));
    await route.continue();
  });
  const page = await context.newPage();
  await page.goto('/compare.html#sf=100', { waitUntil: 'domcontentloaded' });
  await page.mouse.move(700, 500);
  const pushed = [];
  let ready = false;
  for (let i = 0; i < 150 && !(ready && i > 40); i++) {
    // A step taken at the very bottom of the half-loaded page is not counted:
    // the wheel cannot move there, so it does not show what a reader mid-page sees.
    const before = await page.evaluate(() => {
      window.__probe = document.elementFromPoint(innerWidth / 2, innerHeight * 0.4);
      const atBottom = scrollY >= document.documentElement.scrollHeight - innerHeight - 1;
      return atBottom ? null : window.__probe?.getBoundingClientRect().top ?? null;
    });
    await page.mouse.wheel(0, 100);
    await page.waitForTimeout(80);
    const [after, done] = await page.evaluate(() => [
      window.__probe?.isConnected ? window.__probe.getBoundingClientRect().top : null,
      document.body.classList.contains('ready')
    ]);
    ready = done;
    if (before != null && after != null && after - before > 20) pushed.push(Math.round(after - before));
  }
  await context.close();
  expect(ready, 'the page finished loading while it was scrolled').toBe(true);
  expect(pushed, 'px the on-screen content was pushed down').toEqual([]);
});

test('the filter bar folds without moving the page under the reader', async ({ app: page }) => {
  // A fold that changes the page's height lets scroll anchoring pull the page
  // back, unfold the bar, and keep the page jumping back as the reader scrolls
  // down. Scrolling in steps must move one way only.
  await page.setViewportSize({ width: 1280, height: 800 });
  await visit(page, 'compare.html');
  await page.mouse.move(640, 400);
  const read = () => page.evaluate(() => ({ y: scrollY, compact: document.getElementById('controls').classList.contains('compact') }));
  const trace = async dy => {
    const steps = [];
    for (let i = 0; i < 30; i++) {
      await page.mouse.wheel(0, dy);
      // Let the scroll, the observers and any fold run: two frames.
      await page.evaluate(() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r))));
      steps.push(await read());
    }
    return steps;
  };
  const down = await trace(40);
  expect(down.some(s => s.compact), 'the bar folded on the way down').toBe(true);
  for (let i = 1; i < down.length; i++) expect(down[i].y, `step ${i} down`).toBeGreaterThanOrEqual(down[i - 1].y);
  expect(down.at(-1).compact).toBe(true);
  const up = await trace(-40);
  expect(up.at(-1).compact, 'the bar unfolded back at the top').toBe(false);
  for (let i = 1; i < up.length; i++) expect(up[i].y, `step ${i} up`).toBeLessThanOrEqual(up[i - 1].y);
});

test('the at-a-glance charts read at their true scale and keep every label clear', async ({ page }) => {
  await page.goto('/compare.html#sf=10');
  await page.waitForFunction(() => document.body.classList.contains('ready'));
  // The not-completed share runs on a full 0 to 100% axis, so 1% reads as small.
  await expect.poll(() => page.evaluate(() =>
    [...document.querySelectorAll('#view-reliability g[data-mark="x-axis tick label"] text')].map(t => t.textContent)))
    .toEqual(['0%', '25%', '50%', '75%', '100%']);
  // No engine label on the cost-and-speed scatter sits under a corner caption.
  const overlaps = await page.evaluate(() => {
    const boxes = [...document.querySelectorAll('#view-frontier g[data-mark="text"] text')].map(t => [t.textContent, t.getBoundingClientRect()]);
    const corners = [...document.querySelectorAll('#view-frontier .q-tl, #view-frontier .q-tr, #view-frontier .q-bl, #view-frontier .q-br')]
      .map(c => c.getBoundingClientRect()).filter(r => r.width);
    const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    return boxes.filter(([, b]) => corners.some(c => hit(b, c))).map(([name]) => name);
  });
  expect(overlaps).toEqual([]);
  // Price labels are round steps inside the 1 TB price range, and only those:
  // a label outside the range sits outside its panel (a stray "$1" beside the
  // previous panel's "$10" reads as "$100"). Left to Plot, the row gets one
  // label, "$10". No size name may sit on a panel title.
  await page.goto('/compare.html#sf=1000');
  await page.reload();
  await page.waitForFunction(() => document.body.classList.contains('ready'));
  await page.locator('#view-sizing svg g[data-mark="x-axis tick label"]').first().waitFor();
  const layout = await page.evaluate(() => {
    const svg = document.querySelector('#view-sizing svg');
    const texts = mark => [...svg.querySelectorAll(`g[data-mark="${mark}"] text`)].filter(t => t.textContent);
    const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
    const titles = texts('fx-axis tick label').map(t => t.getBoundingClientRect());
    return {
      prices: [...new Set(texts('x-axis tick label').map(t => t.textContent))],
      onTitles: texts('text').filter(t => titles.some(r => hit(t.getBoundingClientRect(), r))).map(t => t.textContent)
    };
  });
  const range = sql(`SELECT min(r.usd_per_hour) AS lo, max(r.usd_per_hour) AS hi
    FROM read_parquet('${join(DATA, 'size_ladder.parquet')}') l
    JOIN read_parquet('${join(DATA, 'rate_card.parquet')}') r USING (platform, compute_label)
    WHERE l.scale_factor = 1000 AND l.block <> 'clickbench' AND l.compute_label <> 'on-demand'`)[0];
  const inRange = roundTickSet(Number(range.lo), Number(range.hi), COMPARE_PRICE_LABELS).map(roundDollars);
  expect(inRange.length, 'the 1 TB price range spans at least two round steps').toBeGreaterThan(1);
  expect(layout.prices).toEqual(inRange);
  expect(layout.onTitles).toEqual([]);
  // At every volume the price labels stay apart: 1-3 steps over two decades
  // run together ("$10$30") in panels this narrow.
  for (const sf of [1, 10, 100, 1000]) {
    await page.goto(`/compare.html#sf=${sf}`);
    await page.reload();
    await page.waitForFunction(() => document.body.classList.contains('ready'));
    await page.locator('#view-sizing svg g[data-mark="x-axis tick label"]').first().waitFor();
    const touching = await page.evaluate(() => {
      const boxes = [...document.querySelectorAll('#view-sizing svg g[data-mark="x-axis tick label"] text')]
        .filter(t => t.textContent).map(t => [t.textContent, t.getBoundingClientRect()]).sort((a, b) => a[1].left - b[1].left);
      return boxes.slice(1).filter(([, b], i) => b.left - boxes[i][1].right < 4).map(([t], i) => `${boxes[i][0]}|${t}`);
    });
    expect(touching, `Compare price labels at SF${sf}`).toEqual([]);
  }
});

test('fits the viewport from phone to desktop', async ({ app: page }) => {
  // A phone, a large phone, a portrait tablet, the width just past the side-nav
  // breakpoint (where a CSS/JS mismatch collapses the at-a-glance side column), and
  // a desktop. The page measures its column each time it is shown, so each
  // width is the window a reader has when they open the page.
  for (const width of [320, 390, 820, 1120, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await openExplorer(page);
    const layout = await page.evaluate(() => ({
      pageWidth: document.documentElement.scrollWidth,
      viewport: document.documentElement.clientWidth,
      sideColumn: document.querySelector('.glance-side').getBoundingClientRect().width,
      // Rendered width over the width the plot was given: Plot's max-width: 100%
      // shrinks an SVG that does not fit, and past a point the type is unreadable.
      ladderScale: [...document.querySelectorAll('.ladder svg')].map(s => s.getBoundingClientRect().width / Number(s.getAttribute('width'))),
      controlsHeight: document.getElementById('controls').getBoundingClientRect().height,
      controlsVar: parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--controls-h')),
      // The at-a-glance side charts sit beside the quadrant when the column
      // has room, under it when not. A caption that widens the quadrant to
      // the whole column pushes them under on every desktop width.
      sideBeside: document.querySelector('.glance-side').getBoundingClientRect().left
        >= document.querySelector('.quadrant').getBoundingClientRect().right - 1
    }));
    expect(layout.pageWidth, `the page never scrolls sideways (${width} px)`).toBeLessThanOrEqual(layout.viewport);
    expect(layout.sideColumn, `the skew-tax and reliability bars have room (${width} px)`).toBeGreaterThan(200);
    expect(layout.ladderScale.length, `five ladders rendered (${width} px)`).toBe(5);
    for (const scale of layout.ladderScale) {
      expect(scale, `a seven-panel ladder scrolls rather than shrinking below legibility (${width} px)`).toBeGreaterThan(0.75);
    }
    expect(Math.abs(layout.controlsHeight - layout.controlsVar), `scroll offset follows the sticky bar (${width} px)`).toBeLessThanOrEqual(1);
    expect(layout.sideBeside, `at-a-glance side charts beside the quadrant only on a desktop (${width} px)`).toBe(width >= 1180);
  }
});

test('reloads with no network after the first visit', async ({ page, context }) => {
  await openExplorer(page);
  await offlineReady(page);
  await context.setOffline(true);
  try {
    await page.reload();
    await page.waitForFunction(() => document.body.classList.contains('ready') || document.querySelector('.error'));
    await expect(page.locator('.error')).toHaveCount(0);
    await expect.poll(() => count(page, '#view-cfrontier g[data-mark="dot"] > *')).toBeGreaterThan(0);
  } finally {
    await context.setOffline(false);
  }
});
