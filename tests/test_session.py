"""F1 — persistent session + keepalive.

Covers the DiagnosticService.session() lifecycle, the keepalive ticker, the
half-duplex serialization guarantee, the client's keepalive() method, and the
TUI reusing one long-lived session across reads.
"""

import asyncio
import threading
import time

import pytest

from trecu.protocol.common import ConnectionInfo, EcuInfo, ProtocolError
from trecu.protocol.iso9141 import Iso9141Client
from trecu.service import DiagnosticService, as_transport_factory
from trecu.transport.mock_obd import MockObdTransport

from mock_ecus import FAIL_FAST, FailingObdTransport


class SpyClient:
    """Stand-in protocol client that counts what the service asks of it.

    Implements every member the service calls on a client. Those calls are
    unguarded, so a spy that drifts from the real client's surface fails as an
    AttributeError rather than a readable assertion.
    """

    def __init__(self):
        self.connects = 0
        self.reads = 0
        self.keepalives = 0
        self.stops = 0
        self.shutdown_order = []

    def connect(self) -> ConnectionInfo:
        self.connects += 1
        return ConnectionInfo(key_bytes=b"\x01\x02", session_started=True)

    def read_dtcs(self):
        self.reads += 1
        return [(0x01, 0x07, 0x08)]  # -> P0107

    def read_identification(self) -> EcuInfo:
        return EcuInfo()

    def read_live(self, pids):
        return {}

    def clear_dtcs(self) -> None:
        pass

    def keepalive(self) -> None:
        self.keepalives += 1

    def stop_communication(self) -> None:
        self.stops += 1
        self.shutdown_order.append("communication")


def _spy_service(spy: SpyClient) -> DiagnosticService:
    # The transport is inert here — the spy client ignores it entirely.
    return DiagnosticService(MockObdTransport(), client=spy)


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
    assert spy.shutdown_order == ["communication"]
    assert [d.code for d in r1.dtcs] == ["P0107"]
    assert r2.count == 1


def test_service_can_reconnect_after_close():
    spy = SpyClient()
    svc = _spy_service(spy)

    with svc.session(keepalive_interval=0):
        assert svc.read_faults().count == 1
    assert svc._active is None
    assert svc._active_info is None

    with svc.session(keepalive_interval=0):
        assert svc.read_faults().count == 1

    assert spy.connects == 2
    assert spy.stops == 2


# -- the device comes from a factory -----------------------------------------
def test_reconnect_after_close_builds_a_fresh_transport():
    """close() releases the device; the next session builds its own."""
    built = []

    def factory():
        t = MockObdTransport()
        built.append(t)
        return t

    svc = DiagnosticService(factory)
    with svc.session(keepalive_interval=0):
        assert svc.read_faults().count == 1
        assert svc.transport is built[0]
    assert svc.transport is None  # released with the session

    with svc.session(keepalive_interval=0):
        assert svc.read_faults().count == 1
        assert svc.transport is built[1]  # a fresh device, not the closed one
    assert len(built) == 2


def test_a_transport_instance_binds_one_device_for_every_session():
    """Handing over an instance pins it — the ``--mock`` TUI's shared ECU."""
    transport = MockObdTransport()
    svc = DiagnosticService(transport)
    with svc.session(keepalive_interval=0):
        svc.clear_faults()
    # A second session over the same device still sees the cleared ECU, the way
    # a real bike retains state between connects.
    with svc.session(keepalive_interval=0):
        assert svc.transport is transport
        assert svc.read_faults().count == 0


def test_a_service_that_never_opens_never_builds_a_device():
    svc = DiagnosticService(
        lambda: pytest.fail("the factory must not be called")
    )
    svc.close()
    assert svc.transport is None


def test_as_transport_factory_rejects_a_non_transport():
    with pytest.raises(TypeError):
        as_transport_factory("/dev/cu.usbserial-3")


def test_close_waits_for_in_flight_io_before_shutdown():
    entered = threading.Event()
    release = threading.Event()

    class BlockingSpy(SpyClient):
        def read_dtcs(self):
            entered.set()
            assert release.wait(timeout=2)
            return super().read_dtcs()

    spy = BlockingSpy()
    svc = _spy_service(spy)
    svc.start_session(keepalive_interval=0)
    reader = threading.Thread(target=svc.read_faults)
    closer = threading.Thread(target=svc.close)
    reader.start()
    assert entered.wait(timeout=1)
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()
    assert spy.shutdown_order == []

    release.set()
    reader.join(timeout=2)
    closer.join(timeout=2)
    assert not reader.is_alive()
    assert not closer.is_alive()
    assert spy.shutdown_order == ["communication"]


def test_start_session_connect_failure_closes_transport():
    # The 5-baud init fails on every attempt, so connect() gives up and raises.
    t = FailingObdTransport()
    svc = DiagnosticService(t, FAIL_FAST)
    with pytest.raises(ProtocolError):
        svc.start_session(keepalive_interval=0)
    assert t.closes >= 1              # transport released despite the failure
    assert svc.transport is None      # ... and the device dropped with it
    assert svc._keepalive is None     # no ticker left dangling


# -- keepalive ticker --------------------------------------------------------
def test_keepalive_fires_on_interval():
    spy = SpyClient()
    with _spy_service(spy).session(keepalive_interval=0.02):
        time.sleep(0.15)
    assert spy.keepalives >= 2        # ticker beat several times while idle


def test_keepalive_beat_count_is_readable_from_another_thread():
    """``beats`` is written by the ticker and read by whoever asks, so it goes
    through a lock — a bare ``+= 1`` on an int attribute is not atomic."""
    spy = SpyClient()
    svc = _spy_service(spy)
    svc.start_session(keepalive_interval=0.02)
    ticker = svc._keepalive
    try:
        time.sleep(0.15)
        assert ticker.beats >= 2      # readable mid-run, from the main thread
    finally:
        svc.close()                   # joins the ticker, so the count settles
    assert ticker.beats == spy.keepalives  # every beat that ran was counted


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
        MockObdTransport(), client=Flaky(), logger=logs.append
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
def test_iso9141_keepalive_pokes_link():
    t = MockObdTransport()
    t.open()
    client = Iso9141Client(t)
    client.connect()
    client.keepalive()               # cheap Mode 01 PID 00 poke, no raise
    assert client.read_dtcs() == [(0x11, 0x08, 0x08)]
    t.close()


# -- TUI owns one long-lived session -----------------------------------------
def test_tui_reuses_one_session_across_reads(mock_app, wait_for):
    builds = {"n": 0}

    def factory():
        builds["n"] += 1
        return MockObdTransport()

    app = mock_app(factory)

    table_rows = lambda: app.query_one("#dtcs").row_count  # noqa: E731

    async def scenario():
        async with app.run_test() as pilot:
            # Auto-read on mount builds the session; the re-read reuses it.
            await wait_for(lambda: table_rows() == 1, pilot.pause)
            await pilot.press("r")
            await wait_for(lambda: app._ecu.connected, pilot.pause)
            assert table_rows() == 1  # the default P1108

    asyncio.run(scenario())
    assert builds["n"] == 1           # transport built once -> one session
