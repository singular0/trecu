"""A simulated Triumph ECU that speaks just enough KWP2000 to exercise the app.

Lets you run the full TUI and the protocol client end-to-end without any
hardware.  It parses real frames and emits real, checksummed responses.
"""

from __future__ import annotations

from typing import List, Tuple

from ..protocol.framing import (
    ADDR_PHYSICAL,
    IncompleteFrame,
    build_frame,
    parse_frame,
)
from .base import Transport

# Default set of "stored" faults, as (dtc_high, dtc_low, status) triples.
# These decode to P0107, P0201, P1176 — all present in the bundled code DB.
_DEFAULT_DTCS: List[Tuple[int, int, int]] = [
    (0x01, 0x07, 0x21),  # P0107 - MAP sensor low
    (0x02, 0x01, 0x2F),  # P0201 - injector 1 circuit
    (0x11, 0x76, 0x20),  # P1176 - Triumph closed-loop / CO adaptation
]


class MockKLineTransport(Transport):
    """In-memory ECU emulator implementing StartComms / ReadDTC / Clear."""

    echoes = False  # not a physical line — nothing to reflect

    def __init__(
        self,
        dtcs: List[Tuple[int, int, int]] | None = None,
        ecu_address: int = 0x11,
        tester_address: int = 0xF1,
        addr_mode: int = ADDR_PHYSICAL,
        key_bytes: bytes = b"\xEA\x8F",
    ):
        self._dtcs = list(_DEFAULT_DTCS if dtcs is None else dtcs)
        self.ecu_address = ecu_address
        self.tester_address = tester_address
        self.addr_mode = addr_mode
        self.key_bytes = key_bytes
        self._rx = bytearray()      # bytes waiting for the client to read
        self._open = False
        self._connected = False

    # -- Transport interface -------------------------------------------------
    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False
        self._rx.clear()

    def reset_input(self) -> None:
        self._rx.clear()

    def fast_init(self, low_ms: int = 25, high_ms: int = 25) -> None:
        self._rx.clear()
        self._connected = False

    def write(self, data: bytes) -> None:
        try:
            frame, _ = parse_frame(bytes(data))
        except (IncompleteFrame, ValueError):
            return  # ignore garbage, like a real ECU would
        response = self._handle(frame.payload)
        if response is not None:
            self._rx.extend(
                build_frame(response, self.tester_address, self.ecu_address, self.addr_mode)
            )

    def read(self, count: int, timeout: float) -> bytes:
        n = min(count, len(self._rx))
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    # -- ECU behaviour -------------------------------------------------------
    def _handle(self, payload: bytes) -> bytes | None:
        if not payload:
            return None
        sid = payload[0]

        if sid == 0x81:  # StartCommunication
            self._connected = True
            return bytes((0xC1,)) + self.key_bytes

        if sid == 0x82:  # StopCommunication
            self._connected = False
            return bytes((0xC2,))

        if sid == 0x10:  # StartDiagnosticSession
            session = payload[1] if len(payload) > 1 else 0x81
            return bytes((0x50, session))

        if sid == 0x3E:  # TesterPresent
            sub = payload[1] if len(payload) > 1 else 0x01
            if sub == 0x02:  # response suppressed
                return None
            return bytes((0x7E, sub))

        if sid == 0x18:  # ReadDTCByStatus
            body = bytearray((0x58, len(self._dtcs)))
            for hi, lo, status in self._dtcs:
                body += bytes((hi, lo, status))
            return bytes(body)

        if sid == 0x14:  # ClearDiagnosticInformation
            self._dtcs.clear()
            return bytes((0x54,))

        # Unknown service -> negative response (serviceNotSupported).
        return bytes((0x7F, sid, 0x11))
