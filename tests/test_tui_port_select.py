import asyncio

from trecu.tui.app import TrecuApp
from trecu.tui.port_select import PortSelectScreen
from trecu.transport.mock import MockKLineTransport

TWO_PORTS = [
    {"device": "/dev/cu.usbserial-A", "description": "FT232R USB UART",
     "manufacturer": "FTDI", "vid": 0x0403, "pid": 0x6001,
     "serial_number": "A", "likely_kkl": True},
    {"device": "/dev/cu.usbserial-B", "description": "FT232R USB UART",
     "manufacturer": "FTDI", "vid": 0x0403, "pid": 0x6001,
     "serial_number": "B", "likely_kkl": True},
]


def test_picker_shown_when_multiple_ports_and_selection_reads():
    picked = {}

    def transport_for_port(device):
        picked["device"] = device
        return MockKLineTransport()

    app = TrecuApp(
        transport_factory=None,
        mock=False,
        list_ports=lambda: list(TWO_PORTS),
        transport_for_port=transport_for_port,
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, PortSelectScreen)
            await pilot.press("enter")          # select the highlighted (first) port
            await pilot.pause(0.4)
            table = app.query_one("#dtcs")
            assert table.row_count == 3
            assert picked["device"] == "/dev/cu.usbserial-A"

    asyncio.run(scenario())


def test_rescan_repopulates_after_ports_appear():
    calls = {"n": 0}

    def lister():
        calls["n"] += 1
        return [] if calls["n"] == 1 else list(TWO_PORTS)

    app = TrecuApp(
        transport_factory=None,
        mock=False,
        list_ports=lister,
        transport_for_port=lambda d: MockKLineTransport(),
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            screen = app.screen
            assert isinstance(screen, PortSelectScreen)
            option_list = screen.query_one("#ports")
            # First scan: empty -> single disabled placeholder option.
            assert option_list.option_count == 1
            await pilot.press("r")              # rescan; now two ports appear
            await pilot.pause(0.2)
            assert option_list.option_count == 2

    asyncio.run(scenario())
