"""KWP2000 (ISO 14230) client for reading Triumph ECU fault codes.

The defaults below target the common Triumph case: a Keihin ECU speaking
KWP2000 over the K-line at 10400 baud with fast-init and physical addressing.
Exact addresses, the diagnostic-session sub-function, and the DTC service can
vary by model/year and ECU supplier (Keihin vs Sagem), so they live in
:class:`Kwp2000Config` and can be overridden.  See the README and TuneECU for
model-specific values.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..transport.base import Transport, TransportError
from .framing import (
    ADDR_PHYSICAL,
    ChecksumError,
    IncompleteFrame,
    ParsedFrame,
    build_frame,
    frame_length_hint,
    parse_frame,
)

# --- KWP2000 service identifiers ------------------------------------------------
SID_START_COMMUNICATION = 0x81
SID_STOP_COMMUNICATION = 0x82
SID_START_DIAGNOSTIC_SESSION = 0x10
SID_TESTER_PRESENT = 0x3E
SID_CLEAR_DIAGNOSTIC_INFO = 0x14
SID_READ_DTC_BY_STATUS = 0x18
SID_READ_ECU_IDENTIFICATION = 0x1A
SID_READ_DATA_BY_LOCAL_ID = 0x21

POSITIVE_RESPONSE_OFFSET = 0x40
NEGATIVE_RESPONSE = 0x7F
NRC_RESPONSE_PENDING = 0x78

# A subset of ISO 14230 negative-response codes for friendlier messages.
_NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}

Logger = Callable[[str], None]


def decode_identification_ascii(data: bytes) -> str:
    """Best-effort ASCII text from an identification payload.

    Identification responses (OBD Mode 09, KWP ReadEcuIdentification) wrap the
    text in a leading count/NODI byte and may zero-pad it.  Keeping only
    printable ASCII drops both without needing to know the exact framing.
    """
    return "".join(chr(b) for b in data if 0x20 <= b <= 0x7E).strip()


class ProtocolError(Exception):
    """Framing, timeout, or unexpected-response error."""


class NegativeResponse(ProtocolError):
    """The ECU returned a 0x7F negative response."""

    def __init__(self, request_sid: int, nrc: int):
        self.request_sid = request_sid
        self.nrc = nrc
        name = _NRC_NAMES.get(nrc, "unknown")
        super().__init__(
            f"negative response to service 0x{request_sid:02X}: "
            f"NRC 0x{nrc:02X} ({name})"
        )


@dataclass
class Kwp2000Config:
    ecu_address: int = 0x11
    tester_address: int = 0xF1
    addr_mode: int = ADDR_PHYSICAL
    baudrate: int = 10400
    # StartDiagnosticSession sub-function; set to None to skip that step.
    diagnostic_session: Optional[int] = 0x81
    read_dtc_status_mask: int = 0x00     # 0x00 = report DTCs regardless of status
    read_dtc_group: int = 0xFF00         # 0xFF00 = all groups
    clear_dtc_group: int = 0xFF00
    p2_timeout: float = 1.0              # normal max time to a response
    pending_timeout: float = 5.0        # extended wait after a 0x78 (busy)
    max_pending: int = 20
    init_low_ms: int = 25
    init_high_ms: int = 25
    # ReadEcuIdentification record-local-identifiers (model/ECU-specific; set any
    # to None to skip). Defaults follow the common KWP2000 assignments.
    id_vin_rli: Optional[int] = 0x90       # vehicle identification number
    id_hardware_rli: Optional[int] = 0x91  # ECU hardware number
    id_software_rli: Optional[int] = 0x94  # ECU software / calibration version


@dataclass
class ConnectionInfo:
    key_bytes: bytes
    session_started: bool


@dataclass
class EcuInfo:
    """ECU identity, populated best-effort from either protocol path.

    Shared vocabulary (like :class:`ConnectionInfo`): the OBD Mode 09 fields map
    directly; the KWP ``ReadEcuIdentification`` records map onto the same slots
    (software version -> ``calibration_id``, hardware number -> ``ecu_name``).
    """

    vin: str = ""
    calibration_id: str = ""
    ecu_name: str = ""
    raw: Dict[int, bytes] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not (self.vin or self.calibration_id or self.ecu_name)

    def as_rows(self) -> List[Tuple[str, str]]:
        """(label, value) pairs for the fields that were actually read."""
        rows: List[Tuple[str, str]] = []
        if self.ecu_name:
            rows.append(("ECU", self.ecu_name))
        if self.vin:
            rows.append(("VIN", self.vin))
        if self.calibration_id:
            rows.append(("Calibration", self.calibration_id))
        return rows

    def summary(self) -> str:
        """Compact one-line identity for a status bar."""
        return " · ".join(v for v in (self.ecu_name, self.vin) if v)


class Kwp2000Client:
    """Stateless-ish request/response client over a :class:`Transport`."""

    def __init__(
        self,
        transport: Transport,
        config: Optional[Kwp2000Config] = None,
        logger: Optional[Logger] = None,
    ):
        self.transport = transport
        self.config = config or Kwp2000Config()
        self._log = logger or (lambda _msg: None)

    # -- logging helper ------------------------------------------------------
    def _hex(self, data: bytes) -> str:
        return " ".join(f"{b:02X}" for b in data)

    # -- low level request/response -----------------------------------------
    def _read_response_frame(self, timeout: float) -> ParsedFrame:
        deadline = time.monotonic() + timeout
        buf = bytearray()
        expected: Optional[int] = None
        while True:
            if expected is None:
                # Read enough to determine the total frame length.
                need = 4  # worst-case header before length is known
            else:
                need = expected - len(buf)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError(
                    f"timeout waiting for response (got {len(buf)} bytes: "
                    f"{self._hex(bytes(buf))})"
                )
            chunk = self.transport.read(max(need, 1), remaining)
            if chunk:
                buf.extend(chunk)
            if expected is None:
                try:
                    expected = frame_length_hint(bytes(buf))
                except IncompleteFrame:
                    continue
            if expected is not None and len(buf) >= expected:
                frame, _ = parse_frame(bytes(buf))
                self._log(f"<- {self._hex(frame.raw)}")
                return frame

    def _discard_echo(self, frame: bytes) -> None:
        if not self.transport.echoes:
            return
        echo = self.transport.read(len(frame), self.config.p2_timeout)
        if echo != frame:
            self._log(
                f"!! echo mismatch (sent {self._hex(frame)}, "
                f"got {self._hex(echo)})"
            )

    def request(self, payload: bytes, timeout: Optional[float] = None) -> bytes:
        """Send ``payload`` (service id + params); return the response payload.

        Handles echo cancellation, the 0x78 response-pending flow, and raises
        :class:`NegativeResponse` on a 0x7F reply.  Returns the response payload
        including its service id (request sid + 0x40).
        """
        cfg = self.config
        timeout = cfg.p2_timeout if timeout is None else timeout
        frame = build_frame(payload, cfg.ecu_address, cfg.tester_address, cfg.addr_mode)
        self.transport.reset_input()
        self._log(f"-> {self._hex(frame)}")
        try:
            self.transport.write(frame)
            self._discard_echo(frame)
        except TransportError as exc:
            raise ProtocolError(str(exc)) from exc

        request_sid = payload[0]
        pending = 0
        while True:
            try:
                frame_in = self._read_response_frame(timeout)
            except (ChecksumError, IncompleteFrame) as exc:
                raise ProtocolError(f"malformed response: {exc}") from exc
            resp = frame_in.payload
            if not resp:
                raise ProtocolError("empty response payload")
            if resp[0] == NEGATIVE_RESPONSE:
                # 7F <requestSid> <nrc>
                nrc = resp[2] if len(resp) >= 3 else 0
                if nrc == NRC_RESPONSE_PENDING and pending < cfg.max_pending:
                    pending += 1
                    self._log(f".. ECU busy (responsePending {pending})")
                    timeout = cfg.pending_timeout
                    continue
                raise NegativeResponse(resp[1] if len(resp) >= 2 else request_sid, nrc)
            if resp[0] != request_sid + POSITIVE_RESPONSE_OFFSET:
                raise ProtocolError(
                    f"unexpected response service 0x{resp[0]:02X} "
                    f"for request 0x{request_sid:02X}"
                )
            return resp

    # -- high level services -------------------------------------------------
    def start_communication(self) -> bytes:
        """Fast-init the K-line and start a KWP2000 session; return key bytes."""
        self._log("fast-init …")
        try:
            self.transport.fast_init(self.config.init_low_ms, self.config.init_high_ms)
        except TransportError as exc:
            raise ProtocolError(f"fast-init failed: {exc}") from exc
        resp = self.request(bytes((SID_START_COMMUNICATION,)))
        key_bytes = resp[1:]
        self._log(f"connected, key bytes: {self._hex(key_bytes)}")
        return key_bytes

    def start_diagnostic_session(self, session: Optional[int] = None) -> bytes:
        session = self.config.diagnostic_session if session is None else session
        if session is None:
            return b""
        return self.request(bytes((SID_START_DIAGNOSTIC_SESSION, session)))

    def stop_communication(self) -> None:
        try:
            self.request(bytes((SID_STOP_COMMUNICATION,)))
        except ProtocolError as exc:
            self._log(f"stop-communication ignored: {exc}")

    def tester_present(self, response_required: bool = False) -> None:
        sub = 0x01 if response_required else 0x02  # 0x02 = no positive response
        frame = build_frame(
            bytes((SID_TESTER_PRESENT, sub)),
            self.config.ecu_address,
            self.config.tester_address,
            self.config.addr_mode,
        )
        self.transport.reset_input()
        self.transport.write(frame)
        self._discard_echo(frame)
        if response_required:
            self._read_response_frame(self.config.p2_timeout)

    def keepalive(self) -> None:
        """Keep the session alive between operations (TesterPresent, no reply).

        Duck-typed peer of :meth:`Iso9141Client.keepalive`; called on an
        interval by :class:`~trecu.service.DiagnosticService` so the ECU doesn't
        time the KWP2000 session out (P3max) while idle.
        """
        self.tester_present(response_required=False)

    def connect(self) -> ConnectionInfo:
        """Full connect sequence: fast-init + StartCommunication + session."""
        key_bytes = self.start_communication()
        started = False
        if self.config.diagnostic_session is not None:
            try:
                self.start_diagnostic_session()
                started = True
            except NegativeResponse as exc:
                # Not fatal — some ECUs read DTCs in the default session.
                self._log(f"diagnostic session not started: {exc}")
        return ConnectionInfo(key_bytes=key_bytes, session_started=started)

    def read_dtcs(self) -> List[Tuple[int, int, int]]:
        """Read stored DTCs; return a list of ``(high, low, status)`` triples."""
        cfg = self.config
        payload = bytes(
            (
                SID_READ_DTC_BY_STATUS,
                cfg.read_dtc_status_mask,
                (cfg.read_dtc_group >> 8) & 0xFF,
                cfg.read_dtc_group & 0xFF,
            )
        )
        resp = self.request(payload)
        # Response: 58 <count> [hi lo status] * count
        if len(resp) < 2:
            raise ProtocolError("short readDTC response")
        count = resp[1]
        body = resp[2:]
        triples: List[Tuple[int, int, int]] = []
        for i in range(0, len(body) - 2, 3):
            triples.append((body[i], body[i + 1], body[i + 2]))
        if count and len(triples) != count:
            self._log(
                f"note: ECU reported {count} DTCs, parsed {len(triples)} triples"
            )
        return triples

    def read_live(self, pids: Iterable[int]) -> Dict[int, bytes]:
        """Poll live data via ReadDataByLocalIdentifier (SID 0x21).

        Duck-typed peer of :meth:`Iso9141Client.read_live`. **Placeholder record
        mapping:** each requested id is read as a single RDBLI record and its
        data bytes returned as-is, decoded by the shared PID table. Real Triumph
        ECUs pack *several* sensors into each model-specific record layout (see
        TuneECU) — those layouts aren't in this codebase and await a hardware
        capture (roadmap F4). The 1:1 id->record convention here keeps this path
        exercisable against the mock and symmetric with the OBD path. A record
        the ECU rejects (negative response) is omitted.
        """
        out: Dict[int, bytes] = {}
        for pid in pids:
            try:
                resp = self.request(bytes((SID_READ_DATA_BY_LOCAL_ID, pid)))
            except ProtocolError:
                continue
            # response: 61 <lid> <data...>
            if len(resp) >= 3 and resp[1] == pid:
                out[pid] = bytes(resp[2:])
        return out

    def read_identification(self) -> EcuInfo:
        """Read ECU identity via ReadEcuIdentification (best-effort per record).

        Each configured record-local-identifier is queried independently; an
        unsupported record (negative response) yields an empty field rather than
        failing the whole call.
        """
        cfg = self.config
        raw: Dict[int, bytes] = {}
        vin = self._read_ecu_id(cfg.id_vin_rli, raw)
        hardware = self._read_ecu_id(cfg.id_hardware_rli, raw)
        software = self._read_ecu_id(cfg.id_software_rli, raw)
        return EcuInfo(
            vin=vin, ecu_name=hardware, calibration_id=software, raw=raw
        )

    def _read_ecu_id(self, rli: Optional[int], raw: Dict[int, bytes]) -> str:
        if rli is None:
            return ""
        try:
            resp = self.request(bytes((SID_READ_ECU_IDENTIFICATION, rli)))
        except ProtocolError as exc:
            self._log(f"ReadEcuIdentification 0x{rli:02X} skipped: {exc}")
            return ""
        # Response: 5A <rli> <data...>
        if len(resp) < 3 or resp[1] != rli:
            return ""
        data = resp[2:]
        raw[rli] = bytes(data)
        return decode_identification_ascii(data)

    def clear_dtcs(self) -> None:
        """Clear stored DTCs (clearDiagnosticInformation, all groups)."""
        cfg = self.config
        payload = bytes(
            (
                SID_CLEAR_DIAGNOSTIC_INFO,
                (cfg.clear_dtc_group >> 8) & 0xFF,
                cfg.clear_dtc_group & 0xFF,
            )
        )
        self.request(payload)
