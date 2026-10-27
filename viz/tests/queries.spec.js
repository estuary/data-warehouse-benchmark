/**
 * Browser tests for the query explorer (queries.html).
 *
 * Same invariant as explorer.spec.js: what the page shows is what the SQL
 * says. Expectations are computed by a DuckDB in Node over the same parquet
 * files the page loads, from the exports directly (cost_curves for the join
 * suites, clickbench_summary for ClickBench, size_ladder, skew_impact,
 * query_catalog), not from the page's own views, and compared with the DOM.
 */
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, visit } from './app.js';
import { settled } from './wait.js';

const require = createRequire(import.meta.url);
const duckdb = require('@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs');

const here = dirname(fileURLToPath(import.meta.url));
const DATA = join(here, '..', 'public', 'data');
const ORIGIN = `http://localhost:${process.env.VIZ_PORT || 4173}`;
const P = f => `read_parquet('${join(DATA, f)}')`;
const PLATFORMS = ['snowflake', 'databricks', 'bigquery', 'redshift', 'fabric', 'motherduck', 'clickhouse'];
const REPO = 'https://github.com/estuary/data-warehouse-benchmark';

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
  // Per-query single-user results, one row per (platform, query, data set).
  con.query(`CREATE VIEW cells AS
    SELECT platform, query_id, CAST(scale_factor AS VARCHAR) AS dataset, median_server_ms AS ms
    FROM ${P('cost_curves.parquet')}
    UNION ALL
    SELECT platform, query_id, 'clickbench', median_server_ms
    FROM ${P('clickbench_summary.parquet')} WHERE is_entry_tier`);
  con.query(`CREATE VIEW catalog AS SELECT * FROM ${P('query_catalog.parquet')}`);
});

test.afterAll(() => { con?.close(); });

const sql = query => con.query(query).toArray().map(r => r.toJSON());

/** The catalog rows a filter state keeps. */
const kept = ({ suite, pattern }) =>
  `SELECT * FROM catalog WHERE block = '${suite}'${pattern ? ` AND list_contains(patterns, '${pattern}')` : ''}`;

async function open(page, hash) {
  const errors = [];
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
  page.on('console', m => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  const requests = [];
  page.on('request', r => requests.push(r.url()));
  await visit(page, `queries.html${hash ? `#${hash}` : ''}`);
  await expect(page.locator('.error')).toHaveCount(0);
  return { errors, requests };
}

const rowIds = page => page.evaluate(() => [...document.querySelectorAll('#q-table-body tr.qrow')].map(tr => tr.dataset.q));

test('starts clean: no errors and no request off the page origin', async ({ page }) => {
  const { errors, requests } = await open(page);
  expect(errors).toEqual([]);
  expect(requests.filter(u => !u.startsWith(ORIGIN) && !u.startsWith('blob:'))).toEqual([]);
});

for (const f of [
  { suite: 'tpcds', sf: '100', pattern: '' },
  { suite: 'dsb', sf: '1000', pattern: 'window' },
  { suite: 'clickbench', sf: null, pattern: 'aggregation' },
  { suite: 'tpcds', sf: '1', pattern: 'set_op' }
]) {
  test(`the table lists exactly the queries the filters keep: ${f.suite} ${f.sf ?? ''} ${f.pattern || 'any'}`, async ({ app: page }) => {
    const hash = new URLSearchParams({ suite: f.suite, ...(f.sf ? { sf: f.sf } : {}), ...(f.pattern ? { pattern: f.pattern } : {}) });
    await open(page, hash.toString());
    const expected = sql(`SELECT query_id FROM (${kept(f)}) ORDER BY number`).map(r => r.query_id);
    expect(await rowIds(page)).toEqual(expected);

    const ds = f.sf ?? 'clickbench';
    // Every measured value, cell by cell.
    const want = sql(`SELECT c.platform, c.query_id, c.ms FROM cells c JOIN (${kept(f)}) k USING (query_id)
                      WHERE c.dataset = '${ds}'`);
    const got = await page.evaluate(() => [...document.querySelectorAll('#q-table-body tr.qrow td[data-p]')]
      .map(td => ({ platform: td.dataset.p, query_id: td.parentElement.dataset.q, ms: td.dataset.ms ?? null, cls: td.className })));
    const key = r => `${r.platform}/${r.query_id}`;
    const byKey = new Map(want.map(r => [key(r), r.ms]));
    expect(got.length).toBe(expected.length * PLATFORMS.length);
    for (const cell of got) {
      if (!byKey.has(key(cell))) {
        expect(cell.cls, `${key(cell)} is not measured`).toContain('na');
      } else if (byKey.get(key(cell)) == null) {
        expect(cell.cls, `${key(cell)} did not finish`).toContain('dnf');
      } else {
        expect(Number(cell.ms), key(cell)).toBeCloseTo(Number(byKey.get(key(cell))), 6);
      }
    }
  });
}

test('the search narrows by query number', async ({ app: page }) => {
  await open(page, 'suite=tpcds&sf=100&search=72');
  expect(await rowIds(page)).toEqual(['tpcds_q72']);
});

test('a column sort is the ORDER BY the database runs', async ({ app: page }) => {
  await open(page, 'suite=dsb&sf=10');
  await page.locator('#q-table-body th[data-sort="redshift"]').click();
  await settled(page);
  const asc = sql(`SELECT k.query_id FROM (${kept({ suite: 'dsb' })}) k
                   LEFT JOIN cells c ON c.query_id = k.query_id AND c.platform = 'redshift' AND c.dataset = '10'
                   ORDER BY c.ms ASC NULLS LAST, k.number`).map(r => r.query_id);
  expect(await rowIds(page)).toEqual(asc);
  expect(page.url()).toContain('sort=redshift%3Aasc');
  await page.locator('#q-table-body th[data-sort="redshift"]').click();
  await settled(page);
  const desc = sql(`SELECT k.query_id FROM (${kept({ suite: 'dsb' })}) k
                    LEFT JOIN cells c ON c.query_id = k.query_id AND c.platform = 'redshift' AND c.dataset = '10'
                    ORDER BY c.ms DESC NULLS LAST, k.number`).map(r => r.query_id);
  expect(await rowIds(page)).toEqual(desc);
});

test('the summary counts each engine\'s fastest queries', async ({ app: page }) => {
  await open(page, 'suite=tpcds&sf=100');
  const want = sql(`WITH c AS (SELECT c.* FROM cells c JOIN (${kept({ suite: 'tpcds' })}) k USING (query_id) WHERE dataset = '100'),
    f AS (SELECT query_id, min(ms) AS best FROM c WHERE ms IS NOT NULL GROUP BY 1)
    SELECT platform, count(*) FILTER (WHERE c.ms = f.best) AS n FROM c LEFT JOIN f USING (query_id) GROUP BY 1`);
  const got = await page.evaluate(() => Object.fromEntries([...document.querySelectorAll('#q-summary-body tbody tr')]
    .map(tr => [tr.querySelector('a').getAttribute('href').split('engine=')[1], Number(tr.querySelector('[data-fastest]').dataset.fastest)])));
  expect(Object.keys(got).length).toBe(want.length);
  for (const r of want) expect(got[r.platform], r.platform).toBe(Number(r.n));
});

test('clicking a query fills the detail panel from the data', async ({ app: page }) => {
  await open(page, 'suite=tpcds&sf=100');
  await expect(page.locator('#q-detail-body')).toBeHidden();
  await page.locator('#q-table-body tr.qrow[data-q="tpcds_q72"]').click();
  await expect(page.locator('#q-detail-title')).toHaveText('TPC-DS 72 on every engine');
  expect(page.url()).toContain('q=tpcds_q72');

  // One dot per engine and volume where the engine finished the query.
  const points = Number(sql(`SELECT count(*) AS n FROM cells WHERE query_id = 'tpcds_q72' AND dataset <> 'clickbench' AND ms IS NOT NULL`)[0].n);
  await expect(page.locator('#q-detail-ds g[data-mark="dot"] > *')).toHaveCount(points);

  // Every size the single-user pass ran it on at 100 GB, on-demand excluded.
  const sizes = Number(sql(`SELECT count(*) AS n FROM ${P('size_ladder.parquet')}
                            WHERE query_id = 'tpcds_q72' AND scale_factor = 100 AND compute_label <> 'on-demand'`)[0].n);
  await expect(page.locator('#q-sizes tr.size-row')).toHaveCount(sizes);

  // SQL links for the canonical text and all seven dialects.
  const links = await page.evaluate(() => [...document.querySelectorAll('#q-sql a')].map(a => [a.dataset.dialect, a.href]));
  expect(links.map(l => l[0])).toEqual(['duckdb', ...PLATFORMS]);
  for (const [d, href] of links) expect(href).toBe(`${REPO}/blob/main/queries/${d}/tpcds/query_72.sql`);

  // The skew pair: one row per engine with both twins measured at 100 GB.
  const pairs = Number(sql(`SELECT count(*) AS n FROM ${P('skew_impact.parquet')} WHERE qnum = 72 AND scale_factor = 100`)[0].n);
  await expect(page.locator('#q-skew tbody tr')).toHaveCount(pairs);
});

test('a ClickBench query shows one bar per engine that finished it', async ({ app: page }) => {
  await open(page, 'suite=clickbench&q=clickbench_q10');
  const bars = Number(sql(`SELECT count(*) AS n FROM cells WHERE query_id = 'clickbench_q10' AND dataset = 'clickbench' AND ms IS NOT NULL`)[0].n);
  await expect(page.locator('#q-detail-cb g[data-mark="bar"] > *')).toHaveCount(bars);
  await expect(page.locator('#q-twin')).toBeHidden();
  await expect(page.locator('#f-volume button').first()).toBeDisabled();
});

test('the table\'s CSV and the detail\'s PNG download with the current filters', async ({ app: page }) => {
  await open(page, 'suite=tpcds&sf=100&pattern=window&q=tpcds_q12');
  const expected = Number(sql(`SELECT count(*) AS n FROM (${kept({ suite: 'tpcds', pattern: 'window' })})`)[0].n);
  const [csv] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('#q-table .chart-tools button', { hasText: 'Data (CSV)' }).click()
  ]);
  const text = await (await csv.createReadStream()).toArray().then(chunks => Buffer.concat(chunks).toString('utf8'));
  const lines = text.trim().split('\n');
  expect(lines.length).toBe(expected + 1);
  expect(lines[0]).toContain('snowflake');
  expect(lines[0]).not.toContain('_measured');
  const [png] = await Promise.all([
    page.waitForEvent('download'),
    page.locator('#q-detail .chart-tools button', { hasText: 'PNG' }).click()
  ]);
  expect(png.suggestedFilename()).toBe('query-detail.png');
});
