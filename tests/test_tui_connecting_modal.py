"""The 'connecting...' spinner modal shown during a fresh K-line connect.

Covers that the modal appears while the (off-thread) connect runs, dismisses on
success, that Cancel abandons the in-flight attempt and tears the resulting
session down once the stalled handshake finally returns, and that a cancel hands
back to the port picker when a port lister is configured. The live-poll loop
connects through the *same* path, so it gets the same modal and fallbacks.
"""

import asyncio
import threading

from textual.widgets import Header

from trecu.tui.screens import ConnectErrorScreen, ConnectingScreen
from trecu.tui.port_select import PortSelectScreen

from mock_ecus import FAIL_FAST, FailingObdTransport, GatedObdTransport


def test_connecting_modal_shown_then_dismissed_on_success(mock_app):
    gate = threading.Event()
    app = mock_app(lambda: GatedObdTransport(gate))

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.2)  # auto-read starts; connect stalls in init
            assert isinstance(app.screen, ConnectingScreen)
            # The modal names the port and what it is doing on it.
            assert app.screen._port == "mock"
            assert "5-baud init" in str(app.screen.query_one("#detail").render())
            gate.set()  # let the handshake complete
            await pilot.pause(0.8)
            assert not isinstance(app.screen, ConnectingScreen)
            assert app._ecu.connected
            assert app.query_one("#dtcs").row_count == 1  # the default P1108

    asyncio.run(scenario())


def test_connecting_modal_cancel_abandons_connect(mock_app):
    gate = threading.Event()
    app = mock_app(lambda: GatedObdTransport(gate))

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


def test_cancel_returns_to_port_selection_when_lister_configured(picker_app):
    gate = threading.Event()
    app = picker_app(lambda d: GatedObdTransport(gate))

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


def test_connect_error_shows_modal_then_returns_to_port_picker(picker_app):
    # A failing fresh connect surfaces a ConnectErrorScreen; OK hands back to
    # the port picker so the user can pick a (different) port and retry.
    app = picker_app(
        lambda d: FailingObdTransport(), config=FAIL_FAST
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


def test_connect_error_modal_returns_to_ready_without_lister(mock_app):
    # With no port lister (a fixed --mock/--port), the error modal's OK has
    # nowhere to route, so it drops back to the ready state instead.
    app = mock_app(FailingObdTransport, config=FAIL_FAST)

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


def test_live_tab_connects_behind_the_same_modal(mock_app, wait_for):
    """Entering Live Data while disconnected gets the Read path's treatment.

    Regression: the live-poll loop connected on its own — no spinner, no Cancel,
    no fallback — so arrowing onto Live Data after a cancelled or failed connect
    blocked silently for seconds with no way out.
    """
    gate = threading.Event()
    app = mock_app(lambda: GatedObdTransport(gate), poll_interval=0.05)

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
            await wait_for(
                lambda: isinstance(app.screen, ConnectingScreen), pilot.pause
            )
            assert app._state == "connecting"
            await pilot.press("escape")           # Cancel is available here too
            await pilot.pause(0.1)
            assert not isinstance(app.screen, ConnectingScreen)
            assert app._state == "disconnected"
            # Releasing both doomed handshakes leaves no session, and the
            # stream stops instead of retrying every tick.
            gate.set()
            await wait_for(lambda: not app._live_running, pilot.pause)
            assert not app._ecu.connected

    asyncio.run(scenario())


def test_live_tab_connect_failure_stops_polling(mock_app, wait_for):
    # A failed connect from the live path surfaces the same error modal as a
    # failed Read, and stops the poll loop rather than re-attempting per tick.
    app = mock_app(FailingObdTransport, config=FAIL_FAST, poll_interval=0.05)

    async def scenario():
        async with app.run_test() as pilot:
            await pilot.pause(0.4)                # mount read -> connect fails
            assert isinstance(app.screen, ConnectErrorScreen)
            await pilot.press("enter")            # OK -> ready state (no lister)
            await pilot.pause(0.2)
            app.action_show_tab("tab-live")
            await wait_for(
                lambda: isinstance(app.screen, ConnectErrorScreen), pilot.pause
            )
            assert app._live_running is False
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, ConnectErrorScreen)
            assert not app._ecu.connected

    asyncio.run(scenario())


def test_cancel_button_on_picker_after_connect_cancel_quits_app(picker_app):
    # The picker that reappears after a connect-cancel must quit when its Cancel
    # button is activated, exactly like the startup picker — even while the
    # abandoned connect thread is still blocked (gate never released here).
    gate = threading.Event()
    app = picker_app(lambda d: GatedObdTransport(gate))

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
