"""Shared in-memory ECU doubles for the session / TUI tests.

The suite never touches hardware (see `CLAUDE.md`), so every connect in it runs
against one of the bundled mock transports.  These are the variants more than
one test file needs — a mock that counts its closes, one whose 5-baud init
stalls on a gate so a test can observe an attempt mid-flight and cancel it, and
one whose init always fails — plus the fail-fast config that keeps a
deliberately-failing connect from spending the real retry budget.

Not a ``test_*.py`` module, so pytest imports it only when a test asks for it.
"""

from __future__ import annotations

import threading
from typing import Dict, List

from trecu.protocol.iso9141 import Iso9141Config
from trecu.protocol.kwp2000 import SlowInitConfig
from trecu.transport.base import TransportError
from trecu.transport.mock_obd import MockObdTransport

#: One attempt, no settle wait — a connect expected to fail gives up at once
#: rather than spending the real ``init_retries`` x ``retry_wait`` seconds.
FAIL_FAST = Iso9141Config(slow_init=SlowInitConfig(init_retries=1, retry_wait=0.0))

#: Two plausible KKL cables, in the shape ``list_ports`` reports them.
TWO_PORTS: List[Dict[str, object]] = [
    {"device": "/dev/cu.usbserial-A", "description": "FT232R USB UART",
     "manufacturer": "FTDI", "vid": 0x0403, "pid": 0x6001,
     "serial_number": "A", "likely_kkl": True},
    {"device": "/dev/cu.usbserial-B", "description": "FT232R USB UART",
     "manufacturer": "FTDI", "vid": 0x0403, "pid": 0x6001,
     "serial_number": "B", "likely_kkl": True},
]


class CountingObdTransport(MockObdTransport):
    """Mock ECU that records how often it was closed (per instance)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.closes = 0

    def close(self) -> None:
        self.closes += 1
        super().close()


class GatedObdTransport(CountingObdTransport):
    """Mock ECU whose 5-baud init blocks until ``gate`` is released.

    The stall sits inside ``five_baud_init``, i.e. within ``client.connect()``
    and *after* the service has reported which protocol it is probing — so a
    test can inspect the connecting modal's port/protocol lines, then either
    release the gate (the connect succeeds) or leave it stalled (to exercise
    Cancel against an attempt that is genuinely still running).
    """

    def __init__(self, gate: threading.Event, **kw):
        super().__init__(**kw)
        self._gate = gate

    def five_baud_init(self, address: int) -> None:
        self._gate.wait(timeout=5)
        super().five_baud_init(address)


class FailingObdTransport(CountingObdTransport):
    """Mock ECU whose 5-baud init always fails, so ``connect()`` raises."""

    def five_baud_init(self, address: int) -> None:
        raise TransportError("simulated 5-baud init failure")
