"""validate-loads --wait, plain-log mode: the per-poll progress report.

Without a terminal the wait loop logs one line per outstanding table, read back
from the failure messages the checker writes. The parser and the formatter have
to agree, or every line falls back to the raw message and the delta since the
last poll and the stall flag silently disappear.
"""

from __future__ import annotations

from benchmark.staging.load_check import _key_and_counts, _mismatch_message, _progress_report

KEY = "snowflake/tpcds/sf10/store_sales"


def test_the_parser_reads_what_the_checker_writes():
    assert _key_and_counts(_mismatch_message(KEY, 1_234_567, 28_800_991)) == (
        KEY,
        1_234_567,
        28_800_991,
    )


def test_a_table_still_loading_shows_its_progress_since_the_last_poll():
    report, now = _progress_report(
        [_mismatch_message(KEY, 1_234_567, 28_800_991)], {KEY: 1_000_000}
    )

    assert report == f"  {KEY}: 1,234,567/28,800,991 (4%, +234,567)"
    assert now == {KEY: 1_234_567}


def test_a_table_that_did_not_move_is_flagged_as_stalled():
    report, _ = _progress_report([_mismatch_message(KEY, 1_234_567, 28_800_991)], {KEY: 1_234_567})

    assert "stalled" in report


def test_a_failure_without_counts_shows_its_reason():
    report, now = _progress_report([f"{KEY}: missing from manifest"], {})

    assert report == f"  {KEY}: missing from manifest"
    assert now == {}
