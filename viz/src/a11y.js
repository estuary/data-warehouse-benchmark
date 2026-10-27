/**
 * Keyboard access for Mosaic's interactive swatch legend.
 *
 * `vg.colorLegend({ as })` renders each engine as a `<div>` with a click
 * handler: mouse-only, invisible to the tab order. Plot rebuilds that markup on
 * every plot render, so the decoration is re-applied from a MutationObserver
 * rather than once.
 */

/** Swatch nodes are the direct children of the swatches container. */
const SWATCHES = '[class*="-swatches"] > div, [class*="-swatches"] > span';

/**
 * Make every swatch in a legend focusable and toggleable with Enter or Space.
 *
 * @param {HTMLElement} legend The element returned by `vg.colorLegend`.
 * @returns {HTMLElement} The same element, for chaining.
 */
export function keyboardToggles(legend) {
  // A toggle re-renders the plot and with it the legend, destroying the node
  // that had focus. Remember which swatch the keyboard toggled so focus can be
  // handed to its replacement, and the next Enter lands where the user expects.
  // Mosaic dims the old swatch (a style mutation) before the rebuild arrives,
  // so the hand-off only fires for a node that is not already focused, and the
  // intent expires if no rebuild follows.
  let refocus = null;
  let refocusTimer = 0;
  const REFOCUS_WINDOW_MS = 2000;

  const decorate = () => {
    for (const node of legend.querySelectorAll(SWATCHES)) {
      if (node.hasAttribute('tabindex')) continue;
      node.setAttribute('tabindex', '0');
      node.setAttribute('role', 'button');
      node.setAttribute('aria-label', `Toggle ${node.textContent.trim()}`);
      node.addEventListener('keydown', ev => {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        ev.preventDefault();
        refocus = node.textContent.trim();
        clearTimeout(refocusTimer);
        refocusTimer = setTimeout(() => { refocus = null; }, REFOCUS_WINDOW_MS);
        // Mosaic's Toggle interactor listens for pointerdown on the legend
        // root and reads the swatch from the event target, so a synthetic
        // pointerdown is the exact equivalent of a mouse click.
        // Enter or Space adds or removes the engine, as a click does (clicksAddToggles).
        node.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, shiftKey: true }));
      });
    }
    // Mosaic signals the deselected state with opacity alone; mirror it for
    // assistive tech. The observer also fires on this attribute change, so
    // guard against writing a value that is already set.
    for (const node of legend.querySelectorAll(SWATCHES)) {
      const pressed = String(node.style.opacity !== '0.2');
      if (node.getAttribute('aria-pressed') !== pressed) node.setAttribute('aria-pressed', pressed);
      if (refocus !== null && node.textContent.trim() === refocus && document.activeElement !== node) {
        refocus = null;
        node.focus();
      }
    }
  };
  new MutationObserver(decorate).observe(legend, {
    childList: true, subtree: true, attributes: true, attributeFilter: ['style']
  });
  decorate();
  return legend;
}

/**
 * Tie each Mosaic menu's <label> to its <select>.
 *
 * `vg.menu({ label })` renders the label beside the select without `for`, so
 * the select has no accessible name; Mosaic's slider already links its own.
 *
 * @param {HTMLElement} root The element holding the menus.
 * @returns {HTMLElement} The same element, for chaining.
 */
let menuIds = 0;
export function labelMenus(root) {
  for (const label of root.querySelectorAll('.input > label')) {
    const select = label.parentElement.querySelector('select');
    if (!select || select.labels.length) continue;
    select.id ||= `menu-${++menuIds}`;
    label.htmlFor = select.id;
  }
  return root;
}

/**
 * Make a plain click or tap on a swatch add or remove that engine.
 *
 * Mosaic's Toggle interactor shows only the clicked engine, and adds or removes
 * one on a shift-click. Readers rarely find the shift-click, and a touch screen
 * has none, so choosing two or three engines looks impossible. With this, the
 * first click still shows only that engine (a shift-click on an empty selection
 * does exactly that), and each later click adds or removes one; the Select all
 * button next to the legend brings every engine back.
 *
 * @param {HTMLElement} legend The element returned by `vg.colorLegend`.
 * @returns {HTMLElement} The same element, for chaining.
 */
export function clicksAddToggles(legend) {
  legend.addEventListener('pointerdown', ev => {
    // Our own re-dispatches (the keyboard, Select all, a shared link) are
    // untrusted and pass straight through, as does a real shift- or cmd-click.
    if (!ev.isTrusted || ev.shiftKey || ev.metaKey) return;
    ev.stopImmediatePropagation();
    ev.target.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, shiftKey: true }));
  }, { capture: true });
  return legend;
}
