"""Phase 3 — live sensor streaming, end to end against the mock ECUs.

Covers both duck-typed clients' read_live (OBD Mode 01 per PID / the KWP
packed 21 80 kwp_local frame), the DiagnosticService.read_live decode +
ordering, the mocks emitting *moving* values, and the TUI's poll loop
populating the Live Data tab.
"""

import asyncio

from trecu.protocol.iso9141 import Iso9141Client
from trecu.protocol.kwp2000 import Kwp2000Client
from trecu.service import DEFAULT_LIVE_PIDS, DiagnosticService
from trecu.transport.mock_kline import MockKLineTransport
from trecu.transport.mock_obd import MockObdTransport


# -- client-level read_live --------------------------------------------------
def test_iso9141_read_live_returns_data_bytes():
    t = MockObdTransport()
    t.open()
    client = Iso9141Client(t)
    client.connect()
    raw = client.read_live([0x0C, 0x05])  # RPM (2 bytes), coolant (1 byte)
    assert set(raw) == {0x0C, 0x05}
    assert len(raw[0x0C]) == 2 and len(raw[0x05]) == 1
    t.close()


def test_kwp_read_live_returns_packed_frame():
    t = MockKLineTransport()
    t.open()
    client = Kwp2000Client(t)
    client.start_communication()
    # A Keihin serves all sensors as one frame on LID 0x80; any other record
    # is rejected and therefore omitted.
    raw = client.read_live([0x80, 0x0C])
    assert set(raw) == {0x80}
    assert len(raw[0x80]) == 106  # 53 draft channels x 2 bytes
    t.close()


def test_read_live_omits_unmodelled_pids():
    t = MockObdTransport()
    t.open()
    client = Iso9141Client(t)
    client.connect()
    raw = client.read_live([0x0C, 0xEE])  # 0xEE isn't modelled by the mock
    assert 0x0C in raw and 0xEE not in raw
    t.close()


# -- service-level decode + ordering -----------------------------------------
def test_service_read_live_decodes_default_set_in_order():
    with DiagnosticService(MockObdTransport(), protocol="iso9141") as svc:
        readings = svc.read_live()
    assert [r.pid for r in readings] == list(DEFAULT_LIVE_PIDS)
    by_pid = {r.pid: r for r in readings}
    assert by_pid[0x0C].unit == "rpm"
    assert 0 <= by_pid[0x0C].value <= 16384
    assert -40 <= by_pid[0x05].value <= 215        # coolant in range
    assert 0 <= by_pid[0x11].value <= 100          # throttle %
    assert 0 <= by_pid[0x42].value <= 65.535       # battery volts


def test_service_read_live_custom_pids():
    with DiagnosticService(MockObdTransport(), protocol="iso9141") as svc:
        readings = svc.read_live([0x05])
    assert len(readings) == 1 and readings[0].pid == 0x05


def test_kwp_service_read_live_decodes_packed_frame():
    # The KWP path reads one 21 80 frame and splits it per the kwp_local
    # table; pids=None means every channel.
    with DiagnosticService(MockKLineTransport(), protocol="kwp-fast") as svc:
        readings = svc.read_live()
        second = {r.pid: r.value for r in svc.read_live()}
    by_ch = {r.pid: r for r in readings}
    assert len(readings) == 53
    assert 1 <= by_ch[5].value <= 5                    # gear
    assert by_ch[3].unit == "°C" and 86 <= by_ch[3].value <= 94   # water temp, -25 offset
    assert by_ch[50].unit == "V" and 13.3 <= by_ch[50].value <= 14.3  # battery, +8 V offset
    assert by_ch[17].value == 0                        # MIL off (raw 1, -1 offset)
    assert by_ch[66].value == -1024                    # unmodelled slot -> offset
    # The frame is alive: at least one channel moved between snapshots.
    assert any(second[p] != by_ch[p].value for p in second)


def test_kwp_service_read_live_honors_channel_filter():
    with DiagnosticService(MockKLineTransport(), protocol="kwp-fast") as svc:
        readings = svc.read_live([50, 3])
    assert [r.pid for r in readings] == [50, 3]


# -- mocks emit *moving* values ----------------------------------------------
def test_mock_values_move_between_reads():
    with DiagnosticService(MockObdTransport(), protocol="iso9141") as svc:
        first = {r.pid: r.value for r in svc.read_live()}
        second = {r.pid: r.value for r in svc.read_live()}
    # At least one sensor visibly changed — the stream isn't dead.
    assert any(first[p] != second[p] for p in first)


# -- TUI poll loop populates the Live Data tab -------------------------------
def test_tui_live_tab_streams():
    from trecu.tui.app import TrecuApp

    # A small PID set keeps one snapshot fast (each PID is a real, paced K-line
    # round-trip in the mock, ~0.1 s), so the poll completes within the pause.
    live_pids = [0x0C, 0x05, 0x11]
    app = TrecuApp(
        transport_factory=lambda: MockObdTransport(),
        mock=True,
        port="mock",
        protocol="iso9141",
        keepalive_interval=0,
        poll_interval=0.05,
        live_pids=live_pids,
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.3)          # auto-read on mount builds the session
            app.action_show_tab("tab-live")  # entering Live Data starts polling
            await pilot.pause(0.8)           # let a full snapshot land
            table = app.query_one("#live")
            assert table.row_count == len(live_pids)
            assert app._streaming is True
            # Leaving the tab pauses the stream.
            app.action_show_tab("tab-faults")
            await pilot.pause(0.1)
            assert app._streaming is False

    asyncio.run(scenario())
