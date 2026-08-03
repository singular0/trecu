"""Fixtures shared by the whole suite.

Two things the TUI tests all need, and used to re-declare a file at a time:

* a :class:`~trecu.tui.app.TrecuApp` wired to an in-memory mock ECU — either
  with its port already fixed (``mock_app``) or with none yet, so it opens the
  port picker (``picker_app``);
* a way to wait on work that runs *off* the event loop (``wait_for``).  Reads,
  clears, and live polls all go through ``asyncio.to_thread``, so their results
  land at a moment no fixed ``sleep`` can predict.

The ECU doubles those apps run against live in ``tests/mock_ecus.py``.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

import pytest

from trecu.transport.mock_obd import MockObdTransport
from trecu.tui.app import TrecuApp

from mock_ecus import TWO_PORTS

Pause = Callable[[float], Awaitable[None]]


async def _wait_for(
    cond: Callable[[], bool],
    pause: Pause = asyncio.sleep,
    timeout: float = 5.0,
    interval: float = 0.05,
) -> None:
    """Poll ``cond`` until it holds, yielding via ``pause`` between checks.

    ``pause`` is a pilot's ``pilot.pause`` when a TUI is being driven — it pumps
    Textual's message queue as well as yielding — and plain ``asyncio.sleep``
    when there is no app (the ``SessionController`` tests).
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if cond():
            return
        await pause(interval)
    raise AssertionError("condition not met within timeout")


@pytest.fixture
def wait_for():
    """The ``await wait_for(cond, pilot.pause)`` helper described above."""
    return _wait_for


@pytest.fixture
def mock_app():
    """Build a ``TrecuApp`` on a fixed mock ECU — no port picker.

    The defaults are what a TUI test almost always wants: the simulated port
    and no keepalive ticker beating underneath the assertions.  Either, and
    every other ``TrecuApp`` argument, can be overridden per test.
    """

    def build(transport_factory=MockObdTransport, **kw) -> TrecuApp:
        kw.setdefault("mock", True)
        kw.setdefault("port", "mock")
        kw.setdefault("keepalive_interval", 0)
        return TrecuApp(transport_factory=transport_factory, **kw)

    return build


@pytest.fixture
def picker_app():
    """Build a ``TrecuApp`` with no port known yet, so it opens the picker.

    ``transport_for_port`` is what the picked device turns into; ``list_ports``
    defaults to the two mock cables in ``mock_ecus.TWO_PORTS``.
    """

    def build(
        transport_for_port,
        *,
        list_ports: Optional[Callable[[], list]] = None,
        **kw,
    ) -> TrecuApp:
        kw.setdefault("keepalive_interval", 0)
        return TrecuApp(
            transport_factory=None,
            mock=False,
            list_ports=list_ports or (lambda: list(TWO_PORTS)),
            transport_for_port=transport_for_port,
            **kw,
        )

    return build
