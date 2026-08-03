"""The Log tab follows the newest line only while the user is parked at the bottom.

The protocol logger writes constantly (a line per request), so a log that always
scrolled to the end made reading back through an earlier frame impossible.
``LogView`` derives ``auto_scroll`` from the scroll position instead; these tests
drive that through the real app, with real key presses, on the mock ECU.
"""

import asyncio

from textual.events import MouseScrollUp
from textual.widgets import TabbedContent

from trecu.tui.log_view import LogView


def _wheel_up(widget) -> MouseScrollUp:
    """One wheel-up notch over ``widget`` — what a terminal sends on a scroll."""
    return MouseScrollUp(
        widget=widget,
        x=1,
        y=1,
        delta_x=0,
        delta_y=-1,
        button=0,
        shift=False,
        meta=False,
        ctrl=False,
    )


async def _open_log_tab(app, pilot, wait_for):
    """Settle the mount read, then bring the Log tab up and focused."""
    await wait_for(lambda: app._state == "connected", pilot.pause)
    app.query_one(TabbedContent).active = "tab-log"
    await pilot.pause(0.1)
    log = app.query_one("#log", LogView)
    # The Log tab focuses its RichLog on activation (_TAB_FOCUS), which is what
    # makes the scroll keys below land on it.
    assert app.focused is log
    return log


def _fill(app, count=200):
    for i in range(count):
        app._append_log(f"filler line {i}")


def test_log_follows_new_lines_while_at_the_bottom(mock_app, wait_for):
    app = mock_app()

    async def scenario():
        async with app.run_test() as pilot:
            log = await _open_log_tab(app, pilot, wait_for)

            _fill(app)
            await pilot.pause(0.1)
            assert log.max_scroll_y > 0  # the log really is longer than the pane
            assert log.scroll_y == log.max_scroll_y
            assert log.auto_scroll

    asyncio.run(scenario())


def test_manual_scroll_up_stops_the_log_following(mock_app, wait_for):
    app = mock_app()

    async def scenario():
        async with app.run_test() as pilot:
            log = await _open_log_tab(app, pilot, wait_for)
            _fill(app)
            await pilot.pause(0.1)

            await pilot.press("up", "up", "up")
            await pilot.pause(0.1)
            parked = log.scroll_y
            assert parked < log.max_scroll_y
            assert not log.auto_scroll

            # New lines arrive; the view must stay exactly where it was left.
            for i in range(50):
                app._append_log(f"later line {i}")
            await pilot.pause(0.1)
            assert log.scroll_y == parked
            assert not log.auto_scroll

    asyncio.run(scenario())


def test_scrolling_back_to_the_bottom_resumes_following(mock_app, wait_for):
    app = mock_app()

    async def scenario():
        async with app.run_test() as pilot:
            log = await _open_log_tab(app, pilot, wait_for)
            _fill(app)
            await pilot.pause(0.1)

            await pilot.press("pageup")
            await pilot.pause(0.1)
            assert not log.auto_scroll

            # Back to the bottom by hand -> following is re-enabled...
            await pilot.press("end")
            await pilot.pause(0.2)
            assert log.scroll_y == log.max_scroll_y
            assert log.auto_scroll

            # ...and the next lines are followed again.
            for i in range(50):
                app._append_log(f"later line {i}")
            await pilot.pause(0.1)
            assert log.scroll_y == log.max_scroll_y

    asyncio.run(scenario())


def test_mouse_wheel_up_stops_following_and_wheel_back_resumes_it(mock_app, wait_for):
    # The wheel is the other way a user leaves the bottom, and it goes through
    # a different Textual path (mouse events, not the focused widget's keys).
    app = mock_app()

    async def scenario():
        async with app.run_test() as pilot:
            log = await _open_log_tab(app, pilot, wait_for)
            _fill(app)
            await pilot.pause(0.1)

            for _ in range(3):
                log.post_message(_wheel_up(log))
            await pilot.pause(0.1)
            parked = log.scroll_y
            assert parked < log.max_scroll_y
            assert not log.auto_scroll

            for i in range(50):
                app._append_log(f"later line {i}")
            await pilot.pause(0.1)
            assert log.scroll_y == parked

            log.scroll_end(animate=False, immediate=True)
            await pilot.pause(0.1)
            assert log.auto_scroll

    asyncio.run(scenario())
