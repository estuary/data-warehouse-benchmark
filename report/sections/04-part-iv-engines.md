<!-- anchor: before-methodology -->

# Part IV: The engines

Seven engines, each described by what it does well and the regime it is built for.
There is no overall ranking here and none is coming: Part III showed that the order
changes when the workload changes and changes again when you fix the budget instead
of the tier, so a single ordering would be an artifact of whichever slice we chose.

Every engine below is a serious system built for a particular regime. What
differs is *where*.

Numbers are from the generated sections that follow, at each engine's designated
tier unless stated. Completion rates matter as much as latency and are reported
alongside it: an engine that did not finish a query has told you something.

The profiles are grouped by the regime each engine is built for, because that is the
axis on which they differ: who is querying, how many of them at once, and
what shape the work takes.

## One analyst at a time

### MotherDuck

**Strongest at:** single-analyst interactive work, at almost any scale.

It is the fastest engine measured on the decision-support suites for one user at a
time, first at every scale by geometric mean of per-query latency (each engine at its
designated tier), and it holds that through 1 TB with every query completing. On ClickBench its top tier posts the fastest geometric mean on the
board. The DuckDB lineage is a real
operational advantage rather than a detail: local development and cloud execution
are the same engine, which removes an entire class of works-on-my-laptop divergence.
The same lineage is also this benchmark's one home-field asymmetry: our queries
are authored in DuckDB SQL and our oracle is DuckDB, so MotherDuck alone runs them
untranslated (Part I, "Where the SQL differs"); read its decision-support lead with
that unmeasured advantage attached.

**The regime:** its execution model gives a single session the machine. That is
exactly why it leads on solo latency at every scale by geometric mean and runs the
cheapest suite up to 100 GB, and it is also why it queues harder than anything else
here as sessions pile up. At 1 TB the lead narrows: Snowflake's Large warehouse runs
the decision-support suite for less money and in less total suite time, $2.86 and
467.0 s against Giga's $3.37 and 502.0 s. Its degradation exponents are
among the steepest we measured. Remarkably, it *still* matches the top useful-concurrency
result at 100 GB (tied with Redshift's 128 RPU band and Fabric's F32 and F64) because
it starts fast enough that steep degradation stays under a five-second target. All of
our test sessions ran under one account and so shared one instance; in production
each user gets a separate instance, and a service account serving a dashboard can
add read-only replicas. Neither arrangement was measured, so read its concurrency
figures as one instance under load. It is built for the analyst rather than the audience, but the audience
has to be larger than intuition suggests before it stops working.

**Sizing note:** the Duckling tier matters enormously at scale and barely at all
below 100 GB, Pulse aside. Giga runs 1.4× faster than Standard at 1 GB and 1.7× faster
at 10 GB, while Pulse runs 9.6× slower than Standard at 10 GB (317.5 s against 33.2 s)
and sheds 39% of a 100-session burst. At 1 TB, moving Jumbo to Giga is 8.70×
faster for 0.58× the cost (4,369 s to 502 s, $5.85 to $3.37): faster *and* cheaper,
with both tiers completing every query. There is no reason to run Jumbo on a terabyte.

## Provisioned capacity for decision support

### Redshift

**Strongest at:** star-schema decision support, and predictable capacity planning.

It is second only to MotherDuck in the decision-support comparison, by geometric mean
at every scale measured, and sheds under 1% of its runs across the whole concurrency
ladder. At 1 TB its 128 RPU band serves 10 concurrent sessions under the five-second
target, tied with Snowflake's X-Large and MotherDuck's Giga for the best in the fleet,
while its designated 64 RPU band serves one. Priced, like every engine here, on the
time it holds its compute, it also runs the second-cheapest suite in the fleet at
10 GB and 100 GB, after MotherDuck. The RPU dial behaves the
way a capacity control should: cost moves monotonically with it, and latency
responds predictably: downward where the data can use the capacity, measurably
upward where it cannot (the 1 GB inversion below), which makes right-sizing an
exercise rather than a guess. AWS-native integration means IAM, VPC and S3 are
already solved if you are already there.

**The regime:** a provisioned-capacity engine that rewards steady use. The priced
figure here is the RPU rate for the time its queries held the workgroup, a
near-marginal price that does not drop as you fill idle capacity. What rewards a busy
warehouse is the bill: the serverless base capacity is charged in full, with a
60-second minimum, whenever any query is running, whether or not queries saturate
it, so capacity a running query leaves unused is money already committed, and the
invoice runs above the priced figure (Part V, "What we cannot price, and why"). Nothing is
billed while no query runs. We pinned maximum capacity to the base; by default
Redshift Serverless scales above the base and bills for the capacity in use. It has the clearest
over-provisioning penalty in the data: at 1 GB, 128 RPU is slower than 16 RPU on 188
of 201 queries and costs {{fact:redshift_overprovision_price}} more. Size to the
data, and resist the dropdown.

### Snowflake

**Strongest at:** workloads you cannot characterize in advance.

It is consistent across every query type, scale and skew profile measured, with no warm
failures at any scale. Under load it sheds under 1.5% of runs across its whole
concurrency grid, all of it at 1 TB and most at 100 sessions (the hardest corner of
the grid), where its useful concurrency still ties for the best in the fleet. That
consistency is the product. By designated-tier suite time it posts the fastest
decision-support pass at 100 GB and 1 TB; by geometric mean it sits third at 1 TB,
behind MotherDuck and Redshift. It is never the one that falls over when
the workload shifts, which is a genuine and underrated property when the workload
is a moving target.

**The regime:** breadth over peak, and cheaper than the reputation suggests. On
Standard-edition rates, Snowflake runs the cheapest suite at 1 TB of any engine here;
the account's edition is not readable by our role, and at Enterprise's 1.5× every
figure rises but 1 TB Large still sits second, behind MotherDuck. At 100 GB and 1 TB warehouse tiers
are a clean latency dial; at 1 and 10 GB the dial is flat, with X-Large the slowest
tier there (101 s and 120 s). Cost is not monotonic in size at 1 TB, where Small and
Medium both run the suite for less than X-Small. The price-per-unit-of-speed *improves*
as you scale up to Large at 1 TB: Large delivers {{fact:sf1000_large_speedup}} X-Small's
speed for {{fact:sf1000_large_money}} its cost, and the trade worsens at X-Large: 7.64×
the speed for 2.16× the cost. At 100 GB the best-value tier shifts with load
rather than holding at one size: X-Small for one user, X-Large at 100 sessions. At 1
and 10 GB X-Small dominates every level, and at 1 TB the solo value tier is Medium.
Size to the concurrency you expect. The concurrency figures above are measured across the full 1
GB-to-1 TB ladder at every warehouse tier.

## Shared serving at fleet concurrency

### BigQuery

**Strongest at:** many users at once, with no cluster to manage.

**Among the lowest failure rates in the fleet**: {{fact:conc_shed_bigquery}} of its
concurrency runs did not complete across its whole grid, every one our ten-minute
timeout at 1 TB. Only Redshift's {{fact:conc_shed_redshift}} and Fabric's {{fact:conc_shed_fabric}} are lower, against
{{fact:conc_shed_worst}} at the other end, and Databricks left roughly
{{fact:conc_shed_databricks_vs_bigquery}} as large a share unfinished, nearly all of
it at 1 TB on its three smallest warehouses. BigQuery keeps taking queries under load
and completes them. At 1 GB it serves the full 100-session ladder at all seven reservation sizes, and at
10 GB at all but the two smallest (100 slots holds 20, 200 slots holds 50). Its latency
under load includes its own query queue: its reported time runs from a job's start,
so the wait between a job's creation and its start is added back from its job
history. That wait is small at the median (well under a tenth of a second) and large
in the tail at 1 TB, where the slowest one in a hundred waited four minutes. It is
also the most distribution-insensitive engine measured: on the 74 query pairs that
isolate distribution from cardinality, its median DSB/TPC-DS ratio is
{{fact:skew_bigquery_clean_ratio}}, the closest to 1.00 of a fleet spanning
{{fact:skew_clean_ratio_range}} (Part III). It is fully serverless, with sizing that
is a number rather than a cluster.

**The regime:** capacity-elastic serving for concurrent fleets. Slot scheduling puts
a floor under trivial queries (its median at one session rises with scale, from
0.41-0.48 s at 1 GB to 1.6-2.1 s at 100 GB and 2.3-3.6 s at 1 TB, well above the
fastest engines here), and that floor is the trade for the elasticity. It is aimed at many users sharing capacity rather
than at one analyst racing a stopwatch.

**Sizing note:** priced on the reservation it holds, BigQuery costs more at larger
sizes. Across a 5× range of reservation sizes at 1 TB, on the queries every size
completed, suite cost rises from $5.06 at 200 slots to $14.00 at 1,000 while suite
time falls from 1,471 s to 773 s, 1.90× the speed for 2.77× the cost. The 800-slot
reservation finishes sooner and costs less than 1,000. The consumed-slot price, which
charges only for slot-time used and moves under 10% across that range (from $4.15 to
$4.54), is kept on every row for pay-per-use readers.

### Databricks

**Strongest at:** SQL as one workload among several on shared data.

By designated-tier geometric mean it is the slowest engine at 1 GB (929 ms) and 1 TB
(3,417 ms), tied with BigQuery for slowest at 100 GB (1,644 ms and 1,638 ms), and
second-slowest at 10 GB, ahead only of BigQuery on-demand, with zero errors across the
warm campaign. Its response to warehouse
sizing is monotonic at 100 GB and 1 TB, and flat at 1 and 10 GB, where Small is the
fastest tier. Its real value is integration rather than any single latency number: the
same governed tables serve SQL, pipelines and ML without a second copy, and
serverless warehouses make the compute side close to zero-ops. Predictive
Optimization is enabled by default, so the platform maintains layout and statistics
without being asked, which is worth knowing when comparing against engines left untuned.

**The regime:** a lakehouse estate where the warehouse is one surface, and the
priciest decision-support suite in the fleet at every scale but 10 GB: $0.23 at 1 GB,
$1.99 at 100 GB and $7.99 at 1 TB. At 10 GB the most expensive suite is BigQuery
on-demand's, $1.18 against Databricks' $0.56. Under load, counting the queue wait
from its query history, its median at 100 sessions runs 18.4 s at 1 GB on
X-Small, 12.6 s at 10 GB on Small, 15.2 s at 100 GB on Medium and 65.9 s at 1 TB on
Large. At each of those tiers the whole burst finished.

At 100 sessions the burst completes wherever the warehouse is sized to the data.
At 1 GB, 10 GB and 100 GB every warehouse size finishes all 201 queries, one lone
exception aside, and at 1 TB the unfinished share falls from 73.1% on the smallest
warehouse to zero at Medium and Large. More compute lowers the share it drops. The
unfinished queries are all our ten-minute timeout firing, with no error from the
warehouse; queries lost to a client-side network fault are ours, not the warehouse's,
and are excluded (Part V, "Known gaps"). What remains at 1 TB below Medium is one
fixed warehouse under a 100-session burst: **a
configuration that cannot answer inside ten minutes under 100 sessions cannot serve
100 interactive users one query each**, so size to the data or let the warehouse add
clusters.

## Wide-table scan analytics

### ClickHouse

**Strongest at:** wide-table scan, filter and aggregate. Its price-performance
there is the standout result in this report.

On ClickBench it returns a {{fact:cb_clickhouse_ms}} geometric mean and runs the
whole suite for {{fact:cb_clickhouse_suite}}, the lowest of any engine's fastest-tier
pass. For comparison, BigQuery returns {{fact:cb_bigquery_ms}} for
{{fact:cb_bigquery_suite}} and Databricks {{fact:cb_databricks_ms}} for
{{fact:cb_databricks_suite}}; every engine faster than ClickHouse runs the suite for
at least {{fact:cb_price_multiple_min}} its cost, and per 1,000 queries ClickHouse is
the cheapest here as well (Part III). Per-replica memory moves this workload too, in
the direction the sizing dropdown promises: the warm suite runs roughly 3× faster at
120 GB per replica than at the three smallest rungs, whose own ordering sits inside
provisioning noise (Part III and Part V carry the detail). If your workload is
telemetry, clickstream or dashboards over one denormalized table, ClickHouse is the
cheapest place to run it: its fastest tier's pass costs the least of any engine's, and
its 1x8GB service runs the cheapest pass measured on the board, $0.0049. Two engines
run the scan suite faster: MotherDuck's Giga tier, at 2.1× the speed for 1.7× the
cost, and Redshift's 128 RPU band.

**The regime:** replicas add
throughput and availability, but by default they do not pool memory for a single
query, so the envelope is set by
per-replica memory rather than cluster size, which is also why single-session
comparisons admit only its single-replica configurations (Part III). That makes it
outstanding for many concurrent scan-shaped queries (its concurrency was measured
on three-replica services, the vendor's default configuration; Part II names the
shape asymmetry). It is also why star-schema
decision support strains it at scale: at 100 GB the designated 1x32GB service fails
about 20% of each suite's warm pass (80 of 99 TPC-DS, 82 of 102 DSB), and only the
larger 1x120GB rung clears the report's 10% completion floor (96 of 99 and 97 of 102).
At 1 TB it **did not finish** the workload, completing {{fact:clickhouse_sf1000_completion}}
queries, treated as DNF rather than ranked on the surviving subset. Of the queries
it attempted at 1 TB, {{fact:clickhouse_sf1000_failure_mix}}. Each figure counts every
query that failed on at least one run, six of which completed on another. The rest of
the shortfall is DSB, most of
which was deliberately not attempted there once TPC-DS had established the ejection
profile (a recorded skip, counted as not finished by the policy above). The memory
and timeout failures are real
results about fit rather than quality (this is a wide-table engine being asked to
do joins), and the harness failures are ours, not ClickHouse's.

**One thing we did not measure** bounds what we can say: at 1 TB, the
external-memory settings that would let large joins spill to disk were left at
defaults and never exercised. (The 100 GB single-replica memory ladder ran with
spill to disk enabled and still failed where one replica's memory ran out, which is
why we attribute the join failures to the memory of a single replica.) A ClickHouse deployment tuned for the join workload would do better
than we report, as would every other engine tuned for its own, but ClickHouse has
the most headroom we left on the table.

## Capacity sized to growth and burst

### Microsoft Fabric

**Strongest at:** completing everything, and low cost at the small end. It finishes
the decision-support suite in full at every scale. On cost it is second-cheapest at
1 GB ($0.0074, behind MotherDuck's $0.0068), third at 10 GB (behind MotherDuck and
Redshift), and fourth of six at 100 GB ($0.67, 1.31x Snowflake's $0.51) and at 1 TB
($7.04, behind Snowflake, MotherDuck and Redshift, though below BigQuery and
Databricks). Its designated SKU steps up with the scale, so suite cost multiplies by
roughly ten per decade of data on the designated ladder: spend tracks data volume,
and what a bigger SKU buys shifts with it (next).

**The regime:** in this study, Fabric's capacity size mainly changed how many
simultaneous queries the platform absorbed, and below 100 GB it did not change how
fast a single query ran, which is the cleanest result in the report. Across the full
seven-rung ladder (F2 to F128, a **64x range of capacity units**), the warm suite runs
from 68.7 s at F2 to 71.7 s at F128 at 1 GB and from 174.5 s to 170.2 s at 10 GB,
every rung within **6.0% and 6.8%** of the fastest. Median query latency is likewise
flat: 0.25 s to 0.24 s at 1 GB, 0.45 s at both ends at 10 GB. Single-session latency,
measured independently as the first rung of the concurrency ladder, spans 0.24 to
0.32 s at 1 GB and 0.45 to 0.57 s at 10 GB, flat across the ladder.

At 100 GB and 1 TB the dial starts to buy speed. Across the 100 GB ladder the warm
suite falls from 714.0 s at F2 to 356.6 s at F32 before F64 and F128 give some back
(379.4 s and 390.6 s), so F16 is the value rung: 1.76x F2's speed for 1.03x its cost.
At 1 TB, stepping F32 to F64 to F128 runs the suite in 1,403.3 s, 1,042.9 s and
886.7 s, 1.58x faster end to end for 1.47x the cost. Per-query latency moves less than
the suite does: the 100 GB geomean falls from 1,137 ms at F2 to 960 ms at F32, and the
1 TB geomean from 2,435 ms at F32 to 2,185 ms at F128.

What moves is how many concurrent sessions the capacity absorbs, and where. At 1 and
10 GB every SKU from F2 through F128 serves 100+ sessions and finishes every burst
whole, holding a 100-session median of 1.7 to 2.0 s at 1 GB and 1.8 to 4.7 s at 10 GB.
At 100 GB the SKU starts to matter. Useful concurrency sits between 5 and 20 sessions
on F2 through F16, jumps to 100+ on F32 and F64, and is 50 on F128. Every SKU finished
its burst. The 100-session median at 100 GB falls from 142.2 s on F2 to 4.8 s on F32
and 3.8 s on F64.

Under load the capacity smooths a burst against the SKU's allowance: a burst draws
compute units well past the SKU's baseline, with the excess spread across later time,
which is how a small SKU serves a 100-session burst at all. Below 1 TB every Fabric
burst finished whole; at 1 TB, F32's decision-support bursts lost 5 queries at 50
sessions and 14 at 100 to the ten-minute timeout. Those 19 are the only unfinished
Fabric queries in the grid. That ties Fabric with Redshift for the fewest unfinished
queries under load ({{fact:conc_shed_fabric}} against {{fact:conc_shed_redshift}}),
with BigQuery next at {{fact:conc_shed_bigquery}}. The same smoothing that absorbs a burst loosens the link
between a query and a bill, so Fabric's per-query cost is a capacity-window price
split by CPU share rather than a metered per-query charge (Part V, "What we cannot
price").

The useful-concurrency threshold belongs to the workload, not to the engine, and it
moves with scale. At 1 and 10 GB there is no threshold: every SKU from F2 through F128
serves 100+. At 100 GB one appears at F32, where useful concurrency climbs from 5 to
20 sessions on the smaller SKUs to 100+ on F32 and F64. The threshold is headroom
*relative to the workload*, so the sizing question
for Fabric is not "how fast do I need this to be" but "how much burst do I need to
absorb, at what volume". Below 100 GB, capacity bought for latency alone is wasted; at
100 GB, stepping to F16 or F32 both absorbs more burst and roughly halves the suite,
for 1.03x and 1.28x F2's cost.

On ClickBench it is last of the seven engines at {{fact:cb_fabric_ms}}, and the
scan suite repeats the profile's central result: across the full F2-to-F128
ladder the geomean moves less than a fifth end to end (1,514 ms to 1,390 ms), with the
rungs above F8 within 6.7% of each other, so the capacity dial buys no scan speed and
F16 is the rung to run it on. F16 is both the fastest pass (1,380 ms) and a cheap one;
F64 runs 6.7% slower for 3.59x the cost, and the cheapest pass is F4 at $0.0719 and
1,533 ms. What a bigger SKU buys on scans is what it buys everywhere here, burst
absorption: under concurrency F32 and F64 serve 100+ sessions while F16 and F128 hold
to 20, and every ClickBench burst finished whole. Read alongside ClickHouse (a
{{fact:cb_clickhouse_ms}} geomean for {{fact:cb_clickhouse_suite}} against Fabric's
{{fact:cb_fabric_suite}}), wide-table scan analytics is not what this engine is for.

**Two things we did not measure**, each bounding what we can say. The bottom of
its ladder at 1 TB: there the warm and concurrency sweeps cover F32, F64 and F128,
with F2 through F16 recorded as too small for the scale (they would have spent the
ladder waiting on ten-minute timeouts). And cold-start behavior after a capacity
resume, which we deliberately waited out rather than measured.

## What is not here

**Cold-start behavior, for any engine.** This is a warm, steady-state study. See
Part I.
