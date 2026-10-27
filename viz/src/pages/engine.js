/**
 * One page per engine, all from one template: `engine.html#engine=<id>`.
 *
 * The page answers the questions a reader brings about one warehouse: which
 * sizes were measured, how speed and cost change with size at each data volume, how many
 * people one unit served, which kinds of query are hard for it, whether skewed
 * data slows it, and where it stands among the seven. Text from the buyer's
 * guide is labelled as such; every number is computed here, in DuckDB, from
 * the same exports the rest of the site reads.
 *
 * State lives in the hash (`engine`, `sf`, `sla`). The charts are Mosaic
 * clients filtered by two Selections (engine, data volume) and one Param (the
 * latency target), so switching engine re-queries in place; the tables are
 * plain SQL re-run on every change.
 */
import * as vg from '@uwdata/vgplot';
import { clausePoints } from '@uwdata/mosaic-core';
import { chartTools } from '../site/chart-tools.js';
import { describeSelection, fmtSla, DATASET_TEXT } from '../site/describe.js';
import { ENGINE_NOTES, IDLE_RULES_AS_OF } from '../site/engine-notes.js';
import { LADDER_GAP_REASONS, LADDER_NOTES } from '../site/ladder-caveats.js';
import { bindTerms } from '../site/glossary.js';
import { ENGINE_LABEL, ENGINE_COLOR, ENGINES } from '../engines.js';
import { ms, pct, ratio, roundDollars, usd, usdExact, usdPerHour } from '../format.js';
import { computeColor, PRICE_RAMP_LABEL, ringKey, sessionsAxis, PLOT_WIDTH, roundTicks, TIP_TEXT } from '../plot-helpers.js';
import { PASS, skipFor, skipLines, skipsSql } from '../site/skips.js';
import { CLICKBENCH, DEFAULT_SLA_MS, ENGINE_PRICE_LABELS, LADDER_COST_COLOR, LADDER_SPEED_COLOR, SEPARATE_SESSION, UNFINISHED_TIE_PP } from '../constants.js';
import { PATTERN_LABELS } from '../data.js';
import { replaceHash } from '../url-state.js';

const $ = id => document.getElementById(id);
const q = sql => vg.coordinator().query(sql, { type: 'json' }).then(rows => Array.from(rows));
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const num = v => (v == null ? null : Number(v));
const lit = s => `'${String(s).replace(/'/g, "''")}'`;

/** The data sets, in reading order, with the short names the tables use. */
const DATASETS = ['1', '10', '100', '1000', CLICKBENCH];
const VOL = { 1: '1 GB', 10: '10 GB', 100: '100 GB', 1000: '1 TB', [CLICKBENCH]: 'ClickBench' };
const JOIN_DATASETS = ['1', '10', '100', '1000'];

/** Query-pattern labels (as `pattern_load.pattern` carries them) and palette: the Compare page's. */
const PATTERNS = Object.values(PATTERN_LABELS);
const PATTERN_COLORS = ['#4477AA', '#EE6677', '#228833', '#CCBB44', '#66CCEE', '#AA3377'];

/** Least gap between two size names on the not-completed chart, in percentage points (about 10 px at its height). */
const SHED_LABEL_GAP = 5;

/** The four join-suite panels every engine's ladder shows, measured or not. */
const LADDER_VOLUMES = [1, 10, 100, 1000];

/** Ladder series: how much faster, and how much more a full run cost. */
const LADDER_SERIES = ['Speedup', 'Cost multiple'];
const LADDER_COLORS = [LADDER_SPEED_COLOR, LADDER_COST_COLOR];

/** Scan queries are compared under this hourly budget, as in the buyer's guide. */
const SCAN_BUDGET_USD = 3;

/** Plots narrower than this stack their data-volume panels vertically. */
const FACET_ROW_MIN_WIDTH = 640;
/** Whether the ladder stacks its panels; set on each visit, from the width measured then. */
let stacked = false;

/** `ENTERPRISE / 200 slots` -> `200 slots`, `X-Small (Gen2)` -> `X-Small`. */
const SHORT_LABEL = col => `regexp_replace(regexp_replace(${col}, '^ENTERPRISE / ', ''), ' \\(Gen2\\)$', '')`;

// ── State ───────────────────────────────────────────────────────────────
function readHash() {
  const p = new URLSearchParams(location.hash.replace(/^#/, ''));
  const engine = ENGINE_LABEL[p.get('engine')] ? p.get('engine') : 'snowflake';
  const sf = DATASETS.includes(p.get('sf')) ? p.get('sf') : '100';
  const raw = Number(p.get('sla'));
  const sla = Number.isFinite(raw) && raw >= 500 && raw <= 20000 ? raw : DEFAULT_SLA_MS;
  return { engine, sf, sla };
}

const state = readHash();

function writeHash() {
  const p = new URLSearchParams();
  p.set('engine', state.engine);
  p.set('sf', state.sf);
  p.set('sla', String(state.sla));
  const hash = `#${p.toString()}`;
  replaceHash('engine', hash);
}

const label = () => ENGINE_LABEL[state.engine];
const useful = sla => `coalesce(max(concurrency) FILTER (WHERE p50_ms <= ${sla} AND shed_pct < sla_max_shed_pct), 0)`;

// ── Views this page adds ────────────────────────────────────────────────
const VIEWS = [
  // Ladders with more than one size at a data volume, as two series per size:
  // speedup over the cheapest size and the cost multiple of the same full run.
  `CREATE OR REPLACE VIEW engine_ladder_long AS
     WITH l AS (
       SELECT *, count(*) OVER (PARTITION BY platform, dataset) AS sizes
       FROM ladder_suite WHERE kind = 'join'
     )
     -- A volume with one size keeps its point: the panel shows what ran there.
     SELECT engine, platform, dataset, scale_factor, compute_label, usd_per_hour,
            ${SHORT_LABEL('compute_label')} AS short_label,
            'Speedup' AS measure, speedup AS value
     FROM l
     UNION ALL
     SELECT engine, platform, dataset, scale_factor, compute_label, usd_per_hour,
            ${SHORT_LABEL('compute_label')}, 'Cost multiple', cost_multiple
     FROM l WHERE sizes > 1 AND cost_multiple IS NOT NULL`,

  // One line of text for each ladder panel with nothing to compare: a volume
  // where one size ran, or none did.
  `CREATE OR REPLACE VIEW engine_ladder_gaps AS
     WITH p AS (SELECT DISTINCT platform FROM rate_card),
     v AS (SELECT unnest([${LADDER_VOLUMES.join(', ')}]) AS scale_factor),
     l AS (
       SELECT platform, scale_factor, count(*) AS sizes, any_value(${SHORT_LABEL('compute_label')}) AS only_size
       FROM ladder_suite WHERE kind = 'join' GROUP BY platform, scale_factor
     )
     SELECT p.platform, v.scale_factor,
            CASE WHEN l.sizes IS NULL THEN 'Not measured at this volume'
                 ELSE 'Only ' || l.only_size || ' ran here,' || chr(10) || 'so there is nothing to compare' END AS msg
     FROM p CROSS JOIN v LEFT JOIN l USING (platform, scale_factor)
     WHERE coalesce(l.sizes, 0) <= 1`,

  `CREATE OR REPLACE VIEW engine_scan_sizes AS
     SELECT *, ${SHORT_LABEL('compute_label')} AS short_label
     FROM size_typical WHERE kind = 'scan' AND usd_per_hour IS NOT NULL`,
  `CREATE OR REPLACE VIEW engine_scan_separate AS
     SELECT * FROM engine_scan_sizes WHERE separate_session`,

  // End-of-line labels for the load chart: each size at the last level it ran.
  `CREATE OR REPLACE VIEW engine_conc_ends AS
     SELECT *, ${SHORT_LABEL('compute_label')} AS short_label
     FROM concurrency
     QUALIFY concurrency = max(concurrency) OVER (PARTITION BY platform, dataset, compute_label)`,

  // Each size named at the end of its line on the not-completed chart. Several
  // sizes often end at the same share (0% most of all), so the labels are
  // spread upward from the lowest by at least SHED_LABEL_GAP points:
  // y_i = max(v_i, y_(i-1) + gap), in closed form max_(j<=i)(v_j - j*gap) + i*gap.
  `CREATE OR REPLACE VIEW engine_shed_ends AS
     WITH last AS (
       SELECT *, ${SHORT_LABEL('compute_label')} AS short_label FROM concurrency
       QUALIFY concurrency = max(concurrency) OVER (PARTITION BY platform, dataset, compute_label)
     ),
     e AS (SELECT *, row_number() OVER (PARTITION BY platform, dataset ORDER BY shed_pct, usd_per_hour) AS i FROM last)
     SELECT *, max(shed_pct - i * ${SHED_LABEL_GAP}) OVER (PARTITION BY platform, dataset ORDER BY i
              ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) + i * ${SHED_LABEL_GAP} AS label_pct
     FROM e`,

  // Pattern profile pooled across sizes: the ratio is formed inside each cell,
  // so taking its median across sizes is legitimate (see data.js).
  `CREATE OR REPLACE VIEW engine_pattern AS
     SELECT engine, platform, dataset, pattern, concurrency,
            median(rel) AS r, count(*) AS sizes, max(n) AS n
     FROM pattern_load GROUP BY ALL`,

  `CREATE OR REPLACE VIEW engine_pattern_ends AS
     SELECT *, pattern AS pattern_text FROM engine_pattern
     QUALIFY concurrency = max(concurrency) OVER (PARTITION BY platform, dataset, pattern)`
];

// ── Selections the charts listen to, new on each visit (see mount) ───────
let $eng, $ds, $engDs, $sla;
function selections() {
  $eng = vg.Selection.single();
  $ds = vg.Selection.single();
  $engDs = vg.Selection.intersect({ include: [$eng, $ds] });
  $sla = vg.Param.value(state.sla);
}
const SRC_ENGINE = { id: 'engine-page-engine' };
const SRC_DATASET = { id: 'engine-page-dataset' };

function publishFilters() {
  $eng.update(clausePoints([vg.column('platform')], [[state.engine]], { source: SRC_ENGINE }));
  $ds.update(clausePoints([vg.column('dataset')], [[state.sf]], { source: SRC_DATASET }));
}

// ── Page skeleton ───────────────────────────────────────────────────────
function skeleton() {
  $('engine-root').innerHTML = `
  <section class="engine-intro" id="eng-summary">
    <h1 id="eng-title"></h1>
    <p class="lede" id="eng-about"></p>
    <div class="engine-summary-grid">
      <div class="callout guide-box">
        <p class="eyebrow">Buying notes</p>
        <dl id="eng-guide"></dl>
      </div>
      <div>
        <h2 class="subhead">How the study ran it</h2>
        <ul id="eng-howwe"></ul>
        <h2 class="subhead">How it bills <span data-term="idle-billing">idle time</span></h2>
        <dl id="eng-idle"></dl>
        <p class="source-note">From vendor documentation as of ${IDLE_RULES_AS_OF}; the benchmark did not measure idle time.</p>
      </div>
    </div>
    <p class="engine-links" id="eng-links"></p>
  </section>

  <section class="card" id="eng-sizes">
    <h2 id="eng-sizes-title"></h2>
    <p class="question">Every <span data-term="size">compute size</span> the study ran, with the vendor's
    <span data-term="list-price">list price per hour</span> and where each size appears in the results.
    The <span data-term="single-user">single-user pass</span> runs each query on its own; the
    <span data-term="concurrency-grid">concurrency grid</span> runs bursts of up to 100 queries at once.</p>
    <div class="figure table-scroll" id="eng-sizes-table"></div>
  </section>

  <section class="card" id="eng-ladder">
    <h2 id="eng-ladder-title"></h2>
    <p class="question">For each data volume, each size ran the same
    <span data-term="join-queries">join queries</span> on its own. The chart compares every size with the
    cheapest one measured at that volume: the blue line is its <span data-term="speedup">speedup</span>
    (how many times faster it ran the suite) and the orange line its cost multiple (how many times as much
    the same run cost). Where the blue line is above the orange one, the speed increased by more than the cost,
    and there is value in sizing up to achieve speedier responses. When the orange line is above the blue line,
    it is not cost-effective to increase warehouse size to achieve faster query response times.</p>
    <div class="figure">
      <p class="callout" id="eng-ladder-note" hidden></p>
      <div id="eng-ladder-legend"></div>
      <div id="eng-ladder-chart"></div>
    </div>
    <p class="note" id="eng-ladder-empty" hidden></p>
    <div class="note" id="eng-ladder-skips" hidden></div>
    <div class="table-scroll" id="eng-ladder-table"></div>
    <p class="source-note"><span data-term="suite-time">Suite time</span> is summed over the queries every
    size at that volume completed, so each size is timed on the same work; the number of queries is shown.</p>
  </section>

  <section class="card" id="eng-scan">
    <h2 id="eng-scan-title"></h2>
    <p class="question">The <span data-term="clickbench">ClickBench</span>
    <span data-term="scan-queries">scan queries</span> run over one fixed table of about 100 GB, so the only
    thing that changes is the size. Each point is one size: its <span data-term="typical-query">typical query
    time</span> against its list price per hour. The time axis has the same range on every engine's page.</p>
    <div class="figure"><div id="eng-scan-chart"></div><p class="source-note" id="eng-scan-separate" hidden></p></div>
    <p class="note" id="eng-scan-empty" hidden></p>
  </section>

  <section class="card" id="eng-concurrency">
    <h2 id="eng-conc-title"></h2>
    <p class="question">A size <span data-term="useful-concurrency">served</span> a number of people when the
    median response stayed under the <span data-term="latency-target">latency target</span> and at least 95%
    of the queries finished. Move the target and every figure below recomputes.</p>
    <div class="engine-controls">
      <label class="ctl-inline">Latency target
        <input type="range" id="eng-sla" min="500" max="20000" step="500">
        <output id="eng-sla-value"></output>
      </label>
    </div>
    <div class="figure table-scroll" id="eng-conc-table"></div>
    <div class="note" id="eng-conc-skips" hidden></div>
    <div class="callout" id="eng-conc-caveat"></div>
  </section>

  <section class="card" id="eng-load">
    <h2 id="eng-load-title"></h2>
    <p class="question">Each line is one size, darker for a higher hourly price, named at its end. The red
    dashed line is the latency target; a red open ring marks a level where 5% or more of the queries
    <span data-term="not-completed">did not complete</span>, which never counts as served.</p>
    <div class="ds-control">
      <p class="ctl-label" id="eng-ds-label">Data volume for this chart and the query-pattern chart below</p>
      <div class="choices" id="eng-ds-choices" role="group" aria-labelledby="eng-ds-label"></div>
    </div>
    <div class="figure">
      <p class="figure-label" id="eng-load-label"></p>
      <div id="eng-load-legend"></div>
      <div id="eng-load-chart"></div>
      <p class="figure-label">Share of queries not completed at each level. Dashed line: the 5% threshold.</p>
      <div id="eng-shed-chart"></div>
    </div>
    <p class="note" id="eng-load-empty" hidden></p>
  </section>

  <section class="card" id="eng-patterns">
    <h2 id="eng-pat-title"></h2>
    <p class="question">Each line is one <span data-term="pattern">query pattern</span>. Its height is that
    pattern's latency divided by this engine's typical query at the same size and number of sessions, so 1×
    is that engine's typical query, and the chart compares the engine with itself. Above 1× a pattern is harder
    than average; a line that rises with sessions degrades faster than the rest of the workload. The
    median is taken across all measured sizes.</p>
    <div class="ds-control">
      <p class="ctl-label" id="eng-pat-ds-label">Data volume for this chart and the response-time chart above</p>
      <div class="choices" id="eng-pat-ds-choices" role="group" aria-labelledby="eng-pat-ds-label"></div>
    </div>
    <div class="callout" id="eng-pat-cb" hidden>
      <p>Query patterns are tagged on the join queries (TPC-DS and DSB) only, so ClickBench has no pattern chart.</p>
      <button type="button" class="choice" data-ds="100">Show 100 GB</button>
    </div>
    <div class="figure">
      <div id="eng-pat-legend"></div>
      <div id="eng-pat-chart"></div>
    </div>
    <p class="note" id="eng-pat-empty" hidden></p>
  </section>

  <section class="card" id="eng-skew">
    <h2 id="eng-skew-title"></h2>
    <p class="question">Each <span data-term="dsb">DSB</span> query runs the same report as its
    <span data-term="tpcds">TPC-DS</span> twin on <span data-term="skew">skewed data</span>. For one user,
    the table gives the median change in time from uniform to skewed data over the query pairs whose tables are
    the same size in both suites: below 0% the skewed version ran faster. Under load, over
    every pair, it gives the median ratio of skewed to uniform time across every size and session
    count: below 1× the skewed version ran faster.</p>
    <div class="figure table-scroll" id="eng-skew-table"></div>
  </section>

  <section class="card" id="eng-peers">
    <h2 id="eng-peers-title"></h2>
    <p class="question">Each row is one measure, with this engine's value, its place among the engines
    measured on it, and the engine that led. Rank 1 is the best on that measure. There is no overall ranking:
    which measure matters depends on your workload.</p>
    <div class="figure table-scroll" id="eng-peers-table"></div>
  </section>`;
}

// ── Charts ──────────────────────────────────────────────────────────────
/** Round tick values for a log time axis; those inside the domain are drawn. */
const TIME_TICKS = [20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000];

/**
 * One scan-chart y range for every engine: the fastest and slowest typical
 * scan time across all engines and sizes, with a margin. Fitted per engine,
 * the log axis would stretch a narrow range (Fabric's sizes are within 1.19x
 * of each other) across the full height, so a flat line would read as a cliff
 * and its one-decimal ticks would repeat ("1.4 s" twice).
 */
async function scanDomain() {
  const [r] = await q('SELECT min(typical_ms) AS lo, max(typical_ms) AS hi FROM engine_scan_sizes');
  const lo = TIME_TICKS.findLast(t => t <= num(r.lo) / 1.2) ?? num(r.lo) / 1.5;
  const hi = TIME_TICKS.find(t => t >= num(r.hi) * 1.2) ?? num(r.hi) * 1.5;
  return [lo, hi];
}

function charts(scanY) {
  const facet = stacked ? 'fy' : 'fx';
  const volFormat = d => VOL[d] ?? d;
  const ladder = vg.plot(
    vg.name('engladder'),
    vg.ruleY([1], { stroke: '#999', strokeDasharray: '4,3' }),
    vg.lineY(vg.from('engine_ladder_long', { filterBy: $eng }), {
      x: 'usd_per_hour', y: 'value', stroke: 'measure', [facet]: 'scale_factor', strokeWidth: 2, marker: 'circle'
    }),
    vg.text(vg.from('engine_ladder_long', { filterBy: $eng }), {
      x: 'usd_per_hour', y: 'value', [facet]: 'scale_factor', text: 'short_label',
      filter: vg.sql`measure = 'Speedup'`,
      dy: -10, fontSize: 11, fill: '#04192b', stroke: 'white', strokeWidth: 3, paintOrder: 'stroke'
    }),
    vg.dot(vg.from('engine_ladder_long', { filterBy: $eng }), {
      x: 'usd_per_hour', y: 'value', [facet]: 'scale_factor', fill: 'measure', r: 3.5,
      channels: { size: 'compute_label' },
      tip: { ...TIP_TEXT, format: { x: usdExact, y: ratio, [facet]: volFormat } }
    }),
    vg.text(vg.from('engine_ladder_gaps', { filterBy: $eng }), {
      [facet]: 'scale_factor', text: 'msg', frameAnchor: 'middle',
      fontSize: 12, fill: '#47506d', lineHeight: 1.3
    }),
    // Round prices, whole dollars bare: four two-decimal labels per narrow panel run together ("$3.00$4.00").
    vg.xScale('log'), vg.xLabel('List price per hour (log scale) →'), vg.xTicks(roundTicks(ENGINE_PRICE_LABELS)), vg.xTickFormat(roundDollars),
    stacked ? vg.fyDomain(LADDER_VOLUMES) : vg.fxDomain(LADDER_VOLUMES),
    // Along the axis, as on Compare: across the top it runs into the first panel's title.
    vg.yScale('log'), vg.yLabel('Speedup and cost (× cheapest size)'), vg.yLabelAnchor('center'), vg.yTickFormat(ratio), vg.yGrid(true),
    stacked ? vg.fyTickFormat(volFormat) : vg.fxTickFormat(volFormat),
    stacked ? vg.fyLabel(null) : vg.fxLabel(null),
    vg.colorDomain(LADDER_SERIES), vg.colorRange(LADDER_COLORS),
    vg.width(PLOT_WIDTH), vg.height(stacked ? 560 : 300),
    vg.marginLeft(66), vg.marginRight(stacked ? 60 : 24), vg.marginTop(24), vg.marginBottom(44)
  );
  $('eng-ladder-legend').append(vg.colorLegend({ for: 'engladder', label: null }));
  $('eng-ladder-chart').append(ladder);

  const scan = vg.plot(
    vg.lineY(vg.from('engine_scan_sizes', { filterBy: $eng }), { x: 'usd_per_hour', y: 'typical_ms', stroke: '#47506d', strokeWidth: 1.5 }),
    vg.dot(vg.from('engine_scan_sizes', { filterBy: $eng }), {
      x: 'usd_per_hour', y: 'typical_ms', fill: 'engine', r: 5,
      channels: { size: 'compute_label', 'queries completed': 'completed', 'first run': 'run_date' },
      tip: { ...TIP_TEXT, format: { x: usdExact, y: ms, fill: false } }
    }),
    // Sizes run in a separate session (constants.js SEPARATE_SESSION): a white
    // ring on top, explained in the note under the chart.
    vg.dot(vg.from('engine_scan_separate', { filterBy: $eng }), {
      x: 'usd_per_hour', y: 'typical_ms', r: 4.5, fill: 'white', stroke: '#b26a00', strokeWidth: 2
    }),
    vg.text(vg.from('engine_scan_sizes', { filterBy: $eng }), {
      x: 'usd_per_hour', y: 'typical_ms', text: 'short_label', dy: -12,
      fontSize: 11, fill: '#04192b', stroke: 'white', strokeWidth: 3, paintOrder: 'stroke'
    }),
    vg.ruleX([SCAN_BUDGET_USD], { stroke: '#999', strokeDasharray: '4,3' }),
    vg.text([{ x: SCAN_BUDGET_USD, t: `$${SCAN_BUDGET_USD} an hour` }], { x: 'x', text: 't', frameAnchor: 'bottom', dy: -6, dx: 5, textAnchor: 'start', fontSize: 11, fill: '#47506d' }),
    vg.xScale('log'), vg.xLabel('List price per hour (log scale) →'), vg.xTickFormat(usdPerHour),
    // The same range on every engine page (scanDomain), so the pages compare.
    vg.yScale('log'), vg.yDomain(scanY), vg.yTicks(TIME_TICKS.filter(t => t >= scanY[0] && t <= scanY[1])),
    vg.yLabel('↑ Typical scan query time (log scale)'), vg.yTickFormat(ms), vg.yGrid(true), vg.xInset(24),
    vg.colorDomain(ENGINES), vg.colorRange(ENGINES.map(e => ENGINE_COLOR[e])),
    vg.width(PLOT_WIDTH), vg.height(300), vg.marginLeft(70), vg.marginRight(40), vg.marginTop(40), vg.marginBottom(44)
  );
  $('eng-scan-chart').append(scan);


  const load = vg.plot(
    vg.name('engload'),
    vg.ruleY([0], { y: $sla, stroke: '#c0392b', strokeDasharray: '4,3', strokeWidth: 1.2 }),
    vg.lineY(vg.from('concurrency', { filterBy: $engDs }), {
      x: 'concurrency', y: 'p50_ms', stroke: 'usd_per_hour', z: 'compute_label', strokeWidth: 2, curve: 'monotone-x'
    }),
    vg.dot(vg.from('concurrency', { filterBy: $engDs }), {
      x: 'concurrency', y: 'p50_ms', fill: 'usd_per_hour', r: 2.5,
      channels: { size: 'compute_label', 'not completed': 'shed_pct', runs: 'runs', 'red ring': 'ring_note' },
      tip: { ...TIP_TEXT, format: { x: d => `${d} sessions`, y: ms, fill: usdExact, 'not completed': pct } }
    }),
    vg.dot(vg.from('concurrency_shed', { filterBy: $engDs }), {
      x: 'concurrency', y: 'p50_ms', stroke: '#c0392b', fill: 'white', r: 4, strokeWidth: 1.6
    }),
    vg.text(vg.from('engine_conc_ends', { filterBy: $engDs }), {
      x: 'concurrency', y: 'p50_ms', text: 'short_label', dx: 8, textAnchor: 'start',
      fontSize: 11, fontWeight: 600, fill: '#04192b', stroke: 'white', strokeWidth: 3, paintOrder: 'stroke'
    }),
    ...sessionsAxis(),
    vg.yScale('log'), vg.yLabel('↑ Median engine time, queue included (log scale)'), vg.yTickFormat(ms), vg.yGrid(true),
    ...computeColor(),
    vg.width(PLOT_WIDTH), vg.height(340), vg.marginLeft(72), vg.marginRight(90), vg.marginTop(20), vg.marginBottom(44)
  );
  $('eng-load-legend').append(vg.colorLegend({
    for: 'engload', label: PRICE_RAMP_LABEL, ticks: [0.5, 1, 2, 5, 10, 20, 50], tickFormat: usdPerHour
  }), ringKey());
  $('eng-load-chart').append(load);

  const shed = vg.plot(
    vg.ruleY([5], { stroke: '#c0392b', strokeDasharray: '4,3', strokeWidth: 1.2 }),
    vg.lineY(vg.from('concurrency', { filterBy: $engDs }), {
      x: 'concurrency', y: 'shed_pct', stroke: 'usd_per_hour', z: 'compute_label', strokeWidth: 2, curve: 'monotone-x'
    }),
    vg.dot(vg.from('concurrency', { filterBy: $engDs }), {
      x: 'concurrency', y: 'shed_pct', fill: 'usd_per_hour', r: 2.2,
      channels: { size: 'compute_label' },
      tip: { ...TIP_TEXT, format: { x: d => `${d} sessions`, y: pct, fill: usdExact } }
    }),
    // Size names at the line ends, as on the response-time chart above.
    vg.text(vg.from('engine_shed_ends', { filterBy: $engDs }), {
      x: 'concurrency', y: 'label_pct', text: 'short_label', dx: 8, textAnchor: 'start',
      fontSize: 11, fontWeight: 600, fill: '#04192b', stroke: 'white', strokeWidth: 3, paintOrder: 'stroke'
    }),
    ...sessionsAxis(),
    vg.yScale('linear'), vg.yDomain([0, 100]), vg.yLabel('↑ Queries not completed'), vg.yTickFormat(pct), vg.yGrid(true),
    ...computeColor(),
    // Tall enough that size names 5 points apart do not touch.
    vg.width(PLOT_WIDTH), vg.height(270), vg.marginLeft(72), vg.marginRight(90), vg.marginTop(26), vg.marginBottom(44)
  );
  $('eng-shed-chart').append(shed);

  const patterns = vg.plot(
    vg.name('engpat'),
    vg.ruleY([1], { stroke: '#999', strokeDasharray: '4,3' }),
    vg.lineY(vg.from('engine_pattern', { filterBy: $engDs }), {
      x: 'concurrency', y: 'r', stroke: 'pattern', strokeWidth: 1.8, curve: 'monotone-x'
    }),
    vg.dot(vg.from('engine_pattern', { filterBy: $engDs }), {
      x: 'concurrency', y: 'r', fill: 'pattern', r: 2.5,
      channels: { 'sizes pooled': 'sizes', 'queries with the tag': 'n' },
      tip: { ...TIP_TEXT, format: { x: d => `${d} sessions`, y: ratio } }
    }),
    vg.text(vg.from('engine_pattern_ends', { filterBy: $engDs }), {
      x: 'concurrency', y: 'r', text: 'pattern_text', dx: 8, textAnchor: 'start',
      fontSize: 11, fill: 'pattern', stroke: 'white', strokeWidth: 3, paintOrder: 'stroke'
    }),
    ...sessionsAxis(),
    vg.yScale('log'), vg.yLabel("↑ Latency against this engine's typical query"), vg.yTickFormat(ratio), vg.yGrid(true),
    vg.yTicks([0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 8]),
    vg.colorDomain(PATTERNS), vg.colorRange(PATTERN_COLORS),
    vg.width(PLOT_WIDTH), vg.height(320), vg.marginLeft(72), vg.marginRight(110), vg.marginTop(20), vg.marginBottom(44)
  );
  $('eng-pat-legend').append(vg.colorLegend({ for: 'engpat', label: 'Query pattern' }));
  $('eng-pat-chart').append(patterns);
}

// ── Tables and text ─────────────────────────────────────────────────────
/** A data table; a column's optional `tip` shows as a hover note on its heading. */
function table(head, rows, { caption = '' } = {}) {
  const th = head.map(h => `<th class="${h.num ? 'num' : ''}"${h.tip ? ` tabindex="0" data-tip="${esc(h.tip)}"` : ''}>${h.label}</th>`).join('');
  const body = rows.map(r => `<tr class="${r.cls ?? ''}">${r.cells.map((c, i) => `<td class="${head[i].num ? 'num' : ''}">${c}</td>`).join('')}</tr>`).join('');
  return `<table class="data-table"><thead><tr>${th}</tr></thead><tbody>${body}</tbody>${caption ? `<caption>${caption}</caption>` : ''}</table>`;
}

const volList = datasets => DATASETS.filter(d => datasets.includes(d)).map(d => VOL[d]).join(', ') || 'Not in the published results';

function renderSummary() {
  const n = ENGINE_NOTES[state.engine];
  $('eng-title').textContent = label();
  $('eng-about').textContent = n.about;
  $('eng-guide').innerHTML = [
    ['Pick it when', n.pickWhen], ['Watch out for', n.watchOut], ['Start at', n.startAt]
  ].map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join('');
  $('eng-howwe').innerHTML = n.howWeRan.map(t => `<li>${esc(t)}</li>`).join('');
  $('eng-idle').innerHTML = `<dt>What you pay for</dt><dd>${esc(n.idle.pays)}</dd><dt>When the warehouse is quiet</dt><dd>${esc(n.idle.quiet)}</dd>`;
  $('eng-links').innerHTML = [
    `<a href="compare.html#engines=${encodeURIComponent(label())}&sf=${state.sf}&sla=${state.sla}">Compare ${esc(label())} with every engine</a>`,
    `<a href="queries.html#engine=${state.engine}">Browse ${esc(label())}'s query results</a>`,
    '<a href="method.html#downloads">Download the data</a>'
  ].join('');
  const L = esc(label());
  $('eng-sizes-title').textContent = `Which sizes of ${label()} were measured`;
  $('eng-ladder-title').textContent = `How speed and cost change with ${label()} size at each data volume`;
  $('eng-scan-title').textContent = `How fast each ${label()} size ran the scan queries, against its price`;
  $('eng-conc-title').textContent = `How many people each ${label()} size served within the latency target`;
  $('eng-load-title').textContent = `How ${label()}'s response time changed as sessions were added`;
  $('eng-pat-title').textContent = `Which kinds of query are hard for ${label()} under load`;
  $('eng-skew-title').textContent = `Does skewed data slow ${label()} down?`;
  $('eng-peers-title').textContent = `How ${label()} compares with the other six engines`;
  $('eng-conc-caveat').innerHTML = `<b>How the study ran ${L} under load:</b> ${esc(n.underLoad ?? n.howWeRan[0])}`;
  document.title = `${label()} results · 2026 Cloud Data Warehouse Benchmark`;
}

async function renderSizes() {
  const P = lit(state.engine);
  const rows = await q(`
    WITH w AS (
      SELECT compute_label, list(DISTINCT CASE WHEN block = 'clickbench' THEN '${CLICKBENCH}' ELSE CAST(scale_factor AS VARCHAR) END) AS ds
      FROM size_ladder WHERE platform = ${P} GROUP BY 1
    ),
    c AS (SELECT compute_label, list(DISTINCT dataset) AS ds FROM concurrency WHERE platform = ${P} GROUP BY 1)
    SELECT r.compute_label, r.billing_unit, r.usd_per_hour, w.ds AS warm, c.ds AS grid
    FROM rate_card r LEFT JOIN w USING (compute_label) LEFT JOIN c USING (compute_label)
    WHERE r.platform = ${P}
    ORDER BY r.usd_per_hour NULLS LAST, r.compute_label`);
  $('eng-sizes-table').innerHTML = table(
    [{ label: 'Size' }, { label: 'Billing unit' }, { label: 'List price per hour', num: true },
      { label: 'Single-user pass' }, { label: 'Concurrency grid' }],
    rows.map(r => ({
      cells: [
        esc(r.compute_label), esc(r.billing_unit),
        r.usd_per_hour == null ? 'Billed by bytes scanned' : usdExact(num(r.usd_per_hour)),
        esc(volList(Array.from(r.warm ?? []))), esc(volList(Array.from(r.grid ?? [])))
      ]
    })),
    { caption: 'List prices as recorded July to October 2026. The concurrency grid lists only sizes with an hourly price.' }
  );
}

async function renderLadder() {
  const P = lit(state.engine);
  const rows = await q(`
    WITH l AS (SELECT *, count(*) OVER (PARTITION BY dataset) AS sizes FROM ladder_suite WHERE kind = 'join' AND platform = ${P})
    SELECT * FROM l ORDER BY scale_factor, usd_per_hour`);
  const multi = rows.filter(r => num(r.sizes) > 1);
  const measured = new Set(multi.map(r => num(r.scale_factor)));
  const gaps = LADDER_VOLUMES.filter(v => !measured.has(v));
  $('eng-ladder-chart').parentElement.hidden = !rows.length;
  // The gap note is built from the data, so it shrinks as volumes are filled
  // and disappears when none is left. Only a reason the data cannot give comes
  // from ladder-caveats.js.
  const grid = gaps.length ? await q(`
    SELECT dataset, count(DISTINCT compute_label) AS n FROM concurrency
    WHERE platform = ${P} AND dataset IN (${gaps.map(v => lit(String(v))).join(', ')}) GROUP BY dataset`) : [];
  const gridSizes = v => num(grid.find(g => g.dataset === String(v))?.n ?? 0);
  const ran = v => {
    const one = rows.find(r => num(r.scale_factor) === v);
    const why = LADDER_GAP_REASONS[state.engine]?.[v]?.html;
    const what = one ? `At ${VOL[v]} the single-user pass ran only ${esc(one.compute_label)}.` : (why ? '' : `At ${VOL[v]} the single-user pass has no ${esc(label())} result.`);
    const more = gridSizes(v) > 1 ? `The concurrency grid below ran ${gridSizes(v)} sizes there.` : '';
    return [what, why ?? '', more].filter(Boolean).join(' ').trim();
  };
  const empty = $('eng-ladder-empty');
  empty.hidden = !gaps.length;
  empty.innerHTML = gaps.length
    ? `<b>Why ${gaps.map(v => VOL[v]).join(' and ')} ${gaps.length > 1 ? 'have' : 'has'} no ladder:</b> ${gaps.map(ran).join(' ')}`
    : '';
  await renderSkips('eng-ladder-skips', PASS.single, 'single-user tests');
  const note = $('eng-ladder-note');
  note.hidden = !LADDER_NOTES[state.engine];
  note.innerHTML = LADDER_NOTES[state.engine]?.html ?? '';
  $('eng-ladder-table').innerHTML = rows.length ? table(
    [{ label: 'Data volume' }, { label: 'Size' }, { label: 'List price per hour', num: true },
      { label: 'Queries completed', num: true }, { label: 'Suite time (queries)', num: true },
      { label: 'Speedup', num: true }, { label: 'Cost multiple', num: true }, { label: 'Speedup exceeds cost multiple' }],
    rows.map(r => {
      const only = num(r.sizes) === 1;
      const base = !only && r.compute_label === r.base_label;
      const sp = num(r.speedup);
      const cm = num(r.cost_multiple);
      return {
        cls: base || only ? 'muted' : '',
        cells: [
          VOL[r.dataset], esc(r.compute_label), usdExact(num(r.usd_per_hour)),
          `${num(r.completed)} of ${num(r.attempted)}`,
          `${num(r.suite_s).toLocaleString(undefined, { maximumFractionDigits: 1 })} s (${num(r.queries)})`,
          only ? '' : ratio(sp), only ? '' : cm == null ? 'No cost recorded' : ratio(cm),
          only ? 'Only size run at this volume' : base ? 'Cheapest size, the baseline' : cm == null ? '' : sp > cm ? 'Yes' : 'No'
        ]
      };
    }),
    {}
  ) : '';
}

async function renderScan() {
  const [row] = await q(`SELECT count(*) AS n FROM engine_scan_sizes WHERE platform = ${lit(state.engine)}`);
  const n = num(row?.n ?? 0);
  $('eng-scan-chart').parentElement.hidden = n === 0;
  const empty = $('eng-scan-empty');
  empty.hidden = n > 1;
  empty.textContent = n === 0
    ? `${label()} has no priced ClickBench sizes in the single-user pass.`
    : `${label()} ran ClickBench on one priced size only.`;
}

async function renderConcurrency() {
  const P = lit(state.engine);
  const rows = await q(`
    SELECT compute_label, any_value(usd_per_hour) AS usd, dataset, ${useful(state.sla)} AS served
    FROM concurrency WHERE platform = ${P} GROUP BY compute_label, dataset`);
  const sizes = [...new Map(rows.map(r => [r.compute_label, num(r.usd)])).entries()].sort((a, b) => a[1] - b[1]);
  const cols = DATASETS.filter(d => rows.some(r => r.dataset === d));
  const cell = (s, d) => rows.find(r => r.compute_label === s && r.dataset === d);
  const best = Object.fromEntries(cols.map(d => [d, Math.max(...rows.filter(r => r.dataset === d).map(r => num(r.served)))]));
  const skips = await q(skipsSql(state.engine));
  $('eng-conc-table').innerHTML = rows.length ? table(
    [{ label: 'Size' }, { label: 'List price per hour', num: true }, ...cols.map(d => ({ label: VOL[d], num: true }))],
    sizes.map(([s, usdH]) => ({
      cells: [esc(s), usdExact(usdH), ...cols.map(d => {
        const c = cell(s, d);
        const skip = c ? null : skipFor(skips, { sf: d, label: s, pass: PASS.load });
        if (skip) return `<span class="dim" data-tip="${esc(plainSkip(skip))}" data-tip-title="Not run on purpose">Skipped</span>`;
        if (!c) return '<span class="dim">Not run</span>';
        const v = num(c.served);
        return v > 0 && v === best[d] ? `<b>${v}</b>` : String(v);
      })]
    })),
    { caption: `People served within a ${fmtSla(state.sla)} median target, with at least 95% of queries completed. Bold: the most any size served at that data volume. "Not run": the size is not in the published grid at that volume. "Skipped": not run on purpose, for the reason given below the table.` }
  ) : `<p class="note">${esc(label())} has no published concurrency results.</p>`;
  await renderSkips('eng-conc-skips', PASS.load, 'tests under load');
}

/** The recorded reason for one skip, as a reader sees it. */
const plainSkip = skip => {
  const line = skipLines([skip], skip.run_type === '*' ? PASS.load : skip.run_type)[0];
  return line ? `${line.label} ${line.reason}` : '';
};

/**
 * Lists the engine's skips for one pass under its section, with their reasons
 * (site/skips.js), so a blank reads as a decision rather than a missing run.
 */
async function renderSkips(id, pass, passName) {
  const [skips, sizes] = await Promise.all([
    q(skipsSql(state.engine)),
    q(`SELECT compute_label FROM rate_card WHERE platform = ${lit(state.engine)} ORDER BY usd_per_hour NULLS LAST`)
  ]);
  const lines = skipLines(skips, pass, sizes.map(r => r.compute_label));
  const el = $(id);
  el.hidden = !lines.length;
  el.innerHTML = lines.length
    ? `<b>Sizes left out of the ${passName} on purpose:</b><ul class="plain-list">${lines.map(l =>
      `<li><b>${esc(l.label)}</b> ${esc(l.reason)}</li>`).join('')}</ul>`
    : '';
}

async function renderLoad() {
  const [row] = await q(`SELECT count(*) AS n FROM concurrency WHERE platform = ${lit(state.engine)} AND dataset = ${lit(state.sf)}`);
  const n = num(row?.n ?? 0);
  $('eng-load-chart').closest('.figure').hidden = n === 0;
  const empty = $('eng-load-empty');
  empty.hidden = n > 0;
  empty.textContent = `${label()} has no published concurrency results for ${DATASET_TEXT[state.sf]}.`;
  $('eng-load-label').textContent = `${DATASET_TEXT[state.sf]} · ${fmtSla(state.sla)} latency target`;
  for (const b of document.querySelectorAll('#eng-ds-choices button, #eng-pat-ds-choices button')) {
    b.setAttribute('aria-pressed', String(b.dataset.ds === state.sf));
  }
}

async function renderPatterns() {
  const cb = state.sf === CLICKBENCH;
  const [row] = cb ? [{ n: 0 }] : await q(`SELECT count(*) AS n FROM engine_pattern WHERE platform = ${lit(state.engine)} AND dataset = ${lit(state.sf)}`);
  const n = num(row.n);
  $('eng-pat-chart').closest('.figure').hidden = n === 0;
  $('eng-pat-cb').hidden = !cb;
  const empty = $('eng-pat-empty');
  empty.hidden = cb || n > 0;
  empty.textContent = `${label()} has no pattern results at ${VOL[state.sf]} under load.`;
}

async function renderSkew() {
  const P = lit(state.engine);
  const rows = await q(`
    WITH w AS (SELECT dataset, median(delta_pct) AS d, count(*) AS pairs FROM skew_warm_clean WHERE platform = ${P} GROUP BY 1),
         l AS (SELECT dataset, median(skew_ratio) AS r, count(*) AS cells FROM skew_load WHERE platform = ${P} GROUP BY 1)
    SELECT coalesce(w.dataset, l.dataset) AS dataset, w.d, w.pairs, l.r, l.cells
    FROM w FULL JOIN l USING (dataset)`);
  const order = d => JOIN_DATASETS.indexOf(d);
  rows.sort((a, b) => order(a.dataset) - order(b.dataset));
  const readW = d => (d == null ? 'Not measured' : `${d > 0 ? '+' : ''}${Number(d).toFixed(1)}% (${d < 0 ? 'skewed ran faster' : d > 0 ? 'skewed ran slower' : 'no change'})`);
  const readL = r => (r == null ? 'Not measured' : `${ratio(r)} (${r < 1 ? 'skewed ran faster' : r > 1 ? 'skewed ran slower' : 'no change'})`);
  $('eng-skew-table').innerHTML = rows.length ? table(
    [{ label: 'Data volume' }, { label: 'One user: median change', num: true }, { label: 'Query pairs', num: true },
      { label: 'Under load: median ratio', num: true }, { label: 'Measurements under load', num: true, tip: 'The number of results behind this median: one for each compute size at each number of people querying at once (1, 5, 10, 20, 50 and 100). It is lower wherever a size was not run at that data volume.' }],
    rows.map(r => ({
      cls: (num(r.d) > 0 || num(r.r) > 1) ? 'lead' : '',
      cells: [VOL[r.dataset], readW(num(r.d)), r.pairs == null ? '' : num(r.pairs), readL(num(r.r)), r.cells == null ? '' : num(r.cells)]
    })),
    { caption: rows.some(r => num(r.d) > 0 || num(r.r) > 1) ? 'Highlighted rows: skewed data ran slower on at least one of the two measures.' : '' }
  ) : `<p class="note">${esc(label())} has no skew pairs in the results.</p>`;
}

/**
 * One measure across engines: the rows are (engine, value) with the value
 * already computed; `lower` says whether smaller is better.
 */
function rankRow(title, rows, fmt, { lower = true, extra = () => '', missing = 'Not measured', tieName = () => '', tieWithin = 0 } = {}) {
  const valid = rows.filter(r => r.v != null && Number.isFinite(num(r.v)));
  const sorted = [...valid].sort((a, b) => (lower ? num(a.v) - num(b.v) : num(b.v) - num(a.v)));
  const mine = sorted.find(r => r.engine === label());
  // Values within `tieWithin` of each other tie (0: only equal values do).
  const same = (a, b) => Math.abs(num(a) - num(b)) <= tieWithin;
  const leaders = sorted.filter(r => sorted.length && same(r.v, sorted[0].v));
  const rank = mine ? sorted.filter(r => !same(r.v, mine.v) && (lower ? num(r.v) < num(mine.v) : num(r.v) > num(mine.v))).length + 1 : null;
  const tied = rank != null && sorted.filter(r => same(r.v, mine.v)).length > 1;
  const leaderText = !leaders.length ? ''
    : leaders.length === 1 ? `${esc(leaders[0].engine)}, ${fmt(num(leaders[0].v), leaders[0])}`
      // Tied leaders each carry their own detail (the size that got there), and
      // the shared value is printed without any one engine's detail.
      : leaders.length <= 3 ? `${leaders.map(r => `${esc(r.engine)}${tieName(r)}`).join(' and ')} tied, ${[...new Set(leaders.map(r => fmt(num(r.v), {})))].join(' and ')}`
        : `${leaders.length} engines tied, ${fmt(num(leaders[0].v), leaders[0])}`;
  return {
    cls: rank === 1 ? 'lead' : '',
    cells: [
      title,
      mine ? `${fmt(num(mine.v), mine)}${extra(mine)}` : `<span class="dim">${missing}</span>`,
      mine ? `${rank} of ${sorted.length}${tied ? ' (tied)' : ''}` : '',
      leaderText
    ]
  };
}

/** The people count behind the peers table's cheapest-size row (Finding 2's middle column). */
const CHEAP_SERVE_PEOPLE = 50;

async function renderPeers() {
  const sla = state.sla;
  const [typical, cost, fails, served, scanFast, scanBudget, cost1tb, growth, cheap50, loadFails, slowdown] = await Promise.all([
    q(`SELECT engine, dataset, typical_ms AS v FROM engine_scale WHERE dataset <> '${CLICKBENCH}'`),
    q(`SELECT engine, frontier_cost AS v, cost_confidence FROM engine_scale WHERE dataset = '100'`),
    q(`SELECT engine, 100.0 * sum(failures) / sum(runs) AS v FROM warm_runs WHERE dataset <> '${CLICKBENCH}' GROUP BY 1`),
    // The size named is the cheapest that reached the engine's best level, as
    // Finding 2 and the Compare page name it (arg_max over a tie picks any).
    q(`WITH c AS (SELECT engine, compute_label, any_value(usd_per_hour) AS usd, ${useful(sla)} AS s
                FROM concurrency WHERE dataset = '100' GROUP BY 1, 2),
            m AS (SELECT engine, max(s) AS v FROM c GROUP BY 1)
       SELECT m.engine, m.v, arg_min(c.compute_label, c.usd) AS size
       FROM m JOIN c ON c.engine = m.engine AND c.s = m.v GROUP BY 1, 2`),
    q(`SELECT engine, min(typical_ms) AS v, arg_min(compute_label, typical_ms) AS size FROM size_typical WHERE kind = 'scan' AND usd_per_hour IS NOT NULL GROUP BY 1`),
    q(`SELECT engine, min(typical_ms) AS v, arg_min(compute_label, typical_ms) AS size FROM size_typical WHERE kind = 'scan' AND usd_per_hour <= ${SCAN_BUDGET_USD} GROUP BY 1`),
    q(`SELECT engine, frontier_cost AS v, cost_confidence FROM engine_scale WHERE dataset = '1000'`),
    // How many times as long the typical join query took at 1 TB as at 1 GB, each at its designated size.
    q(`SELECT engine, max(typical_ms) FILTER (WHERE dataset = '1000') / max(typical_ms) FILTER (WHERE dataset = '1') AS v
       FROM engine_scale WHERE dataset IN ('1', '1000') GROUP BY 1`),
    q(`WITH c AS (SELECT engine, compute_label, any_value(usd_per_hour) AS usd, ${useful(sla)} AS s
                FROM concurrency WHERE dataset = '100' GROUP BY 1, 2)
       SELECT engine, min(usd) AS v, arg_min(compute_label, usd) AS size FROM c WHERE s >= ${CHEAP_SERVE_PEOPLE} GROUP BY 1`),
    q(`SELECT engine, sum(shed_pct * runs) / sum(runs) AS v FROM concurrency WHERE dataset <> '${CLICKBENCH}' GROUP BY 1`),
    // Median over each engine's join-suite cells that finished at 100 sessions of
    // the median response at 100 sessions over that at one session.
    q(`WITH c AS (SELECT engine, dataset, compute_label,
                         max(p50_ms) FILTER (WHERE concurrency = 1) AS p1,
                         max(p50_ms) FILTER (WHERE concurrency = 100) AS p100,
                         max(shed_pct) FILTER (WHERE concurrency = 100) AS shed, any_value(sla_max_shed_pct) AS cap
                  FROM concurrency WHERE dataset <> '${CLICKBENCH}' GROUP BY 1, 2, 3)
       SELECT engine, median(p100 / p1) AS v FROM c WHERE p100 IS NOT NULL AND p1 > 0 AND shed < cap GROUP BY 1`)
  ]);
  const withSize = r => (r.size ? ` (${esc(String(r.size).replace(/^ENTERPRISE \/ /, '').replace(/ \(Gen2\)$/, ''))})` : '');
  const rows = [
    ...JOIN_DATASETS.map(d => rankRow(
      `Typical join query for one user at ${VOL[d]}, at the designated size`,
      typical.filter(r => r.dataset === d), v => ms(v)
    )),
    rankRow('Mean cost per join query at 100 GB, at the designated size', cost, v => usd(v), {
      extra: r => ` <span class="dim">(${esc(r.cost_confidence)})</span>`,
      missing: 'Not measured'
    }),
    rankRow('Mean cost per join query at 1 TB, at the designated size', cost1tb, v => usd(v), {
      extra: r => ` <span class="dim">(${esc(r.cost_confidence)})</span>`,
      missing: 'Not measured'
    }),
    rankRow('How many times as long the typical join query took at 1 TB as at 1 GB, at the designated sizes', growth, v => ratio(v)),
    // Two decimals under 1%, so one failed run in 2,412 does not read as 0%.
    rankRow('Single-user join runs not completed, at the designated sizes', fails, v => (v > 0 && v < 1 ? `${v.toFixed(2)}%` : pct(v))),
    rankRow(`Most people served by one size at 100 GB (${fmtSla(sla)} target)`, served, (v, r) => `${v}${v > 0 ? withSize(r) : ''}`,
      { lower: false, tieName: r => (num(r.v) > 0 ? withSize(r) : '') }),
    rankRow(`Cheapest size that served ${CHEAP_SERVE_PEOPLE} people at once at 100 GB (${fmtSla(sla)} target)`, cheap50,
      (v, r) => `${usdExact(v)} an hour${withSize(r)}`, { missing: 'Not reached', tieName: withSize }),
    rankRow('Join queries not completed under load, across every volume, size and session count', loadFails,
      v => `${v.toFixed(2)}%`, { tieWithin: UNFINISHED_TIE_PP }),
    rankRow('How many times as long the median join query took at 100 sessions as at one, the median across sizes and volumes',
      slowdown, v => ratio(v)),
    rankRow('Typical scan query at the fastest measured size', scanFast, (v, r) => `${ms(v)}${withSize(r)}`, { tieName: withSize }),
    rankRow(`Typical scan query at a size of $${SCAN_BUDGET_USD} an hour or less`, scanBudget, (v, r) => `${ms(v)}${withSize(r)}`, { tieName: withSize })
  ];
  const noBudget = !scanBudget.some(r => r.engine === label());
  $('eng-peers-table').innerHTML = table(
    [{ label: 'Measure' }, { label: label() }, { label: 'Rank' }, { label: 'Leader' }],
    rows,
    { caption: `${noBudget ? `${esc(label())} has no measured size at $${SCAN_BUDGET_USD} an hour or less, so it has no result at that budget. ` : ''}The designated size is the one the study assigned each engine for each data volume. Cost grades: measured, allocated or modeled (see the glossary).` }
  );
}

/** The note under the scan chart for sizes run in a separate session. */
function separateNote() {
  const notes = SEPARATE_SESSION.filter(a => a.platform === state.engine && a.kind === 'scan');
  const el = $('eng-scan-separate');
  el.hidden = !notes.length;
  el.innerHTML = notes.map(a => `<b>Open rings:</b> ${esc(a.note)}`).join(' ');
}

async function renderAll() {
  separateNote();
  writeHash();
  for (const b of $('engine-choices').querySelectorAll('button')) b.setAttribute('aria-pressed', String(b.dataset.engine === state.engine));
  renderSummary();
  await Promise.all([renderSizes(), renderLadder(), renderScan(), renderConcurrency(), renderLoad(), renderPatterns(), renderSkew(), renderPeers()]);
  bindTerms(document.body);
}

// ── Controls ────────────────────────────────────────────────────────────
function controls() {
  $('engine-choices').innerHTML = Object.entries(ENGINE_LABEL).map(([id, l]) =>
    `<button type="button" class="choice" data-engine="${id}"><span class="swatch" style="background:${ENGINE_COLOR[l]}"></span>${l}</button>`).join('');
  $('engine-choices').addEventListener('click', e => {
    const b = e.target.closest('button[data-engine]');
    if (!b || b.dataset.engine === state.engine) return;
    state.engine = b.dataset.engine;
    publishFilters();
    renderAll();
  });

  // One data-volume choice drives both the response-time and the pattern
  // charts; the chips are repeated on each card and kept in step.
  const chips = DATASETS.map(d => `<button type="button" class="choice" data-ds="${d}">${VOL[d]}</button>`).join('');
  $('eng-ds-choices').innerHTML = chips;
  $('eng-pat-ds-choices').innerHTML = chips;
  const pickDataset = e => {
    const b = e.target.closest('button[data-ds]');
    if (!b || b.dataset.ds === state.sf) return;
    state.sf = b.dataset.ds;
    publishFilters();
    writeHash();
    renderLoad();
    renderPatterns();
  };
  for (const id of ['eng-ds-choices', 'eng-pat-ds-choices', 'eng-pat-cb']) $(id).addEventListener('click', pickDataset);

  const slider = $('eng-sla');
  const out = $('eng-sla-value');
  slider.value = String(state.sla);
  out.textContent = fmtSla(state.sla);
  slider.addEventListener('input', () => {
    state.sla = Number(slider.value);
    out.textContent = fmtSla(state.sla);
    $sla.update(state.sla);
    writeHash();
    renderConcurrency();
    renderLoad();
    renderPeers();
  });
}

function tools() {
  const P = () => lit(state.engine);
  const sel = extra => () => describeSelection({ engines: [label()], extra });
  chartTools($('eng-sizes'), { subtitle: sel(), csv: () => `SELECT * FROM rate_card WHERE platform = ${P()} ORDER BY usd_per_hour NULLS LAST` });
  chartTools($('eng-ladder'), { subtitle: sel(['Join queries, single user, every size against the cheapest']), csv: () => `SELECT * FROM ladder_suite WHERE kind = 'join' AND platform = ${P()} ORDER BY scale_factor, usd_per_hour` });
  chartTools($('eng-scan'), { subtitle: sel(['Scan queries (ClickBench), single user']), csv: () => `SELECT * FROM size_typical WHERE kind = 'scan' AND platform = ${P()} ORDER BY usd_per_hour` });
  chartTools($('eng-concurrency'), {
    subtitle: () => describeSelection({ sla: state.sla, engines: [label()] }),
    csv: () => `SELECT engine, compute_label, dataset, any_value(usd_per_hour) AS usd_per_hour, ${useful(state.sla)} AS people_served FROM concurrency WHERE platform = ${P()} GROUP BY ALL ORDER BY usd_per_hour, dataset`
  });
  chartTools($('eng-load'), {
    subtitle: () => describeSelection({ dataset: state.sf, sla: state.sla, engines: [label()] }),
    csv: () => `SELECT engine, dataset, compute_label, usd_per_hour, concurrency, p50_ms, shed_pct, runs FROM concurrency WHERE platform = ${P()} AND dataset = ${lit(state.sf)} ORDER BY usd_per_hour, concurrency`
  });
  chartTools($('eng-patterns'), {
    subtitle: () => describeSelection({ dataset: state.sf, engines: [label()] }),
    csv: () => `SELECT * FROM engine_pattern WHERE platform = ${P()} AND dataset = ${lit(state.sf)} ORDER BY pattern, concurrency`
  });
  chartTools($('eng-skew'), {
    subtitle: sel(['DSB (skewed) against TPC-DS (uniform)']),
    csv: () => `SELECT 'one user' AS measure, dataset, median(delta_pct) AS value, count(*) AS n FROM skew_warm_clean WHERE platform = ${P()} GROUP BY dataset
                UNION ALL SELECT 'under load', dataset, median(skew_ratio), count(*) FROM skew_load WHERE platform = ${P()} GROUP BY dataset ORDER BY 1, 2`
  });
  chartTools($('eng-peers'), {
    subtitle: () => describeSelection({ sla: state.sla, engines: [label()], extra: ['against the other six engines'] }),
    csv: () => `SELECT engine, dataset, typical_ms, frontier_ms AS median_ms, frontier_cost AS mean_cost_usd, cost_confidence, queries, failures FROM engine_scale ORDER BY dataset, engine`
  });
}

/**
 * Render this page into its HTML; src/site/router.js calls it on every visit.
 * @param {{signal: AbortSignal}} ctx `signal` aborts when the reader leaves the page.
 */
export default async function mount({ signal }) {
  Object.assign(state, readHash());
  stacked = PLOT_WIDTH < FACET_ROW_MIN_WIDTH;
  selections();
  for (const sql of VIEWS) await vg.coordinator().exec(sql);
  skeleton();
  controls();
  publishFilters();
  charts(await scanDomain());
  tools();
  await renderAll();
  window.addEventListener('hashchange', () => {
    const next = readHash();
    if (next.engine === state.engine && next.sf === state.sf && next.sla === state.sla) return;
    Object.assign(state, next);
    $('eng-sla').value = String(state.sla);
    $('eng-sla-value').textContent = fmtSla(state.sla);
    $sla.update(state.sla);
    publishFilters();
    renderAll();
  }, { signal });
}
