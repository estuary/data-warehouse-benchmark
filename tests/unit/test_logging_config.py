"""The audit log also carries the library records the harness relies on."""

from __future__ import annotations

import json
import logging

from benchmark.logging_config import AUDITED_LIBRARY_LOGGERS, configure_logging


def test_databricks_retry_log_reaches_the_audit_file(tmp_path):
    """A throttled Databricks request (HTTP 429) is logged by the connector at
    INFO under its own logger; it must land in the JSON audit log, and a second
    configure must not write it twice."""
    configure_logging(log_dir=tmp_path, run_label="first")
    path = configure_logging(log_dir=tmp_path, run_label="second")
    assert "databricks.sql.auth.retry" in AUDITED_LIBRARY_LOGGERS

    logging.getLogger("databricks.sql.auth.retry").info("Received status code 429 for POST request")
    for handler in logging.getLogger("benchmark").handlers:
        handler.flush()

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    throttled = [e for e in lines if "status code 429" in e.get("message", "")]
    assert len(throttled) == 1
