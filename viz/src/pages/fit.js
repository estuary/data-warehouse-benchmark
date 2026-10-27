/**
 * Find your warehouse: the buyer's guide's decision guide as a tool.
 *
 * A reader answers the guide's three questions (join or scan queries, people
 * querying at once, hourly spend) plus a data volume and a latency target, and
 * the page lists every (engine, compute size) in the concurrency grid that
 * served that many sessions within the target. Nothing here is a ranking or a
 * recommendation beyond that filter: the rows are the grid's own cells, sorted
 * by list price, and every figure is computed in DuckDB from the same views
 * the compare page reads.
 *
 * The one piece of arithmetic that is not a measurement is the illustrative
 * monthly figure, and it says so: list price times the hours the meter runs,
 * where an engine that bills while idle (per ENGINE_NOTES, from vendor
 * documentation) runs all month and one that stops when idle runs only the
 * reader's busy hours.
 *
 * State lives in the URL hash so a shortlist can be linked:
 *   #kind=join&sf=100&people=10&sla=5000&budget=&engines=&hours=8
 * An empty `engines` means all seven; `engines=none` means none chosen.
 */
import { coordinator } from '@uwdata/vgplot';
import { chartTools } from '../site/chart-tools.js';
import { describeSelection, fmtSla } from '../site/describe.js';
import { ENGINE_NOTES, IDLE_RULES_AS_OF } from '../site/engine-notes.js';
import { bindTerms } from '../site/glossary.js';
import { engineHref } from '../site/layout.js';
import { ENGINE_COLOR, ENGINE_LABEL } from '../engines.js';
import { ms, pct, usdExact as usdPerHour } from '../format.js';
import { CLICKBENCH, DEFAULT_SLA_MS, MEASURED_LEVELS, SCAN_SUITE_QUERIES, levelsFor } from '../constants.js';
import { replaceHash } from '../url-state.js';

/** Session levels a reader can ask for: the ones the grid ran. */
const LEVELS = MEASURED_LEVELS.filter(l => l > 0);
/** Data volumes of the join suites. */
const SCALES = [1, 10, 100, 1000];
const VOLUME_TEXT = { 1: '1 GB', 10: '10 GB', 100: '100 GB', 1000: '1 TB' };
/** Illustrative month: 730 hours for a meter that never stops, else busy hours on 21.7 working days. */
export const HOURS_PER_MONTH = 730;
export const WORKING_DAYS_PER_MONTH = 21.7;
const PLATFORMS = Object.keys(ENGINE_LABEL);

/** The hash value for "no engine chosen"; an empty value means all seven. */
const NO_ENGINES = 'none';
const DEFAULTS = { kind: 'join', sf: 100, people: 10, sla: DEFAULT_SLA_MS, budget: null, engines: PLATFORMS, hours: 8 };

/** The guide's four archetypes. `set` is applied over the defaults; `platform` keeps the inputs. */
const PRESETS = [
  {
    id: 'small-team', name: 'The small team',
    text: 'A 200-person software company with about 100 GB of data and five analysts needs fast single-user queries at a low daily cost.',
    set: { kind: 'join', sf: 100, people: 5 }
  },
  {
    id: 'product', name: 'The product company',
    text: 'A company serving page-view and click dashboards to 300 people, up to 100 of whom may load one at once, needs cheap throughput on scan queries over one wide table of events.',
    set: { kind: 'scan', people: 100 }
  },
  {
    id: 'enterprise', name: 'The enterprise with a terabyte reporting layer',
    text: 'An insurer with a 1 TB warehouse and 400 dashboard users needs to know what to expect when one hundred of them refresh the dashboard at once.',
    set: { kind: 'join', sf: 1000, people: 100 }
  },
  {
    id: 'platform', name: 'The company already on a platform',
    text: 'A company running on Microsoft, Databricks or Snowflake has chosen its platform and needs to know how to size within it.',
    platform: true
  }
];

const KINDS = [
  { id: 'join', label: 'Join queries', sub: 'reports, finance, BI dashboards' },
  { id: 'scan', label: 'Scan queries', sub: 'events, telemetry, product analytics' },
  { id: 'both', label: 'Both', sub: 'some of each' }
];

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const q = sql => coordinator().query(sql, { type: 'json' });
const num = v => (v == null ? null : Number(v));
const money = v => `$${Math.round(v).toLocaleString('en-US')}`;
const list = items => (items.length <= 1 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`);

// ── State and the URL hash ────────────────────────────────────────────────
function readHash() {
  const p = new URLSearchParams(location.hash.replace(/^#/, ''));
  const s = { ...DEFAULTS, engines: [...PLATFORMS] };
  const kind = p.get('kind');
  if (KINDS.some(k => k.id === kind)) s.kind = kind;
  const sf = Number(p.get('sf'));
  if (SCALES.includes(sf)) s.sf = sf;
  const people = Number(p.get('people'));
  if (LEVELS.includes(people)) s.people = people;
  const sla = Number(p.get('sla'));
  if (Number.isFinite(sla) && sla >= 500 && sla <= 20000) s.sla = Math.round(sla / 500) * 500;
  const budget = p.get('budget');
  if (budget && Number.isFinite(Number(budget)) && Number(budget) > 0) s.budget = Number(budget);
  const engines = p.get('engines');
  if (engines === NO_ENGINES) s.engines = [];
  else if (engines) {
    const chosen = engines.split(',').filter(e => PLATFORMS.includes(e));
    if (chosen.length) s.engines = chosen;
  }
  const hours = Number(p.get('hours'));
  if (Number.isFinite(hours) && hours >= 1 && hours <= 24) s.hours = Math.round(hours);
  return s;
}

function writeHash(s) {
  const p = new URLSearchParams();
  p.set('kind', s.kind);
  p.set('sf', String(s.sf));
  p.set('people', String(s.people));
  p.set('sla', String(s.sla));
  p.set('budget', s.budget == null ? '' : String(s.budget));
  p.set('engines', s.engines.length === PLATFORMS.length ? '' : s.engines.length ? s.engines.join(',') : NO_ENGINES);
  p.set('hours', String(s.hours));
  // Engine ids are plain words; keep the commas readable in a shared link.
  const hash = `#${p.toString().replace(/%2C/g, ',')}`;
  replaceHash('fit', hash);
}

/** Data sets the current kind asks about: a scale factor as text, or CLICKBENCH. */
const datasets = s => (s.kind === 'join' ? [String(s.sf)] : s.kind === 'scan' ? [CLICKBENCH] : [String(s.sf), CLICKBENCH]);

// ── SQL ───────────────────────────────────────────────────────────────────
const sqlStr = v => `'${String(v).replace(/'/g, "''")}'`;
// No engine chosen matches nothing (`IN ()` is not valid SQL).
const engineFilter = s => (s.engines.length === PLATFORMS.length ? ''
  : s.engines.length ? `AND platform IN (${s.engines.map(sqlStr).join(', ')})` : 'AND false');

/**
 * People served per (engine, size) at `ds` for the target: the frontier's own
 * aggregate. `top_level` is the highest level the size was run at: where a size
 * served every level it ran and that stops short of 100, its figure is a floor.
 */
const servedSql = (s, ds) => `
  SELECT engine, platform, compute_label, any_value(usd_per_hour) AS usd_per_hour,
         coalesce(max(concurrency) FILTER (WHERE p50_ms <= ${s.sla} AND shed_pct < sla_max_shed_pct), 0) AS served,
         max(concurrency) AS top_level
  FROM concurrency WHERE dataset = ${sqlStr(ds)} ${engineFilter(s)}
  GROUP BY engine, platform, compute_label`;

/**
 * Every configuration that served at least `people` sessions within the
 * target, with its behaviour at the smallest measured level at or above
 * `people` (the level the reader's load falls on), cheapest first.
 * `cheapest` flags each engine's lowest-priced qualifying size.
 */
export const resultsSql = (s, ds) => `
  WITH c AS (${servedSql(s, ds)}),
  lvl AS (
    SELECT platform, compute_label, min(concurrency) AS level
    FROM concurrency WHERE dataset = ${sqlStr(ds)} AND concurrency >= ${s.people}
    GROUP BY platform, compute_label
  ),
  at_level AS (
    SELECT g.platform, g.compute_label, g.concurrency AS level, g.p50_ms, g.shed_pct
    FROM concurrency g JOIN lvl ON g.platform = lvl.platform AND g.compute_label = lvl.compute_label
                                AND g.concurrency = lvl.level
    WHERE g.dataset = ${sqlStr(ds)}
  )
  SELECT c.engine, c.platform, c.compute_label, c.usd_per_hour, c.served, c.top_level,
         at_level.level, at_level.p50_ms AS median_ms_at_level, at_level.shed_pct AS not_completed_pct_at_level,
         c.usd_per_hour = min(c.usd_per_hour) OVER (PARTITION BY c.platform) AS cheapest
  FROM c LEFT JOIN at_level USING (platform, compute_label)
  WHERE c.served >= ${s.people}${s.budget == null ? '' : ` AND c.usd_per_hour <= ${s.budget}`}
  ORDER BY c.usd_per_hour, c.engine, c.compute_label`;

/** The most any single configuration served, over the chosen engines, budget ignored. */
const bestSql = (s, ds) => `
  WITH c AS (${servedSql(s, ds)})
  SELECT engine, platform, compute_label, usd_per_hour, served FROM c
  WHERE served = (SELECT max(served) FROM c) AND served > 0
  ORDER BY usd_per_hour, engine`;

/** Configurations that served enough but list above the budget. */
const overBudgetSql = (s, ds) => `
  WITH c AS (${servedSql(s, ds)})
  SELECT count(*) AS n, min(usd_per_hour) AS cheapest FROM c
  WHERE served >= ${s.people} AND usd_per_hour > ${s.budget ?? 'NULL'}`;

/**
 * Single-user typical query per engine at `ds`: at its designated size (the
 * cross-engine comparison population), and at the cheapest size that served
 * the reader's load. ClickHouse ran the grid on three replicas and the
 * single-user pass on one, so its grid label maps to one replica of the same
 * memory ("3x64GB" -> "1x64GB"). `size_change` says how the two sizes compare
 * (in the CSV download and the row's data-change attribute; the table itself
 * shows both size names and leaves the comparison to the reader): 'same' (one
 * measurement shown twice), 'smaller' or 'larger' by list price, 'pricing'
 * where one side has no hourly price (BigQuery on-demand), or 'unmeasured'
 * where the cheapest size was not run with one user (ClickHouse 3x24GB: the
 * single-replica ladder has no 1x24GB).
 */
export const speedSql = (s, ds) => `
  WITH r AS (${resultsSql(s, ds)}),
  cheap AS (SELECT platform, arg_min(compute_label, usd_per_hour) AS compute_label FROM r GROUP BY platform),
  j AS (
    SELECT e.engine, e.platform, d.compute_label AS designated_size, e.typical_ms AS designated_typical_ms,
           cheap.compute_label AS cheapest_qualifying_size,
           CASE WHEN cheap.platform = 'clickhouse' THEN regexp_replace(cheap.compute_label, '^[0-9]+x', '1x')
                ELSE cheap.compute_label END AS single_user_size,
           st.typical_ms AS cheapest_qualifying_typical_ms, st.completed, st.attempted
    FROM engine_scale e
    LEFT JOIN designated_size d ON d.platform = e.platform AND d.dataset = e.dataset
    LEFT JOIN cheap ON cheap.platform = e.platform
    LEFT JOIN size_typical st
      ON st.platform = cheap.platform AND st.dataset = e.dataset
     AND st.compute_label = CASE WHEN cheap.platform = 'clickhouse'
                                 THEN regexp_replace(cheap.compute_label, '^[0-9]+x', '1x')
                                 ELSE cheap.compute_label END
    WHERE e.dataset = ${sqlStr(ds)} ${engineFilter(s).replace('platform', 'e.platform')}
  )
  SELECT j.*,
         CASE WHEN j.cheapest_qualifying_size IS NULL OR j.designated_size IS NULL THEN NULL
              WHEN j.single_user_size = j.designated_size THEN 'same'
              WHEN j.cheapest_qualifying_typical_ms IS NULL THEN 'unmeasured'
              WHEN rd.usd_per_hour IS NULL OR rc.usd_per_hour IS NULL THEN 'pricing'
              WHEN rc.usd_per_hour < rd.usd_per_hour THEN 'smaller'
              ELSE 'larger' END AS size_change
  FROM j
  LEFT JOIN rate_card rd ON rd.platform = j.platform AND rd.compute_label = j.designated_size
  LEFT JOIN rate_card rc ON rc.platform = j.platform AND rc.compute_label = j.single_user_size
  ORDER BY j.designated_typical_ms`;

/** A size label as the table shows it ("on-demand" is a pricing model, so it reads as a name). */
const sizeName = label => (label === 'on-demand' ? 'On-demand' : label ?? '');

/** The illustrative monthly rule, stated once: see the module docstring. */
export const monthly = (usd, billsIdle, hours) =>
  usd * (billsIdle ? HOURS_PER_MONTH : hours * WORKING_DAYS_PER_MONTH);

// ── Inputs ────────────────────────────────────────────────────────────────
function chip(label, pressed, onClick, { sub, disabled, data } = {}) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'choice';
  b.setAttribute('aria-pressed', String(pressed));
  if (disabled) { b.disabled = true; b.setAttribute('aria-disabled', 'true'); }
  if (data) Object.entries(data).forEach(([k, v]) => { b.dataset[k] = v; });
  b.innerHTML = sub ? `${esc(label)} <small>${esc(sub)}</small>` : esc(label);
  b.addEventListener('click', onClick);
  return b;
}

let state;
let showAll = false;
/** The "already on a platform" example sets no inputs, so it is marked until the next change. */
let platformPicked = false;

/** The example whose inputs the form still holds exactly, if any. */
function activePreset(s) {
  if (platformPicked) return 'platform';
  const same = (a, b) => (Array.isArray(a) ? a.length === b.length && a.every((v, i) => v === b[i]) : a === b);
  const match = PRESETS.find(p => p.set && Object.entries({ ...DEFAULTS, ...p.set }).every(([k, v]) => same(s[k], v)));
  return match?.id ?? null;
}
let version = 0;

function renderInputs() {
  const s = state;
  const active = activePreset(s);
  for (const b of $('fit-preset-list').querySelectorAll('[data-preset]')) b.setAttribute('aria-pressed', String(b.dataset.preset === active));
  $('in-kind').replaceChildren(...KINDS.map(k =>
    chip(k.label, s.kind === k.id, () => update({ kind: k.id }), { sub: k.sub, data: { kind: k.id } })));
  const scan = s.kind === 'scan';
  // Scan queries have one fixed table, so the choice is shown as made rather
  // than as four greyed-out chips, which read as broken.
  $('in-sf').replaceChildren(...(scan
    ? [chip('About 100 GB (ClickBench)', true, () => {}, { data: { sf: 'clickbench' } })]
    : SCALES.map(sf => chip(VOLUME_TEXT[sf], s.sf === sf, () => update({ sf }), { data: { sf: String(sf) } }))));
  $('sf-hint').textContent = scan
    ? 'The scan queries ran on one fixed table, so there is no volume to choose. Your join-query volume is kept for when you switch back.'
    : s.kind === 'both'
      ? 'This volume applies to the join queries. The scan queries ran on one fixed table of about 100 GB.'
      : '';
  $('in-people').replaceChildren(...LEVELS.map(n =>
    chip(String(n), s.people === n, () => update({ people: n }), { data: { people: String(n) } })));
  $('in-sla').value = String(s.sla);
  $('sla-out').textContent = fmtSla(s.sla);
  $('in-budget').value = s.budget == null ? '' : String(s.budget);
  $('in-hours').value = String(s.hours);
  $('in-engines').replaceChildren(...PLATFORMS.map(p => {
    const on = s.engines.includes(p);
    const b = chip(ENGINE_LABEL[p], on, () => {
      const next = on ? s.engines.filter(e => e !== p) : [...s.engines, p];
      update({ engines: PLATFORMS.filter(e => next.includes(e)) });
    }, { data: { engine: p } });
    b.insertAdjacentHTML('afterbegin', `<span class="swatch" style="background:${ENGINE_COLOR[ENGINE_LABEL[p]]}"></span>`);
    return b;
  }), chip('Select all', false, () => update({ engines: [...PLATFORMS] }),
    { data: { engine: 'all' }, disabled: s.engines.length === PLATFORMS.length }),
  // Clear, then pick one: the short way from all seven to a single engine.
  chip('Clear all', false, () => update({ engines: [] }),
    { data: { engine: 'none' }, disabled: !s.engines.length }));
}

function wireInputs() {
  $('in-sla').addEventListener('input', e => update({ sla: Number(e.target.value) }));
  $('in-budget').addEventListener('change', e => {
    const v = Number(e.target.value);
    update({ budget: e.target.value !== '' && Number.isFinite(v) && v > 0 ? v : null });
  });
  $('in-hours').addEventListener('change', e => {
    const v = Math.round(Number(e.target.value));
    update({ hours: Number.isFinite(v) && v >= 1 && v <= 24 ? v : DEFAULTS.hours });
  });
  const helper = () => {
    const users = Number($('in-users').value);
    const share = Number($('in-share').value);
    if (!(users > 0 && share > 0 && share <= 100)) {
      $('helper-out').textContent = 'Enter a number of people and a share between 1 and 100.';
      return null;
    }
    const raw = (users * share) / 100;
    const level = LEVELS.find(l => l >= raw) ?? LEVELS.at(-1);
    const shown = raw < 1 ? raw.toFixed(1) : Math.round(raw * 10) / 10;
    $('helper-out').textContent = raw > LEVELS.at(-1)
      ? `That is about ${shown} people at once, more than the 100 the study measured. The closest measured level is 100; plan on several units of compute for the rest.`
      : `That is about ${shown} people at once. The study measured 1, 5, 10, 20, 50 and 100 at once, so this rounds up to ${level}.`;
    return level;
  };
  $('in-users').addEventListener('input', helper);
  $('in-share').addEventListener('input', helper);
  $('in-helper-apply').addEventListener('click', () => {
    const level = helper();
    if (level) update({ people: level });
  });
  helper();

  $('fit-preset-list').replaceChildren(...PRESETS.map(p => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'fit-preset';
    b.dataset.preset = p.id;
    b.innerHTML = `<b>${esc(p.name)}</b><span>${esc(p.text)}</span>`;
    b.addEventListener('click', () => {
      if (p.platform) {
        $('engines-hint').textContent = 'Choose the platform you already run on: turn the others off to see how to size within yours.';
        $('field-engines').classList.add('fit-flash');
        setTimeout(() => $('field-engines')?.classList.remove('fit-flash'), 1600);
        $('field-engines').scrollIntoView({ behavior: 'smooth', block: 'center' });
        platformPicked = true;
        renderInputs();
        return;
      }
      $('engines-hint').textContent = '';
      update({ ...DEFAULTS, engines: [...PLATFORMS], ...p.set });
      // On a phone the results sit a screen or more below the examples, so a
      // tap would otherwise change nothing the reader can see.
      const results = $('fit-results');
      if (results.getBoundingClientRect().top > window.innerHeight * 0.6) {
        results.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    });
    return b;
  }));
}

function update(patch) {
  platformPicked = false;
  state = { ...state, ...patch };
  writeHash(state);
  renderInputs();
  render();
}

// ── Results ───────────────────────────────────────────────────────────────
const scopeText = (s, ds) => (ds === CLICKBENCH
  ? '<span data-term="scan-queries">scan queries</span> on one table of about 100 GB'
  : `<span data-term="join-queries">join queries</span> at about ${VOLUME_TEXT[ds]}`);

const idleCell = platform => {
  const idle = ENGINE_NOTES[platform]?.idle;
  if (!idle) return '';
  const month = idle.fitMonth
    ?? `The monthly estimate on this page counts only your busy hours for ${ENGINE_LABEL[platform]}, because it stops billing when nobody is querying.`;
  return `<span class="fit-idle ${idle.billsIdle ? 'bills' : 'stops'}" tabindex="0" data-tip-title="When the warehouse is quiet" data-tip="${esc(`${idle.quiet} ${month}`)}">${idle.billsIdle ? 'Bills while idle' : 'Stops when idle'}</span>`;
};

/**
 * Setup notes the reader needs before comparing these engines' concurrency
 * figures: every engine whose scale-out feature the study switched off, and
 * the two whose units under load differ from the rest (ClickHouse's three
 * replicas, MotherDuck's one shared instance).
 */
const CAVEAT_ENGINES = ['snowflake', 'databricks', 'redshift', 'clickhouse', 'motherduck'];

/**
 * The people a data set's results are read at: the reader's number, except on
 * scan queries, where the most the study ran at once is the whole 43-query suite.
 */
const peopleAt = (s, ds) => (ds === CLICKBENCH ? Math.min(s.people, SCAN_SUITE_QUERIES) : s.people);

/** On scan queries, a number above 43 is read at 43, and the note says why. */
const scanCapNote = (s, ds) => (ds === CLICKBENCH && s.people > SCAN_SUITE_QUERIES
  ? `<p class="callout fit-scan-cap">The scan suite has ${SCAN_SUITE_QUERIES} queries, so the most the study ran at once on
     scan queries is ${SCAN_SUITE_QUERIES}, and these results are for ${SCAN_SUITE_QUERIES} at once. If ${s.people} people may run
     scan queries at the same time, the size you need is at least as large as the ones listed here.</p>`
  : '');

async function renderResults(reader, ds, bodyId, qId, v) {
  // `reader` keeps the number the reader chose, for the scan note; everything
  // else reads the results at the number this data set was measured to.
  const s = { ...reader, people: peopleAt(reader, ds) };
  const [rows, best, over] = await Promise.all([
    q(resultsSql(s, ds)),
    q(bestSql(s, ds)),
    s.budget == null ? Promise.resolve([{ n: 0 }]) : q(overBudgetSql(s, ds))
  ]);
  if (v !== version) return null;
  const body = $(bodyId);
  $(qId).innerHTML = `Every size of every engine that kept its median response under ${fmtSla(s.sla)}
    with at least 95% of queries finished, at ${s.people} or more
    <span data-term="session">sessions</span> at once, on ${scopeText(s, ds)}.
    Sorted by <span data-term="list-price">list price</span> per hour; the highlighted row is each engine's cheapest size that qualified.`;

  if (!s.engines.length) {
    body.innerHTML = '<p class="callout">Choose at least one engine above.</p>';
    return rows;
  }

  if (!rows.length) {
    const top = best.length ? num(best[0].served) : 0;
    const overN = Number(over[0]?.n ?? 0);
    const cfgs = best.map(r => `<li data-platform="${esc(r.platform)}" data-label="${esc(r.compute_label)}">${esc(r.engine)} ${esc(r.compute_label)} (${usdPerHour(num(r.usd_per_hour))} per hour)</li>`).join('');
    const longer = s.sla < 10000 ? 10000 : 20000;
    const longerHash = new URLSearchParams(location.hash.replace(/^#/, ''));
    longerHash.set('sla', String(longer));
    body.innerHTML = `${scanCapNote(reader, ds)}
      <div class="callout warn fit-none" data-best="${top}">
        <p><b>No single unit of compute we measured served ${s.people} people at once on ${scopeText(s, ds)}
        within ${fmtSla(s.sla)}${s.budget != null ? ` at or under ${usdPerHour(s.budget)} per hour` : ''}.</b></p>
        ${overN ? `<p>${overN} configuration${overN > 1 ? 's' : ''} did serve that many but list${overN > 1 ? '' : 's'} above your budget, from ${usdPerHour(num(over[0].cheapest))} per hour.</p>` : ''}
        ${top > 0
          ? `<p>The most any single size served within the target was <b>${top}</b> at once:</p><ul class="fit-best">${cfgs}</ul>`
          : `<p>No size of the chosen engines held the target at any session count here.</p>`}
        <p>What the study supports doing instead:</p>
        <ul>
          <li>Plan on several units of compute, each serving the load one unit handled here.</li>
          <li>Choose a platform that adds clusters or capacity under load. The study held compute fixed, so it measured one unit at a time.</li>
          <li><a href="#${longerHash.toString()}" data-sla="${longer}" class="fit-longer">Try a ${fmtSla(longer)} latency target</a> and see which sizes qualify.</li>
          <li>Accept that some users will wait. Under a burst every engine in the study accepted the queries and made people wait, so decide in advance how long your users can wait for an answer.</li>
        </ul>
      </div>`;
    body.querySelector('.fit-longer')?.addEventListener('click', e => { e.preventDefault(); update({ sla: longer }); });
    bindTerms(body);
    return rows;
  }

  const extras = rows.filter(r => !r.cheapest).length;
  const trs = rows.map(r => {
    const p = r.platform;
    const idle = ENGINE_NOTES[p]?.idle;
    const usd = num(r.usd_per_hour);
    const month = monthly(usd, idle?.billsIdle ?? true, s.hours);
    const hide = !r.cheapest && !showAll;
    return `<tr class="${r.cheapest ? 'lead' : 'fit-extra'}"${hide ? ' hidden' : ''}
        data-platform="${esc(p)}" data-label="${esc(r.compute_label)}" data-cheapest="${r.cheapest ? 'true' : 'false'}"
        data-price="${usd}" data-monthly="${month}" data-served="${num(r.served)}">
      <td><span class="swatch" style="background:${ENGINE_COLOR[r.engine]}"></span><a href="${engineHref(p)}">${esc(r.engine)}</a></td>
      <td>${esc(r.compute_label)}</td>
      <td class="num">${usdPerHour(usd)}</td>
      <td class="num">${num(r.served)}${num(r.served) === num(r.top_level) && num(r.top_level) < levelsFor(ds).at(-1)
        ? ' <small tabindex="0" data-tip="The study ran this size no higher than this level, so it may serve more">highest run</small>' : ''}</td>
      <td class="num">${r.median_ms_at_level == null ? '' : `${ms(num(r.median_ms_at_level))} <small>at ${num(r.level)}</small>`}</td>
      <td class="num">${r.not_completed_pct_at_level == null ? '' : pct(num(r.not_completed_pct_at_level))}</td>
      <td>${idleCell(p)}</td>
      <td class="num">${money(month)}</td>
    </tr>`;
  }).join('');
  const present = new Set(rows.map(r => r.platform));
  const caveats = CAVEAT_ENGINES.filter(p => present.has(p))
    .map(p => `<li><b>${ENGINE_LABEL[p]}:</b> ${esc(ENGINE_NOTES[p].howWeRan[0])}</li>`).join('');
  body.innerHTML = `${scanCapNote(reader, ds)}
    <div class="table-scroll">
      <table class="data-table fit-table">
        <thead><tr>
          <th>Engine</th><th>Size</th><th class="num">List price per hour</th>
          <th class="num" data-tip="Most sessions this size served within the target">Most people served</th>
          <th class="num" data-tip="Median response at the smallest measured level at or above your number">Median response</th>
          <th class="num" data-tip="Share of queries not completed at that level"><span data-term="not-completed">Not completed</span></th>
          <th><span data-term="idle-billing">When idle</span></th>
          <th class="num">Illustrative month</th>
        </tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>
    ${extras ? `<button type="button" class="choice fit-toggle" aria-pressed="${showAll}">${showAll ? 'Show only the cheapest size per engine' : `Show all ${rows.length} sizes that qualified`}</button>` : ''}
    <p class="source-note">
      Median response and not completed are at the smallest session level the study ran at or above ${s.people}.
      <b>Illustrative month</b>: an estimate of one month's bill for that size at list price. For compute that keeps
      billing while nobody is querying, it counts every hour of the month (${HOURS_PER_MONTH}); for compute that stops
      billing when idle, it counts only your ${s.hours} busy hours on each of ${WORKING_DAYS_PER_MONTH} working days.
      Fabric always counts every hour. BigQuery and ClickHouse count every hour here because that is how the study ran
      them: BigQuery with a fixed number of reserved slots, and ClickHouse with its services always running. Either can
      be set up to stop billing when nobody is querying, and then it would count only your busy hours. The estimate
      ignores minimum billing increments,
      discounts and storage; it is arithmetic on list prices, not a quote. Idle billing is inferred from each vendor's
      documentation as of ${IDLE_RULES_AS_OF}; this benchmark did not measure its impact directly.
    </p>
    ${caveats ? `<p class="fit-caveat-h">Read these engines' results with their setup in mind:</p><ul class="fit-caveats">${caveats}</ul>` : ''}`;
  body.querySelector('.fit-toggle')?.addEventListener('click', () => { showAll = !showAll; render(); });
  bindTerms(body);
  return rows;
}

async function renderSpeed(s, v) {
  const sets = datasets(s);
  // Scan queries were measured up to the whole 43-query suite at once, so a larger
  // number reads at 43 here as it does in the results table above.
  const tables = await Promise.all(sets.map(ds => q(speedSql({ ...s, people: peopleAt(s, ds) }, ds))));
  if (v !== version) return;
  $('fit-speed-body').innerHTML = sets.map((ds, i) => {
    const rows = tables[i];
    if (!rows.length) return '<p class="callout">Choose at least one engine above.</p>';
    const people = peopleAt(s, ds);
    const assigned = ds === CLICKBENCH
      ? 'At its <span data-term="entry-tier">entry tier</span> on the scan table'
      : `At the <span data-term="designated">size the study assigned</span> for ${VOLUME_TEXT[ds]}`;
    const trs = rows.map(r => {
      const mapped = r.cheapest_qualifying_typical_ms != null && r.single_user_size !== r.cheapest_qualifying_size;
      return `
      <tr data-platform="${esc(r.platform)}" data-designated="${esc(r.designated_size ?? '')}" data-cheapest="${esc(r.cheapest_qualifying_size ?? '')}" data-change="${esc(r.size_change ?? '')}">
        <td><span class="swatch" style="background:${ENGINE_COLOR[r.engine]}"></span><a href="${engineHref(r.platform)}">${esc(r.engine)}</a></td>
        <td class="grp-start">${esc(sizeName(r.designated_size))}</td>
        <td class="num">${ms(num(r.designated_typical_ms))}</td>
        <td class="grp-start">${r.cheapest_qualifying_size
          ? `${esc(r.cheapest_qualifying_size)}${mapped ? ` <small>(one-user time on ${esc(r.single_user_size)})</small>` : ''}`
          : '<span class="muted">none qualified</span>'}</td>
        <td class="num">${r.cheapest_qualifying_typical_ms == null
          ? (r.cheapest_qualifying_size ? '<span class="muted">not run single-user</span>' : '')
          : ms(num(r.cheapest_qualifying_typical_ms))}${
          r.cheapest_qualifying_typical_ms != null && num(r.completed) < num(r.attempted) ? ` <small>(${num(r.completed)} of ${num(r.attempted)} queries finished)</small>` : ''}</td>
      </tr>`;
    }).join('');
    return `
      <p class="fit-sub">${ds === CLICKBENCH ? 'Scan queries, one table of about 100 GB' : `Join queries at about ${VOLUME_TEXT[ds]}`}</p>
      <div class="table-scroll"><table class="data-table fit-speed-table" data-dataset="${esc(ds)}">
        <thead>
          <tr><th rowspan="2">Engine</th>
            <th colspan="2" class="grp-start grp">${assigned}</th>
            <th colspan="2" class="grp-start grp">At the cheapest size that served ${people} ${people === 1 ? 'person' : 'people'} within ${fmtSla(s.sla)}</th></tr>
          <tr><th class="grp-start">Size</th><th class="num">Typical query, one user</th>
            <th class="grp-start">Size</th><th class="num">Typical query, one user</th></tr>
        </thead>
        <tbody>${trs}</tbody>
      </table></div>`;
  }).join('') + `<p class="source-note">Both times are for one person running one query at a time: the median of three
    runs per query, then the geometric mean across queries. Where the two sizes are the same, the two times are the same
    measurement. Where they differ, the gap between the times is what the change of size did to one person's query.
    Under load ClickHouse ran on three replicas; its single-user time is shown where the study ran one replica of the
    same memory size.</p>`;
  bindTerms($('fit-speed-body'));
}

async function render() {
  const v = ++version;
  const s = state;
  const [primary, second] = datasets(s);
  $('fit-results-scan').hidden = !second;
  $('fit-results').querySelector('h2').textContent = s.kind === 'both'
    ? `Which configurations served ${s.people} people at once on join queries`
    : `Which configurations served ${peopleAt(s, primary)} people at once within ${fmtSla(s.sla)}`;
  $('fit-results-scan').querySelector('h2').textContent = `Which configurations served ${peopleAt(s, CLICKBENCH)} people at once on scan queries`;
  await Promise.all([
    renderResults(s, primary, 'fit-results-body', 'fit-results-q', v),
    second ? renderResults(s, second, 'fit-results-scan-body', 'fit-results-scan-q', v) : null,
    renderSpeed(s, v)
  ]);
  if (v !== version) return;
  const links = datasets(s).map(ds => `<a href="compare.html#sf=${ds}&sla=${s.sla}">${ds === CLICKBENCH ? 'scan queries' : `join queries at ${VOLUME_TEXT[ds]}`}</a>`);
  $('fit-next').innerHTML = `See how latency climbs with sessions for every size on the compare page: ${list(links)}.
    Each engine's page shows its whole size ladder.`;
}

/** Subtitle for exported figures: the whole selection, in words. */
const subtitle = ds => () => describeSelection({
  dataset: ds(),
  sla: state.sla,
  engines: state.engines.length === PLATFORMS.length ? null : state.engines.map(p => ENGINE_LABEL[p]),
  extra: [`${state.people} people at once`, ...(state.budget != null ? [`at most ${usdPerHour(state.budget)} per hour`] : [])]
});

/** Render this page into its HTML; src/site/router.js calls it on every visit. */
export default async function mount() {
  state = readHash();
  showAll = false;
  platformPicked = false;
  writeHash(state);
  wireInputs();
  renderInputs();
  chartTools($('fit-results'), { subtitle: subtitle(() => datasets(state)[0]), csv: () => resultsSql(state, datasets(state)[0]) });
  chartTools($('fit-results-scan'), { subtitle: subtitle(() => CLICKBENCH), csv: () => resultsSql(state, CLICKBENCH) });
  chartTools($('fit-speed'), {
    subtitle: () => describeSelection({ dataset: datasets(state)[0], engines: state.engines.length === PLATFORMS.length ? null : state.engines.map(p => ENGINE_LABEL[p]) }),
    csv: () => datasets(state).map(ds => `SELECT * FROM (${speedSql(state, ds)})`).join(' UNION ALL BY NAME ')
  });
  await render();
}
