/**
 * The hand-written caveats on the engine page's size ladder: text the data
 * cannot state for itself. Everything else about a gap (which volume, which
 * size ran, how many sizes the concurrency grid ran there) is computed from the
 * exports at render time, so it updates, and disappears, as new runs land.
 *
 * Each entry here is tied to the data it explains, and `tests/engine.spec.js`
 * ("stale caveat") fails, naming the entry, once the data no longer matches:
 * when a gap below has been filled, or a note's claim no longer holds. After
 * new results are analyzed (`benchmark analyze`), rebuild the explorer and run
 * its tests; remove or reword whatever a stale-caveat failure names.
 *
 * No imports, so the tests can read this module directly.
 */

/**
 * Why an engine has no ladder at a volume, where the reason is not in the
 * exports: `{ platform: { scale_factor: { when, html } } }`. A size skipped on
 * purpose is in them (site/skips.js lists it from scale_skips.parquet), so
 * this holds no entries. `when` is the state the text describes: `none` (no size in the single-user export at that
 * volume) or `single` (one size). Shown only while that volume has fewer than
 * two sizes; the stale-caveat test holds the text to its `when`.
 */
export const LADDER_GAP_REASONS = {};

/**
 * A note on the chart itself, for an engine whose ladder reads oddly without
 * one. `claim` is what the data must keep showing for the note to stay:
 * `flat.scales` lists the volumes where every size's suite time is within
 * `flat.tolerance` of the cheapest size's; `slower` names a volume where the
 * `small` size took between `ratio[0]` and `ratio[1]` times as long as `large`.
 */
export const LADDER_NOTES = {
  fabric: {
    html: "<b>Why the blue line is flat up to 10 GB:</b> in this study, at those volumes Fabric's capacity size mainly changed how many simultaneous queries it absorbed, not how fast one query runs. At 100 GB the three smallest capacities, F2, F4 and F8, each took about twice as long as F32 to run the suite; F16 and larger were close to F32. The smaller capacities also borrowed compute beyond their size, which Fabric bills, so at 100 GB a run on F2 cost about seven times its list price for the time it took, and the cost multiples here include that. The table of people served, further down, shows how many people each size could serve at once.",
    claim: {
      flat: { scales: [1, 10], tolerance: 1.1 },
      slower: { scale: 100, small: 'F2', large: 'F32', ratio: [1.8, 2.2] }
    }
  }
};
