<!-- anchor: before-decision-support -->

# Part II: Sizing

Two questions this part answers: **how much compute should you buy**, and **how many
people will that serve**. They are usually asked separately and they have one answer,
because the second is what the first is for.

Everything here holds the compute *static*. That is a deliberate control and the most
important thing to understand about these numbers: see "Scaling out" at the end of
this part for what it buys you and what it deliberately leaves unmeasured.

## Growing the data is cheaper than you would guess

Take each engine at the compute tier it was designated for each scale, and multiply
the data by ten:

<!-- verify: scaling_at_designated_tier -->
| Engine | SF1 | SF10 | SF100 | SF1000 | 10× data costs… |
|---|---|---|---|---|---|
| Snowflake | 80.5 s | 97.4 s | 165.7 s | 467.0 s | 1.2× → 1.7× → 2.8× |
| Databricks | 211.3 s | 245.8 s | 420.7 s | 1014.6 s | 1.2× → 1.7× → 2.4× |
| Redshift | 19.0 s | 45.3 s | 193.4 s | 884.2 s | 2.4× → 4.3× → 4.6× |
| Fabric | 68.7 s | 176.6 s | 356.6 s | 913.4 s | 2.6× → 2.0× → 2.6× |

**Read this table for what it is.** Each engine climbs its own tier ladder as the
data grows, so every multiplier above combines *more data* with *more compute*. It
measures the designated configuration at each scale (the size this study assigned
to each engine for that data volume, starting small and stepping up as the data
grows), and it is emphatically **not** a
fixed-hardware measurement. The Databricks 1.2× on the first step, for instance,
spans X-Small at 1 GB to Small at 10 GB: a warehouse change as well as a data change.

With that said, the result is real and it is the most reassuring one in the report:
**stepping up the designated sizes, ten times the data costs between 1.2× and
4.6× the time, never ten.** The multiplier is smallest at the low end, where fixed
overheads dominate and the data is nearly free, and climbs as the data starts to
matter.

Hold the hardware fixed instead and the picture changes: on a single tier, ten times
the data *can* cost more than ten times the time. So the finding is that **the sizing ladder is doing real work** rather than that
query time is independent of data volume.
An engine kept on one tier as its data grows tenfold will not enjoy this curve.

The spread between engines is the actionable part. Snowflake and Databricks hold a
shallow curve: under 3× per decade. Redshift's steepens faster, to 4.6× on the last
step.

**A note on the ladders, because they are not the same shape.** Each engine steps
through the designated sizes this study assigned it, in its vendor's own size units
(vendors do not publish recommended sizes by data volume): Snowflake
X-Small to Small to Medium to Large, Databricks the same progression, Redshift
8 to 8 to 16 to 64 RPU, Fabric F2 to F8 to F32 to F128. Snowflake and Databricks
double at every step, Fabric quadruples at each, and Redshift holds its smallest tier
across the two smallest scales before stepping up.

We use this designated ladder rather than forcing a uniform doubling, for two
reasons. It starts small and steps up as the data grows, the way an operator sizing
to their data would, which is the question the table is meant to answer. And our
own data says the flat low end
is *correct* rather than an oversight: at 1 GB, Redshift's 128 RPU is slower than its
16 RPU on 188 of 201 queries. Spending more at small data does not buy speed on this
engine, so a ladder that spent anyway would measure a configuration nobody should
choose.

The consequence to keep in mind: because the ladders differ in shape, this table
compares each engine against *its own* designated ladder, not engines against each other.

*(Three engines are omitted, each because its designated ladder cannot answer the
question this table asks. **MotherDuck**: its tier changes twice (Standard to Jumbo
between 10 and 100 GB, then Jumbo to Giga between 100 GB and 1 TB), so two of three
steps would measure a hardware change rather than a data change. **BigQuery**: its
designated compute changes *billing model*, not just size (on-demand at 1 and 10 GB,
a slot reservation above), and the resulting step from 10 to 100 GB reads as 0.9×, faster on
ten times the data, which is an artifact of that switch rather than a scaling result.
**ClickHouse**: it completes 69 of the 201 decision-support queries at 1 TB, so its
totals at the two largest scales would be sums over different query sets. All three
appear in the fixed-tier tables below, where those problems do not arise.)*

## Doubling the compute does not halve the time

This is the question the sizing dropdown poses, and on nearly every engine whose
compute we swept at fixed data the answer is **no, and the shortfall is
large**. Snowflake, Redshift and BigQuery are shown here; Databricks and
Fabric reproduce the shape in their Part IV profiles. MotherDuck
at 1 TB, treated separately below, is one exception: its tier jumps are large
enough to pay for themselves. ClickHouse is the other, and only at its smallest
step: the first doubling from 1x8GB to 1x16GB returns 2.23× at 1 GB, 3.17× at 10 GB
and 2.23× at 100 GB, because the 8 GB rung is memory-starved rather than because the
step scales cleanly.

Snowflake at SF1000, across five warehouse tiers, each step doubling the compute:

<!-- verify: snowflake_sf1000_ladder -->
| Tier | Suite time | Speedup vs X-Small | Gain from this doubling |
|---|---|---|---|
| X-Small | 2822.1 | 1.00× | — |
| Small | 1341.9 | 2.10× | 2.10× |
| Medium | 698.1 | 4.04× | 1.92× |
| Large | 467.0 | 6.04× | 1.49× |
| X-Large | 369.6 | 7.64× | 1.26× |

Sixteen times the hardware buys **{{fact:sf1000_full_ladder_speedup}}**. The first
doubling exceeds the ideal at {{fact:sf1000_best_single_gain}}; every step after
it returns less than the one before, down to 1.26× at the top.

*All five rows cover all 201 decision-support queries: every tier completes the full
suite at 1 TB, so no tier's total is a sum over a survivor subset.*

Redshift tells the same story in its own units (8 to 128 RPU at SF100 is sixteen
times the capacity for **4.20×**), and BigQuery likewise: at SF1000, going from 200
to 800 slots is four times the capacity for **{{fact:bq_sf1000_200_800_speedup}}**.

Anyone who has met Amdahl's law will recognize the diminishing-returns shape: on
Snowflake the implied serial fraction is roughly 7% at 1 TB. Amdahl assumes fixed
work; we hold the query set fixed across tiers to make the fit meaningful, but a tier
change can also alter the plan, so treat it as indicative. What the sweep adds is the
magnitude, per engine, at each scale, and a second effect that matters more: the
return on compute depends on the data volume.

## The return on compute depends on whether your data is big enough to use it

The same Snowflake ladder, at SF100 instead of SF1000:

<!-- verify: snowflake_speedup_by_scale -->
| Tier | SF100 speedup | SF1000 speedup |
|---|---|---|
| X-Small | 1.00× | 1.00× |
| Small | 1.33× | 2.10× |
| Medium | 1.68× | 4.04× |
| Large | 1.90× | 6.04× |
| X-Large | 1.93× | 7.64× |

The hardware ladder is identical. At 1 TB it returns {{fact:sf1000_full_ladder_speedup}}; at 100 GB, 1.93×. **The
difference is not the engine, it is the amount of work available to spread**, and
the same comparison reproduces on five other engines. We did not
capture per-node or per-slot usage, so we can say the speedup fails to
materialize at 100 GB but not that we watched the capacity sit idle.

The practical form of this: *on this workload, at these scales, scaling up answers
data volume rather than per-query slowness.* It is a single-session result and does
not transfer to the concurrency axis, where added capacity does add headroom. If queries are slow on a small warehouse over a small dataset, a bigger
warehouse is close to the least effective remedy available: the ceiling is roughly
2× no matter how much you spend.

Redshift makes the point more starkly, because at SF1 it inverts outright:

<!-- verify: redshift_sf1_inversion -->
| Redshift @ SF1 | Suite time | Cost |
|---|---|---|
| 16 RPU | 15.2 | $0.03 |
| 32 RPU | 15.9 | $0.05 |
| 64 RPU | 18.2 | $0.12 |
| 128 RPU | 23.4 | $0.31 |

**128 RPU is {{fact:redshift_overprovision_pct}} slower than 16 RPU and {{fact:redshift_overprovision_price}} the price**, and it is not a suite-level
artifact: the larger cluster is slower on **188 of the 201 queries individually**, by
a median of 1.72×. We did not capture step-level execution detail, so we can report
that the larger cluster loses without asserting why. Over-provisioning here is
actively counterproductive, and it is the easiest mistake to
make, because the dropdown makes it effortless.

## Where scaling up is nearly free, and where it is not

Speed and price move together, so the useful quantity is what a unit of speedup
costs. Take the ratio of cost to speedup: **1.00 means the extra speed was
proportionally priced; below 1.00 means it was a bargain; above means you overpaid.**

Snowflake, same ladder, both scales:

<!-- verify: snowflake_cost_per_speedup -->
| Tier | SF100 cost-per-speedup | SF1000 cost-per-speedup |
|---|---|---|
| X-Small | 1.00 | 1.00 |
| Small | 1.12 | 0.46 |
| Medium | 1.40 | 0.24 |
| Large | 2.23 | 0.22 |
| X-Large | 4.35 | 0.28 |

**The sign flips.** At SF100, every step up costs more per unit of speed: X-Large
buys 1.93× the speed for {{fact:sf100_xlarge_money}} the money, a cost-per-speedup
of {{fact:sf100_xlarge_cost_per_speedup}}. At SF1000, every tier above X-Small costs
less per unit of speed, from 0.46 at Small to a low of 0.22 at Large before rising
to 0.28 at X-Large: Large delivers {{fact:sf1000_large_speedup}} the speed for
{{fact:sf1000_large_money}} the money, so its speed is roughly
{{fact:sf1000_large_price_per_speed}} the price of X-Small's.

The arithmetic behind it: a warehouse bills per unit time, so the cost of a step up
is the rate increase discounted by the time saved. At 1 TB the time saved is large
enough to outrun the rate; at 100 GB it is not. Only the first doubling exceeds the
ideal (the largest single gain is {{fact:sf1000_best_single_gain}}, and every step
after it returns less), so the effect comes from the *accumulated* speedup across the
ladder outpacing the accumulated rate rather than from most steps paying for
themselves.

This has a direct operational reading, and it is the most useful finding in this
part:

> **At small data, scaling up costs money and buys little. At large data, speed gets
> cheaper as you scale up to Large, and occasionally the bigger tier is cheaper
> outright. The right warehouse size is a function of your data volume, not your
> patience.**

To be precise about "cheaper": at 1 TB the bill is not monotonic in tier size. Small
($2.05) and Medium ($2.11) run cheaper outright than X-Small's
{{fact:sf1000_xsmall_cost}}, and only Large ({{fact:sf1000_large_cost}}) and X-Large
($4.62) cost more. Large still buys {{fact:sf1000_large_speedup}} the speed for that
{{fact:sf1000_large_money}} spend. What falls across the ladder is the price *per unit
of speed*, not always the bill.

MotherDuck shows the strongest form of the same effect. At SF1000, moving from Jumbo
to Giga is **8.70× faster for 0.58× the cost**: faster *and* cheaper, a strictly
dominant configuration. Both tiers completed all 201 queries, so this is not a
survivor effect. On this suite Jumbo is dominated on both axes.

Redshift's SF100 ladder finds its optimum in the middle: 16 RPU delivers
1.83× the speed of 8 RPU for 1.09× the cost, and the price per unit of speed bottoms
at 32 RPU before each step past it costs more than it returns.

BigQuery, priced on the reservation it holds, is no exception. Across 200 to 1000
slots at SF1000, its cost per suite climbs from $5.06 to $14.00 while suite time
falls from 1,471 s to 773 s: 1.90× the speed for 2.77× the cost. A larger reservation
usually costs more and finishes sooner, though at the top the order breaks: 800 slots
finishes sooner and costs less than 1000 slots.

The consumed-slot price, which charges for slot-time used rather than for the
baseline held, stays nearly flat across that range, moving under 10%. That price
understates a fixed reservation, which pays for its slots whether or not queries fill
them, so we price the held baseline instead. The consumed-slot figure is kept on every
row for readers who run BigQuery pay-per-use.

Separately, BigQuery's solo latency has a floor: at 100 GB its median at one session
sits between 1.6 and 2.1 s across reservation sizes, well above the fastest engines
here, which is why it does not lead at low concurrency.

## How many users one fixed capacity serves

This is where the volume of measurement pays off. Solo latency is one number per
configuration; the concurrency ladder run at every scale is a surface, and it answers
the question a buyer has: **how many people can use this, on this hardware,
with this much data?**

Useful concurrency, the most simultaneous sessions a configuration serves while
holding median latency under five seconds and its failure rate low. A session count
is the width of one burst (Part I): N sessions starting together on one suite's 99
or 102 queries, about one query each at 100, which is how many people can be
answered when they arrive at once, not how many can query all day. Latency here is
the engine's time on a query, its queue wait included and the network excluded:

<!-- verify: useful_concurrency -->
| Engine | Tier | 1 GB | 10 GB | 100 GB | 1 TB |
|---|---|---|---|---|---|
| BigQuery | ENTERPRISE / 100 slots | 100+ | 20 | 5 | — |
| BigQuery | ENTERPRISE / 1000 slots | 100+ | 100+ | 50 | 5 |
| BigQuery | ENTERPRISE / 200 slots | 100+ | 50 | 10 | 1 |
| BigQuery | ENTERPRISE / 400 slots | 100+ | 100+ | 20 | 1 |
| BigQuery | ENTERPRISE / 500 slots | 100+ | 100+ | 20 | 1 |
| BigQuery | ENTERPRISE / 600 slots | 100+ | 100+ | 20 | 1 |
| BigQuery | ENTERPRISE / 800 slots | 100+ | 100+ | 50 | 5 |
| ClickHouse | 3x120GB | 100+ | 50 | 5 | — |
| ClickHouse | 3x16GB | 100+ | 5 | 0 | — |
| ClickHouse | 3x24GB | 100+ | 10 | 0 | — |
| ClickHouse | 3x32GB | 100+ | 10 | 0 | — |
| ClickHouse | 3x48GB | 100+ | 20 | 0 | — |
| ClickHouse | 3x64GB | 100+ | 20 | 0 | — |
| ClickHouse | 3x8GB | 50 | 0 | 0 | — |
| ClickHouse | 3x96GB | 100+ | 50 | 1 | — |
| Databricks | 2X-Small | 10 | 10 | 1 | 0 |
| Databricks | Large | 50 | 50 | 20 | 1 |
| Databricks | Medium | 50 | 50 | 20 | 1 |
| Databricks | Small | 20 | 20 | 10 | 0 |
| Databricks | X-Small | 20 | 10 | 5 | 0 |
| Fabric | F128 | 100+ | 100+ | 50 | 1 |
| Fabric | F16 | 100+ | 100+ | 20 | — |
| Fabric | F2 | 100+ | 100+ | 10 | — |
| Fabric | F32 | 100+ | 100+ | 100+ | 1 |
| Fabric | F4 | 100+ | 100+ | 20 | — |
| Fabric | F64 | 100+ | 100+ | 100+ | 1 |
| Fabric | F8 | 100+ | 100+ | 5 | — |
| MotherDuck | Giga | 100+ | 100+ | 100+ | 10 |
| MotherDuck | Jumbo | 100+ | 100+ | 10 | 0 |
| MotherDuck | Mega | 100+ | 100+ | 20 | 0 |
| MotherDuck | Pulse | 10 | 5 | 0 | — |
| MotherDuck | Standard | 100+ | 20 | 10 | — |
| Redshift | 128 RPU | 100+ | 100+ | 100+ | 10 |
| Redshift | 16 RPU | 100+ | 100+ | 10 | 0 |
| Redshift | 32 RPU | 100+ | 100+ | 20 | 1 |
| Redshift | 64 RPU | 100+ | 100+ | 50 | 1 |
| Redshift | 8 RPU | 20 | 20 | 5 | 0 |
| Snowflake | Large | 100+ | 100+ | 20 | 5 |
| Snowflake | Medium | 100+ | 100+ | 20 | 1 |
| Snowflake | Small | 100+ | 50 | 20 | 1 |
| Snowflake | X-Large | 100+ | 100+ | 50 | 10 |
| Snowflake | X-Small | 100+ | 50 | 10 | 0 |

*A `+` marks a **censored** ceiling: the configuration served that many sessions at the highest level we asked of it, so the number is a lower bound rather than a limit: {{fact:conc_cells_censored}} of {{fact:conc_cells_total}} cells are censored, and comparing a censored 50 against an uncensored 100 would read as half the capacity when it may only mean we never tried 100 there. `—` marks a cell with no value: at 1 TB these are mostly deliberate skips of a size too small for the scale, and in the exponent table `—` also marks a curve with too few completed levels to fit. `*` would mark a withheld cell, measured but with its latency not published; none is withheld in this data. Databricks, Redshift and BigQuery report execution time only, so each needs its queue wait added back before the number is the engine's full time on a query. All three take it from their job or query history, back-filled through `benchmark reconcile`, so every cell carries it. At 1 TB, Redshift's 8 RPU and 16 RPU served no sessions: the timeout rule stopped them after 5 and 10 sessions. A cell whose failures our own client caused would be withheld the same way. Failure rates are published for every measured cell.*

*One pinned shape in this grid is not like the others. ClickHouse's rows are
three-replica services, the vendor's default configuration, rather than the
single-unit discipline every other engine got. The pin held (replica
count and per-replica memory fixed throughout, and the organization's billing
confirms the three-replica hardware was real), but three replicas share a burst
that every other engine absorbed with one warehouse, one cluster or one
reservation, so read cross-engine comparisons against ClickHouse's rows with that
shape attached. Its single-session comparisons are unaffected: those admit only
single-replica configurations by policy.*

Each row reads as a capacity plan. Snowflake at X-Large carries a hundred
sessions through 10 GB, fifty at 100 GB and ten at 1 TB. Databricks at Large holds
fifty through 10 GB and twenty at 100 GB, then one at 1 TB. MotherDuck Standard
carries a hundred at 1 GB and ten at 100 GB: the same hardware, the same queries, a
different answer at every scale.

**The universal finding: concurrency gets harder as data grows, on every engine.**
The queueing exponent (how steeply latency rises with sessions) climbs with scale
in 37 of the 41 rows that span more than one scale:

<!-- verify: queueing_exponent -->
| Engine | Tier | 1 GB | 10 GB | 100 GB | 1 TB |
|---|---|---|---|---|---|
| BigQuery | ENTERPRISE / 100 slots | 0.41 | 0.63 | 0.85 | — |
| BigQuery | ENTERPRISE / 1000 slots | 0.03 | 0.13 | 0.29 | 0.66 |
| BigQuery | ENTERPRISE / 200 slots | 0.15 | 0.45 | 0.62 | 0.98 |
| BigQuery | ENTERPRISE / 400 slots | 0.14 | 0.26 | 0.47 | 0.83 |
| BigQuery | ENTERPRISE / 500 slots | 0.05 | 0.19 | 0.46 | 0.77 |
| BigQuery | ENTERPRISE / 600 slots | 0.11 | 0.20 | 0.40 | 0.75 |
| BigQuery | ENTERPRISE / 800 slots | 0.04 | 0.16 | 0.34 | 0.69 |
| ClickHouse | 3x120GB | 0.33 | 0.57 | 0.58 | — |
| ClickHouse | 3x16GB | 0.71 | 0.55 | — | — |
| ClickHouse | 3x24GB | 0.53 | 0.69 | — | — |
| ClickHouse | 3x32GB | 0.48 | 0.73 | — | — |
| ClickHouse | 3x48GB | 0.49 | 0.71 | — | — |
| ClickHouse | 3x64GB | 0.45 | 0.65 | — | — |
| ClickHouse | 3x8GB | 0.81 | — | — | — |
| ClickHouse | 3x96GB | 0.42 | 0.60 | 0.61 | — |
| Databricks | 2X-Small | 0.64 | 0.59 | 0.91 | 1.07 |
| Databricks | Large | 0.39 | 0.39 | 0.49 | 0.73 |
| Databricks | Medium | 0.41 | 0.39 | 0.49 | 0.82 |
| Databricks | Small | 0.64 | 0.54 | 0.54 | 0.97 |
| Databricks | X-Small | 0.68 | 0.61 | 0.68 | 1.06 |
| Fabric | F128 | 0.44 | 0.27 | 0.30 | 0.86 |
| Fabric | F16 | 0.41 | 0.39 | 0.67 | — |
| Fabric | F2 | 0.41 | 0.39 | 0.96 | — |
| Fabric | F32 | 0.37 | 0.27 | 0.29 | 1.01 |
| Fabric | F4 | 0.39 | 0.47 | 0.79 | — |
| Fabric | F64 | 0.39 | 0.24 | 0.22 | 1.02 |
| Fabric | F8 | 0.43 | 0.32 | 0.86 | — |
| MotherDuck | Giga | 0.23 | 0.58 | 0.74 | 0.79 |
| MotherDuck | Jumbo | 0.44 | 0.86 | 1.15 | — |
| MotherDuck | Mega | 0.42 | 0.75 | 0.89 | 0.25 |
| MotherDuck | Pulse | 1.51 | 1.43 | — | — |
| MotherDuck | Standard | 0.57 | 1.10 | 1.17 | — |
| Redshift | 128 RPU | 0.69 | 0.67 | 0.67 | 0.93 |
| Redshift | 16 RPU | 0.40 | 0.67 | 0.90 | 0.78 |
| Redshift | 32 RPU | 0.49 | 0.58 | 0.76 | 0.86 |
| Redshift | 64 RPU | 0.53 | 0.60 | 0.75 | 0.97 |
| Redshift | 8 RPU | 1.14 | 0.80 | 0.69 | — |
| Snowflake | Large | 0.44 | 0.49 | 0.68 | 0.75 |
| Snowflake | Medium | 0.40 | 0.53 | 0.66 | 0.77 |
| Snowflake | Small | 0.43 | 0.53 | 0.70 | 0.82 |
| Snowflake | X-Large | 0.31 | 0.41 | 0.57 | 0.64 |
| Snowflake | X-Small | 0.44 | 0.56 | 0.75 | 0.84 |

Nothing else in this report holds so uniformly. Whatever the engine, adding data
makes adding users more expensive, because larger queries hold resources longer, so
the same session count contends harder. A capacity plan built at 1 GB will not
survive contact with 1 TB, and the error compounds in the direction of optimism.

**BigQuery is the clearest case of a sizing dial behaving as it should.** At 100 GB
its exponent falls strictly as slots are added (0.85, 0.62, 0.47, 0.46, 0.40, 0.34,
0.29 from 100 to 1000 slots), so buying capacity buys concurrency headroom in
proportion. Most of that slope is BigQuery's own query queue: at 10 GB the 200-slot
exponent is 0.45 but only 0.25 on execution time alone, and the job history puts the
rest in the wait between a job's creation and its start. At 1 GB the larger
reservations are flat (0.03 to 0.05 at 500, 800 and 1000 slots), because there is not
enough work to contend over, while 200 to 600 slots already queue a little (0.11 to
0.15) and the 100-slot reservation reaches 0.41.

The Databricks and Redshift rows include their queue time, and most of them climb
with scale like the rest. Databricks steepens as data
grows and flattens as the warehouse grows: at 1 TB its two smallest tiers pass 1.0
(2X-Small 1.07, X-Small 1.06), where latency rises faster than the session count,
while Large holds 0.73. Redshift's middle bands do the same from 1 GB to 100 GB
(16 RPU 0.40, 0.67, 0.90), and its largest band stays nearly flat across that range
(128 RPU 0.69, 0.67, 0.67). One band runs the other way: 8 RPU starts as
Redshift's steepest band at 1 GB (1.14) and eases to 0.69 by 100 GB. At 1 TB
the bands Redshift could fit sit between 0.78 and 0.97 (16 RPU 0.78, fit on the 1 to
10 session levels it reached; 32 RPU 0.86; 64 RPU 0.97; 128 RPU 0.93), so the largest
band's flat curve ends at 1 TB, and 8 RPU completed too few levels to fit.

**The exponent is not a score.** MotherDuck runs from 0.23 to 1.51: it degrades
steeply, yet single-user it finishes every query. Under load it sheds 3.70% overall,
the second-highest rate after ClickHouse, with its smaller tiers at 1 TB and Pulse
shedding heavily. An engine can also post a flat exponent by completing only part of
a burst, because the queries that did not finish are not in the median: MotherDuck's
Mega at 1 TB posts a flat 0.25 because 70% of its burst went unfinished at 100
sessions. That is why every exponent here is read beside the share of the burst that
did not complete.

## The number to size against

Neither axis alone tells a buyer what they want to know, and the exponent in
isolation is a trap. **MotherDuck has among the steepest exponents we measured (it
queues hard), and its Giga tier still reaches the top concurrency result published
at 100 GB**, because it starts fast enough that even steep degradation stays under a
useful threshold. A flat curve that starts above the target is the opposite case: it
never crosses the line, however gently it rises.

So the headline metric is **useful concurrency**: the most
simultaneous sessions a configuration serves while keeping median latency under a
stated target and its failure rate low. A query that does not complete is not served. A level whose
median blows past the target serves nobody usefully. It is one number, and it folds in
starting latency, degradation and shedding together.

At 100 GB against a five-second target, that number ranges from 100 sessions down to
effectively zero across the configurations measured, a spread far wider than any
solo-latency table suggests.

## Scaling out: why we held capacity still

Nearly every engine here can add capacity **horizontally** as well as vertically.
Snowflake can run a warehouse as multiple clusters (on Enterprise edition, which lists
at a higher credit price than the Standard edition priced here); Databricks SQL warehouses can
load-balance across clusters; Redshift can add capacity above its baseline; BigQuery
reservations can autoscale beyond their slot floor; ClickHouse Cloud adds replicas;
MotherDuck offers read scaling. Fabric is the odd one out: a capacity SKU is a fixed
capacity-unit budget with built-in burst smoothing that cannot be turned off, so for
Fabric the pin is the SKU and the bursting is part of the product under test.

**We disabled all of it, and that decision is what makes the measurements in this part
mean anything.** The reasoning is worth setting out, because "we turned off a feature
the vendor provides" reads like a limitation and is the opposite.

### An autoscaling engine has no independent variable

The experiment in this part is: hold capacity fixed, vary the load, observe what
happens. If the engine may add capacity *in response to* the load, then raising
concurrency changes two things at once (the demand and the supply serving it), and no
observed latency can be attributed to either. There is no controlled variable left, so
the experiment supports no conclusion.

It invalidates the central analysis outright: the
degradation exponent that describes each latency curve assumes fixed capacity by
construction. Fitted against an autoscaling engine, it measures how aggressively
that vendor's controller adds paid capacity under load rather than how the
engine handles contention. Two engines with identical query execution and
different scaling thresholds would produce different exponents, and we would be
ranking controllers while claiming to rank engines.

### The ceilings are the findings, and autoscaling hides them

Because capacity was fixed, this report's most useful results exist at all.
ClickHouse's per-replica memory limit at 1 TB, Redshift's over-provisioning inversion
at small data, the split between engines whose latency climbs under load and engines
that leave part of a burst unfinished: every one
of those is a boundary that only becomes visible when the engine cannot spend its way
past it. An engine allowed to scale out shows you only its
willingness to add nodes, and the interesting behavior disappears into the elasticity.

A benchmark that lets every engine autoscale mostly reports that cloud warehouses can
be made faster by buying more of them. That is true, uninteresting, and not what
anyone needs to know.

### A per-unit number composes; an autoscaled one does not

Because capacity is fixed, every figure here is a **per-unit** result: what one stated
quantity of compute does with a given data volume and a given number of users. That is
the form a reader can use. If one unit serves N sessions at your data volume
and your engine runs M units, you have a basis for reasoning about the whole system,
and you can apply *your* scaling policy, cooldowns and cost structure to it, which will
not be ours.

An autoscaled figure cannot be decomposed that way. It entangles the engine's execution
with the controller's policy and with our specific load pattern, and none of those
transfer. You would be told what happened once, to us.

**What we pinned, verifiable in the configuration we publish:**

| Engine | Horizontal mechanism | The pin |
|---|---|---|
| Snowflake | Multi-cluster warehouse | Left at the single-cluster default; no multi-cluster settings applied |
| Databricks | Multi-cluster load balancing | `min_num_clusters = max_num_clusters = 1`, re-asserted on every resize |
| Redshift | Capacity above baseline | `max_capacity = base_capacity` |
| BigQuery | Reservation autoscaling | No autoscale on the reservation, plus `ignore_idle_slots = true` so a query cannot borrow beyond its slot floor |
| ClickHouse | Replica scaling | Replica count and per-replica memory pinned out of band, min = max |
| MotherDuck | Read scaling | Not configured; one instance tier throughout |
| Fabric | Capacity bursting / smoothing | Capacity SKU pinned per run (F2–F128); burst smoothing is inherent to the SKU, cannot be disabled, and is measured as part of the product |

### What this does not tell you

We are explicit here because these are real questions, and none are answered in
this report: how
gracefully each engine scales out, how quickly its controller reacts, what the added
capacity costs, and whether the per-unit figure holds as units are added (it may not,
since coordination overhead grows). **Where a configuration hits a ceiling in this
report, that is the ceiling of that fixed capacity, not of the product.** Every engine
here can be made to go further than we show; what we report is what each unit
of it does.

## The shape, summarized

Three regularities hold across every engine measured:

1. **Data scales sublinearly, on the designated ladder.** 10× the data costs 1.2× to
   4.6× the time on the ladders shown, and {{fact:scaling_step_all_engines_range}}
   across all engines measured, never 10×.
2. **Compute scales sublinearly too, and its return rises with data volume.** At
   100 GB and 1 TB, 16× the hardware returns 1.93× to {{fact:sf1000_full_ladder_speedup}}; at 1 GB and 10 GB the
   same span returns 0.80× to 1.54×, often a slowdown. Per-step gains usually shrink,
   though not always monotonically.
3. **Therefore price-performance inverts with scale.** Scaling up is expensive per
   unit of speed at small data and cheap at large data. At small enough data,
   the largest tier can be slower *and* several times more expensive than a middling
   one.

All three are properties of the
surface rather than of any vendor, and each holds in the same direction on every
engine where we have the tiers to test it, which is what makes them worth planning
around. Coverage differs by engine, so the number of engines behind each of the three
varies; the coverage matrix says which.
