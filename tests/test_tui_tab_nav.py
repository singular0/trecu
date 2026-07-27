"""Tab navigation (the ←/→ spine) holds up across state changes.

The Faults tab always shows its DTC table — empty (headers only) when there are
no codes, never hidden behind a separate widget — so tab switching never depends
on which of two panes is currently visible.
"""

import asyncio
import time

from textual.widgets import TabbedContent

from trecu.tui.app import TrecuApp
from trecu.transport.mock_obd import MockObdTransport


def _app(**kw) -> TrecuApp:
    # Default MockObdTransport stores one P1108, so the first read finds a fault
    # and clearing it drops the table to zero rows.
    return TrecuApp(
        transport_factory=lambda: MockObdTransport(**kw),
        mock=True,
        port="mock",
        keepalive_interval=0,
        protocol="iso9141",
    )


async def _wait_for(pilot, cond, timeout=5.0):
    """Pause until ``cond()`` holds — reads run off-thread, so poll not sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met within timeout")


def test_nav_survives_clearing_codes():
    app = _app()

    async def scenario():
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            # Auto-read on mount finds the P1108.
            await _wait_for(pilot, lambda: app.query_one("#dtcs").row_count == 1)

            await pilot.press("right")  # Dashboard -> Faults
            await pilot.pause(0.1)
            assert tabs.active == "tab-faults"

            # Clear the codes via the real confirm flow; the re-read drops the
            # table to zero rows but leaves it visible.
            await pilot.press("c")
            await pilot.pause(0.1)
            await pilot.click("#yes")
            await _wait_for(pilot, lambda: app.query_one("#dtcs").row_count == 0)
            assert app.query_one("#dtcs").display

            await pilot.press("left")   # Faults -> Dashboard
            await pilot.pause(0.15)
            assert tabs.active == "tab-dashboard"

            await pilot.press("right")  # Dashboard -> Faults
            await pilot.pause(0.15)
            assert tabs.active == "tab-faults"

    asyncio.run(scenario())


def test_arrows_do_not_switch_tabs_while_modal_open():
    # The ←/→ bindings are app-level priority=True, so they fire even when a modal
    # owns the screen. Opening the Clear-confirm dialog and pressing arrows must
    # not switch tabs behind it.
    app = _app()

    async def scenario():
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            await _wait_for(pilot, lambda: app.query_one("#dtcs").row_count == 1)

            await pilot.press("right")  # Dashboard -> Faults
            await pilot.pause(0.1)
            assert tabs.active == "tab-faults"

            await pilot.press("c")  # open the Clear-confirm modal
            await _wait_for(pilot, lambda: app.screen is not app.screen_stack[0])

            # Arrows are inert while the modal is up.
            await pilot.press("right")
            await pilot.pause(0.1)
            assert tabs.active == "tab-faults"
            await pilot.press("left")
            await pilot.pause(0.1)
            assert tabs.active == "tab-faults"
            # And the modal is still the active screen (arrows didn't dismiss it).
            assert app.screen is not app.screen_stack[0]

            # Dismiss it; arrows work again.
            await pilot.press("escape")
            await _wait_for(pilot, lambda: app.screen is app.screen_stack[0])
            await pilot.press("left")  # Faults -> Dashboard
            await pilot.pause(0.1)
            assert tabs.active == "tab-dashboard"

    asyncio.run(scenario())


def test_right_cycles_through_all_tabs_with_empty_faults():
    # A full →→→→ sweep still visits every tab in order on a fault-free mock,
    # where the Faults tab shows its (visible) empty DTC table.
    app = _app(dtcs=[], mil=False)

    async def scenario():
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            # row_count is 0 before the read too, so wait on the read completing
            # (state -> connected, connecting modal gone) not on the row count.
            await _wait_for(pilot, lambda: app._state == "connected")
            assert app.query_one("#dtcs").row_count == 0
            assert app.query_one("#dtcs").display

            order = ["tab-dashboard", "tab-faults", "tab-live", "tab-log"]
            assert tabs.active == "tab-dashboard"
            for expected in order[1:] + order[:1]:  # wraps back to dashboard
                await pilot.press("right")
                await pilot.pause(0.1)
                assert tabs.active == expected

    asyncio.run(scenario())
