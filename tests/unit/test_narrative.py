"""Narrative composition.

The report's argument is prose; its numbers are computed. This module is what keeps
the two from disagreeing in public. The behaviours worth pinning are all failure
behaviours: a section that vanishes, a placeholder that ships literally, or a number
that quietly goes stale are each worse than a build that stops.
"""

from __future__ import annotations

import duckdb
import pytest

from benchmark.report import narrative


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    # Carries every column the shared views bind (report/views.py installs them
    # lazily under resolve_facts), not just the ones these facts read.
    c.execute(
        "CREATE TABLE main.sql_benchmark_results ("
        "platform VARCHAR, query_id VARCHAR, scale_factor INTEGER, "
        "run_type VARCHAR, run_number INTEGER, concurrency INTEGER, "
        "compute_label VARCHAR, block VARCHAR, error_message VARCHAR, "
        "server_time_ms DOUBLE, created_at TIMESTAMP)"
    )
    c.execute(
        "INSERT INTO main.sql_benchmark_results VALUES "
        "('snowflake','tpcds_q01',1,'warm',1,NULL,NULL,'tpcds',NULL,10,TIMESTAMP '2026-08-01'),"
        "('snowflake','tpcds_q02',100,'warm',1,NULL,NULL,'tpcds',NULL,10,TIMESTAMP '2026-08-01'),"
        "('redshift','tpcds_q01',1,'concurrency',1,10,NULL,'tpcds',NULL,10,"
        " TIMESTAMP '2026-08-01'),"
        "('redshift','tpcds_q01',1,'concurrency',1,50,NULL,'tpcds',NULL,10,"
        " TIMESTAMP '2026-08-01')"
    )
    yield c
    c.close()


def _write(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_section_without_an_anchor_is_an_error_not_a_silent_drop(tmp_path):
    _write(tmp_path, "01-intro.md", "# Intro\n\nNo anchor here.\n")

    with pytest.raises(narrative.NarrativeError, match="declares no anchor"):
        narrative.load_sections(tmp_path)


def test_unknown_anchor_is_rejected_and_lists_the_valid_ones(tmp_path):
    _write(tmp_path, "01-intro.md", "<!-- anchor: somewhere-else -->\n# Intro\n")

    with pytest.raises(narrative.NarrativeError) as exc:
        narrative.load_sections(tmp_path)

    assert "somewhere-else" in str(exc.value)
    assert "before-methodology" in str(exc.value), "the error must name the valid anchors"


def test_sections_load_in_filename_order_with_the_anchor_stripped(tmp_path):
    _write(tmp_path, "02-second.md", "<!-- anchor: after-everything -->\n# Second\n")
    _write(tmp_path, "01-first.md", "<!-- anchor: after-front-matter -->\n# First\n")

    secs = narrative.load_sections(tmp_path)

    assert [s.path.name for s in secs] == ["01-first.md", "02-second.md"]
    assert secs[0].anchor == "after-front-matter"
    assert "anchor:" not in secs[0].body, "the anchor marker must not reach the report"
    assert secs[0].body.startswith("# First")


def test_facts_resolve_from_the_database(con):
    out = narrative.resolve_facts(
        "{{fact:engines_with_data}} engines, {{fact:total_result_rows}} rows, "
        "levels {{fact:concurrency_levels}}, scales {{fact:scale_factors}}",
        con,
    )

    assert out == "2 engines, 4 rows, levels 10/50, scales SF1/SF100"


def test_concurrency_levels_leave_out_off_protocol_levels(con):
    """A 40-session ClickBench level sits outside the protocol ladder; the fact names
    only the protocol levels, as every published figure reads."""
    con.execute(
        "INSERT INTO main.sql_benchmark_results VALUES ('clickhouse','clickbench_q01',100,"
        "'concurrency',1,40,NULL,'clickbench',NULL,10,TIMESTAMP '2026-07-15')"
    )

    assert narrative.resolve_facts("{{fact:concurrency_levels}}", con) == "10/50"


def test_zero_valued_facts_still_resolve(con):
    """A count of zero is a real, publishable answer — it must not read as failure.

    'There is no cold-start data' and 'Fabric has no rows' are both load-bearing
    claims in the report, and both resolve to 0.
    """
    out = narrative.resolve_facts(
        "cold {{fact:cold_row_count}}, fabric {{fact:fabric_row_count}}", con
    )

    assert out == "cold 0, fabric 0"


def test_unknown_fact_is_an_error_not_a_literal_placeholder(con):
    with pytest.raises(narrative.NarrativeError) as exc:
        narrative.resolve_facts("{{fact:no_such_thing}}", con)

    assert "no_such_thing" in str(exc.value)
    assert "engines_with_data" in str(exc.value), "the error must list what IS available"


def test_compose_splices_at_the_mapped_anchor(tmp_path, con):
    _write(tmp_path, "01-mid.md", "<!-- anchor: before-methodology -->\n# Prose\n")
    generated = ["# A", "", "# B", "", "# Methodology"]

    out = narrative.compose(
        generated, con, anchor_positions={"before-methodology": 4}, sections_dir=tmp_path
    )

    assert out.index("# Prose") < out.index("# Methodology")
    assert out.index("# B") < out.index("# Prose")


def test_compose_appends_rather_than_dropping_an_unmapped_anchor(tmp_path, con):
    """If the generator's shape changes, prose must survive — loudly misplaced beats
    silently absent."""
    _write(tmp_path, "01-mid.md", "<!-- anchor: before-clickbench -->\n# Prose\n")
    generated = ["# A", "# B"]

    out = narrative.compose(generated, con, anchor_positions={}, sections_dir=tmp_path)

    assert "# Prose" in out


def test_compose_resolves_facts_in_authored_prose(tmp_path, con):
    _write(
        tmp_path,
        "01-mid.md",
        "<!-- anchor: after-front-matter -->\nWe measured {{fact:engines_with_data}} engines.\n",
    )

    out = narrative.compose(
        ["# A"], con, anchor_positions={"after-front-matter": 0}, sections_dir=tmp_path
    )

    assert any("We measured 2 engines." in line for line in out)
    assert not any("{{fact:" in line for line in out), "no placeholder may reach the report"


def test_missing_sections_dir_is_not_an_error(con):
    assert narrative.compose(
        ["# A"], con, anchor_positions={}, sections_dir=__import__("pathlib").Path("does/not/exist")
    ) == ["# A"]


def test_colliding_anchors_emit_in_document_order(tmp_path, con):
    """Adjacent anchors resolve to the same line index when nothing sits between them.

    Keying inserts on position alone silently keeps one and drops the rest, or
    misorders them (Part I after Part II). Both must appear, in ANCHORS declaration
    order, which is document order.
    """
    _write(tmp_path, "01-a.md", "<!-- anchor: after-front-matter -->\n# Part I\n")
    _write(tmp_path, "02-b.md", "<!-- anchor: before-decision-support -->\n# Part II\n")
    generated = ["# Front", "# Decision Support"]

    out = narrative.compose(
        generated,
        con,
        # Both anchors at the same index — the collision case.
        anchor_positions={"after-front-matter": 1, "before-decision-support": 1},
        sections_dir=tmp_path,
    )

    assert "# Part I" in out and "# Part II" in out
    assert out.index("# Part I") < out.index("# Part II"), "declaration order is document order"
    assert out.index("# Part II") < out.index("# Decision Support")
    assert out.index("# Front") < out.index("# Part I")
