"""The 'connecting...' spinner modal shown during a fresh K-line connect.

Covers that the modal appears while the (off-thread) connect runs, dismisses on
success, that Cancel abandons the in-flight attempt and tears the resulting
session down once the stalled handshake finally returns, and that a cancel hands
back to the port picker when a port lister is configured. The live-poll loop
connects through the *same* path, so it gets the same modal and fallbacks.
"""

import asyncio
import threading
import time

from textual.widgets import Header

from trecu.protocol.iso9141 import Iso9141Config
from trecu.tui.app import TrecuApp
from trecu.tui.screens import ConnectErrorScreen, ConnectingScreen
from trecu.tui.port_select import PortSelectScreen
from trecu.transport.base import TransportError
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


async def _wait_for(pilot, cond, timeout=5.0):
    """Pause until an off-thread TUI operation satisfies ``cond``."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met within timeout")


def _app(gate: threading.Event) -> TrecuApp:
    return TrecuApp(
        transport_factory=lambda: GatedObdTransport(gate),
        mock=True,
        port="mock",
        keepalive_interval=0,
        protocol="iso9141",
    )


class FailingObdTransport(MockObdTransport):
    """A mock OBD ECU whose 5-baud init always fails — so ``client.connect()``
    (and therefore the fresh session's ``start_session``) raises."""

    def five_baud_init(self, address: int) -> None:
        raise TransportError("simulated 5-baud init failure")


# One-shot init so the failing connect gives up immediately (no retry sleeps).
_FAIL_FAST = Iso9141Config(init_retries=1, retry_wait=0.0)


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
            assert app._ecu.connected
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
            assert app._state == "disconnected"
            # Release the stalled handshake: the abandoned session that the
            # connect thread finishes building must be torn down, not kept.
            gate.set()
            await pilot.pause(0.4)
            assert not app._ecu.connected

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
            assert app._state == "disconnected"
            title = app.query_one(Header).query_one("HeaderTitle").render().plain
            assert title.endswith("○ disconnected")
            # Releasing the doomed connect leaves no session behind.
            gate.set()
            await pilot.pause(0.4)
            assert isinstance(app.screen, PortSelectScreen)
            assert not app._ecu.connected

    asyncio.run(scenario())


def test_connect_error_shows_modal_then_returns_to_port_picker():
    # A failing fresh connect surfaces a ConnectErrorScreen; OK hands back to
    # the port picker so the user can pick a (different) port and retry.
    app = TrecuApp(
        transport_factory=None,
        mock=False,
        config=_FAIL_FAST,
        list_ports=lambda: list(TWO_PORTS),
        transport_for_port=lambda d: FailingObdTransport(),
        keepalive_interval=0,
        protocol="iso9141",
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)
            assert isinstance(app.screen, PortSelectScreen)
            await pilot.press("enter")           # pick a port -> connect fails
            await pilot.pause(0.4)
            assert isinstance(app.screen, ConnectErrorScreen)
            assert app._state == "error"
            assert not app._ecu.connected
            await pilot.press("enter")           # OK -> back to the picker
            await pilot.pause(0.2)
            assert isinstance(app.screen, PortSelectScreen)
            assert not app._ecu.connected

    asyncio.run(scenario())


def test_connect_error_modal_returns_to_ready_without_lister():
    # With no port lister (a fixed --mock/--port), the error modal's OK has
    # nowhere to route, so it drops back to the ready state instead.
    app = TrecuApp(
        transport_factory=lambda: FailingObdTransport(),
        mock=True,
        port="mock",
        config=_FAIL_FAST,
        keepalive_interval=0,
        protocol="iso9141",
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.4)  # auto-read on mount -> connect fails
            assert isinstance(app.screen, ConnectErrorScreen)
            await pilot.press("enter")           # OK
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConnectErrorScreen)
            assert app._state == "disconnected"
            assert not app._ecu.connected

    asyncio.run(scenario())


def test_live_tab_connects_behind_the_same_modal():
    """Entering Live Data while disconnected gets the Read path's treatment.

    Regression: the live-poll loop connected on its own — no spinner, no Cancel,
    no fallback — so arrowing onto Live Data after a cancelled or failed connect
    blocked silently for seconds with no way out.
    """
    gate = threading.Event()
    app = TrecuApp(
        transport_factory=lambda: GatedObdTransport(gate),
        mock=True,
        port="mock",
        keepalive_interval=0,
        protocol="iso9141",
        poll_interval=0.05,
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)                # mount read: connect stalls
            assert isinstance(app.screen, ConnectingScreen)
            await pilot.press("escape")           # cancel it -> disconnected
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ConnectingScreen)
            app.action_show_tab("tab-live")       # arrow onto Live Data
            # The poll loop's connect raises the same cancelable modal, rather
            # than blocking the tick on a silent handshake.
            await _wait_for(pilot, lambda: isinstance(app.screen, ConnectingScreen))
            assert app._state == "connecting"
            await pilot.press("escape")           # Cancel is available here too
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ConnectingScreen)
            assert app._state == "disconnected"
            # Releasing both doomed handshakes leaves no session, and the
            # stream stops instead of retrying every tick.
            gate.set()
            await _wait_for(pilot, lambda: not app._live_running)
            assert not app._ecu.connected

    asyncio.run(scenario())


def test_live_tab_connect_failure_stops_polling():
    # A failed connect from the live path surfaces the same error modal as a
    # failed Read, and stops the poll loop rather than re-attempting per tick.
    app = TrecuApp(
        transport_factory=lambda: FailingObdTransport(),
        mock=True,
        port="mock",
        config=_FAIL_FAST,
        keepalive_interval=0,
        protocol="iso9141",
        poll_interval=0.05,
    )

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.4)                # mount read -> connect fails
            assert isinstance(app.screen, ConnectErrorScreen)
            await pilot.press("enter")            # OK -> ready state (no lister)
            await pilot.pause(0.2)
            app.action_show_tab("tab-live")
            await _wait_for(pilot, lambda: isinstance(app.screen, ConnectErrorScreen))
            assert app._live_running is False
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConnectErrorScreen)
            assert not app._ecu.connected

    asyncio.run(scenario())


def test_cancel_button_on_picker_after_connect_cancel_quits_app():
    # The picker that reappears after a connect-cancel must quit when its Cancel
    # button is activated, exactly like the startup picker — even while the
    # abandoned connect thread is still blocked (gate never released here).
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
            await pilot.click("#cancel")         # Cancel button -> picker returns
            await pilot.pause(0.1)
            assert isinstance(app.screen, PortSelectScreen)
            await pilot.click("#cancel")         # Cancel button -> quit
            await pilot.pause(0.2)
            assert not app.is_running

    asyncio.run(scenario())
