"""Build DSB query set in DuckDB dialect.

DSB shares 99 query templates with TPC-DS (re-run against skewed data) plus 3
DSB-specific templates (100, 101, 102) authored by Microsoft Gray Systems
Lab. The 3 specific templates live in microsoft/dsb under
query_templates_pg/agg_queries/query{100,101,102}.tpl with `define` directives
for parameter substitution.

This script:
1. Copies queries/duckdb/tpcds/query_NN.sql -> queries/duckdb/dsb/query_NN.sql
   for NN in 01..99. The SQL text is identical; what differs at runtime is
   the underlying data — TPC-DS uses uniform distributions, DSB uses skewed
   ones (generated via the real microsoft/dsb binary at SF1 in
   `data/dsb/sf1/`).
2. Writes hand-instantiated DuckDB-dialect SQL for queries 100, 101, 102.

The hand-instantiated values for queries 100-102 pick valid domain values from
TPC-DS SF1 (categories, marital_status, education, buy_potential, ca_state)
and reasonable numeric ranges. They are concrete, deterministic, and
intentionally fixed; the full DSB workload generator (dsqgen) would randomize
these per stream.

Spec deviation note: the upstream dsqgen binary builds cleanly on GCC 11 with
`-fcommon` but exhibits template-parser failures (substitution lookup order
issues in the bundled postgres.tpl driver, plus a separate syntax error in
query templates). Rather than patch the upstream parser, this script reuses the
canonical TPC-DS query text for queries 1-99 and the hand-instantiated SQL
above for 100-102, run against DSB-skewed data so the result hashes diverge
from the TPC-DS ground truth (which is the desired DSB signal). DSB-tuned
parameter values for the 37 templates DSB chose to customize are NOT applied.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

logger = logging.getLogger("benchmark.scripts.build_dsb_queries")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TPCDS_DIR = REPO_ROOT / "queries" / "duckdb" / "tpcds"
DSB_DIR = REPO_ROOT / "queries" / "duckdb" / "dsb"


DSB_Q100 = """\
-- DSB Query 100
-- Two products sold together frequently. Self-joins on store_sales,
-- PK/FK joins. DSB-specific addition to TPC-DS.
SELECT item1.i_item_sk, item2.i_item_sk, count(*) AS cnt
FROM item AS item1,
     item AS item2,
     store_sales AS s1,
     store_sales AS s2,
     date_dim,
     customer,
     customer_address,
     customer_demographics
WHERE item1.i_item_sk < item2.i_item_sk
  AND s1.ss_ticket_number = s2.ss_ticket_number
  AND s1.ss_item_sk = item1.i_item_sk
  AND s2.ss_item_sk = item2.i_item_sk
  AND s1.ss_customer_sk = c_customer_sk
  AND c_current_addr_sk = ca_address_sk
  AND c_current_cdemo_sk = cd_demo_sk
  AND d_year BETWEEN 1999 AND 2000
  AND d_date_sk = s1.ss_sold_date_sk
  AND item1.i_category IN ('Books', 'Music')
  AND item2.i_manager_id BETWEEN 1 AND 20
  AND cd_marital_status = 'M'
  AND cd_education_status = 'College'
  AND s1.ss_list_price BETWEEN 1 AND 5
  AND s2.ss_list_price BETWEEN 1 AND 5
GROUP BY item1.i_item_sk, item2.i_item_sk
ORDER BY cnt
LIMIT 100;
"""

DSB_Q101 = """\
-- DSB Query 101
-- Customers who first buy and return on web, then buy again from store.
-- Non-PK/FK joins. DSB-specific addition to TPC-DS.
SELECT c_customer_sk, c_first_name, c_last_name, count(*) AS cnt
FROM store_sales,
     store_returns,
     web_sales,
     date_dim d1,
     date_dim d2,
     item,
     customer,
     customer_address,
     household_demographics
WHERE ss_ticket_number = sr_ticket_number
  AND ss_customer_sk = ws_bill_customer_sk
  AND ss_customer_sk = c_customer_sk
  AND c_current_addr_sk = ca_address_sk
  AND c_current_hdemo_sk = hd_demo_sk
  AND ss_item_sk = sr_item_sk
  AND sr_item_sk = ws_item_sk
  AND i_item_sk = ss_item_sk
  AND i_category IN ('Books', 'Music', 'Electronics')
  AND sr_returned_date_sk = d1.d_date_sk
  AND ws_sold_date_sk = d2.d_date_sk
  AND d2.d_date BETWEEN d1.d_date AND (d1.d_date + INTERVAL '90 day')
  AND ca_state IN ('TX', 'GA', 'VA', 'KY', 'KS')
  AND d1.d_year = 1999
  AND hd_income_band_sk BETWEEN 0 AND 19
  AND hd_buy_potential = '1001-5000'
  AND ss_sales_price / NULLIF(ss_list_price, 0) BETWEEN 0.0 AND 1.0
GROUP BY c_customer_sk, c_first_name, c_last_name
ORDER BY cnt
LIMIT 100;
"""

DSB_Q102 = """\
-- DSB Query 102
-- Customer demographics where customer buys from store and again from web
-- (initial purchase could also be web). Non-PK/FK joins. DSB addition.
SELECT cd_gender,
       cd_marital_status,
       cd_education_status,
       hd_vehicle_count,
       count(*) AS cnt
FROM store_sales,
     web_sales,
     date_dim d1,
     date_dim d2,
     customer,
     inventory,
     store,
     warehouse,
     item,
     customer_demographics,
     household_demographics,
     customer_address
WHERE ss_item_sk = i_item_sk
  AND ws_item_sk = ss_item_sk
  AND ss_sold_date_sk = d1.d_date_sk
  AND ws_sold_date_sk = d2.d_date_sk
  AND d2.d_date BETWEEN d1.d_date AND (d1.d_date + INTERVAL '30 day')
  AND ss_customer_sk = c_customer_sk
  AND ws_bill_customer_sk = c_customer_sk
  AND ws_warehouse_sk = inv_warehouse_sk
  AND ws_warehouse_sk = w_warehouse_sk
  AND inv_item_sk = ss_item_sk
  AND inv_date_sk = ss_sold_date_sk
  AND inv_quantity_on_hand >= ss_quantity
  AND s_state = w_state
  AND i_category IN ('Books', 'Music', 'Electronics')
  AND i_manager_id IN (1, 11, 21, 31, 41, 51, 61, 71, 81, 91)
  AND c_current_cdemo_sk = cd_demo_sk
  AND c_current_hdemo_sk = hd_demo_sk
  AND c_current_addr_sk = ca_address_sk
  AND ca_state IN ('TX', 'GA', 'VA', 'KY', 'KS')
  AND d1.d_year = 1999
  AND ws_wholesale_cost BETWEEN 0 AND 19
GROUP BY cd_gender, cd_marital_status, cd_education_status, hd_vehicle_count
ORDER BY cnt
LIMIT 100;
"""


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    DSB_DIR.mkdir(parents=True, exist_ok=True)

    tpcds_files = sorted(TPCDS_DIR.glob("query_*.sql"))
    if len(tpcds_files) != 99:
        raise RuntimeError(f"expected 99 TPC-DS query files, found {len(tpcds_files)}")

    for src in tpcds_files:
        dst = DSB_DIR / src.name
        shutil.copyfile(src, dst)
        logger.info("Copied %s -> %s", src.relative_to(REPO_ROOT), dst.relative_to(REPO_ROOT))

    for n, sql in [(100, DSB_Q100), (101, DSB_Q101), (102, DSB_Q102)]:
        dst = DSB_DIR / f"query_{n}.sql"
        dst.write_text(sql, encoding="utf-8")
        logger.info("Wrote DSB-specific %s (%d chars)", dst.relative_to(REPO_ROOT), len(sql))

    total = len(list(DSB_DIR.glob("query_*.sql")))
    logger.info("DSB query set ready: %d files in %s", total, DSB_DIR.relative_to(REPO_ROOT))


if __name__ == "__main__":
    main()
