"""Translation-output drift guard.

The committed SQL in ``queries/<engine>/`` is what the benchmark executes and what a
reader diffs us against. It is produced by sqlglot plus our per-engine AST and regex
passes — and ``pyproject.toml`` carries ``sqlglot>=26.0.0``, an open upper bound. A
dependency upgrade can therefore change what we *would* generate without changing a
line of our code and without failing any other test.

That matters more than an ordinary dependency bump: a reader who runs
``benchmark translate`` on a newer sqlglot and gets different SQL than we published has
no way to tell a library change from us having quietly edited the queries. This test
turns that into a build failure with an explicit instruction instead.

It fingerprints the pipeline's real output (transpilation *plus* the post-passes) per
engine, without writing to the committed files. When sqlglot — or our own passes —
change what we generate, the hash moves and this fails.

**To re-bless after a deliberate change:** run with ``BLESS_TRANSLATION=1``, inspect
the resulting diff in ``queries/`` from a real ``benchmark translate``, and commit the
updated fingerprint alongside it.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib

import pytest

from benchmark.translator import ENGINE_DIALECTS, translate_all

FINGERPRINT = pathlib.Path(__file__).parent / "translation_fingerprint.json"


def _fingerprint() -> dict[str, str]:
    """sha256 of the final SQL per engine, over queries sorted by id."""
    collected: dict[tuple[str, str], str] = {}
    translate_all(write_files=False, collect=collected)
    out: dict[str, str] = {}
    for engine in sorted(ENGINE_DIALECTS):
        h = hashlib.sha256()
        for (eng, qid), sql in sorted(collected.items()):
            if eng == engine:
                # The query id is hashed too, so a query moving between ids is
                # caught rather than cancelling out.
                h.update(qid.encode())
                h.update(sql.encode())
        out[engine] = h.hexdigest()
    return out


def test_translation_output_has_not_drifted():
    import sqlglot

    current = _fingerprint()

    if os.environ.get("BLESS_TRANSLATION"):
        FINGERPRINT.write_text(
            json.dumps({"sqlglot": sqlglot.__version__, "engines": current}, indent=2) + "\n"
        )
        pytest.skip(f"fingerprint re-blessed for sqlglot {sqlglot.__version__}")

    if not FINGERPRINT.exists():
        pytest.fail(
            f"no translation fingerprint recorded. Generate one with "
            f"BLESS_TRANSLATION=1 (sqlglot {sqlglot.__version__} installed)."
        )

    recorded = json.loads(FINGERPRINT.read_text())
    drifted = [e for e, h in current.items() if recorded["engines"].get(e) != h]

    assert not drifted, (
        f"translation output changed for: {', '.join(drifted)}.\n"
        f"Fingerprint was recorded under sqlglot {recorded['sqlglot']}; "
        f"sqlglot {sqlglot.__version__} is installed now.\n"
        "If this is an intentional change, run `benchmark translate`, review the diff "
        "in queries/, then re-bless with BLESS_TRANSLATION=1 and commit both together. "
        "If it is an unintended dependency upgrade, pin sqlglot back."
    )


def test_every_engine_is_covered_by_the_fingerprint():
    """A new engine must not slip in unfingerprinted."""
    if not FINGERPRINT.exists():
        pytest.skip("no fingerprint recorded yet")

    recorded = json.loads(FINGERPRINT.read_text())["engines"]

    assert set(recorded) == set(ENGINE_DIALECTS), (
        "fingerprint does not cover every engine: "
        f"missing {set(ENGINE_DIALECTS) - set(recorded)}, "
        f"stale {set(recorded) - set(ENGINE_DIALECTS)}"
    )
