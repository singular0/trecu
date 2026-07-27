"""The native title bar carries the app identity and connection state."""

import asyncio
import time

from textual.widgets import Header

from trecu import __version__
from trecu.tui.app import TrecuApp
from trecu.transport.mock_obd import MockObdTransport


def test_titlebar_shows_name_version_and_connection_status():
    app = TrecuApp(
        transport_factory=MockObdTransport,
        mock=True,
        port="mock",
        keepalive_interval=0,
        protocol="iso9141",
    )

    async def scenario():
        async with app.run_test() as pilot:
            app.query_one(Header)
            deadline = time.monotonic() + 5
            while app._state != "connected" and time.monotonic() < deadline:
                await pilot.pause(0.05)

            assert app._state == "connected"
            title = app.query_one(Header).query_one("HeaderTitle").render()
            assert title.plain == (
                f"TrECU v{__version__} — ● connected"
            )
            assert not app.query("#spine")

    asyncio.run(scenario())
