"""The startup port picker: it appears when no port is fixed, and its choice
is what the session then connects over."""

import asyncio

from trecu.tui.port_select import PortSelectScreen
from trecu.transport.mock_obd import MockObdTransport

from mock_ecus import TWO_PORTS


def test_picker_shown_when_multiple_ports_and_selection_reads(picker_app, wait_for):
    picked = {}

    def transport_for_port(device):
        picked["device"] = device
        return MockObdTransport()

    app = picker_app(transport_for_port)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, PortSelectScreen)
            await pilot.press("enter")          # select the highlighted (first) port
            # The read runs off the event loop; poll rather than guess a sleep.
            await wait_for(
                lambda: app.query_one("#dtcs").row_count == 1, pilot.pause
            )
            assert picked["device"] == "/dev/cu.usbserial-A"

    asyncio.run(scenario())


def test_connect_button_reads_highlighted_port(picker_app, wait_for):
    picked = {}

    def transport_for_port(device):
        picked["device"] = device
        return MockObdTransport()

    app = picker_app(transport_for_port)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, PortSelectScreen)
            await pilot.press("down")            # move to the second port
            await pilot.click("#connect")        # Connect button, not Enter
            await wait_for(
                lambda: picked.get("device") == "/dev/cu.usbserial-B", pilot.pause
            )

    asyncio.run(scenario())


def test_rescan_repopulates_after_ports_appear(picker_app):
    calls = {"n": 0}

    def lister():
        calls["n"] += 1
        return [] if calls["n"] == 1 else list(TWO_PORTS)

    app = picker_app(lambda d: MockObdTransport(), list_ports=lister)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            screen = app.screen
            assert isinstance(screen, PortSelectScreen)
            table = screen.query_one("#ports")
            # First scan: empty -> single placeholder row.
            assert table.row_count == 1
            await pilot.press("r")              # rescan; now two ports appear
            await pilot.pause(0.2)
            assert table.row_count == 2

    asyncio.run(scenario())
