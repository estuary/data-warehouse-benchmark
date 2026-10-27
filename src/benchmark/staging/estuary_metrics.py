"""Estuary OpenMetrics API client — pipeline-side task telemetry.

Estuary exposes per-task counters in Prometheus/OpenMetrics text format at
``{ESTUARY_OPENMETRICS_URL}/{prefix}/`` (docs.estuary.dev/reference/openmetrics-api),
authenticated with the same refresh token flowctl uses (as a Bearer header).
This gives capture/materialization progress *from the pipeline's side* —
``captured_out_docs_total`` per target collection and
``materialized_out_docs_total`` per source collection — without connecting to
any destination warehouse. That complements ``validate-loads`` (which asserts
destination-side row counts): during a backfill you can watch documents move
even for engines the local machine can't reach.

Presentation-free by design: the CLI layer (``flow-metrics``) owns rendering.
"""

from __future__ import annotations

import logging
import re

import requests
from pydantic import BaseModel

from ..settings import get_settings
from .estuary import FlowctlError

logger = logging.getLogger("benchmark.staging.estuary_metrics")

# The doc-count counters that describe pipeline throughput, in display order.
# Bytes counters and the last-published gauge exist too but doc counts are what
# row-count validation reasons about.
DOC_METRICS: tuple[str, ...] = (
    "captured_out_docs_total",
    "materialized_out_docs_total",
)

_SAMPLE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)\{(?P<labels>.*)\}\s+(?P<value>\S+)")
_LABEL_RE = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<val>(?:\\.|[^"\\])*)"')


class MetricSample(BaseModel):
    """One OpenMetrics sample: ``name{labels} value``."""

    name: str
    labels: dict[str, str]
    value: float

    @property
    def task(self) -> str:
        """The task catalog name (capture or materialization), if labeled."""
        return self.labels.get("task", "")

    @property
    def collection(self) -> str:
        """The collection this sample is about — captures label their target
        collection, materializations their source collection."""
        return self.labels.get("collection", self.labels.get("source_collection", ""))


def fetch_openmetrics(prefix: str | None = None, *, timeout: int = 30) -> str:
    """GET the OpenMetrics text for ``prefix`` (default: settings ACCOUNT).

    Raises FlowctlError on a missing token or a non-200 response.
    """
    settings = get_settings().ESTUARY
    token = settings.REFRESH_TOKEN
    if token is None:
        raise FlowctlError("ESTUARY_REFRESH_TOKEN missing — required for the OpenMetrics API")
    prefix = (prefix or settings.ACCOUNT).strip("/")
    if not prefix:
        raise FlowctlError("no OpenMetrics prefix — set ESTUARY_ACCOUNT or pass one explicitly")
    url = f"{settings.OPENMETRICS_URL.rstrip('/')}/{prefix}/"
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {token.get_secret_value()}"},
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise FlowctlError(
            f"OpenMetrics API returned {resp.status_code} for prefix {prefix!r}: {resp.text[:300]}"
        )
    return resp.text


def parse_samples(text: str) -> list[MetricSample]:
    """Parse OpenMetrics text into samples (skips comments/TYPE/HELP lines)."""
    samples: list[MetricSample] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if not m:
            continue
        try:
            value = float(m["value"])
        except ValueError:
            continue
        labels = {lm["key"]: lm["val"] for lm in _LABEL_RE.finditer(m["labels"])}
        samples.append(MetricSample(name=m["name"], labels=labels, value=value))
    return samples


def doc_totals(
    prefix: str | None = None,
    *,
    task_filter: str | None = None,
    collection_filter: str | None = None,
) -> list[MetricSample]:
    """Fetch + parse, returning the doc-count counters (optionally substring-
    filtered on task / collection), sorted by task then collection."""
    samples = [s for s in parse_samples(fetch_openmetrics(prefix)) if s.name in DOC_METRICS]
    if task_filter:
        samples = [s for s in samples if task_filter in s.task]
    if collection_filter:
        samples = [s for s in samples if collection_filter in s.collection]
    return sorted(samples, key=lambda s: (s.task, s.collection, s.name))
