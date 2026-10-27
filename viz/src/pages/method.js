/**
 * The Method page: prose from the buyer's guide's appendix, plus the parts of
 * the method that are data and are read live: the rate card, the row-count
 * agreement check, the glossary (rendered from src/site/glossary.js) and the
 * list of downloadable exports.
 */
import { coordinator } from '@uwdata/vgplot';
import { chartTools } from '../site/chart-tools.js';
import { ENGINE_NOTES, IDLE_RULES_AS_OF } from '../site/engine-notes.js';
import { renderGlossary } from '../site/glossary.js';
import { ISSUES_URL, REPO_URL, engineHref } from '../site/layout.js';
import { PARQUET_FILES } from '../data.js';
import { ENGINE_COLOR, ENGINE_LABEL, ENGINES } from '../engines.js';
import { usdExact } from '../format.js';

const $ = id => document.getElementById(id);
const q = text => coordinator().query(text, { type: 'json' });
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const swatch = e => `<span class="swatch" style="background:${ENGINE_COLOR[e]}"></span>`;

/** What each export holds, in the words a reader needs to pick one. */
const EXPORT_NOTES = {
  'concurrency_grid.parquet': 'The concurrency grid: one row per engine, data volume, compute size and session count, with the median response time, the share of queries not completed, the run count and the list price per hour.',
  'cost_curves.parquet': 'Single-user results at each engine\'s designated size: one row per engine, query and data volume, with the median time, the mean cost and the failures.',
  'size_ladder.parquet': 'Single-user results at every compute size: one row per engine, suite, data volume, size and query. The source of every sizing chart.',
  'clickbench_summary.parquet': 'Scan-query (ClickBench) results at the sizes the cross-engine comparison uses, one row per engine, size and query.',
  'pattern_load.parquet': 'Query-pattern results under load: for each engine, data volume, size, session count and pattern, the pattern\'s median time and the median time of all queries in the same cell.',
  'skew_impact.parquet': 'Each DSB query against its TPC-DS twin in the single-user pass, per engine and data volume.',
  'skew_load.parquet': 'The same pairing under load, per engine, data volume, size and session count.',
  'row_count_agreement.parquet': 'For each query, data volume and engine, the row count returned and whether it was the same on every run.',
  'rate_card.parquet': 'The list price per hour of every compute size in the results, with its billing unit.',
  'scale_skips.parquet': 'The cells left out on purpose: one row per engine, data volume, suite, compute size and pass (each of the last three can be every one), with the recorded reason and the date it was recorded.',
  'query_catalog.parquet': 'The 244 queries: suite, number, pattern tags and the path of each query\'s SQL in the repository.'
};

async function prices() {
  const rows = await q(`SELECT platform, compute_label, billing_unit, usd_per_hour FROM rate_card ORDER BY platform, usd_per_hour NULLS LAST, compute_label`);
  const byEngine = ENGINES.map(label => {
    const id = Object.keys(ENGINE_LABEL).find(k => ENGINE_LABEL[k] === label);
    return rows.filter(r => r.platform === id).map(r => ({ ...r, label }));
  }).flat();
  // A 48-row reference list: as cards on a phone it would run to several
  // screens, so it keeps its columns there and drops the billing unit to fit
  // (method.css).
  $('prices-body').innerHTML = `<div class="table-scroll"><table class="data-table no-stack rate-card">
    <thead><tr><th>Engine</th><th>Compute size</th><th>Billing unit</th><th class="num">List price per hour</th></tr></thead>
    <tbody>${byEngine.map(r => `<tr><td>${swatch(r.label)}${esc(r.label)}</td><td>${esc(r.compute_label)}</td><td>${esc(r.billing_unit)}</td>
      <td class="num">${r.usd_per_hour == null ? 'no hourly rate' : usdExact(Number(r.usd_per_hour))}</td></tr>`).join('')}</tbody>
    <caption>Every compute size that appears in the results. Source: the benchmark's rate card (src/benchmark/report/compute_rates.py).</caption>
  </table></div>`;
  chartTools($('prices'), {
    title: 'List prices as captured, July to October 2026',
    csv: () => 'SELECT platform, compute_label, billing_unit, usd_per_hour FROM rate_card ORDER BY platform, usd_per_hour'
  });
}

async function correctness() {
  const [totals] = await q(`SELECT count(*) AS n, count(*) FILTER (WHERE distinct_counts = 1) AS same,
      count(DISTINCT query_id) AS queries, count(DISTINCT scale_factor) AS scales FROM row_count_agreement`);
  const agreeAcross = await q(`
    WITH per_query AS (
      SELECT query_id, scale_factor, count(DISTINCT row_count) AS counts, count(DISTINCT platform) AS engines
      FROM row_count_agreement GROUP BY 1, 2
    )
    SELECT count(*) AS combos, count(*) FILTER (WHERE counts = 1) AS agree, max(engines) AS engines FROM per_query`);
  const a = agreeAcross[0];
  $('correctness-body').innerHTML = `<ul class="plain-list">
    <li><b>Stable on repeated runs:</b> in ${Number(totals.same).toLocaleString('en-US')} of ${Number(totals.n).toLocaleString('en-US')}
      engine, query and data-volume combinations, every run returned the same number of rows.</li>
    <li><b>The same across engines:</b> in ${Number(a.agree).toLocaleString('en-US')} of ${Number(a.combos).toLocaleString('en-US')}
      query and data-volume combinations, every engine that ran the query returned the same number of rows.</li>
  </ul>`;
}

function idle() {
  $('idle-date').textContent = IDLE_RULES_AS_OF;
  $('idle-body').innerHTML = `<table class="data-table idle">
    <thead><tr><th>Engine</th><th>What you pay for</th><th>When the warehouse is quiet</th></tr></thead>
    <tbody>${Object.entries(ENGINE_LABEL).map(([id, l]) => `<tr><td>${swatch(l)}<a href="${engineHref(id)}">${esc(l)}</a></td>
      <td>${esc(ENGINE_NOTES[id].idle.pays)}</td><td>${esc(ENGINE_NOTES[id].idle.quiet)}</td></tr>`).join('')}</tbody></table>`;
}

function downloads() {
  $('repo-link').href = REPO_URL;
  $('issues-link').href = ISSUES_URL;
  $('downloads-body').innerHTML = `<table class="data-table">
    <thead><tr><th>File</th><th>What it holds</th></tr></thead>
    <tbody>${PARQUET_FILES.map(f => `<tr><td><a href="data/${esc(f)}" download>${esc(f)}</a></td><td>${esc(EXPORT_NOTES[f] ?? '')}</td></tr>`).join('')}</tbody>
  </table>`;
}

/** Render this page into its HTML; src/site/router.js calls it on every visit. */
export default async function mount() {
  renderGlossary($('glossary-body'));
  idle();
  downloads();
  await Promise.all([prices(), correctness()]);
  // A link to a glossary entry (method.html#term-shed) lands on it once the
  // glossary exists; the browser's own jump runs before it is rendered.
  if (location.hash.startsWith('#term-') || ['#glossary', '#downloads', '#prices'].includes(location.hash)) {
    document.querySelector(location.hash)?.scrollIntoView({ block: 'start' });
    document.querySelector(location.hash)?.classList.add('target');
  }
}
