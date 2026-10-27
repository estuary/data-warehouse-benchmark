/**
 * Every term of art the site uses, defined once.
 *
 * Two surfaces read this list:
 *
 *  - **Inline definitions.** Any element with `data-term="<key>"` becomes a
 *    focusable term with a dotted underline; hovering, focusing or tapping it
 *    shows the one-sentence definition, with a link to the full glossary.
 *  - **The glossary** on the method page, rendered from the same entries in
 *    group order, each with an anchor (`method.html#term-<key>`).
 *
 * Definitions follow the buyer's guide: plain terms first (engine, compute),
 * technical ones after, and each written so a reader who has never run a
 * warehouse can follow it. A definition must describe what this study did, not
 * a textbook meaning, wherever the two differ.
 */

/** Glossary groups, in the order the glossary page lists them. */
export const GROUPS = [
  'The basics',
  'What was measured',
  'The query suites',
  'Reading the results',
  'Cost',
  'Vendor compute units'
];

/** key -> { term, group, def }. `def` may contain inline HTML. */
export const TERMS = {
  engine: {
    term: 'Engine',
    group: 'The basics',
    def: 'The data warehouse\'s core data processing software. On this site it means the warehouse product: Snowflake, Databricks, BigQuery, Redshift, Microsoft Fabric, ClickHouse or MotherDuck.'
  },
  compute: {
    term: 'Compute',
    group: 'The basics',
    def: 'The processing power a warehouse uses to run queries. Every vendor sells it in a series of sizes, and choosing among them is the sizing decision this study measures.'
  },
  size: {
    term: 'Compute size (warehouse size)',
    group: 'The basics',
    def: 'One rung of a vendor\'s compute menu: a Snowflake "Medium", 400 BigQuery slots, 32 Redshift RPUs. Vendors name their sizes differently, so this site compares them by list price per hour.'
  },
  ladder: {
    term: 'Size ladder',
    group: 'The basics',
    def: 'The series of compute sizes a vendor offers, from smallest to largest. The study ran each engine on five to eight rungs of its ladder (ClickHouse on five single-replica and eight three-replica sizes).'
  },
  'data-volume': {
    term: 'Data volume',
    group: 'The basics',
    def: 'How much data the queries run against: about 1 GB, 10 GB, 100 GB or 1 TB here. A warehouse should be sized for the reporting tables that people query, which are usually much smaller than everything the company stores.'
  },
  'scale-factor': {
    term: 'Scale factor (SF1, SF10, SF100, SF1000)',
    group: 'The basics',
    def: 'The TPC-DS name for data volume. SF1 is about 1 GB of data, SF10 about 10 GB, SF100 about 100 GB and SF1000 about 1 TB.'
  },
  session: {
    term: 'Session',
    group: 'What was measured',
    def: 'One connection running one query at a time. A level of 100 sessions opens 100 connections and starts their first queries at the same instant; they then work through one shared queue of queries, so fewer run at once as the queue empties. Each scan level runs the suite\'s 43 queries once, so on scan queries the highest level is all 43 at once.'
  },
  'people-at-once': {
    term: 'People querying at once',
    group: 'What was measured',
    def: 'The number of people with a query running at the same moment. This is usually much smaller than the number of people who use the warehouse. If 1 in 10 of a 400-person department is querying at any moment, that is 40 sessions.'
  },
  'single-user': {
    term: 'Single-user pass (warm pass)',
    group: 'What was measured',
    def: 'Every query run one at a time, once to warm the engine and then three measured times, reporting the median of the three. Every single-user speed, sizing and per-query result on this site comes from this pass.'
  },
  'concurrency-grid': {
    term: 'Concurrency grid',
    group: 'What was measured',
    def: 'Each engine at the compute sizes it was run on under load, in bursts of 1, 5, 10, 20, 50 and 100 simultaneous sessions, at each data volume. On scan queries the levels are 1, 5, 10, 20 and 43, because the suite has 43 queries. A few sizes were stopped at a lower level. The source of every "people served at once" result here.'
  },
  designated: {
    term: 'Designated size',
    group: 'What was measured',
    def: 'The compute size this study assigned each engine for each data volume, starting small and stepping up as the data grows. Vendors do not publish recommended sizes by data volume, so it is a consistent reference point for comparison, not a vendor recommendation. Cross-engine single-user comparisons use it. For BigQuery at 1 GB and 10 GB it is on-demand pricing, not a slot reservation.'
  },
  'entry-tier': {
    term: 'Entry tier (scan queries)',
    group: 'What was measured',
    def: 'The size each engine\'s scan-query comparison uses: the lowest rung of its ClickBench campaign (BigQuery on-demand, ClickHouse 1x16GB, Databricks X-Small, Fabric F2, MotherDuck Standard, Redshift 8 RPU, Snowflake X-Small). For BigQuery, ClickHouse, Databricks and MotherDuck it is not the smallest size measured.'
  },
  'fixed-compute': {
    term: 'Fixed compute, no autoscaling',
    group: 'What was measured',
    def: 'Every measurement ran on one unit of compute at one size, with automatic scaling switched off, so each number describes what one unit does. The exception is Fabric, whose smoothing of bursts over later time cannot be switched off. In production, several engines can add clusters or capacity under load.'
  },
  'time-budget': {
    term: 'Time limit',
    group: 'What was measured',
    def: 'Every query had ten minutes to finish. A query still running at ten minutes counts as not completed; it is recorded as a result, never dropped.'
  },
  'server-time': {
    term: 'Server-side time',
    group: 'What was measured',
    def: 'How long the engine itself took to run a query, read from the engine\'s query history. Network time between our client and the warehouse is excluded.'
  },
  'engine-time': {
    term: 'Engine time under load',
    group: 'What was measured',
    def: 'Under concurrency, how long the engine held a query: its wait in the engine\'s own queue included, the network excluded. Snowflake, Fabric, MotherDuck and ClickHouse report this directly. Databricks, Redshift and BigQuery report execution only, so their queue wait is read from their query history; where it was not captured, that engine\'s latency under load is not shown.'
  },
  'result-cache': {
    term: 'Result caching',
    group: 'What was measured',
    def: 'Warehouses can return a stored answer to a query they have seen before. It was switched off on every engine that allows it, so the times here are for doing the work.'
  },
  'join-queries': {
    term: 'Join queries',
    group: 'The query suites',
    def: 'Queries that combine several tables, for example sales with customers, products, stores and dates. Most business reports and dashboards do this underneath, even when they look simple. The TPC-DS and DSB suites are join queries.'
  },
  'scan-queries': {
    term: 'Scan queries',
    group: 'The query suites',
    def: 'Queries that filter and count over one very wide table with no joins, which is what product analytics and event dashboards typically do. The ClickBench suite is scan queries.'
  },
  tpcds: {
    term: 'TPC-DS',
    group: 'The query suites',
    def: 'The data industry\'s standard test of business reporting: 99 queries over a large table of sales joined to smaller tables of customers, products, stores and dates, with evenly distributed (uniform) data. The TPC-DS and DSB workloads here are derived from TPC-DS: this is not an audited TPC result, and the results are not comparable to published TPC-DS results.'
  },
  dsb: {
    term: 'DSB',
    group: 'The query suites',
    def: '102 queries that run the same reports against the same tables as TPC-DS, but with skewed data, where a few products account for most of the sales. Queries 1 to 99 match TPC-DS by number, so each has a uniform-data twin; 100 to 102 are DSB\'s own.'
  },
  clickbench: {
    term: 'ClickBench',
    group: 'The query suites',
    def: '43 web-analytics queries over one fixed table of about 100 GB of page views. It has no data volume to vary, so its results vary the compute size instead.'
  },
  skew: {
    term: 'Skewed data',
    group: 'The query suites',
    def: 'Data where a few values account for most of the rows, the way real sales data usually looks. The skew comparison divides each DSB query\'s time by its TPC-DS twin\'s: below 1× the skewed version ran faster. Single-user figures use the 74 pairs whose tables are the same size in both suites, as the report does; the other 25 read the returns tables, which DSB also makes larger.'
  },
  pattern: {
    term: 'Query pattern',
    group: 'The query suites',
    def: 'A tag for what a query does: aggregation, complex joins (many tables), set operations (UNION, INTERSECT, EXCEPT), subqueries, text search or window functions. A query can carry several tags.'
  },
  'useful-concurrency': {
    term: 'People served (useful concurrency)',
    group: 'Reading the results',
    def: 'The largest number of simultaneous sessions one compute size served while keeping the median response under the latency target and finishing at least 95% of the queries. It can only take the values the study ran: 0, 1, 5, 10, 20, 50 or 100 on join queries, and 0, 1, 5, 10, 20 or 43 on scan queries, because the scan suite has 43 queries. Where a size was not run up to the highest level, the figure is the highest level run, not a measured limit.'
  },
  'latency-target': {
    term: 'Latency target',
    group: 'Reading the results',
    def: 'The median response time you are willing to accept. The buyer\'s guide uses five seconds; the controls here let you choose any target from half a second to twenty seconds, and every "people served" figure recomputes for it.'
  },
  'not-completed': {
    term: 'Not completed',
    group: 'Reading the results',
    def: 'A query that did not return a result: it hit the ten-minute limit, or the engine or its connection returned an error (ClickHouse running out of memory on a join, MotherDuck running out of memory under load). A level where 5% or more of queries did not complete never counts as served.'
  },
  'typical-query': {
    term: 'Typical query time',
    group: 'Reading the results',
    def: 'The geometric mean of each query\'s median time, which keeps one very slow query from dominating the figure. Where a page reports the plain median across queries instead, it says "median query".'
  },
  'suite-time': {
    term: 'Suite time',
    group: 'Reading the results',
    def: 'The sum of each query\'s median time over the queries every compared size completed. Comparing sizes over one shared set of queries keeps a small size that skipped the hardest queries from looking fast.'
  },
  speedup: {
    term: 'Speedup and cost multiple',
    group: 'Reading the results',
    def: 'How many times faster a larger size ran the suite than the smallest size measured, and how many times as much the same run cost. A larger size pays for itself when its speedup exceeds its cost multiple.'
  },
  behavior: {
    term: 'Absorbs, queues, rejects',
    group: 'Reading the results',
    def: 'How an engine behaved as sessions were added. Absorbs: latency stayed flat. Queues: latency rose with sessions. Rejects: latency stayed flat because a large share of queries did not complete.'
  },
  'log-scale': {
    term: 'Log scale',
    group: 'Reading the results',
    def: 'An axis where each step multiplies the value by the same amount (1, 10, 100). Latencies here span a few milliseconds to minutes, and on a log scale both ends stay readable. Every chart can switch to a linear, zero-based axis.'
  },
  'list-price': {
    term: 'List price per hour',
    group: 'Cost',
    def: 'The vendor\'s published rate for running one compute size for one hour, as recorded July to October 2026. Negotiated and committed-use discounts are outside the study, so use the costs to compare engines with each other. They are not price quotes.'
  },
  'cost-per-query': {
    term: 'Cost per query',
    group: 'Cost',
    def: 'What one query cost: the list rate of the compute it ran on, multiplied by the time the engine spent on it, its wait in the engine\'s own queue included and the network excluded. Under load, the queries that ran together split the time the compute was busy. Every engine is priced on this one clock; how directly the vendor\'s bill backs the figure is graded three ways: measured, allocated or modeled.'
  },
  measured: {
    term: 'Measured cost',
    group: 'Cost',
    def: 'The vendor\'s metered bill for the query itself (BigQuery on-demand, billed by the bytes it scanned).'
  },
  allocated: {
    term: 'Allocated cost',
    group: 'Cost',
    def: 'List rate times engine time, then capped or split against the capacity the vendor billed (Databricks, Redshift, Fabric, BigQuery reservations).'
  },
  modeled: {
    term: 'Modeled cost',
    group: 'Cost',
    def: 'List rate times engine time, with no invoice to check the total against (ClickHouse, MotherDuck, Snowflake). Snowflake\'s own per-query attribution is kept as a cross-check.'
  },
  'idle-billing': {
    term: 'Idle billing',
    group: 'Cost',
    def: 'What an engine charges for hours when nobody is querying. Some meters stop between queries; others bill a reserved capacity around the clock. For a warehouse that is quiet most of the day, this can matter as much as the hourly rate. The benchmark did not measure it.'
  },
  credits: {
    term: 'Credits (Snowflake)',
    group: 'Vendor compute units',
    def: 'Snowflake\'s billing unit. Each warehouse size burns a fixed number of credits per hour; this study priced Gen2 Standard warehouses at Standard edition list rates, $2 a credit.'
  },
  dbu: {
    term: 'DBU (Databricks)',
    group: 'Vendor compute units',
    def: 'Databricks Unit, the billing unit for a serverless SQL warehouse. Sizes run from 2X-Small to Large here.'
  },
  slots: {
    term: 'Slots (BigQuery)',
    group: 'Vendor compute units',
    def: 'BigQuery\'s unit of compute. A reservation holds a fixed number of slots; this study ran 100 to 1,000. On-demand pricing instead bills by bytes scanned.'
  },
  rpu: {
    term: 'RPU (Redshift Serverless)',
    group: 'Vendor compute units',
    def: 'Redshift Processing Unit. The base capacity you select, from 8 to 128 RPU here, billed by the second with a 60-second minimum whenever a query is running.'
  },
  'capacity-unit': {
    term: 'Capacity units, F SKUs (Fabric)',
    group: 'Vendor compute units',
    def: 'Microsoft Fabric sells capacity by SKU, from F2 to F128 here. The number is the capacity units it provides; the benchmark ran on Fabric\'s Data Warehouse.'
  },
  replica: {
    term: 'Replica (ClickHouse Cloud)',
    group: 'Vendor compute units',
    def: 'One ClickHouse compute node, labeled by count and memory: "3x64GB" is three replicas of 64 GB each. Replicas add throughput and availability, but by default they do not pool memory for a single query. The single-user pass used one replica; the concurrency grid used three, the vendor\'s default configuration.'
  },
  duckling: {
    term: 'Duckling (MotherDuck)',
    group: 'Vendor compute units',
    def: 'A dedicated DuckDB instance, sized Pulse, Standard, Jumbo, Mega or Giga. In production each user gets their own; in this study every session ran under one account and shared one instance.'
  }
};

const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const plain = html => html.replace(/<[^>]+>/g, '');

let tip;
let hideTimer;

/** One shared popover for every term on the page. */
function popover() {
  if (tip) return tip;
  tip = document.createElement('div');
  tip.className = 'term-tip';
  tip.setAttribute('role', 'tooltip');
  tip.id = 'term-tip';
  tip.hidden = true;
  tip.addEventListener('mouseenter', () => clearTimeout(hideTimer));
  tip.addEventListener('mouseleave', hide);
  document.body.append(tip);
  return tip;
}

function show(el) {
  const entry = TERMS[el.dataset.term];
  if (!entry) return;
  place(el, `<b>${esc(entry.term)}</b><span>${entry.def}</span>
    <a href="method.html#term-${esc(el.dataset.term)}">Glossary</a>`);
}

/**
 * A short note on any element, in the same card as the glossary terms:
 * `data-tip` is the text, `data-tip-title` an optional heading. Used instead
 * of the browser's `title` tooltip, which is slow to appear and styled by the
 * operating system rather than the site.
 */
function showTip(el) {
  const head = el.dataset.tipTitle ? `<b>${esc(el.dataset.tipTitle)}</b>` : '';
  place(el, `${head}<span>${esc(el.dataset.tip)}</span>`);
}

function place(el, html) {
  clearTimeout(hideTimer);
  const t = popover();
  t.innerHTML = html;
  t.hidden = false;
  const r = el.getBoundingClientRect();
  const width = Math.min(340, window.innerWidth - 24);
  t.style.width = `${width}px`;
  const left = Math.max(12, Math.min(r.left + window.scrollX, window.scrollX + window.innerWidth - width - 12));
  t.style.left = `${left}px`;
  t.style.top = `${r.bottom + window.scrollY + 6}px`;
  el.setAttribute('aria-describedby', 'term-tip');
}

/**
 * `[data-tip]` notes, bound once for the whole document by delegation, so
 * tables and cells rendered at any time are covered. A glossary term inside
 * the element keeps its own card.
 */
let tipsBound = false;
function bindTips() {
  if (tipsBound) return;
  tipsBound = true;
  const tipFor = e => (e.target.closest?.('[data-term]') ? null : e.target.closest?.('[data-tip]'));
  document.addEventListener('mouseover', e => { const el = tipFor(e); if (el) showTip(el); });
  document.addEventListener('mouseout', e => { const el = tipFor(e); if (el && !el.contains(e.relatedTarget)) hide(); });
  document.addEventListener('focusin', e => { const el = tipFor(e); if (el) showTip(el); });
  document.addEventListener('focusout', e => { if (tipFor(e)) hide(); });
  // A note that opens on hover or focus closes on Escape, without moving either (WCAG 1.4.13).
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && tip && !tip.hidden) hideTerm(); });
}

/** Close the definition now, as a page switch does (src/site/router.js). */
export function hideTerm() {
  clearTimeout(hideTimer);
  if (tip) tip.hidden = true;
}

function hide() {
  clearTimeout(hideTimer);
  hideTimer = setTimeout(() => { if (tip) tip.hidden = true; }, 120);
}

/**
 * Turn every `[data-term]` under `root` into an inline definition. Safe to call
 * again after new content renders: bound elements are skipped.
 * @param {HTMLElement} root
 */
export function bindTerms(root) {
  bindTips();
  for (const el of root.querySelectorAll('[data-term]:not([data-term-bound])')) {
    const entry = TERMS[el.dataset.term];
    if (!entry) {
      console.warn(`unknown glossary term: ${el.dataset.term}`);
      continue;
    }
    el.dataset.termBound = '';
    el.classList.add('term');
    el.tabIndex = 0;
    el.setAttribute('role', 'button');
    el.setAttribute('aria-label', `${el.textContent.trim()}: ${plain(entry.def)}`);
    el.addEventListener('mouseenter', () => show(el));
    el.addEventListener('mouseleave', hide);
    el.addEventListener('focus', () => show(el));
    el.addEventListener('blur', hide);
    el.addEventListener('click', e => { e.preventDefault(); show(el); });
  }
}

/**
 * Render the full glossary into `container`, grouped, each term anchored.
 * @param {HTMLElement} container
 */
export function renderGlossary(container) {
  container.innerHTML = GROUPS.map(g => {
    const items = Object.entries(TERMS).filter(([, t]) => t.group === g)
      .map(([key, t]) => `<div class="gloss-item" id="term-${esc(key)}"><dt>${esc(t.term)}</dt><dd>${t.def}</dd></div>`)
      .join('');
    return `<section class="gloss-group"><h3>${esc(g)}</h3><dl>${items}</dl></section>`;
  }).join('');
}
