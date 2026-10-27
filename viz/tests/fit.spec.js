/**
 * Browser tests for the "Find your warehouse" page.
 *
 * Same invariant as explorer.spec.js: what the page lists is what the SQL says.
 * Expected configurations are computed here from the same parquet the page
 * loads, by a DuckDB running in Node, and compared with the rendered rows.
 */
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, reopen, test, visit } from './app.js';
import { ENGINE_NOTES } from '../src/site/engine-notes.js';

const require = createRequire(import.meta.url);
const duckdb = require('@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs');

const here = dirname(fileURLToPath(import.meta.url));
const DATA = join(here, '..', 'public', 'data');
const ORIGIN = `http://localhost:${process.env.VIZ_PORT || 4173}`;

/** The page's `concurrency` view, with its `dataset` column. */
const GRID = `(SELECT *, CASE workload WHEN 'clickbench' THEN 'clickbench' ELSE CAST(scale_factor AS VARCHAR) END AS dataset
               FROM read_parquet('${join(DATA, 'concurrency_grid.parquet')}')
               WHERE publishable AND usd_per_hour IS NOT NULL)`;

/** The illustrative monthly rule the page states under its table. */
const monthlyRule = (usd, platform, hours) => usd * (ENGINE_NOTES[platform].idle.billsIdle ? 730 : hours * 21.7);

let con;
test.beforeAll(async () => {
  const dist = dirname(require.resolve('@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs'));
  const bundles = {
    mvp: { mainModule: join(dist, 'duckdb-mvp.wasm'), mainWorker: join(dist, 'duckdb-node-mvp.worker.cjs') },
    eh: { mainModule: join(dist, 'duckdb-eh.wasm'), mainWorker: join(dist, 'duckdb-node-eh.worker.cjs') }
  };
  const db = await duckdb.createDuckDB(bundles, new duckdb.VoidLogger(), duckdb.NODE_RUNTIME);
  await db.instantiate();
  con = db.connect();
});
test.afterAll(() => con?.close());

const sql = query => con.query(query).toArray().map(r => r.toJSON());

/** Per-configuration people served at `ds` for `sla`. */
const served = (ds, sla, engines) => `
  SELECT platform, compute_label, any_value(usd_per_hour) AS usd,
         coalesce(max(concurrency) FILTER (WHERE p50_ms <= ${sla} AND shed_pct < sla_max_shed_pct), 0) AS served
  FROM ${GRID} WHERE dataset = '${ds}' ${engines ? `AND platform IN (${engines.map(e => `'${e}'`).join(', ')})` : ''}
  GROUP BY platform, compute_label`;

/** Expected result rows, sorted by (price, platform, size), with the cheapest-per-engine flag. */
function expected({ ds, people, sla, budget = null, engines = null }) {
  const rows = sql(`WITH c AS (${served(ds, sla, engines)})
    SELECT platform, compute_label, usd FROM c
    WHERE served >= ${people}${budget == null ? '' : ` AND usd <= ${budget}`}`)
    .map(r => ({ platform: r.platform, label: r.compute_label, usd: Number(r.usd) }));
  const min = {};
  for (const r of rows) min[r.platform] = Math.min(min[r.platform] ?? Infinity, r.usd);
  return rows.map(r => ({ ...r, cheapest: r.usd === min[r.platform] }))
    .sort((a, b) => a.usd - b.usd || a.platform.localeCompare(b.platform) || a.label.localeCompare(b.label));
}

/** Rendered result rows of one results card (hidden rows included). */
const rendered = (page, card = 'fit-results') => page.evaluate(id =>
  [...document.querySelectorAll(`#${id} tbody tr`)].map(tr => ({
    platform: tr.dataset.platform, label: tr.dataset.label, usd: Number(tr.dataset.price),
    cheapest: tr.dataset.cheapest === 'true', monthly: Number(tr.dataset.monthly), hidden: tr.hidden
  })), card);

const sortRows = rows => [...rows].map(({ platform, label, usd, cheapest }) => ({ platform, label, usd, cheapest }))
  .sort((a, b) => a.usd - b.usd || a.platform.localeCompare(b.platform) || a.label.localeCompare(b.label));

async function open(page, hash = '') {
  const errors = [];
  const requests = [];
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
  page.on('console', m => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  page.on('request', r => requests.push(r.url()));
  await visit(page, `fit.html${hash}`);
  await expect(page.locator('.error')).toHaveCount(0);
  return { errors, requests };
}

/** Wait until the results card matches the expectation (renders are async). */
async function expectResults(page, want, card = 'fit-results') {
  await expect.poll(async () => sortRows(await rendered(page, card)), { timeout: 20_000 }).toEqual(want);
  const got = await rendered(page, card);
  for (let i = 1; i < got.length; i++) expect(got[i].usd).toBeGreaterThanOrEqual(got[i - 1].usd);
  // Collapsed by default: only each engine's cheapest size is visible.
  for (const r of got) expect(r.hidden).toBe(!r.cheapest);
}

test('starts clean with the default workload and lists what the SQL lists', async ({ page }) => {
  const { errors, requests } = await open(page);
  await expectResults(page, expected({ ds: '100', people: 10, sla: 5000 }));
  expect(errors).toEqual([]);
  const external = requests.filter(u => !u.startsWith(ORIGIN) && !u.startsWith('blob:'));
  expect(external).toEqual([]);
  expect(await page.evaluate(() => location.hash)).toBe('#kind=join&sf=100&people=10&sla=5000&budget=&engines=&hours=8');
});

test('each archetype preset sets the inputs and the rows follow the SQL', async ({ app: page }) => {
  await open(page);
  await page.locator('[data-preset="small-team"]').click();
  await expectResults(page, expected({ ds: '100', people: 5, sla: 5000 }));

  // The product company asks for 100 on scan queries; the page reads that at
  // the whole 43-query suite at once, the grid's 50-session level.
  await page.locator('[data-preset="product"]').click();
  await expectResults(page, expected({ ds: 'clickbench', people: 50, sla: 5000 }));

  // The enterprise case: no single unit serves 100 at 1 TB within 5 s.
  await page.locator('[data-preset="enterprise"]').click();
  await expect(page.locator('#fit-results .fit-none')).toBeVisible();
  expect(expected({ ds: '1000', people: 100, sla: 5000 })).toEqual([]);

  // "Already on a platform" keeps the inputs.
  const before = await page.evaluate(() => location.hash);
  await page.locator('[data-preset="platform"]').click();
  expect(await page.evaluate(() => location.hash)).toBe(before);
  await expect(page.locator('#engines-hint')).not.toBeEmpty();
});

test('the chosen example stays marked until an input changes', async ({ app: page }) => {
  await open(page);
  const pressed = page.locator('[data-preset][aria-pressed="true"]');
  await expect(pressed).toHaveCount(0);

  await page.locator('[data-preset="small-team"]').click();
  await expect(pressed).toHaveCount(1);
  await expect(pressed).toHaveAttribute('data-preset', 'small-team');
  // Opening the shared link again finds the same example.
  await reopen(page);
  await expect(pressed).toHaveAttribute('data-preset', 'small-team');

  await page.locator('#in-people button[data-people="20"]').click();
  await expect(pressed).toHaveCount(0);
  // Setting the input back to the example's value marks it again.
  await page.locator('#in-people button[data-people="5"]').click();
  await expect(pressed).toHaveAttribute('data-preset', 'small-team');

  await page.locator('[data-preset="platform"]').click();
  await expect(pressed).toHaveAttribute('data-preset', 'platform');
  await page.locator('#in-engines button[data-engine="snowflake"]').click();
  await expect(pressed).toHaveCount(0);
});

test('clear all, then one engine, gets from seven to one in two clicks', async ({ app: page }) => {
  await open(page);
  const on = page.locator('#in-engines button[data-engine]:not([data-engine="all"]):not([data-engine="none"])[aria-pressed="true"]');
  await expect(on).toHaveCount(7);
  await expect(page.locator('#in-engines [data-engine="all"]')).toBeDisabled();

  await page.locator('#in-engines [data-engine="none"]').click();
  await expect(on).toHaveCount(0);
  await expect(page.locator('#in-engines [data-engine="none"]')).toBeDisabled();
  await expect(page.locator('#fit-results')).toContainText('Choose at least one engine');
  await expect(page).toHaveURL(/engines=none/);
  // "None" survives opening the link again rather than reading back as "all".
  await reopen(page);
  await expect(on).toHaveCount(0);

  await page.locator('#in-engines [data-engine="fabric"]').click();
  await expect(on).toHaveCount(1);
  await expect(page).toHaveURL(/engines=fabric(&|$)/);
  await expectResults(page, expected({ ds: '100', people: 10, sla: 5000, engines: ['fabric'] }));
});

test('the none-served state appears exactly when no configuration qualifies, and names the best', async ({ app: page }) => {
  const cases = [
    { ds: '1000', people: 100, sla: 5000 },
    { ds: '1000', people: 10, sla: 5000 },
    { ds: '100', people: 100, sla: 1000 }
  ];
  for (const c of cases) {
    await open(page, `#kind=join&sf=${c.ds}&people=${c.people}&sla=${c.sla}`);
    const want = expected(c);
    if (want.length) {
      await expectResults(page, want);
      await expect(page.locator('#fit-results .fit-none')).toHaveCount(0);
      continue;
    }
    await expect(page.locator('#fit-results .fit-none')).toBeVisible();
    const top = sql(`WITH c AS (${served(c.ds, c.sla)}) SELECT platform, compute_label FROM c
                     WHERE served = (SELECT max(served) FROM c) AND served > 0`);
    const topN = Number(sql(`WITH c AS (${served(c.ds, c.sla)}) SELECT max(served) AS m FROM c`)[0].m);
    expect(Number(await page.locator('#fit-results .fit-none').getAttribute('data-best'))).toBe(topN);
    const named = await page.evaluate(() => [...document.querySelectorAll('#fit-results .fit-best li')]
      .map(li => `${li.dataset.platform}|${li.dataset.label}`).sort());
    expect(named).toEqual(top.map(r => `${r.platform}|${r.compute_label}`).sort());
  }
});

test('budget, engine choice and the latency target re-filter; the monthly figure follows its rule', async ({ app: page }) => {
  const hash = '#kind=join&sf=10&people=20&sla=2000&budget=10&engines=snowflake,redshift,fabric&hours=6';
  await open(page, hash);
  expect(await page.evaluate(() => location.hash)).toBe(hash);
  const want = expected({ ds: '10', people: 20, sla: 2000, budget: 10, engines: ['snowflake', 'redshift', 'fabric'] });
  await expectResults(page, want);
  for (const r of await rendered(page)) expect(r.monthly).toBeCloseTo(monthlyRule(r.usd, r.platform, 6), 6);
  await expect(page.locator('[data-engine="snowflake"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('[data-engine="bigquery"]')).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('[data-people="20"]')).toHaveAttribute('aria-pressed', 'true');

  // Turning an engine on writes the hash and re-queries.
  await page.locator('[data-engine="bigquery"]').click();
  await expect.poll(() => page.evaluate(() => location.hash)).toContain('engines=snowflake,bigquery,redshift,fabric');
  await expectResults(page, expected({ ds: '10', people: 20, sla: 2000, budget: 10, engines: ['snowflake', 'bigquery', 'redshift', 'fabric'] }));

  // The slider re-targets.
  await page.evaluate(() => {
    const s = document.getElementById('in-sla');
    s.value = '8000';
    s.dispatchEvent(new Event('input', { bubbles: true }));
  });
  await expectResults(page, expected({ ds: '10', people: 20, sla: 8000, budget: 10, engines: ['snowflake', 'bigquery', 'redshift', 'fabric'] }));
});

test('"Both" shows join and scan results side by side, and "show all" reveals every size', async ({ app: page }) => {
  await open(page, '#kind=both&sf=100&people=50&sla=5000');
  await expectResults(page, expected({ ds: '100', people: 50, sla: 5000 }));
  await expect(page.locator('#fit-results-scan')).toBeVisible();
  await expectResults(page, expected({ ds: 'clickbench', people: 50, sla: 5000 }), 'fit-results-scan');
  await page.locator('#fit-results .fit-toggle').click();
  await expect.poll(async () => (await rendered(page)).every(r => !r.hidden)).toBe(true);
});

test('the headcount helper rounds up to a measured level', async ({ app: page }) => {
  await open(page);
  await page.locator('.fit-helper > summary').click();
  await page.fill('#in-users', '400');
  await page.fill('#in-share', '6');
  await page.locator('#in-helper-apply').click();
  await expect(page.locator('[data-people="50"]')).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#helper-out')).toContainText('rounds up to 50');
});

test('the idle hover names how the monthly figure counts each engine, and the hours match the rule', async () => {
  const { HOURS_PER_MONTH } = await import('../src/pages/fit.js').catch(() => ({ HOURS_PER_MONTH: 730 }));
  for (const [platform, { idle }] of Object.entries(ENGINE_NOTES)) {
    expect(Boolean(idle.fitMonth), `${platform}: an engine billed for every hour needs its fitMonth sentence`).toBe(idle.billsIdle);
    if (idle.fitMonth) expect(idle.fitMonth).toContain(`${HOURS_PER_MONTH} hours`);
    for (const text of [idle.pays, idle.quiet]) {
      expect(text, `${platform}: notes shown on every page must not refer to one page`).not.toMatch(/illustrative month|Find your warehouse|on this page/i);
    }
  }
});

test('the one-person table names both sizes, and the same size shows the same time', async ({ app: page }) => {
  // The designated sizes the page derives (cost_curves matched to the size
  // ladder) are the run plan's: every single-user result at the designated size
  // matches exactly one ladder size, query for query.
  const match = sql(`
    WITH m AS (
      SELECT c.platform, c.scale_factor, l.compute_label, count(*) AS matched
      FROM read_parquet('${join(DATA, 'cost_curves.parquet')}') c
      JOIN read_parquet('${join(DATA, 'size_ladder.parquet')}') l
        ON l.platform = c.platform AND l.scale_factor = c.scale_factor AND l.query_id = c.query_id
       AND abs(l.median_server_ms - c.median_server_ms) < 1e-6
      GROUP BY ALL)
    SELECT m.platform, m.scale_factor, m.compute_label, m.matched,
           (SELECT count(median_server_ms) FROM read_parquet('${join(DATA, 'cost_curves.parquet')}') c
             WHERE c.platform = m.platform AND c.scale_factor = m.scale_factor) AS total
    FROM m QUALIFY row_number() OVER (PARTITION BY platform, scale_factor ORDER BY matched DESC) = 1`);
  for (const r of match) expect(Number(r.matched), `${r.platform} ${r.scale_factor}`).toBe(Number(r.total));
  const at10 = Object.fromEntries(match.filter(r => Number(r.scale_factor) === 10).map(r => [r.platform, r.compute_label]));
  expect(at10).toMatchObject({ snowflake: 'Small (Gen2)', databricks: 'Small', bigquery: 'on-demand', redshift: '8 RPU', fabric: 'F8', motherduck: 'Standard' });

  await open(page, '#kind=join&sf=10&people=10&sla=5000&budget=&engines=&hours=8');
  const rows = page.locator('#fit-speed-body tbody tr');
  await expect(rows).toHaveCount(7, { timeout: 20_000 });
  for (const tr of await rows.all()) {
    const { designated, cheapest, change } = await tr.evaluate(el => ({ ...el.dataset }));
    const times = await tr.locator('td.num').allTextContents();
    expect(designated, 'the assigned size is named').not.toBe('');
    // Same size: one measurement, shown twice.
    if (change === 'same') {
      expect(cheapest.replace(/^\d+x/, '1x')).toBe(designated);
      expect(times[1].trim()).toBe(times[0].trim());
    }
  }
  await expect(page.locator('#fit-speed-body tr[data-platform="snowflake"]')).toHaveAttribute('data-change', 'smaller');
  await expect(page.locator('#fit-speed-body tr[data-platform="bigquery"]')).toHaveAttribute('data-change', 'pricing');
});
