"""The 'connecting...' spinner modal shown during a fresh K-line connect.

Covers that the modal appears while the (off-thread) connect runs, dismisses on
success, that Cancel abandons the in-flight attempt and tears the resulting
session down once the stalled handshake finally returns, and that a cancel hands
back to the port picker when a port lister is configured.
"""

import asyncio
import threading

from trecu.tui.app import ConnectingScreen, TrecuApp
from trecu.tui.port_select import PortSelectScreen
from trecu.transport.mock_obd import MockObdTransport

TWO_PORTS = [
    {"device": "/dev/cu.usbserial-A", "description": "FT232R USB UART",
     "vid": 0x0403, "pid": 0x6001, "likely_kkl": True},
    {"device": "/dev/cu.usbserial-B", "description": "FT232R USB UART",
     "vid": 0x0403, "pid": 0x6001, "likely_kkl": True},
]


class GatedObdTransport(MockObdTransport):
    """A mock OBD ECU whose 5-baud init blocks until a gate is released.

    The stall is in ``five_baud_init`` (inside ``client.connect()``), i.e. after
    the service has reported which protocol it's probing — so a test can observe
    the modal's port/protocol lines while the connect is deliberately stalled,
    then release it (success) or leave it stalled (to test Cancel).
    """

    def __init__(self, gate: threading.Event):
        super().__init__()
        self._gate = gate

    def five_baud_init(self, address: int) -> None:
        self._gate.wait(timeout=5)
        super().five_baud_init(address)


def _app(gate: threading.Event) -> TrecuApp:
    return TrecuApp(
        transport_factory=lambda: GatedObdTransport(gate),
        mock=True,
        port="mock",
        keepalive_interval=0,
        protocol="iso9141",
    )


def test_connecting_modal_shown_then_dismissed_on_success():
    gate = threading.Event()
    app = _app(gate)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)  # auto-read starts; connect stalls in init
            assert isinstance(app.screen, ConnectingScreen)
            # The modal names the port and the protocol currently being probed.
            assert app.screen._port == "mock"
            assert app.screen._protocol == "iso9141"
            gate.set()  # let the handshake complete
            await pilot.pause(0.8)
            assert not isinstance(app.screen, ConnectingScreen)
            assert app._session is not None
            assert app.query_one("#dtcs").row_count == 1  # the default P1108

    asyncio.run(scenario())


def test_connecting_modal_cancel_abandons_connect():
    gate = threading.Event()
    app = _app(gate)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConnectingScreen)
            await pilot.press("enter")  # Cancel button is focused
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ConnectingScreen)
            assert app._state == "ready"
            # Release the stalled handshake: the abandoned session that the
            # connect thread finishes building must be torn down, not kept.
            gate.set()
            await pilot.pause(0.4)
            assert app._session is None

    asyncio.run(scenario())


def test_cancel_returns_to_port_selection_when_lister_configured():
    gate = threading.Event()
    app = TrecuApp(
        transport_factory=None,
        mock=False,
        list_ports=lambda: list(TWO_PORTS),
        transport_for_port=lambda d: GatedObdTransport(gate),
        keepalive_interval=0,
        protocol="iso9141",
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, PortSelectScreen)
            await pilot.press("enter")           # pick the highlighted port
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConnectingScreen)  # connect stalls
            await pilot.press("escape")          # cancel the connect
            await pilot.pause(0.1)
            # The picker returns *immediately* — while the abandoned connect is
            # still blocked (gate not yet released) — not only once it unwinds.
            assert isinstance(app.screen, PortSelectScreen)
            # Releasing the doomed connect leaves no session behind.
            gate.set()
            await pilot.pause(0.4)
            assert isinstance(app.screen, PortSelectScreen)
            assert app._session is None

    asyncio.run(scenario())


def test_cancel_picker_after_connect_cancel_quits_app():
    # The picker that reappears after a connect-cancel must quit on Cancel,
    # exactly like the startup picker — even while the abandoned connect thread
    # is still blocked (gate never released here).
    gate = threading.Event()
    app = TrecuApp(
        transport_factory=None,
        mock=False,
        list_ports=lambda: list(TWO_PORTS),
        transport_for_port=lambda d: GatedObdTransport(gate),
        keepalive_interval=0,
        protocol="iso9141",
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            await pilot.press("enter")           # pick a port -> connect stalls
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConnectingScreen)
            await pilot.press("escape")          # cancel connect -> picker returns
            await pilot.pause(0.1)
            assert isinstance(app.screen, PortSelectScreen)
            await pilot.press("escape")          # cancel picker -> quit
            await pilot.pause(0.2)
            assert not app.is_running

    asyncio.run(scenario())
