"""Simulated Triumph ECU speaking 5-baud init + OBD-II over ISO 9141-2.

Mirrors the real bike observed over the KKL cable: 5-baud init at 0x33, key
bytes 08 08, and OBD Mode 01/03/04 with the ``48 6B ..`` response header.  The
default stored fault (constructed with no ``dtcs=``) is the single real-bike
P1108 with the MIL on — the deterministic ground truth the test-suite relies
on.  The ``trecu --mock`` CLI overrides that with a random, type-varied set of
real DB codes (``DtcDatabase.random_dtcs``) so a demo run shows a plausible
spread of faults rather than one canned code.

Its **capability bitmap is the bike's too**: Mode 01 PID 00 answers with the
observed ``41 00 BD 36 91 10``, and a PID that bitmap does not advertise gets no
reply at all, like an ECU ignoring a PID it doesn't implement.  Because the mock
models live values for only *some* of the advertised PIDs, it reproduces all
three states a capability-aware poll has to keep apart — advertised, answered,
and understood by TrECU.  Pass ``support_pages=`` (see
:func:`~trecu.protocol.common.encode_pid_support_pages`) for an ECU that
advertises a different set.

Framing is the real thing too, because the client now validates it: every frame
carries a correct checksum and at most :data:`MAX_DATA_BYTES` of data, so an
answer that doesn't fit — more than three DTCs, any Mode 09 string — is emitted
as several back-to-back frames rather than one oversized one.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from ..protocol.common import MAX_DATA_BYTES, parse_pid_support_bitmap
from ._mock_live import sensor_data
from .base import Transport

# Default stored fault: 0x1108 -> P1108 (ambient air pressure sensor).
_DEFAULT_DTCS: List[Tuple[int, int]] = [(0x11, 0x08)]

#: Mode 01 supported-PID bitmap observed on the tested Triumph, byte for byte:
#: ``41 00 BD 36 91 10``. It advertises PIDs 01 03 04 05 06 08 0B 0C 0E 0F 11 14
#: 18 1C and, with its last bit clear, *no* second page — so the bike supports
#: neither PID 33 (barometric pressure) nor PID 42 (battery voltage).
_DEFAULT_SUPPORT_PAGES: Dict[int, bytes] = {0x00: b"\xBD\x36\x91\x10"}

#: Data bytes per Mode 09 response frame (49 <pid> <seq> fill the rest).
_VI_FRAGMENT = MAX_DATA_BYTES - 3


class MockObdTransport(Transport):
    """In-memory ECU emulator for the ISO 9141-2 / OBD path."""

    echoes = False
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
        support_pages: Optional[Dict[int, bytes]] = None,
    ):
        self._dtcs = list(_DEFAULT_DTCS if dtcs is None else dtcs)
        self.mil = mil
        self.key_bytes = key_bytes
        self.init_address = init_address
        self.resp_header = resp_header
        # Mode 01 capability: {page base -> 4 bitmap bytes}. A page absent from
        # here is simply not answered, which is also how an ECU that advertises
        # a next page then stays silent behaves (the partial-discovery case).
        self.support_pages = dict(
            _DEFAULT_SUPPORT_PAGES if support_pages is None else support_pages
        )
        # Every OBD request payload this ECU was sent, in order — so a test can
        # assert what the tester asked for, not merely what it made of the
        # answers (a PID that is never requested costs no timeout).
        self.requests: List[bytes] = []
        # Mode 09 vehicle-information strings ("" = PID unsupported, no reply).
        self._vehicle_info = {0x02: vin, 0x04: calibration_id, 0x0A: ecu_name}
        self._rx = bytearray()
        self._await_inv = False
        self._live_tick = 0  # advances each live-PID read so values move (Phase 3)

    @property
    def supported_pids(self) -> Set[int]:
        """PIDs :attr:`support_pages` advertises — derived, never set apart.

        Kept as a view of the bitmap rather than a second field, so a test that
        changes what this ECU advertises mid-session cannot leave it answering
        for capability it no longer claims.
        """
        return {
            pid
            for base, bitmap in self.support_pages.items()
            for pid in parse_pid_support_bitmap(base, bitmap)
        }

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
            self.requests.append(b[3:-1])
            self._respond(b[3:-1])

    # -- ECU behaviour -------------------------------------------------------
    def _emit(self, payload: bytes) -> None:
        """Queue one response frame: header + payload + checksum.

        The payload is the ISO 9141-2 data field, so it may not exceed
        :data:`MAX_DATA_BYTES` — a real ECU splits a longer answer across
        frames, and the client rejects an oversized one. Emitting one here would
        be the mock inventing framing no bike produces, so it is an error.
        """
        if not 1 <= len(payload) <= MAX_DATA_BYTES:
            raise ValueError(
                f"mock ECU frame payload must be 1..{MAX_DATA_BYTES} bytes, "
                f"got {len(payload)}"
            )
        frame = self.resp_header + payload
        self._rx.extend(frame + bytes((sum(frame) & 0xFF,)))

    def _respond(self, payload: bytes) -> None:
        if not payload:
            return
        mode = payload[0]
        if mode == 0x01:  # current data
            pid = payload[1] if len(payload) > 1 else 0
            if pid in self.support_pages:  # capability bitmap for this page
                self._emit(bytes((0x41, pid)) + self.support_pages[pid])
            elif pid == 0x01:
                # Monitor status: MIL + DTC count. Answered whatever the bitmap
                # says, because J1979 makes it mandatory and it is this ECU's
                # authority on stored faults (the client never gates it either).
                a = (0x80 if self.mil else 0x00) | (len(self._dtcs) & 0x7F)
                self._emit(bytes((0x41, 0x01, a, 0x00, 0x00, 0xFF)))
            elif pid in self.supported_pids:
                # Live sensor PIDs (Phase 3): reply with plausible, moving data.
                # An advertised PID this mock doesn't model stays silent — the
                # "advertised but unanswered" state a capability-aware poll must
                # keep distinct from "not advertised" and from "not decodable".
                data = sensor_data(pid, self._live_tick)
                if data is not None:
                    self._live_tick += 1
                    self._emit(bytes((0x41, pid)) + data)
            # A PID outside the bitmap gets nothing at all: a real ECU does not
            # answer for capability it never claimed.
        elif mode == 0x03:  # stored DTCs
            # Serve *every* stored DTC, three pairs per frame: one frame holds
            # the mode byte plus three pairs and that fills the data field, so a
            # longer list is several back-to-back frames, exactly as a real ECU
            # answers. Capping at one frame would make a >3-fault read look like
            # a count/enumeration mismatch against Mode 01 PID 01.
            slots = list(self._dtcs)
            while len(slots) % 3 or not slots:
                slots.append((0x00, 0x00))  # pad the last frame out to 3 pairs
            for start in range(0, len(slots), 3):
                body = bytearray((0x43,))
                for hi, lo in slots[start : start + 3]:
                    body += bytes((hi, lo))
                self._emit(bytes(body))
        elif mode == 0x04:  # clear DTCs + MIL
            self._dtcs.clear()
            self.mil = False
            self._emit(bytes((0x44,)))
        elif mode == 0x09:  # vehicle information (VIN / cal ID / ECU name)
            pid = payload[1] if len(payload) > 1 else 0
            self._emit_vehicle_info(pid, self._vehicle_info.get(pid, ""))
        elif mode == 0x07:  # pending DTCs — unsupported on this ECU (no reply)
            return
        # anything else: no response, like the real ECU

    def _emit_vehicle_info(self, pid: int, text: str) -> None:
        """Answer Mode 09 as J1979 does: numbered frames of four data bytes.

        A VIN or calibration ID is far longer than the data field, so the ECU
        sends ``49 <pid> <seq> <4 bytes>`` frames with ``seq`` counting from 1
        and the text NUL-padded out to a whole number of frames (front-padded
        for the 17-character VIN, as J1979 specifies). An empty string means the
        PID is unsupported: no reply at all.
        """
        if not text:
            return
        data = text.encode("ascii", "ignore")
        pad = -len(data) % _VI_FRAGMENT
        data = b"\x00" * pad + data if pid == 0x02 else data + b"\x00" * pad
        for seq in range(len(data) // _VI_FRAGMENT):
            start = seq * _VI_FRAGMENT
            self._emit(
                bytes((0x49, pid, seq + 1)) + data[start : start + _VI_FRAGMENT]
            )
