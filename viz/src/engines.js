/**
 * Engine identity: vendor casing and one stable colour per engine, used by
 * every view so a colour means the same engine everywhere on the page.
 *
 * The palette is Okabe-Ito (colour-vision-deficiency safe) assigned so each
 * engine lands near its vendor hue where that does not cost separability.
 * ClickHouse's yellow is darkened from #F0E442 to keep 1-2 px line strokes
 * legible on white.
 */

/** Engine id (as recorded in `platform`) -> display label. */
export const ENGINE_LABEL = {
  snowflake: 'Snowflake',
  databricks: 'Databricks',
  bigquery: 'BigQuery',
  redshift: 'Redshift',
  fabric: 'Fabric',
  motherduck: 'MotherDuck',
  // Last on purpose: ClickHouse has no SF1000 concurrency data, and this order
  // is also the facet order, so its empty panel sits at the edge of the row
  // rather than splitting the populated ones.
  clickhouse: 'ClickHouse'
};

/** Display label -> hex colour. */
export const ENGINE_COLOR = {
  Snowflake: '#56B4E9',
  BigQuery: '#0072B2',
  Databricks: '#D55E00',
  MotherDuck: '#E69F00',
  ClickHouse: '#B79B12',
  Fabric: '#009E73',
  Redshift: '#CC79A7'
};

/** Stable ordering for legends and colour domains. */
export const ENGINES = Object.values(ENGINE_LABEL);
export const COLORS = ENGINES.map(e => ENGINE_COLOR[e]);

/** A SQL CASE expression mapping a platform column to its display label. */
export function engineCase(col = 'platform') {
  const arms = Object.entries(ENGINE_LABEL)
    .map(([id, label]) => `WHEN '${id}' THEN '${label}'`)
    .join(' ');
  return `CASE ${col} ${arms} ELSE ${col} END`;
}

/**
 * How each engine's per-query cost was obtained. Three grades, and they are not
 * the same quantity: `measured` is per-query attribution from the engine's own
 * billing API; `allocated` is a time-weighted share of a billing total;
 * `modeled` is compute time multiplied by the published rate. The cost-speed
 * quadrant encodes this as dot shape so a reader sees it where the cost axis is.
 */
export const COST_CONFIDENCE = {
  Snowflake: 'modeled',
  BigQuery: 'allocated',
  Databricks: 'allocated',
  Redshift: 'allocated',
  Fabric: 'allocated',
  ClickHouse: 'modeled',
  MotherDuck: 'modeled'
};

/**
 * The data sets whose BigQuery cost is on-demand, the vendor's metered bill per
 * query, and so `measured` rather than `allocated`: on-demand is BigQuery's
 * designated configuration at 1 GB and 10 GB (run_plan.yaml) and its entry tier on
 * the scan queries. `tests/site.spec.js` holds this to the exports.
 */
export const BIGQUERY_ON_DEMAND_DATASETS = ['1', '10', 'clickbench'];

/** Confidence grades, best first; also the symbol domain order. */
export const CONFIDENCE_LEVELS = ['measured', 'allocated', 'modeled'];

/**
 * A SQL CASE expression mapping a platform column to its cost-confidence grade
 * (the buyer's guide's tiers: Measured, Allocated, Modeled). `dataset` is the SQL
 * for the row's data set, which decides BigQuery's grade.
 */
export function confidenceCase(col = 'platform', dataset = null) {
  const arms = Object.entries(ENGINE_LABEL)
    .map(([id, label]) => {
      if (id === 'bigquery' && dataset) {
        const onDemand = BIGQUERY_ON_DEMAND_DATASETS.map(d => `'${d}'`).join(', ');
        return `WHEN '${id}' THEN CASE WHEN ${dataset} IN (${onDemand}) THEN 'measured' ELSE '${COST_CONFIDENCE[label]}' END`;
      }
      return `WHEN '${id}' THEN '${COST_CONFIDENCE[label]}'`;
    })
    .join(' ');
  return `CASE ${col} ${arms} ELSE 'modeled' END`;
}
