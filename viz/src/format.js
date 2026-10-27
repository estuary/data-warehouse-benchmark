/**
 * Tick and tooltip formatters shared by every view, so a millisecond or a
 * dollar reads the same wherever it appears.
 */

/** Milliseconds, promoted to seconds at 1,000 ms and above. */
export const ms = d =>
  d >= 1000
    ? `${(d / 1000).toLocaleString(undefined, { maximumFractionDigits: 1 })} s`
    : `${Number(d).toLocaleString(undefined, { maximumFractionDigits: 0 })} ms`;

/**
 * Dollars per query. Costs here run from a hundredth of a cent to a few cents,
 * so trailing zeros are trimmed rather than fixing a decimal count: a linear
 * axis would otherwise read `$0.00000, $0.00100, ...`.
 */
export const usd = d => (d === 0 ? '$0' : `$${Number(d.toFixed(5))}`);

/** Exact dollars and cents, for tables and tooltips: `$64.80`, not `$65`. */
export const usdExact = d => `$${Number(d).toFixed(2)}`;

/** Dollars per hour: list-price capacity rates from $0.36 to $64.80. */
export const usdPerHour = d => `$${d < 10 ? d.toFixed(2) : d.toFixed(0)}`;

/** Round tick sets for a log axis, densest first: 1-2-5, 1-3, then the decades. */
const ROUND_STEPS = [
  [0.1, 0.2, 0.5, 1, 2, 5, 10, 20, 50, 100],
  [0.1, 0.3, 1, 3, 10, 30, 100],
  [0.1, 1, 10, 100]
];
/** Every round step in any set, ascending. */
export const ALL_ROUND_STEPS = [...new Set(ROUND_STEPS.flat())].sort((a, b) => a - b);

/**
 * The densest round tick set with at most `maxLabels` ticks between `lo` and
 * `hi`. Left to itself, Plot labels a narrow log panel at the decades only,
 * so BigQuery's $6-$60 ladder would read "$10" and nothing else.
 * @param {number} lo lowest value on the axis
 * @param {number} hi highest value on the axis
 * @param {number} maxLabels most labels a panel has room for
 * @param {boolean} [inclusive] whether `hi` itself may carry a tick
 */
export function roundTickSet(lo, hi, maxLabels, inclusive = true) {
  const sets = ROUND_STEPS.map(steps => steps.filter(s => s >= lo && (inclusive ? s <= hi : s < hi)));
  return sets.find(set => set.length <= maxLabels) ?? sets.at(-1);
}

/** A round price tick: whole dollars bare (`$20`), cents otherwise (`$0.50`). */
export const roundDollars = d => (Number.isInteger(d) ? `$${d}` : usdPerHour(d));

/** A ratio against a baseline of 1, e.g. `1.4×`. */
export const ratio = d => `${Number(d).toLocaleString(undefined, { maximumFractionDigits: 2 })}×`;

/** A percentage already scaled 0–100. */
export const pct = d => `${Number(d).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
