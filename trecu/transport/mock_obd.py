"""Simulated Triumph ECU speaking 5-baud init + OBD-II over ISO 9141-2.

Mirrors the real bike observed over the KKL cable: 5-baud init at 0x33, key
bytes 08 08, and OBD Mode 01/03/04 with the ``48 6B ..`` response header.  The
default stored fault (constructed with no ``dtcs=``) is the single real-bike
P1108 with the MIL on — the deterministic ground truth the test-suite relies
on.  The ``trecu --mock`` CLI overrides that with a random, type-varied set of
real DB codes (``DtcDatabase.random_dtcs``) so a demo run shows a plausible
spread of faults rather than one canned code.
"""

from __future__ import annotations

from typing import List, Tuple

from ._mock_live import sensor_data
from .base import Transport, TransportError

# Default stored fault: 0x1108 -> P1108 (ambient air pressure sensor).
_DEFAULT_DTCS: List[Tuple[int, int]] = [(0x11, 0x08)]


class MockObdTransport(Transport):
    """In-memory ECU emulator for the ISO 9141-2 / OBD path."""

    echoes = False
    supports_fast_init = False
    supports_slow_init = True

    def __init__(
        self,
        dtcs: List[Tuple[int, int]] | None = None,
        mil: bool = True,
        key_bytes: bytes = b"\x08\x08",
        init_address: int = 0x33,
        resp_header: bytes = b"\x48\x6B\xD1",
        vin: str = "SMTA469N4KT700001",
        calibration_id: str = "T1291234",
        ecu_name: str = "Sagem MC2000",
    ):
        self._dtcs = list(_DEFAULT_DTCS if dtcs is None else dtcs)
        self.mil = mil
        self.key_bytes = key_bytes
        self.init_address = init_address
        self.resp_header = resp_header
        # Mode 09 vehicle-information strings ("" = PID unsupported, no reply).
        self._vehicle_info = {0x02: vin, 0x04: calibration_id, 0x0A: ecu_name}
        self._rx = bytearray()
        self._await_inv = False
        self._live_tick = 0  # advances each live-PID read so values move (Phase 3)

    # -- Transport interface -------------------------------------------------
    def open(self) -> None:
        self._rx.clear()

    def close(self) -> None:
        self._rx.clear()

    def reset_input(self) -> None:
        self._rx.clear()

    def read(self, count: int, timeout: float) -> bytes:
        n = min(count, len(self._rx))
        out = bytes(self._rx[:n])
        del self._rx[:n]
        return out

    def fast_init(self, low_ms: int = 25, high_ms: int = 25) -> None:
        raise TransportError("mock OBD ECU does not support fast-init")

    def five_baud_init(self, address: int) -> None:
        self._rx.clear()
        self._await_inv = False
        if address == self.init_address:
            # a little break-pulse noise, then sync + key bytes
            self._rx.extend(b"\x00\x00" + b"\x55" + self.key_bytes)
            self._await_inv = True

    def write(self, data: bytes) -> None:
        b = bytes(data)
        if self._await_inv and len(b) == 1:
            # tester sent inverted KB2 -> ECU replies with inverted address
            self._await_inv = False
            self._rx.append((~self.init_address) & 0xFF)
            return
        if len(b) >= 5 and b[0] == 0x68:  # OBD request: 68 6A F1 <mode> [pid] cs
            self._respond(b[3:-1])

    # -- ECU behaviour -------------------------------------------------------
    def _emit(self, payload: bytes) -> None:
        frame = self.resp_header + payload
        self._rx.extend(frame + bytes((sum(frame) & 0xFF,)))

    def _respond(self, payload: bytes) -> None:
        if not payload:
            return
        mode = payload[0]
        if mode == 0x01:  # current data
            pid = payload[1] if len(payload) > 1 else 0
            if pid == 0x01:  # monitor status: MIL + DTC count
                a = (0x80 if self.mil else 0x00) | (len(self._dtcs) & 0x7F)
                self._emit(bytes((0x41, 0x01, a, 0x00, 0x00, 0xFF)))
            elif pid == 0x00:  # supported PIDs
                self._emit(bytes((0x41, 0x00, 0xBD, 0x36, 0x91, 0x10)))
            else:
                # Live sensor PIDs (Phase 3): reply with plausible, moving data.
                # An unmodelled PID gets no reply, like a real ECU ignoring an
                # unsupported PID — so read_live omits it rather than surfacing a
                # bogus zero.
                data = sensor_data(pid, self._live_tick)
                if data is not None:
                    self._live_tick += 1
                    self._emit(bytes((0x41, pid)) + data)
        elif mode == 0x03:  # stored DTCs
            # Serve *every* stored DTC (padded to a 3-pair frame when there are
            # fewer): the client reconciles this against the Mode 01 PID 01
            # count, so capping the list here would make a >3-fault read look
            # like a count/enumeration mismatch.
            body = bytearray((0x43,))
            slots = list(self._dtcs)
            while len(slots) < 3:
                slots.append((0x00, 0x00))
            for hi, lo in slots:
                body += bytes((hi, lo))
            self._emit(bytes(body))
        elif mode == 0x04:  # clear DTCs + MIL
            self._dtcs.clear()
            self.mil = False
            self._emit(bytes((0x44,)))
        elif mode == 0x09:  # vehicle information (VIN / cal ID / ECU name)
            pid = payload[1] if len(payload) > 1 else 0
            text = self._vehicle_info.get(pid, "")
            if text:
                # 49 <pid> <count=1> <ascii...>
                self._emit(bytes((0x49, pid, 0x01)) + text.encode("ascii", "ignore"))
        elif mode == 0x07:  # pending DTCs — unsupported on this ECU (no reply)
            return
        # anything else: no response, like the real ECU
