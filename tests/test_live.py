"""Phase 3 — live sensor streaming, end to end against the mock ECU.

Covers the client's read_live (one OBD Mode 01 request per PID), the
DiagnosticService.read_live decode + ordering, the mock emitting *moving*
values, and the TUI's poll loop populating the Live Data tab.
"""

import asyncio

from trecu.protocol.common import encode_pid_support_pages
from trecu.protocol.iso9141 import Iso9141Client
from trecu.service import DEFAULT_LIVE_PIDS, DiagnosticService
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


def test_read_live_omits_an_advertised_pid_that_stays_silent():
    """An advertised PID is asked for; if it doesn't answer it is simply absent
    from the snapshot. (A PID the ECU never advertised isn't asked at all —
    see ``test_pid_support.py``.)"""
    t = MockObdTransport()
    t.open()
    client = Iso9141Client(t)
    client.connect()
    # 0x03 is in the bike's bitmap, but the mock models no value for it.
    raw = client.read_live([0x0C, 0x03])
    assert 0x0C in raw and 0x03 not in raw
    assert b"\x01\x03" in t.requests  # it *was* requested
    t.close()


# -- service-level decode + ordering -----------------------------------------
def test_service_read_live_decodes_default_set_in_order():
    """The default set, in order, minus what this ECU never advertised.

    The mock carries the tested bike's real capability bitmap, which does not
    claim PID 42 (battery voltage) — so the default poll set comes back one
    sensor short, in the order asked for.
    """
    with DiagnosticService(MockObdTransport()) as svc:
        readings = svc.read_live()
    expected = [pid for pid in DEFAULT_LIVE_PIDS if pid != 0x42]
    assert [r.pid for r in readings] == expected
    by_pid = {r.pid: r for r in readings}
    assert by_pid[0x0C].unit == "rpm"
    assert 0 <= by_pid[0x0C].value <= 16384
    assert -40 <= by_pid[0x05].value <= 215        # coolant in range
    assert 0 <= by_pid[0x11].value <= 100          # throttle %


def test_battery_voltage_is_polled_when_the_ecu_advertises_it():
    """The default set is a wish list, not a fixed menu: an ECU that *does*
    claim PID 42 gets it polled (the tested Triumph simply doesn't)."""
    ecu = MockObdTransport(support_pages=encode_pid_support_pages(DEFAULT_LIVE_PIDS))
    with DiagnosticService(ecu) as svc:
        readings = svc.read_live()
    by_pid = {r.pid: r for r in readings}
    assert [r.pid for r in readings] == list(DEFAULT_LIVE_PIDS)
    assert 0 <= by_pid[0x42].value <= 65.535       # battery volts


def test_service_read_live_custom_pids():
    with DiagnosticService(MockObdTransport()) as svc:
        readings = svc.read_live([0x05])
    assert len(readings) == 1 and readings[0].pid == 0x05


# -- mocks emit *moving* values ----------------------------------------------
def test_mock_values_move_between_reads():
    with DiagnosticService(MockObdTransport()) as svc:
        first = {r.pid: r.value for r in svc.read_live()}
        second = {r.pid: r.value for r in svc.read_live()}
    # At least one sensor visibly changed — the stream isn't dead.
    assert any(first[p] != second[p] for p in first)


# -- TUI poll loop populates the Live Data tab -------------------------------
def test_tui_live_tab_streams(mock_app, wait_for):
    # A small PID set keeps the test fast while still exercising multiple real,
    # paced K-line round-trips through the mock.
    live_pids = [0x0C, 0x05, 0x11]
    app = mock_app(poll_interval=0.05, live_pids=live_pids)

    async def scenario():
        async with app.run_test() as pilot:
            # The mount-triggered read and live snapshots both run off-thread.
            # Wait on their observable results rather than assuming CI scheduling
            # will complete them within a fixed sleep.
            await wait_for(lambda: app._state == "connected", pilot.pause)
            app.action_show_tab("tab-live")  # entering Live Data starts polling
            table = app.query_one("#live")
            await wait_for(lambda: table.row_count == len(live_pids), pilot.pause)
            assert table.row_count == len(live_pids)
            assert app._streaming is True
            # Leaving the tab pauses the stream.
            app.action_show_tab("tab-faults")
            await wait_for(lambda: not app._streaming, pilot.pause)
            assert app._streaming is False

    asyncio.run(scenario())


def test_live_table_keeps_skipped_pids_and_cursor(mock_app):
    """A PID absent from one snapshot keeps its row, and the row cursor holds.

    Regression: the live table used to clear + rebuild every tick, so a PID the
    ECU skipped that snapshot vanished from the list, and the cursor snapped back
    to the top row on every update.
    """
    from trecu.protocol.pids import SensorReading
    from trecu.tui.live_table import LiveTable

    app = mock_app(live_pids=[0x0C, 0x05, 0x11])

    def snap(values):
        return [
            SensorReading(pid=pid, name=name, value=val, unit=unit)
            for pid, name, val, unit in values
        ]

    async def scenario():
        async with app.run_test() as pilot:
            table = app.query_one("#live", LiveTable)
            table.update_readings(
                snap(
                    [
                        (0x0C, "RPM", 1000.0, "rpm"),
                        (0x05, "Coolant", 80.0, "C"),
                        (0x11, "Throttle", 12.0, "%"),
                    ]
                )
            )
            assert table.row_count == 3
            # Park the cursor on the middle row.
            table.move_cursor(row=1)
            assert table.cursor_coordinate.row == 1

            # Next snapshot skips the throttle PID and moves the others.
            table.update_readings(
                snap(
                    [
                        (0x0C, "RPM", 2000.0, "rpm"),
                        (0x05, "Coolant", 81.0, "C"),
                    ]
                )
            )
            # Nothing disappears; the skipped PID keeps its last row/value.
            assert table.row_count == 3
            assert table.cursor_coordinate.row == 1
            assert table.get_row(str(0x11))[1] == "12"
            # An answered PID reflects the fresh value in place.
            assert table.get_row(str(0x0C))[1] == "2000"

    asyncio.run(scenario())
