"""Optimization backends: claude-cli subprocess + the pluggable runner."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from benchmark import optimize_pass as op


def test_looks_like_sql():
    assert op._looks_like_sql("SELECT 1")
    assert op._looks_like_sql("  with x as (select 1) select * from x")
    assert op._looks_like_sql("(SELECT 1) UNION ALL (SELECT 2)")
    assert not op._looks_like_sql("Here is the optimized query:")
    assert not op._looks_like_sql("")


def test_make_rewriter_rejects_unknown_backend():
    with pytest.raises(op.OptimizeNotConfiguredError):
        op._make_rewriter("nope", None, 60)


def test_claude_cli_rewriter_builds_command_and_returns_stdout(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda _: "/usr/bin/claude")
    captured = {}

    def fake_run(cmd, input, text, capture_output, timeout, env=None):
        captured["cmd"] = cmd
        captured["input"] = input
        captured["env"] = env
        return SimpleNamespace(returncode=0, stdout="SELECT 1\n", stderr="")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    rewrite = op._claude_cli_rewriter(model="opus", timeout_s=42)
    out = rewrite("PROMPT BODY")

    assert out == "SELECT 1\n"
    assert captured["input"] == "PROMPT BODY"  # prompt on stdin, not argv
    cmd = captured["cmd"]
    assert cmd[:2] == ["/usr/bin/claude", "-p"]
    assert "--output-format" in cmd and "text" in cmd
    # tools disabled -> pure text transform, no agentic/file access
    assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == ""
    assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "opus"


def test_claude_cli_rewriter_strips_claude_code_session_env(monkeypatch):
    # A nested `claude -p` must NOT inherit Claude Code's session vars, or it
    # authenticates as a child session and fails org validation. The subprocess
    # env should keep ordinary vars (PATH) but drop the CLAUDE_CODE* family.
    monkeypatch.setattr(op.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        op.os,
        "environ",
        {
            "PATH": "/usr/bin",
            "HOME": "/home/u",
            "CLAUDECODE": "1",
            "CLAUDE_CODE_CHILD_SESSION": "1",
            "CLAUDE_CODE_SESSION_ID": "abc",
            "CLAUDE_EFFORT": "high",
        },
    )
    captured = {}

    def fake_run(cmd, **k):
        captured["env"] = k.get("env")
        return SimpleNamespace(returncode=0, stdout="SELECT 1", stderr="")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    op._claude_cli_rewriter(model=None, timeout_s=10)("p")

    env = captured["env"]
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/home/u"
    assert not any(k.startswith(("CLAUDE_CODE", "CLAUDECODE", "CLAUDE_EFFORT")) for k in env)


def test_claude_cli_rewriter_omits_model_when_none(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        op.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="SELECT 1", stderr=""),
    )
    op._claude_cli_rewriter(model=None, timeout_s=10)("p")  # no raise


def test_backoff_delay_is_jittered_and_capped():
    # Equal jitter: each delay lies in [c/2, c] for c = min(CAP, BASE*FACTOR**i),
    # and the ceiling never exceeds the cap.
    for attempt in range(8):
        ceiling = min(op._CLI_RETRY_CAP_S, op._CLI_RETRY_BASE_S * op._CLI_RETRY_FACTOR**attempt)
        for _ in range(50):
            d = op._backoff_delay(attempt)
            assert ceiling / 2 <= d <= ceiling <= op._CLI_RETRY_CAP_S


def test_claude_cli_rewriter_retries_transient_then_succeeds(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda _: "/usr/bin/claude")
    sleeps: list[float] = []
    monkeypatch.setattr(op.time, "sleep", lambda s: sleeps.append(s))
    # Deterministic jitter: take the top of the equal-jitter window (the ceiling).
    monkeypatch.setattr(op.random, "uniform", lambda _lo, hi: hi)
    calls = {"n": 0}

    def fake_run(cmd, **k):
        calls["n"] += 1
        if calls["n"] < 3:  # org-validation flake twice, then succeed
            return SimpleNamespace(
                returncode=1, stdout="", stderr="Unable to verify organization for the token"
            )
        return SimpleNamespace(returncode=0, stdout="SELECT 1", stderr="")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    out = op._claude_cli_rewriter(model=None, timeout_s=10)("p")

    assert out == "SELECT 1"
    assert calls["n"] == 3
    # Backed off before attempts 2 and 3, at the first two ceilings.
    assert sleeps == [
        op._CLI_RETRY_BASE_S,
        op._CLI_RETRY_BASE_S * op._CLI_RETRY_FACTOR,
    ]


def test_claude_cli_rewriter_transient_gives_up_after_retries(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(op.time, "sleep", lambda _s: None)
    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return SimpleNamespace(returncode=1, stdout="", stderr="could not be validated (network)")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    rewrite = op._claude_cli_rewriter(model=None, timeout_s=10)
    with pytest.raises(RuntimeError, match=f"after {op._CLI_RETRY_MAX} retries"):
        rewrite("p")
    assert calls["n"] == op._CLI_RETRY_MAX + 1  # initial + all retries


def test_claude_cli_rewriter_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        op.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )
    rewrite = op._claude_cli_rewriter(model=None, timeout_s=10)
    with pytest.raises(RuntimeError, match="claude -p exited 1"):
        rewrite("p")


def test_claude_cli_rewriter_missing_binary_raises(monkeypatch):
    monkeypatch.setattr(op.shutil, "which", lambda _: None)
    with pytest.raises(op.OptimizeNotConfiguredError, match=r"claude.*PATH"):
        op._claude_cli_rewriter(model=None, timeout_s=10)


def _seed_query(tmp_path, monkeypatch, sql: str = "SELECT 1\n"):
    """Point QUERIES_ROOT at a temp tree with one snowflake query."""
    qroot = tmp_path / "queries"
    (qroot / "snowflake" / "tpcds").mkdir(parents=True)
    (qroot / "snowflake" / "tpcds" / "query_01.sql").write_text(sql, encoding="utf-8")
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(op, "QUERIES_ROOT", qroot)
    return qroot


def test_optimize_for_platform_rewrites_and_audits(tmp_path, monkeypatch):
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT 1\n")
    # Stub the backend: return a changed, valid SQL string.
    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: lambda prompt: "SELECT 2")

    n = op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1)

    assert n == 1
    assert (qroot / "snowflake" / "tpcds" / "query_01.sql").read_text() == "SELECT 2\n"
    log = json.loads((qroot.parent / "results" / "optimization_snowflake.json").read_text())
    assert log["backend"] == "claude-cli"
    assert log["rewritten"] == 1 and log["errors"] == 0
    assert log["guidance"]["correctness"]  # registry guidance recorded


def test_optimize_for_platform_keeps_original_on_non_sql_reply(tmp_path, monkeypatch):
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT 1\n")
    monkeypatch.setattr(
        op, "_make_rewriter", lambda *a, **k: lambda prompt: "Sure! Here is your query:"
    )

    n = op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1)

    assert n == 0
    # Original preserved — never write prose.
    assert (qroot / "snowflake" / "tpcds" / "query_01.sql").read_text() == "SELECT 1\n"
    log = json.loads((qroot.parent / "results" / "optimization_snowflake.json").read_text())
    assert log["errors"] == 1


def test_optimize_for_platform_missing_dir_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(op, "QUERIES_ROOT", tmp_path / "queries")
    with pytest.raises(op.OptimizeNotConfiguredError, match="no translated queries"):
        op.optimize_for_platform("snowflake", backend="claude-cli")


# --- model pinning -----------------------------------------------------------


def test_make_rewriter_claude_cli_pins_optimize_model(monkeypatch):
    from benchmark.settings import get_settings

    monkeypatch.setattr(op.shutil, "which", lambda _: "/usr/bin/claude")
    captured = {}

    def fake_run(cmd, **k):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="SELECT 1", stderr="")

    monkeypatch.setattr(op.subprocess, "run", fake_run)
    # No explicit model -> claude-cli should pin BENCHMARK_OPTIMIZE_MODEL, not omit --model.
    op._make_rewriter("claude-cli", None, 30)("prompt")
    cmd = captured["cmd"]
    assert "--model" in cmd
    assert cmd[cmd.index("--model") + 1] == get_settings().OPTIMIZE_MODEL


# --- sqlglot validation + retry ----------------------------------------------


def test_invalid_sql_retries_then_succeeds(tmp_path, monkeypatch):
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT 1\n")
    calls = {"n": 0}

    def stub(_prompt: str) -> str:
        calls["n"] += 1
        return "SELECT (" if calls["n"] == 1 else "SELECT 2"  # invalid, then valid

    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: stub)
    n = op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1, max_attempts=3)

    assert n == 1
    assert calls["n"] == 2  # retried once after the parse failure
    assert (qroot / "snowflake" / "tpcds" / "query_01.sql").read_text() == "SELECT 2\n"


def test_invalid_sql_kept_original_after_max_attempts(tmp_path, monkeypatch):
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT 1\n")
    calls = {"n": 0}

    def stub(_prompt: str) -> str:
        calls["n"] += 1
        return "SELECT ("  # never parses

    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: stub)
    n = op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1, max_attempts=2)

    assert n == 0
    assert calls["n"] == 2  # exhausted attempts
    assert (qroot / "snowflake" / "tpcds" / "query_01.sql").read_text() == "SELECT 1\n"
    log = json.loads((qroot.parent / "results" / "optimization_snowflake.json").read_text())
    assert log["errors"] == 1
    entry = log["queries"][0]
    assert entry["status"] == "error" and "sqlglot parse error" in entry["error"]


def test_retry_prompt_carries_the_parse_error(tmp_path, monkeypatch):
    _seed_query(tmp_path, monkeypatch, "SELECT 1\n")
    prompts: list[str] = []

    def stub(prompt: str) -> str:
        prompts.append(prompt)
        return "SELECT (" if len(prompts) == 1 else "SELECT 2"

    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: stub)
    op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1, max_attempts=3)

    assert len(prompts) == 2
    assert "did not parse" not in prompts[0]  # first is the normal optimization prompt
    assert "failed to parse" in prompts[1]  # retry carries the error + broken SQL
    assert "SELECT (" in prompts[1]


# --- arity tripwire + equivalence gate ---------------------------------------


class _FakeChecker:
    """Stand-in for LocalEquivalenceChecker returning a scripted verdict."""

    def __init__(self, verdict):
        self._verdict = verdict
        self.calls = 0

    def check(self, query_id, block, sql, dialect):
        from benchmark.equivalence import EquivResult

        self.calls += 1
        return EquivResult(self._verdict, "scripted")


def test_arity_change_retries_then_keeps_original(tmp_path, monkeypatch):
    # Original projects 2 columns; the rewrite drops one -> arity tripwire fires.
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT a, b FROM store_sales\n")
    monkeypatch.setattr(
        op, "_make_rewriter", lambda *a, **k: lambda _p: "SELECT a FROM store_sales"
    )

    n = op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1, max_attempts=2)

    assert n == 0
    assert (
        qroot / "snowflake" / "tpcds" / "query_01.sql"
    ).read_text() == "SELECT a, b FROM store_sales\n"
    log = json.loads((qroot.parent / "results" / "optimization_snowflake.json").read_text())
    assert log["errors"] == 1
    assert "output column count changed" in log["queries"][0]["error"]


def test_equivalence_differ_retries_then_succeeds(tmp_path, monkeypatch):
    from benchmark.equivalence import EquivResult

    _seed_query(tmp_path, monkeypatch, "SELECT a, b FROM store_sales\n")
    # First candidate diverges, second is equal — same arity throughout.
    calls = {"n": 0}

    def stub(_prompt):
        calls["n"] += 1
        return "SELECT a, b FROM store_sales WHERE a > 0"

    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: stub)

    class Checker:
        def __init__(self):
            self.n = 0

        def check(self, *a):
            self.n += 1
            return EquivResult("differ" if self.n == 1 else "equal", "scripted")

    checker = Checker()
    n = op.optimize_for_platform(
        "snowflake", backend="claude-cli", jobs=1, max_attempts=3, equiv_checker=checker
    )
    assert n == 1
    assert checker.n == 2  # retried once after the divergence


def test_equivalence_deferred_is_accepted_and_logged(tmp_path, monkeypatch):
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT a, b FROM store_sales\n")
    monkeypatch.setattr(
        op, "_make_rewriter", lambda *a, **k: lambda _p: "SELECT a, b FROM store_sales WHERE a > 0"
    )
    checker = _FakeChecker("deferred")
    n = op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1, equiv_checker=checker)
    assert n == 1
    log = json.loads((qroot.parent / "results" / "optimization_snowflake.json").read_text())
    entry = log["queries"][0]
    assert entry["status"] == "rewritten"
    assert entry["equivalence"] == "deferred"
    assert entry["original_sql"] == "SELECT a, b FROM store_sales"
    assert log["equivalence_deferred"] == 1 and log["equivalence_verified"] == 0


def test_baselines_roundtrip_and_revert(tmp_path, monkeypatch):
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT a, b FROM store_sales\n")
    monkeypatch.setattr(
        op, "_make_rewriter", lambda *a, **k: lambda _p: "SELECT a, b FROM store_sales WHERE a > 0"
    )
    op.optimize_for_platform(
        "snowflake", backend="claude-cli", jobs=1, equiv_checker=_FakeChecker("equal")
    )

    baselines = op.load_optimization_baselines("snowflake")
    assert baselines == {"tpcds_q01": "SELECT a, b FROM store_sales"}

    # The file currently holds the rewrite; reverting restores the baseline.
    qfile = qroot / "snowflake" / "tpcds" / "query_01.sql"
    assert qfile.read_text() == "SELECT a, b FROM store_sales WHERE a > 0\n"
    assert op.revert_query_file("snowflake", "tpcds_q01", baselines["tpcds_q01"]) is True
    assert qfile.read_text() == "SELECT a, b FROM store_sales\n"


# --- resumable progress journal ----------------------------------------------


def test_resume_skips_completed_queries(tmp_path, monkeypatch):
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT 1\n")
    calls = {"n": 0}

    def first(_prompt):
        calls["n"] += 1
        return "SELECT 2"

    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: first)
    assert op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1) == 1
    assert calls["n"] == 1
    qfile = qroot / "snowflake" / "tpcds" / "query_01.sql"
    assert qfile.read_text() == "SELECT 2\n"
    assert op._progress_path("snowflake").exists()

    # Second run: the rewriter must never be invoked — the query is already done.
    def boom(_prompt):
        raise AssertionError("rewriter called for an already-optimized query")

    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: boom)
    assert op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1) == 0
    assert qfile.read_text() == "SELECT 2\n"  # untouched on resume


def test_force_reoptimizes_despite_journal(tmp_path, monkeypatch):
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT 1\n")
    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: lambda _p: "SELECT 2")
    op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1)

    qfile = qroot / "snowflake" / "tpcds" / "query_01.sql"
    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: lambda _p: "SELECT 3")
    assert op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1, force=True) == 1
    assert qfile.read_text() == "SELECT 3\n"


def test_resume_reruns_errored_query(tmp_path, monkeypatch):
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT 1\n")
    # First run fails the gate (non-SQL reply) -> journaled as an error.
    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: lambda _p: "sorry, here you go:")
    assert op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1, max_attempts=1) == 0

    # Resume must RE-RUN the errored query (errors are not "done").
    calls = {"n": 0}

    def good(_prompt):
        calls["n"] += 1
        return "SELECT 2"

    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: good)
    assert op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1) == 1
    assert calls["n"] == 1
    assert (qroot / "snowflake" / "tpcds" / "query_01.sql").read_text() == "SELECT 2\n"


def test_resume_reoptimizes_when_file_changed_since_journal(tmp_path, monkeypatch):
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT 1\n")
    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: lambda _p: "SELECT 2")
    op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1)

    # Simulate a re-translate / manual edit: the file no longer matches the
    # journaled output_hash, so resume must re-optimize it (not skip).
    qfile = qroot / "snowflake" / "tpcds" / "query_01.sql"
    qfile.write_text("SELECT 9\n", encoding="utf-8")
    calls = {"n": 0}

    def again(_prompt):
        calls["n"] += 1
        return "SELECT 2"

    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: again)
    op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1)
    assert calls["n"] == 1  # not skipped — the file had drifted from the journal


def test_queries_filter_processes_only_named_and_preserves_journal(tmp_path, monkeypatch):
    # Two queries; pre-seed a journal entry for the one we will NOT target, then
    # run a scoped --queries on the other. The untargeted query's journal record
    # must survive (merge, not overwrite).
    qroot = tmp_path / "queries"
    (qroot / "snowflake" / "tpcds").mkdir(parents=True)
    (qroot / "snowflake" / "tpcds" / "query_01.sql").write_text("SELECT 1\n", encoding="utf-8")
    (qroot / "snowflake" / "tpcds" / "query_02.sql").write_text("SELECT 2\n", encoding="utf-8")
    (tmp_path / "results").mkdir()
    monkeypatch.setattr(op, "QUERIES_ROOT", qroot)
    # Seed a prior journal record for q01 (status unchanged).
    op._append_progress(
        "snowflake",
        {"query": "x", "status": "unchanged", "query_id": "tpcds_q01", "output_hash": "deadbeef"},
        op.threading.Lock(),
    )

    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: lambda _p: "SELECT 99")
    n = op.optimize_for_platform(
        "snowflake", backend="claude-cli", jobs=1, queries=frozenset({"tpcds_q02"})
    )

    assert n == 1
    # Only q02 was rewritten; q01 untouched on disk.
    assert (qroot / "snowflake" / "tpcds" / "query_02.sql").read_text() == "SELECT 99\n"
    assert (qroot / "snowflake" / "tpcds" / "query_01.sql").read_text() == "SELECT 1\n"
    # Journal still has BOTH q01 (preserved) and q02 (new).
    journal = op._load_progress("snowflake")
    assert set(journal) == {"tpcds_q01", "tpcds_q02"}
    assert journal["tpcds_q01"]["status"] == "unchanged"  # preserved
    assert journal["tpcds_q02"]["status"] == "rewritten"


def test_parse_gate_skipped_when_original_unparseable(tmp_path, monkeypatch):
    # If sqlglot can't parse the ORIGINAL (a dialect gap, not a bad rewrite), the
    # gate is disabled so a valid optimization isn't rejected for the wrong reason.
    qroot = _seed_query(tmp_path, monkeypatch, "SELECT 1\n")
    monkeypatch.setattr(op, "_sql_parse_error", lambda _sql, _dialect: "simulated dialect gap")
    monkeypatch.setattr(op, "_make_rewriter", lambda *a, **k: lambda _p: "SELECT 2")

    n = op.optimize_for_platform("snowflake", backend="claude-cli", jobs=1)
    assert n == 1  # accepted via looks_like_sql only; not gated on sqlglot
    assert (qroot / "snowflake" / "tpcds" / "query_01.sql").read_text() == "SELECT 2\n"
