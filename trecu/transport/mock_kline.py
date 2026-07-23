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
from ._mock_live import kwp_live_frame
from .base import Transport, TransportError

# Default set of "stored" faults, as (dtc_high, dtc_low, status) triples.
# These decode to P0107, P0201, P1105 — all present in the bundled code DB.
# NOTE: real Keihin ECUs answering legacy 0x18 ReadDTCByStatus return raw
# fault numbers that are *not* SAE-J2012 bit-encoded (decoded with the "K"
# family prefix, e.g. bytes 15 35 -> K1535); tests exercising that path pass
# Keihin-style triples via the ``dtcs=`` parameter instead of these defaults.
_DEFAULT_DTCS: List[Tuple[int, int, int]] = [
    (0x01, 0x07, 0x21),  # P0107 - MAP sensor short to negative
    (0x02, 0x01, 0x2F),  # P0201 - injector 1 circuit
    (0x11, 0x05, 0x20),  # P1105 - MAP sensor pipe fault
]


class MockKLineTransport(Transport):
    """In-memory Keihin-style ECU emulator (StartComms / DTCs / Clear / ident).

    Defaults mirror the community-documented Triumph Keihin K-line ECU: address
    ``0xD5`` / tester ``0xF5``, DTCs served to OBD Mode 03 (over KWP framing)
    as well as legacy ``0x18``, AccessTimingParameter accepted (recorded in
    ``timing_params``), identification on the Keihin RLIs. Fast-init only by
    default; pass ``supports_slow_init=True`` to also emulate the 5-baud
    Keihin slow init (`kwp-slow`) — it answers only at ``ecu_address``, like a
    real ECU.
    """

    echoes = False  # not a physical line — nothing to reflect

    def __init__(
        self,
        dtcs: List[Tuple[int, int, int]] | None = None,
        ecu_address: int = 0xD5,
        tester_address: int = 0xF5,
        addr_mode: int = ADDR_PHYSICAL,
        key_bytes: bytes = b"\xEA\x8F",
        vin: str = "SMTA469N4KT700001",
        hardware: str = "1050ECU-KEIHIN",
        software: str = "V1.23",
        supports_slow_init: bool = False,
    ):
        self._dtcs = list(_DEFAULT_DTCS if dtcs is None else dtcs)
        self.ecu_address = ecu_address
        self.tester_address = tester_address
        self.addr_mode = addr_mode
        self.key_bytes = key_bytes
        self.supports_slow_init = supports_slow_init  # shadows the class flag
        # ReadEcuIdentification records, keyed by the default config RLIs
        # (the Triumph identifier list — keep in sync with Kwp2000Config).
        self._identification = {
            0xA0: vin.encode("ascii", "ignore"),
            0xAE: hardware.encode("ascii", "ignore"),
            0x8C: software.encode("ascii", "ignore"),
        }
        self.timing_params: bytes | None = None  # last AccessTimingParameter set
        self._rx = bytearray()      # bytes waiting for the client to read
        self._open = False
        self._connected = False
        self._await_inv = False     # 5-baud handshake: expecting inverted KB2
        self._live_tick = 0  # advances each live-data read so values move (Phase 3)

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
        self._await_inv = False

    def five_baud_init(self, address: int) -> None:
        if not self.supports_slow_init:
            raise TransportError(
                "this mock is fast-init only (pass supports_slow_init=True)"
            )
        self._rx.clear()
        self._await_inv = False
        self._connected = False
        if address == self.ecu_address:  # a real ECU only answers its own address
            self._rx.extend(b"\x55" + self.key_bytes)
            self._await_inv = True

    def write(self, data: bytes) -> None:
        b = bytes(data)
        if self._await_inv and len(b) == 1:
            # tester sent inverted KB2 -> ECU closes the handshake with the
            # inverted init address (= the ECU address for a KWP slow init)
            self._await_inv = False
            self._connected = True
            self._rx.append((~self.ecu_address) & 0xFF)
            return
        try:
            frame, _ = parse_frame(b)
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

        if sid == 0x83:  # AccessTimingParameter ("set values" and friends)
            self.timing_params = bytes(payload[2:])  # remembered for assertions
            return bytes((0xC3,)) + bytes(payload[1:])

        if sid == 0x3E:  # TesterPresent
            sub = payload[1] if len(payload) > 1 else 0x01
            if sub == 0x02:  # response suppressed
                return None
            return bytes((0x7E, sub))

        if sid == 0x1A:  # ReadEcuIdentification
            rli = payload[1] if len(payload) > 1 else 0
            data = self._identification.get(rli)
            if data is None:
                return bytes((0x7F, 0x1A, 0x31))  # requestOutOfRange
            return bytes((0x5A, rli)) + data

        if sid == 0x21:  # ReadDataByLocalIdentifier (live data, Phase 3)
            # A Keihin serves all live sensors as one packed frame on LID 0x80
            # (the Keihin MODE_READ_SENSORS RLI) — the kwp_local draft layout. Any
            # other record is rejected, like a real ECU for an unknown LID.
            lid = payload[1] if len(payload) > 1 else 0
            if lid != 0x80:
                return bytes((0x7F, 0x21, 0x31))  # requestOutOfRange
            self._live_tick += 1
            return bytes((0x61, lid)) + kwp_live_frame(self._live_tick)

        if sid == 0x03:  # OBD Mode 03 over KWP framing (the standard K-line default)
            body = bytearray((0x43,))
            for hi, lo, _status in self._dtcs:
                body += bytes((hi, lo))
            return bytes(body)

        if sid == 0x18:  # ReadDTCByStatus (ABS/older-ECU variant)
            body = bytearray((0x58, len(self._dtcs)))
            for hi, lo, status in self._dtcs:
                body += bytes((hi, lo, status))
            return bytes(body)

        if sid == 0x14:  # ClearDiagnosticInformation
            self._dtcs.clear()
            return bytes((0x54,))

        # Unknown service -> negative response (serviceNotSupported).
        return bytes((0x7F, sid, 0x11))
