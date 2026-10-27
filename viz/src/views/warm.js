/**
 * The single-query (warm) pass: one session, one designated compute size per
 * engine and scale.
 *
 * Its cost-speed frontier leads the Compare page's "at a glance" panel together
 * with two one-number-per-engine bars derived from the same pass (skew tax,
 * reliability); the other two views (latency against data volume, ClickBench
 * per query) are the behaved-as-expected check. Query patterns and skew at
 * every size are the concurrency views' job: one compute size and one session
 * is the setting those views exist to move past.
 */
import * as vg from '@uwdata/vgplot';
import { CONFIDENCE_LEVELS, ENGINES } from '../engines.js';
import { ms, usd } from '../format.js';
import { GLANCE_MAIN_WIDTH, GLANCE_NARROW, GLANCE_SIDE_WIDTH, PLOT_WIDTH, engineColor, TIP_TEXT } from '../plot-helpers.js';

/** `+12.3%` / `-4.0%`: a signed percentage from a number already in percent. */
const signedPct = d => `${d > 0 ? '+' : ''}${Number(d).toFixed(1)}%`;
const pct1 = d => `${Number(d).toFixed(1)}%`;

/**
 * Build the warm-pass views.
 *
 * @param {ReturnType<import('../controls.js').buildControls>} c
 * @returns {{latency: HTMLElement, frontier: HTMLElement, confidenceLegend: HTMLElement,
 *   skewTax: HTMLElement, reliability: HTMLElement, clickbench: HTMLElement}}
 */
export function warmViews({ $engines, $engineScale, $axis, $zero }) {
  // ── At a glance: cost vs speed ─────────────────────────────────────────
  // One point per engine at the selected scale: median over per-query median
  // server time against mean over per-query mean cost. Two dashed rules at the
  // medians of the engines on screen split the plane into quadrants; the HTML
  // captions in the corners (compare.html) name them. The rules are aggregates
  // over the filtered set, so toggling engines moves them.
  const frontier = vg.plot(
    vg.name('frontier'),
    vg.ruleX(vg.from('engine_scale', { filterBy: $engineScale }), {
      x: vg.median('frontier_ms'), stroke: '#c9ced6', strokeDasharray: '4,4'
    }),
    vg.ruleY(vg.from('engine_scale', { filterBy: $engineScale }), {
      y: vg.median('frontier_cost'), stroke: '#c9ced6', strokeDasharray: '4,4'
    }),
    // Dot shape is the cost-confidence grade (engines.js): the y axis mixes
    // measured, allocated and modeled costs, and a reader comparing two dots
    // should see that where the axis is, not in a footnote at the end.
    vg.dot(vg.from('engine_scale', { filterBy: $engineScale }), {
      x: 'frontier_ms',
      y: 'frontier_cost',
      fill: 'engine',
      symbol: 'cost_confidence',
      r: 9,
      stroke: 'white',
      strokeWidth: 1.5,
      channels: { 'cost confidence': 'cost_confidence', queries: 'queries', failures: 'failures' },
      tip: { ...TIP_TEXT, format: { x: ms, y: usd, symbol: false } }
    }),
    vg.text(vg.from('engine_scale', { filterBy: $engineScale }), {
      x: 'frontier_ms',
      y: 'frontier_cost',
      text: 'engine',
      dy: -16,
      fontSize: 12,
      fontWeight: 600,
      fill: 'currentColor',
      stroke: 'white',
      strokeWidth: 3,
      paintOrder: 'stroke'
    }),
    vg.xScale($axis),
    vg.xZero($zero),
    vg.xInset(36),
    // Extra room at the top, so the highest dot's label clears the HTML
    // corner caption ("Slower, pricier") laid over the plot.
    vg.yInsetTop(56),
    vg.yInsetBottom(28),
    vg.xLabel('Median server-side time →   (faster ←)'),
    vg.xTickFormat(ms),
    vg.yScale($axis),
    vg.yZero($zero),
    vg.yLabel('↑ Mean cost per query (USD)   (cheaper ↓)'),
    vg.yTickFormat(usd),
    vg.grid(true),
    ...engineColor(),
    vg.colorLabel('engine'),
    vg.symbolDomain(CONFIDENCE_LEVELS),
    vg.symbolRange(['circle', 'square', 'triangle']),
    vg.width(GLANCE_MAIN_WIDTH),
    vg.height(400),
    vg.marginLeft(72),
    vg.marginBottom(40),
    vg.marginTop(24),
    // The right margin holds the two right-hand corner captions; on a phone
    // they are hidden and the plot area needs the room more.
    vg.marginRight(GLANCE_NARROW ? 28 : 76)
  );

  const confidenceLegend = vg.symbolLegend({ for: 'frontier', label: 'Cost confidence (dot shape)' });

  // ── At a glance: skew tax ──────────────────────────────────────────────
  // Median over query numbers of (DSB - TPC-DS) / TPC-DS per engine, the same
  // pairing as the skew-under-load panels, at one session and the designated
  // tier. Positive bars mean skewed data ran slower. The y order is the fixed
  // engine order rather than a sort by value: the page lists facts and lets the
  // reader rank them.
  const skewTax = vg.plot(
    vg.name('skewtax'),
    vg.ruleX([0], { stroke: '#8a9099' }),
    vg.barX(vg.from('skew_warm_clean', { filterBy: $engineScale }), {
      x: vg.median('delta_pct'),
      y: 'engine',
      fill: 'engine',
      channels: {
        'paired queries': vg.count(),
        'TPC-DS median': vg.median('tpcds_server_ms'),
        'DSB median': vg.median('dsb_server_ms')
      },
      tip: { ...TIP_TEXT, format: { x: signedPct, 'TPC-DS median': ms, 'DSB median': ms } }
    }),
    // The full label needs about 330 px of this chart's width; the short one
    // leans on the caption under the chart, which already says what left of
    // zero means.
    vg.xLabel(GLANCE_SIDE_WIDTH < 340
      ? 'Median % vs uniform →'
      : '← skewed faster   median % vs uniform   skewed slower →'),
    vg.xTickFormat(d => `${d > 0 ? '+' : ''}${d}%`),
    vg.xGrid(true),
    vg.yDomain(ENGINES),
    vg.yLabel(null),
    ...engineColor(),
    vg.width(GLANCE_SIDE_WIDTH),
    vg.height(215),
    vg.marginLeft(88),
    vg.marginBottom(42),
    vg.marginTop(8)
  );

  // ── At a glance: reliability ───────────────────────────────────────────
  // Share of measured runs that did not complete, per engine at the designated
  // tier (entry tier for ClickBench): sum of per-query failures over the runs
  // those queries had (`warm_runs`, data.js). Every bar is labelled so a clean
  // engine reads as an explicit 0.0% rather than an empty row.
  const failedShare = vg.sql`100.0 * sum(failures) / sum(runs)`;
  const reliability = vg.plot(
    vg.name('reliability'),
    vg.barX(vg.from('warm_runs', { filterBy: $engineScale }), {
      x: failedShare,
      y: 'engine',
      fill: 'engine',
      channels: { queries: vg.count(), 'failed runs': vg.sum('failures') },
      tip: { ...TIP_TEXT, format: { x: pct1 } }
    }),
    vg.text(vg.from('warm_runs', { filterBy: $engineScale }), {
      x: failedShare,
      y: 'engine',
      text: vg.sql`printf('%.1f%%', ${failedShare})`,
      dx: 6,
      textAnchor: 'start',
      fontSize: 12,
      fill: 'currentColor'
    }),
    // The full 0 to 100% scale, so a 1% bar reads as small. An axis fitted
    // to the data would stretch a 1% bar across the chart, where it reads as
    // 100%. The label on every bar gives the exact share.
    vg.xScale('linear'),
    vg.xDomain([0, 100]),
    vg.xTicks([0, 25, 50, 75, 100]),
    vg.xLabel('Measured runs that did not complete →'),
    vg.xTickFormat(d => `${d}%`),
    vg.xGrid(true),
    vg.yDomain(ENGINES),
    vg.yLabel(null),
    ...engineColor(),
    vg.width(GLANCE_SIDE_WIDTH),
    vg.height(215),
    vg.marginLeft(88),
    vg.marginRight(56),
    vg.marginBottom(42),
    vg.marginTop(8)
  );

  // ── Latency vs scale ───────────────────────────────────────────────────
  // Median across queries of the per-query median server time, per engine and
  // suite. Aggregation happens in the mark, so it recomputes under any filter.
  const latency = vg.plot(
    vg.lineY(vg.from('curves', { filterBy: $engines }), {
      x: 'scale_factor',
      y: vg.median('median_server_ms'),
      stroke: 'engine',
      fx: 'suite',
      strokeWidth: 2,
      curve: 'monotone-x'
    }),
    vg.dot(vg.from('curves', { filterBy: $engines }), {
      x: 'scale_factor',
      y: vg.median('median_server_ms'),
      fill: 'engine',
      fx: 'suite',
      r: 3.5,
      channels: { queries: vg.count() },
      tip: { ...TIP_TEXT, format: { x: d => `SF${d}`, y: ms } }
    }),
    vg.text(vg.from('curves_ends', { filterBy: $engines }), {
      x: 'scale_factor',
      y: 'ms',
      fx: 'suite',
      text: 'engine',
      fill: 'engine',
      dx: 6,
      textAnchor: 'start',
      fontSize: 11,
      fontWeight: 600,
      stroke: 'white',
      strokeWidth: 3,
      paintOrder: 'stroke'
    }),
    vg.xScale('log'),
    vg.xDomain([0.7, 1400]),
    vg.xTicks([1, 10, 100, 1000]),
    vg.xTickFormat(d => ({ 1: '1 GB', 10: '10 GB', 100: '100 GB', 1000: '1 TB' }[d] ?? `SF${d}`)),
    vg.xLabel('Data volume →'),
    vg.yScale($axis),
    vg.yZero($zero),
    vg.yLabel('↑ Median query time'),
    // Centred on the axis: anchored at the top it runs into the TPC-DS facet
    // title once the panels are phone-narrow.
    vg.yLabelAnchor('center'),
    vg.yTickFormat(ms),
    vg.yGrid(true),
    vg.fxDomain(['TPC-DS', 'DSB']),
    vg.fxLabel(null),
    ...engineColor(),
    vg.colorLabel('engine'),
    vg.width(PLOT_WIDTH),
    vg.height(320),
    vg.marginLeft(64),
    vg.marginRight(84),
    vg.marginBottom(38)
  );

  // ── ClickBench ─────────────────────────────────────────────────────────
  // Not filtered by the scale menu: ClickBench is one fixed ~100 GB dataset, so
  // there is no scale axis to select. Restricted in SQL to each engine's entry
  // compute tier (see data.js) — the only slice all seven engines share.
  const clickbench = vg.plot(
    vg.dot(vg.from('clickbench', { filterBy: $engines }), {
      x: 'median_server_ms',
      y: 'engine',
      fill: 'engine',
      r: 3,
      fillOpacity: 0.3
    }),
    vg.tickX(vg.from('clickbench', { filterBy: $engines }), {
      x: vg.median('median_server_ms'),
      y: 'engine',
      stroke: 'black',
      strokeWidth: 2.5,
      channels: { tier: 'tier', queries: vg.count() },
      tip: { ...TIP_TEXT, format: { x: ms } }
    }),
    vg.xScale($axis),
    vg.xZero($zero),
    vg.xLabel('Server-side time per query →'),
    vg.xTickFormat(ms),
    vg.yLabel(null),
    vg.xGrid(true),
    ...engineColor(),
    vg.width(PLOT_WIDTH),
    vg.height(300),
    vg.marginLeft(96),
    vg.marginBottom(40)
  );

  return { latency, frontier, confidenceLegend, skewTax, reliability, clickbench };
}
