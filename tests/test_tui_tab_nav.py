"""Tab navigation (the ←/→ spine) holds up across state changes.

The Faults tab always shows its DTC table — empty (headers only) when there are
no codes, never hidden behind a separate widget — so tab switching never depends
on which of two panes is currently visible.

``mock_app``'s default ECU stores one P1108, so the mount read finds a fault and
clearing it drops the table to zero rows.
"""

import asyncio

from textual.widgets import TabbedContent

from trecu.transport.mock_obd import MockObdTransport


def test_nav_survives_clearing_codes(mock_app, wait_for):
    app = mock_app()

    async def scenario():
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            # Auto-read on mount finds the P1108.
            await wait_for(lambda: app.query_one("#dtcs").row_count == 1, pilot.pause)

            await pilot.press("right")  # Dashboard -> Faults
            await pilot.pause(0.1)
            assert tabs.active == "tab-faults"

            # Clear the codes via the real confirm flow; the re-read drops the
            # table to zero rows but leaves it visible.
            await pilot.press("c")
            await pilot.pause(0.1)
            await pilot.click("#yes")
            await wait_for(lambda: app.query_one("#dtcs").row_count == 0, pilot.pause)
            assert app.query_one("#dtcs").display

            await pilot.press("left")   # Faults -> Dashboard
            await pilot.pause(0.15)
            assert tabs.active == "tab-dashboard"

            await pilot.press("right")  # Dashboard -> Faults
            await pilot.pause(0.15)
            assert tabs.active == "tab-faults"

    asyncio.run(scenario())


def test_arrows_do_not_switch_tabs_while_modal_open(mock_app, wait_for):
    # The ←/→ bindings are app-level priority=True, so they fire even when a modal
    # owns the screen. Opening the Clear-confirm dialog and pressing arrows must
    # not switch tabs behind it.
    app = mock_app()

    async def scenario():
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            await wait_for(lambda: app.query_one("#dtcs").row_count == 1, pilot.pause)

            await pilot.press("right")  # Dashboard -> Faults
            await pilot.pause(0.1)
            assert tabs.active == "tab-faults"

            await pilot.press("c")  # open the Clear-confirm modal
            await wait_for(lambda: app.screen is not app.screen_stack[0], pilot.pause)

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
            await wait_for(lambda: app.screen is app.screen_stack[0], pilot.pause)
            await pilot.press("left")  # Faults -> Dashboard
            await pilot.pause(0.1)
            assert tabs.active == "tab-dashboard"

    asyncio.run(scenario())


def test_right_cycles_through_all_tabs_with_empty_faults(mock_app, wait_for):
    # A full →→→→ sweep still visits every tab in order on a fault-free mock,
    # where the Faults tab shows its (visible) empty DTC table.
    app = mock_app(lambda: MockObdTransport(dtcs=[], mil=False))

    async def scenario():
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            # row_count is 0 before the read too, so wait on the read completing
            # (state -> connected, connecting modal gone) not on the row count.
            await wait_for(lambda: app._state == "connected", pilot.pause)
            assert app.query_one("#dtcs").row_count == 0
            assert app.query_one("#dtcs").display

            order = ["tab-dashboard", "tab-faults", "tab-live", "tab-log"]
            assert tabs.active == "tab-dashboard"
            for expected in order[1:] + order[:1]:  # wraps back to dashboard
                await pilot.press("right")
                await pilot.pause(0.1)
                assert tabs.active == expected

    asyncio.run(scenario())


def test_verbose_stays_on_dashboard_until_an_error(mock_app, wait_for):
    app = mock_app(verbose=True)

    async def scenario():
        async with app.run_test() as pilot:
            tabs = app.query_one(TabbedContent)
            await wait_for(lambda: app._state == "connected", pilot.pause)
            assert tabs.active == "tab-dashboard"

            app._on_error(RuntimeError("test failure"))
            # Let Textual dispatch the resulting TabActivated event before the
            # run_test context tears down its screen stack.
            await pilot.pause(0.5)
            assert tabs.active == "tab-log"
            lines = app.query_one("#log").lines
            text = "\n".join(line.text for line in lines)
            assert "OBD request: Mode 09 (vehicle information)" in text
            assert "ECU operation complete: decoded" in text

    asyncio.run(scenario())
