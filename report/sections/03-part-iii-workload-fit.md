<!-- anchor: before-clickbench -->

# Part III: Workload fit

Two questions a sizing dropdown cannot answer: **does the ranking survive a change of
workload**, and **what happens when more than one person uses the system at once**.
Both answers are less comfortable than a leaderboard, and both are more useful.

## Three suites, two controlled steps

The three query suites in this benchmark are three
points on one axis, not three benchmarks, chosen so that each step changes exactly one thing:

- **TPC-DS**: 99 queries against a star schema with mostly uniform data.
- **DSB**: the *same* schema and the *same* 99 queries, against skewed data.
- **ClickBench**: 43 queries against one wide denormalized table. No joins:
  scan, filter, aggregate.

The first step changes the **data distribution** with the queries held constant. The
second changes the **shape of the query** entirely. An engine's behavior across the
two steps is a more useful fact than its position on any one suite.

There are two things this progression must not be read as. The suites do not share a scale
axis: TPC-DS and DSB run at four scales from 1 GB to 1 TB, while ClickBench is one
fixed ~100 GB dataset. The scale tag on its rows records which campaign a run rode
along with, and is pooled away. And they do not share a correctness basis: the
decision-support suites run our own translations, validated against a DuckDB oracle
at 1 GB and cross-checked by row-count agreement above it, while ClickBench runs the
SQL published in the ClickBench project for each engine: trusted, performance-only,
never result-compared.
Fabric is the exception, running a validated T-SQL translation of ours.

## Step one: same queries, skewed data

DSB regenerates the TPC-DS tables with skewed distributions. In the generated data,
about half of the `store_sales` rows sit on 1% of the item keys, against about 2%
under TPC-DS's uniform generator. We recomputed that concentration from the staged
data at every scale, 1 GB through 1 TB, and it holds at all four. The skew is not
uniform across columns: it is sharpest on the item key,
moderate on the customer key, and the store key has too few distinct values for the
comparison to mean anything. "Skewed data" here means: fact-table foreign keys
concentrated onto a small hot set, most extremely on item.

DSB is not *only* a distribution change, and pooling away that fact would corrupt the
comparison. Three tables are also substantially larger in DSB (`store_returns`
2.5×, `web_returns` 2×, `catalog_returns` 1.5×), and 25 of the 99 paired queries
touch one of them. Those 25 mix a cardinality change into the distribution change,
so the table below reports them in their own column and draws no skew conclusion
from them. Only the remaining 74 pairs isolate what we are after: same SQL, same
row counts, different distribution.

Each cell is the median per-query DSB/TPC-DS latency ratio at 100 GB, each engine at
its designated tier, over the pairs that engine completed in both suites:

<!-- verify: skew_ratio_by_engine -->
| Engine | Distribution only: median ratio | n | Returns-affected: median ratio | n |
|---|---|---|---|---|
| MotherDuck | 0.67 | 74 | 0.87 | 25 |
| Databricks | 0.70 | 74 | 0.79 | 25 |
| Fabric | 0.71 | 74 | 0.92 | 25 |
| Snowflake | 0.79 | 74 | 1.01 | 25 |
| Redshift | 0.80 | 74 | 0.99 | 25 |
| BigQuery | 0.89 | 74 | 1.04 | 25 |

ClickHouse does not appear in this table, for one reason. At its designated 100 GB
tier (one replica, 32 GB), it completes 80 of 99 TPC-DS queries and 82 of 102 DSB queries, failing about 19% of each suite against
this report's 10% completion floor. An engine below that floor did not finish the
suite, and a median over its survivors would compare a different, easier query set.
Publishing one beside six engines that completed every pair would read as a
comparison when it is a subset.

What the single-replica sweep buys instead is a sharper statement about where
ClickHouse's limit sits, because completion at 100 GB turns out to be governed by
memory almost one-for-one with the data:

<!-- verify: clickhouse_completion_by_memory -->
| Per-replica memory | TPC-DS completed | DSB completed |
|---|---|---|
| 8 GB | 27 / 99 | 33 / 102 |
| 16 GB | 55 / 99 | 56 / 102 |
| 32 GB | 80 / 99 | 82 / 102 |
| 64 GB | 91 / 99 | 91 / 102 |
| 120 GB | 96 / 99 | 97 / 102 |

*32 GB is the designated 100 GB tier, which is why the ratio table above excludes
ClickHouse rather than reporting its survivors.*

Only the 120 GB rung clears the floor. 64 GB misses it on DSB by under a percentage
point (10.8% failed against the 10% allowance), which is the closest any
configuration in this benchmark comes to the line. Read down the column and the
constraint is legible: this is an engine whose decision-support ceiling is set by
whether a replica's memory approaches the working set, not by how much throughput
you buy. That is a different failure mode from every other engine here, all of which
completed both suites at their designated tier and differ only in how fast.

Every engine shown in the ratio table completed all 74 clean and all 25
returns-affected pairs.

**At 100 GB every engine ran the skewed data faster, because the skewed data gave the
same queries less to do.** Every median in the distribution-only column sits below 1.00,
on all six engines, which reads at first as contradicting the intuitive story (skew
makes hot keys, hot keys break parallelism, skew is harder). It does not test that
story. The pairs run identical SQL over tables with identical row counts, but the
TPC-DS query constants were chosen for evenly spread data, and on skewed data many of
them land where there is little: of the 74 clean pairs at 100 GB, 8 DSB queries return
fewer rows than their TPC-DS twin, 5 of them none, the same counts on
every engine. The skewed columns are also cheaper to read: for the same SQL, the DSB
queries read 0.36 times the bytes on Snowflake, 0.43 times on BigQuery and 0.81 times
on MotherDuck. No query here targets the hot values, which is the case the intuitive
story is about (DSB's own parameter generator exists to produce such queries, and we
did not use it). We captured no query plans, so how the saving splits between less
matching and cheaper reading is not measured.

What the column does measure is **how much each engine's runtime responds when the
same queries meet a distribution that gives them less to do**: the medians span
{{fact:skew_clean_ratio_range}}. BigQuery's moves the least
({{fact:skew_bigquery_clean_ratio}}), and MotherDuck's and Databricks' move the most.
Those two are 0.03 apart and should be read as tied, not ranked. On this basis
BigQuery's runtime is the least moved by the change, for better and worse: it
converts the least of the reduced work into speed, and by the same token its behavior
on uniform data is the best predictor of its behavior on this skewed data. Neither end
of the span is the good end; the span is the finding.

**Read the column as a span, not a ranking.** A median of per-query ratios inherits
per-query noise, and the one same-configuration replicate in this corpus puts that at
roughly ten percent per query (Part V). The endpoints here are separated by more than
that; the five engines between 0.67 and 0.80 are not, and we do not claim an order
among them.

The returns-affected column is a second, smaller observation rather than a control:
on the 25 pairs where DSB also enlarges the tables being queried, every engine's
ratio rises toward or past 1.00. Snowflake and BigQuery edge above it, Redshift breaks
even, and MotherDuck, Databricks and Fabric still net a saving. We flag it because a pooled
99-query figure would blend the two effects
and understate both.

One engine breaks the pattern at 1 TB. There, at its designated tier, Redshift ran
the skewed queries slower than the uniform ones, the only engine to do so at that
scale; every other engine still ran the skewed data faster. If your data is heavily
skewed and large, test Redshift on it before committing.

DSB also carries three queries of its own (q100 through q102) with no TPC-DS
counterpart, so they appear in every suite-level figure but in no pair; the ratio
table above is pairs-only because a ratio needs both sides. They earn one
observation before moving on: they are the only decision-support queries that join
fact tables to fact tables (products bought together in one basket; buy online,
return, buy again in store), and that one shape scrambles the otherwise stable
order. At 100 GB the basket self-join returns {{fact:dsb_q100_spread}} (the geomean
leader's single slowest DSB query), and the three together carry
{{fact:dsb_unpaired_share_range}}. Three queries are an anecdote, not a ranking
axis, and we captured no plans to explain the spread. We flag it anyway, for two
reasons: these templates are the closest thing in the suite to real funnel and
basket analytics, and their weight in the suite totals means an engine's DSB
aggregate is partly a verdict on a join shape the paired comparison never sees.

## Step two: same engines, different query shape

ClickBench replaces the star schema (a fact table joined to dimensions, the join
graph doing most of the work) with one wide table and no joins at all. Comparing an
engine across the two families asks: **does the ranking survive a change in the
shape of the query?**

One honesty before the answer: the step is not as clean as the skew step, because
it changes more than shape. The decision-support suites run our machine
translations of DuckDB-authored SQL; ClickBench runs the SQL published in the
ClickBench project for each engine, written for that engine (Part I, "Where the SQL
differs"). So an engine crossing this
step changes workload shape *and* SQL authorship at once, and some unknown share of
any movement belongs to the second. For Fabric alone the authorship does not change:
its ClickBench queries are our translation too, so crossing the step moves only the
shape for it. We keep the comparison because the
architectural evidence points the same way independently of authorship (the
memory-model argument below does not depend on who wrote the SQL), but the reader
should carry the confound across this section.

Does the ranking survive? It does not.

On the star-schema suites, ClickHouse runs **{{fact:clickhouse_star_trajectory}}**,
each figure over the queries every engine completed at that scale. The
trajectory is the finding: the gap widens as the data grows, by 100 GB it is
failing more than a tenth of each suite's warm pass (under this report's
completion floor, so no latency there enters a comparison). At 1 TB it **did
not finish the workload**, completing {{fact:clickhouse_sf1000_completion}} queries
before hitting its per-replica memory ceiling. A DNF is a result, not a gap, and
it enters as a completion figure, never a latency, because a median over the surviving
queries would compare a different, easier suite. On ClickBench the same engine is third at its
fastest measured tier, and the cheapest overall to run the suite. Nothing about the engine
changed.

### The comparison depends on what you hold constant

Tier choice is where most benchmark tables quietly mislead, so here are both versions.

**Each engine at its fastest measured tier**, which is what a leaderboard reports:

<!-- verify: clickbench_fastest_tier -->
| Engine | Geomean | Tier | Suite cost | $/1k queries | List $/hr |
|---|---|---|---|---|---|
| MotherDuck | 74 ms | Giga | $0.0366 | $0.43 | $24.00 |
| Redshift | 104 ms | 128 RPU | $0.1327 | $2.18 | $48.00 |
| ClickHouse | 159 ms | 1x120GB | $0.0217 | $0.26 | $4.48 |
| Snowflake | 190 ms | X-Large | $0.1192 | $2.12 | $43.20 |
| BigQuery | 510 ms | ENTERPRISE / 600 slots | $0.2397 | $5.35 | $36.00 |
| Databricks | 598 ms | Large | $0.1634 | $3.19 | $28.00 |
| Fabric | 1380 ms | F16 | $0.1293 | $2.18 | $2.88 |

*MotherDuck's Giga is priced at $24.00 an hour, the rate MotherDuck published after the runs; it was $36.00 while they ran.*

Without the price column, ClickHouse sits third in that table. With it, something
else is going on: **ClickHouse runs the suite for {{fact:cb_clickhouse_suite}} at a
{{fact:cb_clickhouse_ms}} geomean, while BigQuery spends {{fact:cb_bigquery_suite}}
to return {{fact:cb_bigquery_ms}} and Databricks {{fact:cb_databricks_suite}} for
{{fact:cb_databricks_ms}}.** Every engine faster than ClickHouse here runs the suite
for at least {{fact:cb_price_multiple_min}} its cost. And BigQuery's largest
reservation measured is slower than its mid-sized one, so past some point the extra
spend stops helping on this workload.

**Each engine at its cheapest ClickBench pass:**

<!-- verify: clickbench_cheapest_suite -->
| Engine | Suite cost | Tier | Geomean | $/1k queries | List $/hr |
|---|---|---|---|---|---|
| ClickHouse | $0.0049 | 1x8GB | 437 ms | $0.05 | $0.30 |
| Snowflake | $0.0228 | X-Small | 385 ms | $0.45 | $2.70 |
| MotherDuck | $0.0248 | Standard | 253 ms | $0.19 | $2.40 |
| Redshift | $0.0475 | 16 RPU | 243 ms | $0.69 | $6.00 |
| Fabric | $0.0719 | F4 | 1533 ms | $0.58 | $0.72 |
| Databricks | $0.0856 | X-Small | 1176 ms | $1.40 | $4.20 |
| BigQuery | $0.0933 | ENTERPRISE / 100 slots | 1035 ms | $1.84 | $6.00 |

Now the order changes again. ClickHouse runs cheapest here too, at its smallest
replica, but speed and cost part ways: the two fastest passes in this table
(Redshift at 16 RPU, MotherDuck Standard) sit mid-table on cost, and the cheapest
pass is only mid-speed. The two tables answer different questions (*how fast can this
go* versus *how cheaply can it run the suite*), and a buyer paying a monthly bill
usually wants the second.

One assumption sits under both tables and must not travel silently: the suite cost
counts compute only while queries are running, and leaves out the idle time and
per-period minimums a real invoice carries, the same boundary the rest of this report
draws (the cost method in Part I). An engine that stops the meter between queries
comes closest to that figure on a real bill; an always-on replica or a reservation
billed through quiet hours runs furthest above it, so the boundary flatters those
most. The price gaps here are real for a saturated dashboard fleet and progressively
less real as the hardware idles. Both tables also
carry Part I's cost-confidence tiers. ClickHouse, MotherDuck and Snowflake are
priced from compute time and list rate rather than read from a billing API, the
report's Modeled tier, while BigQuery's reservation rows and Fabric's capacity rows
are Allocated. On Fabric's smaller SKUs a ClickBench pass draws more capacity units
than the SKU holds, so its suite cost runs above list rate times its engine time and
the hourly-rate column understates it. The cheapest and most quotable ratios here
belong to that Modeled tier: they rest on the rate card with no invoice behind them.

**Why no multi-replica ClickHouse configuration appears:** ClickHouse Cloud scales by
adding replicas, and replicas add throughput and availability, not single-query
speed. Our one multi-replica warm measurement, taken side by side with the same
memory at one replica, makes the case itself:
{{fact:cb_replica_null_result}}, the same work at twice the hourly price. So
single-session tables admit only its single-replica configurations, which is the
same vertical-sizing axis every other engine here is measured on; the multi-replica
ladder is reported where replicas help, in the concurrency section. The
single-replica memory ladder itself is measured across the full 1x8GB-to-1x120GB
sweep, and per-replica memory moves the scan workload: the two largest rungs are
markedly faster than everything below them. Below 64 GB the ordering is not
stable: the same 32 GB size returned geomeans up to 2× apart across three
separately provisioned replicas, so read the three smallest rungs as one band
rather than a curve (Part V carries the variance note).

### What separates the two workloads

We looked for a finer distinction *within* the decision-support suites and did not
find one worth reporting. Of the {{fact:cross_engine_common_n}} comparable queries,
{{fact:join_heavy_n}} join three or more tables and {{fact:join_light_n}} do not,
and the two groups rank the engines almost identically: per-engine geomean ratios
between the groups run {{fact:join_split_ratio_range}}. Nearly every query in these
suites joins, so a join-heavy split tells you little inside them. **The meaningful
line is between the suites, not within them.**

### The regime map

The tables above are the same seven engines measured on both sides of the shape step.
Read alongside the scale ladders in Part II, they say something more useful than an
order: **each engine is fast in the regime its storage and memory model was built
for**, and the two workload families here sit on opposite sides of that line.
Per-engine detail is in Part IV; the table below gives the shape.

| Engine | What the architecture optimizes for | Where it sits here | What it trades away | Who it suits |
|---|---|---|---|---|
| **ClickHouse** | Sorted MergeTree wide-table scan; replicas add throughput; sorting and pre-aggregation happen on the ingest path | Near the front on ClickBench at a fraction of the suite cost of the engines around it; falls further behind on the star-schema suites as scale grows, and did not finish them at 1 TB | Single-query memory pooling across replicas (by default), and therefore join-graph work at scale | Telemetry, clickstream, event analytics and dashboards over one denormalized table |
| **MotherDuck** | Single-node vectorized execution; no distributed shuffle to pay for; per-second billing | Leads the star-schema suites at every scale measured; fastest ClickBench geomean at its top tier | Elasticity beyond one node: it needs its largest tier at 1 TB | The analyst and the small team, where interactive work still fits on a single node |
| **Redshift** | Mature MPP where distribution and sort keys are the primary tuning surface | Second on the star-schema suites at every scale, an untuned baseline with no DISTKEY or SORTKEY applied; the second-cheapest suite after MotherDuck at 10 GB and 100 GB, and it sheds {{fact:conc_shed_redshift}} of its runs under load | Reaching its ceiling requires physical design that we deliberately did not do | Classic warehousing teams who will invest in that physical design |
| **Snowflake** | Predictable behavior across pattern, scale and skew; operational simplicity | Lowest decision-support suite time at 100 GB and 1 TB, third by geometric mean at 1 TB; on Standard-edition pricing the cheapest suite at 1 TB, and mid-pack on cost below that | Per-query speed: it never leads by geometric mean or per-pattern median | Mixed estates buying consistency and low operational variance |
| **BigQuery** | Fully serverless, slot-based, storage decoupled, per-byte economics | Tied with Databricks for last of six by geometric mean on the star-schema suites at 100 GB (1,638 ms and 1,644 ms), and among the dearest per query; sizing is non-monotonic, its largest reservation slower than a smaller one; third-lowest shed rate in the fleet under load, after Redshift and Fabric | Most penalized of the seven by our no-physical-design decision: an unclustered, unpartitioned table while billing per byte scanned | Teams wanting no infrastructure and a reservation ceiling, who will cluster and partition |
| **Databricks** | Lakehouse over open formats; one platform for SQL and everything that is not SQL | Slowest on the star-schema suites by geometric mean at 1 GB and 1 TB, tied with BigQuery for slowest at 100 GB (1,644 ms and 1,638 ms), second-slowest at 10 GB, and sixth of seven on ClickBench; the dearest per query at 1 GB, 100 GB and 1 TB; under load it finishes every burst from 1 GB to 100 GB bar one query of 201 at 100 GB on 2X-Small, and at 1 TB the unfinished share falls to zero by the Medium tier | SQL-only price-performance against single-purpose SQL engines | Estates whose center of gravity is already the lakehouse |
| **Fabric** | A capacity-unit SaaS estate over OneLake: one SKU dial provisions the whole platform, warehouse compute included | Completes the decision-support suite at every scale, second-cheapest at 1 GB behind MotherDuck and mid-table on cost above it (third of seven at 10 GB, fourth of six at 100 GB and 1 TB); its designated SKU steps with scale, so spend tracks data volume. In this study, capacity size did not change single-query speed below 100 GB, where even F2 absorbed a 100-session burst; at 100 GB and 1 TB it changed both how many simultaneous queries the platform absorbed and how fast the suite ran, as sizing from F2 to F32 halves the 100 GB suite. Last of seven on ClickBench | Spend barely moves single-query latency below 100 GB, and moves it less than suite time even above; raw single-session speed is mid-field at every scale | Microsoft-centric estates sizing for growth and concurrency headroom, at low per-query cost through 10 GB |

It is **not** a ranking: each row records where an engine's architecture placed it
in this data. And these are
**untuned baselines, not ceilings**: no engine here received the physical design its
vendor would recommend. The policy is applied uniformly; its cost is not. It
flatters the engines whose defaults and ingest paths suit our data and penalizes
the ones that expect to be tuned: most of all BigQuery, billing per byte over an
unclustered table, and least the engines that sort on ingest. The treatment is
uniform, the effect is not, and we state the direction where we know it.

### The recommendation

Match the engine to the shape of the workload, and to the budget, before comparing
any latency number.

- **Wide-table scan, filter and aggregate**: telemetry, clickstream, event
  analytics, dashboards over one denormalized table. This is what ClickHouse is
  built for, and its price-performance here is not close: at its fastest tier it
  returns a {{fact:cb_clickhouse_ms}} geomean and runs the whole suite for
  {{fact:cb_clickhouse_suite}}, less than any other engine's fastest-tier pass.
  MotherDuck's top tiers are faster outright, at a higher suite cost. And at the
  cheapest configuration measured for each engine, {{fact:cb_cheapest_tier_duel}}.
- **Star-schema decision support**: a fact table joined to dimensions, which is
  what most BI semantic layers generate. MotherDuck leads at every scale measured
  and Redshift is second at every scale. Here the join graph rather than the scan
  does the work, and by default single-query memory does not pool across ClickHouse replicas:
  its gap widens with scale and at 1 TB it did not finish the suite. The finishing
  order is a statement about fit, not about quality.
- **Both**, which is most real estates. The ranking will not agree with itself
  across them. Weight by which workload carries your latency-sensitive traffic, and
  size each engine to the budget you would approve rather than to its
  fastest tier.

### Reputation versus measurement

Most engine selection happens before anyone runs a query. It happens in the
conversation: analyst notes, migration war stories, the vendors' own benchmarks and
the disputes they trigger (Part I opens with the 2021 episode). That conversation
assigns each engine a reputation, and reputations move slower than products. This
is the one section where we set our measurements beside those reputations, and it
is editorial by construction: the middle column is our reading of the conversation
as it stood in mid-2026, which is an opinion about opinions, and a reader with
different priors should keep them. The right column is measured, and every figure
in it appears elsewhere in this report with its method attached.

| Engine | The reputation | What this data shows |
|---|---|---|
| Snowflake | Premium and easy, priced accordingly | The "easy" half is as advertised: consistent across every pattern, scale and skew profile measured, with no warm failures, fastest by decision-support suite time at 100 GB and 1 TB though never by geometric mean or per-pattern median. The "priced accordingly" half holds at small data but not at large: on Standard-edition rates it is the cheapest suite at 1 TB, and even at Enterprise's 1.5× it stays second |
| ClickHouse | Very fast scans, weak joins | As advertised, and the boundary now has coordinates: a {{fact:cb_clickhouse_ms}} ClickBench geomean at a {{fact:cb_clickhouse_suite}} suite cost, and a DNF on the 1 TB join suites at its largest replica |
| BigQuery | Serverless concurrency champion, sluggish on small queries | As advertised. Among the lowest failure rates under load in the fleet (only Redshift's and Fabric's are lower), above a 100 GB solo floor of 1.6 to 2.1 seconds |
| Redshift | The legacy engine you migrate away from | Not in this data. Second on the decision-support suites at every scale, untuned, with a capacity dial that behaves predictably; the second-cheapest suite at 10 GB and 100 GB, and the lowest shed rate under load in the fleet |
| Databricks | A lakehouse platform that also positions itself as the fastest, cheapest SQL warehouse, per its own benchmark | The lakehouse half is true; the fastest-and-cheapest half is not verified in this data. Slowest on SQL latency by geometric mean at 1 GB, 100 GB and 1 TB, second-slowest at 10 GB, and the dearest per query at 1 GB, 100 GB and 1 TB. Its measured strengths are reliability and platform breadth: zero warm errors, every burst finished under load from 1 GB to 100 GB bar one query of 201 at 100 GB on 2X-Small, while the same governed tables serve everything that is not SQL |
| MotherDuck | A small-data tool that stops working past laptop scale | Not in this data. The fastest decision-support engine at every scale through 1 TB, completing everything, with the steepest queueing curves once sessions pile up on one shared instance, which is the arrangement measured; in production each user gets a separate instance |
| Fabric | Not performance-competitive | Half true. Last of seven on the scan suite. It completes the decision-support suite at every scale and comes fourth by geometric mean throughout, at second-cheapest suite cost at 1 GB, third at 10 GB, and fourth of six at 100 GB and 1 TB |

Two reputations fail outright against measurement, and they fail in opposite
directions: the conversation discounts Redshift about as hard as it caps
MotherDuck, and this data supports neither. A third cracks in one place, Snowflake's
price: on Standard-edition rates it runs the cheapest suite at 1 TB, and even at
Enterprise's 1.5× it stays second behind MotherDuck, so the reputation is right
about the ease and, at large data, wrong about the money, on the rates our role can
read. Before enjoying the upsets, Redshift and MotherDuck carry a share of our own
method, in opposite directions: the no-physical-design policy works against
Redshift, the engine most designed to be tuned with distribution and sort keys, so
a tuned deployment should do better than its untuned second place; and MotherDuck's
lead carries the home-field asymmetry. Both are set out in Part I.

The pattern in the failures is the useful part. Reputations rank engines on
narrative: modern or legacy, momentum or decline, serious or toy. The measured
differences are almost entirely **regime fit**, which is this part's thesis
arrived at from one more direction. The regime map above locates each engine. An
engine picked to match its workload did well here regardless of its
reputation, and the two engines the conversation writes off are the two that beat
their reputations by the widest margin.

## Concurrency: what completes, and how fast

Solo latency is what benchmarks report and what demos show. It is also close to
irrelevant for a system serving a team, because engines diverge under load far more
than they diverge alone.

Start with the most actionable result in this data. **Where an engine sheds part of
a burst, more compute lowers the share it drops.** Databricks at 1 TB shows this
cleanly, because we measured every tier at 100 sessions:

<!-- verify: databricks_1tb_completion_by_tier -->
| Tier | Unfinished at 100 sessions | Median engine time of what completed |
|---|---|---|
| 2X-Small | 73.1% | 195.8 s |
| X-Small | 52.2% | 254.7 s |
| Small | 17.4% | 218.1 s |
| Medium | 0.0% | 138.6 s |
| Large | 0.0% | 65.9 s |

The unfinished share falls with every step up in size, from 73.1% on the smallest
warehouse to zero by Medium, and it stays at zero on Large. Below 1 TB it barely
sheds at all: zero unfinished at 1 GB and 10 GB on every tier, and a single query of
201 at 100 GB.

The unfinished queries here are all our ten-minute timeout firing, and the warehouse
returned no error on them. The median column includes the queue wait, read from the
warehouse's query history, so it is the engine's time on the queries that completed,
and the two columns can be read together: at 2X-Small the warehouse both drops 73.1%
of the burst and takes 195.8 s on the median query it keeps, while Large drops none
and holds the median to 65.9 s.

This is a fixed-allocation result, and the framing matters: every engine's
horizontal scaling is pinned off by protocol (Part II, "Scaling out"), and
Databricks is built to add clusters in response to exactly this kind of burst. What
sheds at 1 TB below Medium is what one fixed warehouse does under a 100-session
burst, and a deployment allowed to add clusters would face less of it.

The rest of this section builds the apparatus that puts that result in context and
locates every engine on it. The obvious presentation is a latency-versus-users curve per configuration;
we have {{fact:concurrency_cells}} of them, and they are unreadable as a set. So we
reduce each to comparable numbers.

**How latency responds to load.** Latency under concurrency follows a power law
closely enough to fit (`L(N) = L(1) × N^k`), so a single exponent describes the
whole curve. `k ≈ 0` means added sessions are nearly free. `k ≈ 1` means pure
queueing: every added user costs everyone proportional waiting. Above 1 means
contention.

**Whether the engine keeps its promises.** The share of queries that fail at the
highest load measured. This is *independent* of the exponent, and reporting latency
without it is actively misleading.

That second axis is the finding. Consider two engines at 100 sessions: one holds
median latency perfectly flat, the other doubles it. On a latency chart the first
looks better. But if the first left **a third of the queries unfinished inside our
budget**, its median describes only the rest; the second finished everything, slower.

Two cautions apply. Unfinished queries are not all one kind, and the grid says which
kind each cell's are. Most are our ten-minute timeout firing: all of Databricks',
BigQuery's, Snowflake's, Redshift's and Fabric's. ClickHouse's are almost all the engine refusing a query
that would exceed its memory limit. MotherDuck's are mostly timeouts, with some
out-of-memory and internal errors. A timeout shows that the engine did not finish in ten minutes, not
whether it queued the query or never accepted it. Rows our own harness failed, a
query sent into a session it had already lost, are left out of every figure. And the
distinction matters less than it appears: the metric a buyer needs is **how many
sessions get an answer at an interactive latency**, which is why useful concurrency is
judged against a five-second median rather than against the ten-minute timeout. At ten
minutes the configuration is already 120× outside interactive range, however the engine
got there.

**The two signatures suit different uses.** A configuration whose latency stays flat
while part of a burst goes unfinished protects the latency of what it completes; one
whose latency climbs while everything finishes protects completion at everyone's
expense. A latency chart alone cannot tell them apart, which is why the unfinished
share is published beside every latency.

Across the grid, {{fact:concurrency_mode_mix}}.

### What we are not claiming

Our workers draw from a shared queue, so as it empties the offered load falls away
from the requested level. Every exponent here is therefore a **lower bound** on
degradation under genuinely sustained load, and every shed rate is measured under a
burst rather than a steady state. Real capacity is worse than these numbers,
uniformly.

We also report no throughput and no queries-per-second. The apparatus measures
per-query latency under an N-session burst; it does not measure sustained capacity,
and we will not convert one into the other.

Every figure in this section is measured engine behavior. Databricks, Redshift and BigQuery
report execution time only, so on all three the queue wait is read from each engine's
job history at reconcile and added back, and every concurrency cell is published,
latency and failure rate alike.
