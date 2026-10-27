/**
 * Load the benchmark exports into DuckDB-WASM and derive the views the plots
 * read from.
 *
 * Two things are worth knowing about the shape of these views.
 *
 * **Display names live in SQL.** Every view exposes an `engine` column
 * carrying the vendor-cased label, so plots colour and facet on one column and
 * a legend toggle publishes a predicate that binds against every table.
 *
 * **Only what a view reads is shipped.** The payload is the exports in
 * `PARQUET_FILES` below. Adding a view that needs another export means adding
 * it here and in `scripts/prepare-data.mjs`.
 *
 * **The data-set menu binds to one column, `dataset`.** Every view a scale
 * filter reaches exposes it: a scale factor as text (`'100'`) for TPC-DS + DSB
 * rows, `CLICKBENCH` for ClickBench rows, which have no data volume (one fixed
 * ~100 GB table; the grid pools its campaign tags and carries `workload`). The
 * views that only exist for the decision-support pair (patterns, skew) simply
 * have no ClickBench rows, and the page hides their sections in that mode.
 */
import { coordinator, loadParquet } from '@uwdata/vgplot';
import { CLICKBENCH, SCAN_SUITE_QUERIES, SEPARATE_SESSION, WARM_MEASURED_RUNS } from './constants.js';
import { confidenceCase, engineCase } from './engines.js';

/** Parquet payload, registered under these exact names by `connectDatabase`. */
export const PARQUET_FILES = [
  'cost_curves.parquet',
  'clickbench_summary.parquet',
  'concurrency_grid.parquet',
  'pattern_load.parquet',
  'skew_load.parquet',
  'skew_impact.parquet',
  'row_count_agreement.parquet',
  'size_ladder.parquet',
  'rate_card.parquet',
  'query_catalog.parquet',
  'scale_skips.parquet'
];

const ENGINE = engineCase('platform');

/** Suite ids -> the casing the report uses. */
const SUITE = `CASE block WHEN 'tpcds' THEN 'TPC-DS' WHEN 'dsb' THEN 'DSB' ELSE block END`;

/** The `dataset` value of a decision-support row: its scale factor as text. */
const SCALE_DATASET = 'CAST(scale_factor AS VARCHAR)';

/** Each row's cost-confidence grade, which for BigQuery depends on the data set. */
const CONFIDENCE = confidenceCase('platform', SCALE_DATASET);
const CONFIDENCE_SCAN = confidenceCase('platform', `'${CLICKBENCH}'`);

/**
 * The "typical query" aggregate: geometric mean of per-query medians, the
 * buyer's guide's definition. It keeps one very slow query from dominating a
 * suite figure, which a plain mean would not, and still moves when most
 * queries get faster, which a median of a skewed set can hide.
 */
/**
 * Query-pattern tags (queries/registry.py) -> the words a reader sees. The
 * tags are identifiers; a legend reading "set_op" asked every reader to decode it.
 */
export const PATTERN_LABELS = {
  aggregation: 'Aggregation',
  complex_join: 'Many-table joins',
  set_op: 'Set operations',
  subquery: 'Subqueries',
  text_search: 'Text search',
  window: 'Window functions'
};
/**
 * The reducer's behavior codes (analysis/concurrency_shape.py) -> what a
 * reader sees in the legend and the table. "unknown" means the ladder had too
 * few served levels to fit a trend, which is what it says here.
 */
export const BEHAVIOR_LABELS = {
  absorbs: 'Response time stayed flat',
  queues: 'Response time rose',
  'queues+sheds': 'Rose, and some queries did not complete',
  rejects: 'Many queries did not complete',
  unknown: 'Too few served levels to tell'
};
const BEHAVIOR = `CASE failure_mode ${Object.entries(BEHAVIOR_LABELS).map(([k, v]) => `WHEN '${k}' THEN '${v}'`).join(' ')} ELSE failure_mode END`;

const PATTERN_NAME = `CASE pattern ${Object.entries(PATTERN_LABELS).map(([k, v]) => `WHEN '${k}' THEN '${v}'`).join(' ')} ELSE pattern END`;

/** True for a size listed in SEPARATE_SESSION (constants.js), in SQL. */
const SEPARATE_SQL = SEPARATE_SESSION.length
  ? `(${SEPARATE_SESSION.map(a => `(any_value(platform) = '${a.platform}' AND any_value(kind) = '${a.kind}' AND any_value(compute_label) IN (${a.labels.map(l => `'${l}'`).join(', ')}))`).join(' OR ')})`
  : 'false';

const GEOMEAN = col => `exp(avg(ln(${col})) FILTER (WHERE ${col} > 0))`;


const VIEWS = [
  // The single-user ladder without the cells recorded as skipped on purpose:
  // a partial run left behind in a skipped cell (MotherDuck Pulse at 100 GB
  // has 22 TPC-DS queries recorded before the skip) would otherwise become
  // that ladder's baseline. The page lists the skip and its reason instead.
  `CREATE OR REPLACE VIEW size_ladder AS
     SELECT l.* FROM size_ladder_raw l
     WHERE NOT EXISTS (
       SELECT 1 FROM scale_skips s
       WHERE s.platform = l.platform AND s.scale_factor IS NOT DISTINCT FROM l.scale_factor
         AND s.run_type IN ('*', 'warm') AND s.block IN ('*', l.block)
         AND s.compute_label IN ('*', l.compute_label))`,
  // The rate card as the pages show it: only sizes with a published result, so
  // a size that appears in the results database but in no export (ClickHouse
  // 2x32GB) does not show as a row of "Not in the published results".
  `CREATE OR REPLACE VIEW rate_card AS
     SELECT r.* FROM rate_card_raw r
     WHERE (r.platform, r.compute_label) IN (
       SELECT (platform, compute_label) FROM size_ladder
       UNION SELECT (platform, compute_label) FROM concurrency_grid
       UNION SELECT (platform, compute_label) FROM clickbench_summary)`,
  // Per-query warm measurements: the grain everything else aggregates from.
  `CREATE OR REPLACE VIEW curves AS
     SELECT ${ENGINE} AS engine, ${SUITE} AS suite, platform, block, query_id,
            scale_factor, median_server_ms, median_ms, p95_ms, mean_cost_usd, failures
     FROM cost_curves`,

  // One point per (engine, data set): the cost-performance frontier. Median over
  // per-query median server time; mean over per-query mean cost. The
  // decision-support points are at the designated tier per scale (that is what
  // cost_curves holds); the ClickBench point is at each engine's entry tier,
  // the one tier all seven measured on the fixed table.
  `CREATE OR REPLACE VIEW engine_scale AS
     SELECT ${ENGINE} AS engine, platform, ${SCALE_DATASET} AS dataset,
            ${CONFIDENCE}            AS cost_confidence,
            median(median_server_ms) AS frontier_ms,
            avg(mean_cost_usd)       AS frontier_cost,
            count(*)                 AS queries,
            sum(failures)            AS failures,
            ${GEOMEAN('median_server_ms')} AS typical_ms
     FROM cost_curves GROUP BY platform, scale_factor
     UNION ALL
     SELECT ${ENGINE}, platform, '${CLICKBENCH}', ${CONFIDENCE_SCAN},
            median(median_server_ms), avg(mean_cost_usd), count(*), sum(failures),
            ${GEOMEAN('median_server_ms')}
     FROM clickbench_summary WHERE is_entry_tier GROUP BY platform`,

  // Per-query failures over measured runs, for the at-a-glance reliability bars.
  // cost_curves counts failures out of the warm pass's fixed run count; the
  // ClickBench summary carries its own `runs`.
  `CREATE OR REPLACE VIEW warm_runs AS
     SELECT ${ENGINE} AS engine, platform, ${SCALE_DATASET} AS dataset, query_id,
            failures, ${WARM_MEASURED_RUNS} AS runs
     FROM cost_curves
     UNION ALL
     SELECT ${ENGINE}, platform, '${CLICKBENCH}', query_id, failures, runs
     FROM clickbench_summary WHERE is_entry_tier`,

  // ClickBench runs one fixed ~100 GB dataset, so its campaign tag varies the
  // COMPUTE TIER, not the data volume. Restricting to each engine's entry tier
  // is the only slice where all seven compare without averaging across
  // warehouse sizes; `tier` is carried through so the chart can name it.
  `CREATE OR REPLACE VIEW clickbench AS
     SELECT ${ENGINE} AS engine, platform, query_id,
            compute_label AS tier,
            median_server_ms, median_ms, mean_cost_usd, runs
     FROM clickbench_summary
     WHERE is_entry_tier`,

  // Per-pattern behaviour under load. `rel` is the pattern's median latency
  // divided by the engine's typical query in the SAME cell (engine, scale,
  // compute size, level). Normalising inside the cell divides out both raw
  // engine speed and warehouse size, which is what makes it safe to median the
  // ratio across compute sizes — a raw latency would not be.
  `CREATE OR REPLACE VIEW pattern_load AS
     SELECT ${ENGINE} AS engine, platform, scale_factor, ${SCALE_DATASET} AS dataset, compute_label,
            concurrency, ${PATTERN_NAME} AS pattern, pattern AS pattern_tag, pattern_ms, cell_ms,
            pattern_ms / cell_ms AS rel, n
     FROM pattern_load_raw`,

  // Concurrency ladder. Kept at per-level grain on purpose: the useful-concurrency
  // metric depends on the latency target, so the SLA control recomputes it in the
  // browser rather than baking one target into the export. The formula is in
  // views/concurrency.js and matches benchmark.analysis.concurrency_shape exactly.
  // Unpriceable sizes (BigQuery on-demand is scan-priced) have no cost axis, and
  // non-publishable cells carry client-side cancellations that would be misread as
  // engine shedding — both are excluded here. `workload` tells the two families
  // apart; ClickBench cells have no scale factor (the export pools the tags).
  // A scan level queues the suite's 43 queries once, so the 50-session level ran
  // all 43 at once: it reads as 43 here, and every page takes its levels from it.
  `CREATE OR REPLACE VIEW concurrency AS
     SELECT ${ENGINE} AS engine, platform, workload, scale_factor,
            CASE workload WHEN '${CLICKBENCH}' THEN '${CLICKBENCH}' ELSE ${SCALE_DATASET} END AS dataset,
            compute_label,
            CASE WHEN workload = '${CLICKBENCH}' AND concurrency = 50 THEN ${SCAN_SUITE_QUERIES}
                 ELSE concurrency END AS concurrency,
            p50_ms, shed_pct, runs,
            k, ${BEHAVIOR} AS failure_mode, failure_mode AS failure_mode_code, shed_pct_at_max, solo_latency_ms,
            usd_per_hour, sla_max_shed_pct,
            -- The tooltip line behind a red ring on the response-time charts.
            CASE WHEN shed_pct >= sla_max_shed_pct
                 THEN printf('%d%% or more did not complete, so this level never counts as served', CAST(sla_max_shed_pct AS INTEGER))
                 ELSE 'no' END AS ring_note
     FROM concurrency_grid
     WHERE publishable AND usd_per_hour IS NOT NULL`,

  // DSB against TPC-DS per cell, joined to the grid for the size's list price
  // (the ladders colour by it) and to inherit the grid's publishable filter.
  `CREATE OR REPLACE VIEW skew_load AS
     SELECT c.engine, s.platform, s.scale_factor, c.dataset, s.compute_label, s.concurrency,
            s.skew_ratio, s.tpcds_ms, s.dsb_ms, s.pairs, c.usd_per_hour
     FROM skew_load_raw s
     JOIN concurrency c USING (platform, scale_factor, compute_label, concurrency)
     WHERE c.workload = 'decision_support'`,

  // The levels the frontier's FILTER excludes on shed rate alone: the engine
  // kept its median down by not finishing the work. The degradation view rings
  // these so a low-looking median is not mistaken for capacity.
  `CREATE OR REPLACE VIEW concurrency_shed AS
     SELECT * FROM concurrency WHERE shed_pct >= sla_max_shed_pct`,

  // Warm skew tax: each DSB query against its TPC-DS twin (same query number)
  // at the designated tier and one session. `delta_pct` is already a percentage:
  // positive means the skewed variant ran slower. `clean` is false for the 25
  // pairs whose SQL touches a *_returns table, where DSB also enlarges the
  // tables queried (query_catalog.returns_pair, the report's own split). Every
  // per-engine median reads `skew_warm_clean`, the report's basis, so the site
  // and the guide quote the same figure; the per-query tables keep every pair.
  `CREATE OR REPLACE VIEW skew_warm AS
     SELECT ${ENGINE} AS engine, s.platform, s.scale_factor, CAST(s.scale_factor AS VARCHAR) AS dataset, s.qnum,
            s.tpcds_server_ms, s.dsb_server_ms, s.delta_server_ms, s.delta_pct,
            NOT coalesce(c.returns_pair, false) AS clean
     FROM skew_impact s
     LEFT JOIN query_catalog c ON c.block = 'tpcds' AND c.number = s.qnum`,
  `CREATE OR REPLACE VIEW skew_warm_clean AS SELECT * FROM skew_warm WHERE clean`,

  // Direct labels for the ladder views: the cheapest and the priciest warehouse
  // size of each engine, at the last level that size ran, so the two ends of
  // the price ramp are named on the chart and a reader can anchor the shades
  // in between. Built once per source view (latency ladders, skew ladders).
  // `short_label` drops the vendor prefix and generation suffix ("ENTERPRISE /
  // 1000 slots" -> "1000 slots", "X-Small (Gen2)" -> "X-Small"): a panel is
  // about 115 px wide and the full label runs into the next engine's panel.
  ...['concurrency', 'skew_load'].map(src => `
     CREATE OR REPLACE VIEW ${src}_extremes AS
     WITH cfg AS (
       SELECT engine, dataset, compute_label, any_value(usd_per_hour) AS usd, max(concurrency) AS top
       FROM ${src} GROUP BY 1, 2, 3
     ),
     ext AS (
       SELECT *, usd = min(usd) OVER (PARTITION BY engine, dataset) AS is_min,
                 usd = max(usd) OVER (PARTITION BY engine, dataset) AS is_max
       FROM cfg
     )
     SELECT s.*,
            regexp_replace(regexp_replace(s.compute_label, '^ENTERPRISE / ', ''), ' \\(Gen2\\)$', '') AS short_label,
            CASE WHEN e.is_min AND e.is_max THEN 'only size'
                      WHEN e.is_min THEN 'cheapest' ELSE 'priciest' END AS price_end
     FROM ${src} s
     JOIN ext e ON s.engine = e.engine AND s.dataset = e.dataset
               AND s.compute_label = e.compute_label AND s.concurrency = e.top
     WHERE e.is_min OR e.is_max`),
  // The two ends split, so the ladders can put the cheapest size's label above
  // its point and the priciest's below: at the top session count the two often
  // land within a few pixels of each other and the labels would collide.
  ...['concurrency', 'skew_load'].flatMap(src => [
    `CREATE OR REPLACE VIEW ${src}_extremes_lo AS SELECT * FROM ${src}_extremes WHERE price_end <> 'priciest'`,
    `CREATE OR REPLACE VIEW ${src}_extremes_hi AS SELECT * FROM ${src}_extremes WHERE price_end = 'priciest'`
  ]),

  // ── Compute-size ladders (single-user pass at every size) ──────────────
  // One row per (engine, suite, data volume, size, query). ClickBench rows
  // have no data volume (one fixed table); they carry `dataset` = CLICKBENCH
  // like everywhere else. BigQuery on-demand is a pricing model, not a size,
  // and has no hourly rate, so it stays out of every ladder.
  `CREATE OR REPLACE VIEW ladder AS
     SELECT ${ENGINE} AS engine, ${SUITE} AS suite, l.platform, l.block,
            l.scale_factor,
            CASE WHEN l.block = 'clickbench' THEN '${CLICKBENCH}' ELSE CAST(l.scale_factor AS VARCHAR) END AS dataset,
            CASE WHEN l.block = 'clickbench' THEN 'scan' ELSE 'join' END AS kind,
            l.compute_label, l.query_id, l.median_server_ms, l.mean_cost_usd,
            l.runs, l.failures, l.first_run_at, l.last_run_at, r.usd_per_hour
     FROM size_ladder l
     LEFT JOIN rate_card r USING (platform, compute_label)
     WHERE l.compute_label <> 'on-demand'`,

  // The name of each engine's designated size at each data volume, and its
  // entry tier on the scan table. cost_curves is the single-user pass at the
  // designated size but carries no size label; the same runs appear in the
  // size ladder under their label, so the label whose per-query medians match
  // cost_curves names the size (every query matches at every engine and
  // volume; tests/fit.spec.js holds that). BigQuery's on-demand rows match
  // "on-demand" at 1 GB and 10 GB.
  `CREATE OR REPLACE VIEW designated_size AS
     WITH m AS (
       SELECT c.platform, ${SCALE_DATASET.replace('scale_factor', 'c.scale_factor')} AS dataset, l.compute_label, count(*) AS matched
       FROM cost_curves c
       JOIN size_ladder_raw l
         ON l.platform = c.platform AND l.scale_factor = c.scale_factor AND l.query_id = c.query_id
        AND abs(l.median_server_ms - c.median_server_ms) < 1e-6
       WHERE l.block <> 'clickbench'
       GROUP BY ALL
     )
     SELECT platform, dataset, compute_label FROM m
     QUALIFY row_number() OVER (PARTITION BY platform, dataset ORDER BY matched DESC, compute_label) = 1
     UNION ALL
     SELECT platform, '${CLICKBENCH}', any_value(compute_label)
     FROM clickbench_summary WHERE is_entry_tier GROUP BY platform`,

  // Suite totals per size over the COMMON-COMPLETE query set: the queries every
  // size at that (engine, workload, data volume) completed. This is the report's
  // ladder method (verifiers._ladder_common_complete): summing each size's own
  // completed set would understate the fast sizes, because the queries the
  // small sizes dropped are the expensive ones. `speedup` and `cost_multiple`
  // are against the cheapest size on the same ladder; `completed` counts the
  // queries this size finished out of `attempted`, before the common-set cut.
  `CREATE OR REPLACE VIEW ladder_suite AS
     WITH d AS (SELECT * FROM ladder WHERE median_server_ms IS NOT NULL),
     k AS (SELECT platform, kind, dataset, count(DISTINCT compute_label) AS sizes FROM ladder GROUP BY ALL),
     done AS (
       SELECT platform, kind, dataset, compute_label,
              count(median_server_ms) AS completed, count(*) AS attempted
       FROM ladder GROUP BY ALL
     ),
     common AS (
       SELECT d.platform, d.kind, d.dataset, d.query_id
       FROM d JOIN k USING (platform, kind, dataset)
       GROUP BY d.platform, d.kind, d.dataset, d.query_id
       HAVING count(DISTINCT d.compute_label) = any_value(k.sizes)
     ),
     agg AS (
       SELECT d.platform, d.kind, d.dataset, d.compute_label,
              any_value(d.engine) AS engine, any_value(d.scale_factor) AS scale_factor,
              any_value(d.usd_per_hour) AS usd_per_hour,
              count(*) AS queries,
              sum(d.median_server_ms) / 1000 AS suite_s,
              sum(d.mean_cost_usd) AS suite_cost,
              ${GEOMEAN('d.median_server_ms')} AS typical_ms
       FROM d JOIN common USING (platform, kind, dataset, query_id)
       GROUP BY ALL
     ),
     base AS (
       SELECT platform, kind, dataset,
              arg_min(suite_s, usd_per_hour) AS base_s,
              arg_min(suite_cost, usd_per_hour) AS base_cost,
              arg_min(compute_label, usd_per_hour) AS base_label
       FROM agg GROUP BY ALL
     )
     SELECT a.*, done.completed, done.attempted, base.base_label,
            base.base_s / a.suite_s AS speedup,
            a.suite_cost / nullif(base.base_cost, 0) AS cost_multiple
     FROM agg a
     JOIN base USING (platform, kind, dataset)
     JOIN done USING (platform, kind, dataset, compute_label)`,

  // Typical query per size over EVERY query that size completed (not the
  // common set): the buyer's guide's scan-suite tables (fastest measured size,
  // every size under a budget) rank sizes this way. Carries the completion
  // count so a size that skipped queries is visible as such.
  `CREATE OR REPLACE VIEW size_typical AS
     SELECT engine, platform, kind, dataset, scale_factor, compute_label,
            any_value(usd_per_hour) AS usd_per_hour,
            ${GEOMEAN('median_server_ms')} AS typical_ms,
            median(median_server_ms) AS median_ms,
            count(median_server_ms) AS completed, count(*) AS attempted,
            sum(mean_cost_usd) FILTER (WHERE median_server_ms IS NOT NULL) AS completed_cost,
            strftime(min(first_run_at), '%Y-%m-%d') AS run_date,
            ${SEPARATE_SQL} AS separate_session
     FROM ladder GROUP BY ALL`,

  // The last point of each engine's line in the data-volume chart, so the
  // lines are named on the chart itself and a downloaded image needs no legend.
  `CREATE OR REPLACE VIEW curves_ends AS
     WITH m AS (
       SELECT engine, platform, suite, scale_factor, median(median_server_ms) AS ms
       FROM curves GROUP BY ALL
     )
     SELECT * FROM m QUALIFY row_number() OVER (PARTITION BY platform, suite ORDER BY scale_factor DESC) = 1`,

  // Per-query single-user results at the designated size, with the query's tags.
  `CREATE OR REPLACE VIEW query_results AS
     SELECT c.engine, c.suite, c.platform, c.block, c.query_id, q.number,
            c.scale_factor, CAST(c.scale_factor AS VARCHAR) AS dataset,
            c.median_server_ms, c.mean_cost_usd, c.failures, q.patterns
     FROM curves c LEFT JOIN query_catalog q USING (query_id)
     UNION ALL
     SELECT s.engine, 'ClickBench', s.platform, 'clickbench', s.query_id, q.number,
            NULL, '${CLICKBENCH}', s.median_server_ms, s.mean_cost_usd, s.failures, q.patterns
     FROM (SELECT ${ENGINE} AS engine, * FROM clickbench_summary WHERE is_entry_tier) s
     LEFT JOIN query_catalog q USING (query_id)`,
];

/** Parquet files whose bare name is taken by a derived view; their tables get `_raw`. */
const DERIVED = new Set(['pattern_load', 'skew_load', 'size_ladder', 'rate_card']);

/** Table name for a parquet file. */
function tableName(file) {
  const base = file.replace('.parquet', '');
  return DERIVED.has(base) ? `${base}_raw` : base;
}

/**
 * Register the parquet files as tables and build the derived views.
 * @returns {Promise<void>}
 */
export async function loadTables() {
  const mc = coordinator();
  for (const file of PARQUET_FILES) {
    await mc.exec(loadParquet(tableName(file), file));
  }
  for (const sql of VIEWS) {
    await mc.exec(sql);
  }
}

/**
 * Headline facts for the page header, computed in DuckDB like everything else.
 * The grid figures come first, then the single-user per-query measurements.
 * @returns {Promise<{engines:number, configurations:number, gridRuns:number,
 *                    queries:number, measurements:number, agreement:number,
 *                    agreed:number, compared:number, scales:number[]}>}
 */
export async function headlineStats() {
  const mc = coordinator();
  const [row] = await mc.query(
    `SELECT
       (SELECT count(DISTINCT platform) FROM cost_curves)                       AS engines,
       (SELECT count(DISTINCT (platform, compute_label)) FROM concurrency_grid) AS configurations,
       (SELECT sum(runs) FROM concurrency_grid)                                 AS grid_runs,
       (SELECT count(*) FROM (
          SELECT DISTINCT query_id FROM cost_curves
          UNION SELECT DISTINCT query_id FROM clickbench_summary))              AS queries,
       (SELECT (SELECT count(*) FROM cost_curves)
             + (SELECT count(*) FROM clickbench_summary))                       AS measurements,
       -- Agreement ACROSS engines: a (query, data volume) agrees when every engine
       -- that ran it returned one row count. (distinct_counts is run-to-run
       -- stability within one engine, a different check; the Method page shows both.)
       (SELECT count(*) FILTER (WHERE counts = 1) FROM (
          SELECT count(DISTINCT row_count) AS counts FROM row_count_agreement
          GROUP BY query_id, scale_factor))                                   AS agreed,
       (SELECT count(DISTINCT (query_id, scale_factor)) FROM row_count_agreement) AS compared`,
    { type: 'json' }
  );
  const scales = await mc.query(
    'SELECT DISTINCT scale_factor FROM cost_curves ORDER BY scale_factor',
    { type: 'json' }
  );
  return {
    engines: Number(row.engines),
    configurations: Number(row.configurations),
    gridRuns: Number(row.grid_runs),
    queries: Number(row.queries),
    measurements: Number(row.measurements),
    agreement: 100 * Number(row.agreed) / Number(row.compared),
    agreed: Number(row.agreed),
    compared: Number(row.compared),
    scales: scales.map(r => Number(r.scale_factor))
  };
}
