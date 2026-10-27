/**
 * The concurrency study: the frontier, the degradation ladders and the
 * per-pattern behaviour under load, the first section of the Compare page;
 * the single-user views are in `warm.js` and `sizing.js`.
 */
import * as vg from '@uwdata/vgplot';
import { MEASURED_LEVELS, SCAN_LEVELS, levelsFor } from '../constants.js';
import { BEHAVIOR_LABELS, PATTERN_LABELS } from '../data.js';
import { ENGINES } from '../engines.js';
import { ms, pct, ratio, usdExact, usdPerHour } from '../format.js';
import { PLOT_WIDTH, PRICE_RAMP_LABEL, computeColor, engineColor, engineFacets, priceEndLabels, sessionsAxis, sqlList, TIP_TEXT } from '../plot-helpers.js';
import { configTable } from '../config-table.js';

/**
 * How far, in ladder steps, engines are spread sideways within one level so
 * that two configurations landing on the same (sessions, cost) do not cover
 * each other. Seven engines at ±3 slots span just over half a step.
 */
const DODGE_STEP = 0.09;

/** Query-pattern tags, in the order the legend lists them. */
const PATTERNS = Object.values(PATTERN_LABELS);

/**
 * Query-pattern palette: Paul Tol's "bright" scheme, which is designed so no
 * pair collapses under colour-vision deficiency (the Tableau default puts a
 * red and a green side by side). It is also deliberately unlike
 * the engine palette: in the pattern views engine is the facet and colour means
 * pattern, and reusing the engine hues there would read as a colour-coding
 * error.
 */
const PATTERN_COLORS = ['#4477AA', '#EE6677', '#228833', '#CCBB44', '#66CCEE', '#AA3377'];

/**
 * Build the three concurrency views and their legends.
 *
 * @param {ReturnType<import('../controls.js').buildControls>} c
 * @returns {{frontierConcurrency: HTMLElement, configs: HTMLElement, sweep: HTMLElement,
 *   behaviourLegend: HTMLElement, legend: HTMLElement, computeLegend: HTMLElement,
 *   degradation: HTMLElement, shedRate: HTMLElement, skewLoad: HTMLElement,
 *   skewLegend: HTMLElement, patternLoad: HTMLElement, patternLegend: HTMLElement}}
 */
export function concurrencyViews({ $engines, $scale, $engineScale, $config, $sla, $axis, $zero }) {
  // ── 1. Concurrency Pareto frontier ─────────────────────────────────────
  // x is the useful-concurrency metric, recomputed per SLA in SQL. The FILTER
  // clause is the definition from benchmark.analysis.concurrency_shape: the
  // highest level whose median latency clears the target and whose shed rate
  // stays under the policy threshold. Verified to reproduce that reducer
  // exactly across all 146 cells (tests/integration/test_committed_results_db.py).
  // The coalesce is load-bearing: a config clearing no level would otherwise be
  // NULL, and Plot drops NULL-x marks — hiding exactly the configurations that
  // cannot meet the target.
  const usefulConcurrency = vg.sql`coalesce(max(concurrency) FILTER (
      WHERE p50_ms <= ${$sla} AND shed_pct < sla_max_shed_pct), 0)`;

  // The metric is plotted at its position on the measured ladder (0..6), not
  // at its value: equal spacing means one step up the ladder, and gridlines
  // fall only on session counts that were run. Within a step, each engine is
  // nudged sideways by a fixed slot so configurations that tie on both
  // sessions and cost (Redshift 32 RPU and MotherDuck Mega both serve 20 at
  // $12/h) stay visible instead of one covering the other.
  // What an hour of this capacity costs per analyst it can actually serve at
  // the SLA. A per-row fact for the tooltip and the table only: plotted, or
  // used as a default order, it would become the composite ranking this page
  // deliberately does not have. NULL (blank) where nothing is served.
  const usdPerSession = vg.sql`any_value(usd_per_hour) / nullif(${usefulConcurrency}, 0)`;

  // `any_value(engine)` keeps the whole expression an aggregate. Mosaic probes
  // each channel's type with `SELECT <expr>, * ... GROUP BY ALL`, and a bare
  // `engine` beside `max(...)` fails that probe with a binder error on every
  // page load (harmless to the chart, but an error in every reader's console).
  // A level's rung on the ladder. Scan queries top out at 43 (the whole suite at
  // once), which takes the rung 50 has on the join ladder.
  const rung = vg.sql`coalesce(list_position(${sqlList(MEASURED_LEVELS)}, ${usefulConcurrency}),
      list_position(${sqlList(SCAN_LEVELS)}, ${usefulConcurrency})) - 1`;
  const ladderPosition = vg.sql`${rung}
      + (list_position(${sqlList(ENGINES)}, any_value(engine)) - ${(ENGINES.length + 1) / 2}) * ${DODGE_STEP}`;
  // The rung labels of the data set on show: scan queries read 43 where the
  // join ladder reads 50, and have no 100. Plot redraws on every selection
  // change, so the labels follow the data-set menu.
  const rungLabel = i => String(levelsFor($scale.value)[i] ?? '');
  // The axis spans the rungs the data set has, so scan queries end at 43
  // rather than beside an empty column where the join ladder has 100.
  const rungs = () => levelsFor($scale.value).map((_, i) => i);
  const $rungDomain = vg.Param.value([-0.5, rungs().length - 0.5]);
  const $rungTicks = vg.Param.value(rungs());
  $scale.addEventListener('value', () => {
    $rungTicks.update(rungs());
    $rungDomain.update([-0.5, rungs().length - 0.5]);
  });

  // y is the list-price hourly rate for the compute size, so the two axes are
  // "sessions served" against "what the capacity costs" — down and to the right
  // is the frontier. One dot per (engine, compute size).
  const frontierConcurrency = vg.plot(
    vg.name('cfrontier'),
    vg.dot(vg.from('concurrency', { filterBy: $engineScale }), {
      x: ladderPosition,
      y: 'usd_per_hour',
      fill: 'engine',
      symbol: 'failure_mode',
      r: 7,
      fillOpacity: 0.85,
      stroke: 'white',
      strokeWidth: 1,
      // Tooltip-only channels. A `title` channel would replace the whole tip
      // with one string; these add to it instead, and the plotted x (a ladder
      // index) is hidden in favour of the level it stands for.
      channels: {
        'compute size': 'compute_label',
        'people served within target': usefulConcurrency,
        'USD per hour per person served': usdPerSession,
        'not completed at the most sessions run': 'shed_pct_at_max'
      },
      // Plot lists channels in declaration order, so a reader sees the engine
      // and size first, then what it served and what that costs.
      tip: {
        ...TIP_TEXT,
        format: {
          fill: true,
          'compute size': true,
          'people served within target': true,
          y: usdPerHour,
          'USD per hour per person served': usdExact,
          'not completed at the most sessions run': pct,
          x: false,
          symbol: false
        }
      }
    }),
    vg.xScale('linear'),
    vg.xDomain($rungDomain),
    vg.xTicks($rungTicks),
    vg.xTickFormat(rungLabel),
    // The full label needs about 470 px; on a phone the short one leans on
    // "How to read this chart", which says the levels are evenly spaced.
    vg.xLabel(PLOT_WIDTH < 560
      ? 'People served within the latency target →'
      : 'People served at once within the latency target (the levels tested, evenly spaced) →'),
    vg.yScale($axis),
    vg.yZero($zero),
    vg.yLabel('↑ Compute list price (USD per hour)'),
    vg.yTickFormat(usdPerHour),
    vg.yInset(20),
    vg.grid(true),
    ...engineColor(),
    vg.colorLabel('engine'),
    vg.symbolDomain(Object.values(BEHAVIOR_LABELS)),
    vg.width(PLOT_WIDTH),
    vg.height(380),
    vg.marginLeft(74),
    vg.marginBottom(40),
    vg.marginRight(30)
  );

  // The same cells as a table, sharing the aggregate so the two cannot drift.
  const configs = configTable({ filterBy: $engineScale, sla: $sla, usefulConcurrency, usdPerSession, config: $config });

  // ── 1c. One configuration across data volumes ──────────────────────────
  // Every other view fixes the scale and varies the engines. This does the
  // opposite for the one (engine, compute size) the table has selected: how
  // many sessions it serves within the SLA as the data grows from 1 GB to
  // 1 TB. It listens to $config and $sla only; filtering it by $scale would
  // leave one point. y is the ladder, as on the frontier.
  const sweep = vg.plot(
    vg.name('sweep'),
    vg.lineY(vg.from('concurrency', { filterBy: $config }), {
      x: 'scale_factor',
      y: rung,
      stroke: 'engine',
      strokeWidth: 2,
      curve: 'monotone-x'
    }),
    vg.dot(vg.from('concurrency', { filterBy: $config }), {
      x: 'scale_factor',
      y: rung,
      fill: 'engine',
      r: 5,
      channels: {
        'compute size': 'compute_label',
        'people served within target': usefulConcurrency,
        'USD per hour': vg.max('usd_per_hour'),
        'not completed at the most sessions run': vg.max('shed_pct_at_max')
      },
      tip: { ...TIP_TEXT, format: { x: d => `SF${d}`, y: false, 'USD per hour': usdExact, 'not completed at the most sessions run': pct } }
    }),
    vg.xScale('log'),
    vg.xDomain([0.7, 1400]),
    vg.xTicks([1, 10, 100, 1000]),
    vg.xTickFormat(d => `SF${d}`),
    vg.xLabel('Scale factor (data volume) →'),
    vg.yScale('linear'),
    vg.yDomain([-0.5, MEASURED_LEVELS.length - 0.5]),
    vg.yTicks(MEASURED_LEVELS.map((_, i) => i)),
    vg.yTickFormat(i => String(MEASURED_LEVELS[i] ?? '')),
    vg.yLabel('↑ People served within the target'),
    vg.yGrid(true),
    ...engineColor(),
    vg.colorLabel('engine'),
    vg.width(PLOT_WIDTH),
    vg.height(220),
    vg.marginLeft(74),
    vg.marginBottom(40),
    vg.marginRight(30)
  );

  const behaviourLegend = vg.symbolLegend({
    for: 'cfrontier',
    label: 'How the size behaved as sessions were added'
  });

  // Interactive legend: click a swatch to toggle that engine everywhere.
  const legend = vg.colorLegend({
    for: 'cfrontier',
    as: $engines,
    field: 'engine',
    columns: 4,
    label: null
  });

  // Ladders use `lineY`, not `line`: Mosaic adds an ORDER BY on x only for the
  // directional variants, and without it the points of a series are connected
  // in GROUP BY result order, which draws loops.
  // ── 2. Degradation under load ──────────────────────────────────────────
  // Small multiples, one panel per engine. Overlaid, this would be ~39 lines
  // sharing seven colours, the compute-size dimension present but unreadable
  // and the curves crossing too much to trace. Faceting separates the engines and
  // lets the within-engine ladder read as stacked curves — bigger compute sits
  // lower. The SLA rule repeats in every panel, so "where the line crosses it"
  // stays legible per engine, which is the frontier's x axis.
  //
  // Median latency alone hides the other way a level fails: queries that never
  // completed. The open rings mark levels where the shed rate reached the
  // policy threshold — the frontier never counts those levels, however low the
  // median looks.
  const degradation = vg.plot(
    vg.name('degradation'),
    vg.ruleY([0], { y: $sla, stroke: '#c0392b', strokeDasharray: '4,3', strokeWidth: 1.2 }),
    vg.lineY(vg.from('concurrency', { filterBy: $engineScale }), {
      x: 'concurrency',
      y: 'p50_ms',
      stroke: 'usd_per_hour',
      z: 'compute_label',
      fx: 'engine',
      strokeWidth: 2,
      strokeOpacity: 0.9,
      curve: 'monotone-x'
    }),
    vg.dot(vg.from('concurrency', { filterBy: $engineScale }), {
      x: 'concurrency',
      y: 'p50_ms',
      fill: 'usd_per_hour',
      fx: 'engine',
      r: 2,
      channels: { 'compute size': 'compute_label', 'not completed': 'shed_pct', 'runs': 'runs', 'red ring': 'ring_note' },
      tip: { ...TIP_TEXT, format: { fx: false, x: d => `${d} sessions`, y: ms, fill: usdExact, 'not completed': pct } }
    }),
    // Rings in the SLA-rule red: failure is one encoding, price is another, and
    // rings on the price ramp would fight it.
    vg.dot(vg.from('concurrency_shed', { filterBy: $engineScale }), {
      x: 'concurrency',
      y: 'p50_ms',
      stroke: '#c0392b',
      fill: 'white',
      fx: 'engine',
      r: 3.8,
      strokeWidth: 1.6
    }),
    ...priceEndLabels('concurrency_extremes', $engineScale, 'p50_ms'),
    ...engineFacets(),
    ...sessionsAxis(),
    vg.yScale($axis),
    // Headroom above the slowest point, so its size name (Databricks
    // "2X-Small") sits below the panel title instead of on it.
    vg.yInsetTop(16),
    vg.yZero($zero),
    vg.yLabel('Median engine time, queue included'),
    vg.yTickFormat(ms),
    vg.yGrid(true),
    ...computeColor(),
    vg.marginLeft(72)
  );

  // One ramp for every ladder view: the same shade is the same money in each.
  const computeLegend = vg.colorLegend({
    for: 'degradation',
    label: PRICE_RAMP_LABEL,
    ticks: [0.5, 1, 2, 5, 10, 20, 50],
    tickFormat: usdPerHour
  });

  // ── 2b. Shed rate under load ───────────────────────────────────────────
  // The other half of each ladder. The rings above say a level crossed the
  // policy threshold; this says by how much: ClickHouse at SF100 sheds 44 to
  // 85% at every size, which a ring cannot express. Same facets and x axis as
  // the latency ladders so the two rows read as one figure; the dashed rule is
  // the threshold the frontier's FILTER applies (`sla_max_shed_pct`, 5%).
  const shedRate = vg.plot(
    vg.ruleY(vg.from('concurrency', { filterBy: $engineScale }), {
      y: vg.max('sla_max_shed_pct'),
      stroke: '#c0392b',
      strokeDasharray: '4,3',
      strokeWidth: 1.2
    }),
    vg.lineY(vg.from('concurrency', { filterBy: $engineScale }), {
      x: 'concurrency',
      y: 'shed_pct',
      stroke: 'usd_per_hour',
      z: 'compute_label',
      fx: 'engine',
      strokeWidth: 2,
      strokeOpacity: 0.9,
      curve: 'monotone-x'
    }),
    vg.dot(vg.from('concurrency', { filterBy: $engineScale }), {
      x: 'concurrency',
      y: 'shed_pct',
      fill: 'usd_per_hour',
      fx: 'engine',
      r: 1.8,
      channels: { 'compute size': 'compute_label', 'runs': 'runs' },
      tip: { ...TIP_TEXT, format: { fx: false, x: d => `${d} sessions`, y: pct, fill: usdExact } }
    }),
    ...engineFacets(),
    ...sessionsAxis(),
    vg.height(220),
    vg.yScale('linear'),
    vg.yDomain([0, 100]),
    vg.yLabel('Queries not completed'),
    vg.yTickFormat(pct),
    vg.yGrid(true),
    ...computeColor(),
    vg.marginLeft(72)
  );

  // ── 4. What data skew costs, under load ────────────────────────────────
  // The single-user skew bars pair each DSB query with its TPC-DS twin at one
  // compute size and one session. This asks the same question of every cell in
  // the grid: `skew_ratio` is the median over paired query numbers of the
  // DSB-over-TPC-DS server time, formed per query inside one cell, so engine
  // speed, warehouse size and the level's queueing all divide out. Above 1 the
  // skewed variant is slower; a line that climbs with sessions is an optimizer
  // whose skew handling degrades under load. Same panels, x axis and colour as
  // the ladders above.
  const skewLoad = vg.plot(
    vg.name('skewload'),
    vg.ruleY([1], { stroke: '#999', strokeDasharray: '4,3' }),
    vg.lineY(vg.from('skew_load', { filterBy: $engineScale }), {
      x: 'concurrency',
      y: 'skew_ratio',
      stroke: 'usd_per_hour',
      z: 'compute_label',
      fx: 'engine',
      strokeWidth: 2,
      strokeOpacity: 0.9,
      curve: 'monotone-x'
    }),
    ...priceEndLabels('skew_load_extremes', $engineScale, 'skew_ratio'),
    vg.dot(vg.from('skew_load', { filterBy: $engineScale }), {
      x: 'concurrency',
      y: 'skew_ratio',
      fill: 'usd_per_hour',
      fx: 'engine',
      r: 1.8,
      channels: {
        'compute size': 'compute_label',
        'paired queries': 'pairs',
        'TPC-DS median': 'tpcds_ms',
        'DSB median': 'dsb_ms'
      },
      tip: { ...TIP_TEXT, format: { fx: false, x: d => `${d} sessions`, y: ratio, fill: usdExact, 'TPC-DS median': ms, 'DSB median': ms } }
    }),
    ...engineFacets(),
    ...sessionsAxis(),
    vg.yScale('log'),
    vg.yLabel('DSB ÷ TPC-DS server-side time'),
    vg.yTickFormat(ratio),
    vg.yGrid(true),
    ...computeColor(),
    vg.marginLeft(76)
  );

  // A ramp legend of its own, so a line here can be read as a size too. Same
  // ramp, same ticks, mounted above it.
  const skewLegend = vg.colorLegend({
    for: 'skewload',
    label: PRICE_RAMP_LABEL,
    ticks: [0.5, 1, 2, 5, 10, 20, 50],
    tickFormat: usdPerHour
  });

  // ── 3. Where each engine is strong, under load ─────────────────────────
  // Asked at one compute size and one session, this mostly answers "which
  // engine is fastest overall": each engine's patterns come out one flat shade.
  // Normalising inside the cell removes that: `rel` is the pattern's
  // latency against the engine's own typical query at the SAME scale, size and
  // session count, so 1x is the engine's own baseline and the line shape is the
  // engine's profile. Medianing across compute sizes is legitimate precisely
  // because the ratio is size-normalised.
  const patternLoad = vg.plot(
    vg.name('patternload'),
    vg.ruleY([1], { stroke: '#999', strokeDasharray: '4,3' }),
    vg.lineY(vg.from('pattern_load', { filterBy: $engineScale }), {
      x: 'concurrency',
      y: vg.median('rel'),
      stroke: 'pattern',
      fx: 'engine',
      strokeWidth: 1.6,
      curve: 'monotone-x'
    }),
    // Dot area is the number of warehouse sizes behind the median, so a point
    // resting on one or two cells (Fabric's text-search spike at 5×) reads as
    // thinner evidence than one pooled over seven. The tooltip gives the count
    // and the queries that carry the tag.
    vg.dot(vg.from('pattern_load', { filterBy: $engineScale }), {
      x: 'concurrency',
      y: vg.median('rel'),
      fill: 'pattern',
      fx: 'engine',
      r: vg.count(),
      channels: { 'warehouse sizes': vg.count(), 'queries with the tag': vg.max('n') },
      tip: { ...TIP_TEXT, format: { x: d => `${d} sessions`, y: ratio, r: false } }
    }),
    ...engineFacets(),
    ...sessionsAxis(),
    vg.yScale('log'),
    vg.yLabel("Latency vs this engine's typical query"),
    vg.yTickFormat(ratio),
    vg.yGrid(true),
    vg.colorDomain(PATTERNS),
    vg.colorRange(PATTERN_COLORS),
    vg.colorLabel('pattern'),
    vg.rDomain([1, 8]),
    vg.rRange([1.4, 4.2]),
    vg.marginLeft(76)
  );

  const patternLegend = vg.colorLegend({
    for: 'patternload',
    label: 'Query pattern'
  });

  return {
    frontierConcurrency, configs, sweep, behaviourLegend, legend,
    computeLegend, degradation, shedRate, skewLoad, skewLegend,
    patternLoad, patternLegend
  };
}
