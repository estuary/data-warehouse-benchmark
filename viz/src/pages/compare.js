/**
 * The Compare page: every engine on the same charts, driven by one set of
 * filters (workload and data volume, latency target, axes, engines).
 *
 * The charts are Mosaic clients built in src/views/; this module lays them
 * out, builds the filter bar and the section navigation, fills the tables that
 * are plain HTML (the cheapest size per level, the sizing ladders, the scan
 * tables), and gives every chart its download tools.
 */
import { coordinator } from '@uwdata/vgplot';
import { chartTools } from '../site/chart-tools.js';
import { bindTerms } from '../site/glossary.js';
import { describeSelection, fmtSla, VOLUME_TEXT } from '../site/describe.js';
import { headlineStats } from '../data.js';
import { buildViews } from '../views.js';
import { SIZING_VIEWS } from '../views/sizing.js';
import { clicksAddToggles, keyboardToggles, labelMenus } from '../a11y.js';
import { applyEngines, readState, writeState } from '../url-state.js';
import { mountSynthesis } from '../synthesis.js';
import { CLICKBENCH, DEFAULT_SCALE, DEFAULT_SLA_MS, SEPARATE_SESSION, levelsFor } from '../constants.js';
import { BIGQUERY_ON_DEMAND_DATASETS, COST_CONFIDENCE, ENGINE_COLOR, ENGINES } from '../engines.js';
import { ms, ratio, usdExact } from '../format.js';
import { ringKey } from '../plot-helpers.js';

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmt = n => n.toLocaleString('en-US');
const q = text => coordinator().query(text, { type: 'json' });
const shortLabel = l => String(l).replace(/^ENTERPRISE \/ /, '').replace(/ \(Gen2\)$/, '');
const swatch = engine => `<span class="swatch" style="background:${ENGINE_COLOR[engine]}"></span>`;
/** Disconnect `observer` when the reader leaves the page. */
const until = (signal, observer) => {
  signal.addEventListener('abort', () => observer.disconnect(), { once: true });
  return observer;
};

/**
 * The section navigation, in page order. `pair` entries need the TPC-DS/DSB
 * pair and are hidden with their sections when scan queries are selected.
 */
const NAV = [
  { group: 'People served at once', items: [
    { id: 'sec-frontier', n: '1', label: 'People served and hourly price' },
    { id: 'sec-degradation', n: '2', label: 'Response time under load' }
  ] },
  { group: 'Single-user speed and cost', items: [
    { id: 'sec-glance', n: '3', label: 'Speed and cost per query' },
    { id: 'sec-sizing', n: '4', label: 'Speed and cost by size' },
    { id: 'sec-scan', n: '5', label: 'Scan queries by price' },
    { id: 'sec-latency', n: '6', label: 'Time as data grows' },
    { id: 'sec-clickbench', n: '7', label: 'Scan queries one by one' }
  ] },
  { group: 'Query types and data', items: [
    { id: 'sec-patterns', n: '8', label: 'Query patterns under load', pair: true },
    { id: 'sec-skew', n: '9', label: 'Skewed data', pair: true }
  ] },
  { group: null, items: [
    { id: 'sec-reading', n: '', label: 'Before you quote a number' }
  ] }
];

/** Wrap an input in a labelled control group. */
function group(className, ...children) {
  const div = document.createElement('div');
  div.className = `ctl-group ${className}`.trim();
  div.append(...children);
  return div;
}

/** Fire the events an input's listeners expect after a programmatic set. */
function setInput(el, value) {
  el.value = String(value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

/** The current filter state, read from the same Selections and Params the plots use. */
function currentState({ $scale, $engines, $sla }) {
  const raw = $scale.value;
  const engines = Array.isArray($engines.value) && $engines.value.length ? $engines.value.map(p => p[0]) : null;
  return { dataset: raw == null ? String(DEFAULT_SCALE) : String(raw), engines, sla: Number($sla.value) };
}

/** SQL predicate for the engine filter ('' when every engine is selected). */
function engineWhere(engines, col = 'engine') {
  return engines ? `${col} IN (${engines.map(e => `'${e.replace(/'/g, "''")}'`).join(', ')})` : 'true';
}

/**
 * Lay the controls out as labelled groups. The DOM order of the two <select>s
 * (data set first, axes second) and the slider is part of the page's contract
 * with tests/explorer.spec.js and must not change.
 */
function mountControls(controls, elements, signal) {
  const { $sla, $engines } = controls;

  // Live readout beside the slider: its own label says "(ms)", the readout
  // says what that is in seconds.
  const slaValue = document.createElement('output');
  slaValue.className = 'ctl-value';
  slaValue.setAttribute('aria-live', 'polite');
  const render = v => { slaValue.textContent = fmtSla(v); };
  render($sla.value);
  $sla.addEventListener('value', render);
  const slaRow = document.createElement('div');
  slaRow.className = 'ctl-sla-row';
  slaRow.append(elements.slaSlider, slaValue);

  const enginesLabel = document.createElement('span');
  enginesLabel.className = 'ctl-label';
  // A click or tap adds or removes an engine once one is chosen (clicksAddToggles in a11y.js).
  const verb = matchMedia('(pointer: coarse)').matches ? 'tap' : 'click';
  enginesLabel.innerHTML = `Engines <small>${verb} one to show only it, then ${verb} others to add or remove them</small>`;
  const selectAll = document.createElement('button');
  selectAll.type = 'button';
  selectAll.className = 'ctl-select-all';
  selectAll.textContent = 'Select all';
  selectAll.addEventListener('click', () => resetEngines($engines, elements.legend));

  const reset = document.createElement('button');
  reset.type = 'button';
  reset.className = 'ctl-reset';
  reset.textContent = 'Reset filters';
  reset.dataset.tip = 'Back to join queries at 100 GB, a 5 s latency target, log axes and every engine';
  reset.addEventListener('click', () => {
    const selects = $('controls').querySelectorAll('select');
    setInput(selects[0], DEFAULT_SCALE);
    setInput(selects[1], 'log');
    setInput($('controls').querySelector('input[type=range]'), DEFAULT_SLA_MS);
    resetEngines($engines, elements.legend);
  });

  const body = document.createElement('div');
  body.className = 'ctl-body';
  body.append(
    group('', elements.scaleMenu),
    group('', slaRow),
    group('', elements.axisMenu),
    group('ctl-engines', enginesLabel, clicksAddToggles(keyboardToggles(elements.legend)), selectAll),
    reset
  );
  $('controls').append(labelMenus(body));
  mountCompactBar(controls, signal);
  trackControlsHeight($('controls'), signal);
}

/**
 * Once the filter bar has scrolled to the top of the window, fold it to one
 * line that states the selection, with a button to open it again. The full
 * bar is about 150 px tall and would cover the top of every chart it sticks
 * above.
 */
function mountCompactBar(controls, signal) {
  const bar = $('controls');
  const text = bar.querySelector('.ctl-summary-text');
  const expand = bar.querySelector('.ctl-expand');
  const summarize = () => {
    const s = currentState(controls);
    text.textContent = describeSelection(s);
    // Sentences that name the chart's data volume follow the selection.
    for (const el of document.querySelectorAll('.sel-volume')) el.textContent = VOLUME_TEXT[s.dataset] ?? `data set ${s.dataset}`;
  };
  for (const src of [controls.$scale, controls.$engines, controls.$sla]) src.addEventListener('value', summarize);
  summarize();
  expand.addEventListener('click', () => {
    const open = !bar.classList.contains('expanded');
    bar.classList.toggle('expanded', open);
    expand.textContent = open ? 'Done' : 'Change filters';
  });
  // Folding must not change the page's layout. The bar is in normal flow, so a
  // 100 px fold would move everything below it up; the browser's scroll
  // anchoring would pull the page back by as much, the sentinel would come back
  // into view, the bar would unfold, and the page would jump back while the
  // reader scrolls down. The folded bar keeps the full bar's footprint as extra
  // bottom margin instead.
  const gap = parseFloat(getComputedStyle(bar).marginBottom) || 0;
  // The full bar's height is measured unfolded, even while folded: a return
  // visit restores the scroll before the engine legend has drawn, so a height
  // remembered from the last unfolded moment would leave out the legend and
  // the page would come back shorter than the reader left it. The classes are
  // put back before the browser paints, so nothing flickers.
  const unfoldedHeight = () => {
    const expanded = bar.classList.contains('expanded');
    bar.classList.remove('compact', 'expanded');
    const h = bar.getBoundingClientRect().height;
    bar.classList.add('compact');
    bar.classList.toggle('expanded', expanded);
    return h;
  };
  const keepFootprint = () => {
    if (!bar.classList.contains('compact')) {
      bar.style.marginBottom = '';
      return;
    }
    const h = bar.getBoundingClientRect().height;
    bar.style.marginBottom = `${Math.max(0, unfoldedHeight() - h) + gap}px`;
  };
  if ('ResizeObserver' in window) until(signal, new ResizeObserver(keepFootprint)).observe(bar);
  // A folded bar does not change size when its hidden controls redraw (the
  // legend, after its plot), so watch them directly.
  until(signal, new MutationObserver(keepFootprint)).observe(bar.querySelector('.ctl-body'), { childList: true, subtree: true });
  // The sentinel sits just above the bar: when it leaves the viewport past the
  // TOP edge, the bar is stuck. A sentinel below the fold (a phone opening the
  // page) is not intersecting either, and treating that as stuck would fold the
  // bar with a blank footprint under it, then unfold it as the reader arrives. On
  // narrow screens the bar does not stick at all (style.css), so it never folds.
  const sticks = () => getComputedStyle(bar).position === 'sticky';
  // Act on the LATEST entry. Folding shifts the layout for one frame (the
  // footprint margin lands on the next resize callback), so on a fast scroll the
  // sentinel can flicker back into view and one batch carries [visible, hidden];
  // reading the first entry would unfold the bar and never fold it again.
  until(signal, new IntersectionObserver(entries => {
    const entry = entries[entries.length - 1];
    const stuck = sticks() && !entry.isIntersecting && entry.boundingClientRect.top < 0;
    bar.classList.toggle('compact', stuck);
    if (!stuck) {
      bar.classList.remove('expanded');
      expand.textContent = 'Change filters';
    }
    keepFootprint();
  })).observe($('controls-sentinel'));
}

/**
 * Mirror the data-set menu onto <body> so the stylesheet can hide what only
 * exists for the TPC-DS/DSB pair (`.ds-only`) and show the scan-query notes
 * (`.cb-only`). The plots themselves need nothing: their filter already returns
 * no rows for a data set a view has none of.
 */
function trackDataset($scale) {
  const sync = () => document.body.classList.toggle(CLICKBENCH, String($scale.value) === CLICKBENCH);
  $scale.addEventListener('value', sync);
  sync();
}

/**
 * Keep `--controls-h` equal to the sticky bar's rendered height, so a nav
 * click lands each section just below the bar at every width.
 */
function trackControlsHeight(bar, signal) {
  const root = document.documentElement.style;
  const set = () => root.setProperty('--controls-h', `${Math.ceil(bar.getBoundingClientRect().height)}px`);
  set();
  if ('ResizeObserver' in window) until(signal, new ResizeObserver(set)).observe(bar);
  // Back to the stylesheet's default for the pages without the bar.
  signal.addEventListener('abort', () => root.removeProperty('--controls-h'), { once: true });
}

/**
 * Return the engine legend to "all selected". Mosaic's Toggle has no setter,
 * so this replays the presses a person makes: with several engines selected, a
 * plain press on one leaves only it, and a second press on the same swatch
 * clears the selection; with exactly one selected, one press clears it.
 */
function resetEngines($engines, legend) {
  const selected = Array.isArray($engines.value) ? $engines.value : [];
  if (!selected.length) return;
  const label = selected[0][0];
  const press = () => {
    const node = [...legend.querySelectorAll('[class*="-swatches"] > div, [class*="-swatches"] > span')]
      .find(n => n.textContent.trim() === label);
    node?.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true }));
  };
  press();
  if (selected.length > 1) requestAnimationFrame(press);
}

/**
 * Build the side navigation and keep it in step with the scroll position.
 * Buttons, not anchor links: an href="#…" would overwrite the control state
 * the URL hash carries (see url-state.js).
 */
function mountNav(signal) {
  const toc = $('toc');
  const buttons = new Map();
  for (const { group: name, items } of NAV) {
    const ol = document.createElement('ol');
    if (name) {
      ol.className = 'toc-group';
      const h = document.createElement('p');
      h.className = 'toc-title';
      h.textContent = name;
      toc.append(h);
    }
    for (const item of items) {
      const li = document.createElement('li');
      if (item.pair) li.className = 'ds-only';
      const b = document.createElement('button');
      b.type = 'button';
      b.innerHTML = `<span class="n">${item.n}</span> ${item.label}`;
      b.addEventListener('click', () => $(item.id).scrollIntoView({ behavior: 'smooth', block: 'start' }));
      li.append(b);
      ol.append(li);
      buttons.set(item.id, b);
    }
    toc.append(ol);
  }

  // Scroll-spy: the section nearest the top of the viewport is the active one.
  const setActive = id => {
    for (const [k, b] of buttons) b.classList.toggle('active', k === id);
  };
  const targets = [...buttons.keys()].map(id => $(id)).filter(Boolean);
  const visible = new Map();
  const observer = until(signal, new IntersectionObserver(entries => {
    for (const e of entries) visible.set(e.target.id, e.isIntersecting ? e.boundingClientRect.top : Infinity);
    let best = null; let bestTop = Infinity;
    for (const [id, top] of visible) if (top < bestTop) { best = id; bestTop = top; }
    if (best) setActive(best);
  }, { rootMargin: '-25% 0px -55% 0px', threshold: 0 }));
  targets.forEach(t => observer.observe(t));
  setActive(targets[0]?.id);
}

// ── SQL behind each chart: the CSV downloads and the HTML tables ──────────

/** People served per (engine, size) at the current target: the frontier's own aggregate. */
const servedSql = s => `
  SELECT engine, compute_label AS size, any_value(usd_per_hour) AS usd_per_hour,
         coalesce(max(concurrency) FILTER (WHERE p50_ms <= ${s.sla} AND shed_pct < sla_max_shed_pct), 0) AS people_served,
         any_value(failure_mode) AS behavior, any_value(shed_pct_at_max) AS not_completed_pct_at_100
  FROM concurrency WHERE dataset = '${s.dataset}' AND ${engineWhere(s.engines)}
  GROUP BY engine, compute_label ORDER BY people_served DESC, usd_per_hour`;

/** Every level of every ladder, as the latency and not-completed charts draw it. */
const ladderSql = s => `
  SELECT engine, compute_label AS size, usd_per_hour, concurrency AS sessions,
         p50_ms AS median_ms, shed_pct AS not_completed_pct, runs
  FROM concurrency WHERE dataset = '${s.dataset}' AND ${engineWhere(s.engines)}
  ORDER BY engine, usd_per_hour, sessions`;

const CSV = {
  'sec-frontier': servedSql,
  'sec-degradation': ladderSql,
  'sec-glance': s => `
    SELECT e.engine, e.frontier_ms AS median_query_ms, e.typical_ms AS typical_query_ms,
           e.frontier_cost AS mean_cost_per_query_usd, e.cost_confidence, e.queries,
           w.failed_runs, w.runs
    FROM engine_scale e
    LEFT JOIN (SELECT engine, dataset, sum(failures) AS failed_runs, sum(runs) AS runs FROM warm_runs GROUP BY ALL) w
      USING (engine, dataset)
    WHERE e.dataset = '${s.dataset}' AND ${engineWhere(s.engines, 'e.engine')} ORDER BY e.engine`,
  'sec-sizing': s => `
    SELECT engine, compute_label AS size, usd_per_hour, completed, attempted, queries AS shared_queries,
           suite_s, suite_cost AS suite_cost_usd, speedup, cost_multiple, base_label AS compared_with
    FROM ladder_suite WHERE dataset = '${s.dataset}' AND ${engineWhere(s.engines)}
    ORDER BY engine, usd_per_hour`,
  'sec-scan': s => `
    SELECT engine, compute_label AS size, usd_per_hour, typical_ms AS typical_query_ms, median_ms AS median_query_ms, completed, attempted
    FROM size_typical WHERE kind = 'scan' AND ${engineWhere(s.engines)} ORDER BY engine, usd_per_hour`,
  'sec-latency': s => `
    SELECT engine, suite, scale_factor, median(median_server_ms) AS median_query_ms, count(*) AS queries
    FROM curves WHERE ${engineWhere(s.engines)} GROUP BY ALL ORDER BY engine, suite, scale_factor`,
  'sec-clickbench': s => `
    SELECT engine, tier AS size, query_id, median_server_ms, mean_cost_usd, runs
    FROM clickbench WHERE ${engineWhere(s.engines)} ORDER BY engine, query_id`,
  'sec-patterns': s => `
    SELECT engine, pattern, concurrency AS sessions, median(rel) AS ratio_to_typical_query, count(*) AS sizes
    FROM pattern_load WHERE dataset = '${s.dataset}' AND ${engineWhere(s.engines)}
    GROUP BY ALL ORDER BY engine, pattern, sessions`,
  'sec-skew': s => `
    SELECT 'single user' AS pass, engine, NULL AS size, NULL AS sessions,
           median(delta_pct) AS median_change_pct, NULL AS dsb_over_tpcds, count(*) AS pairs
    FROM skew_warm_clean WHERE dataset = '${s.dataset}' AND ${engineWhere(s.engines)} GROUP BY engine
    UNION ALL
    SELECT 'under load', engine, compute_label, concurrency, NULL, skew_ratio, pairs
    FROM skew_load WHERE dataset = '${s.dataset}' AND ${engineWhere(s.engines)}
    ORDER BY 1, 2, 3, 4`
};

/** Sections that ignore the data-volume filter, so their subtitle leaves it out. */
const NO_DATASET = new Set(['sec-scan', 'sec-latency', 'sec-clickbench']);

function mountTools(controls) {
  for (const card of document.querySelectorAll('main > section.view')) {
    const build = CSV[card.id];
    chartTools(card, {
      subtitle: () => {
        const s = currentState(controls);
        const withSla = ['sec-frontier', 'sec-degradation'].includes(card.id);
        return describeSelection({
          dataset: NO_DATASET.has(card.id) ? (card.id === 'sec-latency' ? undefined : CLICKBENCH) : s.dataset,
          sla: withSla ? s.sla : undefined,
          engines: s.engines
        });
      },
      csv: build ? () => build(currentState(controls)) : undefined
    });
  }
}

// ── HTML tables ──────────────────────────────────────────────────────────

/**
 * Under the frontier: the cheapest size that served each level, the buyer's
 * guide's Table 1 read off the same aggregate. Makes the chart legible in a
 * download without hovering.
 */
async function renderSteps(s) {
  const rows = await q(`WITH c AS (${servedSql(s)}) SELECT * FROM c WHERE people_served > 0`);
  const levels = levelsFor(s.dataset).filter(l => l > 0);
  const items = levels.map(level => {
    const ok = rows.filter(r => Number(r.people_served) >= level);
    if (!ok.length) return `<li><b>${level} at once:</b> no single size served this many within ${fmtSla(s.sla)}.</li>`;
    const min = Math.min(...ok.map(r => Number(r.usd_per_hour)));
    const cheapest = ok.filter(r => Number(r.usd_per_hour) === min)
      .sort((a, b) => ENGINES.indexOf(a.engine) - ENGINES.indexOf(b.engine))
      .map(r => `${swatch(r.engine)}${esc(r.engine)} ${esc(shortLabel(r.size))}`).join(', ');
    return `<li><b>${level} at once:</b> ${cheapest}, ${usdExact(min)} an hour</li>`;
  });
  $('frontier-steps').innerHTML = `<p class="chart-title">The cheapest size that served each number of people within ${esc(fmtSla(s.sla))}</p><ul>${items.join('')}</ul>`;
}

async function renderSizingTable(s) {
  const rows = await q(CSV['sec-sizing'](s));
  const el = $('view-sizing-table');
  if (!rows.length) {
    el.innerHTML = '<p class="note">No engine was measured at more than one size for this selection.</p>';
    return;
  }
  const body = rows.map(r => `<tr>
      <td>${swatch(r.engine)}${esc(r.engine)}</td><td>${esc(shortLabel(r.size))}</td>
      <td class="num">${usdExact(Number(r.usd_per_hour))}</td>
      <td class="num">${r.completed} of ${r.attempted}</td>
      <td class="num">${Number(r.suite_s).toLocaleString('en-US', { maximumFractionDigits: 1 })} s</td>
      <td class="num">${ratio(Number(r.speedup))}</td>
      <td class="num">${r.cost_multiple == null ? '' : ratio(Number(r.cost_multiple))}</td>
      <td class="num">${r.shared_queries}</td>
    </tr>`).join('');
  el.innerHTML = `<div class="table-scroll"><table class="data-table">
    <thead><tr><th>Engine</th><th>Size</th><th class="num">List price per hour</th><th class="num">Queries completed</th>
      <th class="num">Suite time</th><th class="num">Speedup</th><th class="num">Cost multiple</th><th class="num">Queries in the shared set</th></tr></thead>
    <tbody>${body}</tbody>
    <caption>Speedup and cost multiple are against each engine's cheapest size on the same ladder, over the queries every size of that engine completed.</caption>
  </table></div>`;
}

async function renderScanTables(s) {
  const budget = Number($('scan-budget').value) || 3;
  const table = (rows, empty) => (rows.length
    ? `<table class="data-table"><thead><tr><th>Engine</th><th>Size</th><th class="num">Typical query</th><th class="num">List price per hour</th></tr></thead>
       <tbody>${rows.map(r => `<tr><td>${swatch(r.engine)}${esc(r.engine)}</td><td>${esc(shortLabel(r.size))}</td>
         <td class="num">${ms(Number(r.typical_ms))}</td><td class="num">${usdExact(Number(r.usd_per_hour))}</td></tr>`).join('')}</tbody></table>`
    : `<p class="note">${empty}</p>`);
  const pick = where => q(`
    SELECT engine, arg_min(compute_label, typical_ms) AS size, min(typical_ms) AS typical_ms,
           arg_min(usd_per_hour, typical_ms) AS usd_per_hour
    FROM size_typical WHERE kind = 'scan' AND usd_per_hour IS NOT NULL AND ${engineWhere(s.engines)} AND ${where}
    GROUP BY engine ORDER BY typical_ms`);
  const [fastest, under] = await Promise.all([pick('true'), pick(`usd_per_hour <= ${budget}`)]);
  $('view-scan-fastest').innerHTML = table(fastest, 'No engines match the current filter.');
  const missing = ENGINES.filter(e => (!s.engines || s.engines.includes(e)) && !under.some(r => r.engine === e));
  $('view-scan-budget').innerHTML = table(under, `No measured size lists at or under ${usdExact(budget)} an hour.`)
    + (under.length && missing.length ? `<p class="source-note">No measured size at or under ${usdExact(budget)} an hour: ${missing.join(', ')}.</p>` : '');
}

/**
 * A plain engine colour key inside the figures that colour by engine and do
 * not name every mark. The interactive legend lives in the filter bar, which a
 * downloaded image does not include.
 */
function mountEngineKeys(controls) {
  const targets = ['view-cfrontier', 'view-scan'].map(id => $(id));
  const render = () => {
    const { engines } = currentState(controls);
    const shown = ENGINES.filter(e => !engines || engines.includes(e));
    const html = shown.map(e => `<span>${swatch(e)}${esc(e)}</span>`).join('');
    for (const t of targets) {
      let key = t.parentElement.querySelector(':scope > .engine-key');
      if (!key) {
        key = document.createElement('div');
        key.className = 'engine-key';
        t.before(key);
      }
      key.innerHTML = html;
    }
  };
  controls.$engines.addEventListener('value', render);
  render();
}

/**
 * The glance chart's dot-shape key, from the same grading the chart draws
 * (engines.js COST_CONFIDENCE; BigQuery is measured where its cost is on-demand).
 */
function renderShapeNote(s) {
  const grade = e => (e === 'BigQuery' && BIGQUERY_ON_DEMAND_DATASETS.includes(s.dataset) ? 'measured' : COST_CONFIDENCE[e]);
  const shown = ENGINES.filter(e => !s.engines || s.engines.includes(e));
  const SHAPES = [
    ['measured', 'circle', 'measured', 'from the vendor\'s bill for each query'],
    ['allocated', 'square', 'allocated', 'from billing'],
    ['modeled', 'triangle', 'modeled', 'from compute time']
  ];
  const parts = SHAPES.map(([g, shape, term, how]) => {
    const es = shown.filter(e => grade(e) === g);
    return es.length ? `<b>${shape}</b> <span data-term="${term}">${term}</span> ${how} (${es.map(esc).join(', ')})` : null;
  }).filter(Boolean);
  $('glance-shape-note').innerHTML = `Dot shape is how the cost was obtained: ${parts.join('; ')}. Dashed lines: medians of the engines shown.`;
}

function mountTables(controls) {
  let version = 0;
  const run = async () => {
    const v = ++version;
    const s = currentState(controls);
    renderShapeNote(s);
    await Promise.all([renderSteps(s), renderSizingTable(s), renderScanTables(s)].map(p => p.catch(err => {
      console.warn('table render failed:', err);
    })));
    if (v === version) bindTerms(document.body);
  };
  for (const src of [controls.$scale, controls.$engines, controls.$sla]) src.addEventListener('value', run);
  $('scan-budget').addEventListener('input', () => {
    run();
    $('scan-budget').dispatchEvent(new CustomEvent('budget'));
  });
  return run();
}

/**
 * Render this page into its HTML; src/site/router.js calls it on every visit.
 * @param {{signal: AbortSignal}} ctx `signal` aborts when the reader leaves the page.
 */
export default async function mount({ signal }) {
  for (const view of SIZING_VIEWS) await coordinator().exec(view);
  const stats = await headlineStats();

  // Controls open in the state the URL describes and mirror every change back
  // into it, so a view can be linked to.
  const initial = readState();
  const { controls, elements } = buildViews(initial);

  // Controls first: the legend attaches to the named frontier plot, so the
  // frontier must be in hand before the legend renders. buildViews already
  // created both, and Mosaic resolves the named-plot request either way.
  mountControls(controls, elements, signal);
  trackDataset(controls.$scale);
  $('legend-cfrontier').append(elements.behaviourLegend);
  $('view-cfrontier').append(elements.frontierConcurrency);
  $('view-configs').append(elements.configs);
  $('view-sweep').append(elements.sweep);
  $('legend-degradation').append(elements.computeLegend, ringKey());
  $('view-degradation').append(elements.degradation);
  $('view-shed').append(elements.shedRate);
  $('legend-skewload').append(elements.skewLegend);
  $('view-skewload').append(elements.skewLoad);
  $('legend-patternload').append(elements.patternLegend);
  $('view-patternload').append(elements.patternLoad);
  $('view-latency').append(elements.latency);
  $('view-frontier').append(elements.frontier);
  $('legend-frontier').append(elements.confidenceLegend);
  $('view-skewtax').append(elements.skewTax);
  $('view-reliability').append(elements.reliability);
  $('view-clickbench').append(elements.clickbench);
  $('legend-sizing').append(elements.sizingLegend);
  $('view-sizing').append(elements.sizing);
  $('view-scan').append(elements.scan);
  $('scan-separate').innerHTML = SEPARATE_SESSION.filter(a => a.kind === 'scan')
    .map(a => `<b>Open rings:</b> ${esc(a.note)}`).join(' ');

  if (initial.engines) applyEngines(elements.legend, initial.engines);
  writeState(controls);
  mountNav(signal);
  mountSynthesis(controls);
  mountTools(controls);
  mountEngineKeys(controls);
  await mountTables(controls);

  $('facts').innerHTML = [
    `<b>${stats.engines}</b> engines`,
    `<b>${stats.configurations}</b> compute sizes under load`,
    `<b>${fmt(stats.gridRuns)}</b> query runs in the concurrency grid`,
    `<b>${fmt(stats.queries)}</b> queries in three suites`,
    `<b>${fmt(stats.agreed)} of ${fmt(stats.compared)}</b> query results with the same row count on every engine`
  ].map(s => `<li>${s}</li>`).join('');
}
