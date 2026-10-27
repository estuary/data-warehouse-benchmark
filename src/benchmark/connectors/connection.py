"""Retry-aware connection establishment shared across the harness.

The concurrency pass opens many connections at once (one per worker thread); on a
flaky network that connect storm produces transient ``Network is unreachable`` /
``Could not connect ... after 1 attempt`` failures, and the engine connectors make a
single connect attempt with no backoff, so one such failure would abort a whole pass.

``connect_with_retry`` centralizes the CLAUDE.md error policy for connects:
exponential backoff with jitter (≈2s, 8s, 32s), a bounded number of attempts, a
fresh connector per attempt (a half-open connector can be poisoned), and an
optional semaphore so simultaneous connect handshakes stay throttled. It's engine
-agnostic — it drives any ``WarehouseConnector`` via its ``connect`` / ``disconnect``
contract — so the warm runner, the concurrency worker pool, and one-off probes can
all share one hardened path.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable

from ..exceptions import ConnectorError
from .base import WarehouseConnector

logger = logging.getLogger("benchmark.connectors.connection")

DEFAULT_ATTEMPTS = 4  # 1 initial + 3 retries → backoffs of ~2s, 8s, 32s
DEFAULT_BASE_DELAY_S = 2.0


def _backoff_delay(attempt: int, base_delay: float) -> float:
    """Equal-jitter exponential backoff for 0-based ``attempt``.

    Nominal delay is ``base_delay × 4**attempt`` (2s, 8s, 32s); jitter spreads a
    thundering herd of simultaneous reconnects so they don't all retry in lockstep.
    """
    nominal = base_delay * (4**attempt)
    return random.uniform(nominal / 2, nominal)


def connect_with_retry(
    factory: Callable[[], WarehouseConnector],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    throttle: threading.Semaphore | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> WarehouseConnector:
    """Return a connected connector, retrying transient connect failures.

    Args:
        factory: builds a FRESH connector each call — a failed connect may leave a
            connector half-open, so every attempt starts clean.
        attempts: total tries (default 4 = 1 + 3 retries).
        base_delay: first backoff base in seconds (grows ×4 per attempt).
        throttle: optional semaphore held only around the connect handshake, so N
            worker threads don't all open sockets at once (the connect storm).
        sleep: injectable for tests.

    Raises:
        ConnectorError: every attempt failed; chained to the last underlying error.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last_exc: Exception | None = None
    for attempt in range(attempts):
        connector = factory()
        try:
            if throttle is not None:
                with throttle:
                    connector.connect()
            else:
                connector.connect()
            return connector
        except Exception as exc:  # any connect failure is a retry candidate
            last_exc = exc
            # Best-effort close so a half-open connector doesn't leak a socket.
            try:
                connector.disconnect()
            except Exception:
                logger.debug("disconnect of half-open connector failed", exc_info=True)
            if attempt + 1 >= attempts:
                break
            delay = _backoff_delay(attempt, base_delay)
            logger.warning(
                "connect attempt %d/%d failed; retrying in %.1fs: %s",
                attempt + 1,
                attempts,
                delay,
                exc,
            )
            sleep(delay)
    raise ConnectorError(f"connect failed after {attempts} attempt(s): {last_exc}") from last_exc


#: Reconnect attempts after a timeout drops a session (1 + 2 retries, ~2 s then ~8 s).
RECONNECT_ATTEMPTS = 3


def reconnect_in_place(
    connector: WarehouseConnector,
    *,
    attempts: int = RECONNECT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY_S,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Reconnect ``connector`` itself, retrying with backoff.

    Unlike :func:`connect_with_retry`, which builds a fresh connector per attempt,
    this reuses the object: the runner and every concurrency worker hold a
    reference to it, so a replacement would not reach them. Sets
    ``connector.connection_lost`` to whether every attempt failed, so the caller
    can stop sending queries into a session that no longer exists.

    Returns:
        True when the connector is connected again.
    """
    for attempt in range(attempts):
        try:
            connector.connect()
        except Exception as exc:  # any connect failure is a retry candidate
            try:
                connector.disconnect()
            except Exception:
                logger.debug("disconnect of half-open connector failed", exc_info=True)
            if attempt + 1 >= attempts:
                logger.error("reconnect failed after %d attempt(s): %s", attempts, exc)
                break
            delay = _backoff_delay(attempt, base_delay)
            logger.warning(
                "reconnect attempt %d/%d failed; retrying in %.1fs: %s",
                attempt + 1,
                attempts,
                delay,
                exc,
            )
            sleep(delay)
        else:
            connector.connection_lost = False
            return True
    connector.connection_lost = True
    return False
