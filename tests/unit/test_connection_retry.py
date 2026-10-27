"""connect_with_retry: backoff, fresh-connector-per-attempt, throttle, exhaustion."""

from __future__ import annotations

import threading

import pytest

from benchmark.connectors.connection import connect_with_retry
from benchmark.exceptions import ConnectorError


class _FakeConnector:
    """Connects successfully only on/after ``succeed_on`` (1-based) attempt."""

    def __init__(self, seq: list[str], *, fail_times: int) -> None:
        self._seq = seq
        self._fail_times = fail_times
        self.connected = False

    def connect(self) -> None:
        self._seq.append("connect")
        if len([s for s in self._seq if s == "connect"]) <= self._fail_times:
            raise OSError("Network is unreachable")
        self.connected = True

    def disconnect(self) -> None:
        self._seq.append("disconnect")


def _no_sleep(_delay: float) -> None:
    pass


def test_succeeds_after_transient_failures():
    seq: list[str] = []
    made: list[_FakeConnector] = []

    def factory() -> _FakeConnector:
        c = _FakeConnector(seq, fail_times=2)  # each fresh one keys off shared seq
        made.append(c)
        return c

    conn = connect_with_retry(factory, sleep=_no_sleep)
    assert conn.connected
    # Fresh connector per attempt (poisoned half-open ones aren't reused).
    assert len(made) == 3
    # Each failed attempt is closed best-effort.
    assert seq.count("disconnect") == 2


def test_raises_connectorerror_after_exhausting_attempts():
    seq: list[str] = []
    with pytest.raises(ConnectorError, match="after 2 attempt"):
        connect_with_retry(
            lambda: _FakeConnector(seq, fail_times=99),
            attempts=2,
            sleep=_no_sleep,
        )
    assert seq.count("connect") == 2


def test_throttle_is_held_during_connect():
    sem = threading.Semaphore(1)
    seq: list[str] = []

    class _CheckThrottle(_FakeConnector):
        def connect(self) -> None:
            # The semaphore must be held (acquired) while connecting.
            assert not sem.acquire(blocking=False), "throttle not held during connect"
            super().connect()

    conn = connect_with_retry(
        lambda: _CheckThrottle(seq, fail_times=0), throttle=sem, sleep=_no_sleep
    )
    assert conn.connected
    # Released after — acquirable again.
    assert sem.acquire(blocking=False)


def test_backoff_delays_follow_2_8_32():
    seq: list[str] = []
    delays: list[float] = []
    with pytest.raises(ConnectorError):
        connect_with_retry(
            lambda: _FakeConnector(seq, fail_times=99),
            attempts=4,
            base_delay=2.0,
            sleep=delays.append,
        )
    # 3 backoffs between 4 attempts; jitter keeps each within (nominal/2, nominal).
    assert len(delays) == 3
    for d, nominal in zip(delays, [2.0, 8.0, 32.0], strict=True):
        assert nominal / 2 <= d <= nominal
