"""Fixed-capacity BigQuery Enterprise reservation management for the slot sweep.

Snowflake's warehouse sizes are pre-created and selected per connection, so its
sizing sweep needs no infra changes. BigQuery is different: a single project-wide
reservation's ``slot_capacity`` is the only slot knob, so to sweep fixed slot
capacities we patch that reservation's baseline between steps via the Reservation
API (no ``terraform apply`` in the loop), wait for it to go live, then benchmark.

**Fixed capacity** = a baseline ``slot_capacity`` with **no autoscale** and
``ignore_idle_slots=True`` — the latter so a query can't borrow idle slots and
exceed the provisioned N, which would contaminate an "N slots" measurement.
Baseline slots must be a **multiple of 50** (BigQuery editions constraint), 50
minimum.

This manages the reservation **out-of-band from terraform** for the duration of a
sweep (so it drifts from terraform state); call :meth:`teardown` at the end to
stop billing. Requires the runner service account to hold
``roles/bigquery.resourceAdmin``.

The heavy ``google-cloud-bigquery-reservation`` import is deferred into methods so
importing this module (e.g. for the CLI surface or tests) never requires the SDK.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..exceptions import ConnectorError
from ..settings import get_settings

logger = logging.getLogger("benchmark.compute.bigquery_reservation")

# Matches terraform/modules/bigquery/main.tf so the API-managed reservation is the
# same object terraform would create (name + project-level QUERY assignment).
RESERVATION_ID = "enterprise-benchmark-reservation"
SLOT_INCREMENT = 50
MIN_SLOTS = 50


def validate_slots(slots: int) -> int:
    """Return ``slots`` if it's a valid fixed baseline, else raise ``ValueError``.

    BigQuery editions require baseline slots to be a multiple of 50; 50 is the
    smallest useful reservation (0 means idle-only, effectively on-demand).
    """
    if slots < MIN_SLOTS or slots % SLOT_INCREMENT != 0:
        raise ValueError(
            f"BigQuery slots must be a multiple of {SLOT_INCREMENT} and "
            f">= {MIN_SLOTS} (got {slots})"
        )
    return slots


class BigQueryReservationManager:
    """Create / resize / tear down the fixed-capacity Enterprise reservation."""

    def __init__(self, project: str, location: str, credentials: Any | None = None) -> None:
        self.project = project
        self.location = location
        self._credentials = credentials
        self._client: Any | None = None

    @classmethod
    def from_settings(cls) -> BigQueryReservationManager:
        """Build from ``BENCHMARK_BIGQUERY_*`` settings + the runner SA key."""
        bq = get_settings().BIGQUERY
        if not bq.PROJECT:
            raise ConnectorError("BIGQUERY_PROJECT missing — cannot manage a reservation")
        credentials = None
        if bq.GCP_SA_KEY:
            import os

            from google.oauth2 import service_account

            credentials = service_account.Credentials.from_service_account_file(
                os.path.expanduser(str(bq.GCP_SA_KEY))
            )
        return cls(bq.PROJECT, bq.REGION or "us-central1", credentials)

    # -- internals ----------------------------------------------------------
    def _reservation_client(self) -> Any:
        if self._client is None:
            from google.cloud import bigquery_reservation_v1 as bqr

            self._client = bqr.ReservationServiceClient(credentials=self._credentials)
        return self._client

    @property
    def parent(self) -> str:
        return f"projects/{self.project}/locations/{self.location}"

    @property
    def reservation_name(self) -> str:
        return f"{self.parent}/reservations/{RESERVATION_ID}"

    # -- operations ---------------------------------------------------------
    def live_slots(self) -> int | None:
        """The reservation's currently-configured ``slot_capacity``, or ``None``
        if the reservation doesn't exist."""
        from google.api_core.exceptions import NotFound

        client = self._reservation_client()
        try:
            return int(client.get_reservation(name=self.reservation_name).slot_capacity)
        except NotFound:
            return None

    def ensure(self, slots: int) -> None:
        """Create-or-resize the reservation to exactly ``slots`` fixed baseline
        slots (no autoscale, ``ignore_idle_slots=True``), and make sure the
        project-level QUERY assignment exists so queries route to it."""
        validate_slots(slots)
        from google.api_core.exceptions import NotFound
        from google.cloud import bigquery_reservation_v1 as bqr
        from google.protobuf import field_mask_pb2

        client = self._reservation_client()
        # No `autoscale` field ⇒ fixed capacity. ignore_idle_slots=True caps the
        # reservation at exactly `slots` (can't borrow idle capacity).
        reservation = bqr.Reservation(
            slot_capacity=slots,
            ignore_idle_slots=True,
            edition=bqr.Edition.ENTERPRISE,
        )
        try:
            client.get_reservation(name=self.reservation_name)
            reservation.name = self.reservation_name
            client.update_reservation(
                reservation=reservation,
                update_mask=field_mask_pb2.FieldMask(paths=["slot_capacity", "ignore_idle_slots"]),
            )
            logger.info("resized BigQuery reservation to %d slots", slots)
        except NotFound:
            client.create_reservation(
                parent=self.parent,
                reservation_id=RESERVATION_ID,
                reservation=reservation,
            )
            logger.info("created BigQuery reservation with %d slots", slots)
        self._ensure_assignment()

    def _ensure_assignment(self) -> None:
        """Idempotently create the project-wide QUERY assignment."""
        from google.cloud import bigquery_reservation_v1 as bqr

        client = self._reservation_client()
        assignee = f"projects/{self.project}"
        for a in client.list_assignments(parent=self.reservation_name):
            if a.assignee == assignee and a.job_type == bqr.Assignment.JobType.QUERY:
                return
        client.create_assignment(
            parent=self.reservation_name,
            assignment=bqr.Assignment(assignee=assignee, job_type=bqr.Assignment.JobType.QUERY),
        )
        logger.info("created project QUERY assignment for the reservation")

    def wait_until_live(self, slots: int, timeout_s: int = 300, poll_s: int = 10) -> None:
        """Block until the reservation reports ``slots`` (resize isn't instant),
        then a short settle so query routing reflects the new capacity."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self.live_slots() == slots:
                time.sleep(poll_s)  # settle: let query routing pick up the change
                logger.info("BigQuery reservation live at %d slots", slots)
                return
            time.sleep(poll_s)
        raise ConnectorError(
            f"BigQuery reservation did not reach {slots} slots within {timeout_s}s"
        )

    def wait_until_routed(self, timeout_s: int = 600, poll_s: int = 20) -> bool:
        """Block until queries actually ROUTE to the reservation, not just until it
        exists. Creating the project QUERY assignment is not instant — BigQuery takes
        minutes to propagate routing, so queries submitted right after
        ``wait_until_live`` run **on-demand** until it takes effect (observed: the
        first ~20 warm queries billed on-demand before routing engaged). Probe with a
        slot-consuming query and inspect its ``reservation_id`` until it lands on the
        reservation. Returns True once confirmed; on timeout logs a warning and returns
        False (continue-on-failure — the run proceeds, early queries may be on-demand).
        The probe scans no bytes (pure compute over GENERATE_ARRAY) so it's ~free
        whether it lands on-demand or on the reservation.
        """
        from google.cloud import bigquery

        client = bigquery.Client(
            project=self.project, credentials=self._credentials, location=self.location
        )
        # 1M elements — under BigQuery's GENERATE_ARRAY cap (~1.05M); pure compute,
        # scans no bytes, so it's ~free whether it lands on-demand or on the reservation.
        probe = "SELECT SUM(x) FROM UNNEST(GENERATE_ARRAY(1, 1000000)) AS x"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                job = client.query(probe)
                job.result()
                rid = getattr(job, "reservation_id", None)
            except Exception:  # a probe hiccup must never abort the run
                logger.warning("BigQuery routing probe query failed; retrying", exc_info=True)
                rid = None
            if rid and RESERVATION_ID in rid:
                logger.info("BigQuery query routing live on reservation (%s)", rid)
                return True
            logger.info(
                "waiting for BigQuery query routing to reservation (currently %s)",
                rid or "on-demand",
            )
            time.sleep(poll_s)
        logger.warning(
            "BigQuery query routing not confirmed within %ds; early queries may run on-demand",
            timeout_s,
        )
        return False

    def teardown(self) -> None:
        """Delete the assignment(s) + reservation to stop billing. Idempotent."""
        from google.api_core.exceptions import NotFound

        client = self._reservation_client()
        try:
            for a in client.list_assignments(parent=self.reservation_name):
                client.delete_assignment(name=a.name)
            client.delete_reservation(name=self.reservation_name)
            logger.info("tore down BigQuery reservation + assignment")
        except NotFound:
            logger.info("BigQuery reservation already absent — nothing to tear down")
