"""The native title bar carries the app identity and connection state."""

import asyncio

from textual.widgets import Header

from trecu import __version__


def test_titlebar_shows_name_version_and_connection_status(mock_app, wait_for):
    app = mock_app()

    async def scenario():
        async with app.run_test() as pilot:
            app.query_one(Header)
            await wait_for(lambda: app._state == "connected", pilot.pause)

            expected = f"TrECU v{__version__} — ● connected"
            title_widget = app.query_one(Header).query_one("HeaderTitle")
            await wait_for(
                lambda: title_widget.render().plain == expected, pilot.pause
            )
            assert title_widget.render().plain == expected
            assert not app.query("#spine")

    asyncio.run(scenario())
