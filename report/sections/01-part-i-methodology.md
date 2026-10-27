<!-- anchor: after-front-matter -->

# Part I: Methodology

> Hand-authored narrative, composed into the generated report. Counts that also
> appear in the generated tables are resolved from the results database at build
> time rather than typed here, so the prose cannot drift away from the data
> (see `report/narrative.py`).

## Why this benchmark exists

Ask which cloud data warehouse is fastest and you will find, as of August 2026, that
the audited answers are few and come from elsewhere. The Transaction Processing
Performance Council's decision-support benchmarks are the most widely cited results
in this category requiring an independent auditor and a full disclosure report.
Its TPC-DS results list currently carries **two active results worldwide**, both
from Chinese cloud vendors. Across TPC-DS and TPC-H together, **none of the seven engines measured here
(Snowflake, Databricks, BigQuery, Redshift, Fabric, ClickHouse, MotherDuck)
holds a current audited result.**
Databricks held one, in 2021; the TPC now lists it as historical.

What fills that vacuum is vendor marketing, and it is not idle. The 2021 dispute in
which Databricks announced a TPC-DS record and Snowflake publicly disputed the
configuration and the pricing behind the accompanying comparison is the genre's
defining episode. Its lesson, in our reading, is that a benchmark without
disclosure cannot be checked even when nobody lies, and that an unverifiable number
should be discounted accordingly.

What this report offers is the thing the audited results and the vendor blogs both
lack: **the shape of the function.** It does not fill the audit gap; we are not
audited, and Section "What this benchmark cannot tell you" says so plainly.

The published comparisons vary one axis at a time. TPC prices a single fixed
configuration, and its throughput metric folds the stream count into the score
rather than reporting behavior across it. ClickBench holds hardware fixed within a
result and excludes concurrency by design, which its maintainers state plainly.
ClickHouse's CostBench is the closest prior art to this report's cost axis, the
work that put a cost dimension on a leaderboard space that had none. It varies data
volume across three row counts, but holds compute configuration fixed at one size per
engine and does not vary user concurrency. The vendor posts that sweep warehouse sizes
hold concurrency at one; the ones that sweep concurrency hold size fixed. We found
none that publishes the surface.

So the question here is not *which engine is fastest*. It is:

> **How do latency and cost behave as a function of the engine, its compute size,
> the data scale, the concurrency offered to it, and the shape of the workload?**

### This report supersedes our 2025 edition

Estuary's 2025 benchmark ran **13 queries** derived from TPC-H at roughly 1 TB and
extended with semi-structured JSON, across five engines, with result caches bypassed
and cost read from each vendor's billing 24 hours after execution. It asked a narrower
question, and this report replaces it.

What changed is method, and it is most of what this section describes: 244 queries
across three suites rather than 13; four scale factors rather than one; **five to
eight compute configurations per engine rather than one**; a concurrency ladder rather than
single-session timings; one ingest path common to every engine; and per-query cost
carrying an explicit confidence tier. This report is also more prescriptive about
*how* each engine is configured: pinned, managed and serverless offerings, with the
configuration recorded on every row.

**Some findings differ from the 2025 benchmark, and that is the thesis rather than an
erratum.** Redshift is the clearest instance: it places second at every scale on the
star-schema suites here, where the 2025 benchmark found it struggling. A different
configuration of the same engine is precisely what this report exists to measure: the
whole argument is that latency and cost are functions of configuration, workload
and load, so a single configuration cannot stand in for an engine. Where the billing
model itself differs between the two (on-demand versus a reservation, provisioned
versus serverless), the two cost axes are not comparable at all, and no comparison
between them should be drawn.

One further difference worth naming, because it cuts at us as easily as at anyone: the
client and the network sit inside any wall-clock measurement. This report records
wall-clock and engine-reported server time separately and publishes both, so a reader
can see how much of a figure is the engine.

### No normalization is neutral, ours included

Every benchmark in this field spends its credibility arguing that its comparison is
fair. We think that argument is unwinnable and mostly beside the point. These engines
are not variants of one design: a single-node execution model, a slot-scheduled
serverless pool, a provisioned RPU cluster, a shared-nothing replica set and a
lakehouse warehouse do not have a common unit. Any normalization (matched hardware,
matched price, matched anything) is a choice, and every choice advantages someone.
The disputes that have consumed this genre are almost all arguments about which
normalization was the honest one.

We take a different approach. **We put the same workload through each engine under
identical treatment, and then describe how each one responds.** The responses differ
enormously, and those differences are the content rather than a problem to be
normalized away. Where we do normalize (to a designated tier, to a fixed budget),
we show more than one normalization and say what each is for, because no single one
is the true comparison.

The practical form of this approach: instead of a single ordering, you will find how
each engine behaves as the data grows, as the compute changes, and as
users arrive, which is what you need to decide whether an engine fits *your*
workload, and which no ranking can tell you.

That is a surface with five axes, and the useful findings live in its curvature:
where doubling compute stops buying speed, where an engine's concurrency envelope
ends, where the cheapest configuration inverts under load. A ranking is a single
number sampled from that surface at coordinates someone else chose for you.

## What we measured

{{fact:engines_with_data}} engines (Snowflake, Databricks SQL, BigQuery, Amazon
Redshift Serverless, Microsoft Fabric, ClickHouse Cloud, and MotherDuck) against
244 queries drawn from three suites:

| Suite | Queries | What it exercises |
|---|---|---|
| TPC-DS | 99 | Decision support over mostly uniform, independent data distributions |
| DSB | 102 | The TPC-DS queries re-run against **skewed** data, plus DSB-specific queries: skew is the variable that changes |
| ClickBench | 43 | Web-analytics scan, filter and aggregate over one wide table |

TPC-DS and DSB run at four scale factors ({{fact:scale_factors}}): roughly 1 GB,
10 GB, 100 GB and 1 TB. **ClickBench has no scale ladder**: it is one fixed
`hits` dataset, so its results are keyed on compute size and concurrency only. Where
a scale tag appears on a ClickBench row it is an artifact of how runs are recorded,
carries no meaning, and is pooled away in analysis.

Each engine was swept across a
ladder of compute sizes (five for Databricks, MotherDuck, Redshift and Snowflake,
seven for BigQuery, seven for Fabric, and more for ClickHouse), crossed with a concurrency ladder
covering {{fact:concurrency_levels}} simultaneous sessions. Coverage is not uniform
across that grid; the coverage matrix shows exactly which cells exist.

**Fabric's compute ladder runs F2 through F128** at 1, 10 and 100 GB. At 1 TB, F2
through F16 are recorded as too small for the scale, and F32, F64 and F128 are
measured.

**There is no cold-start data**: {{fact:cold_row_count}} rows across every engine
and scale. That decision and its cost are described under "What this benchmark cannot tell
you"; the report is explicitly a study of *warm, steady-state, repeated* querying,
the regime a reporting workload lives in.

## One ingest path, and why it is the control

Every prior multi-engine warehouse benchmark we examined loads each engine through
that engine's own fast path: `COPY` for Redshift, `bq load` for BigQuery, and so
on. This is the natural choice and it quietly confounds the experiment, because the
storage layout, the column types, and the load-time behavior then differ per engine
for reasons that have nothing to do with query execution.

We did the opposite. **One capture of one dataset in S3 feeds seven materializations
through a single pipeline, with identical settings.** All seven carry exactly 193
bindings; the sorted set of source collections is byte-identical across all of them;
delta-updates, field selection and the metadata-column exclusion are set the same
way on 193 of 193 bindings on every engine; and the column types are declared once,
from DuckDB's catalog, rather than inferred per destination. The per-engine
differences that remain are enumerated in full: a Snowflake timestamp type, a
MotherDuck delete-handling flag, a Databricks per-binding schema name, and a
ClickHouse database-creation step. None changes the row set or the logical schema.

### The disclosure that belongs with it

**This study was run jointly by Estuary and CorrDyn, and the pipeline described above
is Estuary's product.** Estuary is the data movement apparatus: one capture fed all
seven warehouses from the same stored copy. CorrDyn designed the benchmark, wrote the
harness, ran the campaigns and wrote this report. Estuary is not one of the systems
being measured or compared and does not appear in any ranking.

Every measured vendor was sent its own configuration, price basis and results before
publication, and given a fixed window to respond: one vendor's terms require it and
we extended the same courtesy to all seven. **No vendor, Estuary included, held
editorial control or approval over the findings.** Objections we accepted are listed
with what changed; objections we did not accept are published verbatim alongside our
reasoning.

We state the conflict here, before any result, because the alternative is that a
reader discovers it later and reasonably wonders what else was placed below the
fold.

The honest framing of the conflict points in
an unusual direction: Estuary's product is the
*apparatus*, not a contestant. That makes the loading path simultaneously our strongest methodological
control and the single place where a reader is most entitled to check our work. So
we publish the full load specification and the per-engine binding configuration,
and invite exactly that scrutiny.

### What the single loader costs us

It is a control, not a free lunch, and it takes three things away.

**No engine gets its native load optimizations.** If an engine's own bulk loader
would have produced a better on-disk layout, we do not have it.

**Uniform treatment does not produce uniform outcomes.** The same declared type
lands differently across engines, and those differences are real: several engines
required explicit casts in their query text (enumerated in "Where the SQL differs")
to compensate.

**It is one path, so it is one set of choices.** A different pipeline would land
different physical layouts and could plausibly shift results. We cannot quantify
that from this data, and we do not claim otherwise.

## Physical design: uniform treatment, non-uniform outcome

Every engine is benchmarked on its as-loaded tables, with no clustering keys, no
distribution or sort keys, no partitioning, and no manually-created statistics: on
**all** engines or none.

The reasoning is that the alternative is unbounded. Per-engine physical tuning has
no natural stopping point, and in practice whoever cares most, or knows a given
engine best, wins. The literature warns about exactly this: failing to tune the opponent
as carefully as your own system is among the most common ways a database benchmark
misleads, and it is usually not deliberate. The choice also has precedent: Fivetran's
multi-engine warehouse benchmark likewise declined sort keys, clustering keys and
partitioning.

**But "no physical design" is a decision about our behavior, not a statement about
the resulting systems, and it does not leave them on equal footing.** Each engine
retains whatever its defaults give it:

- **ClickHouse** keeps a MergeTree table with a sorting key, a sorted primary index
  no other engine here receives.
- **Databricks** has **Predictive Optimization enabled**, inherited from the catalog,
  so the platform maintains file layout and statistics on its own. Like ClickHouse,
  it is optimizing whether or not we ask it to.
- **Redshift** is assigned **AUTO** distribution and sort keys, so the service picks a
  layout rather than having none, but it picks without workload knowledge.
- **Snowflake** keeps load-order micro-partitions, unclustered.
- **BigQuery** holds an unpartitioned, unclustered table. We ran it on slot
  reservations at every scale, and single-user on on-demand as well up to 100 GB
  (on-demand is denied at 1 TB). On a reservation it is billed on the reserved slots
  it held while its queries ran, rather than on bytes scanned; under on-demand byte
  pricing the same
  unclustered layout would also be the least forgiving cost structure of the seven.
- **MotherDuck** gets only the file layout.

So the correct reading is **uniform treatment, non-uniform outcome**: the policy
advantages engines whose defaults are strong and penalizes engines that expect to be
tuned.

**These numbers are an untuned baseline, not any engine's ceiling.** A well-tuned
deployment can plausibly beat what we report. What the baseline answers is
a narrower and still-useful question: what does this engine do with your data when
you have not yet done anything to it?

## Where the SQL differs

The 201 TPC-DS and DSB queries are authored once in DuckDB SQL, validated locally,
and translated to each engine's dialect programmatically; the 43 ClickBench queries
are handled separately, below. Beyond that mechanical translation, three
categories of difference exist, and we would rather enumerate them than let a reader
find them.

**Correctness patches.** Five of the seven measured engines carry small hand-edits
(rounding added to a union, explicit casts on averages), made because the landed
column types differ (see above) and a query otherwise returned wrong results. These are corrections, not
tuning, but they mean **"every engine ran identical SQL" is false**, and we will not
write it.

**ClickBench uses the SQL published in the ClickBench project for each engine.** For
ClickBench we use the per-engine SQL published in the benchmark's own repository (contributed per system and merged
upstream) rather than our translations, which matches upstream practice and is
almost certainly fairer than dialect translations of ours would be. The consequence is that these results are **timed but
not compared against our oracle**, and two queries are known to differ semantically
across engines. ClickBench itself does not verify any results. We treat that block as
timing-only. Fabric is the exception: ClickBench
publishes no Fabric variant, so it runs our own T-SQL translation. It is the one engine
whose ClickBench SQL is ours, and the only one whose ClickBench results we validated
before running.

**One engine plays at home, and we cannot measure the advantage.** The queries are
authored in DuckDB SQL and the correctness oracle *is* DuckDB. MotherDuck is a
DuckDB engine. It runs the canonical queries with zero translation distance while
every other engine executes a machine translation of them, paying some idiomaticity
tax whose size we have no way to measure: there is no translation-free arm to
compare against. This deserves the same treatment as the single loader, and it
points in an uncomfortable
direction, because the engine it favors is the one that leads the decision-support
suites, and Estuary, which ran this study jointly with us, operates in the same market.

What we can
say: the dialect gap it bridges is the same gap the correctness patches above make
visible for other engines (types, rounding, aggregate semantics), those patches are
corrections rather than tuning, and ClickBench (where every engine but Fabric runs the SQL
the ClickBench project publishes for it) shows the same engine leading at its top tier.
That is evidence the result is not *only* home advantage; it is not evidence the
advantage is zero.

## How queries were executed

**Warm protocol.** One warmup execution, then three measured runs, per query per
engine per configuration. We report the **median** of the measured runs, not the
best. Reporting the fastest of N is the norm in this field; it maximizes the
apparent gap and hides variance, and every individual run is published so this
choice can be checked.

**Result caching is disabled** wherever the engine exposes a switch, and the exact
statement used is documented per engine. One of the seven engines reported here
(MotherDuck) has no result cache to disable, and nothing was attempted on it.

Caching deserves a caveat sharper than the usual one. Our protocol runs identical
query text with identical constants, four times in a row, on one session. That is
the most cache-favorable design available, and disabling the *result* cache does
not defeat buffer-pool or metadata warming. TPC's answer to this problem is
structural rather than configurational: its substitution parameters are seeded so
that nobody, including whoever runs the benchmark, knows the query constants before the run starts.
We did not adopt that, and it is the most transferable control we left on the table.

**Compute is fixed, and every engine's autoscaling is off.** Six of the seven can add
capacity horizontally: multi-cluster warehouses, transient capacity above a
baseline, reservation autoscaling, added replicas, read scaling. We disabled all of
it and held each configuration at a stated size. Fabric's capacity model is the
exception: its SKU is a fixed capacity-unit budget whose built-in burst smoothing
cannot be turned off, so the SKU is what we pinned and the bursting is part of the
product under test.

Holding compute fixed is the load-bearing control in the report, not a convenience. An engine that may
add capacity *in response to* load has no independent variable left: raising the
concurrency changes the demand and the supply together, so no
observation can be attributed to either. It also makes the central analysis meaningless:
the degradation exponent we fit to each latency curve assumes fixed capacity, and
against an autoscaling engine it would measure how aggressively that vendor's
controller reacts rather than how the engine handles contention.

Holding capacity still is also what produces the findings. Every ceiling in this report
(a per-replica memory limit, an over-provisioning inversion, the difference between an
engine whose latency climbs under load and one that leaves part of a burst unfinished) is only visible because the engine could not
spend its way past it. Part II sets out the full reasoning, the exact setting pinned per
engine, and what the choice deliberately leaves unmeasured.

**Concurrency means real parallel sessions.** For a level of N, we open N independent
connections, one operating-system thread each, hold them at a barrier, and release
them simultaneously so the burst begins at once. This is not a thread pool
multiplexing one connection, which is what a number of published concurrency claims
measure.

The protocol has a limitation, and it applies to every concurrency chart and table in this
report: the workers drain
a **shared queue**, so as the queue empties the offered load decays from N toward 1.
The metric is therefore *median per-query latency while up to N sessions drain a
shared work queue*, not sustained throughput. **We report no queries-per-second.**
The workload is fixed: each level runs each suite once as its own burst, TPC-DS's 99
queries across the N sessions and then DSB's 102, so at 100 sessions each session
runs about one query per burst and nothing waits in the queue at all. A burst ends
when its last query returns. At 100 sessions that takes seconds at 1 GB (a median of
1 to 30 s on every engine), half a minute to four minutes at 100 GB, and longer
only where a query runs into the timeout (most of ClickHouse's 10 GB bursts) or at
1 TB. A level of N is therefore N people starting a query at the
same moment, the dashboard-refresh case, and not N people querying through an
afternoon. Where a
configuration is described as surviving or saturating a level, that is a statement
about its error rate and median latency there, not a throughput measurement.

**Timeouts.** Ten minutes per query at every scale, on every engine. A query that
exceeds it is recorded as a timeout with its duration: a result, not an absence. Only
failures the engine itself raised are counted as engine failures; a query the client
gave up on measures the client, so those runs are excluded from every error rate and
latency figure here.

## What "latency" means here

We report **server-side execution time** as the primary metric, taken from each
engine's own query history, and exclude client wall-clock from cross-engine
comparison because it includes driver and network round-trip that varies with where
the client happens to sit.

That choice carries a problem: **"server-side
time" is not one quantity.** Snowflake's figure includes compilation and queueing;
Databricks' excludes compilation and result fetch; Redshift's excludes queueing and
planning; BigQuery's is the job's own start-to-end span, which excludes creation but
includes its internal queueing. Presented as a single
column, this systematically charges some engines for time it forgives others.

We close the largest of these gaps in the data rather than in a footnote.
Databricks' compilation time, between a tenth and a third of its single-user time in
this study, is read from its query history and added back, so its figures include
compilation as Snowflake's do, single-user and under load alike. The time a query
waits in the engine's own queue is added back the same way on the three engines that
report execution only, from their job histories (Redshift's single-user runs predate the
history we kept, and count their wait as zero): alone, BigQuery's job dispatch adds
about 70 ms a query, and Databricks' and Redshift's waits measured zero at one
session; under load the wait is most of the story (Part V). Redshift's compilation is
not added: its compiled segments are cached after the warm-up run, and its query
history, which keeps a week, does not reach back to these runs.

We therefore publish a per-engine table naming the exact source field and what each
includes and excludes, at the point the metric is defined rather than in an
appendix, and we publish **wall-clock alongside server time** for headline figures.
Wall-clock is the one quantity *defined* identically everywhere, though it is not
therefore comparable, since it also carries each engine's driver and network path.

## What a query costs

Every engine is priced on one clock. A query costs the public list rate of the
compute it ran on, multiplied by the time the engine spent on it. That time is the
engine's total elapsed time, or, on the three engines that report execution apart
from the wait around it (Databricks, Redshift and BigQuery), the execution plus the
engine's queue time, with Databricks' compile added back because its latency
includes it. The client's wall clock is never the basis, since it also carries the
network round trip and the result fetch: at 1 GB it ran 1.2 times engine time on
ClickHouse, 1.6 times on BigQuery, 2.1 times on MotherDuck and 2.9 times on
Redshift. On Fabric it ran 1.1 times.

Pricing a query is not new. Fivetran's warehouse benchmark and ClickHouse's
CostBench both put a dollar figure on a query by applying a published rate card to a
measured runtime, and we are indebted to both. Our per-query figure is the same
construction: the rate card times the engine's time on the query. The vendor's
billing telemetry does two further jobs beyond that figure. Where it exists, it confirms the
published rate is the rate we are charged, and under load it caps or splits the
shared window, so concurrent queries divide a real bill instead of each carrying
the full list rate.

Every figure then carries a confidence tier recording how directly the vendor's
billing backs it:

| Tier | Engines | What the figure is |
|---|---|---|
| **Measured** | BigQuery on-demand | The metered bill itself: bytes billed at the published rate |
| **Allocated** | Databricks, Redshift, Fabric, BigQuery reservations | Rate card times engine time, then capped or split against the capacity the vendor billed (on Fabric, raised instead where a query draws past its SKU) |
| **Modeled** | ClickHouse, MotherDuck, Snowflake | Rate card times engine time, with no invoice to check it against (Snowflake keeps its per-query attribution as a cross-check) |

The tier is a statement about evidence: every figure is list rate times engine time,
so the costs sit on one footing and compare across engines directly, and the tier
says only whether that figure is the metered bill (Measured), the rate-card figure
after billing capped or split it (Allocated), or the rate-card figure with its
rate confirmed but no invoice behind the total (Modeled). The results database
stores the three tiers as `direct`, `proportional` and `estimated`.

Under load the clock still runs once. A window of shared compute is billed while at
least one query is running and split among the queries that ran in it; the idle
stretches between windows (auto-suspend and auto-stop tails, resizes, reconnects,
and the turnaround between queries) are not charged to any query. A query that
failed is billed at zero, and its share of the window stays with the queries that
finished. Each engine splits its window by the best resource measure it reports:
Fabric by CPU time, BigQuery by slot time, Redshift and Databricks by execution
time, and the rest by engine time. The split moves the per-query figures, the
medians and the cost per thousand queries, but leaves the totals unchanged.

The prices are public list rates with no discounts applied, each engine taken at the
cheapest tier that runs our configuration: Snowflake Standard edition at $2 a credit
(Gen2 standard warehouses run on Standard), BigQuery Enterprise (a fixed baseline
reservation needs it), ClickHouse Scale, Fabric pay-as-you-go F SKUs, Databricks
serverless SQL at list, Redshift Serverless at $0.375 per RPU-hour, and MotherDuck
at list. The result is that single-user warm cost comes out at exactly the list rate
for the engine's time on BigQuery, ClickHouse, MotherDuck and
Snowflake at every scale, and between 0.90 and 1.00 times that on Databricks and on
Redshift at 1 TB, where billed capacity pulls it slightly below the rate-card figure.
On Fabric it sits at or above the rate card: a statement that draws more capacity
units than its SKU holds is charged the CU-seconds it consumed, 1.07 to 1.19 times
list at the designated SKUs.

### The boundary of the cost figure

What is inside: the compute held while a query runs, at the provisioned size and the
public list rate.

What is outside, and would move a real bill: storage; data egress; the idle and
auto-suspend time between queries; per-session and resume minimums; the cost of
the load path; client-side compute; and every committed-use or negotiated discount.
All prices are public list rates, captured and dated.

Storage and start-up are not small omissions, and the literature has priced them:
the VLDB 2019 cloud-DBMS study found block storage costing roughly five times object
storage with real per-query latency consequences, and initialization times of five
to twelve minutes across the systems it tested. Both sit outside our boundary,
excluded as properties of a deployment rather than of a query. The figures above
show how much they can matter, so we keep the exclusion visible rather than fold it
in.

Two consequences are worth stating directly. Redshift's priced figure is its RPU
rate for the time its queries held the workgroup, capped by the RPU-seconds AWS
billed; the serverless base capacity still bills in full, with a 60-second minimum,
whenever any query is running, whether queries saturate it or not, so the invoice
runs ahead of the priced figure. BigQuery's
reservation is priced on the baseline slots held for the time queries held them, so
a larger reservation costs more per suite even as it finishes faster; the
consumed-slot price, which charges only for slot-time used, is kept on every row for
readers who prefer pay-per-use economics. Both gaps between what we price and what a
platform bills are laid out in Part V, "What we cannot price, and why".

## What this benchmark cannot tell you

These limits are stated up front, because a limitation a reader finds is worth less
than one they are handed.

**Correctness is verified against an oracle at the smallest scale, and argued from
there.** 197 of the 201 decision-support queries are checked against a DuckDB oracle
at roughly 1 GB (an element-wise comparison with a 1e-4 relative tolerance on
numerics, not a hash), and four are excluded because their tie-breaking is
engine-arbitrary. We do not re-run that oracle at larger scales. Here is why we think
it transfers, stated so you can disagree with the reasoning rather than guess at it.

**The SQL is identical across scales and the data comes from one generator.** Each
engine executes the same file at 1 GB as at 1 TB, and TPC-DS at 1 TB is the same
generator as at 1 GB with a different scale factor. A dialect translation is either
right or wrong; it does not become wrong because there are more rows. So what the
oracle check establishes (that our translation of a query means what the canonical
DuckDB version means) is a property of the SQL, not of the data volume.

**The one class that genuinely varies with volume, we found and removed rather than
assumed away.** Engine-specific numeric behavior in aggregates is real: Redshift's
`AVG` preserves its argument's type, so `AVG` over a BIGINT column truncated to an
integer (50 against the oracle's 50.xx), and `AVG(DECIMAL(p,s))` held scale `s`
where the oracle computes in double. That produced eight concrete failures
(TPC-DS/DSB q07, q13, q17, q18, q22, q26, q27, q39), diagnosed at 1 GB and fixed by
casting aggregate arguments to double precision, for Redshift and for Fabric. The fix
is structural: once the aggregate is computed in double everywhere, more rows cannot
reintroduce a type-scale divergence. What remains is floating-point accumulation
order, which differs by engine and which the 1e-4 *relative* tolerance is sized for:
relative, so it does not loosen as values grow.

**And row-count agreement covers every scale.** Across
{{fact:row_count_agreement_cells}} (query, scale) cells spanning all four scales, the
engines disagree on the number of rows returned **{{fact:row_count_divergences}}
times**, and no engine ever contradicts itself across compute sizes or runs. Most
templates end in `LIMIT 100`, so agreement on exactly 100 rows carries little
information; {{fact:row_count_discriminating}} of those cells agree on some other
count, where the number is a property of the data.

**What is left uncovered, precisely:** a bug in an engine's execution of a plan that
only large data selects (a spill path, a distributed shuffle, a parallel aggregation),
returning a plausible wrong *value* with the right row count. Nothing here would
catch that. We think it is unlikely and we have not measured it, and those are two
different statements.

**Coverage is incomplete, and the gaps are not random.** They cluster where engines
were stressed, which means any average computed over surviving results flatters the
engines that failed most. We therefore publish the completion matrix as a headline
artifact, report failure rate as a result in its own right, and compute cross-engine
figures over the set of queries every admitted engine completed, with counts stated.
"Admitted" is the comparison policy stated in the results sections: an
engine that failed more than 10% of a suite's warm pass at a scale, judged at its
largest measured compute size, is DNF there and contributes a completion figure
rather than a latency.

**The scale ladders measure a sized-up experience, not fixed-hardware
scaling.** Compute is designated per scale by this study, starting small and
stepping up as the data grows (roughly twice the compute per ten times the data);
vendors do not publish recommended sizes by data volume, so the designated size is
a consistent reference point, not a vendor recommendation. The latency-versus-scale
curves therefore entangle data growth with the compute growth a
buyer would purchase alongside it. That is the question a buyer asks (what
happens when my data grows and I size accordingly), but it is not the physicist's
question of how one fixed configuration degrades, and no table here answers the
second one.

**The workload-shape axis has two read-only families.** Every conclusion about
workload fit rests on star-schema decision support and one wide-table scan suite:
no writes, no merges, no updates, no semi-structured data, and only the three
unpaired DSB queries touching fact-to-fact joins. "The ranking is a property of the
workload" is demonstrated across that span and asserted no further.

**No cold-start, ingest, refresh, durability or ACID results.** None were measured.

**We are not audited**, and this is not an audited TPC result. The TPC-DS and DSB
workloads used here are derived from TPC-DS, and the results are not comparable to
published TPC-DS results. All work derived from TPC benchmarks is identified as
derived, and the deviations are enumerated separately.

**One pipeline, one snapshot in time, and a different region per engine.** Each engine
sits where it is provisioned: AWS us-east-1 (S3 staging, Redshift), AWS us-west-2
(ClickHouse), GCP us-central1 (BigQuery), Azure centralus (Databricks). There is no
common region, so data locality and network path differ per engine. Cloud engines
change; these measurements are dated. The runs took place between July and October
2026, so the report is a snapshot of Q3 2026.
