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
from typing import Dict, Iterable, List, Optional, Tuple

from ..transport.base import Transport, TransportError
from .kwp2000 import (
    STATUS_CONFIRMED,
    STATUS_PENDING,
    ConnectionInfo,
    EcuInfo,
    Logger,
    ProtocolError,
    decode_identification_ascii,
    parse_obd_dtc_pairs,
    slow_init_handshake,
)

# OBD-II (SAE J1979) service/mode identifiers we use.
MODE_CURRENT_DATA = 0x01
MODE_STORED_DTC = 0x03
MODE_CLEAR_DTC = 0x04
MODE_PENDING_DTC = 0x07
MODE_VEHICLE_INFO = 0x09
POSITIVE_OFFSET = 0x40

# Mode 09 (vehicle information) PIDs.
VI_PID_VIN = 0x02
VI_PID_CALIBRATION_ID = 0x04
VI_PID_ECU_NAME = 0x0A


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
    id_timeout: float = 0.5                # per-PID wait for Mode 09 (often unsupported)
    live_timeout: float = 0.4              # per-PID wait when polling Mode 01 live data
    dtc_retries: int = 3                   # Mode 03 answers intermittently; retry
    dtc_retry_wait: float = 0.2            # settle between Mode 03 retries


class Iso9141Client:
    """5-baud init + OBD-II client. Same method surface as Kwp2000Client."""

    # OBD Mode 03/07 responses are SAE-J2012 bit-encoded: decode structurally
    # (duck-type parity with Kwp2000Client.dtc_family).
    dtc_family: Optional[str] = None
    # Live data is one Mode 01 request per standardized PID (vs. the KWP
    # path's single packed kwp_local frame).
    live_source = "obd_mode01"

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
        """One 5-baud init attempt via the shared handshake (see kwp2000.py).

        The handshake validation — requiring the ECU's inverted-address close,
        so a garbled init drives a retry instead of a half-open link — lives in
        :func:`slow_init_handshake`, shared with the KWP slow-init path.
        """
        cfg = self.config
        return slow_init_handshake(
            self.transport,
            cfg.init_address,
            w4=cfg.w4,
            sync_timeout=cfg.sync_timeout,
            byte_timeout=cfg.byte_timeout,
            log=self._log,
        )

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

    def keepalive(self) -> None:
        """Keep the ISO 9141-2 link alive between operations.

        Duck-typed peer of :meth:`Kwp2000Client.keepalive`. OBD-II / ISO 9141-2
        have no TesterPresent service, so poke the link with a cheap, read-only
        Mode 01 PID 00 (supported-PIDs) request — enough traffic to avoid the P3
        idle timeout. Raises :class:`ProtocolError` if the ECU has gone away, so
        the keepalive ticker can log the loss.
        """
        self.obd_request(bytes((MODE_CURRENT_DATA, 0x00)))

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
    def _read_status_strict(self) -> Tuple[bool, int]:
        """Mode 01 PID 01 -> (MIL on?, stored DTC count); raises if unanswered.

        On the real Sagem ECU, Mode 01 PID 01 answers reliably where Mode 03
        does not, so it is the authority for whether faults exist. A missing or
        malformed reply therefore means the *session is dead*, not that there
        are zero faults — so raise rather than silently return ``(False, 0)``.
        """
        resp = self.obd_request(bytes((MODE_CURRENT_DATA, 0x01)))
        if (
            len(resp) >= 3
            and resp[0] == MODE_CURRENT_DATA + POSITIVE_OFFSET
            and resp[1] == 0x01
        ):
            a = resp[2]
            return (bool(a & 0x80), a & 0x7F)
        raise ProtocolError(f"unexpected Mode 01 PID 01 response: {self._hex(resp)}")

    def read_status(self) -> Tuple[bool, int]:
        """Best-effort Mode 01 PID 01 -> (MIL on?, stored DTC count)."""
        try:
            return self._read_status_strict()
        except ProtocolError:
            return (False, 0)

    def read_live(self, pids: Iterable[int]) -> Dict[int, bytes]:
        """Poll OBD Mode 01 PIDs; return ``{pid: data_bytes}`` for those answered.

        One Mode 01 request per PID — widely supported and how other live-data
        tools poll too. A PID the ECU doesn't answer (timeout, wrong echo) is
        simply omitted, so a partial dict is normal; the caller decodes whatever
        came back via the shared PID table. Duck-typed peer of
        :meth:`Kwp2000Client.read_live`.
        """
        out: Dict[int, bytes] = {}
        for pid in pids:
            try:
                resp = self.obd_request(
                    bytes((MODE_CURRENT_DATA, pid)), timeout=self.config.live_timeout
                )
            except ProtocolError:
                continue
            # payload: 41 <pid> <data...>
            if (
                len(resp) >= 3
                and resp[0] == MODE_CURRENT_DATA + POSITIVE_OFFSET
                and resp[1] == pid
            ):
                out[pid] = bytes(resp[2:])
        return out

    def read_identification(self) -> EcuInfo:
        """Read ECU identity via OBD Mode 09 (VIN / Calibration ID / ECU name).

        Best-effort: many motorcycle ECUs don't implement Mode 09, so each PID
        is queried with a short timeout and a missing reply yields an empty
        field rather than an error.
        """
        raw: Dict[int, bytes] = {}
        vin = self._read_vehicle_info(VI_PID_VIN, raw)
        calibration = self._read_vehicle_info(VI_PID_CALIBRATION_ID, raw)
        ecu_name = self._read_vehicle_info(VI_PID_ECU_NAME, raw)
        return EcuInfo(
            vin=vin, calibration_id=calibration, ecu_name=ecu_name, raw=raw
        )

    def _read_vehicle_info(self, pid: int, raw: Dict[int, bytes]) -> str:
        try:
            resp = self.obd_request(
                bytes((MODE_VEHICLE_INFO, pid)), timeout=self.config.id_timeout
            )
        except ProtocolError:
            return ""
        # Response payload: 49 <pid> <count> <ascii...>
        if len(resp) < 3 or resp[0] != MODE_VEHICLE_INFO + POSITIVE_OFFSET or resp[1] != pid:
            return ""
        data = resp[2:]
        raw[pid] = bytes(data)
        return decode_identification_ascii(data)

    def _parse_dtc_response(
        self, resp: bytes, mode: int, status: int
    ) -> List[Tuple[int, int, int]]:
        if not resp or resp[0] != mode + POSITIVE_OFFSET:
            raise ProtocolError(
                f"unexpected Mode {mode:02X} response: {self._hex(resp)}"
            )
        return parse_obd_dtc_pairs(resp[1:], status)

    def _request_dtcs(
        self, mode: int, status: int, timeout: Optional[float] = None
    ) -> List[Tuple[int, int, int]]:
        """One DTC request: triples on a positive response (possibly empty).

        Raises :class:`ProtocolError` on no/invalid response, so the caller can
        tell "the ECU said zero codes" apart from "the ECU never answered".
        """
        resp = self.obd_request(bytes((mode,)), timeout=timeout)
        return self._parse_dtc_response(resp, mode, status)

    def _read_stored(self, expected: int) -> List[Tuple[int, int, int]]:
        """Read stored DTCs (Mode 03), reconciled against the reliable count.

        The real Sagem ECU answers Mode 03 only intermittently even with the MIL
        latched, so retry up to ``dtc_retries`` times. ``expected`` is the count
        from Mode 01 PID 01 (the authority): if it says codes exist but Mode 03
        keeps coming back empty, raise — reporting "no codes" for a read that
        never actually enumerated is the bug that made reads look flaky.
        """
        attempts = max(1, self.config.dtc_retries)
        result: List[Tuple[int, int, int]] = []
        for attempt in range(attempts):
            if attempt > 0:
                time.sleep(self.config.dtc_retry_wait)
            try:
                result = self._request_dtcs(MODE_STORED_DTC, STATUS_CONFIRMED)
            except ProtocolError as exc:
                self._log(f"Mode 03 attempt {attempt + 1}/{attempts} failed: {exc}")
                result = []
            if len(result) >= expected:  # expected==0 accepts an empty result
                return result
        if not result and expected > 0:
            raise ProtocolError(
                f"status reports {expected} stored DTC(s) but Mode 03 returned "
                f"none after {attempts} attempts"
            )
        if len(result) < expected:
            self._log(
                f"warning: status reports {expected} stored DTC(s), read {len(result)}"
            )
        return result

    def read_dtcs(self) -> List[Tuple[int, int, int]]:
        """Read stored (Mode 03) + pending (Mode 07) DTCs as (hi, lo, status).

        Mode 01 PID 01 (MIL + count) is this ECU's reliable authority, so it is
        read first: its count drives the Mode 03 retry/reconcile, and a missing
        PID 01 reply surfaces as a hard error (dead session) instead of a false
        "no codes". Pending (Mode 07) is best-effort — unsupported on some ECUs.
        """
        _mil, count = self._read_status_strict()
        stored = self._read_stored(count)
        try:
            pending = self._request_dtcs(
                MODE_PENDING_DTC, STATUS_PENDING, timeout=self.config.pending_timeout
            )
        except ProtocolError:
            pending = []
        seen = {(h, l) for h, l, _ in stored}
        return stored + [(h, l, s) for (h, l, s) in pending if (h, l) not in seen]

    def clear_dtcs(self) -> None:
        """Clear stored DTCs and turn off the MIL (Mode 04)."""
        resp = self.obd_request(bytes((MODE_CLEAR_DTC,)))
        if not resp or resp[0] != MODE_CLEAR_DTC + POSITIVE_OFFSET:
            raise ProtocolError("clear (Mode 04) not acknowledged")
