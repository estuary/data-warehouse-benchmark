"""Editorial engine verdicts for the report's Decision Guide section.

Each verdict states in words what the measured data shows for one engine:
qualitative, hand-maintained, and drawn from the results. Keep the text
number-light: the Decision Guide section pairs each verdict with a stats line
computed live from the results DB, so numbers stay current without editing
this file. Engines present here but absent from the DB render as "campaign
pending"; engines in the DB but missing here render with stats only and a
placeholder asking for a verdict.

Update a verdict when new data changes what it says.

**Framing rule.** Each engine is described by the workloads it suits and its
operating envelope: where it performs well, and where its results show it
reaching its limits. The report's thesis is that performance is a *function* of
workload, size and concurrency, so naming the regime an engine suits is more
useful to a buyer than a single ranking.

This is a rule about **prose, not data**. Completion rates, per-level error
rates and the coverage matrix are published unchanged and at full prominence,
whatever the prose says; the verdicts never soften or omit them. The report
keeps its own criticism for its method, where a reader needs it to judge the
numbers.
"""

from __future__ import annotations

# Per-engine editorial verdicts. Keys:
#   pick_when  — the one-line punchline: the situation this engine is the answer to
#   strengths  — what it demonstrably does well, across ability x latency x
#                concurrency x cost, plus the value it provides (integration,
#                flexibility, scalability, predictability, operational simplicity)
#   best_fit   — the operating envelope: the workload regime and scale band where
#                these strengths hold, and where they stop.
VERDICTS: dict[str, dict[str, str]] = {
    "motherduck": {
        "pick_when": (
            "a small team wants the fastest interactive analytics measured, at "
            "pennies per workday, and works query-by-query rather than under "
            "shared load."
        ),
        "strengths": (
            "The solo leader: fastest on the decision-support suites at every "
            "scale measured, fastest on most query patterns (Redshift takes "
            "windows and text search), with a clean no-DNF sweep all the way "
            "through SF1000. A full analyst workday costs pennies. DuckDB "
            "semantics mean local development and cloud execution are the same "
            "engine, which removes an entire class of dev/prod divergence, and "
            "the Duckling tiers scale cleanly with data size."
        ),
        "best_fit": (
            "Built for the analyst, not the audience. Its execution model gives "
            "a single session the whole machine, which is exactly why solo "
            "latency and cost are unmatched, and why the regime it targets is "
            "interactive and exploratory work rather than a shared serving tier "
            "behind a dashboard fleet. Size the Duckling to the data, and the "
            "economics hold from SF1 to SF1000."
        ),
    },
    "redshift": {
        "pick_when": (
            "you are AWS-native, the workload is join-heavy BI, and you have "
            "enough sustained load to keep an RPU baseline busy."
        ),
        "strengths": (
            "The window-function and text-search specialist (fastest in the "
            "fleet on both patterns), and second on the decision-support "
            "suites at every scale, with under 1% of concurrency runs errored "
            "across its whole grid. The RPU dial behaves like a capacity "
            "control should: cost moves monotonically with it, and latency "
            "responds predictably, downward where the data can use the "
            "capacity and measurably upward where it cannot (the 1 GB inversion), "
            "so right-sizing is an exercise rather than a guess. Deep AWS "
            "integration means IAM, VPC and S3 are already solved."
        ),
        "best_fit": (
            "A provisioned-capacity engine that rewards steady use. Because "
            "the full RPU base bills, with a 60-second minimum, whenever any "
            "query is running, whether or not queries saturate it, its "
            "economics are strongest for sustained workloads that keep the "
            "warehouse busy. The measured per-query figure here is a "
            "capacity allocation, not a marginal price, so it improves as "
            "the capacity is kept busier. Small tiers are genuinely sufficient at small "
            "scale: 8 RPU is close to 128 RPU solo at SF1."
        ),
    },
    "snowflake": {
        "pick_when": (
            "the workload is mixed and unpredictable and consistency matters "
            "more than topping any single dimension."
        ),
        "strengths": (
            "The pattern-agnostic workhorse: consistently strong on "
            "essentially every query type, with no warm failures at any scale "
            "or skew profile measured. That consistency is the product: the "
            "same predictable behavior whether the workload is uniform or "
            "skewed, small or 1 TB, one user or fifty. At the 1 TB / "
            "100-session extreme it sheds a large share of the burst (as the "
            "other latency-protecting engines do there), and its "
            "useful concurrency at 1 TB still ties for the best measured. "
            "Warehouse tiers are a clean latency dial once the data is large "
            "enough to use them (100 GB and up), and "
            "the operational surface is about as simple as this category gets."
        ),
        "best_fit": (
            "The engine to choose when you cannot characterize the workload in "
            "advance. Its breadth is the point: it is rarely the single fastest "
            "on any one pattern, and it is never the one that falls over when "
            "the workload shifts. Tier inversions under load are real and "
            "useful: the best-value tier solo is not the best-value tier at "
            "fifty sessions, so size to the concurrency you expect."
        ),
    },
    "bigquery": {
        "pick_when": ("hundreds of users hit it at once and you run reserved slots."),
        "strengths": (
            "The concurrency and chaos absorber: the flattest latency "
            "degradation under 100-way concurrency in the fleet, with **among the "
            "lowest error rates under load** (only Redshift and Fabric leave fewer "
            "queries unfinished), and the most "
            "skew-insensitive optimizer here: DSB's skewed distributions "
            "barely move it. Adding slots buys faster answers and more "
            "concurrency headroom, at a higher cost per query. Fully "
            "serverless, with no cluster to manage."
        ),
        "best_fit": (
            "A capacity-elastic engine for shared, bursty, many-user serving. "
            "Slot scheduling adds a floor to trivial queries, which is the "
            "trade for the elasticity, so it is aimed at concurrent "
            "fleets rather than single-analyst latency races. Reserved slots "
            "are the regime measured here; on-demand byte pricing is a "
            "different economic model that suits ad-hoc exploration rather "
            "than repeated analytics."
        ),
    },
    "clickhouse": {
        "pick_when": (
            "the workload is high-QPS scan, filter and aggregate over wide "
            "tables — telemetry, web analytics, dashboards."
        ),
        "strengths": (
            "Sustained-dashboard economics nobody else here matches: "
            "sub-second single-table analytics at the lowest per-query cost in "
            "the fleet for its target workload, holding up under heavy "
            "concurrent load on its vendor-default three-replica shape. On the scan-and-aggregate work it is designed "
            "for it is in a class of its own, and the replica-configuration "
            "space gives unusually fine-grained control over the "
            "cost/throughput trade."
        ),
        "best_fit": (
            "A wide-table analytics engine: replicas add throughput but by default do not pool "
            "memory for a single query, so the envelope is set by per-replica memory rather "
            "than by cluster size. That makes it outstanding for many "
            "concurrent scan-shaped queries, and points large join-heavy "
            "decision-support work at 1 TB toward a different tool or toward "
            "external-memory settings not exercised at 1 TB. Match the replica "
            "memory to the largest single query, not to total data volume."
        ),
    },
    "databricks": {
        "pick_when": (
            "the warehouse is one surface on a lakehouse you already run for engineering and ML."
        ),
        "strengths": (
            "Consistent latency that holds its shape across the full "
            "scale range, SF1 to SF1000, with zero errors across the warm "
            "campaign and a clean, monotonic response to warehouse sizing from "
            "100 GB up. Its "
            "real value is integration rather than any single latency number: "
            "the same governed tables serve SQL analytics, pipelines and ML "
            "without a separate copy, and serverless warehouses make the "
            "compute side close to zero-ops."
        ),
        "best_fit": (
            "The engine to pick when SQL analytics is one workload among "
            "several on shared data, and the value of a single platform "
            "outweighs winning a query-latency benchmark. Sizing behaves "
            "predictably enough to right-size against a concurrency target; "
            "the regime it is built for is a mixed lakehouse estate rather "
            "than a dedicated low-latency serving tier."
        ),
    },
    "fabric": {
        "pick_when": (
            "you are Microsoft-centric, sizing for data growth and burst "
            "concurrency rather than for single-query speed."
        ),
        "strengths": (
            "Completes the decision-support suite at every scale, at the "
            "second-lowest suite cost measured at 1 GB (after MotherDuck) and "
            "near the cheapest at every scale after (the designated SKU steps "
            "with scale, so spend tracks data volume). In this study, capacity "
            "size mainly changed how many simultaneous queries the platform "
            "absorbed: up to 10 GB a 64× range of compute left single-query "
            "latency essentially unchanged, and at 100 GB F32 was the smallest "
            "SKU to serve 100 sessions."
        ),
        "best_fit": (
            "A capacity-unit platform for estates that live in OneLake, sized "
            "to the burst it must absorb rather than to a latency target; "
            "below 100 GB, single-query speed does not respond measurably to "
            "capacity spend. The envelope is decision support at interactive "
            "volumes: wide-table scan analytics is not its regime (last of "
            "seven on ClickBench), and burst headroom is "
            "workload-relative: at 100 GB, F32 is the smallest SKU to clear "
            "100 useful sessions."
        ),
    },
}
