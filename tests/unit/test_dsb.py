"""DSB skewed-data generation: generator precedence + .dat->parquet conversion.

The real skew comes from the microsoft/dsb generator (native on Linux, Docker on
macOS) — building it + generating GBs is too heavy for CI, so these tests cover
the harness logic with the generators mocked: idempotent skip-if-present, the
native→(build|docker)→raise precedence (NO silent uniform fallback), and the
dsdgen .dat → Parquet conversion (incl. trailing-pipe + .meta cleanup).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from benchmark.datagen import dsb
from benchmark.exceptions import DataGenerationError


def test_convert_dat_to_parquet_applies_canonical_names_and_types(tmp_path: Path, monkeypatch):
    # Canonical schema mocked (no DuckDB tpcds extension needed). r_reason_id is
    # VARCHAR holding a numeric-looking value, to prove we don't auto-infer it as
    # an integer (which would drop leading zeros and break string ops).
    monkeypatch.setattr(
        dsb,
        "tpcds_table_schema",
        lambda: {
            "reason": (
                ("r_reason_sk", "BIGINT"),
                ("r_reason_id", "VARCHAR"),
                ("r_reason_desc", "VARCHAR"),
            )
        },
    )
    # 3 values + an empty trailing field (dsdgen's final delimiter), plus a *.meta sidecar.
    (tmp_path / "reason.dat").write_text(
        "1|00777|returned|\n2|00042|defective|\n", encoding="utf-8"
    )
    (tmp_path / "reason_r_reason_desc.meta").write_text("skew-dist", encoding="utf-8")

    counts = dsb._convert_dat_to_parquet(tmp_path)

    assert counts == {"reason": 2}
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(tmp_path / "reason.parquet")
    # Real names, trailing field dropped.
    assert pf.schema_arrow.names == ["r_reason_sk", "r_reason_id", "r_reason_desc"]
    data = pf.read().to_pydict()
    assert data["r_reason_sk"] == [1, 2]  # cast to BIGINT
    assert data["r_reason_id"] == ["00777", "00042"]  # VARCHAR, leading zeros intact
    assert not (tmp_path / "reason.dat").exists()  # raw .dat tidied
    assert not list(tmp_path.glob("*.meta"))  # skew sidecars tidied


def test_macos_without_docker_raises_no_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dsb, "_dsb_binary", lambda: None)
    monkeypatch.setattr(dsb, "_is_darwin", lambda: True)
    monkeypatch.setattr(dsb, "_docker_available", lambda: False)

    with pytest.raises(DataGenerationError):
        dsb.generate_dsb_sf(1, tmp_path)
    assert not (tmp_path / ".dsb_fallback").exists()  # no silent uniform fallback


def test_prefers_local_binary_over_docker(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(dsb, "_dsb_binary", lambda: tmp_path / "dsdgen")
    monkeypatch.setattr(dsb, "_generate_raw_local", lambda b, s, o: calls.append("local"))
    monkeypatch.setattr(dsb, "_generate_raw_docker", lambda s, o: calls.append("docker"))
    monkeypatch.setattr(dsb, "_convert_dat_to_parquet", lambda o: {"item": 18000})

    counts = dsb.generate_dsb_sf(1, tmp_path)

    assert calls == ["local"]
    assert counts == {"item": 18000}


def test_uses_docker_on_macos_when_no_local_binary(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(dsb, "_dsb_binary", lambda: None)
    monkeypatch.setattr(dsb, "_is_darwin", lambda: True)  # macOS -> Docker generator
    monkeypatch.setattr(dsb, "_docker_available", lambda: True)
    monkeypatch.setattr(dsb, "_ensure_dsb_image", lambda: True)
    monkeypatch.setattr(dsb, "_generate_raw_local", lambda b, s, o: calls.append("local"))
    monkeypatch.setattr(dsb, "_generate_raw_docker", lambda s, o: calls.append("docker"))
    monkeypatch.setattr(dsb, "_convert_dat_to_parquet", lambda o: {"item": 18000})

    counts = dsb.generate_dsb_sf(1, tmp_path)

    assert calls == ["docker"]
    assert counts == {"item": 18000}


def test_linux_builds_native_when_no_binary(tmp_path: Path, monkeypatch):
    # On Linux (non-Darwin) with no native binary, build it — never Docker.
    calls: list[str] = []
    monkeypatch.setattr(dsb, "_dsb_binary", lambda: None)
    monkeypatch.setattr(dsb, "_is_darwin", lambda: False)
    monkeypatch.setattr(dsb, "_build_native_binary", lambda: calls.append("build") or Path("x"))
    monkeypatch.setattr(dsb, "_generate_raw_local", lambda b, s, o: calls.append("local"))
    monkeypatch.setattr(dsb, "_generate_raw_docker", lambda s, o: calls.append("docker"))
    monkeypatch.setattr(dsb, "_convert_dat_to_parquet", lambda o: {"item": 18000})

    counts = dsb.generate_dsb_sf(1, tmp_path)

    assert calls == ["build", "local"]  # built native, used it; never Docker
    assert counts == {"item": 18000}


def test_linux_build_failure_raises_not_fallback(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dsb, "_dsb_binary", lambda: None)
    monkeypatch.setattr(dsb, "_is_darwin", lambda: False)

    def _boom() -> Path:
        raise DataGenerationError("no toolchain")

    monkeypatch.setattr(dsb, "_build_native_binary", _boom)

    with pytest.raises(DataGenerationError):
        dsb.generate_dsb_sf(1, tmp_path)
    assert not (tmp_path / ".dsb_fallback").exists()  # did NOT fall back


def _write_table_parquets(directory: Path) -> None:
    """Write a 1-row parquet for every TPC-DS table (a 'complete' scale dir)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    for table in dsb.TPCDS_TABLES:
        pq.write_table(pa.table({"x": [1]}), directory / f"{table}.parquet")


def test_skips_regeneration_when_data_present(tmp_path: Path, monkeypatch):
    _write_table_parquets(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(dsb, "_dsb_binary", lambda: calls.append("binary") or None)
    monkeypatch.setattr(dsb, "_build_native_binary", lambda: calls.append("build") or Path("x"))
    monkeypatch.setattr(dsb, "_generate_raw_local", lambda b, s, o: calls.append("local"))
    monkeypatch.setattr(dsb, "_generate_raw_docker", lambda s, o: calls.append("docker"))

    counts = dsb.generate_dsb_sf(1, tmp_path)

    assert calls == []  # short-circuited before any generator/build path
    assert counts == {t: 1 for t in dsb.TPCDS_TABLES}


def test_force_regenerates_even_when_present(tmp_path: Path, monkeypatch):
    _write_table_parquets(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(dsb, "_dsb_binary", lambda: tmp_path / "dsdgen")
    monkeypatch.setattr(dsb, "_generate_raw_local", lambda b, s, o: calls.append("local"))
    monkeypatch.setattr(dsb, "_convert_dat_to_parquet", lambda o: {"item": 18000})

    counts = dsb.generate_dsb_sf(1, tmp_path, force=True)

    assert calls == ["local"]  # forced regeneration despite present data
    assert counts == {"item": 18000}


def test_fallback_marker_triggers_regeneration(tmp_path: Path, monkeypatch):
    # Data present but the prior run was a uniform fallback -> regenerate to try
    # for real skew (don't skip on a degraded prior result).
    _write_table_parquets(tmp_path)
    (tmp_path / ".dsb_fallback").write_text("prior uniform fallback\n", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(dsb, "_dsb_binary", lambda: tmp_path / "dsdgen")
    monkeypatch.setattr(dsb, "_generate_raw_local", lambda b, s, o: calls.append("local"))
    monkeypatch.setattr(dsb, "_convert_dat_to_parquet", lambda o: {"item": 18000})

    counts = dsb.generate_dsb_sf(1, tmp_path)

    assert calls == ["local"]  # regenerated despite present parquets
    assert counts == {"item": 18000}
    assert not (tmp_path / ".dsb_fallback").exists()  # stale marker cleared
