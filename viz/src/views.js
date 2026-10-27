/**
 * Compose the coordinated views.
 *
 * The shared selections and inputs live in `controls.js`; the concurrency
 * views in `views/concurrency.js`; the single-user views in `views/warm.js`
 * and `views/sizing.js`. Attribute bundles they share are in
 * `plot-helpers.js`. This module only wires them together, so the Compare
 * page (`pages/compare.js`) has one thing to call.
 */
import { buildControls, SCALE_OPTIONS, DEFAULT_SCALE } from './controls.js';
import { concurrencyViews } from './views/concurrency.js';
import { warmViews } from './views/warm.js';
import { sizingViews } from './views/sizing.js';

/**
 * Build every view and the shared selections.
 * @param {Parameters<typeof buildControls>[0]} [initial] Starting control values.
 * @returns {{controls: object, selections: object, params: object, elements: object}}
 */
export function buildViews(initial) {
  const controls = buildControls(initial);
  const { $engines, $scale, $axis, $zero, $sla, scaleMenu, slaSlider, axisMenu } = controls;

  return {
    controls,
    selections: { $engines, $scale },
    params: { $axis, $zero, $sla },
    elements: {
      scaleMenu, slaSlider, axisMenu,
      ...concurrencyViews(controls),
      ...warmViews(controls),
      ...sizingViews(controls)
    }
  };
}

export { SCALE_OPTIONS, DEFAULT_SCALE };
