/**
 * Browser tests for the engine page (engine.html#engine=<id>).
 *
 * The invariant is the one tests/explorer.spec.js guards for the Compare page:
 * what the page shows is what the SQL says. Expectations are computed from the
 * same parquet files the page loads, by a DuckDB running in Node, and compared
 * with the rendered tables.
 */
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { CLICKBENCH, DEFAULT_SLA_MS, ENGINE_PRICE_LABELS, SCAN_SUITE_QUERIES } from '../src/constants.js';
import { roundDollars, roundTickSet } from '../src/format.js';
import { LADDER_GAP_REASONS, LADDER_NOTES } from '../src/site/ladder-caveats.js';
import { expect, expectNoBrokenValues, test, visit } from './app.js';
import { settled } from './wait.js';

const require = createRequire(import.meta.url);
const duckdb = require('@duckdb/duckdb-wasm/dist/duckdb-node-blocking.cjs');

const here = dirname(fileURLToPath(import.meta.url));
const DATA = join(here, '..', 'public', 'data');
const ORIGIN = `http://localhost:${process.env.VIZ_PORT || 4173}`;
const file = name => `read_parquet('${join(DATA, name)}')`;

const ENGINES = ['snowflake', 'databricks', 'bigquery', 'redshift', 'fabric', 'motherduck', 'clickhouse'];
const VOL = { 1: '1 GB', 10: '10 GB', 100: '100 GB', 1000: '1 TB', [CLICKBENCH]: 'ClickBench' };

/** Same expression as the page and the Python reducer. */
const useful = sla =>
  `coalesce(max(concurrency) FILTER (WHERE p50_ms <= ${sla} AND shed_pct < sla_max_shed_pct), 0)`;

/**
 * The published grid as the page's `concurrency` view filters it, with its
 * `dataset` key and the scan grid's 50-session level read as the whole
 * 43-query suite at once.
 */
const GRID = `(SELECT * REPLACE (CASE WHEN workload = '${CLICKBENCH}' AND concurrency = 50 THEN ${SCAN_SUITE_QUERIES} ELSE concurrency END AS concurrency),
                      CASE workload WHEN '${CLICKBENCH}' THEN '${CLICKBENCH}' ELSE CAST(scale_factor AS VARCHAR) END AS dataset
               FROM ${file('concurrency_grid.parquet')} WHERE publishable AND usd_per_hour IS NOT NULL)`;

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

function sql(query) {
  return con.query(query).toArray().map(r => r.toJSON());
}

/** Open one engine's page and wait for it to render (see `visit` in tests/app.js). */
async function openEngine(page, engine, extra = '') {
  const errors = [];
  page.on('pageerror', e => errors.push(`pageerror: ${e.message}`));
  page.on('console', m => { if (m.type() === 'error') errors.push(`console: ${m.text()}`); });
  const requests = [];
  page.on('request', r => requests.push(r.url()));
  await visit(page, `engine.html#engine=${engine}${extra}`);
  await expect(page.locator('.error')).toHaveCount(0);
  return { errors, requests };
}

const tableRows = (page, id) => page.evaluate(sel =>
  [...document.querySelectorAll(`${sel} tbody tr`)].map(tr => [...tr.children].map(td => td.textContent.trim())), `#${id}`);
const tableHead = (page, id) => page.evaluate(sel =>
  [...document.querySelectorAll(`${sel} thead th`)].map(th => th.textContent.trim()), `#${id}`);

const setSla = (page, value) => page.evaluate(v => {
  const s = document.getElementById('eng-sla');
  s.value = String(v);
  s.dispatchEvent(new Event('input', { bubbles: true }));
}, value);

test('every engine page starts clean: no errors, no other hosts, every chart drawn', async ({ page }) => {
  test.setTimeout(240_000);
  const { errors, requests } = await openEngine(page, 'snowflake');
  for (const engine of ENGINES) {
    // Switching engine re-renders in place, as a reader does it.
    await page.locator(`#engine-choices button[data-engine="${engine}"]`).click();
    await expect(page).toHaveURL(new RegExp(`engine=${engine}`));
    await settled(page);
    await expect(page.locator('#eng-title')).not.toHaveText('');
    expect(await page.locator('#eng-sizes-table tbody tr').count(), `${engine} sizes`).toBeGreaterThan(0);
    expect(await page.locator('#eng-peers-table tbody tr').count(), `${engine} peers`).toBeGreaterThan(0);
    await expectNoBrokenValues(page, engine);
  }
  expect(errors).toEqual([]);
  const external = requests.filter(u => !u.startsWith(ORIGIN) && !u.startsWith('blob:'));
  expect(external, 'every request must stay on the page origin').toEqual([]);

  // Each engine opened afresh from its address also reaches ready without errors.
  for (const engine of ENGINES) {
    const fresh = await openEngine(page, engine);
    expect(fresh.errors, engine).toEqual([]);
  }
});

test('the sizes table lists the rate card for the engine', async ({ app: page }) => {
  for (const engine of ['snowflake', 'bigquery', 'clickhouse']) {
    await openEngine(page, engine);
    const rows = await tableRows(page, 'eng-sizes-table');
    // Sizes with a published result only (the page leaves out a rate-card size no export has).
    const card = sql(`SELECT compute_label, usd_per_hour FROM ${file('rate_card.parquet')}
                      WHERE platform = '${engine}' AND (platform, compute_label) IN (SELECT (platform, compute_label) FROM ${file('size_ladder.parquet')} UNION SELECT (platform, compute_label) FROM ${file('concurrency_grid.parquet')} UNION SELECT (platform, compute_label) FROM ${file('clickbench_summary.parquet')})
                      ORDER BY usd_per_hour NULLS LAST, compute_label`);
    expect(rows.map(r => r[0])).toEqual(card.map(r => r.compute_label));
    expect(rows.map(r => r[2])).toEqual(card.map(r =>
      (r.usd_per_hour == null ? 'Billed by bytes scanned' : `$${Number(r.usd_per_hour).toFixed(2)}`)));
  }
});

test('the people-served table matches the SQL and re-targets with the latency target', async ({ app: page }) => {
  for (const engine of ['redshift', 'databricks']) {
    await openEngine(page, engine);
    for (const sla of [DEFAULT_SLA_MS, 1000, 20000]) {
      if (sla !== DEFAULT_SLA_MS) await setSla(page, sla);
      await settled(page);
      const head = await tableHead(page, 'eng-conc-table');
      const rows = await tableRows(page, 'eng-conc-table');
      const expected = sql(`SELECT compute_label, dataset, ${useful(sla)} AS s, any_value(usd_per_hour) AS usd
                            FROM ${GRID} WHERE platform = '${engine}' GROUP BY 1, 2`);
      const bySize = new Map(rows.map(r => [r[0], r]));
      expect(bySize.size).toBe(new Set(expected.map(r => r.compute_label)).size);
      for (const e of expected) {
        const row = bySize.get(e.compute_label);
        const col = head.indexOf(VOL[e.dataset]);
        expect(col, `${engine} column for ${e.dataset}`).toBeGreaterThan(1);
        expect(row[col], `${engine} ${e.compute_label} ${e.dataset} at ${sla} ms`).toBe(String(Number(e.s)));
      }
    }
  }
});

test('ladder speedups follow the common-complete method', async ({ app: page }) => {
  for (const engine of ['snowflake', 'redshift']) {
    await openEngine(page, engine);
    const rows = await tableRows(page, 'eng-ladder-table');
    const expected = sql(`
      WITH l AS (
        SELECT l.*, r.usd_per_hour FROM ${file('size_ladder.parquet')} l
        LEFT JOIN ${file('rate_card.parquet')} r USING (platform, compute_label)
        WHERE l.platform = '${engine}' AND l.block <> 'clickbench' AND l.compute_label <> 'on-demand'
      ),
      d AS (SELECT * FROM l WHERE median_server_ms IS NOT NULL),
      k AS (SELECT scale_factor, count(DISTINCT compute_label) AS n FROM l GROUP BY 1),
      common AS (
        SELECT d.scale_factor, d.query_id FROM d JOIN k USING (scale_factor)
        GROUP BY 1, 2 HAVING count(DISTINCT d.compute_label) = any_value(k.n)
      ),
      agg AS (
        SELECT scale_factor, compute_label, any_value(usd_per_hour) AS usd, sum(median_server_ms) AS t
        FROM d JOIN common USING (scale_factor, query_id) GROUP BY 1, 2
      ),
      sized AS (SELECT *, count(*) OVER (PARTITION BY scale_factor) AS sizes FROM agg)
      SELECT scale_factor, compute_label,
             arg_min(t, usd) OVER (PARTITION BY scale_factor) / t AS speedup
      FROM sized WHERE sizes > 1 ORDER BY scale_factor, usd`);
    expect(rows.length).toBe(expected.length);
    expected.forEach((e, i) => {
      expect(rows[i][0]).toBe(VOL[e.scale_factor]);
      expect(rows[i][1]).toBe(e.compute_label);
      const shown = Number(rows[i][5].replace('×', ''));
      expect(shown, `${engine} ${e.compute_label} SF${e.scale_factor}`).toBeCloseTo(Number(e.speedup), 2);
    });
  }
});

test('every engine ladder shows all four volumes and says why one has no ladder', async ({ app: page }) => {
  // What the single-user pass ran, per engine and volume: 0, 1 or several sizes.
  const sizes = sql(`SELECT platform, scale_factor, count(DISTINCT compute_label) AS n
    FROM ${file('size_ladder.parquet')} WHERE block <> 'clickbench' AND compute_label <> 'on-demand' GROUP BY ALL`);
  const count = (e, sf) => Number(sizes.find(r => r.platform === e && Number(r.scale_factor) === sf)?.n ?? 0);
  for (const engine of ENGINES) {
    await openEngine(page, engine);
    const svgText = () => page.locator('#eng-ladder-chart svg').first().textContent();
    // One panel per volume, titled by the facet axis, whether or not anything ran there.
    await expect.poll(() => page.evaluate(() =>
      [...document.querySelectorAll('#eng-ladder-chart g[data-mark="fx-axis tick label"] text')].map(t => t.textContent)))
      .toEqual(['1 GB', '10 GB', '100 GB', '1 TB']);
    const gaps = [1, 10, 100, 1000].filter(sf => count(engine, sf) <= 1);
    const note = page.locator('#eng-ladder-empty');
    if (!gaps.length) { await expect(note).toBeHidden(); continue; }
    await expect(note).toBeVisible();
    for (const sf of gaps) {
      await expect(note).toContainText(VOL[sf]);
      expect(await svgText(), `${engine} ${VOL[sf]}`).toContain(count(engine, sf) ? 'so there is nothing to compare' : 'Not measured at this volume');
    }
  }
});

test('every engine ladder labels its price axis at round steps inside its range', async ({ app: page }) => {
  // Left to Plot, a narrow panel is labelled at the decades only: BigQuery's
  // $6-$60 ladder would read "$10" and nothing else.
  const ranges = sql(`SELECT l.platform, min(r.usd_per_hour) AS lo, max(r.usd_per_hour) AS hi
    FROM (SELECT DISTINCT platform, compute_label FROM ${file('size_ladder.parquet')}
          WHERE block <> 'clickbench' AND compute_label <> 'on-demand') l
    JOIN ${file('rate_card.parquet')} r USING (platform, compute_label) GROUP BY 1`);
  for (const engine of ENGINES) {
    const { lo, hi } = ranges.find(r => r.platform === engine);
    const expected = roundTickSet(Number(lo), Number(hi), ENGINE_PRICE_LABELS).map(roundDollars);
    expect(expected.length, `${engine} spans at least two round steps`).toBeGreaterThan(1);
    await openEngine(page, engine);
    await expect.poll(() => page.evaluate(() =>
      [...new Set([...document.querySelectorAll('#eng-ladder-chart g[data-mark="x-axis tick label"] text')].map(t => t.textContent))]), engine)
      .toEqual(expected);
  }
});

test('any table sorts by a column heading and returns to its own order', async ({ app: page }) => {
  await openEngine(page, 'snowflake');
  const id = 'eng-ladder-table';
  const th = page.locator(`#${id} thead th`, { hasText: 'Suite time' });
  const col = (await tableHead(page, id)).findIndex(h => h.startsWith('Suite time'));
  const seconds = async () => (await tableRows(page, id)).map(r => Number(r[col].replace(/,/g, '').replace(/[^\d.].*$/, '')));
  const original = await tableRows(page, id);

  await th.click();
  await expect(th).toHaveAttribute('aria-sort', 'ascending');
  const up = await seconds();
  expect(up).toEqual([...up].sort((a, b) => a - b));
  // The cells are only reordered, never rewritten.
  const asRows = rows => rows.map(r => r.join('|')).sort();
  expect(asRows(await tableRows(page, id))).toEqual(asRows(original));
  await th.press('Enter');
  await expect(th).toHaveAttribute('aria-sort', 'descending');
  const down = await seconds();
  expect(down).toEqual([...down].sort((a, b) => b - a));
  await th.click();
  await expect(th).toHaveAttribute('aria-sort', 'none');
  expect(await tableRows(page, id)).toEqual(original);
});

test('a sorted table keeps its sort when the page redraws it', async ({ app: page }) => {
  await openEngine(page, 'fabric');
  const id = 'eng-conc-table';
  const th = page.locator(`#${id} thead th`, { hasText: 'List price per hour' });
  await th.click();
  await th.click();
  await expect(th).toHaveAttribute('aria-sort', 'descending');
  const first = async () => (await tableRows(page, id))[0][0];
  expect(await first()).toBe('F128');
  await setSla(page, 2500);
  await expect(page.locator(`#${id} caption`)).toContainText('2.5 s');
  await expect(page.locator(`#${id} thead th`, { hasText: 'List price per hour' })).toHaveAttribute('aria-sort', 'descending');
  expect(await first()).toBe('F128');
  // Back to the page's order, so later tests read the default.
  await page.locator(`#${id} thead th`, { hasText: 'List price per hour' }).click();
  expect(await first()).toBe('F2');
  await setSla(page, DEFAULT_SLA_MS);
});

test('the ladder chart explains itself where a line is flat', async ({ app: page }) => {
  // ClickHouse has a cost line like every other engine, so no note and the
  // full legend.
  await openEngine(page, 'clickhouse');
  await expect(page.locator('#eng-ladder-note')).toBeHidden();
  await expect(page.locator('#eng-ladder-legend')).toBeVisible();

  await page.locator('#engine-choices button[data-engine="fabric"]').click();
  await expect(page.locator('#eng-ladder-note')).toContainText('not how fast one query runs');
  await expect(page.locator('#eng-ladder-note')).toContainText('about twice as long as F32');
  await expect(page.locator('#eng-ladder-legend')).toBeVisible();
  await page.locator('#engine-choices button[data-engine="snowflake"]').click();
  await expect(page.locator('#eng-ladder-note')).toBeHidden();
});

/** Sizes in the single-user export per (engine, volume), join suites, as the ladder counts them. */
const ladderSizes = () => sql(`SELECT platform, scale_factor, count(DISTINCT compute_label) AS n
  FROM ${file('size_ladder.parquet')} WHERE block <> 'clickbench' AND compute_label <> 'on-demand' GROUP BY ALL`);

// The stale-caveat tests. The ladder's hand-written caveats (src/site/ladder-caveats.js)
// describe the data as it stood; when new runs land, these fail and name the text to
// remove or reword, so a caveat never outlives the gap or the result it explains.
test('stale caveat: every hand-written ladder reason still describes a gap in the data', () => {
  const sizes = ladderSizes();
  for (const [platform, byScale] of Object.entries(LADDER_GAP_REASONS)) {
    for (const [sf, { when }] of Object.entries(byScale)) {
      const n = Number(sizes.find(r => r.platform === platform && Number(r.scale_factor) === Number(sf))?.n ?? 0);
      const limit = when === 'none' ? 0 : 1;
      expect(n, `Stale caveat: ${platform} at ${VOL[sf]} now has ${n} size(s) in size_ladder.parquet, `
        + `but LADDER_GAP_REASONS.${platform}[${sf}] in src/site/ladder-caveats.js says "${when}". Remove or reword it.`)
        .toBeLessThanOrEqual(limit);
    }
  }
});

test('stale caveat: every ladder note still states what the data shows', () => {
  /** Common-complete suite time per size at each volume with two or more sizes. */
  const suiteTimes = platform => sql(`
    WITH j AS (SELECT * FROM ${file('size_ladder.parquet')}
               WHERE platform = '${platform}' AND block IN ('tpcds', 'dsb') AND median_server_ms IS NOT NULL),
    k AS (SELECT scale_factor, count(DISTINCT compute_label) AS n FROM j GROUP BY 1 HAVING n > 1),
    ok AS (SELECT scale_factor, block, query_id FROM j JOIN k USING (scale_factor)
           GROUP BY ALL HAVING count(DISTINCT compute_label) = any_value(k.n))
    SELECT scale_factor, compute_label, sum(median_server_ms) AS s FROM j JOIN ok USING (scale_factor, block, query_id) GROUP BY ALL`);
  for (const [platform, { claim }] of Object.entries(LADDER_NOTES)) {
    const t = suiteTimes(platform);
    if (claim.flat) {
      for (const sf of claim.flat.scales) {
        const s = t.filter(r => Number(r.scale_factor) === sf).map(r => Number(r.s));
        expect(s.length, `LADDER_NOTES.${platform}: ${VOL[sf]} has fewer than two sizes`).toBeGreaterThan(1);
        const spread = Math.max(...s) / Math.min(...s);
        expect(spread, `Stale caveat: ${platform}'s sizes at ${VOL[sf]} differ by ${spread.toFixed(2)}x `
          + `in suite time, so LADDER_NOTES.${platform} in src/site/ladder-caveats.js (a flat blue line there) no longer holds. Reword or remove it.`)
          .toBeLessThan(claim.flat.tolerance);
      }
    }
    if (claim.slower) {
      const { scale, small, large, ratio: [lo, hi] } = claim.slower;
      const at = label => Number(t.find(r => Number(r.scale_factor) === scale && r.compute_label === label)?.s);
      const ratio = at(small) / at(large);
      expect(ratio >= lo && ratio <= hi, `Stale caveat: at ${VOL[scale]} ${platform} ${small} took ${ratio.toFixed(2)}x as long as ${large}, `
        + `outside the ${lo}-${hi}x LADDER_NOTES.${platform} in src/site/ladder-caveats.js states. Reword it.`).toBe(true);
    }
  }
});

test('every skip recorded on purpose is listed with its reason, and its cells say Skipped', async ({ app: page }) => {
  const { plainReason, PASS, READER_REASONS } = await import('../src/site/skips.js');
  const skips = sql(`SELECT * FROM ${file('scale_skips.parquet')} ORDER BY ALL`);
  expect(skips.length).toBeGreaterThan(0);
  for (const s of skips) {
    expect(READER_REASONS[s.reason], `scale_skips reason has no reader text in src/site/skips.js READER_REASONS: "${s.reason}"`).toBeTruthy();
  }
  for (const platform of [...new Set(skips.map(s => s.platform))]) {
    await openEngine(page, platform);
    for (const s of skips.filter(x => x.platform === platform)) {
      const reason = plainReason(s.reason);
      expect(reason, 'a reason keeps no decision bookkeeping').not.toMatch(/operator|run_plan/i);
      if (s.run_type === PASS.single || s.run_type === '*') await expect(page.locator('#eng-ladder-skips')).toContainText(reason);
      if (s.run_type === PASS.load || s.run_type === '*') await expect(page.locator('#eng-conc-skips')).toContainText(reason);
    }
  }
  // A grid cell skipped on purpose reads "Skipped", not "Not run": Fabric's F2 to F16 at 1 TB.
  await openEngine(page, 'fabric');
  const rows = await tableRows(page, 'eng-conc-table');
  const head = await page.locator('#eng-conc-table thead th').allInnerTexts();
  const col = head.findIndex(h => h.includes('1 TB'));
  for (const size of ['F2', 'F4', 'F8', 'F16']) expect(rows.find(r => r[0] === size)[col], `fabric ${size} at 1 TB`).toBe('Skipped');
});

test('the gap note under the ladder is built from the data', async ({ app: page }) => {
  // An engine with two or more sizes at every volume shows no note; one that
  // ran a single size at some volume names those volumes.
  const sizes = ladderSizes();
  for (const platform of ['databricks', 'fabric']) {
    await openEngine(page, platform);
    const note = page.locator('#eng-ladder-empty');
    const single = [1, 10, 100, 1000].filter(sf =>
      Number(sizes.find(r => r.platform === platform && Number(r.scale_factor) === sf)?.n ?? 0) === 1);
    if (!single.length) {
      await expect(note).toBeHidden();
      continue;
    }
    for (const sf of single) await expect(note).toContainText(`At ${VOL[sf]} the single-user pass ran only`);
    await expect(note).toContainText('The concurrency grid below ran');
  }
});

test('one data-volume choice drives both load charts, and ClickBench says why patterns are empty', async ({ app: page }) => {
  await openEngine(page, 'snowflake', `&sf=${CLICKBENCH}`);
  const pressed = sel => page.locator(`${sel} button[aria-pressed="true"]`);
  await expect(page.locator('#eng-pat-cb')).toBeVisible();
  await expect(page.locator('#eng-pat-empty')).toBeHidden();
  await expect(pressed('#eng-pat-ds-choices')).toHaveText('ClickBench');

  await page.locator('#eng-pat-cb button').click();
  await expect(page).toHaveURL(/sf=100(&|$)/);
  await expect(page.locator('#eng-pat-cb')).toBeHidden();
  await expect(page.locator('#eng-pat-chart svg')).toBeVisible();
  await expect(pressed('#eng-ds-choices')).toHaveText('100 GB');
  await expect(pressed('#eng-pat-ds-choices')).toHaveText('100 GB');

  await page.locator('#eng-pat-ds-choices button[data-ds="10"]').click();
  await expect(pressed('#eng-ds-choices')).toHaveText('10 GB');
  await expect(page.locator('#eng-load-label')).toContainText('10 GB');
});

test('the hash carries the engine, data set and latency target', async ({ app: page }) => {
  await openEngine(page, 'fabric', `&sf=${CLICKBENCH}&sla=2000`);
  await expect(page.locator('#eng-title')).toHaveText('Fabric');
  await expect(page.locator(`#eng-ds-choices button[data-ds="${CLICKBENCH}"]`)).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('#eng-sla-value')).toHaveText('2 s');
  await page.locator('#eng-ds-choices button[data-ds="1000"]').click();
  await expect(page).toHaveURL(/sf=1000/);
  await setSla(page, 3500);
  await expect(page).toHaveURL(/sla=3500/);
});

test('every engine leads at least one measure in its comparison table', async ({ app: page }) => {
  // The guide's Conclusion says each of the seven engines led on at least one
  // measure (Table 6); the engine pages carry the same measures, so hold it here.
  for (const engine of ['snowflake', 'databricks', 'bigquery', 'redshift', 'fabric', 'clickhouse', 'motherduck']) {
    await openEngine(page, engine);
    await expect.poll(() => page.locator('#eng-peers-table tbody tr').count()).toBeGreaterThan(13);
    const ranks = await page.locator('#eng-peers-table tbody tr td:nth-child(3)').allInnerTexts();
    expect(ranks.some(r => r.startsWith('1 of')), `${engine} leads no measure in its comparison table`).toBe(true);
  }
});
