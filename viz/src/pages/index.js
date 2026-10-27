/**
 * The Overview page: what the study is, how to get to the answer you came for,
 * and the buyer's guide's four findings with their numbers computed live from
 * the same exports every other page reads. The headings follow the guide; the
 * figures under them come from the data, so the two can be checked against
 * each other and a stale sentence in either shows up here.
 */
import { coordinator } from '@uwdata/vgplot';
import { chartTools } from '../site/chart-tools.js';
import { ENGINE_NOTES, IDLE_RULES_AS_OF } from '../site/engine-notes.js';
import { REPORT_LIVE, engineHref, reportHref } from '../site/layout.js';
import { headlineStats } from '../data.js';
import { UNFINISHED_TIE_PP } from '../constants.js';
import { ENGINE_COLOR, ENGINE_LABEL, ENGINES } from '../engines.js';
import { ms, ratio, usdExact } from '../format.js';

const $ = id => document.getElementById(id);
const q = text => coordinator().query(text, { type: 'json' });
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const num = Number;
const short = l => String(l).replace(/^ENTERPRISE \/ /, '').replace(/ \(Gen2\)$/, '');
const swatch = e => `<span class="swatch" style="background:${ENGINE_COLOR[e]}"></span>`;
const byEngine = (a, b) => ENGINES.indexOf(a.engine) - ENGINES.indexOf(b.engine);
const fmt = n => n.toLocaleString('en-US');
/** "A", "A and B", "A, B and C". */
const andList = items => (items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`);
const times = n => `${Number(n).toLocaleString('en-US', { maximumFractionDigits: n >= 10 ? 0 : 1 })} times`;

/** The latency target the guide's findings use, in ms. */
const GUIDE_SLA_MS = 5000;

/** Useful concurrency per (engine, size) at one data set and target. */
const served = (dataset, sla = GUIDE_SLA_MS) => `
  SELECT engine, compute_label, any_value(usd_per_hour) AS usd,
         coalesce(max(concurrency) FILTER (WHERE p50_ms <= ${sla} AND shed_pct < sla_max_shed_pct), 0) AS people
  FROM concurrency WHERE dataset = '${dataset}' GROUP BY 1, 2`;

// ── Finding 1: what a bigger size buys depends on the data volume ─────────
async function findingOne() {
  const rows = await q(`
    SELECT dataset, compute_label, speedup, cost_multiple, usd_per_hour, queries
    FROM ladder_suite WHERE platform = 'snowflake' AND kind = 'join' AND dataset IN ('100', '1000')
    ORDER BY usd_per_hour`);
  const at = (ds, label) => rows.find(r => r.dataset === ds && r.compute_label === label);
  const xl100 = at('100', 'X-Large (Gen2)');
  const xl1000 = at('1000', 'X-Large (Gen2)');
  const lg1000 = at('1000', 'Large (Gen2)');
  const rs = await q(`
    SELECT compute_label, suite_s, suite_cost FROM ladder_suite
    WHERE platform = 'redshift' AND kind = 'join' AND dataset = '1'`);
  const r16 = rs.find(r => r.compute_label === '16 RPU');
  const r128 = rs.find(r => r.compute_label === '128 RPU');
  const r16Fastest = r16 && rs.every(r => num(r.suite_s) >= num(r16.suite_s));
  const sizes = [...new Set(rows.map(r => r.compute_label))];
  const cell = (ds, label) => {
    const r = at(ds, label);
    if (!r) return '<td class="num"></td><td class="num"></td>';
    const paid = num(r.speedup) > num(r.cost_multiple);
    return `<td class="num">${ratio(num(r.speedup))}</td><td class="num${paid ? ' good' : ''}">${ratio(num(r.cost_multiple))}</td>`;
  };
  const table = `<div class="figure table-scroll"><table class="data-table">
    <thead><tr><th rowspan="2">Snowflake size</th><th class="num" colspan="2">At 100 GB</th><th class="num" colspan="2">At 1 TB</th></tr>
      <tr><th class="num">Times as fast</th><th class="num">Times the run's cost</th><th class="num">Times as fast</th><th class="num">Times the run's cost</th></tr></thead>
    <tbody>${sizes.map(l => `<tr><td>${esc(short(l))}</td>${cell('100', l)}${cell('1000', l)}</tr>`).join('')}</tbody>
    <caption>Each size against X-Small, over the ${andList([...new Set(rows.map(r => fmt(num(r.queries))))])} join queries every size completed. The run's cost is the measured cost of running those queries, not the hourly price: a size that is twice the price and more than twice as fast costs less per run. Highlighted costs are where the size ran faster by more than it cost.</caption>
  </table></div>`;
  const redshift = r16 && r128
    ? `<p>At the other end, at 1 GB, Redshift's largest size (128 RPU) ran the join suite
       ${Math.round((num(r128.suite_s) / num(r16.suite_s) - 1) * 100)}% slower than ${r16Fastest ? 'its fastest size, 16 RPU,' : 'its 16 RPU size'} and cost
       ${times(num(r128.suite_cost) / num(r16.suite_cost))} as much.</p>`
    : '';
  $('f1-body').innerHTML = `
    <p>Every vendor sells a menu of compute sizes, and whether a larger size is faster depends on how much
    data it has to work with. On Snowflake at 100 GB, X-Large ran the join queries ${times(num(xl100.speedup))}
    as fast as X-Small for ${times(num(xl100.cost_multiple))} the cost. At 1 TB, Large ran them
    ${times(num(lg1000.speedup))} as fast for ${times(num(lg1000.cost_multiple))} the cost, and X-Large
    ${times(num(xl1000.speedup))} as fast for ${times(num(xl1000.cost_multiple))} the cost.</p>
    ${table}${redshift}
    <p><b>What this means for a buyer:</b> choose the size for the data volume you run today, and move up as the data grows.</p>`;
  chartTools($('home-f1'), {
    title: 'What a bigger Snowflake size buys at 100 GB and at 1 TB',
    subtitle: () => 'Join queries (TPC-DS + DSB), single user, each size against X-Small',
    csv: () => `SELECT dataset AS data_volume_sf, compute_label AS size, usd_per_hour, speedup, cost_multiple, queries
      FROM ladder_suite WHERE platform = 'snowflake' AND kind = 'join' AND dataset IN ('100', '1000') ORDER BY 1, usd_per_hour`
  });
}

// ── Finding 2: the cheapest size that served N people ────────────────────
async function findingTwo() {
  const [rows, big] = await Promise.all([q(served('100')), q(served('1000'))]);
  const engines = ENGINES.filter(e => rows.some(r => r.engine === e));
  const cheapest = (engine, n) => rows.filter(r => r.engine === engine && num(r.people) >= n)
    .sort((a, b) => num(a.usd) - num(b.usd))[0];
  const tableRows = engines.map(engine => {
    const own = rows.filter(r => r.engine === engine).sort((a, b) => num(a.usd) - num(b.usd))[0];
    const cells = [10, 50, 100].map(n => {
      const c = cheapest(engine, n);
      return `<td>${c ? `${esc(short(c.compute_label))}, ${usdExact(num(c.usd))}` : '<span class="muted">Not reached</span>'}</td>`;
    }).join('');
    return { engine, usd: num(own.usd), html: `<tr><td>${swatch(engine)}${esc(engine)}</td>
      <td>${esc(short(own.compute_label))}, ${usdExact(num(own.usd))} (${num(own.people) ? `served ${num(own.people)}` : 'served none'})</td>${cells}</tr>` };
  }).sort((a, b) => a.usd - b.usd);
  const fifty = rows.filter(r => num(r.people) >= 50);
  const perEngine50 = engines.map(e => cheapest(e, 50)).filter(Boolean);
  const lo50 = Math.min(...perEngine50.map(r => num(r.usd)));
  const hi50 = Math.max(...perEngine50.map(r => num(r.usd)));
  const hundred = [...new Set(rows.filter(r => num(r.people) >= 100).map(r => r.engine))];
  const bestBig = Math.max(...big.map(r => num(r.people)));
  const bestBigBy = big.filter(r => num(r.people) === bestBig).sort(byEngine)
    .map(r => `${esc(r.engine)} ${esc(short(r.compute_label))}`);
  $('f2-body').innerHTML = `
    <p>Single-query speed is what vendors usually demonstrate. It says little about how a warehouse behaves
    when a team uses it. The study measured how many <span data-term="people-at-once">people querying at once</span>
    each size served while keeping the median response under five seconds and finishing at least 95% of the
    queries. The table answers the sizing question for 100 GB: for each engine, the cheapest measured size
    that served 10, 50 and 100 people at once.</p>
    <div class="figure table-scroll"><table class="data-table">
      <thead><tr><th>Engine</th><th>Its cheapest size (people served)</th><th>Serving 10</th><th>Serving 50</th><th>Serving 100</th></tr></thead>
      <tbody>${tableRows.map(r => r.html).join('')}</tbody>
      <caption>Join queries at 100 GB, 5 s median latency target, list price per hour. ClickHouse ran three replicas under load; MotherDuck's sessions shared one instance.</caption>
    </table></div>
    <p>The price of serving 50 people at once ranged from ${usdExact(lo50)} to ${usdExact(hi50)} an hour across
    the ${perEngine50.length} engines that reached it (${fifty.length} sizes in all). Only ${hundred.length === 1 ? 'one engine' : `${hundred.length} engines`},
    ${andList(hundred.sort())}, reached 100 people on a single size. At 1 TB, the most any single size served
    was ${bestBig}, by ${andList(bestBigBy)}.</p>
    <p><b>What this means for a buyer:</b> count the people who will have a query running at the same moment,
    and price the warehouse size that serves that number at the data volume you will run.</p>`;
  chartTools($('home-f2'), {
    title: 'The cheapest size that served 10, 50 and 100 people at once, at 100 GB',
    subtitle: () => 'Join queries (TPC-DS + DSB) at about 100 GB · 5 s median latency target · list price per hour',
    csv: () => `${served('100')} ORDER BY engine, usd`
  });
}

// ── Finding 3: joins and scans order the engines differently ─────────────
async function findingThree() {
  let [join, joinFast, scanFast, scanCheap] = await Promise.all([
    q(`SELECT e.engine, e.typical_ms, c.done, c.n FROM engine_scale e
       JOIN (SELECT platform, count(median_server_ms) AS done, count(*) AS n FROM cost_curves
             WHERE scale_factor = 100 AND block IN ('tpcds', 'dsb') GROUP BY 1) c USING (platform)
       WHERE e.dataset = '100' ORDER BY e.typical_ms`),
    q(`SELECT engine, min(typical_ms) AS ms FROM size_typical
       WHERE kind = 'join' AND dataset = '100' AND usd_per_hour IS NOT NULL GROUP BY engine ORDER BY ms`),
    q(`SELECT engine, arg_min(compute_label, typical_ms) AS size, min(typical_ms) AS ms, arg_min(usd_per_hour, typical_ms) AS usd
       FROM size_typical WHERE kind = 'scan' AND usd_per_hour IS NOT NULL GROUP BY engine ORDER BY ms`),
    q(`SELECT engine, arg_min(compute_label, typical_ms) AS size, min(typical_ms) AS ms, arg_min(usd_per_hour, typical_ms) AS usd
       FROM size_typical WHERE kind = 'scan' AND usd_per_hour <= 3 GROUP BY engine ORDER BY ms`)
  ]);
  // The report's completion floor: an engine that did not finish at least 90% of
  // the join suite is shown with its count and left out of the join ranking, since
  // a time over the queries it happened to finish compares an easier suite.
  const COMPLETION_FLOOR = 0.9;
  const allJoin = join;
  const unfinished = allJoin.filter(r => num(r.done) < COMPLETION_FLOOR * num(r.n));
  join = allJoin.filter(r => !unfinished.includes(r));
  const rank = (list, e) => { const i = list.findIndex(r => r.engine === e); return i < 0 ? null : i + 1; };
  const body = ENGINES.map(engine => {
    const j = join.find(r => r.engine === engine);
    const u = unfinished.find(r => r.engine === engine);
    const f = scanFast.find(r => r.engine === engine);
    const c = scanCheap.find(r => r.engine === engine);
    return `<tr><td>${swatch(engine)}${esc(engine)}</td>
      <td class="num">${j ? `${ms(num(j.typical_ms))} <span class="rank">#${rank(join, engine)}</span>`
        : u ? `<span class="muted">did not finish (${fmt(num(u.done))} of ${fmt(num(u.n))})</span>` : '<span class="muted">not compared</span>'}</td>
      <td class="num">${f ? `${ms(num(f.ms))} <span class="rank">#${rank(scanFast, engine)}</span>` : ''}</td>
      <td>${f ? `${esc(short(f.size))}, ${usdExact(num(f.usd))}` : ''}</td>
      <td class="num">${c ? `${ms(num(c.ms))} <span class="rank">#${rank(scanCheap, engine)}</span>` : '<span class="muted">no measured size</span>'}</td></tr>`;
  }).join('');
  // The headline and the first sentence name what actually moves: the engines
  // whose rank changes by three or more places, and whether the top two hold.
  const ORDINALS = ['first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh'];
  const place = n => (n === ENGINES.length ? 'last' : ORDINALS[n - 1]);
  // The join column is at the designated size and the scan columns at the
  // fastest size; say so, and whether ranking joins at their fastest size too
  // would change the order (it is the query type that reorders, not the basis).
  joinFast = joinFast.filter(r => join.some(j => j.engine === r.engine));
  const sameAtFastest = joinFast.length === join.length && joinFast.every((r, i) => r.engine === join[i].engine);
  // Movers compare like with like: both ranks among the engines ranked on joins.
  const scanRanked = scanFast.filter(r => join.some(j => j.engine === r.engine));
  const movers = ENGINES
    .map(engine => ({ engine, from: rank(join, engine), to: rank(scanRanked, engine) }))
    .filter(m => m.from && m.to && Math.abs(m.from - m.to) >= 3)
    .sort((a, b) => Math.abs(b.from - b.to) - Math.abs(a.from - a.to));
  const topJoin = join.slice(0, 2).map(r => r.engine);
  const topScan = scanRanked.slice(0, 2).map(r => r.engine);
  const topHolds = topJoin.every(e => topScan.includes(e));
  // An engine that did not finish the join suite but places in the top three on
  // scans is the headline; otherwise the biggest mover between the two rankings.
  const climber = unfinished.map(r => ({ engine: r.engine, to: rank(scanFast, r.engine) }))
    .filter(r => r.to && r.to <= 3).sort((a, b) => a.to - b.to)[0];
  const lead = movers[0];
  if (climber) {
    $('f3-title').textContent = `${climber.engine} did not finish the join queries at 100 GB but placed ${place(climber.to)} on scan queries`;
  } else if (lead) {
    $('f3-title').textContent = `Scan queries move ${lead.engine} from ${place(lead.from)} to ${place(lead.to)}`;
  }
  const moves = andList(movers.map(m => `${esc(m.engine)} from #${m.from} to #${m.to}`));
  const notRanked = unfinished.map(r => `${esc(r.engine)} finished only ${fmt(num(r.done))} of the ${fmt(num(r.n))} join queries at 100 GB, so it is not ranked on them${
    rank(scanFast, r.engine) ? `; on scan queries it placed #${rank(scanFast, r.engine)}` : ''}. `).join('');
  const ch = rank(scanCheap, 'ClickHouse');
  $('f3-body').innerHTML = `
    <p><span data-term="join-queries">Join queries</span> combine a large sales table with smaller tables, which is what
    most reports look like underneath. <span data-term="scan-queries">Scan queries</span> filter and count over one wide
    table, which is what event dashboards do. ${notRanked}Among the engines ranked on both, moving from one kind to the other moves ${moves || 'no engine by more than two places'}${
      topHolds ? `, while ${andList(topJoin.map(esc))} lead on both` : ''}. Comparing at a fixed hourly budget
    reorders them again: ClickHouse places #${ch} among sizes listed at $3 an hour or less.</p>
    <div class="figure table-scroll"><table class="data-table">
      <thead><tr><th>Engine</th><th class="num">Join queries at 100 GB, designated size</th><th class="num">Scan queries, fastest size</th><th>That size</th><th class="num">Scan queries, fastest size at $3 an hour or less</th></tr></thead>
      <tbody>${body}</tbody>
      <caption>Typical query time (geometric mean of each query's median), single user. The number after each time is the engine's rank in that column. An engine that finished fewer than 90% of the join queries is not ranked on them. The join column uses each engine's designated size${sameAtFastest ? '; ranked at each engine\'s fastest join size instead, the order is the same' : ''}.</caption>
    </table></div>
    <p><b>What this means for a buyer:</b> before looking at any speed figure, decide whether your queries join multiple tables
    or scan a single table, and compare the engines at the hourly spend you would approve rather than at their fastest sizes.</p>`;
  chartTools($('home-f3'), {
    title: 'Join queries and scan queries order the engines differently',
    subtitle: () => 'Typical single-user query time; join queries at about 100 GB at each engine\'s designated size; scan queries on one table of about 100 GB',
    csv: () => `SELECT 'join, 100 GB, designated size' AS measure, engine, NULL AS size, typical_ms, NULL AS usd_per_hour FROM engine_scale WHERE dataset = '100'
      UNION ALL SELECT 'scan, every size', engine, compute_label, typical_ms, usd_per_hour FROM size_typical WHERE kind = 'scan'
      ORDER BY 1, 2`
  });
}

// ── Finding 4: idle billing (vendor documentation, not measured) ─────────
function findingFour() {
  $('idle-date').textContent = IDLE_RULES_AS_OF;
  const rows = Object.entries(ENGINE_LABEL).map(([id, label]) => {
    const n = ENGINE_NOTES[id];
    return `<tr><td>${swatch(label)}<a href="${engineHref(id)}">${esc(label)}</a></td><td>${esc(n.idle.pays)}</td><td>${esc(n.idle.quiet)}</td></tr>`;
  }).join('');
  $('f4-body').innerHTML = `<table class="data-table idle">
    <thead><tr><th>Engine</th><th>What you pay for</th><th>When the warehouse is quiet</th></tr></thead>
    <tbody>${rows}</tbody></table>
    <p><b>What this means for a buyer:</b> describe your usage pattern first (a steady all-day load, a burst each
    morning, or a few analysts now and then), then check how each engine's meter behaves in the quiet hours.</p>`;
}

// ── Further results ──────────────────────────────────────────────────────
async function further() {
  const [skew, [pairs], reliability] = await Promise.all([
    q(`SELECT engine, scale_factor, median(delta_pct) AS d FROM skew_warm_clean WHERE scale_factor IN (100, 1000) GROUP BY ALL`),
    q(`SELECT count(DISTINCT qnum) AS n FROM skew_warm_clean`),
    q(`SELECT engine, sum(shed_pct * runs) / sum(runs) AS pct FROM concurrency GROUP BY engine ORDER BY pct`)
  ]);
  const at = sf => skew.filter(r => num(r.scale_factor) === sf);
  const s100 = at(100);
  const s1000 = at(1000);
  const faster100 = s100.filter(r => num(r.d) < 0).length;
  const slower1000 = s1000.filter(r => num(r.d) > 0).sort(byEngine);
  // Slower is quoted as a multiple (1.8 times as long), the guide's form;
  // faster as a percentage change.
  const asLong = d => `${(1 + num(d) / 100).toFixed(1)} times as long`;
  // Shares within UNFINISHED_TIE_PP of the lowest tie for the fewest, as the guide says.
  const fewest = reliability.filter(r => num(r.pct) - num(reliability[0].pct) <= UNFINISHED_TIE_PP);
  const next = reliability[fewest.length];
  const pctOf = r => `${num(r.pct).toFixed(2)}%`;
  $('further-body').innerHTML = `<ul class="plain-list">
    <li><b>Skewed data mostly ran faster than uniform data.</b> In the single-user pass at 100 GB, the median skewed (DSB) query
      ran faster than its uniform (TPC-DS) twin on ${faster100} of ${s100.length} engines. At 1 TB, ${slower1000.length
        ? `${slower1000.map(r => `${esc(r.engine)} was the exception: its median skewed query took ${asLong(r.d)}`).join('; ')}`
        : 'every engine still ran the skewed queries faster'}. Both figures use the ${num(pairs.n)} query pairs whose
      tables are the same size in both suites, as the report does.
      <a href="compare.html#sf=1000&amp;view=sec-skew">See skew on every engine</a></li>
    <li><b>Unfinished queries under load.</b> Across every data volume, size and session count, ${fewest.length > 1
        ? `${andList(fewest.map(r => esc(r.engine)))} tied for the smallest share of queries unfinished (${andList(fewest.map(pctOf))})`
        : `${esc(fewest[0].engine)} left the smallest share of queries unfinished (${pctOf(fewest[0])})`}${next ? `, followed by ${esc(next.engine)} (${pctOf(next)})` : ''}.
      <a href="compare.html#view=sec-degradation">See response times and unfinished queries under load</a></li>
    <li><b>Each engine in detail.</b> The engine pages show where each engine ranked among the seven on each measure.
      <span class="engine-inline">${Object.entries(ENGINE_LABEL).map(([id, l]) => `<a href="${engineHref(id)}">${esc(l)}</a>`).join(' · ')}</span></li>
  </ul>`;
}

/** Render this page into its HTML; src/site/router.js calls it on every visit. */
export default async function mount() {
  // No link to the report until it is published (see REPORT_LIVE).
  if (REPORT_LIVE) $('report-link').href = reportHref('home');
  else {
    // Without the guide, the workload finder is the page's primary action.
    $('report-link').remove();
    $('fit-link').classList.replace('btn-secondary', 'btn-primary');
  }
  $('engine-links').innerHTML = Object.entries(ENGINE_LABEL)
    .map(([id, label]) => `<a href="${engineHref(id)}">${esc(label)}</a>`).join('');
  const stats = await headlineStats();
  const [{ n: warm }] = await q('SELECT sum(runs) AS n FROM ladder');
  $('facts').innerHTML = [
    `<b>${stats.engines}</b> engines`,
    `<b>${stats.queries}</b> queries in three suites`,
    '<b>1 GB to 1 TB</b> of data',
    '<b>1 to 100</b> people at once',
    `<b>${fmt(num(warm) + stats.gridRuns)}</b> measured query runs`
  ].map(s => `<li>${s}</li>`).join('');
  $('agreement').textContent = `At every data volume, ${stats.agreed.toLocaleString('en-US')} of ${stats.compared.toLocaleString('en-US')} join-query results (each join query at each data volume) returned the same number of rows on every engine that ran them.`;
  findingFour();
  await Promise.all([findingOne(), findingTwo(), findingThree(), further()]);
}
