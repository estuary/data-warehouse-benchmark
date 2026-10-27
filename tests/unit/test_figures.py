"""The number-drift gate.

A report figure goes wrong by selecting the wrong slice far more often than by bad
arithmetic, and a wrong number looks plausible on the page. This gate is the only
mechanism that catches that class, so its own failure modes matter: a table that
quietly opts out of checking, a drift that reads as a rounding artifact, or a
"not measured" cell compared as if it were a zero would each let a wrong figure
through while reporting success.
"""

from __future__ import annotations

import duckdb
import pytest

from benchmark.report import figures


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test registers its own verifiers without leaking into the next."""
    saved = dict(figures.VERIFIERS)
    figures.VERIFIERS.clear()
    yield
    figures.VERIFIERS.clear()
    figures.VERIFIERS.update(saved)


@pytest.fixture
def con():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def _section(tmp_path, body: str):
    (tmp_path / "01-part.md").write_text(body, encoding="utf-8")
    return tmp_path


TABLE = """<!-- verify: demo -->
| Tier | Suite time |
|---|---|
| X-Small | 100.0 s |
| Large | 25.0 s |
"""


def test_a_matching_table_reports_no_drift(tmp_path, con):
    figures.VERIFIERS["demo"] = (lambda _c: [["X-Small", "100.0 s"], ["Large", "25.0 s"]], 1)

    assert figures.verify(con, _section(tmp_path, TABLE)) == []


def test_a_wrong_number_is_reported_with_both_values(tmp_path, con):
    figures.VERIFIERS["demo"] = (lambda _c: [["X-Small", "100.0 s"], ["Large", "40.0 s"]], 1)

    (drift,) = figures.verify(con, _section(tmp_path, TABLE))

    assert drift.kind == "value"
    assert drift.row_label == "Large"
    assert drift.published == "25.0 s"
    assert drift.recomputed == "40.0 s"
    assert "Suite time" in drift.column, "the column must be named, not just the row"


def test_rounding_is_tolerated_but_a_wrong_slice_is_not(tmp_path, con):
    """The bar has to sit between the two. Published tables round to 1-2 decimals, so
    exact equality would cry wolf; a wrong slice is off by far more."""
    rounding = (lambda _c: [["X-Small", "100.4 s"], ["Large", "25.0 s"]], 1)
    wrong_tier = (lambda _c: [["X-Small", "112.0 s"], ["Large", "25.0 s"]], 1)

    figures.VERIFIERS["demo"] = rounding
    assert figures.verify(con, _section(tmp_path, TABLE)) == []

    figures.VERIFIERS["demo"] = wrong_tier
    assert len(figures.verify(con, _section(tmp_path, TABLE))) == 1


def test_not_measured_never_compares_equal_to_zero(tmp_path, con):
    """Conflating "—" (never ran) with 0 (ran and served nothing) is its own
    misstatement, and a useful-concurrency table can hold both."""
    body = "<!-- verify: demo -->\n| Tier | 1 TB |\n|---|---|\n| Small | — |\n"
    figures.VERIFIERS["demo"] = (lambda _c: [["Small", "0"]], 1)

    (drift,) = figures.verify(con, _section(tmp_path, body))

    assert drift.published == "—"
    assert drift.recomputed == "0"


def test_typographic_minus_compares_equal_to_ascii_minus(tmp_path, con):
    """Prose writes U+2212; SQL emits '-'. Without normalization every negative
    number reports drift and the noise buries the real findings."""
    body = "<!-- verify: demo -->\n| Tier | k |\n|---|---|\n| Medium | −0.07 |\n"
    figures.VERIFIERS["demo"] = (lambda _c: [["Medium", "-0.07"]], 1)

    assert figures.verify(con, _section(tmp_path, body)) == []


def test_bold_and_units_do_not_defeat_comparison(tmp_path, con):
    body = "<!-- verify: demo -->\n| Tier | Cost |\n|---|---|\n| Large | **$4,293.00** |\n"
    figures.VERIFIERS["demo"] = (lambda _c: [["Large", "$4293.00"]], 1)

    assert figures.verify(con, _section(tmp_path, body)) == []


def test_a_marked_table_with_no_verifier_is_an_error(tmp_path, con):
    """Otherwise a table opts out of the gate by deleting one line of code."""
    with pytest.raises(figures.FigureError, match="no verifier registered"):
        figures.verify(con, _section(tmp_path, TABLE))


def test_a_marker_with_no_table_is_an_error(tmp_path, con):
    """A moved table that left its promise behind must fail loudly."""
    figures.VERIFIERS["demo"] = (lambda _c: [], 1)
    body = "<!-- verify: demo -->\n\nSome prose, no table.\n"

    with pytest.raises(figures.FigureError, match="not followed by a table"):
        figures.verify(con, _section(tmp_path, body))


def test_missing_and_extra_rows_are_coverage_not_value_drift(tmp_path, con):
    """A curated table is a legitimate editorial choice; a wrong number is not.
    Counting them together would let 48 coverage notes hide one real defect."""
    figures.VERIFIERS["demo"] = (
        lambda _c: [["X-Small", "100.0 s"], ["Large", "25.0 s"], ["Medium", "50.0 s"]],
        1,
    )

    drifts = figures.verify(con, _section(tmp_path, TABLE))

    assert [d.kind for d in drifts] == ["coverage"]
    assert drifts[0].row_label == "Medium"


def test_two_key_columns_keep_engine_tier_rows_distinct(tmp_path, con):
    """With one key column every Redshift row collides on the label "Redshift" and
    only the last is ever checked — the table would silently go unverified."""
    body = (
        "<!-- verify: demo -->\n| Engine | Tier | 1 GB |\n|---|---|---|\n"
        "| Redshift | 8 RPU | 100 |\n| Redshift | 128 RPU | 50 |\n"
    )
    figures.VERIFIERS["demo"] = (
        lambda _c: [["Redshift", "8 RPU", "100"], ["Redshift", "128 RPU", "999"]],
        2,
    )

    (drift,) = figures.verify(con, _section(tmp_path, body))

    assert drift.row_label == "Redshift · 128 RPU"
    assert drift.published == "50"


def test_every_marker_in_the_real_sections_has_a_verifier():
    """The gate is only as good as its coverage of the shipped sections.

    Reloading re-runs the ``@verifier`` decorators against the (fixture-cleared)
    registry, so this checks the real markers against the real verifiers.
    """
    import importlib

    from benchmark.report import verifiers as real

    importlib.reload(real)

    missing = [t.name for t in figures.load_tables() if t.name not in figures.VERIFIERS]

    assert not missing, f"marked tables with no verifier: {missing}"


def test_censoring_marker_is_not_invisible_to_the_gate():
    """``50+`` must not compare equal to ``50``.

    ``_number`` reads a trailing ``+`` as decoration and returns 50.0 for both, so a
    gate comparing numbers alone ignores the marker: when new data un-censors a
    cell, the published ``50+`` becomes a lower bound the data does not support,
    and ``verify-figures`` would still report everything in agreement.

    The marker is a claim about the number — "we never asked for more" — so the gate
    has to check it. Both directions matter: a stale ``+`` overstates uncertainty, a
    missing one asserts a limit we never established.
    """
    assert not figures._cell_matches("50+", "50", 0.0)
    assert not figures._cell_matches("50", "50+", 0.0)
    assert figures._cell_matches("50+", "50+", 0.0)
    assert figures._cell_matches("50", "50", 0.0)
    # The marker check must not disturb the non-numeric sentinels.
    assert figures._cell_matches("—", "—", 0.0)
    assert not figures._cell_matches("—", "50", 0.0)
    assert not figures._cell_matches("*", "50+", 0.0)
