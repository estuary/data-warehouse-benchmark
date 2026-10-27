/**
 * The sizing views: what a bigger compute size buys (the buyer's guide's
 * Finding 1) and how each size of each engine runs the scan suite against its
 * hourly price (the guide's scan-suite tables).
 *
 * Both read the single-user pass at EVERY size (`ladder_suite`,
 * `size_typical`, data.js), not the designated-size population the other
 * single-user views use: the sizing question is exactly the one the designated
 * size holds fixed.
 */
import * as vg from '@uwdata/vgplot';
import { COMPARE_PRICE_LABELS, LADDER_COST_COLOR, LADDER_SPEED_COLOR } from '../constants.js';
import { ENGINES } from '../engines.js';
import { ms, ratio, roundDollars, usdExact, usdPerHour } from '../format.js';
import { PLOT_WIDTH, engineColor, engineFacets, roundTicks, TIP_TEXT } from '../plot-helpers.js';

/** Speedup and cost multiple: one colour each, kept off the engine palette. */
export const SIZING_MEASURES = ['Speedup', 'Cost multiple'];
const SIZING_COLORS = [LADDER_SPEED_COLOR, LADDER_COST_COLOR];

/**
 * Build the sizing views.
 * @param {ReturnType<import('../controls.js').buildControls>} c
 * @returns {{sizing: HTMLElement, sizingLegend: HTMLElement, scan: HTMLElement}}
 */
export function sizingViews({ $engines, $engineScale }) {
  // ── What a bigger size buys ─────────────────────────────────────────────
  // One panel per engine, x = the size's list price (log), two lines against
  // the cheapest size on the same ladder: how many times faster the suite ran
  // (blue) and how many times as much it cost (orange). Blue above orange
  // means the size paid for itself. `ladder_long` (below, created by the page) holds
  // only ladders with two or more sizes at the data volume.
  const sizing = vg.plot(
    vg.name('sizing'),
    vg.ruleY([1], { stroke: '#999', strokeDasharray: '4,3' }),
    vg.lineY(vg.from('ladder_long', { filterBy: $engineScale }), {
      x: 'usd_per_hour',
      y: 'value',
      stroke: 'measure',
      fx: 'engine',
      strokeWidth: 2,
      curve: 'monotone-x'
    }),
    vg.dot(vg.from('ladder_long', { filterBy: $engineScale }), {
      x: 'usd_per_hour',
      y: 'value',
      fill: 'measure',
      fx: 'engine',
      r: 3,
      channels: { 'compute size': 'compute_label', 'queries in the shared set': 'queries' },
      tip: { ...TIP_TEXT, format: { fx: false, x: usdPerHour, y: ratio } }
    }),
    // The two ends of each ladder are named, so a reader can anchor the price
    // axis without hovering.
    // The cheapest size is named below its point and the priciest above
    // its own, so a short ladder's two names (BigQuery's "200 slots" and
    // "1000 slots") do not collide.
    ...[[vg.sql`lo`, 11], [vg.sql`hi AND NOT lo`, -9]].map(([filter, dy]) => vg.text(vg.from('ladder_long_ends', { filterBy: $engineScale }), {
      x: 'usd_per_hour',
      y: 'value',
      fx: 'engine',
      text: 'short_label',
      filter,
      dy,
      fontSize: 11,
      fontWeight: 600,
      fill: '#04192b',
      stroke: 'white',
      strokeWidth: 3,
      paintOrder: 'stroke'
    })),
    // A panel with nothing to compare says so, rather than standing empty:
    // the single-user sweep did not run every size at every volume.
    vg.text(vg.from('ladder_gaps', { filterBy: $engineScale }), {
      fx: 'engine', text: 'msg', frameAnchor: 'middle', fontSize: 12, fill: '#47506d', lineHeight: 1.3
    }),
    ...engineFacets(),
    // Round prices inside the shared range, as many as a narrow panel holds.
    vg.xScale('log'),
    vg.xLabel('List price per hour of the size (log) →'),
    vg.xTicks(roundTicks(COMPARE_PRICE_LABELS)),
    vg.xTickFormat(roundDollars),
    vg.yScale('log'),
    // Headroom above the fastest point, so its name (MotherDuck "Giga") sits
    // below the panel title instead of on it.
    vg.yInsetTop(18),
    vg.yLabel('Speedup and cost (× cheapest size, log)'),
    vg.yTickFormat(ratio),
    vg.yGrid(true),
    vg.colorDomain(SIZING_MEASURES),
    vg.colorRange(SIZING_COLORS),
    vg.marginLeft(64)
  );

  const sizingLegend = vg.colorLegend({ for: 'sizing', label: null });

  // ── Scan queries by size and price ──────────────────────────────────────
  // Every ClickBench size of every engine: list price against typical query
  // (geometric mean of per-query medians, over the queries that size
  // completed). Only the engine filter applies: the scan table has one size.
  const scan = vg.plot(
    vg.name('scan'),
    vg.lineY(vg.from('scan_sizes', { filterBy: $engines }), {
      x: 'usd_per_hour',
      y: 'typical_ms',
      stroke: 'engine',
      strokeWidth: 1.5,
      strokeOpacity: 0.7
    }),
    vg.dot(vg.from('scan_sizes', { filterBy: $engines }), {
      x: 'usd_per_hour',
      y: 'typical_ms',
      fill: 'engine',
      r: 5,
      stroke: 'white',
      strokeWidth: 1,
      channels: { 'compute size': 'compute_label', 'queries completed': 'completed', 'first run': 'run_date' },
      tip: { ...TIP_TEXT, format: { x: usdExact, y: ms } }
    }),
    // Sizes run in a separate session (constants.js SEPARATE_SESSION): a white
    // ring on top, explained in the note under the chart.
    vg.dot(vg.from('scan_separate', { filterBy: $engines }), {
      x: 'usd_per_hour', y: 'typical_ms', r: 4, fill: 'white', stroke: '#b26a00', strokeWidth: 2
    }),
    vg.text(vg.from('scan_fastest', { filterBy: $engines }), {
      x: 'usd_per_hour',
      y: 'typical_ms',
      text: 'label',
      dx: 8,
      textAnchor: 'start',
      fontSize: 12,
      fontWeight: 600,
      fill: '#04192b',
      stroke: 'white',
      strokeWidth: 3,
      paintOrder: 'stroke'
    }),
    vg.xScale('log'),
    vg.xLabel('List price per hour (log) →'),
    vg.xTickFormat(usdPerHour),
    vg.yScale('log'),
    vg.yLabel('↑ Typical scan query (log)'),
    vg.yTickFormat(ms),
    vg.grid(true),
    ...engineColor(),
    vg.colorLabel('engine'),
    vg.width(PLOT_WIDTH),
    vg.height(380),
    vg.marginLeft(70),
    vg.marginRight(130),
    vg.marginBottom(42)
  );

  return { sizing, sizingLegend, scan };
}

/**
 * The page-level views these charts read. Created by the Compare page after
 * the shared views exist (data.js), because they are specific to these two
 * charts.
 */
export const SIZING_VIEWS = [
  // Ladders with two or more sizes at a data volume, as one row per measure.
  `CREATE OR REPLACE VIEW ladder_long AS
     WITH s AS (
       SELECT *, count(*) OVER (PARTITION BY platform, kind, dataset) AS sizes
       FROM ladder_suite
     )
     SELECT engine, platform, kind, dataset, compute_label, usd_per_hour, queries,
            completed, attempted, 'Speedup' AS measure, speedup AS value
     FROM s WHERE sizes > 1
     UNION ALL
     SELECT engine, platform, kind, dataset, compute_label, usd_per_hour, queries,
            completed, attempted, 'Cost multiple', cost_multiple
     FROM s WHERE sizes > 1 AND cost_multiple > 0`,

  // One line of text for each engine with no ladder at a data volume: one
  // size ran there, or none did (reasons on each engine's page).
  `CREATE OR REPLACE VIEW ladder_gaps AS
     WITH e AS (SELECT DISTINCT engine, platform FROM ladder_suite),
     d AS (SELECT DISTINCT kind, dataset FROM ladder_suite),
     l AS (
       SELECT platform, kind, dataset, count(*) AS sizes,
              any_value(regexp_replace(regexp_replace(compute_label, '^ENTERPRISE / ', ''), ' \\(Gen2\\)$', '')) AS only_size
       FROM ladder_suite GROUP BY platform, kind, dataset
     )
     SELECT e.engine, e.platform, d.kind, d.dataset,
            CASE WHEN l.sizes IS NULL THEN 'Not measured' || chr(10) || 'at this volume'
                 ELSE 'Only ' || l.only_size || ' ran' || chr(10) || 'at this volume' END AS msg
     FROM e CROSS JOIN d LEFT JOIN l USING (platform, kind, dataset)
     WHERE coalesce(l.sizes, 0) <= 1`,

  // The cheapest and the priciest size of each ladder, on the speedup line.
  `CREATE OR REPLACE VIEW ladder_long_ends AS
     WITH e AS (
       SELECT *, usd_per_hour = min(usd_per_hour) OVER w AS lo,
                 usd_per_hour = max(usd_per_hour) OVER w AS hi
       FROM ladder_long WHERE measure = 'Speedup'
       WINDOW w AS (PARTITION BY platform, kind, dataset)
     )
     SELECT *, regexp_replace(regexp_replace(compute_label, '^ENTERPRISE / ', ''), ' \\(Gen2\\)$', '') AS short_label
     FROM e WHERE lo OR hi`,

  // Scan sizes with an hourly price, for the price-against-speed chart.
  `CREATE OR REPLACE VIEW scan_sizes AS
     SELECT * FROM size_typical WHERE kind = 'scan' AND usd_per_hour IS NOT NULL`,

  // Sizes measured in a separate session from the rest of their ladder.
  `CREATE OR REPLACE VIEW scan_separate AS SELECT * FROM scan_sizes WHERE separate_session`,

  // Each engine's fastest scan size, labelled on the chart.
  `CREATE OR REPLACE VIEW scan_fastest AS
     SELECT s.*, s.engine || ' ' || regexp_replace(regexp_replace(s.compute_label, '^ENTERPRISE / ', ''), ' \\(Gen2\\)$', '') AS label
     FROM scan_sizes s
     QUALIFY row_number() OVER (PARTITION BY platform ORDER BY typical_ms) = 1`
];

/** Engines in the page's fixed order, for tables built outside Mosaic. */
export const engineOrder = name => ENGINES.indexOf(name);
