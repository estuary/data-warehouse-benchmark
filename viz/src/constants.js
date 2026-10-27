/**
 * Facts about the campaign the explorer hard-codes.
 *
 * Kept free of imports so the browser tests can read the same values in Node
 * and hold them against the parquet: each one is a claim about the data, and
 * `tests/explorer.spec.js` checks the claim.
 */

/**
 * The concurrency levels actually run, plus 0 for "served none at this SLA".
 * Useful concurrency can only ever take one of these values, so the frontier's
 * axis is a ladder over exactly this set rather than a continuous range.
 */
export const MEASURED_LEVELS = [0, 1, 5, 10, 20, 50, 100];

/**
 * List-price range of every compute size in the grid, USD per hour. The
 * ladders colour by price on a log ramp pinned to this domain so the ramp does
 * not shift when a filter changes.
 */
export const COMPUTE_PRICE_RANGE = [0.35, 65];

/**
 * Measured runs per query in the warm pass (1 warmup discarded, then 3
 * measured). `cost_curves.failures` counts failures out of these, so the
 * at-a-glance reliability bar divides by it; the tests check no query exceeds it.
 */
export const WARM_MEASURED_RUNS = 3;

/** Scale factor the page opens at. */
export const DEFAULT_SCALE = 100;

/**
 * The data-set menu's value for ClickBench. The other values are the scale
 * factors as strings; this one selects the fixed ~100 GB `hits` table, whose
 * grid cells carry `workload = 'clickbench'` and no scale factor.
 */
export const CLICKBENCH = 'clickbench';

/**
 * Queries in the ClickBench suite. The concurrency pass queues the suite once
 * per level and the sessions drain that one queue, so the 50-session level ran
 * all 43 at once and no more. The explorer labels that level 43 (data.js), and
 * the export leaves out the 100-session level, which added only idle
 * connections (analysis/concurrency_grid.py).
 */
export const SCAN_SUITE_QUERIES = 43;

/**
 * The scan-query levels, plus 0: the join ladder with its top two rungs
 * replaced by the whole suite at once. 43 takes the rung 50 has on the join
 * ladder, so one axis position serves both.
 */
export const SCAN_LEVELS = [0, 1, 5, 10, 20, SCAN_SUITE_QUERIES];

/** The levels a data set was measured at: `SCAN_LEVELS` for ClickBench, else `MEASURED_LEVELS`. */
export const levelsFor = dataset => (dataset === CLICKBENCH ? SCAN_LEVELS : MEASURED_LEVELS);

/**
 * The size-ladder charts' two lines, on the engine page and Compare alike:
 * speedup in blue, cost multiple in orange. Red is kept for the latency target
 * and the "did not complete" rings, so the cost line is never read as either.
 */
export const LADDER_SPEED_COLOR = '#4267e9';
export const LADDER_COST_COLOR = '#c65a00';

/**
 * Price labels a size-ladder panel has room for. One engine's sizes span under
 * two decades, so its page allows 1-2-5 steps (Fabric: $0.50 to $20); Compare
 * puts seven panels of about 115 px across the row, where 1-3 steps over two
 * decades run together ("$10$30").
 */
export const ENGINE_PRICE_LABELS = 6;
export const COMPARE_PRICE_LABELS = 3;

/** Latency target the frontier is computed against, in milliseconds. */
export const DEFAULT_SLA_MS = 5000;

/**
 * Measurements shown with a caution mark. Each entry is a size measured in a
 * separate session from the rest of its engine's ladder whose result breaks
 * that ladder's trend, so it is not directly comparable with its neighbours.
 * The dates are claims about the data; tests/site.spec.js checks them.
 */
export const SEPARATE_SESSION = [
  {
    platform: 'bigquery',
    kind: 'scan',
    labels: ['ENTERPRISE / 100 slots', 'ENTERPRISE / 500 slots'],
    ranOn: '2026-08-28',
    restRanOn: '2026-07-25',
    note: 'Run on August 28, a month after the other BigQuery scan sizes (July 25). The 500-slot run was slower than 400 slots on 40 of 43 queries (by more than 15% on 32) while using under half its slots, so the 100-slot and 500-slot points are not directly comparable with the rest of the ladder.'
  }
];

/**
 * Shares of unfinished queries under load that differ by less than this many
 * percentage points read as a tie: about three queries in 30,000, well inside
 * the study's rule that small differences between engines are ties. It makes
 * Redshift (0.05%) and Fabric (0.06%) tie for the fewest, as the guide says.
 */
export const UNFINISHED_TIE_PP = 0.01;
