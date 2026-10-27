/**
 * The "bottom line" under each chart.
 *
 * Every sentence here is computed by DuckDB from the same views the chart above
 * it reads, under the same filters (data set, engines, SLA), and recomputed on
 * every change. A hand-written summary would be true for one filter state and
 * silently wrong for the rest; these cannot disagree with the chart.
 *
 * The page deliberately has no composite ranking. Each line names the leader on
 * one measured axis and the caveat that qualifies it, and stops there.
 */
import { coordinator } from '@uwdata/vgplot';
import { CLICKBENCH, DEFAULT_SCALE } from './constants.js';
import { CONFIDENCE_LEVELS, ENGINES } from './engines.js';
import { ms, pct, ratio, usd, usdExact } from './format.js';

const byId = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const b = s => `<b>${esc(s)}</b>`;
const num = v => Number(v);
const fmtSla = v => (v >= 1000 ? `${(v / 1000).toFixed(1).replace(/\.0$/, '')} s` : `${v} ms`);
const times = n => `${Number(n).toLocaleString(undefined, { maximumFractionDigits: n >= 10 ? 0 : 1 })}×`;
const list = items => (items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`);
/** Engines in the page's fixed order, so a list reads the same every time. */
const inOrder = names => ENGINES.filter(e => names.includes(e));

/** Current filter state, read from the same Selections and Params the plots use. */
function state({ $scale, $engines, $sla }) {
  const raw = $scale.value;
  const engines = Array.isArray($engines.value) && $engines.value.length ? $engines.value.map(p => p[0]) : null;
  return { ds: raw == null ? String(DEFAULT_SCALE) : String(raw), engines, sla: Number($sla.value) };
}

/** WHERE clause for the current state; `dataset: false` for views without that column. */
function where(s, { dataset = true } = {}) {
  const parts = [];
  if (dataset) parts.push(`dataset = '${s.ds}'`);
  if (s.engines) parts.push(`engine IN (${s.engines.map(e => `'${e.replace(/'/g, "''")}'`).join(', ')})`);
  return parts.length ? `WHERE ${parts.join(' AND ')}` : '';
}

const q = sql => coordinator().query(sql, { type: 'json' });
const minBy = (rows, f) => rows.reduce((a, r) => (f(r) < f(a) ? r : a));
const maxBy = (rows, f) => rows.reduce((a, r) => (f(r) > f(a) ? r : a));

/** Useful concurrency per (engine, size) at the current SLA: the frontier's own aggregate. */
const configsSql = s => `
  SELECT engine, compute_label, any_value(usd_per_hour) AS usd,
         coalesce(max(concurrency) FILTER (WHERE p50_ms <= ${s.sla} AND shed_pct < sla_max_shed_pct), 0) AS sessions
  FROM concurrency ${where(s)} GROUP BY 1, 2`;

// ── At a glance: the page's bottom line ──────────────────────────────────
async function glance(s) {
  const cb = s.ds === CLICKBENCH;
  const [rows, runs, skew] = await Promise.all([
    q(`SELECT engine, frontier_ms, frontier_cost, cost_confidence FROM engine_scale ${where(s)}`),
    q(`SELECT engine, 100.0 * sum(failures) / sum(runs) AS failed FROM warm_runs ${where(s)} GROUP BY 1`),
    cb ? Promise.resolve([]) : q(`SELECT engine, median(delta_pct) AS d FROM skew_warm_clean ${where(s)} GROUP BY 1`)
  ]);
  if (!rows.length) return 'No engines match the current filters.';
  const fastest = minBy(rows, r => num(r.frontier_ms));
  const priced = rows.filter(r => r.frontier_cost != null);
  const cheapest = priced.length ? minBy(priced, r => num(r.frontier_cost)) : fastest;
  const out = [];
  // A missing cost is NULL; Number(null) is 0, which would print "$0".
  const costOf = r => (r.frontier_cost == null ? null : num(r.frontier_cost));
  if (rows.length === 1) {
    const c = costOf(fastest);
    out.push(`${b(fastest.engine)}: ${ms(num(fastest.frontier_ms))} for the median query, ${c == null
      ? 'with no cost recorded' : `at ${usd(c)} per query`}.`);
  } else if (!priced.length) {
    out.push(`${b(fastest.engine)} had the fastest median query, at ${ms(num(fastest.frontier_ms))}. No cost is shown for the engines selected.`);
  } else if (fastest.engine === cheapest.engine) {
    out.push(`${b(fastest.engine)} had both the fastest median query (${ms(num(fastest.frontier_ms))}) and the lowest cost per query (${usd(num(fastest.frontier_cost))}).`);
  } else {
    out.push(`${b(fastest.engine)} had the fastest median query, at ${ms(num(fastest.frontier_ms))}. ${b(cheapest.engine)} had the lowest cost per query, at ${usd(num(cheapest.frontier_cost))}.`);
  }
  const failed = runs.filter(r => num(r.failed) > 0).sort((p, r) => num(r.failed) - num(p.failed));
  if (!failed.length) out.push('Every engine completed every measured run.');
  else out.push(`${list(failed.map(r => `${b(r.engine)} did not complete ${pct(num(r.failed))} of its runs`))}, and ${failed.length > 1 ? 'their' : 'its'} speed and cost cover only the runs that finished.`);
  if (!cb && skew.length > 1) {
    const faster = skew.filter(r => num(r.d) < 0).length;
    const worst = maxBy(skew, r => num(r.d));
    out.push(num(worst.d) > 0
      ? `The skewed version of the queries ran faster on ${faster} of ${skew.length} engines. It ran slowest, relative to uniform data, on ${b(worst.engine)}, at +${pct(num(worst.d))}.`
      : `The skewed version of the queries ran faster than the uniform version on all ${skew.length} engines.`);
  }
  // Best grade first, whatever order the rows arrived in.
  const grades = CONFIDENCE_LEVELS.filter(g => rows.some(r => r.cost_confidence === g));
  if (grades.length > 1) {
    out.push(`<span class="synth-caveat">These costs combine ${list(grades)} figures (shown by dot shape), so treat small cost differences between shapes as approximate.</span>`);
  }
  return out.join(' ');
}

// ── 1. Frontier ──────────────────────────────────────────────────────────
async function frontier(s) {
  const rows = await q(configsSql(s));
  if (!rows.length) return 'No configurations match the current filters.';
  const sla = fmtSla(s.sla);
  const served = rows.filter(r => num(r.sessions) > 0);
  if (!served.length) return `No size held a median response under ${sla} at any session count for this selection. A longer latency target will show which sizes come closest.`;
  const top = served.reduce((a, r) =>
    (num(r.sessions) > num(a.sessions) || (num(r.sessions) === num(a.sessions) && num(r.usd) < num(a.usd)) ? r : a));
  // Every configuration tied with the leader on both sessions and price is
  // named: at SF100 two sizes serve 100 sessions at the same hourly rate, and
  // naming whichever row arrived first would be an arbitrary pick.
  const leaders = served.filter(r => num(r.sessions) === num(top.sessions) && num(r.usd) === num(top.usd));
  const cfg = r => `${b(r.engine)} (${esc(r.compute_label)})`;
  const perSession = r => num(r.usd) / num(r.sessions);
  const perPerson = d => (d >= 0.01 ? usdExact(d) : `$${Number(d).toPrecision(2)}`);
  const best = minBy(served, perSession);
  const zeros = rows.length - served.length;
  const out = [`With a ${sla} latency target, ${list(leaders.map(cfg))} served the most people at once, ${num(top.sessions)}, at ${usdExact(num(top.usd))} an hour.`];
  const bestLeads = leaders.some(r => r.engine === best.engine && r.compute_label === best.compute_label);
  if (!bestLeads) {
    out.push(`The lowest hourly price per person served was ${cfg(best)}, at ${perPerson(perSession(best))}.`);
  } else {
    out.push(`${leaders.length > 1 ? 'They also had' : 'It also had'} the lowest hourly price per person served, ${perPerson(perSession(best))}.`);
  }
  if (zeros) out.push(`${zeros} of ${rows.length} sizes served no one within this target.`);
  return out.join(' ');
}

// ── 2. Latency under load ────────────────────────────────────────────────
async function degradation(s) {
  const [rows, shed] = await Promise.all([
    q(`WITH c AS (${configsSql(s)}) SELECT engine, max(sessions) AS best FROM c GROUP BY 1`),
    q(`SELECT DISTINCT engine FROM concurrency ${where(s)} AND shed_pct >= sla_max_shed_pct`)
  ]);
  if (!rows.length) return 'No engines match the current filters.';
  const sla = fmtSla(s.sla);
  const top = Math.max(...rows.map(r => num(r.best)));
  const low = Math.min(...rows.map(r => num(r.best)));
  // Every engine tied at the top is named: at 100 GB and 5 s two engines reach
  // 100 sessions, and naming whichever row came first would misstate the result.
  const leaders = inOrder(rows.filter(r => num(r.best) === top).map(r => r.engine));
  const trailers = inOrder(rows.filter(r => num(r.best) === low).map(r => r.engine));
  const out = [];
  if (top === 0) {
    out.push(`No engine kept its median response under ${sla} at any session count for this selection.`);
  } else if (rows.length === 1) {
    out.push(`${b(leaders[0])} kept its median response under ${sla} up to ${top} sessions at its best size.`);
  } else {
    out.push(`${list(leaders.map(b))} kept the median response under ${sla} up to ${top} sessions at ${leaders.length > 1 ? 'their' : 'its'} best size.`);
    if (low < top) {
      out.push(low === 0
        ? `${list(trailers.map(b))} did not stay under ${sla} at any session count.`
        : `${list(trailers.map(b))} stayed under it only up to ${low}.`);
    }
  }
  const shedders = inOrder(shed.map(r => r.engine));
  out.push(shedders.length
    ? `${list(shedders.map(b))} left 5% or more of queries unfinished at one or more levels (open rings), and those levels do not count as served.`
    : 'No engine left 5% or more of queries unfinished at any level.');
  return out.join(' ');
}

// ── 3. Query patterns ────────────────────────────────────────────────────
async function patterns(s) {
  const rows = await q(`SELECT engine, pattern, median(rel) AS r FROM pattern_load ${where(s)} GROUP BY 1, 2`);
  if (!rows.length) return 'No pattern data matches the current filters.';
  const hardest = new Map();
  for (const r of rows) {
    const h = hardest.get(r.engine);
    if (!h || num(r.r) > num(h.r)) hardest.set(r.engine, r);
  }
  const counts = new Map();
  for (const r of hardest.values()) counts.set(r.pattern, (counts.get(r.pattern) ?? 0) + 1);
  const [mode, n] = [...counts.entries()].sort((p, r) => r[1] - p[1])[0];
  const peak = maxBy([...hardest.values()], r => num(r.r));
  const name = p => esc(String(p).toLowerCase());
  const lead = hardest.size === 1
    ? `Taking every session count together, ${b(peak.engine)} was slowest on ${name(peak.pattern)}, at ${ratio(num(peak.r))} its typical query.`
    : `Taking every session count together, ${b(name(mode))} was the slowest pattern relative to each engine's typical query on ${n} of ${hardest.size} engines. The largest of those medians was ${b(peak.engine)} on ${name(peak.pattern)}, at ${ratio(num(peak.r))} its typical query; the chart shows each session count separately.`;
  return lead;
}

// ── 4. Skew under load ───────────────────────────────────────────────────
async function skewLoad(s) {
  const rows = await q(`SELECT engine, median(skew_ratio) AS r FROM skew_load ${where(s)} GROUP BY 1`);
  if (!rows.length) return 'No skew pairs match the current filters.';
  const worst = maxBy(rows, r => num(r.r));
  const best = minBy(rows, r => num(r.r));
  if (rows.length === 1) return `Under load, ${b(worst.engine)} ran the skewed queries in ${ratio(num(worst.r))} the time of their uniform twins.`;
  const faster = rows.filter(r => num(r.r) < 1).length;
  const out = [`Under load, the skewed queries ran faster than their uniform twins (a ratio below 1×) on ${faster} of ${rows.length} engines.`];
  out.push(num(worst.r) > 1
    ? `${b(worst.engine)} ran them slower, at ${ratio(num(worst.r))}, and ${b(best.engine)} ran them fastest, at ${ratio(num(best.r))}.`
    : `The ratio was highest on ${b(worst.engine)}, at ${ratio(num(worst.r))}, and lowest on ${b(best.engine)}, at ${ratio(num(best.r))}.`);
  return out.join(' ');
}

// ── 5. Latency vs data volume (spans scales, so engines only) ────────────
async function latency(s) {
  const rows = await q(`
    WITH m AS (SELECT engine, scale_factor, median(median_server_ms) AS ms FROM curves ${where(s, { dataset: false })} GROUP BY 1, 2)
    SELECT engine, arg_min(ms, scale_factor) AS lo_ms, arg_max(ms, scale_factor) AS hi_ms,
           min(scale_factor) AS lo, max(scale_factor) AS hi
    FROM m GROUP BY 1`);
  if (!rows.length) return 'No engines match the current filters.';
  // Compare growth over one range: the widest any engine shows, and only the
  // engines measured at both ends of it, naming any left out.
  const VOL = { 1: '1 GB', 10: '10 GB', 100: '100 GB', 1000: '1 TB' };
  const lo = Math.min(...rows.map(r => num(r.lo)));
  const hi = Math.max(...rows.map(r => num(r.hi)));
  const full = rows.filter(r => num(r.lo) === lo && num(r.hi) === hi);
  const out = rows.filter(r => !full.includes(r)).map(r => b(r.engine));
  const growth = r => num(r.hi_ms) / num(r.lo_ms);
  const range = `from ${VOL[lo] ?? lo} to ${VOL[hi] ?? hi}`;
  const left = out.length ? ` ${list(out)} ${out.length > 1 ? 'are' : 'is'} left out because ${out.length > 1 ? 'they were' : 'it was'} not measured across that whole range.` : '';
  if (!full.length) return 'No engine was measured across the whole range of data volumes shown.';
  const least = minBy(full, growth);
  const most = maxBy(full, growth);
  if (full.length === 1) return `${b(least.engine)}'s median query took ${times(growth(least))} as long at ${VOL[hi] ?? hi} as at ${VOL[lo] ?? lo}.${left}`;
  return `${range.charAt(0).toUpperCase()}${range.slice(1)}, the median query time grew least on ${b(least.engine)} (${times(growth(least))}) and most on ${b(most.engine)} (${times(growth(most))}).${left}`;
}

// ── 6. ClickBench ────────────────────────────────────────────────────────
async function clickbench(s) {
  const rows = await q(`SELECT engine, any_value(tier) AS tier, median(median_server_ms) AS ms FROM clickbench ${where(s, { dataset: false })} GROUP BY 1`);
  if (!rows.length) return 'No engines match the current filters.';
  const fast = minBy(rows, r => num(r.ms));
  const slow = maxBy(rows, r => num(r.ms));
  if (rows.length === 1) return `${b(fast.engine)}'s median scan query took ${ms(num(fast.ms))} at its entry tier.`;
  return `At each engine's entry tier (${list(rows.slice().sort((p, r) => p.engine.localeCompare(r.engine)).map(r => `${r.engine} ${r.tier}`))}), the median scan query was fastest on ${b(fast.engine)}, at ${ms(num(fast.ms))}, and slowest on ${b(slow.engine)}, at ${ms(num(slow.ms))}, which is ${times(num(slow.ms) / num(fast.ms))} as long.`;
}

// ── 4. What a bigger size buys ───────────────────────────────────────────
async function sizing(s) {
  const rows = await q(`
    WITH l AS (SELECT *, count(*) OVER (PARTITION BY platform) AS sizes FROM ladder_suite ${where(s)})
    SELECT engine, compute_label, speedup, cost_multiple, usd_per_hour, queries FROM l
    WHERE sizes > 1 AND compute_label <> base_label AND cost_multiple > 0`);
  if (!rows.length) return 'No engine was measured at more than one size for this selection.';
  const paid = rows.filter(r => num(r.speedup) > num(r.cost_multiple));
  const best = maxBy(rows, r => num(r.speedup));
  const size = r => `${b(r.engine)} ${esc(String(r.compute_label).replace(/ \(Gen2\)$/, '').replace(/^ENTERPRISE \/ /, ''))}`;
  const out = [`Of the ${rows.length} sizes larger than each engine's cheapest that have a recorded cost, ${paid.length} had a speedup greater than their cost multiple, so they cost less per unit of speed than the cheapest size.`];
  out.push(`The largest speedup was ${size(best)}, ${ratio(num(best.speedup))} faster than the engine's cheapest size for ${ratio(num(best.cost_multiple))} the cost, over the ${num(best.queries)} queries every ${b(best.engine)} size completed.`);
  return out.join(' ');
}

// ── 5. Scan queries by price ─────────────────────────────────────────────
async function scan(s) {
  const rows = await q(`SELECT engine, compute_label, typical_ms, usd_per_hour FROM size_typical
    WHERE kind = 'scan' AND usd_per_hour IS NOT NULL ${s.engines ? `AND ${where(s, { dataset: false }).replace(/^WHERE /, '')}` : ''}`);
  if (!rows.length) return 'No engines match the current filters.';
  const fast = minBy(rows, r => num(r.typical_ms));
  const label = r => `${b(r.engine)} ${esc(String(r.compute_label).replace(/ \(Gen2\)$/, '').replace(/^ENTERPRISE \/ /, ''))}`;
  const out = [`The fastest scan size measured was ${label(fast)}, with a typical query of ${ms(num(fast.typical_ms))} at ${usdExact(num(fast.usd_per_hour))} an hour.`];
  const budget = Number(byId('scan-budget')?.value) || 3;
  const under = rows.filter(r => num(r.usd_per_hour) <= budget);
  if (under.length) {
    const cheap = minBy(under, r => num(r.typical_ms));
    if (cheap !== fast) out.push(`Among sizes listed at ${usdExact(budget)} an hour or less, the fastest was ${label(cheap)}, at ${ms(num(cheap.typical_ms))}.`);
  }
  return out.join(' ');
}

/** Element id -> builder. Each id wraps a <p> that receives the sentence. */
const TARGETS = [
  ['syn-glance', glance],
  ['syn-frontier', frontier],
  ['syn-degradation', degradation],
  ['syn-sizing', sizing],
  ['syn-scan', scan],
  ['syn-patterns', patterns],
  ['syn-skew', skewLoad],
  ['syn-latency', latency],
  ['syn-clickbench', clickbench]
];

/**
 * Fill every bottom line now and again on every filter change. A version
 * counter drops results that arrive after a newer state was requested.
 * @param {ReturnType<import('./controls.js').buildControls>} controls
 */
export function mountSynthesis(controls) {
  const { $scale, $engines, $sla } = controls;
  let version = 0;
  const run = async () => {
    const v = ++version;
    const s = state(controls);
    await Promise.all(TARGETS.map(async ([id, build]) => {
      const el = byId(id)?.querySelector('p');
      if (!el) return;
      try {
        const html = await build(s);
        if (v === version) el.innerHTML = html;
      } catch (err) {
        console.warn(`bottom line ${id}:`, err);
        if (v === version) el.textContent = 'Summary unavailable for this selection.';
      }
    }));
  };
  for (const source of [$scale, $engines, $sla]) source.addEventListener('value', run);
  byId('scan-budget')?.addEventListener('budget', run);
  run();
}
