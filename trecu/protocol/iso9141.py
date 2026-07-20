"""ISO 9141-2 (5-baud slow init) + OBD-II services over the K-line.

This is the path confirmed on a real Triumph: the ECU requires a 5-baud slow
init at address 0x33, answers with sync 0x55 + key bytes 08 08, and then speaks
standard OBD-II (SAE J1979) request/response with the ISO 9141-2 header
``68 6A F1``.  DTCs are read with Mode 03 (stored) / Mode 07 (pending) and
cleared with Mode 04.

Request : 68 6A F1 <mode> [pid] <cs>
Response: 48 6B <src> <mode+0x40> <data...> <cs>
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..transport.base import Transport, TransportError
from .kwp2000 import ConnectionInfo, Logger, ProtocolError

# OBD-II (SAE J1979) service/mode identifiers we use.
MODE_CURRENT_DATA = 0x01
MODE_STORED_DTC = 0x03
MODE_CLEAR_DTC = 0x04
MODE_PENDING_DTC = 0x07
POSITIVE_OFFSET = 0x40

# Synthetic per-DTC status bytes so decoded codes carry a meaningful label
# (OBD Mode 03/07 do not include a KWP-style status byte).
STATUS_CONFIRMED = 0x08  # decode_status -> "confirmed"
STATUS_PENDING = 0x04    # decode_status -> "pending"


@dataclass
class Iso9141Config:
    init_address: int = 0x33               # 5-baud init address
    header: Tuple[int, int, int] = (0x68, 0x6A, 0xF1)  # OBD physical request header
    baudrate: int = 10400
    w4: float = 0.030                      # gap before sending inverted key byte
    p2_timeout: float = 0.8                # max wait for a response
    pending_timeout: float = 0.3           # shorter wait for optional Mode 07
    quiet_gap: float = 0.05                # end-of-message idle gap
    sync_timeout: float = 0.6              # wait for the 0x55 sync after init
    byte_timeout: float = 0.4              # wait for a single handshake byte
    request_gap: float = 0.06              # min idle between requests (P3)
    init_retries: int = 4                  # slow-init can need a few tries
    retry_wait: float = 2.0                # settle time between init attempts


class Iso9141Client:
    """5-baud init + OBD-II client. Same method surface as Kwp2000Client."""

    def __init__(
        self,
        transport: Transport,
        config: Optional[Iso9141Config] = None,
        logger: Optional[Logger] = None,
    ):
        self.transport = transport
        self.config = config or Iso9141Config()
        self._log = logger or (lambda _msg: None)

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _hex(data: bytes) -> str:
        return " ".join(f"{b:02X}" for b in data)

    def _read_byte(self, timeout: float) -> Optional[int]:
        b = self.transport.read(1, timeout)
        return b[0] if b else None

    def _read_sync(self, timeout: float) -> Optional[int]:
        """Read bytes until the 0x55 sync appears, skipping break-pulse noise."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            b = self.transport.read(1, remaining)
            if b and b[0] == 0x55:
                return 0x55

    def _collect(self, timeout: float) -> bytes:
        """Collect a whole response: read until an idle gap or the timeout."""
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = self.transport.read(64, min(self.config.quiet_gap, remaining))
            if chunk:
                buf.extend(chunk)
            elif buf:
                break  # got something, then a quiet gap -> message complete
        return bytes(buf)

    # -- init / connect ------------------------------------------------------
    def _slow_init(self) -> bytes:
        t = self.transport
        if not getattr(t, "supports_slow_init", False):
            raise ProtocolError("transport does not support 5-baud slow init")
        cfg = self.config
        t.reset_input()
        self._log(f"5-baud init @ 0x{cfg.init_address:02X} …")
        t.five_baud_init(cfg.init_address)

        if self._read_sync(cfg.sync_timeout) != 0x55:
            raise ProtocolError("no 0x55 sync byte after 5-baud init")
        kb1 = self._read_byte(cfg.byte_timeout)
        kb2 = self._read_byte(cfg.byte_timeout)
        if kb1 is None or kb2 is None:
            raise ProtocolError("missing key bytes after sync")

        time.sleep(cfg.w4)  # W4
        inv = (~kb2) & 0xFF
        t.reset_input()
        t.write(bytes((inv,)))
        if t.echoes:
            self._read_byte(cfg.byte_timeout)  # discard echo of our inverted byte
        inv_addr = self._read_byte(cfg.byte_timeout)
        self._log(
            f"slow-init ok: key bytes {kb1:02X} {kb2:02X}, "
            f"inv-addr {'--' if inv_addr is None else f'{inv_addr:02X}'}"
        )
        return bytes((kb1, kb2))

    def connect(self) -> ConnectionInfo:
        if not getattr(self.transport, "supports_slow_init", False):
            raise ProtocolError("transport does not support 5-baud slow init")
        last: Optional[Exception] = None
        for attempt in range(self.config.init_retries):
            if attempt > 0:
                self._log(
                    f"slow-init retry {attempt} (settle {self.config.retry_wait}s)"
                )
                time.sleep(self.config.retry_wait)
            try:
                key = self._slow_init()
                return ConnectionInfo(key_bytes=key, session_started=True)
            except (ProtocolError, TransportError) as exc:
                last = exc
                self._log(f"slow-init attempt {attempt + 1} failed: {exc}")
        raise ProtocolError(f"5-baud init failed: {last}")

    def stop_communication(self) -> None:
        # ISO 9141 has no explicit stop; the session just times out.
        return None

    # -- OBD request/response ------------------------------------------------
    def obd_request(self, data: bytes, timeout: Optional[float] = None) -> bytes:
        """Send an OBD request; return the response payload (mode byte + data)."""
        cfg = self.config
        timeout = cfg.p2_timeout if timeout is None else timeout
        body = bytes(cfg.header) + data
        frame = body + bytes((sum(body) & 0xFF,))
        t = self.transport
        time.sleep(cfg.request_gap)
        t.reset_input()
        self._log(f"-> {self._hex(frame)}")
        try:
            t.write(frame)
            if t.echoes:
                self._read_exact(len(frame), cfg.p2_timeout)  # discard echo
        except TransportError as exc:
            raise ProtocolError(str(exc)) from exc

        raw = self._collect(timeout)
        if not raw:
            raise ProtocolError("no OBD response (timeout)")
        self._log(f"<- {self._hex(raw)}")
        # Trim any leading noise before the 0x48 response header.
        start = raw.find(0x48)
        frame_in = raw[start:] if start >= 0 else raw
        if len(frame_in) < 5:
            raise ProtocolError(f"short OBD response: {self._hex(raw)}")
        if (sum(frame_in[:-1]) & 0xFF) != frame_in[-1]:
            self._log("!! OBD response checksum mismatch (continuing best-effort)")
        return frame_in[3:-1]  # strip 3-byte header + checksum -> mode + data

    def _read_exact(self, count: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            chunk = self.transport.read(count - len(buf), remaining)
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    # -- high level ----------------------------------------------------------
    def read_status(self) -> Tuple[bool, int]:
        """Mode 01 PID 01 -> (MIL on?, stored DTC count)."""
        try:
            resp = self.obd_request(bytes((MODE_CURRENT_DATA, 0x01)))
        except ProtocolError:
            return (False, 0)
        if len(resp) >= 3 and resp[0] == MODE_CURRENT_DATA + POSITIVE_OFFSET:
            a = resp[2]
            return (bool(a & 0x80), a & 0x7F)
        return (False, 0)

    def _read_dtc_mode(
        self, mode: int, status: int, timeout: Optional[float] = None
    ) -> List[Tuple[int, int, int]]:
        try:
            resp = self.obd_request(bytes((mode,)), timeout=timeout)
        except ProtocolError:
            return []
        if not resp or resp[0] != mode + POSITIVE_OFFSET:
            return []
        body = resp[1:]  # DTC byte pairs
        out: List[Tuple[int, int, int]] = []
        for i in range(0, len(body) - 1, 2):
            hi, lo = body[i], body[i + 1]
            if hi == 0 and lo == 0:
                continue
            out.append((hi, lo, status))
        return out

    def read_dtcs(self) -> List[Tuple[int, int, int]]:
        """Read stored (Mode 03) and pending (Mode 07) DTCs as (hi, lo, status)."""
        stored = self._read_dtc_mode(MODE_STORED_DTC, STATUS_CONFIRMED)
        pending = self._read_dtc_mode(
            MODE_PENDING_DTC, STATUS_PENDING, timeout=self.config.pending_timeout
        )
        seen = {(h, l) for h, l, _ in stored}
        return stored + [(h, l, s) for (h, l, s) in pending if (h, l) not in seen]

    def clear_dtcs(self) -> None:
        """Clear stored DTCs and turn off the MIL (Mode 04)."""
        resp = self.obd_request(bytes((MODE_CLEAR_DTC,)))
        if not resp or resp[0] != MODE_CLEAR_DTC + POSITIVE_OFFSET:
            raise ProtocolError("clear (Mode 04) not acknowledged")
