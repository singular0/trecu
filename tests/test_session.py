"""F1 — persistent session + TesterPresent keepalive.

Covers the DiagnosticService.session() lifecycle, the keepalive ticker, the
half-duplex serialization guarantee, both clients' keepalive() methods, and the
TUI reusing one long-lived session across reads.
"""

import asyncio
import threading
import time

import pytest

from trecu.protocol.iso9141 import Iso9141Client
from trecu.protocol.kwp2000 import (
    ConnectionInfo,
    EcuInfo,
    Kwp2000Client,
    ProtocolError,
)
from trecu.service import DiagnosticService
from trecu.transport.mock_kline import MockKLineTransport
from trecu.transport.mock_obd import MockObdTransport


class SpyClient:
    """Duck-typed protocol client that counts what the service asks of it."""

    def __init__(self):
        self.connects = 0
        self.reads = 0
        self.keepalives = 0
        self.stops = 0

    def connect(self) -> ConnectionInfo:
        self.connects += 1
        return ConnectionInfo(key_bytes=b"\x01\x02", session_started=True)

    def read_dtcs(self):
        self.reads += 1
        return [(0x01, 0x07, 0x08)]  # -> P0107

    def read_identification(self) -> EcuInfo:
        return EcuInfo()

    def clear_dtcs(self) -> None:
        pass

    def keepalive(self) -> None:
        self.keepalives += 1

    def stop_communication(self) -> None:
        self.stops += 1


def _spy_service(spy: SpyClient) -> DiagnosticService:
    # The transport is inert here — the spy client ignores it entirely.
    return DiagnosticService(MockKLineTransport(), client=spy)


# -- session lifecycle -------------------------------------------------------
def test_session_connects_once_and_reuses_across_operations():
    spy = SpyClient()
    with _spy_service(spy).session(keepalive_interval=0) as svc:  # ticker off
        r1 = svc.read_faults()
        r2 = svc.read_faults()
        svc.clear_faults()
    assert spy.connects == 1          # connected once; the connection persists
    assert spy.reads == 2
    assert spy.stops == 1             # stop_communication on close
    assert [d.code for d in r1.dtcs] == ["P0107"]
    assert r2.count == 1


def test_start_session_connect_failure_closes_transport():
    # iso9141 refuses a transport that can't do 5-baud init, so connect fails.
    t = MockKLineTransport()  # supports_slow_init is False
    svc = DiagnosticService(t, protocol="iso9141")
    with pytest.raises(ProtocolError):
        svc.start_session(keepalive_interval=0)
    assert t._open is False           # transport released despite the failure
    assert svc._keepalive is None     # no ticker left dangling


# -- keepalive ticker --------------------------------------------------------
def test_keepalive_fires_on_interval():
    spy = SpyClient()
    with _spy_service(spy).session(keepalive_interval=0.02):
        time.sleep(0.15)
    assert spy.keepalives >= 2        # ticker beat several times while idle


def test_keepalive_serialized_behind_io_lock():
    """A beat can't touch the wire while a real operation holds the lock."""
    spy = SpyClient()
    svc = _spy_service(spy)
    svc.start_session(keepalive_interval=0.02)
    try:
        with svc._io_lock:            # stand in for an in-flight read/clear
            time.sleep(0.1)
            assert spy.keepalives == 0  # blocked — half-duplex serialization
        time.sleep(0.1)
        assert spy.keepalives > 0     # fires once the wire is free again
    finally:
        svc.close()


def test_close_stops_the_keepalive_thread():
    spy = SpyClient()
    svc = _spy_service(spy)
    svc.start_session(keepalive_interval=0.02)
    time.sleep(0.06)
    svc.close()
    settled = spy.keepalives
    time.sleep(0.1)
    assert spy.keepalives == settled  # no beats after close
    assert spy.stops == 1
    assert not any(
        t.name == "trecu-keepalive" for t in threading.enumerate()
    )


def test_keepalive_disabled_when_interval_zero():
    spy = SpyClient()
    svc = _spy_service(spy)
    svc.start_session(keepalive_interval=0)
    try:
        assert svc._keepalive is None
        time.sleep(0.05)
        assert spy.keepalives == 0
    finally:
        svc.close()


def test_keepalive_failure_is_logged_not_fatal():
    logs = []

    class Flaky(SpyClient):
        def keepalive(self):
            raise ProtocolError("boom")

    svc = DiagnosticService(
        MockKLineTransport(), client=Flaky(), logger=logs.append
    )
    svc.start_session(keepalive_interval=0.02)
    try:
        time.sleep(0.1)
        # Still usable after keepalive failures; the loop kept ticking.
        assert svc.read_faults().count == 1
        assert any("keepalive failed" in m for m in logs)
    finally:
        svc.close()


# -- client keepalive methods ------------------------------------------------
def test_kwp_keepalive_sends_tester_present():
    t = MockKLineTransport()
    t.open()
    client = Kwp2000Client(t)
    client.start_communication()
    client.keepalive()               # TesterPresent, response suppressed
    # Session still works afterwards.
    assert client.read_dtcs()
    t.close()


def test_iso9141_keepalive_pokes_link():
    t = MockObdTransport()
    t.open()
    client = Iso9141Client(t)
    client.connect()
    client.keepalive()               # cheap Mode 01 PID 00 poke, no raise
    assert client.read_dtcs() == [(0x11, 0x08, 0x08)]
    t.close()


# -- TUI owns one long-lived session -----------------------------------------
def test_tui_reuses_one_session_across_reads():
    from trecu.tui.app import TrecuApp

    builds = {"n": 0}

    def factory():
        builds["n"] += 1
        return MockKLineTransport()

    app = TrecuApp(
        transport_factory=factory, mock=True, port="mock", keepalive_interval=0
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)    # auto-read on mount builds the session
            await pilot.press("r")    # re-read reuses it
            await pilot.pause(0.3)
            assert app.query_one("#dtcs").row_count == 3
            assert app._session is not None

    asyncio.run(scenario())
    assert builds["n"] == 1           # transport built once -> one session
