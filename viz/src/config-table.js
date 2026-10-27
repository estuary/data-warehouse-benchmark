/**
 * The concurrency frontier as a table: one row per (engine, compute size) at
 * the selected scale, with the useful-concurrency metric recomputed for the
 * current SLA.
 *
 * Mosaic's stock `vg.table` lists stored columns and cannot express an
 * aggregate that depends on a Param, so this is a small `MosaicClient` of its
 * own. It follows the same contract as every plot: `filterBy` is a Selection
 * whose predicate the coordinator injects, the SLA Param triggers a re-query,
 * and sorting is an ORDER BY the database runs. Nothing is sorted or filtered
 * in JavaScript. It lists facts and lets the reader order them; it computes no
 * score.
 *
 * A click on a row publishes that (platform, compute size) to the `config`
 * single-selection, which drives the scale sweep beneath the table.
 */
import { MosaicClient, Query, column, coordinator, sql } from '@uwdata/vgplot';
import { clausePoints } from '@uwdata/mosaic-core';
import { CLICKBENCH } from './constants.js';
import { CONFIG_FIELDS } from './controls.js';
import { ENGINE_COLOR, ENGINES } from './engines.js';
import { pct, usdExact } from './format.js';
import { sqlList } from './plot-helpers.js';

/** Aliases of the SLA-dependent aggregates in the query; also their sort keys. */
const SESSIONS = 'sessions';
const USD_PER_SESSION = 'usd_per_session';

/**
 * Columns in display order. `text` columns are left-aligned; `format` renders
 * a number for display and is never applied to the sort.
 */
const COLUMNS = [
  { key: 'engine', label: 'Engine', text: true },
  { key: 'compute_label', label: 'Compute size', text: true },
  { key: 'usd_per_hour', label: 'USD per hour', format: usdExact },
  { key: SESSIONS, label: 'People served within target' },
  { key: USD_PER_SESSION, label: 'USD per hour per person served', format: usdExact },
  { key: 'failure_mode', label: 'Behavior', text: true },
  { key: 'shed_pct_at_max', label: 'Not completed at the highest level', format: pct }
];

/** Per-cell constants: everything the aggregate is grouped by. `platform` is
 *  not displayed; it is what the row publishes when clicked. */
const CELL_COLUMNS = ['platform', 'engine', 'compute_label', 'usd_per_hour', 'failure_mode', 'shed_pct_at_max'];

export class ConfigTable extends MosaicClient {
  /**
   * @param {object} options
   * @param {import('@uwdata/mosaic-core').Selection} options.filterBy
   *   Engines + scale, the same selection the frontier plot listens to.
   * @param {import('@uwdata/mosaic-core').Param} options.sla
   *   Latency target; a change re-queries the table.
   * @param {*} options.usefulConcurrency
   *   The frontier's SQL aggregate for sessions within the SLA. Shared with the
   *   plot so the two can never disagree.
   * @param {*} options.usdPerSession
   *   The plot's hourly-cost-per-served-session aggregate; NULL where nothing
   *   is served. A fact to sort by, never a score.
   * @param {import('@uwdata/mosaic-core').Selection} options.config
   *   Single-selection a clicked row publishes its (platform, compute size) to.
   */
  constructor({ filterBy, sla, usefulConcurrency, usdPerSession, config }) {
    super(filterBy);
    this.usefulConcurrency = usefulConcurrency;
    this.usdPerSession = usdPerSession;
    this.config = config;
    this.sort = { key: null, desc: false };

    this.element = document.createElement('table');
    this.element.className = 'configs';
    this.head = this.element.createTHead();
    this.body = this.element.createTBody();
    this.caption = this.element.createCaption();
    this.renderHead();

    // Param changes re-run the query; the aggregate reads the Param by value
    // each time the SQL is generated.
    sla.addEventListener('value', () => this.requestUpdate());
    // The highlighted row follows the selection, wherever it was set from.
    config.addEventListener('value', () => this.highlight());
  }

  /** The currently selected [platform, compute_label], or null. */
  selected() {
    const point = this.config.value?.[0];
    return Array.isArray(point) ? point : null;
  }

  /** Mark the row matching the selection; clear the rest. */
  highlight() {
    const sel = this.selected();
    for (const tr of this.body.rows) {
      const on = !!sel && tr.dataset.platform === sel[0] && tr.dataset.compute === sel[1];
      tr.classList.toggle('selected', on);
      tr.setAttribute('aria-selected', String(on));
    }
  }

  /**
   * Publish a row's configuration to the single-selection.
   * @param {HTMLTableRowElement} tr
   */
  select(tr) {
    const point = [tr.dataset.platform, tr.dataset.compute];
    this.config.update(clausePoints(CONFIG_FIELDS.map(f => column(f)), [point], { source: this }));
  }

  /** Column headers, each a sortable button for mouse and keyboard. */
  renderHead() {
    const tr = document.createElement('tr');
    for (const col of COLUMNS) {
      const th = document.createElement('th');
      th.textContent = col.label;
      th.scope = 'col';
      th.tabIndex = 0;
      if (col.text) th.classList.add('text');
      const sorted = this.sort.key === col.key;
      th.classList.toggle('sorted', sorted);
      th.setAttribute('aria-sort', sorted ? (this.sort.desc ? 'descending' : 'ascending') : 'none');
      if (sorted) th.textContent += this.sort.desc ? ' ▾' : ' ▴';
      const toggle = () => this.sortBy(col.key);
      th.addEventListener('click', toggle);
      th.addEventListener('keydown', ev => {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        ev.preventDefault();
        toggle();
      });
      tr.append(th);
    }
    this.head.replaceChildren(tr);
  }

  /**
   * Cycle a column through ascending, descending, and the default order, then
   * ask the coordinator for the re-ordered rows.
   * @param {string} key
   */
  sortBy(key) {
    if (this.sort.key !== key) this.sort = { key, desc: false };
    else if (!this.sort.desc) this.sort = { key, desc: true };
    else this.sort = { key: null, desc: false };
    this.renderHead();
    this.requestQuery();
    // Re-rendering the header replaced the focused node; put focus back.
    const idx = COLUMNS.findIndex(c => c.key === key);
    this.head.querySelectorAll('th')[idx]?.focus();
  }

  /**
   * @param {*} filter The selection predicate the coordinator injects.
   * @returns {Query}
   */
  query(filter = []) {
    const { key, desc } = this.sort;
    // Default order: engines as the legend lists them, then the ladder of
    // sizes from largest to smallest, so a table reads like the frontier plot
    // does top to bottom.
    const order = key
      ? [sql`${column(key)} ${desc ? 'DESC' : 'ASC'} NULLS LAST`]
      : [sql`list_position(${sqlList(ENGINES)}, engine)`, sql`usd_per_hour DESC`];
    return Query.from('concurrency')
      .select(Object.fromEntries(CELL_COLUMNS.map(c => [c, column(c)])))
      .select({ [SESSIONS]: this.usefulConcurrency, [USD_PER_SESSION]: this.usdPerSession })
      .where(filter)
      .groupby(CELL_COLUMNS.map(c => column(c)))
      .orderby(order);
  }

  /**
   * @param {*} data Arrow table from the coordinator.
   * @returns {this}
   */
  queryResult(data) {
    const rows = data.toArray();
    const frag = document.createDocumentFragment();
    for (const row of rows) {
      const tr = document.createElement('tr');
      tr.dataset.platform = String(row.platform);
      tr.dataset.compute = String(row.compute_label);
      tr.tabIndex = 0;
      tr.addEventListener('click', () => this.select(tr));
      tr.addEventListener('keydown', ev => {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        ev.preventDefault();
        this.select(tr);
      });
      if (Number(row[SESSIONS]) === 0) tr.classList.add('zero');
      for (const col of COLUMNS) {
        const td = document.createElement('td');
        const v = row[col.key];
        if (col.text) td.classList.add('text');
        if (col.key === 'engine') {
          const swatch = document.createElement('span');
          swatch.className = 'swatch';
          swatch.style.background = ENGINE_COLOR[v] ?? '#999';
          td.append(swatch, document.createTextNode(String(v)));
        } else {
          td.textContent = v == null ? '' : col.format ? col.format(Number(v)) : String(v);
        }
        tr.append(td);
      }
      frag.append(tr);
    }
    this.body.replaceChildren(frag);
    this.highlight();
    const n = rows.length;
    // The sweep under the table plots a configuration across data volumes;
    // ClickBench has one, so the page hides it and the invitation goes too.
    const sweep = document.body.classList.contains(CLICKBENCH) ? '' : ' Click a row to follow it across data volumes below.';
    this.caption.textContent = n === 0
      ? 'No configurations match the current filters.'
      : `${n} configuration${n === 1 ? '' : 's'}. Rows in grey served no level within the SLA.${sweep}`;
    return this;
  }
}

/**
 * Build the table and register it with the coordinator.
 * @param {ConstructorParameters<typeof ConfigTable>[0]} options
 * @returns {HTMLTableElement}
 */
export function configTable(options) {
  const client = new ConfigTable(options);
  coordinator().connect(client);
  return client.element;
}
