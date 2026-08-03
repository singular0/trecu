"""KWP2000 (ISO 14230) client for reading Triumph ECU fault codes.

The defaults below target the real Triumph Keihin K-line case as documented by
community reverse engineering: ECU address ``0xD5`` / tester ``0xF5``
(headers ``81/82 D5 F5``), StartDiagnosticSession ``10 02``, DTCs read with OBD
Mode 03 carried over KWP framing, and AccessTimingParameter after connect.
Exact addresses, the diagnostic-session sub-function, and the DTC service can
still vary by model/year and ECU supplier (Keihin vs Sagem), so they live in
:class:`Kwp2000Config` and can be overridden.

This module is also the home of the *shared* protocol vocabulary
(:class:`EcuClient`, :class:`ConnectionInfo`, :class:`EcuInfo`,
:class:`ProtocolError`, ...) and of the service logic both clients share: the
5-baud slow init (:class:`SlowInitConfig`, :func:`slow_init_handshake`, and the
:func:`slow_init_with_retries` loop both clients connect through) and OBD DTC
pair parsing (:func:`parse_obd_dtc_pairs`) — ``iso9141.py`` imports them here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Protocol, Tuple, runtime_checkable

from ..logging import Logger, LoggerLike, as_logger
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
SID_STOP_DIAGNOSTIC_SESSION = 0x20
SID_ACCESS_TIMING_PARAMETER = 0x83
SID_TESTER_PRESENT = 0x3E
SID_CLEAR_DIAGNOSTIC_INFO = 0x14
SID_READ_DTC_BY_STATUS = 0x18
SID_READ_ECU_IDENTIFICATION = 0x1A
SID_READ_DATA_BY_LOCAL_ID = 0x21
# OBD (SAE J1979) Mode 03, carried over KWP framing — the default DTC
# read on the Triumph K-line (0x18 ReadDTCByStatus is the ABS/older variant).
SID_OBD_MODE_STORED_DTC = 0x03

# Synthetic per-DTC status bytes for OBD Mode 03/07 reads, which carry no
# KWP-style statusOfDTC byte. Values chosen so decode_status() yields a
# meaningful label. Shared by both protocol clients.
STATUS_CONFIRMED = 0x08  # decode_status -> "confirmed"
STATUS_PENDING = 0x04    # decode_status -> "pending"

POSITIVE_RESPONSE_OFFSET = 0x40
NEGATIVE_RESPONSE = 0x7F
NRC_RESPONSE_PENDING = 0x78

_SID_NAMES = {
    SID_START_COMMUNICATION: "StartCommunication",
    SID_STOP_COMMUNICATION: "StopCommunication",
    SID_START_DIAGNOSTIC_SESSION: "StartDiagnosticSession",
    SID_STOP_DIAGNOSTIC_SESSION: "StopDiagnosticSession",
    SID_ACCESS_TIMING_PARAMETER: "AccessTimingParameter",
    SID_TESTER_PRESENT: "TesterPresent",
    SID_CLEAR_DIAGNOSTIC_INFO: "ClearDiagnosticInformation",
    SID_READ_DTC_BY_STATUS: "ReadDTCByStatus",
    SID_READ_ECU_IDENTIFICATION: "ReadEcuIdentification",
    SID_READ_DATA_BY_LOCAL_ID: "ReadDataByLocalIdentifier",
    SID_OBD_MODE_STORED_DTC: "OBD Mode 03 (stored DTCs)",
}

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


def parse_obd_dtc_pairs(body: bytes, status: int) -> List[Tuple[int, int, int]]:
    """Parse OBD Mode 03/07 DTC byte pairs into ``(hi, lo, status)`` triples.

    ``body`` is the response payload *after* the mode byte. All-zero pairs
    (frame padding on ISO 9141-2; absent under KWP's explicit length) are
    skipped; ``status`` is the synthetic status attached to every triple.
    Shared by the iso9141 client and the KWP client's Mode-03-over-KWP read.
    """
    out: List[Tuple[int, int, int]] = []
    for i in range(0, len(body) - 1, 2):
        hi, lo = body[i], body[i + 1]
        if hi == 0 and lo == 0:
            continue
        out.append((hi, lo, status))
    return out


@dataclass
class SlowInitConfig:
    """Timing and retry policy for the 5-baud slow init.

    One concern, one place: the ISO 9141-2 init at ``0x33`` and the Keihin KWP
    init at the ECU address ``0xD5`` run the *identical* waveform under the
    identical retry discipline, so :class:`~trecu.protocol.iso9141.Iso9141Config`
    and :class:`Kwp2000Config` each carry this as their ``slow_init`` section
    rather than repeating the five fields. Only the address differs, and that
    stays in the protocol's own config because it is a protocol fact.
    """

    w4: float = 0.030            # gap before sending the inverted key byte
    sync_timeout: float = 0.6    # wait for the 0x55 sync after init
    byte_timeout: float = 0.4    # wait for a single handshake byte
    init_retries: int = 4        # slow-init can need a few tries
    retry_wait: float = 2.0      # settle time between init attempts


def slow_init_handshake(
    transport: Transport,
    address: int,
    config: Optional[SlowInitConfig] = None,
    *,
    log: Optional[LoggerLike] = None,
) -> bytes:
    """Run the ISO 9141-2 / ISO 14230 5-baud slow-init handshake at ``address``.

    Shared by both protocol clients — the waveform and validation are identical;
    only the init address differs (``0x33``/``0x43`` for the ISO 9141-2 "OBD"
    init, the ECU address ``0xD5`` for a Keihin KWP slow init). Drives the
    transport's ``five_baud_init``, waits for the ``0x55`` sync + two key
    bytes, answers with the inverted second key byte, and **requires** the
    ECU's inverted-address close. Returns the two key bytes.

    This is *one* attempt; callers want :func:`slow_init_with_retries`.
    """
    cfg = config or SlowInitConfig()
    w4, sync_timeout, byte_timeout = cfg.w4, cfg.sync_timeout, cfg.byte_timeout
    if not transport.supports_slow_init:
        raise ProtocolError("transport does not support 5-baud slow init")

    def read_byte(timeout: float) -> Optional[int]:
        b = transport.read(1, timeout)
        return b[0] if b else None

    logger = as_logger(log, verbose=True)
    transport.reset_input()
    logger.debug(f"5-baud init @ 0x{address:02X} ...")
    transport.five_baud_init(address)

    # Read until the 0x55 sync appears, skipping break-pulse noise.
    deadline = time.monotonic() + sync_timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProtocolError("no 0x55 sync byte after 5-baud init")
        b = transport.read(1, remaining)
        if b and b[0] == 0x55:
            break
    kb1 = read_byte(byte_timeout)
    kb2 = read_byte(byte_timeout)
    if kb1 is None or kb2 is None:
        raise ProtocolError("missing key bytes after sync")

    time.sleep(w4)  # W4
    inv = (~kb2) & 0xFF
    transport.reset_input()
    transport.write(bytes((inv,)))
    if transport.echoes:
        read_byte(byte_timeout)  # discard echo of our inverted byte
    inv_addr = read_byte(byte_timeout)
    # The ECU closes the handshake by echoing the inverted init address; its
    # arrival is proof it accepted our key-byte reply. A missing or wrong
    # inv-addr means the init didn't take (seen on the real bike when 5-baud
    # timing drifts and the key bytes come back garbled) — reject so the
    # caller's retry loop tries again rather than proceeding on a dead link.
    expected = (~address) & 0xFF
    if inv_addr is None:
        raise ProtocolError(
            f"slow-init handshake incomplete (key bytes {kb1:02X} {kb2:02X}): "
            "no inverted-address reply"
        )
    if inv_addr != expected:
        raise ProtocolError(
            f"slow-init handshake mismatch: inv-addr {inv_addr:02X}, "
            f"expected {expected:02X}"
        )
    logger.debug(
        f"slow-init ok: key bytes {kb1:02X} {kb2:02X}, inv-addr {inv_addr:02X}"
    )
    return bytes((kb1, kb2))


def slow_init_with_retries(
    transport: Transport,
    address: int,
    config: Optional[SlowInitConfig] = None,
    *,
    log: Optional[LoggerLike] = None,
) -> bytes:
    """Retry :func:`slow_init_handshake` at ``address`` per ``config``.

    The 5-baud init is timing-flaky — on macOS roughly half of first attempts
    come back garbled (see the hardware notes in ``CLAUDE.md``) — and the
    handshake deliberately *rejects* a garbled init rather than proceeding on a
    half-open link. Retrying with a settle gap is therefore part of the init,
    not a client-specific nicety: both clients wrap the handshake in this exact
    loop and differ only in which address they init at.

    Raises :class:`ProtocolError` if every attempt fails, naming the last error.
    """
    cfg = config or SlowInitConfig()
    logger = as_logger(log, verbose=True)
    if not transport.supports_slow_init:
        raise ProtocolError("transport does not support 5-baud slow init")
    last: Optional[Exception] = None
    for attempt in range(max(1, cfg.init_retries)):
        if attempt > 0:
            logger.debug(f"slow-init retry {attempt} (settle {cfg.retry_wait}s)")
            time.sleep(cfg.retry_wait)
        try:
            return slow_init_handshake(transport, address, cfg, log=logger)
        except (ProtocolError, TransportError) as exc:
            last = exc
            logger.debug(f"slow-init attempt {attempt + 1} failed: {exc}")
    raise ProtocolError(f"5-baud init failed: {last}")


@dataclass
class Kwp2000Config:
    # Triumph K-line addressing per community reverse engineering: engine ECU
    # 0xD5, tester 0xF5 (request headers 81/82 D5 F5).
    ecu_address: int = 0xD5
    tester_address: int = 0xF5
    addr_mode: int = ADDR_PHYSICAL
    baudrate: int = 10400
    # "fast" = fast-init pulse + StartCommunication (0x81);
    # "slow" = ISO 14230 5-baud init at ecu_address (the Keihin K-line
    # fallback) — the handshake itself yields the key bytes.
    init_mode: str = "fast"
    # StartDiagnosticSession sub-function (10 02 on the K-line);
    # set to None to skip that step.
    diagnostic_session: Optional[int] = 0x02
    # DTC read service: 0x03 = OBD Mode 03 over KWP framing (the K-line
    # default for Triumph) or 0x18 = ReadDTCByStatus (ABS/older-ECU variant).
    read_dtc_service: int = SID_OBD_MODE_STORED_DTC
    read_dtc_status_mask: int = 0x00     # 0x00 = report DTCs regardless of status
    read_dtc_group: int = 0xFF00         # 0xFF00 = all groups
    # DTC family letter for the 0x18 path. Keihin ECUs answer ReadDTCByStatus
    # with raw fault numbers that are *not* SAE-J2012 bit-encoded; the
    # community convention labels them by which ECU answered ("K" engine; ABS
    # modules would be "C"/"L", but those are CAN-only on Triumphs — out of a
    # KKL cable's reach). Ignored on the 0x03 path, whose responses are
    # J2012-encoded.
    dtc_family: str = "K"
    clear_dtc_group: int = 0xFF00
    # AccessTimingParameter (0x83 sub 0x03 "set values") P-timing bytes sent
    # after the session starts (83 03 1E 02 0A 14 00 =
    # 30/2/10/20/0). None skips the step.
    timing_params: Optional[bytes] = bytes((0x1E, 0x02, 0x0A, 0x14, 0x00))
    p2_timeout: float = 1.0              # normal max time to a response
    pending_timeout: float = 5.0        # extended wait after a 0x78 (busy)
    max_pending: int = 20
    init_low_ms: int = 25
    init_high_ms: int = 25
    # 5-baud handshake timing + retry policy (init_mode="slow"); the same
    # section Iso9141Config carries, because it is the same handshake.
    slow_init: SlowInitConfig = field(default_factory=SlowInitConfig)
    # ReadEcuIdentification record-local-identifiers (model/ECU-specific; set
    # any to None to skip). Defaults are from the community Triumph identifier
    # list (0xA0, 0xAE, 0x8C); which record carries which field is unconfirmed on
    # hardware (roadmap F4) — the standard KWP assignments (0x90 VIN / 0x91
    # hardware / 0x94 software) remain available as overrides.
    id_vin_rli: Optional[int] = 0xA0
    id_hardware_rli: Optional[int] = 0xAE
    id_software_rli: Optional[int] = 0x8C


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


@runtime_checkable
class EcuClient(Protocol):
    """The surface every protocol client exposes to :mod:`trecu.service`.

    :class:`Iso9141Client` and :class:`Kwp2000Client` are duck-typed peers, not
    a class hierarchy: they share this contract and nothing else (they speak
    entirely different wire protocols). Stating it here — the home of the shared
    vocabulary — turns "add a third client" from a prose checklist into a
    checkable one: ``isinstance(client, EcuClient)`` verifies every member is
    present, and a type checker verifies the signatures. Nothing inherits from
    it; conformance stays structural.

    Two of the members steer decoding that happens in the *service*, not the
    client, which is why they're part of the contract:

    * ``live_source`` — which table decodes this client's live data
      (``"obd_mode01"`` -> ``obd_sensors.json``; ``"kwp_local"`` ->
      ``keihin_sensors.json``, split from one packed frame).
    * ``dtc_family`` — ``None`` for the structural SAE J2012 decode, or a family
      letter (``"K"``) to prefix to raw, non-J2012 Keihin fault numbers.

    Both are declared read-only so either a plain class attribute (as in
    ``Iso9141Client``) or a computed ``@property`` (``Kwp2000Client.dtc_family``,
    which depends on the configured DTC service) satisfies them.

    ``stop_diagnostic_session`` is deliberately *not* here: it is a KWP-only
    service, and :meth:`~trecu.service.DiagnosticService.close` probes for it.
    """

    @property
    def live_source(self) -> str: ...

    @property
    def dtc_family(self) -> Optional[str]: ...

    def connect(self) -> ConnectionInfo: ...

    def read_dtcs(self) -> List[Tuple[int, int, int]]: ...

    def read_identification(self) -> EcuInfo: ...

    def read_live(self, pids: Iterable[int]) -> Dict[int, bytes]: ...

    def clear_dtcs(self) -> None: ...

    def keepalive(self) -> None: ...

    def stop_communication(self) -> None: ...


class Kwp2000Client:
    """Stateless-ish request/response client over a :class:`Transport`."""

    def __init__(
        self,
        transport: Transport,
        config: Optional[Kwp2000Config] = None,
        logger: Optional[LoggerLike] = None,
    ):
        self.transport = transport
        self.config = config or Kwp2000Config()
        # Supplying a callback directly has historically requested protocol
        # traces. The service passes a configured Logger to apply CLI verbosity.
        self._log = as_logger(logger, verbose=True)

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
                self._log.debug(f"<- {self._hex(frame.raw)}")
                return frame

    def _discard_echo(self, frame: bytes) -> None:
        if not self.transport.echoes:
            return
        echo = self.transport.read(len(frame), self.config.p2_timeout)
        if echo != frame:
            self._log.warning(
                f"echo mismatch (sent {self._hex(frame)}, "
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
        request_sid = payload[0]
        service = _SID_NAMES.get(request_sid, "unknown service")
        params = self._hex(payload[1:]) or "none"
        self._log.debug(
            f"KWP request: {service} (0x{request_sid:02X}), params={params}, "
            f"timeout={timeout:g}s"
        )
        frame = build_frame(payload, cfg.ecu_address, cfg.tester_address, cfg.addr_mode)
        self.transport.reset_input()
        self._log.debug(f"-> {self._hex(frame)}")
        try:
            self.transport.write(frame)
            self._discard_echo(frame)
        except TransportError as exc:
            raise ProtocolError(str(exc)) from exc

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
                    self._log.debug(f".. ECU busy (responsePending {pending})")
                    timeout = cfg.pending_timeout
                    continue
                self._log.debug(
                    f"KWP negative response: {service} (0x{request_sid:02X}), "
                    f"NRC=0x{nrc:02X} ({_NRC_NAMES.get(nrc, 'unknown')})"
                )
                raise NegativeResponse(resp[1] if len(resp) >= 2 else request_sid, nrc)
            if resp[0] != request_sid + POSITIVE_RESPONSE_OFFSET:
                raise ProtocolError(
                    f"unexpected response service 0x{resp[0]:02X} "
                    f"for request 0x{request_sid:02X}"
                )
            self._log.debug(
                f"KWP response: {service} acknowledged, {len(resp) - 1} data byte(s)"
            )
            return resp

    # -- high level services -------------------------------------------------
    def start_communication(self) -> bytes:
        """Fast-init the K-line and start a KWP2000 session; return key bytes."""
        self._log.debug("fast-init ...")
        try:
            self.transport.fast_init(self.config.init_low_ms, self.config.init_high_ms)
        except TransportError as exc:
            raise ProtocolError(f"fast-init failed: {exc}") from exc
        resp = self.request(bytes((SID_START_COMMUNICATION,)))
        key_bytes = resp[1:]
        self._log.debug(f"connected, key bytes: {self._hex(key_bytes)}")
        return key_bytes

    def start_diagnostic_session(self, session: Optional[int] = None) -> bytes:
        session = self.config.diagnostic_session if session is None else session
        if session is None:
            return b""
        return self.request(bytes((SID_START_DIAGNOSTIC_SESSION, session)))

    def stop_diagnostic_session(self) -> None:
        """Return the ECU to its default session before ending communication.

        ISO 14230-3 calls for this service when StartDiagnosticSession was
        previously accepted. Some manufacturer-specific ECUs omit it, so
        shutdown remains best-effort and continues with StopCommunication when
        it is rejected or times out.
        """
        try:
            self.request(bytes((SID_STOP_DIAGNOSTIC_SESSION,)))
        except ProtocolError as exc:
            self._log.warning(f"stop-diagnostic-session ignored: {exc}")

    def stop_communication(self) -> None:
        try:
            self.request(bytes((SID_STOP_COMMUNICATION,)))
        except ProtocolError as exc:
            self._log.warning(f"stop-communication ignored: {exc}")

    def tester_present(self, response_required: bool = False) -> None:
        sub = 0x01 if response_required else 0x02  # 0x02 = no positive response
        frame = build_frame(
            bytes((SID_TESTER_PRESENT, sub)),
            self.config.ecu_address,
            self.config.tester_address,
            self.config.addr_mode,
        )
        self.transport.reset_input()
        self._log.debug(
            "KWP keepalive: TesterPresent "
            + ("with response" if response_required else "response suppressed")
        )
        self._log.debug(f"-> {self._hex(frame)}")
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

    def _slow_connect(self) -> bytes:
        """5-baud init at the ECU address (the Keihin K-line fallback).

        Runs the shared :func:`slow_init_with_retries` — the flaky-5-baud
        lesson from the real bike applies to any slow init, not just the
        Sagem's. The handshake replaces StartCommunication: its key bytes are
        the session's key bytes.
        """
        key = slow_init_with_retries(
            self.transport,
            self.config.ecu_address,
            self.config.slow_init,
            log=self._log,
        )
        self._log.debug(f"connected (5-baud), key bytes: {self._hex(key)}")
        return key

    def _access_timing_parameter(self) -> None:
        """Tune the KWP P-timing windows (AccessTimingParameter, best-effort).

        The tester sends ``83 03 1E 02 0A 14 00`` ("set values") right after the
        session starts. Not every ECU implements it, so a refusal or timeout is
        logged, never fatal; ``timing_params=None`` skips the step entirely.
        """
        params = self.config.timing_params
        if params is None:
            return
        try:
            self.request(
                bytes((SID_ACCESS_TIMING_PARAMETER, 0x03)) + bytes(params)
            )
        except ProtocolError as exc:
            self._log.warning(f"timing parameters not accepted: {exc}")

    def connect(self) -> ConnectionInfo:
        """Full connect sequence, per ``config.init_mode``.

        ``"fast"``: fast-init pulse + StartCommunication (0x81).
        ``"slow"``: ISO 14230 5-baud init at the ECU address; the handshake
        yields the key bytes and no StartCommunication is sent.
        Both then attempt StartDiagnosticSession (``10 02``) and
        AccessTimingParameter, each best-effort.
        """
        if self.config.init_mode == "slow":
            key_bytes = self._slow_connect()
        else:
            key_bytes = self.start_communication()
        started = False
        if self.config.diagnostic_session is not None:
            try:
                self.start_diagnostic_session()
                started = True
                self._log.debug(
                    f"KWP diagnostic session 0x{self.config.diagnostic_session:02X} started"
                )
            except NegativeResponse as exc:
                # Not fatal — some ECUs read DTCs in the default session.
                self._log.warning(f"diagnostic session not started: {exc}")
        self._access_timing_parameter()
        return ConnectionInfo(key_bytes=key_bytes, session_started=started)

    @property
    def dtc_family(self) -> Optional[str]:
        """Family letter for DTC decoding, or ``None`` for structural J2012.

        Mode 03 over KWP framing carries J2012-encoded bytes (decode
        structurally); ``0x18`` ReadDTCByStatus carries raw Keihin fault
        numbers, labelled with ``config.dtc_family``.
        """
        if self.config.read_dtc_service == SID_OBD_MODE_STORED_DTC:
            return None
        return self.config.dtc_family

    def read_dtcs(self) -> List[Tuple[int, int, int]]:
        """Read stored DTCs; return a list of ``(high, low, status)`` triples.

        Two services, selected by ``config.read_dtc_service``:

        * ``0x03`` (default) — OBD Mode 03 carried over KWP framing, the
          K-line default for Triumph. The ``43 <hi lo>...`` response has no
          per-DTC status byte, so triples carry the synthetic
          :data:`STATUS_CONFIRMED` (like the iso9141 path).
        * ``0x18`` — ReadDTCByStatus (ABS/older-ECU variant); triples carry the
          ECU's real statusOfDTC byte.
        """
        cfg = self.config
        if cfg.read_dtc_service == SID_OBD_MODE_STORED_DTC:
            resp = self.request(bytes((SID_OBD_MODE_STORED_DTC,)))
            triples = parse_obd_dtc_pairs(resp[1:], STATUS_CONFIRMED)
            self._log.debug(f"KWP ECU reported {len(triples)} stored DTC(s)")
            return triples
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
            self._log.warning(
                f"ECU reported {count} DTCs, parsed {len(triples)} triples"
            )
        self._log.debug(f"KWP ECU reported {len(triples)} DTC record(s)")
        return triples

    # Live data on this path is the packed Keihin frame: the service requests
    # the kwp_local table's LID (0x80) and splits the response by channel.
    live_source = "kwp_local"

    def read_live(self, pids: Iterable[int]) -> Dict[int, bytes]:
        """Poll live data via ReadDataByLocalIdentifier (SID 0x21).

        Duck-typed peer of :meth:`Iso9141Client.read_live`: each requested id
        is read as one RDBLI record and its data bytes returned as-is. On a
        Triumph Keihin the record that matters is LID ``0x80`` — the Keihin
        MODE_READ_SENSORS RLI — whose response packs *all* live channels into one
        frame; the service requests exactly that id (from the ``kwp_local``
        table, see ``live_source``) and splits the frame by channel. A record
        the ECU rejects (negative response) is omitted.
        """
        out: Dict[int, bytes] = {}
        for pid in pids:
            self._log.debug(f"KWP live-data read: local identifier 0x{pid:02X}")
            try:
                resp = self.request(bytes((SID_READ_DATA_BY_LOCAL_ID, pid)))
            except ProtocolError as exc:
                self._log.debug(
                    f"KWP live-data identifier 0x{pid:02X} unavailable: {exc}"
                )
                continue
            # response: 61 <lid> <data...>
            if len(resp) >= 3 and resp[1] == pid:
                out[pid] = bytes(resp[2:])
                self._log.debug(
                    f"KWP live-data identifier 0x{pid:02X}: "
                    f"{len(resp) - 2} byte(s)"
                )
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
            self._log.warning(
                f"ReadEcuIdentification 0x{rli:02X} skipped: {exc}"
            )
            return ""
        # Response: 5A <rli> <data...>
        if len(resp) < 3 or resp[1] != rli:
            return ""
        data = resp[2:]
        raw[rli] = bytes(data)
        self._log.debug(
            f"KWP identification record 0x{rli:02X}: {len(data)} byte(s)"
        )
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
        self._log.debug("KWP clearing all stored DTCs")
        self.request(payload)
        self._log.debug("KWP clear-DTC request acknowledged")
