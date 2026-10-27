/**
 * Cells left out of the study on purpose, read from `scale_skips.parquet`
 * (the results DB's `scale_skips` table, recorded with `benchmark skip-scale`).
 *
 * A skip names an engine and a data volume, and a suite, a compute size and a
 * pass (`warm` for the single-user pass, `concurrency` for the grid), each of
 * which may be `*` for all. The engine page lists an engine's skips with their
 * recorded reasons, so a blank is never mistaken for a run that failed or one
 * still to come. Each reason is shown in reader's words (READER_REASONS), not
 * the recorded shorthand.
 *
 * No imports, so the tests can read this module directly.
 */

/** A volume as the pages name it. */
const VOLUME = { 1: '1 GB', 10: '10 GB', 100: '100 GB', 1000: '1 TB' };

/** Suites as the pages name them. */
const SUITE = { tpcds: 'TPC-DS', dsb: 'DSB', clickbench: 'the scan queries' };

/** The pass a page section shows: the single-user ladder or the concurrency grid. */
export const PASS = { single: 'warm', load: 'concurrency' };

/** SQL for one engine's skips, in a fixed order. */
export const skipsSql = platform => `
  SELECT scale_factor, block, compute_label, run_type, reason FROM scale_skips
  WHERE platform = '${String(platform).replace(/'/g, "''")}'
  ORDER BY scale_factor, block, compute_label`;

/**
 * What each recorded reason means, in words a reader can follow. The recorded
 * text is shorthand ("ejection profile", "DNF", "run plan"); the page shows
 * these instead. Keyed by the recorded reason exactly, so a new skip
 * shows its recorded text until it is written up here, and the "skips" test in
 * tests/engine.spec.js fails, naming it, until then.
 */
export const READER_REASONS = {
  '3x120GB completed 66 of 99 TPC-DS queries single-user at 1 TB (per-replica memory is the ceiling); under load it would only add out-of-memory errors and timeouts':
    'ClickHouse runs each query inside the memory of one replica. At 1 TB, even the largest configuration, three replicas of 120 GB each, finished only 66 of the 99 TPC-DS queries for a single user, and most of the rest ran out of memory. Running many users at once would only have added more of the same failures, so it was not measured.',
  'one replica holds less memory than 3x120GB, which completed 66 of 99 TPC-DS queries at 1 TB (DSB was skipped); the run plan designates three replicas there':
    'ClickHouse runs each query inside the memory of one replica, so a single replica cannot hold a larger query than the three-replica service, which finished only 66 of the 99 TPC-DS queries at 1 TB. The one-replica sizes would have failed at least as often, so the 1 TB single-user pass ran on three 120 GB replicas only, and that run is not part of this one-replica ladder.',
  "TPC-DS established the at-ceiling ejection profile (36% DNF at 3x120GB); DSB's skewed variants of the same schema would re-prove it at hours of timeout cost":
    'DSB runs the same reports as TPC-DS over the same tables, with skewed data. TPC-DS had already shown where ClickHouse runs out of memory at 1 TB, with about a third of its queries unfinished, so DSB would have repeated that result at the cost of many hours of queries waiting out the ten-minute limit.',
  '8 and 16 RPU cannot finish the 1 TB warm suite; 32/64/128 RPU are the measured bands there (run_plan, DNF by design)':
    'The 1 TB single-user pass was planned from 32 RPU up, the smallest size we would start with at that volume. 8 and 16 RPU were measured under load at 1 TB instead.',
  'Duckling tier too small for 1 TB; Jumbo/Mega/Giga are the measured tiers there':
    'These sizes are too small for 1 TB of data, so the study measured Jumbo, Mega and Giga there.',
  'F2 to F16 are too small for 1 TB; F32, F64 and F128 are the measured sizes there':
    'These capacities are too small for 1 TB of data, so the study measured F32, F64 and F128 there.',
  'Pulse is too small to finish the 100 GB suite within the 600 s timeout (at one session three TPC-DS queries timed out); Standard, Jumbo, Mega and Giga are the measured tiers there':
    'It is too small to finish the 100 GB queries within the ten-minute limit: with a single user, three TPC-DS queries timed out. The study measured Standard, Jumbo, Mega and Giga there.',
  'not run: reloading 1 TB into the rebuilt project for one cell is not worth it':
    'Measuring this one size under load would have needed a separate load of the full 1 TB into BigQuery for a single cell, so it was left out. 100 slots was measured with a single user at 1 TB, and every larger reservation was measured under load there.'
};

/**
 * A recorded reason as a reader sees it: its READER_REASONS text, or else the
 * recorded text without the decision's date and owner, starting with a capital
 * and ending with a full stop.
 * @param {string} reason
 */
export function plainReason(reason) {
  if (READER_REASONS[reason]) return READER_REASONS[reason];
  let s = String(reason)
    .replace(/^operator decision \d{4}-\d{2}-\d{2}:\s*/i, '')
    .replace(/^not run:\s*/i, '')
    .replace(/\s*\((?:operator|run_plan)\b[^)]*\)/gi, '')
    .trim();
  s = s.charAt(0).toUpperCase() + s.slice(1);
  return /[.!?]$/.test(s) ? s : `${s}.`;
}

/** True when skip `s` covers `pass` (a PASS value). */
const coversPass = (s, pass) => s.run_type === '*' || s.run_type === pass;

/**
 * The skip that covers one table cell, or undefined. `block` null means the
 * join suites together, which only an every-suite skip covers.
 */
export function skipFor(skips, { sf, label, pass, block = null }) {
  return skips.find(s => Number(s.scale_factor) === Number(sf)
    && (s.compute_label === '*' || s.compute_label === label)
    && coversPass(s, pass)
    && (block == null ? s.block === '*' : s.block === '*' || s.block === block));
}

const and = items => (items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`);

/**
 * One line per (volume, suite, reason) for the skips covering `pass`, naming the
 * sizes: `[{ volume, what, label, reason }]`, where `label` reads "At 1 TB: F2, F4,
 * F8 and F16." and the reason follows it without repeating the sizes. `order`
 * ranks size labels (cheapest first).
 */
export function skipLines(skips, pass, order = []) {
  const groups = new Map();
  for (const s of skips.filter(x => coversPass(x, pass))) {
    const key = `${s.scale_factor}|${s.block}|${s.reason}`;
    if (!groups.has(key)) groups.set(key, { sf: Number(s.scale_factor), block: s.block, reason: s.reason, labels: [] });
    groups.get(key).labels.push(s.compute_label);
  }
  const rank = l => (order.indexOf(l) < 0 ? Infinity : order.indexOf(l));
  return [...groups.values()].map(g => {
    const all = g.labels.includes('*');
    const sizes = all ? 'every size' : and([...g.labels].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b)));
    const suite = g.block === '*' ? '' : `, ${SUITE[g.block] ?? g.block} only`;
    const volume = VOLUME[g.sf] ?? String(g.sf);
    return { volume, what: `${sizes}${suite}`, label: `At ${volume}: ${sizes}${suite}.`, reason: plainReason(g.reason), sf: g.sf };
  });
}
