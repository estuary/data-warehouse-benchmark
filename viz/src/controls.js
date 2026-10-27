/**
 * The shared selections, params and the inputs that drive them.
 *
 * Every filter is a Mosaic `Selection`: an input publishes a clause, the
 * coordinator rewrites each dependent client's query, and DuckDB-WASM answers
 * it. No view holds its own copy of the data and nothing is filtered in JS.
 *
 * Selection wiring:
 *
 *   $engines ──┬─────────────────────────────► latency, clickbench
 *              └── $engineScale ─────────────► frontier + its table, degradation,
 *   $scale ────┘                                shed rate, pattern load, warm frontier
 *
 *   $config (single, from a table row) ──────► the configuration's scale sweep
 *
 *   $sla  (Param) ────────────────────────────► frontier + its table, degradation, sweep
 *   $axis/$zero (Params) ─────────────────────► every measurement axis
 *
 * The sweep is deliberately NOT filtered by $scale: its x axis is the scale.
 */
import * as vg from '@uwdata/vgplot';
import { clausePoints } from '@uwdata/mosaic-core';
import { CLICKBENCH, DEFAULT_SCALE, DEFAULT_SLA_MS } from './constants.js';

/**
 * The data sets a view can show: the four TPC-DS + DSB scale factors, and
 * ClickBench. Values are strings because the menu publishes them as a predicate
 * on the `dataset` column every filtered view exposes (see data.js), where a
 * scale factor is its number as text and ClickBench is `CLICKBENCH`.
 */
export const SCALE_OPTIONS = [
  { value: '1', label: 'Join queries at 1 GB (SF1)' },
  { value: '10', label: 'Join queries at 10 GB (SF10)' },
  { value: '100', label: 'Join queries at 100 GB (SF100)' },
  { value: '1000', label: 'Join queries at 1 TB (SF1000)' },
  { value: CLICKBENCH, label: 'Scan queries (ClickBench, 100 GB table)' }
];
export { DEFAULT_SCALE, DEFAULT_SLA_MS };

/** Columns a configuration is identified by; the table publishes these. */
export const CONFIG_FIELDS = ['platform', 'compute_label'];

/** The configuration the sweep shows before any row is clicked. */
const DEFAULT_CONFIG = ['snowflake', 'Medium (Gen2)'];

/**
 * Build the selections, params and input elements every view shares.
 *
 * @param {{scale?: number|string, sla?: number, axis?: string, config?: string[]}} [initial]
 *   Starting values, normally from the URL hash (see url-state.js). Each
 *   falls back to the page default when absent.
 * @returns {{
 *   $engines: vg.Selection, $scale: vg.Selection, $engineScale: vg.Selection,
 *   $config: vg.Selection,
 *   $axis: vg.Param, $zero: vg.Param, $sla: vg.Param,
 *   scaleMenu: HTMLElement, slaSlider: HTMLElement, axisMenu: HTMLElement
 * }}
 */
export function buildControls(initial = {}) {
  // Sources: one selection per input.
  const $engines = vg.Selection.intersect();
  const $scale = vg.Selection.intersect();

  // Composite: what most views actually listen to.
  const $engineScale = vg.Selection.intersect({ include: [$engines, $scale] });

  // One (engine, compute size) at a time, published by a click on a table row.
  // Seeded so the sweep has something to show before the first click.
  const $config = vg.Selection.single();
  $config.update(clausePoints(CONFIG_FIELDS.map(f => vg.column(f)), [initial.config ?? DEFAULT_CONFIG], { source: 'default' }));

  // Axis scale toggle. Measurement axes (latency, cost, ratio) are logarithmic
  // by default: latency spans 36 ms to 5,144 ms, and on a linear axis most
  // engines collapse into the floor. Switching to linear also turns on a zero
  // baseline, which is only valid off the log scale — hence two Params kept in
  // step rather than one. The scale-factor axis is always log: it is a
  // 1/10/100/1000 ladder, and 0 GB is not a measurement. The people-served
  // axis is not a measurement axis either: it is the tested session levels,
  // evenly spaced, and says so in its label. Every axis this menu switches is
  // a time or a price, which is what the menu's label names.
  const axis0 = initial.axis ?? 'log';
  const $axis = vg.Param.value(axis0);
  const $zero = vg.Param.value(axis0 === 'linear');
  $axis.addEventListener('value', v => $zero.update(v === 'linear'));

  const axisMenu = vg.menu({
    label: 'Price and time axes',
    as: $axis,
    options: [
      { value: 'log', label: 'Log scale' },
      { value: 'linear', label: 'Linear, zero-based' }
    ],
    value: axis0
  });

  // Latency target for the useful-concurrency metric. Changing it re-queries the
  // frontier: the metric is recomputed in DuckDB, not recalculated in JS.
  const sla0 = initial.sla ?? DEFAULT_SLA_MS;
  const $sla = vg.Param.value(sla0);

  const slaSlider = vg.slider({
    label: 'Latency target (ms)',
    as: $sla,
    min: 500,
    max: 20000,
    step: 500,
    value: sla0,
    width: 180
  });

  // One menu for the data set. Its predicate binds to `dataset`, which every
  // view filtered by it derives from its own columns, so ClickBench (no scale
  // factor) and the four scale factors are selected the same way.
  const scaleMenu = vg.menu({
    label: 'Workload and data volume',
    as: $scale,
    field: 'dataset',
    options: SCALE_OPTIONS,
    value: String(initial.scale ?? DEFAULT_SCALE)
  });

  return {
    $engines, $scale, $engineScale, $config,
    $axis, $zero, $sla,
    scaleMenu, slaSlider, axisMenu
  };
}
