/**
 * The query explorer: every one of the 244 queries, one row each, with each
 * engine's single-user time beside it, and a detail panel for the query a
 * reader picks.
 *
 * Every figure on the page is a SQL query against the views in `data.js`:
 *
 *  - `query_results`: per-query single-user medians at each engine's
 *    designated size (entry tier for ClickBench), with the query's tags.
 *  - `ladder`: the same queries at every compute size the single-user pass ran.
 *  - `skew_warm`: each DSB query against its TPC-DS twin, per engine and volume.
 *  - `query_catalog`: ids, suites, tags and the SQL file path per dialect.
 *
 * The controls are plain HTML and live in the URL hash
 * (`#suite=tpcds&sf=100&pattern=&q=&engine=&sort=`), so a filtered table or a
 * chosen query can be linked to, including from the engine pages.
 */
import * as vg from '@uwdata/vgplot';
import { clausePoint } from '@uwdata/mosaic-core';
import { chartTools } from '../site/chart-tools.js';
import { DATASET_TEXT } from '../site/describe.js';
import { engineHref, REPO_URL } from '../site/layout.js';
import { ENGINE_COLOR, ENGINE_LABEL } from '../engines.js';
import { ms, pct, ratio, usd, usdPerHour } from '../format.js';
import { PLOT_WIDTH, engineColor, TIP_TEXT } from '../plot-helpers.js';
import { CLICKBENCH } from '../constants.js';
import { PATTERN_LABELS } from '../data.js';
import { replaceHash } from '../url-state.js';

const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const sqlStr = s => `'${String(s).replace(/'/g, "''")}'`;
const q = sql => vg.coordinator().query(sql, { type: 'json' });

/** Engine ids in the page's fixed engine order. */
const PLATFORMS = Object.keys(ENGINE_LABEL);

/** Suites, by `block` id. */
const SUITES = [
  { id: 'tpcds', label: 'TPC-DS', term: 'tpcds', note: '99 join queries on uniform data' },
  { id: 'dsb', label: 'DSB', term: 'dsb', note: '102 join queries on skewed data' },
  { id: 'clickbench', label: 'ClickBench', term: 'clickbench', note: '43 scan queries on one table' }
];
const SUITE_LABEL = Object.fromEntries(SUITES.map(s => [s.id, s.label]));

/** Data volumes for the join suites. */
const VOLUMES = [
  { sf: '1', label: '1 GB' },
  { sf: '10', label: '10 GB' },
  { sf: '100', label: '100 GB' },
  { sf: '1000', label: '1 TB' }
];
const VOLUME_LABEL = Object.fromEntries(VOLUMES.map(v => [v.sf, v.label]));

/** Every dialect a query has a SQL file for: the canonical text first. */
const DIALECTS = [['duckdb', 'DuckDB (canonical)'], ...PLATFORMS.map(p => [p, ENGINE_LABEL[p]])];

/** `210.47` -> `210×`, `3.48` -> `3.5×`: whole numbers once the spread reaches ten. */
const spreadText = v => `${v.toLocaleString('en-US', { maximumFractionDigits: v >= 10 ? 0 : 1 })}×`;

/**
 * Reader labels for the raw pattern tags. The shared map covers the six the
 * analysis pools; the two rarer tags the registry can emit are named here.
 */
const EXTRA_PATTERNS = { recursive_cte: 'Recursive CTE', lateral_join: 'Lateral join' };
const patternName = p => PATTERN_LABELS[p] ?? EXTRA_PATTERNS[p] ?? String(p).replace(/_/g, ' ');
/** `tpcds_q07` -> `TPC-DS 7`. */
const queryName = (block, number) => `${SUITE_LABEL[block] ?? block} ${number}`;

// ── Page state, mirrored into the hash ───────────────────────────────────
const DEFAULTS = { suite: 'tpcds', sf: '100', pattern: '', q: '', engine: '', sort: 'id:asc', search: '' };
const state = { ...DEFAULTS };

function readHash() {
  const p = new URLSearchParams(location.hash.replace(/^#/, ''));
  const suite = p.get('suite');
  if (SUITES.some(s => s.id === suite)) state.suite = suite;
  const sf = p.get('sf');
  if (VOLUMES.some(v => v.sf === sf)) state.sf = sf;
  state.pattern = p.get('pattern') ?? '';
  state.q = p.get('q') ?? '';
  const engine = p.get('engine') ?? '';
  state.engine = PLATFORMS.includes(engine) ? engine : '';
  const sort = p.get('sort');
  const [key, dir] = (sort ?? '').split(':');
  if ((key === 'id' || key === 'spread' || PLATFORMS.includes(key)) && (dir === 'asc' || dir === 'desc')) state.sort = sort;
  state.search = p.get('search') ?? '';
}

function writeHash() {
  const p = new URLSearchParams();
  p.set('suite', state.suite);
  if (state.suite !== 'clickbench') p.set('sf', state.sf);
  for (const k of ['pattern', 'q', 'engine', 'search']) if (state[k]) p.set(k, state[k]);
  if (state.sort !== DEFAULTS.sort) p.set('sort', state.sort);
  const hash = `#${p.toString()}`;
  replaceHash('queries', hash);
}

/** The `dataset` value the current suite and volume select. */
const dataset = () => (state.suite === 'clickbench' ? CLICKBENCH : state.sf);

/** Words for the current selection, printed under exported figures. */
function selectionText({ withQuery = false } = {}) {
  const parts = [state.suite === 'clickbench'
    ? DATASET_TEXT[CLICKBENCH]
    : `${SUITE_LABEL[state.suite]} at about ${VOLUME_LABEL[state.sf]} (SF${state.sf})`];
  if (state.pattern) parts.push(`queries tagged ${patternName(state.pattern).toLowerCase()}`);
  if (state.search) parts.push(`matching "${state.search}"`);
  if (withQuery && state.q) parts.push(`query ${state.q}`);
  parts.push(state.suite === 'clickbench'
    ? 'single-user median server-side time at each engine\'s entry tier'
    : 'single-user median server-side time at each engine\'s designated size');
  return parts.join(' · ');
}

// ── SQL ──────────────────────────────────────────────────────────────────

/**
 * One cell per (engine, query, data set). `query_results` already holds one
 * row per cell; the grouping only guards against a duplicate campaign row.
 * `ms` is NULL where every measured run failed: the engine did not finish it.
 */
const CELLS_VIEW = `
  CREATE OR REPLACE VIEW queries_cells AS
  SELECT engine, platform, block, query_id, any_value(number) AS number, dataset,
         any_value(scale_factor) AS scale_factor,
         median(median_server_ms) AS ms, avg(mean_cost_usd) AS cost,
         sum(failures) AS failures
  FROM query_results GROUP BY engine, platform, block, query_id, dataset`;

/** The detail chart's sources: join suites across volumes, ClickBench at entry tier. */
const DETAIL_VIEWS = [
  `CREATE OR REPLACE VIEW queries_ds_cells AS
   SELECT * FROM queries_cells WHERE block IN ('tpcds', 'dsb') AND ms IS NOT NULL`,
  `CREATE OR REPLACE VIEW queries_cb_cells AS
   SELECT * FROM queries_cells WHERE block = 'clickbench' AND ms IS NOT NULL`
];

/** The catalog rows the filters keep. */
function catalogWhere() {
  const parts = [`q.block = ${sqlStr(state.suite)}`];
  if (state.pattern) parts.push(`list_contains(q.patterns, ${sqlStr(state.pattern)})`);
  const s = state.search.trim().toLowerCase();
  if (s) {
    const n = Number(s.replace(/\D/g, ''));
    const byNumber = /\d/.test(s) && Number.isFinite(n) ? ` OR q.number = ${n}` : '';
    parts.push(`(lower(q.query_id) LIKE ${sqlStr(`%${s}%`)}${byNumber})`);
  }
  return parts.join(' AND ');
}

/** Cells for the filtered queries at the current data set. */
const filteredCells = () => `
  SELECT c.* FROM queries_cells c
  JOIN query_catalog q USING (query_id)
  WHERE ${catalogWhere()} AND c.dataset = ${sqlStr(dataset())}`;

/** One row per filtered query, one column per engine. */
function matrixSql({ order = true } = {}) {
  const cols = PLATFORMS.map(p => `
      max(c.ms) FILTER (WHERE c.platform = '${p}') AS "${p}",
      count(c.platform) FILTER (WHERE c.platform = '${p}') > 0 AS "${p}_measured"`).join(',');
  const [key, dir] = state.sort.split(':');
  const by = key === 'id' ? 'q.number' : key === 'spread' ? 'spread' : `"${key}"`;
  return `
    SELECT q.query_id, q.block, q.number, q.patterns,${cols},
           min(c.ms) AS fastest_ms,
           arg_min(c.engine, c.ms) FILTER (WHERE c.ms IS NOT NULL) AS fastest,
           max(c.ms) / nullif(min(c.ms), 0) AS spread
    FROM query_catalog q
    LEFT JOIN queries_cells c ON c.query_id = q.query_id AND c.dataset = ${sqlStr(dataset())}
    WHERE ${catalogWhere()}
    GROUP BY q.query_id, q.block, q.number, q.patterns
    ${order ? `ORDER BY ${by} ${dir.toUpperCase()} NULLS LAST, q.number` : ''}`;
}

/** Per engine: queries where it was fastest, its median ratio to the fastest, not finished. */
const summarySql = () => `
  WITH c AS (${filteredCells()}),
  f AS (SELECT query_id, min(ms) AS best FROM c WHERE ms IS NOT NULL GROUP BY 1)
  SELECT c.engine, c.platform,
         count(*) AS measured,
         count(*) FILTER (WHERE c.ms IS NOT NULL AND c.ms = f.best) AS fastest,
         median(c.ms / f.best) FILTER (WHERE c.ms IS NOT NULL) AS median_ratio,
         count(*) FILTER (WHERE c.ms IS NULL) AS not_finished
  FROM c LEFT JOIN f USING (query_id)
  GROUP BY c.engine, c.platform
  ORDER BY fastest DESC, median_ratio ASC NULLS LAST`;

/** Every size the single-user pass ran this query on, at the current data set. */
const sizesSql = id => `
  SELECT engine, platform, compute_label, usd_per_hour, median_server_ms, mean_cost_usd, runs, failures
  FROM ladder
  WHERE query_id = ${sqlStr(id)} AND dataset = ${sqlStr(dataset())}
  ORDER BY list_position(${`[${PLATFORMS.map(sqlStr).join(', ')}]`}, platform), usd_per_hour NULLS LAST`;

/** The detail chart's rows, for its CSV. */
const detailCsvSql = id => `
  SELECT engine, block, query_id, dataset, scale_factor, ms AS median_server_ms, cost AS mean_cost_usd, failures
  FROM queries_cells WHERE query_id = ${sqlStr(id)}
  ORDER BY engine, scale_factor`;

/** This query's DSB-against-TPC-DS single-user result per engine at the current volume. */
const skewSql = number => `
  SELECT engine, platform, tpcds_server_ms, dsb_server_ms, delta_pct
  FROM skew_warm WHERE qnum = ${Number(number)} AND dataset = ${sqlStr(state.sf)}
  ORDER BY list_position(${`[${PLATFORMS.map(sqlStr).join(', ')}]`}, platform)`;

// ── Rendering ────────────────────────────────────────────────────────────

/**
 * Background for a time cell, relative to the row's fastest: white at the
 * fastest, deepening with each tenfold, capped at a hundredfold.
 */
function heat(v, best) {
  if (v == null || best == null || best <= 0) return '';
  const steps = Math.min(Math.log10(v / best) / 2, 1);
  if (steps <= 0) return '';
  return `background: rgba(213, 94, 0, ${(0.06 + steps * 0.3).toFixed(3)})`;
}

function renderFilters() {
  const suiteChips = SUITES.map(s => `<button type="button" class="choice" data-suite="${s.id}"
      aria-pressed="${state.suite === s.id}" data-tip="${esc(s.note)}">${s.label}</button>`).join('');
  const cb = state.suite === 'clickbench';
  const volumeChips = VOLUMES.map(v => `<button type="button" class="choice" data-sf="${v.sf}"
      aria-pressed="${!cb && state.sf === v.sf}" ${cb ? 'disabled' : ''}>${v.label}</button>`).join('');
  $('f-suite').innerHTML = suiteChips;
  $('f-volume').innerHTML = volumeChips;
  $('f-volume-note').hidden = !cb;
  $('f-pattern').value = state.pattern;
  $('f-engine').value = state.engine;
  $('f-search').value = state.search;
}

async function renderTable() {
  const rows = await q(matrixSql());
  const [{ total }] = await q(`SELECT count(*) AS total FROM query_catalog WHERE block = ${sqlStr(state.suite)}`);
  const [key, dir] = state.sort.split(':');
  const arrow = k => (k === key ? (dir === 'asc' ? ' ▲' : ' ▼') : '');
  const aria = k => (k === key ? ` aria-sort="${dir === 'asc' ? 'ascending' : 'descending'}"` : '');
  const hl = p => (p === state.engine ? ' hl' : '');
  const head = `
    <tr>
      <th class="sortable" data-sort="id"${aria('id')} tabindex="0">Query${arrow('id')}</th>
      <th>Patterns</th>
      ${PLATFORMS.map(p => `<th class="num sortable${hl(p)}" data-sort="${p}"${aria(p)} tabindex="0">
        <span class="swatch" style="background:${ENGINE_COLOR[ENGINE_LABEL[p]]}"></span>${ENGINE_LABEL[p]}${arrow(p)}</th>`).join('')}
      <th>Fastest</th>
      <th class="num sortable" data-sort="spread"${aria('spread')} tabindex="0" data-tip="Slowest completed time divided by the fastest">Spread${arrow('spread')}</th>
    </tr>`;
  const body = rows.map(r => {
    const best = r.fastest_ms == null ? null : Number(r.fastest_ms);
    const cells = PLATFORMS.map(p => {
      const v = r[p] == null ? null : Number(r[p]);
      if (!r[`${p}_measured`]) return `<td class="num na${hl(p)}" data-p="${p}" data-tip="Not measured at this volume">·</td>`;
      if (v == null) return `<td class="num dnf${hl(p)}" data-p="${p}">did not finish</td>`;
      const fastest = best != null && v === best;
      return `<td class="num${hl(p)}${fastest ? ' best' : ''}" data-p="${p}" data-ms="${v}" style="${heat(v, best)}">${ms(v)}</td>`;
    }).join('');
    const tags = (r.patterns ?? []).length ? Array.from(r.patterns).map(patternName).join(', ') : '<span class="muted-text">none</span>';
    const sel = r.query_id === state.q ? ' selected' : '';
    return `<tr class="qrow${sel}" data-q="${esc(r.query_id)}" tabindex="0">
      <td class="qid">${esc(queryName(r.block, r.number))}</td>
      <td class="tags">${tags}</td>${cells}
      <td>${esc(r.fastest ?? 'none')}</td>
      <td class="num">${r.spread == null ? '' : spreadText(Number(r.spread))}</td>
    </tr>`;
  }).join('');
  $('q-table-body').innerHTML = rows.length
    ? `<table class="data-table qtable no-stack"><thead>${head}</thead><tbody>${body}</tbody></table>`
    : '<p class="empty">No query matches these filters. Clear the pattern or the search to see more.</p>';
  $('q-count').textContent = `Showing ${rows.length} of ${total} ${SUITE_LABEL[state.suite]} queries.`;
  return rows;
}

async function renderSummary() {
  const rows = await q(summarySql());
  if (!rows.length) {
    $('q-summary-body').innerHTML = '<p class="empty">No query matches these filters.</p>';
    return;
  }
  const n = Math.max(...rows.map(r => Number(r.measured)));
  $('q-summary-body').innerHTML = `
    <table class="data-table">
      <thead><tr>
        <th>Engine</th>
        <th class="num">Fastest on</th>
        <th class="num">Median time against the fastest</th>
        <th class="num">Did not finish</th>
      </tr></thead>
      <tbody>${rows.map(r => `<tr${r.platform === state.engine ? ' class="lead"' : ''}>
        <td><span class="swatch" style="background:${ENGINE_COLOR[r.engine]}"></span><a href="${engineHref(r.platform)}">${esc(r.engine)}</a></td>
        <td class="num" data-fastest="${Number(r.fastest)}">${Number(r.fastest)} of ${Number(r.measured)}</td>
        <td class="num">${r.median_ratio == null ? '' : ratio(Number(r.median_ratio))}</td>
        <td class="num">${Number(r.not_finished)}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  $('q-summary-note').textContent = `Counts are over the ${n} queries shown in the table above. `
    + 'Ties count for every engine that shares the fastest time. A median of 1× means the engine was usually the fastest; 2× means its typical query took twice as long as the fastest engine\'s.';
}

// ── Detail panel ─────────────────────────────────────────────────────────

const DETAIL_SOURCE = {};
/** The query the detail panel shows; a new Selection on each visit (see mount). */
let $query;

/** Detail chart geometry, shared by the plot and the label spreader. */
const DS_HEIGHT = 300;
const DS_MARGIN = { top: 24, bottom: 40 };
/** Minimum vertical gap between two end-of-line labels, in pixels. */
const LABEL_GAP_PX = 13;

/**
 * Nudge end-of-line labels apart on a log axis so that engines finishing close
 * together stay readable. Works in pixel space against the plot's own pinned
 * domain, then maps back to data values; the points themselves never move.
 */
function spreadLabels(ends, [lo, hi]) {
  const inner = DS_HEIGHT - DS_MARGIN.top - DS_MARGIN.bottom;
  const toPx = v => ((Math.log(hi) - Math.log(v)) / (Math.log(hi) - Math.log(lo))) * inner;
  const toV = px => Math.exp(Math.log(hi) - (px / inner) * (Math.log(hi) - Math.log(lo)));
  const items = ends.map(e => ({ ...e, px: toPx(e.ms) })).sort((a, b) => a.px - b.px);
  for (let i = 1; i < items.length; i++) {
    if (items[i].px - items[i - 1].px < LABEL_GAP_PX) items[i].px = items[i - 1].px + LABEL_GAP_PX;
  }
  // Pushed past the bottom: shift the whole stack back up.
  const overflow = items.length ? items[items.length - 1].px - inner : 0;
  if (overflow > 0) items.forEach(it => { it.px -= overflow; });
  return items.map(it => ({ ...it, label_ms: toV(it.px) }));
}

/**
 * The join-suite detail chart: median time against data volume, one line per
 * engine. Its rows and its spread-out end labels are staged as two small
 * tables named for the query, so a chart for a different query never reads a
 * result Mosaic cached for the previous one.
 */
async function joinDetailChart(id, rows) {
  const vals = rows.map(r => r.ms);
  const domain = [Math.min(...vals) / 1.4, Math.max(...vals) * 1.4];
  const last = new Map();
  for (const r of rows) if (!last.has(r.engine) || r.scale_factor > last.get(r.engine).scale_factor) last.set(r.engine, r);
  const labels = spreadLabels([...last.values()], domain);
  const key = id.replace(/[^a-z0-9_]/gi, '');
  const pts = `queries_d_${key}`;
  const lab = `queries_l_${key}`;
  const mc = vg.coordinator();
  await mc.exec(`CREATE OR REPLACE TABLE ${pts} AS SELECT engine, scale_factor, ms, cost FROM queries_ds_cells WHERE query_id = ${sqlStr(id)}`);
  const values = labels.map(l => `(${sqlStr(l.engine)}, ${Number(l.scale_factor)}, ${Number(l.label_ms)})`).join(', ');
  await mc.exec(`CREATE OR REPLACE TABLE ${lab} AS SELECT * FROM (VALUES ${values}) t(engine, scale_factor, label_ms)`);
  return vg.plot(
    vg.lineY(vg.from(pts), { x: 'scale_factor', y: 'ms', stroke: 'engine', strokeWidth: 2 }),
    vg.dot(vg.from(pts), {
      x: 'scale_factor', y: 'ms', fill: 'engine', r: 3.5,
      channels: { 'mean cost per run': 'cost' },
      tip: { ...TIP_TEXT, format: { x: d => VOLUME_LABEL[String(d)] ?? `SF${d}`, y: ms, 'mean cost per run': usd } }
    }),
    vg.text(vg.from(lab), {
      x: 'scale_factor', y: 'label_ms', text: 'engine', fill: 'engine',
      dx: 8, textAnchor: 'start', fontSize: 12, fontWeight: 600,
      stroke: 'white', strokeWidth: 3, paintOrder: 'stroke'
    }),
    vg.xScale('log'),
    vg.xDomain([0.7, 1400]),
    vg.xTicks([1, 10, 100, 1000]),
    vg.xTickFormat(d => VOLUME_LABEL[String(d)] ?? ''),
    vg.xLabel('Data volume →'),
    vg.yScale('log'),
    vg.yDomain(domain),
    vg.yLabel('↑ Median server-side time'),
    vg.yTickFormat(ms),
    vg.grid(true),
    ...engineColor(),
    vg.width(Math.min(PLOT_WIDTH, 760)),
    vg.height(DS_HEIGHT),
    vg.marginTop(DS_MARGIN.top),
    vg.marginLeft(64),
    vg.marginRight(96),
    vg.marginBottom(DS_MARGIN.bottom)
  );
}

function detailPlots() {
  const cbChart = vg.plot(
    vg.barX(vg.from('queries_cb_cells', { filterBy: $query }), {
      x: 'ms', y: 'engine', fill: 'engine', sort: { y: 'x' },
      channels: { 'mean cost per run': 'cost' },
      tip: { ...TIP_TEXT, format: { x: ms, 'mean cost per run': usd } }
    }),
    vg.text(vg.from('queries_cb_cells', { filterBy: $query }), {
      x: 'ms', y: 'engine', text: vg.sql`CASE WHEN ms >= 1000 THEN printf('%.1f s', ms / 1000) ELSE printf('%d ms', CAST(round(ms) AS INTEGER)) END`,
      dx: 6, textAnchor: 'start', fontSize: 12, fill: '#04192b'
    }),
    // Linear from zero: a bar's length is its time, which a log axis (with no
    // zero to start a bar from) cannot show.
    vg.xScale('linear'),
    vg.xLabel('Median server-side time at the entry tier →'),
    vg.xTickFormat(ms),
    vg.yLabel(null),
    vg.xGrid(true),
    ...engineColor(),
    vg.width(Math.min(PLOT_WIDTH, 760)),
    vg.height(260),
    vg.marginLeft(96),
    vg.marginRight(70),
    vg.marginBottom(40)
  );
  return { cbChart };
}

async function renderDetail() {
  const card = $('q-detail');
  const id = state.q;
  const [row] = id ? await q(`SELECT * FROM query_catalog WHERE query_id = ${sqlStr(id)}`) : [];
  if (!row) {
    $('q-detail-title').textContent = 'Pick a query to see it on every engine';
    $('q-detail-empty').hidden = false;
    $('q-detail-body').hidden = true;
    card.classList.add('empty-detail');
    return;
  }
  card.classList.remove('empty-detail');
  $('q-detail-empty').hidden = true;
  $('q-detail-body').hidden = false;
  const name = queryName(row.block, row.number);
  const cb = row.block === 'clickbench';
  $('q-detail-title').textContent = `${name} on every engine`;
  const tags = Array.from(row.patterns ?? []);
  $('q-detail-tags').innerHTML = tags.length
    ? `Pattern tags: ${tags.map(t => `<b>${esc(patternName(t))}</b>`).join(', ')}.`
    : 'This query carries no pattern tags.';

  $query.update(clausePoint(vg.column('query_id'), id, { source: DETAIL_SOURCE }));
  if (!cb) {
    const rows = (await q(`SELECT engine, scale_factor, ms, cost FROM queries_ds_cells
                             WHERE query_id = ${sqlStr(id)} ORDER BY engine, scale_factor`))
      .map(r => ({ engine: r.engine, scale_factor: Number(r.scale_factor), ms: Number(r.ms), cost: r.cost == null ? null : Number(r.cost) }));
    $('q-detail-ds').replaceChildren(rows.length
      ? await joinDetailChart(id, rows)
      : Object.assign(document.createElement('p'), { className: 'empty', textContent: 'No engine finished this query at any data volume.' }));
  }
  $('q-detail-ds').hidden = cb;
  $('q-detail-cb').hidden = !cb;
  $('q-detail-chart-note').innerHTML = cb
    ? 'Each bar is one engine at its <span data-term="clickbench">ClickBench</span> <span data-term="entry-tier">entry tier</span> on the fixed table. Engines that did not finish the query have no bar.'
    : 'Each line is one engine at its <span data-term="designated">designated size</span> for each data volume, so the size grows with the data. Both axes are logarithmic. A missing point is a volume where the engine did not finish the query or was not measured.';

  // Every size at the selected volume.
  const sizes = await q(sizesSql(id));
  const vol = cb ? 'the fixed ClickBench table' : `about ${VOLUME_LABEL[state.sf]}`;
  $('q-sizes-title').textContent = `Every compute size this query ran on, at ${vol}`;
  $('q-sizes').innerHTML = sizes.length ? `
    <div class="table-scroll"><table class="data-table">
      <thead><tr><th>Engine</th><th>Compute size</th><th class="num">List price per hour</th>
        <th class="num">Median time</th><th class="num">Mean cost per run</th></tr></thead>
      <tbody>${sizes.map(s => {
        const dnf = s.median_server_ms == null;
        return `<tr class="size-row${dnf ? ' muted' : ''}">
          <td><span class="swatch" style="background:${ENGINE_COLOR[s.engine]}"></span>${esc(s.engine)}</td>
          <td>${esc(s.compute_label)}</td>
          <td class="num">${s.usd_per_hour == null ? '' : usdPerHour(Number(s.usd_per_hour))}</td>
          <td class="num">${dnf ? 'did not finish' : ms(Number(s.median_server_ms))}</td>
          <td class="num">${s.mean_cost_usd == null ? '' : usd(Number(s.mean_cost_usd))}</td>
        </tr>`;
      }).join('')}</tbody>
    </table></div>`
    : '<p class="empty">The single-user pass has no per-size results for this query at this volume.</p>';

  // SQL for every dialect.
  $('q-sql').innerHTML = DIALECTS.map(([d, label]) =>
    `<li><a class="sql-link" data-dialect="${d}" href="${REPO_URL}/blob/main/${esc(row.sql_path.replace('{platform}', d))}">${esc(label)}</a></li>`).join('');

  // The twin in the other join suite, and this pair's skew result.
  const twinBox = $('q-twin');
  if (cb) {
    twinBox.hidden = true;
  } else {
    twinBox.hidden = false;
    const other = row.block === 'tpcds' ? 'dsb' : 'tpcds';
    const [twin] = await q(`SELECT query_id FROM query_catalog WHERE block = '${other}' AND number = ${Number(row.number)}`);
    const skew = twin ? await q(skewSql(row.number)) : [];
    const link = twin ? `<a href="#" data-goto="${esc(twin.query_id)}" data-goto-suite="${other}">${esc(queryName(other, row.number))}</a>` : '';
    $('q-twin-text').innerHTML = twin
      ? `This query's twin is ${link}: the same report on the same tables, with ${other === 'dsb' ? 'skewed' : 'uniform'} data instead of ${other === 'dsb' ? 'uniform' : 'skewed'}. The table compares the two single-user times at about ${esc(VOLUME_LABEL[state.sf])}. A negative change means the skewed version ran faster.`
      : `DSB ${row.number} has no TPC-DS twin: DSB adds three queries (100 to 102) that TPC-DS does not have.`;
    $('q-skew').innerHTML = skew.length ? `
      <div class="table-scroll"><table class="data-table">
        <thead><tr><th>Engine</th><th class="num">TPC-DS (uniform)</th><th class="num">DSB (skewed)</th><th class="num">Change with skew</th></tr></thead>
        <tbody>${skew.map(s => `<tr>
          <td><span class="swatch" style="background:${ENGINE_COLOR[s.engine]}"></span>${esc(s.engine)}</td>
          <td class="num">${s.tpcds_server_ms == null ? '' : ms(Number(s.tpcds_server_ms))}</td>
          <td class="num">${s.dsb_server_ms == null ? '' : ms(Number(s.dsb_server_ms))}</td>
          <td class="num">${s.delta_pct == null ? '' : `${Number(s.delta_pct) > 0 ? '+' : ''}${pct(Number(s.delta_pct))}`}</td>
        </tr>`).join('')}</tbody>
      </table></div>`
      : (twin ? '<p class="empty">No engine has both versions of this pair measured at this volume.</p>' : '');
  }
}

// ── Wiring ───────────────────────────────────────────────────────────────

async function refresh({ detail = false } = {}) {
  writeHash();
  renderFilters();
  const rows = await renderTable();
  await renderSummary();
  // A selected query the filters no longer show stays selected: its detail
  // panel is still correct, and dropping it would lose the reader's place.
  if (detail || !state.q) await renderDetail();
  return rows;
}

function selectQuery(id, { scroll = true } = {}) {
  state.q = id;
  writeHash();
  for (const tr of document.querySelectorAll('.qrow')) tr.classList.toggle('selected', tr.dataset.q === id);
  renderDetail().then(() => {
    if (scroll) $('q-detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
}

function wire(signal) {
  $('f-suite').addEventListener('click', e => {
    const b = e.target.closest('[data-suite]');
    if (!b || b.dataset.suite === state.suite) return;
    state.suite = b.dataset.suite;
    refresh();
  });
  $('f-volume').addEventListener('click', e => {
    const b = e.target.closest('[data-sf]');
    if (!b || b.disabled || b.dataset.sf === state.sf) return;
    state.sf = b.dataset.sf;
    refresh({ detail: true });
  });
  $('f-pattern').addEventListener('change', e => { state.pattern = e.target.value; refresh(); });
  $('f-engine').addEventListener('change', e => { state.engine = e.target.value; refresh(); });
  let t;
  signal.addEventListener('abort', () => clearTimeout(t));
  $('f-search').addEventListener('input', e => {
    clearTimeout(t);
    t = setTimeout(() => { state.search = e.target.value; refresh(); }, 200);
  });
  $('f-clear').addEventListener('click', () => {
    Object.assign(state, { pattern: '', search: '', engine: '', sort: DEFAULTS.sort });
    refresh();
  });

  const sortBy = th => {
    const key = th.dataset.sort;
    const [cur, dir] = state.sort.split(':');
    // A fresh column sorts the natural way: ids up, times and spread from the top.
    const first = key === 'id' ? 'asc' : key === 'spread' ? 'desc' : 'asc';
    state.sort = `${key}:${cur === key ? (dir === 'asc' ? 'desc' : 'asc') : first}`;
    refresh();
  };
  const table = $('q-table-body');
  table.addEventListener('click', e => {
    const th = e.target.closest('th[data-sort]');
    if (th) return sortBy(th);
    const tr = e.target.closest('tr.qrow');
    if (tr) selectQuery(tr.dataset.q);
  });
  table.addEventListener('keydown', e => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const th = e.target.closest('th[data-sort]');
    const tr = e.target.closest('tr.qrow');
    if (th || tr) e.preventDefault();
    if (th) sortBy(th);
    else if (tr) selectQuery(tr.dataset.q);
  });
  $('q-detail').addEventListener('click', e => {
    const a = e.target.closest('[data-goto]');
    if (!a) return;
    e.preventDefault();
    state.suite = a.dataset.gotoSuite;
    state.q = a.dataset.goto;
    refresh({ detail: true });
  });
}

/**
 * Render this page into its HTML; src/site/router.js calls it on every visit.
 * @param {{signal: AbortSignal}} ctx `signal` aborts when the reader leaves the page.
 */
export default async function mount({ signal }) {
  Object.assign(state, DEFAULTS);
  readHash();
  $query = vg.Selection.single();
  await vg.coordinator().exec(CELLS_VIEW);
  for (const v of DETAIL_VIEWS) await vg.coordinator().exec(v);

  // Pattern and engine menus.
  const patterns = await q(`SELECT DISTINCT unnest(patterns) AS p FROM query_catalog ORDER BY 1`);
  $('f-pattern').innerHTML = '<option value="">Any pattern</option>'
    + patterns.map(r => `<option value="${esc(r.p)}">${esc(patternName(r.p))}</option>`).join('');
  $('f-engine').innerHTML = '<option value="">No engine highlighted</option>'
    + PLATFORMS.map(p => `<option value="${p}">${ENGINE_LABEL[p]}</option>`).join('');

  const { cbChart } = detailPlots();
  $('q-detail-cb').append(cbChart);

  wire(signal);
  await refresh({ detail: true });

  chartTools($('q-table'), {
    subtitle: () => selectionText(),
    csv: () => matrixSql().replace(/,\s*count\(c\.platform\) FILTER \(WHERE c\.platform = '[a-z]+'\) > 0 AS "[a-z]+_measured"/g, ''),
    file: 'query-times',
    target: $('q-table-body')
  });
  chartTools($('q-summary'), {
    subtitle: () => selectionText(),
    csv: () => summarySql(),
    file: 'fastest-engine-counts',
    target: $('q-summary-body')
  });
  chartTools($('q-detail'), {
    subtitle: () => selectionText({ withQuery: true }),
    csv: () => (state.q ? detailCsvSql(state.q) : 'SELECT 1 AS no_query_selected WHERE false'),
    file: 'query-detail',
    target: $('q-detail-figure')
  });
}
