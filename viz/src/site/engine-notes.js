/**
 * What the site says about each engine in words, as opposed to numbers.
 *
 * Three kinds of text live here, and each says where it came from:
 *
 *  - `about`, `pickWhen`, `watchOut`, `startAt`: the buyer's guide's engine
 *    profile (Table 7 and the per-engine sections), condensed and held to the
 *    shipped data. Pages label them "Buying notes" rather than quoting the
 *    guide, and put the live numbers beside them so a reader can check one
 *    against the other. Figures are always computed on the page.
 *  - `idle`: how the engine bills hours when nobody is querying, from each
 *    vendor's public documentation as of September 2026 (the guide's Table 4).
 *    This is not measured by the benchmark and the pages say so. `pays` and
 *    `quiet` are shown on several pages, so they describe the vendor's billing
 *    and say nothing about any one page. `billsIdle` describes the engine as
 *    the study ran it (ClickHouse always on, BigQuery on reservations with a
 *    fixed number of slots), and `fitMonth` is the one
 *    sentence Find your warehouse (fit.js) adds to its hover where its monthly figure
 *    counts every hour.
 *  - `howWeRan`: the study's own setup choices for this engine, the ones a
 *    reader needs before comparing its numbers with another engine's.
 */

/** Date the idle-billing rules were read from vendor documentation. */
export const IDLE_RULES_AS_OF = 'September 2026';

/** @type {Record<string, {
 *   about: string, pickWhen: string, watchOut: string, startAt: string,
 *   unit: string, idle: {pays: string, quiet: string, billsIdle: boolean, fitMonth?: string},
 *   howWeRan: string[]
 * }>} */
export const ENGINE_NOTES = {
  snowflake: {
    about: 'A cloud data warehouse sold as virtual warehouses in T-shirt sizes, from X-Small up. The study ran Gen2 Standard warehouses, priced at Standard edition list rates.',
    pickWhen: 'The workload is mixed and unpredictable, and consistency matters more than leading on any single measure.',
    watchOut: 'Rarely the fastest on any single measure, and a size that is good value at 1 TB is wasted money at 100 GB.',
    startAt: 'X-Small for up to 10 people at once at 100 GB (Small if single-query speed matters); X-Large for 50 people at once at 100 GB.',
    unit: 'credits',
    idle: {
      pays: 'Compute by the second while a warehouse is running, with a 60-second minimum each time it starts.',
      quiet: 'The meter stops between queries, but only once the warehouse auto-suspends after its configured idle period.',
      billsIdle: false
    },
    howWeRan: [
      'Gen2 Standard warehouses, one cluster. Adding clusters under load (multi-cluster warehouses) requires Enterprise edition, which lists at a higher credit price than the Standard edition priced here.',
      'Ran the join suites on the same translated SQL as every other engine.',
      'No clustering keys; tables as loaded.'
    ]
  },
  databricks: {
    about: 'A lakehouse platform whose serverless SQL warehouses serve the same governed tables that its pipelines and machine learning use. The study ran serverless SQL warehouses from 2X-Small to Large on Azure.',
    pickWhen: 'The data warehouse is one use case on a lakehouse you already run, or need to run at scale.',
    watchOut: 'At 1 TB, the smaller sizes could not keep up with 100 sessions: 73% of the queries on 2X-Small, 52% on X-Small and 17% on Small did not finish due to timeouts after 10 minutes, while Medium and Large finished all queries within 10 minutes. Up to 100 GB every size finished almost every query. The platform adds clusters under load in production; this study held it to one.',
    startAt: 'X-Small at 100 GB for a few people at once; Medium for about 20. Add clusters to serve more.',
    unit: 'DBUs',
    idle: {
      pays: 'Serverless compute while the warehouse is running, including idle minutes before it stops.',
      quiet: 'The meter stops once the warehouse auto-stops, after ten idle minutes by default.',
      billsIdle: false
    },
    howWeRan: [
      'A serverless SQL warehouse pinned to one cluster per run. Autoscaling across clusters, the feature built for exactly these bursts, was off.',
      'Tables as loaded; Predictive Optimization left at its default.'
    ]
  },
  bigquery: {
    underLoad: 'Enterprise Edition slot reservations of a fixed size, from 100 to 1,000 slots, with autoscaling off, so each result is what one reservation size served.',
    about: 'Google\'s serverless warehouse. Compute is bought as a number of slots, so there is no cluster to manage. The study ran Enterprise Edition reservations from 100 to 1,000 slots, plus on-demand pricing at the smaller volumes.',
    pickWhen: 'Many people query at once, you need response times to hold steady as more of them arrive, and you run reserved slots.',
    watchOut: 'More slots barely speed up a single query: at 100 GB the typical query took about a second and a half at every reservation measured, so it does not lead when only a few people are querying.',
    startAt: '200 slots at 100 GB; 800 or more at 1 TB.',
    unit: 'slot-hours',
    idle: {
      pays: 'Reserved slots, billed every second whether or not they are used. Slots added automatically under load are billed per second, with a one-minute minimum by default. The study priced the reserved slots for the time its queries were running.',
      quiet: 'A reservation with a fixed number of slots, the kind this study used, bills for those slots around the clock, even while nobody is querying. A reservation can instead start from zero slots and add them automatically as queries arrive (in multiples of 50 slots); it then bills only while it is scaled up, from the first query until at least a minute after the last. The study did not measure that setup.',
      fitMonth: 'The monthly estimate on this page counts all 730 hours of the month for BigQuery, because that is how this study ran it. With a reservation that starts from zero slots, it would count only your busy hours.',
      billsIdle: true
    },
    howWeRan: [
      'Slot reservations, so BigQuery has an hourly price comparable with the other engines. On-demand pricing (billed by bytes scanned) was measured at 1 GB, 10 GB and 100 GB. It is BigQuery\'s designated configuration at 1 GB and 10 GB and its entry tier on the scan queries, so the cross-engine single-user comparisons there use it; the size charts leave it out.',
      'No partitioning or clustering. This may penalize BigQuery more than any other engine: on-demand queries read every row of each column they used, and were billed for all of it.'
    ]
  },
  redshift: {
    about: 'Amazon\'s warehouse. The study ran Redshift Serverless, sized in Redshift Processing Units (RPUs) from 8 to 128, always pinned to its base capacity.',
    pickWhen: 'You are AWS-native, the primary workload is join-heavy reporting, and queries arrive steadily enough to share the capacity each of them is billed for.',
    watchOut: 'The full base capacity is billed, with a 60-second minimum, whenever any query is running, so a large base is expensive for small, scattered queries; the largest sizes are slower at small data volumes; skewed data slowed it at 1 TB.',
    startAt: '8 RPU up to 10 GB; 16 RPU at 100 GB; 32 RPU or more at 1 TB.',
    unit: 'RPU-hours',
    idle: {
      pays: 'The full base capacity you selected, by the second with a 60-second minimum, whenever at least one query is running.',
      quiet: 'Nothing. The meter stops when no query is running.',
      billsIdle: false
    },
    howWeRan: [
      'Serverless, base capacity pinned (maximum equal to base), so each run measured one fixed size. By default, Redshift Serverless scales above the base (AI-driven scaling is on by default) and bills for the capacity in use.',
      'No distribution keys or sort keys. Redshift is the engine most designed to be tuned with them, so a tuned deployment should do better than these results.'
    ]
  },
  fabric: {
    about: 'Microsoft\'s analytics platform. The study ran its Data Warehouse, a columnar warehouse over OneLake, on capacities from F2 to F128.',
    pickWhen: 'You already run on Microsoft and need a capacity that absorbs bursts of many simultaneous users, more than you need the fastest single query.',
    watchOut: 'Fabric lets a burst use more compute than the capacity provides and spreads the excess over the following minutes or hours, which Microsoft calls smoothing. If the excess grows past what the capacity can repay, Fabric throttles new work until it is paid down; Microsoft\'s capacity overage option can instead bill the excess at three times the pay-as-you-go rate, which the study did not measure. The study saw this once: after 100 people queried F8 at 100 GB, the capacity was moved down to F2, and F2 refused every connection for 25 minutes. In the concurrency grid, Fabric finished nearly every query: 19 of its 30,449 runs did not finish (0.06%), tied with Redshift (0.05%) for the lowest share of the seven engines, and all of them were ten-minute timeouts on F32 at 1 TB. On scan queries over one wide table it was last of the seven engines. Two things can mislead a test: a new warehouse the study created came with result-set caching switched on, and a capacity is cold after a pause and resume (the first 50-session burst at 100 GB after a resume took a median of 53.5 seconds, against 3.7 seconds warm).',
    startAt: 'F16.',
    unit: 'capacity units',
    idle: {
      pays: 'A capacity, in capacity units, billed every second it is running. Bursts above its allowance are smoothed into later time, and that smoothing cannot be switched off.',
      quiet: 'The capacity bills whether or not anyone is querying; it can be paused by hand, and pausing bills any smoothed usage still outstanding.',
      fitMonth: 'The monthly estimate on this page counts all 730 hours of the month for Fabric, because a capacity keeps billing until someone pauses it.',
      billsIdle: true
    },
    howWeRan: [
      'Data Warehouse component, queried in T-SQL over ODBC.',
      'Capacity smoothing and throttling left at the platform\'s behavior; queries that failed count as not completed.'
    ]
  },
  clickhouse: {
    about: 'A column store built for fast scans over one wide, sorted table. The study ran ClickHouse Cloud with replicas from 8 GB to 120 GB of memory each.',
    pickWhen: 'The work is scan queries over one wide table: events, telemetry, product analytics.',
    watchOut: 'By default, replicas do not pool memory for a single query, so large joins fail where one replica\'s memory runs out. At 1 TB it left about a third of the TPC-DS runs unfinished, and DSB was not run.',
    startAt: 'One replica with 64 GB for one team; for many people running scan queries, three 48 GB replicas, which ran all 43 scan queries at once.',
    unit: 'compute units',
    idle: {
      pays: 'Replicas by the minute while they run.',
      quiet: 'A service left running all the time, the way this study ran it, bills through quiet hours. On every tier, a service can instead pause itself after a set time without queries (at least five minutes), which stops compute billing; the first query after a pause then waits while the service starts again. The study did not measure that delay.',
      fitMonth: 'The monthly estimate on this page counts all 730 hours of the month for ClickHouse, because that is how this study ran it. With automatic pausing turned on, it would count only your busy hours.',
      billsIdle: true
    },
    howWeRan: [
      'Single-user pass on one replica; concurrency grid on three replicas, the vendor\'s default configuration. Every other engine ran one unit under load.',
      'External-memory (spill to disk) settings left at their defaults. A deployment tuned for joins would finish more of the join queries than these results show.'
    ]
  },
  motherduck: {
    about: 'A cloud service built on DuckDB, a single-machine analytics database. Each user\'s queries run on a dedicated DuckDB instance, a "Duckling", sized Pulse, Standard, Jumbo, Mega or Giga.',
    pickWhen: 'A small team wants the fastest interactive analytics measured, at a low daily cost.',
    watchOut: 'Response times climbed faster than any other engine\'s as sessions piled onto one instance, and 1 TB needs its largest size. The queries were written in DuckDB SQL and checked against DuckDB, so MotherDuck alone ran them untranslated; the study cannot measure that advantage.',
    startAt: 'Standard up to 10 GB; Jumbo at 100 GB; Giga at 1 TB.',
    unit: 'Duckling-hours',
    idle: {
      pays: 'Standard and larger instances by the second while they run (one-minute minimum), plus a cooldown after the last query of one to ten minutes by default, depending on size.',
      quiet: 'The instance shuts down after the cooldown and stops billing.',
      billsIdle: false
    },
    howWeRan: [
      'Every session ran under one account, so all of them shared one Duckling. In production, different users get separate instances, and a service account can add read-only replicas; neither arrangement was measured. Read MotherDuck\'s concurrency figures as one instance under load.',
      'Giga is priced at $24.00 an hour, the rate MotherDuck published after the runs (it was $36.00 during them).'
    ]
  }
};
