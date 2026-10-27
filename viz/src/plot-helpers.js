/**
 * Plot attribute bundles shared by more than one view.
 *
 * Each returns an array of vgplot attribute directives to spread into a
 * `vg.plot(...)` call, so the plots themselves stay readable Mosaic literals
 * and the repeated decisions (engine colours, the seven-panel facet layout,
 * the sessions axis) are made once.
 */
import * as vg from '@uwdata/vgplot';
import { COMPUTE_PRICE_RANGE } from './constants.js';
import { ALL_ROUND_STEPS, roundTickSet } from './format.js';
import { ENGINES, COLORS } from './engines.js';

/**
 * Plot widths follow the content column the page lays out in `style.css`.
 *
 * The page is a two-column grid on wide screens (a sticky view nav beside the
 * content) and a single column below `NAV_BREAKPOINT`; the numbers here mirror
 * the CSS custom properties so the plots fill the column they are given rather
 * than a fixed text measure. On a phone the width shrinks to the viewport so
 * the single-column plots fit; the faceted plots keep a floor of
 * `FACET_MIN_WIDTH` because seven panels cannot be read narrower than that,
 * and horizontal scroll is the lesser evil (`.ladder` in `style.css` lets
 * them keep that width and scroll inside their card).
 *
 * Widths are measured when a page mounts (`measurePlots`, called by
 * src/site/router.js before each page's code runs, and once at import). The
 * site is one document whose pages swap in place, so the layout they measure
 * (a side nav on Compare only) changes with the page; the exports are live
 * bindings, so every view reads the current values when it renders. Plot gives
 * every SVG `max-width: 100%`, so a window that later shrinks scales the plots
 * down; one that grows leaves them at the width they were given.
 */
const CONTENT_MAX = 1080;      // matches --content-max
const NAV_WIDTH = 220;         // matches --nav-w
const NAV_GAP = 40;            // matches the grid gap
const NAV_BREAKPOINT = 1100;   // matches the @media rule that hides the side nav
const PAGE_GUTTER = 64;        // body padding, both sides
const CARD_INSET = 50;         // .view card: 24px padding each side + 1px borders
const PHONE_BREAKPOINT = 640;  // matches the @media rule that tightens the gutters
const PHONE_GUTTER = 32;       // body padding on a phone, both sides
const PHONE_CARD_INSET = 30;   // .view card on a phone: 14px padding each side + 1px borders
const FACET_MIN_WIDTH = 812;

/**
 * The at-a-glance panel puts the cost-speed quadrant beside two stacked bar
 * charts. Side by side needs room; below `GLANCE_SPLIT_MIN` the three stack
 * full-width instead (the `.duo` flex row wraps to match).
 */
const GLANCE_SPLIT_MIN = 760;
const GLANCE_GAP = 20;

/**
 * Round ticks for a log price axis (`roundTickSet` in format.js picks which).
 * It is a Plot tick interval rather than a tick array because Plot draws an
 * array as given, even outside the axis range, where a stray label lands in
 * the neighbouring facet panel (a "$1" beside the previous panel's "$10" reads
 * as "$100"); the ticks of an interval are cut to the range. Plot asks for
 * `range(min, offset(floor(max)))`, and every set is a subset of the steps
 * `floor` and `offset` walk, so no chosen tick lies past `max`.
 * @param {number} maxLabels most labels a panel has room for
 */
export const roundTicks = maxLabels => ({
  floor: x => ALL_ROUND_STEPS.findLast(s => s <= x) ?? ALL_ROUND_STEPS[0],
  offset: x => ALL_ROUND_STEPS.find(s => s > x) ?? Infinity,
  range: (lo, hi) => roundTickSet(lo, hi, maxLabels, false)
});

/** Width of a single-column plot: the interior of the card it sits in. */
export let PLOT_WIDTH = 320;
/** Faceted plots: as wide as the column allows, never below the readable floor. */
export let WIDE_WIDTH = FACET_MIN_WIDTH;
export let GLANCE_MAIN_WIDTH = 320;
export let GLANCE_SIDE_WIDTH = 320;
/**
 * Below this the at-a-glance plots are phone-sized: the quadrant gives up its
 * wide right margin (the corner captions it held are hidden by `style.css` at
 * the matching breakpoint) and the skew-tax axis takes a label that fits.
 */
export let GLANCE_NARROW = true;

/** Measure the content column of the page now in the document and size the plots to it. */
export function measurePlots() {
  const viewport = document.documentElement.clientWidth;
  const phone = viewport <= PHONE_BREAKPOINT;
  // Only the Compare page has the side navigation (#toc); every other page
  // gives its content the full column, and subtracting the nav there would draw
  // each chart about 260 px narrower than its card.
  const hasNav = !!document.getElementById('toc');
  const column = viewport >= NAV_BREAKPOINT && hasNav
    ? viewport - NAV_WIDTH - NAV_GAP - PAGE_GUTTER
    : viewport - (phone ? PHONE_GUTTER : PAGE_GUTTER);
  // Every plot sits inside a padded card, so the budget is the card's interior.
  const available = Math.min(CONTENT_MAX, column) - (phone ? PHONE_CARD_INSET : CARD_INSET);
  PLOT_WIDTH = Math.max(320, available);
  WIDE_WIDTH = Math.max(FACET_MIN_WIDTH, PLOT_WIDTH);
  const split = PLOT_WIDTH >= GLANCE_SPLIT_MIN;
  GLANCE_MAIN_WIDTH = split ? Math.round(PLOT_WIDTH * 0.58) : PLOT_WIDTH;
  // 2px under the exact budget: a flex row wraps the moment its children reach it.
  GLANCE_SIDE_WIDTH = split ? PLOT_WIDTH - GLANCE_MAIN_WIDTH - GLANCE_GAP - 2 : PLOT_WIDTH;
  GLANCE_NARROW = GLANCE_MAIN_WIDTH < 480;
}
measurePlots();

/** A SQL list literal from JS values: `['a', 'b']` -> `['a', 'b']`, `[1, 5]` -> `[1, 5]`. */
export const sqlList = items => `[${items.map(v => (typeof v === 'string' ? `'${v}'` : v)).join(', ')}]`;

/**
 * In the faceted ladders the engine is the panel title, so colour is free to
 * encode the one thing those views are about: the size of the compute. Lines
 * are coloured by the size's list price per hour on a log ramp, darker for
 * bigger, shared across every panel and every ladder view so a shade means the
 * same money everywhere. The domain is pinned to the campaign's price range
 * (`COMPUTE_PRICE_RANGE`) so the ramp does not shift when a filter changes.
 */
export const computeColor = () => [
  vg.colorScale('log'),
  vg.colorDomain(COMPUTE_PRICE_RANGE),
  // Multi-hue, lightness-monotone ramp (teal, blue, indigo, navy): adjacent
  // sizes differ in hue as well as darkness, which a single-hue ramp cannot
  // manage at 2 px. Red is kept out of it for the SLA rule and the rings.
  vg.colorRange(['#7fcdbb', '#41b6c4', '#1d91c0', '#225ea8', '#253494', '#081d58']),
  vg.colorLabel('Warehouse size by list price per hour (darker is more expensive)')
];

/** One phrase for the price ramp wherever it is described. */
export const PRICE_RAMP_LABEL = 'Warehouse size by list price per hour (darker is more expensive)';

/** The red of the latency target and the "did not complete" rings. */
const TARGET_RED = '#c0392b';

/**
 * A key for the response-time charts' two red marks, shown beside the price
 * legend, so a reader looking at the chart can tell what they mean without the
 * text above it.
 * @param {number} [limitPct] The share of queries not completed that rings a level.
 * @returns {HTMLElement}
 */
export function ringKey(limitPct = 5) {
  const el = document.createElement('p');
  el.className = 'ring-key';
  el.innerHTML = `
    <span><svg width="14" height="14" aria-hidden="true"><circle cx="7" cy="7" r="4.5" fill="white" stroke="${TARGET_RED}" stroke-width="1.6"/></svg>
      Red ring: ${limitPct}% or more of the queries did not complete at that level, so it never counts as served</span>
    <span><svg width="22" height="14" aria-hidden="true"><line x1="1" y1="7" x2="21" y2="7" stroke="${TARGET_RED}" stroke-width="1.2" stroke-dasharray="4,3"/></svg>
      Red dashed line: the latency target</span>`;
  return el;
}

/**
 * Direct labels at the end of the cheapest and priciest line in each panel.
 * Two marks: the cheapest size's label sits above its end point and the
 * priciest's below, because at the top session count the two ends often land
 * a few pixels apart and one label would cover the other.
 * @returns {Array} Two text marks to spread into a plot.
 */
export const priceEndLabels = (src, filterBy, y) => [['lo', -9], ['hi', 11]].map(([end, dy]) =>
  vg.text(vg.from(`${src}_${end}`, { filterBy }), {
    x: 'concurrency',
    y,
    fx: 'engine',
    // The short form (data.js): the full label is wider than a panel.
    text: 'short_label',
    // Left of the endpoint: a label to the right runs off the panel edge. The
    // white halo keeps it legible where it crosses the line's last segment.
    dx: -6,
    dy,
    textAnchor: 'end',
    fontSize: 11,
    fontWeight: 600,
    fill: '#04192b',
    stroke: 'white',
    strokeWidth: 3,
    paintOrder: 'stroke'
  }));

/** Pin the colour scale so an engine keeps its colour even when deselected. */
export const engineColor = () => [vg.colorDomain(ENGINES), vg.colorRange(COLORS)];

/**
 * One panel per engine, sharing both axes so heights stay comparable.
 *
 * `fxPadding` widens the gap between panels: without it the last x tick of one
 * panel ("100") runs into the first of the next ("1"). The y-label anchor is
 * centred because a top-anchored label collides with the facet titles.
 */
export const engineFacets = () => [
  vg.fxDomain(ENGINES),
  vg.fxLabel(null),
  vg.fxPadding(0.18),
  vg.yLabelAnchor('center'),
  vg.width(WIDE_WIDTH),
  vg.height(330),
  vg.marginTop(22),
  vg.marginBottom(40)
];

/**
 * The concurrency-ladder x axis: log over 1..100 sessions, with the domain
 * padded so the end points are not clipped and ticks only at the decades.
 */
export const sessionsAxis = () => [
  vg.xScale('log'),
  vg.xDomain([0.8, 130]),
  vg.xTicks([1, 10, 100]),
  vg.xLabel('Concurrent sessions →')
];

/**
 * Tooltip text options for every chart. Plot's tip cuts each line at 20 ems
 * with an ellipsis, which hides the value behind long channel names ("Typical
 * scan query time (log scale) 1…."). A wider line, and wrapping instead of
 * cutting, keeps every number readable.
 */
export const TIP_TEXT = { lineWidth: 40, textOverflow: null };
