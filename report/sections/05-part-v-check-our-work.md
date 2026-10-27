<!-- anchor: after-everything -->

# Part V: Check our work

A benchmark you cannot check is an advertisement with numbers in it. Here is what
we publish so you can check this one, the limits of
what we can tell you, and how to tell us we are wrong.

## What we publish with this report

- **The harness.** The full benchmark tool (connectors, runners, cost collectors,
  analysis and this report generator) at the exact commit these results were built
  from.
- **The query files, per engine.** Every statement executed, in the dialect it was
  executed in, so the per-engine differences enumerated in Part I can be diffed
  rather than believed.
- **The raw results database.** Every measured execution, append-only, including the
  failures. This is the artifact from which every table in this report is computed,
  and from which any of them can be recomputed under a different key.
- **The load specification.** The capture and all seven materializations, so the
  claim that every engine received identical data through identical settings is
  inspectable rather than asserted.
- **The cost configuration and rate card**, with the rate used for every compute
  size and the date it was captured.

## How we check ourselves

Publishing the raw data invites you to recompute our numbers. That invitation is only
worth taking if our own numbers are stable and follow from that data, so the build
enforces both. Each mechanism below is stated with what it cannot catch, because a
check whose limits are unstated is worth less than no check.

**The report regenerates byte-identically from a fixed database.** Same results DB,
same output, every time. No figure moves between renders. The interactive chart
assets (fixed element ids, deterministically ordered points) are byte-stable too.
Money, duration and log-mean aggregates are computed in exact decimal rather than
floating point for this reason, since a parallel float sum can differ in its low
bits and move a displayed value across a rounding boundary. *What this does not do:* guarantee the number is right,
only that it is the same number twice.

**Every hand-authored table is recomputed from the published database at build time,
and the build fails if a published figure disagrees.** {{fact:verified_tables}} tables
are gated this way. The check is stronger than comparing numbers: each verifier
encodes the methodology its table *claims* (designated tiers resolved from the run
plan, per-tier totals taken over the queries every tier completed), so a table
computed a different way than it describes fails, not merely a mistyped digit. Prose
figures are placeholders resolved from the same database rather than typed, so a
sentence cannot drift away from the table above it. *What this does not do:* verify
the design. It confirms that a number follows from our data under the method we
stated; it cannot tell you the method was the right one.

**Cross-engine row-count agreement runs at every scale**, and is reported in full
earlier in this document. Seven engines (six independent dialect translations plus
MotherDuck's untranslated DuckDB SQL) agreeing on how many rows a query returns is
evidence that needs no oracle. *What
this does not do:* catch a wrong value with a right row count.

**Failures we caused are attributed to us, not to the engine.** One shared definition
classifies client-side terminations wherever error rates are read, and an affected
cell is marked `withheld` rather than published as engine behavior, because a
failure rate that is ours, printed against a vendor's name, is the one error that
cannot be corrected after publication. *What this does not do:* find failure classes
we did not think to name.

**A cell that completed fewer queries than its peers carries its count.** Where an
engine finished less than 90% of what the best-covered engine managed at the same
workload and scale, the cell reads `(n=X of Y)`. A median over the queries an engine
survived is not comparable to a median over all of them, and the difference is easy to
miss when both are rendered as one number.

**Sweeps have a declared extent.** The run plan declares the size ladder each engine's
sizing campaign should walk (in the vendor's own published size units, such as
Snowflake's warehouse sizes, where they exist), so coverage is computed against intent rather than against whatever ran. This
is what lets a deliberately short ladder be told apart from an unfinished campaign.

**Deliberate omissions are recorded as such**, with their reason, so an absence in the
coverage matrix can be distinguished from an oversight.

**The execution steps do not branch per engine.** The warm and concurrency runners
run the same steps for every engine; the only engine checks are the compute-pin
preflight and the unused cold path. This is checkable with one grep rather than taken
on trust.

These mechanisms exist for a specific reason: reviewing prose reliably catches
arithmetic and reliably misses **slice selection**: the wrong compute tier, the wrong
deduplication key, the wrong query set. Each such error is individually plausible on
the page and invisible to a reader, the authors included. Recomputation catches the
class of error that reading misses.

## What we do not publish, and why

**Credentials, account identifiers and anything that would let a reader spend our
money.** Configuration is published; access is not.

**Load durations and landed row counts per engine.** Row counts were validated live
against every engine during loading, but never persisted, so we cannot publish
them. The load-path uniformity claim in Part I therefore rests on the specification
rather than on recorded outcomes, a real gap in that control.

## What we cannot price, and why

Every engine is priced on one clock: its compute's public list rate for the time the
engine spent on the query, with billing used to confirm the rate and, under load, to
cap or split the shared window (Part I, "What a query costs"). Billing confirms the
price rather than setting it because each vendor's billing reads most easily in a
different unit: billed warehouse-hours on Databricks, slot-time consumed on BigQuery,
and on Snowflake an attribution view that counts whole warehouse-seconds, one to two
more per query than the engine ran. Those sit on different footings and are not
comparable.

Three limitations in the cost data are not going away, and a reader recomputing from
our raw results will meet them.

**A Fabric query's exact cost depends on what you meter.** Fabric bills a capacity
and meters consumption in smoothed capacity-unit (CU) seconds: a burst may draw well
beyond the SKU's baseline for a while, with the excess spread across later time, and
the data shows bursts using that headroom. At 100 sessions the smallest SKU drew
CPU-seconds at 4.97× its capacity times the burst span at 1 GB, 9.91× at 10 GB and
12.15× at 100 GB, while the largest stayed near or below its baseline (F128 at 0.10×,
0.43× and 1.79×). That headroom is why SKUs 64× apart in capacity served the same
100-session burst at similar latencies at 1 and 10 GB (a 2.0 s median on both F2 and
F128 at 1 GB), though not at 100 GB, where F2's median is 142.2 s against F128's 8.5 s.

We price each window on that metered consumption. A window is one warm statement, or one
burst level split wherever the capacity sat idle, and it costs $0.18 per CU-hour times the greater of the SKU's capacity
across the window and the CU-seconds the window drew, split across the
statements that finished by their CPU share. Where a window stays inside its SKU, the
figure is the list rate for the capacity held; where it bursts past the SKU, the figure
is the CU-seconds consumed, which can run well above list rate times a query's engine
time: a single F2 statement at 100 GB is charged 7.06 to 7.44× that. Concurrent queries
split one window's cost rather than each paying the full rate. Priced per statement
instead, at each statement's full elapsed time times its SKU rate, the concurrency bill
would run several times higher.

Read Fabric's cost column as a capacity-window price split by CPU share rather than a
metered per-query charge. The allocation runs somewhat above the capacity units a query
consumes, because a window is charged for the capacity it holds even where a
query underuses it. The committed billing extract confirms the $0.18 per CU-hour rate on
every meter.

**Redshift's invoice runs above its priced figure, and 188 of its rows have no bill
behind them.** Redshift Serverless bills its base capacity in full whenever any query
is running, with a 60-second minimum per compute period. That minimum is a charge per
period, not per query: a benchmark pass runs hundreds of queries inside one period, so
applying it to each query would bill hundreds of wake-ups that never happened. The
priced figure is the RPU rate for the time queries held the workgroup, capped by the
RPU-seconds AWS billed, and the invoice runs above it.

Redshift keeps its usage history for seven days, and for 188 successful rows no
billing record was read inside that window. They carry engine time, so each is priced
on the same rule, at the RPU rate times the time it held the workgroup, taking its
share of a shared window's cap alongside the billed rows. Those figures are stamped
`estimated` (the Modeled tier), because no invoice backs them. These rows also carry
`metadata.voided_cost_usd`, a figure that applies the 60-second minimum to every query;
no number in this report uses it. No successful row in the database is unpriced.

**BigQuery reservation cost is priced on the baseline it holds.** A fixed reservation
bills for its slots for as long as it is held, so we price each query at the baseline
slots times $0.06 a slot-hour for the time queries held the reservation, split by
each query's slot time. That is what a committed reservation costs, and at 1 TB it
moves with size: $5.06 at 200 slots up to $14.00 at 1,000. The consumed-slot price
charges only for the slots a query uses and moves under 10% across that range; it is
the honest number for the pay-per-use side of BigQuery Editions, so
it is kept on every row (`metadata.slot_ms_cost_usd`) for anyone who runs that way.
The rate and the date we captured it are published, and the consumed figure can be
recomputed from the raw slot-time on every row.

## Known gaps

Stated once, plainly, rather than distributed through the text as caveats:

- **The oracle check runs at roughly 1 GB; above that the argument is transfer, not
  re-measurement.** The SQL is identical at every scale and the data comes from one
  generator, so translation correctness does not vary with volume. The one class
  that does, engine-specific aggregate typing, was diagnosed from eight named failures
  at 1 GB and fixed structurally: aggregates are computed in double. Row-count
  agreement then covers all four scales: {{fact:row_count_divergences}} disagreements
  across {{fact:row_count_agreement_cells}} cells, {{fact:row_count_discriminating}}
  of them agreeing on a count other than the `LIMIT 100` cap. What is left uncovered
  is a bug in an execution plan only large data selects, returning a plausible wrong
  value with the right row count. See Part I for the full reasoning.
- **Every ladder is complete apart from recorded skips, and the unevenness
  left is completion within cells.** Fabric's F2 through F16 at 1 TB are recorded
  skips. Averages over surviving results are not comparable across engines with
  different completion rates; the coverage matrix is published for exactly this reason.
- **Client-caused failures are attributed to us, not to the engine.** Where our own
  harness ends a query the engine had not refused, that cell is withheld rather than
  published as engine behavior. On Databricks, a client-side network fault cut our
  client off from statements the warehouse was still running, and the warehouse
  canceled them as inactive. Those runs are ours, not the warehouse's, and are
  excluded: the Databricks results in Part III (every burst finished below 1 TB bar
  one query of 201, and at 1 TB the unfinished share falling to zero by the Medium
  tier) come from runs without the fault. No harness-failed rows remain in the
  published concurrency figures.
- **Latency under load is the engine's time.**
  Concurrency latency is the engine's time on a query, its queue wait included and
  the network excluded. Databricks, Redshift and BigQuery report execution time only,
  so their queue wait is read from each engine's job history at reconcile and added
  back. (Databricks' compilation, which its execution time also leaves out, is added back
  into every Databricks figure, single-user and under load; see Part I.) Every
  Databricks, Redshift and BigQuery concurrency cell is published with its latency, as
  is every Fabric cell.
- **Concurrency ceilings above 100 sessions were not probed.** The ladder tops out
  at 100, and {{fact:conc_cells_censored}} of {{fact:conc_cells_total}} concurrency
  cells served the highest level asked of them, so their published ceiling (marked
  `+` in the useful-concurrency table) is a lower bound, not a measured limit.
- **No cold-start, ingest, refresh, durability or ACID results.** This is a whole
  regime we do not cover: what it costs to make fresh data query-ready, to maintain
  derived data, and to serve queries while ingest and maintenance run in parallel.
  ClickHouse's CostBench measures exactly that path and prices the line items our
  compute-only boundary excludes; on that question its methodology goes where ours
  does not.
- **ClickHouse's concurrency shape is three replicas.**
  Its concurrency runs use the vendor's default configuration
  (three replicas, per-replica memory swept) while every other engine's
  concurrency ran on a single pinned unit. The configurations were fixed
  throughout, every row names its shape, and the organization's billing confirms
  the hardware was real, but the cross-engine concurrency comparison carries the
  asymmetry: three replicas share a burst that other engines absorbed with one
  unit. Single-session comparisons are unaffected, since those admit only
  single-replica configurations by policy.
- **Same-size ClickHouse replicas do not measure the same.** The warm ClickBench
  single-replica ladder spans 1x8GB through 1x120GB, and its top is clean: the two
  largest rungs are markedly faster than everything below. Its bottom is not: the
  32 GB size returned geomeans 1.8× apart across two separately provisioned
  replicas, so the ordering among the three smallest rungs is provisioning noise
  rather than a curve. We can report that variance but not its mechanism; we did
  not instrument the replicas. (The decision-support single-replica memory ladder
  is the completion-by-memory table in Part III.) Multi-replica measurements are
  reported on the concurrency axis only, because replicas add throughput rather
  than single-query speed.
- **Concurrency is a burst, not a steady state.** Our workers drain a shared queue,
  so degradation figures are lower bounds and no throughput claim is made.
- **No error bars, anywhere.** A warm cell is a median of three executions; every
  concurrency observation is a single execution; the queueing exponents are fits over
  a handful of points. The engines are managed services, measured in different
  calendar windows, and they change underneath a study like
  this one. We did not run enough repetitions to publish dispersion estimates per
  cell, so treat small cross-engine differences as ties. The findings this report
  leans on are order-of-magnitude regime differences precisely because those are the
  claims this design can support; a few percent between two engines is not a finding
  here, ours or anyone else's.

  Suite-level figures (the scaling ladder, the sizing curves, per-suite geomeans) are
  the stable quantities, which is why the report builds its arguments on them. Anything
  computed per query, or from a median of per-query ratios, carries more run-to-run
  movement before any engine difference is involved: the skew ratios in Part III are
  that kind of quantity (Part III draws the same line), so the five engines clustered
  between 0.67 and 0.80 there should be read as one group rather than as ranks.
- **One pipeline, four regions, three clouds, one snapshot in time:** Q3 2026, with
  runs between July and October 2026.

## Tell us we are wrong

We are not audited: this is not an audited TPC result. The TPC-DS and DSB workloads
used here are derived from TPC-DS, and the results are not comparable to published
TPC-DS results. The substitute we can offer is that everything needed to
contradict us is in the box.

If you find an error, we want it publicly. We commit to a **challenge window** after
publication during which we will respond to methodology challenges in writing, log
what we accept, and republish corrected figures with the change recorded rather than
quietly edited. Corrections will carry a date and a description of what moved, so a
number you cited last month can be checked against the one standing today.

**Vendors:** every measured engine was sent its own configuration, price basis and
results ahead of publication. Where a vendor disputed something and we agreed, the
figure in this report is the corrected one. Where we did not agree, the objection is
published with our reasoning so a reader can judge for themselves.

If you re-run this and get a materially different answer, that is the most useful
thing that could happen to this report.
