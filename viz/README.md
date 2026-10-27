# Benchmark Explorer

A fully client-side site for the 2026 Cloud Data Warehouse Benchmark results.
[Mosaic](https://idl.uw.edu/mosaic/) drives coordinated views over DuckDB-WASM:
every control re-queries a database running **in the browser**. There is no
server, no API, and no backend: the deployable artifact is a directory of
static files.

## Pages

The site is one Vite build with six HTML entry points, one per page, so every
page has its own URL, opens directly and reloads offline. Once the first page
is up the six are one document: a link to another page swaps that page in over
the database already running, in 50 to 300 ms where a fresh start takes about
2.5 s (see "One document, six pages" below). The pages share the site chrome
(`src/site/layout.js`: navigation, the buyer's-guide call to action, disclosure footer), the glossary
(`src/site/glossary.js`), the chart download tools (`src/site/chart-tools.js`)
and the data layer (`src/data.js`). Each page keeps its own controls in its own
URL hash, and the back button returns to a page with the controls and the
scroll position it was left with.

| Page | For | What it holds |
|---|---|---|
| `index.html` Overview | everyone, first | What was measured, entry points by question, the buyer's guide's four findings with their figures computed live, and the study's limits |
| `fit.html` Find your warehouse | buyers, executives | Workload inputs (join or scan, data volume, people at once, latency target, budget) and every size that served that load, cheapest first, with idle billing and an illustrative monthly figure |
| `engine.html#engine=<id>` Engines | vendors, practitioners | One template for all seven: sizes and prices, what a bigger size buys, people served per size and volume, latency under load, patterns, skew, and its rank on each measure |
| `compare.html` Compare all results | analysts, authors | Every engine on the same charts under one filter bar: people served and price, latency under load, single-user speed and cost, sizing, scan queries by price, data volume, patterns and skew |
| `queries.html` Query explorer | practitioners, vendors | All 244 queries per engine and volume, sortable, with a per-query detail and links to each engine's SQL |
| `method.html` Method and glossary | everyone | How the study ran, six choices and their tradeoffs, limits, correctness checks, the live rate card, idle billing, the glossary, and the parquet downloads |

Every chart and table card carries **PNG, SVG, Data (CSV) and Copy link**
buttons. An image export is rebuilt as a self-describing figure (title, the
current selection, the chart and its legends, a source line and the link that
reproduces it); the CSV is the SQL behind the chart under the same filters.
Terms marked `data-term="<key>"` show their glossary definition on hover, focus
or tap, and link to `method.html#term-<key>`.

Numbers on the pages are never typed into the HTML: each is a query over the
exports, so a stale sentence in the buyer's guide shows up as a mismatch here
rather than being copied. The words about each engine that are not measurements
(what it is, the guide's "pick it when", how it bills idle time from vendor
documentation) live in `src/site/engine-notes.js` and say where they came from.

## Build

```bash
npm install && npm run build
```

`dist/` is the deployable artifact. Copy it to any static host: GitHub Pages,
Cloudflare Pages, S3 + CloudFront. Vite is configured with `base: './'`, so the
bundle works from any path prefix (a project-site subpath, an S3 key prefix)
with no reconfiguration.

To view it locally:

```bash
npx serve dist
```

A static HTTP server is required. **DuckDB-WASM cannot run from `file://`**,
opening `dist/index.html` directly will fail to start.

For development with hot reload:

```bash
npm run dev
```

## Regenerating the data

The app reads the benchmark's own Parquet exports. Regenerate them from the
results database at the repository root:

```bash
uv run benchmark analyze
```

That writes `results/exports/*.parquet`. `npm run build` then copies them into
`public/data/` (via `scripts/prepare-data.mjs`, which also runs on `npm run
dev`). The script fails loudly if an export is missing rather than shipping a
stale or partial payload.

It also holds the exports to `scripts/export-schema.json`, every export's
columns and DuckDB types as the explorer was written against them. The pages
read columns by name from query rows, so a column the analysis drops or
renames would come back `undefined` and render as a blank or "NaN" rather than
failing, and a type change can break the arithmetic (DuckDB hands a BIGINT to
JavaScript as a BigInt, and mixing a BigInt with a number throws). A column
missing or retyped fails the build with the columns named; a new column is only
reported. When a change is intended, update the pages that read those columns,
then record the new schema with `npm run data -- --write-schema`.
`tests/integration/test_explorer_export_contract.py` checks the same file on
every pull request, where a change to `benchmark analyze` lands, and the browser
suite fails any page that shows NaN, undefined, null or Infinity.

The eleven tables consumed:

| File | Grain |
|---|---|
| `concurrency_grid.parquet` | per (workload, platform, scale, compute size, level): p50 engine time (the engine's queue wait included, the network excluded; NULL where it was not captured), the unfinished share and its split into timeouts, dropped connections and engine errors, `usd_per_hour`, per-cell shape constants and `withheld_reason`. `workload` is `decision_support` (TPC-DS + DSB pooled) or `clickbench` (campaign tags pooled, `scale_factor` NULL, levels 1/5/10/20/50; the explorer reads the 50-session level as 43, the whole suite at once) |
| `cost_curves.parquet` | per (platform, query, block, scale): median server/wall ms, p95, mean cost, failures (out of `WARM_MEASURED_RUNS` = 3) |
| `clickbench_summary.parquet` | per (platform, query): ClickBench, with `compute_label` and an `is_entry_tier` flag |
| `pattern_load.parquet` | per (platform, scale, compute size, level, pattern): pattern latency and its in-cell normalization base |
| `skew_load.parquet` | per (platform, scale, compute size, level): median DSB-over-TPC-DS ratio across paired query numbers, with the pair count |
| `skew_impact.parquet` | per (platform, query number, scale): DSB vs TPC-DS server time at the designated tier, one session; `delta_pct` > 0 means the skewed variant was slower. Per-engine medians read `skew_warm_clean` (`src/data.js`): the 74 pairs whose tables are the same size in both suites, the report's basis |
| `row_count_agreement.parquet` | cross-engine correctness evidence |
| `size_ladder.parquet` | per (platform, block, scale, compute size, query): the warm pass at every size. The sizing views sum it over the queries every size completed (`ladder_suite` in `src/data.js`), the report's ladder method; ClickBench rows have `scale_factor` NULL. The pages read it through a `size_ladder` view that leaves out any cell recorded as skipped in the single-user pass (`scale_skips.parquet`), so a partial run left in a skipped cell never becomes a ladder's baseline |
| `rate_card.parquet` | per (platform, compute size): billing unit and list price per hour, for every size in the results. The pages read it through a `rate_card` view that keeps only sizes with a row in `size_ladder`, `concurrency_grid` or `clickbench_summary` |
| `query_catalog.parquet` | per query: suite, number, pattern tags, the repo path of its SQL (`{platform}` stands for the dialect directory), and `returns_pair`, true for the 25 TPC-DS/DSB pairs that read a `*_returns` table (the report's skew split) |
| `scale_skips.parquet` | per recorded skip: engine, data volume, suite, compute size and pass (`warm` or `concurrency`; `*` for all in each of the last three), the reason and the date recorded. The engine page lists an engine's skips under its size ladder and its people-served table, and marks a skipped grid cell "Skipped" (`src/site/skips.js`) |

`benchmark analyze` also writes `pattern_summary.parquet`, which the explorer
does not ship: query patterns are read under load, from `pattern_load.parquet`.
`skew_impact.parquet` feeds the at-a-glance skew-tax bars; the same question
under load is `skew_load.parquet`, which pairs each DSB query with its TPC-DS twin
inside every grid cell (the skew-under-load panels).

These are already deduplicated and policy-filtered to the report's canonical
population (designated compute tier, comparison-admission policy, latest-wins).
The app does not re-derive them; it aggregates from them.

## Shipped size

| Component | On disk | Over the wire (gzip) |
|---|---:|---:|
| DuckDB-WASM module + worker | 35.0 MB | ~7.9 MB |
| DuckDB Parquet extension | 3.1 MB | ~0.7 MB |
| App JS + CSS | 917 KB | 284 KB |
| Parquet data | 1.2 MB | 1.2 MB |
| HTML + service worker | 66 KB | 23 KB |
| **Total `dist/`** | **40.2 MB** | **~10.0 MB** |

The wasm module dominates and is fetched once. A same-origin service worker
(`public/sw.js`) keeps a copy of everything the app fetched, so the page
reloads and works with no network after the first visit. Only 1.2 MB of data is
ever queried, well inside the browser's comfort zone.

The Parquet extension is self-hosted (see below), so its 3.1 MB is part of the
artifact rather than a runtime fetch from `extensions.duckdb.org`.

## Bottom lines, cost confidence and the price ramp

**Every chart ends in a computed sentence.** `src/synthesis.js` runs one small
DuckDB query per chart against the same views the chart reads, under the same
filters, and rewrites the sentence on every change of data set, engine or SLA.
Nothing in those boxes is hand-written, so a box cannot disagree with the chart
above it after the SLA slider moves. The At a glance box doubles as the page's
conclusion. A test checks the frontier's box names the same leader the table
shows and follows the slider.

**Cost confidence is on the chart.** The cost-speed quadrant's dot shape is the
cost-confidence grade from `src/engines.js` (`COST_CONFIDENCE`): circle for
the vendor's metered per-query bill (BigQuery on-demand), square for
time-weighted allocation from billing (Databricks, Redshift, Fabric, BigQuery
reservations), triangle for compute time times rate (ClickHouse, MotherDuck,
Snowflake). A symbol legend sits under the plot and the grade is in the tooltip.
The people-served frontier's axis is list price per hour, a published rate with
the same standing for every engine, so it carries no such marker.

**The price ramp is multi-hue.** `computeColor()` in `src/plot-helpers.js`
runs teal to navy (a YlGnBu tail) at 2 px strokes, so adjacent warehouse sizes
differ in hue as well as darkness; a single-hue ramp collapses in the
seven-panel ladders. Shed rings are drawn in the SLA-rule red so failure and
price are different encodings. The cheapest and priciest size in each panel are
labelled at the end of their line (`*_extremes` views in `src/data.js`) in a short
form that fits a panel (vendor prefix and generation suffix dropped), and the
skew-under-load panels carry the same ramp legend as the response-time ladders.
One phrase describes the ramp everywhere: `PRICE_RAMP_LABEL`.

## Architecture notes

**Why the DuckDB assets are self-hosted.** Mosaic's default `wasmConnector()`
pulls the wasm bundle from the jsDelivr CDN. This app copies it into
`public/duckdb/` and passes explicit URLs instead (`src/db.js`), which keeps the
page working offline and free of third-party runtime dependencies. URLs are
resolved against `document.baseURI` so they survive `base: './'` under any path
prefix.

**The Parquet reader is an extension, and it is self-hosted too.** Since
DuckDB 1.5 the Parquet reader is a loadable extension rather than part of the
wasm module. A DuckDB that has not been handed one autoloads it from
`https://extensions.duckdb.org` on the first `read_parquet`: a runtime CDN fetch
and an offline failure. `src/db.js` loads our own copy explicitly
(`public/duckdb/parquet.duckdb_extension.wasm`, tracked via Git LFS) before any
data is read. The file is version-locked to the module: when
`@duckdb/duckdb-wasm` is upgraded, read the DuckDB version the app logs at
startup and refresh the extension from
`https://extensions.duckdb.org/<version>/wasm_eh/parquet.duckdb_extension.wasm`.
`scripts/prepare-data.mjs` refuses to build without the file, and it checks the
version too: it instantiates the installed module in Node, asks it for its
DuckDB version, reads the version stamped in the extension's metadata footer,
and fails the build with the exact download command when they differ. A
mismatch therefore never reaches the page.

**No SharedArrayBuffer.** Only the single-threaded `eh` bundle is shipped, so
the host never needs to send the COOP/COEP headers cross-origin isolation
requires: most static hosts cannot set them for third-party origins. The `mvp`
fallback is deliberately omitted; every browser since Chrome 95 / Firefox 100 /
Safari 15.2 handles `eh`, and carrying both would double the artifact.

**Parquet is registered, not fetched by DuckDB.** The page fetches each file and
hands it to DuckDB via `registerFileBuffer`. Relative paths do not resolve
inside the wasm worker, and this keeps every byte accounted for.

**Offline is a service worker, not the HTTP cache.** Static hosts send
`must-revalidate` (or nothing) for the HTML, so a reload without a network fails
at the navigation itself however well the assets were cached. `public/sw.js`
precaches the shell and its hashed assets on install, claims the page as soon as
it activates, and then caches the DuckDB files and the data as the page fetches
them. Hashed `assets/` are served cache-first; everything else is network-first
with the cache as fallback, so a redeploy with the same file names (the wasm, the
parquet) is picked up whenever the network is there. The HTML the app fetches to
switch pages in place is cached as the page it is, and each page's code (a chunk
the entry script imports) is cached with the rest, so a link works offline to a
page the tab never opened. It is registered in
production builds only; in dev it would fight Vite's hot reload.

**One document, six pages.** Starting DuckDB-WASM costs about 2.5 s (about
1.5 s compiling the 36 MB module, 0.35 s loading the Parquet extension, 0.25 s
building the tables; the startup log line splits it), while a page's own
queries take tens of milliseconds, so a site of six separate documents would
make the reader wait the full start on every click. All six HTML files load one script,
`src/site/app.js`, which starts the database once (`src/site/boot.js`) and hands
over to `src/site/router.js`. The router answers a plain click on a link to
another page itself: it fetches that page's HTML (from the offline cache when
there is no network), waits for the page being left to finish its queries,
aborts that page's `AbortSignal`, disconnects its charts and inputs from Mosaic
(keeping the query cache, so a page seen before comes back from it) and forgets
its named plots, then puts the new page's content, title, description and body
class in place between the site header and footer and runs its code. The
address bar and history follow each switch. A link to the same page (only the
hash differs), a download, another site or a modified click is left to the
browser, and any failure, or a page from a newer build than the one running
(a deploy since the tab opened), falls back to an ordinary page load.

Each page module in `src/pages/` exports `mount({ signal })`, which renders the
page into the content now in the document and is called on every visit, so a
page resets its own state at the top of `mount` (module-level variables outlive
a visit) and creates its Mosaic Selections and Params there. Anything that
outlives the page's DOM (a window listener, an observer, a timer, a custom
property on `<html>`) is tied to `signal`. A page writes its URL hash only
through `replaceHash` in `src/url-state.js`, which refuses once the address no
longer names that page: the back button changes the address before the page it
leaves is taken down. Every page's stylesheet is loaded on every page, so each
one wraps its rules in `:where(.page-<id>)`, which scopes them to its page's
body class without adding specificity, and `src/site/app.js` imports the sheets
in the cascade order each page had as its own document. The router keeps the
scroll position in the history entry and restores it on back and forward (and
on a reload), following the page as its charts draw until the position fits.

**Cross-view filtering is done by the database.** Interactions publish Mosaic
`Selection` clauses; the coordinator rewrites each dependent client's SQL and
DuckDB answers it. Nothing is filtered by JavaScript array manipulation.

```
$engines ──┬──────────────────────────────► latency, clickbench
           └── $engineScale ──────────────► concurrency frontier + its table,
$scale ────┘                                 degradation, shed rate, skew under load,
                                             pattern load, warm cost frontier

$config (single, from a table row) ────► the configuration's scale sweep

$sla  (Param) ──────────────────────────► concurrency frontier + its table, degradation, sweep
$axis/$zero (Params) ───────────────────► every measurement axis
```

The sweep is deliberately not filtered by `$scale`: its x axis is the scale.

**The controls live in the URL hash.** `#sf=1000&sla=2000&axis=linear&engines=Redshift,MotherDuck&config=redshift:64%20RPU`
opens the page in that state, and every change writes the hash back with
`history.replaceState`, so a view can be linked to from the report. The hash is
a mirror of the Selections and Params, not a second source of truth
(`src/url-state.js`). Engines are the one input Mosaic gives no setter for: the
legend's toggles are pointer handlers, so a linked engine set is replayed as
the pointer presses a person would make once the swatches exist.

**Tooltips are built from extra channels, not `title`.** Observable Plot
replaces the whole tip with the `title` string when one is present, so a
frontier tip would show only a compute label. The marks instead declare
tooltip-only channels (`channels: { 'compute size': 'compute_label', ... }`),
which Mosaic adds to the query and Plot lists in the tip, and hide the channels
that are only plumbing (`tip: { format: { x: false } }`).

**The engine legend is keyboard-reachable.** Mosaic renders each swatch as a
`<div>` with a pointer handler and rebuilds them on every render.
`src/a11y.js` gives them `tabindex`, `role="button"` and `aria-pressed` from a
MutationObserver, dispatches the `pointerdown` Mosaic listens for on Enter or
Space, and hands focus to the rebuilt swatch so a second press lands where the
first did. Readers rarely find the shift-click and a touch screen has none, so
`clicksAddToggles` turns a plain click or tap into Mosaic's shift-toggle: the
first one still shows only that engine, and later ones add or remove one.

**Other accessibility rules the code keeps.** Every toggle and tool is at least
24 px tall (WCAG 2.5.8). Mosaic's menus render their `<label>` unlinked, so
`labelMenus` in `src/a11y.js` ties each to its `<select>`. A chart or table
that scrolls sideways joins the tab order as a named region while it overflows
(`focusableScrollAreas` in `src/site/a11y.js`), so a keyboard can scroll it. A
definition that opens on hover or focus closes on Escape (WCAG 1.4.13).
`tests/a11y.spec.js` holds the text-size floor, the 320 px reflow on every
page, the named menus and the Escape key.

**Axis scales.** Measurement axes are logarithmic by default: latency spans
36 ms to 5,144 ms, and on a linear axis most engines collapse into the floor.
The *Price and time axes* control switches them to linear with a zero baseline;
the two are kept in step by paired Mosaic `Param`s, since a zero baseline is
only valid off a log scale. The scale-factor axis is always logarithmic: it is
a 1 / 10 / 100 / 1000 ladder, and 0 GB is not a measurement. The
people-served axis on the concurrency frontier is the tested session levels
(0, 1, 5, 10, 20, 50, 100; 43 for scan queries), evenly spaced, and its label
says so: a served count can only be one of those levels, and the menu does not
switch it.

**In the ladders, colour is the size of the compute.** The response-time,
not-completed and skew-under-load ladders are faceted by engine, so the engine
is the panel title and colour is free. Each line is coloured by its compute
size's list price per hour on one log ramp (`COMPUTE_PRICE_RANGE` in
`src/constants.js`, a round bracket of the $0.36 to $64.80 the sizes span,
darker for bigger) shared by every panel and every ladder view, with a ramp
legend above the response-time ladders. That is
what lets a reader see that the bigger sizes hold their latency and shed less as
sessions climb, which seven same-coloured lines per panel could not show. The
engine palette still rules everywhere the engine is a mark rather than a facet:
the frontier, the table, the sweep and the single-user charts.

**Skew under load.** The single-user skew-tax bars compare each DSB query with
its TPC-DS twin at one size and one session. `skew_load.parquet` makes the same
pairing inside every grid cell: `skew_ratio` is the median over paired query
numbers of DSB time over TPC-DS time, formed per query and per cell so engine
speed, warehouse size and queueing divide out. The skew-under-load panels draw
it as ladders on the same panels, axis and price ramp as the degradation view. Cells with fewer than 20
surviving pairs are dropped by the reducer.

**Lines are `lineY`, never `line`.** Mosaic adds an ORDER BY on the
independent axis only for the directional variants; a plain `line` connects a
series in GROUP BY result order and draws loops in the ladders. The test suite
checks every ladder path's x coordinates are non-decreasing.

**Engine order ends with ClickHouse.** `ENGINE_LABEL` in `src/engines.js` is
also the facet and legend order. ClickHouse has no SF1000 concurrency data, so
it goes last: its empty panel sits at the edge of the row instead of splitting
the populated ones.

**Rendering waits for `requestAnimationFrame`.** Mosaic schedules plot renders
and query consolidation on animation frames, which browsers suspend in a hidden
tab. A page loaded in a background tab shows its plots when the tab is fronted,
not before. Automated checks must run in a visible or headless page; an
embedded preview pane that reports `document.hidden` will sit on empty views
indefinitely.

## A note on the concurrency frontier

The lead view plots **useful concurrency** (the most sessions an engine served
while holding median engine time under the SLA and leaving under 5% unfinished) against the
**list-price hourly cost** of that compute size. Both come from the benchmark's
own reducer, `analysis/concurrency_shape.py`.

Only the SLA-dependent half is computed in the browser, and it is the same
expression the reducer uses:

```sql
coalesce(max(concurrency) FILTER (WHERE p50_ms <= :sla AND shed_pct < sla_max_shed_pct), 0)
```

`tests/integration/test_committed_results_db.py` holds it to `shapes()` at
three targets across all 146 cells, where it matches exactly, so dragging the
SLA slider re-targets the published metric rather than approximating it.
Cells that clear no level coalesce to 0, matching the reducer's
`max(served) if served else 0`; dropping them would hide precisely the
configurations that cannot meet the target. Cells whose engine time was not
captured (`withheld_reason`; none in the shipped exports) are not
publishable and are filtered out with the client-capped ones, rather than shown
with an execution-only median.

The horizontal axis is a **ladder over the levels actually run**
(0, 1, 5, 10, 20, 50, 100). The metric can take no other value, so a continuous
axis would draw gridlines at session counts never measured. The trade-off is
that equal spacing means one step up the ladder, not equal magnitude. It is
implemented as a linear scale over the ladder *index* rather than a point
scale, because within a level each engine is nudged sideways by a fixed slot:
two configurations that tie on both sessions and cost (Redshift 32 RPU and
MotherDuck Mega both serve 20 sessions at $12/hour) would otherwise sit one on
top of the other, and a point scale has no room to dodge. The tooltip reports
the level, not the plotted position.

**The same cells as a table.** Under the frontier, a collapsed table lists
every (engine, compute size) at the selected scale with its list price,
sessions within the current SLA, behavior signature and shed rate at 100
sessions. Mosaic's stock `vg.table` cannot express an aggregate that depends on
a Param, so `src/config-table.js` is a small `MosaicClient` of its own: the
coordinator injects the same `$engineScale` predicate, the SLA Param triggers a
re-query, and it shares the plot's `usefulConcurrency` SQL node so the two can
never disagree. Clicking a heading sorts by re-querying with an ORDER BY; the
default order is the legend's engine order, largest compute first, so the table
reads like the plot does top to bottom. It lists facts and lets the reader
order them; it computes no score.

**One configuration across data volumes.** Every other view fixes the scale
and varies the engines. Clicking a table row publishes its (platform, compute
size) to a `Selection.single()`, and a small plot under the table shows that
one warehouse size's sessions within the SLA at every scale it ran, on the same
ladder axis as the frontier. It is seeded with Snowflake Medium so it is never
empty, and it is the one view the scale menu does not touch.

The companion **degradation** view is faceted into one panel per engine.
Overlaid, it would be ~39 lines sharing seven colors, with the compute-size
dimension present but unreadable and curves crossing too much to trace. Small
multiples keep every configuration while letting each engine's ladder read as
stacked curves, with the SLA rule repeated in each panel. All panels share both
axes, so heights stay comparable across engines. An **open ring** marks a level
where the shed rate reached the policy threshold: the frontier's FILTER excludes
those levels on shed alone, and without the rings a low median at a level where
half the queries never finished reads as capacity. A second row of panels
below it plots the **shed rate** itself against sessions, with the 5% threshold
as a rule, because a ring says that a level crossed the line and not by how
much (ClickHouse at SF100 sheds 44 to 85% at every size).

The frontier tooltip and the table also carry **USD per hour per session
served**: the list price divided by the sessions within the SLA, blank where
none were served. It is what a buyer computes in their head, so the page does
it for them, but it stays a per-row fact. Plotted, or used as the default sort,
it would become the composite ranking this page deliberately does not have.

Shape encodes the behavior signature, shown in plain words (`absorbs`,
`queues`, `queues+sheds`, `rejects`, `unknown` map to the labels in
`BEHAVIOR_LABELS`, `src/data.js`). A non-completion (`shed_pct`) is any query
that did not return a result: the ten-minute client limit, or an error from the
engine or its connection (ClickHouse memory errors on joins, MotherDuck
out-of-memory at high session counts).

## A note on ClickBench

ClickBench runs one fixed ~100 GB `hits` table, so its campaign tag varies the
**compute tier**, not the data volume: Snowflake X-Small/Small/Medium,
MotherDuck Standard/Jumbo, ClickHouse 16 GB/32 GB, BigQuery on-demand vs a
200-slot reservation. Pooling those tags would average across warehouse sizes.
The Compare page's per-query ClickBench view therefore shows each engine at its **entry
tier**, the lowest rung of its scan campaign (for BigQuery, ClickHouse, Databricks and
MotherDuck, not the smallest size measured; the glossary lists them). The full
ladder is in `size_ladder.parquet`, which the scan-by-price view and the
engine pages read.

ClickBench is also a **data set** in the controls, beside the four scale
factors. Selecting it cuts the same views from ClickBench's own measurements:
the at-a-glance quadrant and reliability bars from the entry-tier warm pass, and
the concurrency frontier, its table and the latency and shed ladders from the
`workload = 'clickbench'` cells of the concurrency grid, which pool the campaign
tags (the table does not change size between them) into one cell per engine and
warehouse size and keep the unified level protocol so the frontier's ladder axis
is the same axis. The menu publishes one predicate on a `dataset` column that
every filtered view derives (`src/data.js`): a scale factor as text for
decision-support rows, `clickbench` for ClickBench rows. Views that need the
TPC-DS and DSB pair (the skew tax, query patterns under load, data skew under
load, and the across-volumes sweep under the table) have no ClickBench rows and
are hidden in that mode, with their nav entries; `src/pages/compare.js` mirrors the selection
onto `<body>` as a class and the stylesheet does the hiding. The URL hash carries
it as `sf=clickbench`.

## Layout across screen sizes

The page is a two-column grid from 1100 px up: a sticky view nav beside a
content column capped at 1080 px, with the controls bar sticky at the top of
that column. Below 1100 px the nav becomes a strip of chips above the content
and the column takes the full width. Below 640 px the gutters tighten and the
controls bar stops being sticky, because at three rows tall it would take a
third of a phone screen; it then never folds to one line either (it folds only
when its sentinel leaves past the top of the window while the bar is sticky).

**Tables on a phone.** Below 640 px every `.data-table` inside `<main>` is laid
out as one card per row, each cell labelled with its column heading.
`src/site/stack-tables.js` sets the labels (`data-label`, resolving two-row
headers) and ARIA table roles from a MutationObserver, so tables rendered later
are covered; `site.css` draws them. Exported images are rendered outside
`<main>` and keep the table layout. A table opts out with `no-stack`: the query
matrix (pinned first column, scrolls) and the rate card (which drops its billing
unit to fit).

**Notes on hover.** Short notes on cells, headings and buttons use
`data-tip="…"` (with an optional `data-tip-title`), never the `title`
attribute: `src/site/glossary.js` shows them at once in the same card as the
glossary terms; the browser's own tooltip is slow and styled by the
operating system. A test fails if a data table carries a `title`.

**Sorting any table.** Every `.data-table` sorts by a click (or Enter or Space)
on a column heading: ascending, descending, then back to the page's own order,
which stays the default. `src/site/sort-tables.js` does it from a
MutationObserver, like the phone labels, and only reorders rows, so the table's
formatting is unchanged; `site.css` draws an arrow on the sorted heading from
`aria-sort`. Values are read from the rendered text (a leading number with its
unit, $, ×, %, ms, s, GB or TB, sorts as a number; other text in natural order;
blanks such as "Not run" last), and `data-sort-value` on a cell overrides that.
A table the page redraws in the same place keeps the reader's sort. Left alone:
the Compare size table and the query matrix, which sort in SQL themselves, a
table marked `no-sort`, and any table with spanned cells.

**The engine page's size ladder** always shows the four join-suite volumes. A
volume where the single-user pass ran one size shows that size's point and says
so in the panel; one with no run says "Not measured at this volume". The note
under the chart is built from the data (which size ran, and how many sizes the
concurrency grid ran there), so panels, notes and the Compare ladder all update
by themselves when new runs are analyzed and the explorer is rebuilt.

**Hand-written caveats and the stale-caveat tests.** The few caveats the data
cannot state for itself live in `src/site/ladder-caveats.js`, such as the
note on Fabric's ladder (flat up to 10 GB, F2 twice as slow as F32 at 100 GB).
Sizes skipped on purpose come from `scale_skips.parquet` (`src/site/skips.js`);
each recorded reason is shown in reader's words from `READER_REASONS`, and the
skips test fails when a new recorded reason has none. Each is tied to
the data it explains, and the `stale caveat` tests in `tests/engine.spec.js`
fail, naming the entry and the file, once a gap is filled or a note's claim
stops holding. After new results land (`benchmark analyze`), run
`npm run build` and `npx playwright test`, then remove or reword what a
stale-caveat failure names.

Plot widths are decided when a page is shown, in `src/plot-helpers.js`, from the same
numbers the stylesheet uses, so a plot fills the column it is given. The
at-a-glance panel splits that width between the quadrant and the two bar
charts when there is room (a content column of 760 px or more) and stacks them
otherwise; the CSS only wraps what the JS has sized, so the two cannot disagree.
On a phone the quadrant keeps one corner caption ("fast, cheap"), and the
skew-tax axis takes a shorter label. The seven-panel ladders have a floor of 812 px because seven panels cannot be read narrower than that;
with room for about 730 px of chart they shrink a little to fit, and below
860 px of window they keep their size and scroll sideways inside their card,
under a "Scroll sideways" hint, since a phone shows no scroll bar. The configuration table under the
frontier scrolls the same way. The page itself never scrolls sideways.

Because the widths are measured when a page is shown (`measurePlots` in
`src/plot-helpers.js`), rotating a tablet or resizing a window scales the plots
down (Plot's own `max-width: 100%`) but never up; opening the page again, or a
reload, lays it out for the new width.

## Source layout

```
src/db.js            DuckDB-WASM bootstrap (module, worker, Parquet extension) + coordinator wiring
src/data.js          Parquet registration and the derived SQL views
src/controls.js      The shared Selections, Params and the inputs that drive them
src/views.js         Composes the views below; the one thing the Compare page calls
src/synthesis.js     The computed "Bottom line" sentence under each Compare chart
src/constants.js     Facts about the data the page hard-codes (ladder, price range, defaults); import-free so tests can check them
src/views/concurrency.js   The concurrency views: frontier, degradation and shed ladders, patterns and skew under load
src/views/warm.js          Single-user views: cost–speed quadrant, skew tax, reliability, data volume, ClickBench per query
src/views/sizing.js        What a bigger size buys (per-engine ladders) and scan queries by price
src/config-table.js  The frontier as a sortable table: a MosaicClient over the same aggregate
src/plot-helpers.js  Attribute bundles shared across plots (engine colors, the 7-panel facet layout)
src/format.js        Tick and tooltip formatters (ms, USD, ratios, percentages)
src/a11y.js          Keyboard and touch access for the swatch legend; labels for Mosaic's menus
src/engines.js       Vendor casing and the stable per-engine color palette
src/site/app.js      The one entry script every page loads: all the stylesheets, then startup
src/site/boot.js     Startup, once per visit: chrome, service worker, DuckDB, views, error surface, then the first page
src/site/router.js   Switches pages in place over the running database; history, scroll, `ready` marker, fallbacks
src/site/layout.js   Site header (Estuary x CorrDyn logos, navigation, buyer's-guide call to action), footer and page list
src/site/report.js   REPORT_URL and REPORT_LIVE: where the buyer's guide is published and whether to link it; reportHref adds the campaign tags
src/site/published.js  SITE_URL and the preview card: each page's canonical address and link preview, written into the HTML at build (vite.config.js)
src/site/a11y.js     Moves Plot's mark labels off <g> elements (to data-mark) as plots render; puts sideways-scrolling areas in the tab order
src/site/brand.js    The favicon (Estuary's icon) each page's <head> inlines
src/brand/           The header logos: Estuary's colour-on-dark PNG and CorrDyn's white SVG, inlined at build
src/site/scroll-anchor.js  Holds the reader's place while charts draw, where the browser has no scroll anchoring
src/site/glossary.js Every term, its definition, inline popovers and the glossary page
src/site/chart-tools.js    PNG / SVG / CSV / link tools on every card
src/site/describe.js       One-line description of a selection, for exports
src/site/engine-notes.js   The words about each engine that are not measurements, with their sources
src/site/stack-tables.js   Column labels and table roles for the one-card-per-row phone layout
src/site/sort-tables.js    Click-to-sort headings for every data table, keeping each page's default order
src/site/ladder-caveats.js Hand-written ladder caveats, each held to the data by the stale-caveat tests
src/site/skips.js          Sizes skipped on purpose, listed from scale_skips.parquet, each reason in reader's words (READER_REASONS)
src/pages/*.js       One module per page (index, fit, engine, compare, queries, method), each exporting `mount({ signal })`
public/sw.js         Offline cache (no build step; must stay dependency-free)
scripts/og-image.mjs       Renders the link-preview card, public/og-image.png, from the header logos and Inter
scripts/prepare-data.mjs   Stages parquet + wasm into public/, checks the extension matches the module's DuckDB version and the exports match export-schema.json
scripts/export-schema.json The columns and types of every export, as the explorer reads them (`npm run data -- --write-schema` records them)
tests/explorer.spec.js     Compare page: mark counts vs SQL, controls, URL state, offline
tests/app.js               The `app` fixture (one running page per worker) and `visit` / `reopen`, which open a page on it by a link
tests/wait.js              Waits the specs share, in place of sleeps: settled (no Mosaic query in flight), offlineReady, and switchTo / shown for a page reached by a link
tests/site.spec.js         Every page clean and on-origin, switching pages in place (one DuckDB start, back and forward, offline), the Overview and Method figures, sizing and scan views, downloads, glossary, phone widths, the copy's claims (scan ceiling, entry tiers, skew, agreement, no dead report link) and phone tables and touch
tests/{fit,engine,queries}.spec.js   One suite per page, each checked against SQL over the same parquet
tests/a11y.spec.js         Every page: no text under 12 px, no sideways scroll at 320 px; named menus; Escape closes a definition
tests/meta.spec.js         Every page's canonical address and link-preview tags name the published site; the preview card ships at its declared size
scripts/smoke.mjs          Post-deploy check of the published URL, run by the workflow
src/url-state.js     Controls in the URL hash, both directions
playwright.config.js       Serves dist/ on :4173 for the suite (VIZ_OUT_DIR / VIZ_PORT override both)
```

The site chrome follows Estuary's visual identity
(estuary.dev/brand-guidelines): a Dark Blue header band, Dark Blue ink, Gray
secondary text, Violet tints for panels and highlights, and Inter, self-hosted
from `@fontsource-variable/inter` so no font request leaves the site. The tokens
are the custom properties at the top of `src/style.css`. Text links and
filled buttons use `--accent`, Estuary Blue darkened just enough for 4.5:1
contrast on white and on the panel tint; `--brand-blue` is the exact brand value
for fills that carry no small text. The data colors below are deliberately not
brand colors: a chart encodes engines, not the publisher.

Text sizes are in rem, so they follow the reader's browser font setting: at the
default 16 px, body text is 16 px and nothing is smaller than 12 px. Chart text
is 12 px, and 11 px for the point labels in the narrow small-multiple panels; it
stays in px because each chart's margins are laid out in px.

Engine colors are Okabe-Ito (color-vision-deficiency safe), assigned near each
vendor's hue where that does not cost separability. One color per engine,
pinned via an explicit `colorDomain`/`colorRange` so an engine keeps its color
even when deselected. The query-pattern palette in the pattern views is Paul
Tol's "bright" scheme, chosen for the same reason and deliberately unlike the
engine palette: there engine is the facet and color means pattern. In that view
dot area is the number of warehouse sizes behind each median, so a point resting
on one or two cells reads as thinner evidence than one pooled over seven.

## Verifying a build

```bash
npm run build && npm run test:install && npm test
```

Run this before pushing a change under `viz/` or `results/exports/`: CI does not
run the suite on every push. The specs in `tests/` drive the built bundle in
headless Chromium (Playwright) against `dist/` on a plain static server, never
`vite dev`. Every expected count and ordering is computed from the same parquet
files the page loads, by a DuckDB running in Node, and compared with the
rendered DOM.

The tests run in parallel, one worker per core by default
(`VIZ_WORKERS=2 npm test` to choose): about 1.4 minutes on four cores, 1.5 with
two workers.
A fresh page pays the 2.5 s DuckDB start (the startup log line splits it: about
1.5 s compiling the wasm module, 0.35 s loading the Parquet extension, 0.25 s
building tables), and most tests' own work takes a fraction of that. So most
tests take the `app` fixture from `tests/app.js`, one page per worker that
stays open, and open their page with `visit`, which follows a link to it,
answered in place in well under a second; a visit to the page already on screen
goes by another page first, so the page mounts afresh and reads its hash as a
load would. Every visit therefore also checks that the page before it cleaned
up after itself, which is what the site does for a reader moving between pages.
A test about starting up (what a fresh load logs or requests, going offline, a
context of its own) takes Playwright's own `page` instead; `visit` loads a page
with nothing running yet. Sharing a page this way more than halves the suite's
run time on four cores. No test sleeps: after an
input a test waits for the page to go quiet (`settled` in `tests/wait.js`,
which reads `window.__vizIdle`, true when Mosaic has no query queued or
running), and before going offline it waits for the offline cache to hold
every page, asset and data file (`offlineReady`). Use those, or a web-first
assertion, rather than `waitForTimeout`.

In CI (`.github/workflows/viz.yml`) the suite runs only on demand: "Run
workflow" on the Explorer workflow, choosing the branch; that run tests and
never deploys. On a push to `main` or to a `preview/*` branch the workflow
builds, publishes to GitHub Pages and smoke-tests the published URLs
(`scripts/smoke.mjs`: the Compare page must reach its ready marker and draw a
frontier, with retries for CDN lag). Every page carries a `noindex, nofollow`
robots tag, so search engines do not list the Pages site; delete that tag (one
line near the top of each HTML file) to allow indexing.

At build, `vite.config.js` writes each page's canonical address and its link
preview (Open Graph and Twitter card tags) into the HTML, from the page's own
`<title>` and description and from `SITE_URL` in `src/site/published.js`, so a
preview or mirror points readers and crawlers at the published site. The card a
shared link shows is `public/og-image.png`; after changing a logo or its copy,
run `node scripts/og-image.mjs` to render it again. Links to the buyer's guide
carry campaign tags (`reportHref` in `src/site/report.js`), with `utm_content`
naming the link (`header`, `home` or `footer`), so the publisher's analytics
can count the readers the explorer sends; the explorer itself runs no analytics.

### Review previews

Pages holds one site per repository, so a preview shares it: `main` is built at
the site root and the most recently pushed `preview/*` branch under
`/preview/`. A push to either rebuilds both from source and deploys them
together, one deploy at a time, so neither can drop the other. To review a
branch, name it `preview/<topic>`, push, and open `<pages url>/preview/` once the
workflow's deploy job finishes (its run summary prints the link). The preview
disappears on the next deploy after the branch is deleted. The `github-pages`
environment's deployment-branch policy must list `preview/*` for this to work.

What the suite checks, and what a manual check should cover when the suite
is not run:

- No console errors and no request to any host but the page's own. The
  Parquet extension is the one to watch: a missing or stale copy makes DuckDB
  reach for `extensions.duckdb.org`.
- The rendered mark count in each view equals the row count the same SQL
  returns from the parquet (at SF100: 39 frontier dots and 39 table rows, of
  which 6 serve no level at a 5 s SLA and 27 at 1 s; 233 degradation points
  plus 56 shed rings and 233 shed-rate points, one skew point per grid cell
  with 20 or more surviving pairs, 244 pattern points, 54 latency points, 7
  warm-frontier points, 301 ClickBench dots).
- Every control re-filters its dependents: the scale menu (all but latency and
  ClickBench, which have no scale axis), the SLA slider (frontier, its table and
  the SLA rule), the axis toggle, the table's column sort, and the engine
  legend by mouse and by keyboard.
- A reload with the network disabled reaches the ready state.
- Selecting ClickBench redraws the frontier, table and ladders from the grid's
  ClickBench cells and the quadrant and reliability bars from the entry-tier
  summary, hides the pair-only views and their nav entries, round-trips through
  the URL hash, and selecting a scale factor again restores everything.
- At 320, 390, 820, 1120 and 1440 px the page does not scroll sideways, the
  at-a-glance side column has room, no ladder is shrunk below 75% of the width
  it was given, and the sticky bar's scroll offset matches its rendered height.
- `cd .. && uv run pytest --run-integration tests/integration` passes: it holds
  the SQL and Python definitions of useful concurrency to the same answer.

The tests also assert the shed-rate ladders draw one point per grid row and
repeat the threshold rule in every panel, that the table's sessions column
equals the reducer's answer at 1 s, 5 s and 20 s, and that clicking a table row
re-targets the scale sweep to exactly the scales that configuration ran. The
frontier's tick labels and `MEASURED_LEVELS` are asserted equal to the distinct
levels in the parquet plus 0, `COMPUTE_PRICE_RANGE` is asserted to bracket every
size's price, and a hash-seeded page is checked to open in that state and to write
changes back.
