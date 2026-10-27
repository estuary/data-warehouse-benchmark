/**
 * Shareable state: the controls live in the URL hash.
 *
 *   #sf=1000&sla=2000&axis=linear&engines=Snowflake,Redshift&config=snowflake:Medium%20(Gen2)
 *
 * `sf` is the data set: a scale factor, or `clickbench`.
 *
 * Reading happens once at startup and seeds the inputs before they publish
 * their first clause, so the page opens in the linked state without an
 * intermediate render. Writing happens on every Selection or Param change with
 * `history.replaceState`, so the address bar always describes what is on
 * screen and the back button is not spammed. Every value still flows through
 * the same Selections and Params the inputs use; the hash is a mirror, not a
 * second source of truth.
 *
 * Engines are the one input Mosaic gives no programmatic setter for: the legend
 * toggles are pointer handlers reading the swatch under the cursor. They are
 * replayed as pointerdown events once the swatches exist, a plain press on the
 * first engine and shift-presses on the rest, which is exactly what a person
 * does to reach that state.
 */
import { CLICKBENCH } from './constants.js';
import { CONFIG_FIELDS } from './controls.js';
import { pageFor } from './site/layout.js';

/** Hash keys. */
const KEY = { scale: 'sf', sla: 'sla', axis: 'axis', engines: 'engines', config: 'config' };

/**
 * Parse the hash into initial values. Missing or malformed keys are omitted so
 * callers fall back to their defaults.
 * @returns {{scale?: number|string, sla?: number, axis?: string, engines?: string[], config?: string[]}}
 */
export function readState() {
  const params = new URLSearchParams(location.hash.replace(/^#/, ''));
  const out = {};
  const raw = params.get(KEY.scale);
  const scale = Number(raw);
  if (raw === CLICKBENCH) out.scale = CLICKBENCH;
  else if ([1, 10, 100, 1000].includes(scale)) out.scale = scale;
  const sla = Number(params.get(KEY.sla));
  if (Number.isFinite(sla) && sla >= 500 && sla <= 20000) out.sla = sla;
  const axis = params.get(KEY.axis);
  if (axis === 'log' || axis === 'linear') out.axis = axis;
  const engines = params.get(KEY.engines);
  if (engines) out.engines = engines.split(',').map(s => s.trim()).filter(Boolean);
  const config = params.get(KEY.config);
  if (config && config.includes(':')) {
    const i = config.indexOf(':');
    out.config = [config.slice(0, i), config.slice(i + 1)];
  }
  return out;
}

/**
 * Put `hash` in the address bar without adding a history entry, if the address
 * still names `page`. The pages swap in place (src/site/router.js), and the
 * back button changes the address before the page it leaves is taken down, so
 * a write that lands in between would otherwise stamp this page's controls
 * onto the next page's address. The entry's state (the router keeps the
 * scroll position there) is carried over.
 * @param {string} page Page id of the page writing, from `PAGES` in layout.js.
 * @param {string} hash The new hash, with its `#`.
 */
export function replaceHash(page, hash) {
  if (pageFor(location.href) !== page || location.hash === hash) return;
  history.replaceState(history.state, '', hash);
}

/**
 * Mirror the controls into the hash whenever they change.
 * @param {ReturnType<import('./controls.js').buildControls>} controls
 */
export function writeState(controls) {
  const { $scale, $sla, $axis, $engines, $config } = controls;
  const write = () => {
    const params = new URLSearchParams();
    const scale = $scale.value;
    if (scale != null) params.set(KEY.scale, String(scale));
    params.set(KEY.sla, String($sla.value));
    if ($axis.value !== 'log') params.set(KEY.axis, $axis.value);
    // A legend toggle publishes points as [label] tuples; null means "all".
    const engines = $engines.value;
    if (Array.isArray(engines) && engines.length) params.set(KEY.engines, engines.map(p => p[0]).join(','));
    const config = $config.value?.[0];
    if (Array.isArray(config) && config.length === CONFIG_FIELDS.length) params.set(KEY.config, config.join(':'));
    replaceHash('compare', `#${params.toString()}`);
  };
  for (const s of [$scale, $sla, $axis, $engines, $config]) s.addEventListener('value', write);
  write();
}

/**
 * Select exactly `labels` in the swatch legend by replaying the pointer
 * gestures Mosaic's Toggle interactor listens for, once the swatches exist.
 * @param {HTMLElement} legend The element returned by `vg.colorLegend`.
 * @param {string[]} labels Engine display labels to leave selected.
 */
export function applyEngines(legend, labels) {
  const wanted = new Set(labels);
  const press = (node, shiftKey) =>
    node.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, shiftKey }));
  const swatches = () => [...legend.querySelectorAll('[class*="-swatches"] > div, [class*="-swatches"] > span')];
  // One press at a time, each on a freshly found swatch, and each only after
  // the legend has redrawn from the previous press (or 600 ms have passed). A
  // press sent while the legend is still redrawing lands on a detached node
  // and is lost, and on a slow CI runner one animation frame is not enough.
  const redrawn = () => new Promise(resolve => {
    const done = () => { observer.disconnect(); clearTimeout(timer); resolve(); };
    const observer = new MutationObserver(done);
    observer.observe(legend, { childList: true, subtree: true, attributes: true });
    const timer = setTimeout(done, 600);
  });
  const pressInTurn = async names => {
    for (let i = 0; i < names.length; i++) {
      const node = swatches().find(n => n.textContent.trim() === names[i]);
      if (!node) continue;
      const settled = redrawn();
      // A plain press on the first engine, shift-presses to add the rest.
      press(node, i > 0);
      await settled;
    }
  };
  const attempt = () => {
    const all = swatches();
    if (!all.length) return false;
    const names = all.map(n => n.textContent.trim()).filter(t => wanted.has(t));
    if (!names.length || names.length === all.length) return true;
    pressInTurn(names);
    return true;
  };
  if (attempt()) return;
  const observer = new MutationObserver(() => {
    if (attempt()) observer.disconnect();
  });
  observer.observe(legend, { childList: true, subtree: true });
}
