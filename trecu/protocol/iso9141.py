"""ISO 9141-2 (5-baud slow init) + OBD-II services over the K-line.

This is the path confirmed on a real Triumph: the ECU requires a 5-baud slow
init at address 0x33, answers with sync 0x55 + key bytes 08 08, and then speaks
standard OBD-II (SAE J1979) request/response with the ISO 9141-2 header
``68 6A F1``.  DTCs are read with Mode 03 (stored) / Mode 07 (pending) and
cleared with Mode 04.

Request : 68 6A F1 <mode> [pid] <cs>
Response: 48 6B <src> <mode+0x40> <data...> <cs>

The session opens with a capability read: Mode 01 PID 00 (and the further
bitmap pages it advertises) says which PIDs this ECU implements, and that set is
cached for the session so no live request is ever sent to a PID the ECU never
claimed.

Nothing reaches a decoder unvalidated: a response is split into whole frames
with verified checksums (``split_response_frames`` in ``common.py``), then
matched on header, ECU source address, response mode, and echoed PID. Corrupt,
misaddressed, truncated, or unrelated traffic is discarded rather than decoded,
and an answer too long for the 7-byte data field — a >3-DTC Mode 03, any Mode 09
string — is reassembled from its frames.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from ..logging import LoggerLike, as_logger
from ..transport.base import Transport, TransportError
from .common import (
    MAX_DATA_BYTES,
    PID_PAGE_SPAN,
    PID_SUPPORT_PAGES,
    STATUS_CONFIRMED,
    STATUS_PENDING,
    ConnectionInfo,
    EcuInfo,
    ObdFrame,
    ProtocolError,
    SlowInitConfig,
    decode_identification_ascii,
    parse_obd_dtc_pairs,
    parse_pid_support_bitmap,
    reassemble_identification,
    slow_init_with_retries,
    split_response_frames,
)

# OBD-II (SAE J1979) service/mode identifiers we use.
MODE_CURRENT_DATA = 0x01
MODE_STORED_DTC = 0x03
MODE_CLEAR_DTC = 0x04
MODE_PENDING_DTC = 0x07
MODE_VEHICLE_INFO = 0x09
POSITIVE_OFFSET = 0x40
NEGATIVE_RESPONSE = 0x7F

_MODE_NAMES = {
    MODE_CURRENT_DATA: "current data",
    MODE_STORED_DTC: "stored DTCs",
    MODE_CLEAR_DTC: "clear DTCs",
    MODE_PENDING_DTC: "pending DTCs",
    MODE_VEHICLE_INFO: "vehicle information",
}

# The negative-response codes J1979 actually uses; anything else is reported
# by number.
_NRC_NAMES = {
    0x11: "service not supported",
    0x12: "sub-function not supported",
    0x21: "busy, repeat request",
    0x22: "conditions not correct",
    0x31: "request out of range",
    0x78: "response pending",
}

# Mode 09 (vehicle information) PIDs.
VI_PID_VIN = 0x02
VI_PID_CALIBRATION_ID = 0x04
VI_PID_ECU_NAME = 0x0A

# Mode 01 PID 01 is the MIL + stored-DTC count: mandatory in J1979 and this
# ECU's reliable authority on whether faults exist (see CLAUDE.md). It is asked
# for regardless of the capability bitmap — gating the request that reports the
# ECU's state on the ECU's own advertisement would be circular.
PID_STATUS = 0x01


@dataclass
class Iso9141Config:
    init_address: int = 0x33               # 5-baud init address
    header: Tuple[int, int, int] = (0x68, 0x6A, 0xF1)  # OBD physical request header
    # Response framing: `48 6B <ecu>`. The first two bytes are fixed by
    # ISO 9141-2 (format byte + the tester address an ECU replies to); the third
    # is the answering module's own address and varies by bike — 0xD1 on the
    # tested Triumph, 0x10/0x11 on many cars. Left as None it is *latched* from
    # the first module that answers this session and every later frame must
    # match it, so traffic from another module is rejected instead of decoded.
    # Set it explicitly to pin one module from the first request.
    response_format: int = 0x48
    response_target: int = 0x6B
    ecu_address: Optional[int] = None
    max_data_bytes: int = MAX_DATA_BYTES   # ISO 9141-2 data-field limit
    max_frames: int = 16                   # bound on frames accepted per response
    baudrate: int = 10400
    p2_timeout: float = 0.8                # max wait for a response
    pending_timeout: float = 0.3           # shorter wait for optional Mode 07
    quiet_gap: float = 0.05                # end-of-message idle gap
    request_gap: float = 0.06              # min idle between requests (P3)
    frame_gap: float = 0.05                # extra wait for a follow-on frame (P2)
    # 5-baud handshake timing + retry policy (see SlowInitConfig): the
    # physical-layer wake-up, kept apart from these J1979 service timings.
    slow_init: SlowInitConfig = field(default_factory=SlowInitConfig)
    id_timeout: float = 0.5                # per-PID wait for Mode 09 (often unsupported)
    live_timeout: float = 0.4              # per-PID wait when polling Mode 01 live data
    dtc_retries: int = 3                   # Mode 03 answers intermittently; retry
    dtc_retry_wait: float = 0.2            # settle between Mode 03 retries


class Iso9141Client:
    """5-baud init + OBD-II client: the one protocol client TrECU speaks.

    Everything the service needs from a client lives here — connect, read DTCs,
    read identification, poll live data, clear, keepalive, stop.
    """

    def __init__(
        self,
        transport: Transport,
        config: Optional[Iso9141Config] = None,
        logger: Optional[LoggerLike] = None,
    ):
        self.transport = transport
        self.config = config or Iso9141Config()
        self._log = as_logger(logger, verbose=True)
        # The module this session talks to: the configured address, or the one
        # latched from the first ECU that answers. Reset by connect().
        self._ecu_address: Optional[int] = self.config.ecu_address
        # What this ECU advertised via Mode 01 PID 00, cached for the session's
        # lifetime and reset by connect(). None = never successfully read, so
        # capability is *unknown* (see the supported_pids property).
        self._supported_pids: Optional[FrozenSet[int]] = None
        # A keepalive noticing the bitmap has changed says so once, not on every
        # beat for the rest of the session.
        self._capability_change_logged = False

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

    def _collect_multi(self, timeout: float) -> bytes:
        """Collect a response that may span several back-to-back frames.

        The frames of a multi-frame answer are separate messages, so the ECU may
        leave a full P2 gap between them — long enough to look like the end of
        the message to :meth:`_collect`. After each batch, wait one more
        ``frame_gap`` for a follow-on frame; the first silent window ends the
        response. Only requests whose answer can legitimately span frames pay
        that extra wait.
        """
        buf = bytearray(self._collect(timeout))
        if not buf:
            return b""
        for _ in range(self.config.max_frames):
            more = self._collect(self.config.frame_gap)
            if not more:
                break
            buf.extend(more)
        return bytes(buf)

    def _consume_echo(self, frame: bytes) -> bytes:
        """Swallow the K-line's reflection of ``frame``; return any extra bytes.

        The single-wire line reflects everything transmitted, so the first bytes
        back are our own. Read exactly that many and check them: an exact match
        (optionally behind leading line noise) is the echo and is discarded,
        while anything else is *not* silently thrown away — it is returned to be
        parsed as inbound traffic, so a missing or mismatched echo surfaces as a
        framing error on real data rather than eating the ECU's reply.
        """
        echoed = self._read_exact(len(frame), self.config.p2_timeout)
        if echoed == frame:
            return b""
        at = echoed.find(frame)
        if at >= 0:
            self._log.debug(f"discarded {at} byte(s) of noise before the echo")
            return echoed[at + len(frame) :]
        self._log.warning(
            f"K-line echo mismatch: sent {self._hex(frame)}, "
            f"read back {self._hex(echoed) or 'nothing'}"
        )
        return echoed

    # -- init / connect ------------------------------------------------------
    def connect(self) -> ConnectionInfo:
        """5-baud init at ``config.init_address``; the key bytes open the session.

        The handshake and the retry-with-settle loop around it live in
        :func:`slow_init_with_retries` (``common.py``), including the validation
        that rejects a garbled init rather than proceeding on a half-open link.

        Immediately after the handshake the ECU's Mode 01 capability bitmap is
        read (:meth:`discover_supported_pids`) so every later request can be
        capability-aware. That read is best-effort: an ECU that won't answer
        PID 00 still gets a working session, it just has *unknown* capability.
        """
        key = slow_init_with_retries(
            self.transport,
            self.config.init_address,
            self.config.slow_init,
            log=self._log,
        )
        # A new session may be a different module: drop any latched address and
        # the previous session's capability set so both are learned again (a
        # configured address stays pinned).
        self._ecu_address = self.config.ecu_address
        self._supported_pids = None
        self._capability_change_logged = False
        return ConnectionInfo(
            key_bytes=key,
            session_started=True,
            supported_pids=self.discover_supported_pids(),
        )

    def stop_communication(self) -> None:
        # ISO 9141 has no explicit stop; the session just times out.
        return None

    def keepalive(self) -> None:
        """Keep the ISO 9141-2 link alive between operations.

        OBD-II / ISO 9141-2 have no TesterPresent service, so poke the link with
        a cheap, read-only Mode 01 PID 00 (supported-PIDs) request — enough
        traffic to avoid the P3 idle timeout. Raises :class:`ProtocolError` if
        the ECU has gone away, so the keepalive ticker can log the loss.

        The beat reuses PID 00 but is **not** a rediscovery: the session's cached
        capability set is never rebuilt or replaced from a beat, so the poll plan
        can't quietly change underneath a running stream. A page-0 bitmap that
        disagrees with the cache is worth knowing about, so it is logged — once
        per session, since the ticker would otherwise repeat it every beat.
        """
        base = PID_SUPPORT_PAGES[0]
        resp = self.obd_request(bytes((MODE_CURRENT_DATA, base)))
        cached = self._supported_pids
        if cached is None or self._capability_change_logged:
            return  # unknown capability: a keepalive is not the place to learn it
        try:
            page = parse_pid_support_bitmap(base, resp[2:])
        except ProtocolError as exc:
            self._log.debug(f"keepalive bitmap not parsed (cache kept): {exc}")
            return
        if page != {p for p in cached if p <= base + PID_PAGE_SPAN}:
            self._capability_change_logged = True
            self._log.warning(
                "ECU capability page 00 changed mid-session "
                f"({self._pid_list(page)}); keeping the session's cached set"
            )

    # -- capability discovery ------------------------------------------------
    @property
    def supported_pids(self) -> Optional[FrozenSet[int]]:
        """PIDs this ECU advertised, or ``None`` when capability is unknown.

        ``None`` and ``frozenset()`` are different answers and must not be
        collapsed: ``None`` means no usable bitmap was ever read, so nothing may
        be ruled out; an empty set means the ECU advertised nothing.
        """
        return self._supported_pids

    def discover_supported_pids(
        self, *, refresh: bool = False
    ) -> Optional[FrozenSet[int]]:
        """Read the Mode 01 capability bitmap and cache it for the session.

        Requests PID 00 and walks on to PID 20, 40, … **only** while the page
        just read advertises the next one, so an ECU that stops at one page is
        asked exactly once. The result is cached until :meth:`connect` (or
        ``refresh=True``) — later calls cost no traffic.

        Best-effort by design: a missing or malformed bitmap leaves capability
        unknown (``None``) rather than raising, because "this ECU won't say" must
        not read as "this ECU supports nothing". A page that fails *after* a good
        one keeps what was already learned — a partial set is still capability.
        """
        if self._supported_pids is not None and not refresh:
            return self._supported_pids
        found: Set[int] = set()
        known = False
        for base in PID_SUPPORT_PAGES:
            if base != PID_SUPPORT_PAGES[0] and base not in found:
                break  # the page before this one did not advertise it
            try:
                page = self._read_support_page(base)
            except ProtocolError as exc:
                level = self._log.warning if known else self._log.debug
                level(f"supported-PID page {base:02X} unavailable: {exc}")
                break
            found |= page
            known = True
        self._supported_pids = frozenset(found) if known else None
        if known:
            self._log.debug(
                f"ECU advertises {len(found)} PID(s): {self._pid_list(found)}"
            )
        else:
            self._log.warning(
                "ECU did not report its supported PIDs; capability unknown, so "
                "live requests are not filtered"
            )
        return self._supported_pids

    def _read_support_page(self, base: int) -> FrozenSet[int]:
        """One capability page: ``41 <base> <4 bitmap bytes>`` decoded to PIDs."""
        resp = self.obd_request(bytes((MODE_CURRENT_DATA, base)))
        page = parse_pid_support_bitmap(base, resp[2:])
        self._log.debug(
            f"supported-PID page {base:02X}: bitmap {self._hex(resp[2:6])} -> "
            f"{self._pid_list(page)}"
        )
        return page

    @staticmethod
    def _pid_list(pids: Iterable[int]) -> str:
        return " ".join(f"{p:02X}" for p in sorted(pids)) or "none"

    def live_plan(self, pids: Iterable[int]) -> List[int]:
        """The requested PIDs this ECU actually advertises, in order.

        The capability filter every live request goes through: with a known
        capability set, a PID the ECU never advertised is dropped here rather
        than costing a request and its timeout on the wire. With capability
        unknown, nothing is dropped — an unfiltered request is the only honest
        move when the ECU never said what it supports.
        """
        supported = self._supported_pids
        plan: List[int] = []
        skipped: List[int] = []
        for pid in pids:
            (plan if supported is None or pid in supported else skipped).append(pid)
        if skipped:
            self._log.debug(
                f"skipping {len(skipped)} unadvertised PID(s): "
                f"{self._pid_list(skipped)}"
            )
        return plan

    # -- OBD request/response ------------------------------------------------
    def obd_request(self, data: bytes, timeout: Optional[float] = None) -> bytes:
        """Send an OBD request; return the one matching response payload.

        The payload is the response mode byte (``request mode + 0x40``), the
        request's echoed PID when it carried one, and the data — all validated
        before it is returned, so callers may index it without re-checking.
        Raises :class:`ProtocolError` when nothing valid and related arrives.
        """
        return self._exchange(data, timeout, multi=False)[0]

    def obd_request_multi(
        self, data: bytes, timeout: Optional[float] = None
    ) -> List[bytes]:
        """Like :meth:`obd_request` for an answer that may span several frames.

        Mode 03/07 (more than three DTCs) and Mode 09 (any string) exceed the
        7-byte ISO 9141-2 data field and come back as several frames. Returns
        every matching frame's payload, in arrival order.
        """
        return self._exchange(data, timeout, multi=True)

    def _exchange(
        self, data: bytes, timeout: Optional[float], *, multi: bool
    ) -> List[bytes]:
        """One request/response round trip, validated end to end."""
        cfg = self.config
        timeout = cfg.p2_timeout if timeout is None else timeout
        mode = data[0]
        detail = self._hex(data[1:]) or "none"
        self._log.debug(
            f"OBD request: Mode {mode:02X} ({_MODE_NAMES.get(mode, 'unknown')}), "
            f"data={detail}, timeout={timeout:g}s"
        )
        raw = self._transmit(data, timeout, multi=multi)
        self._log.debug(f"<- {self._hex(raw)}")
        payloads = self._match(self._frames(raw), data)
        self._log.debug(
            f"OBD response: Mode {payloads[0][0]:02X}, {len(payloads)} frame(s), "
            f"{sum(len(p) for p in payloads) - len(payloads)} data byte(s)"
        )
        return payloads

    def _transmit(self, data: bytes, timeout: float, *, multi: bool) -> bytes:
        """Send the request frame and collect the raw bytes that came back."""
        cfg = self.config
        body = bytes(cfg.header) + data
        frame = body + bytes((sum(body) & 0xFF,))
        t = self.transport
        time.sleep(cfg.request_gap)
        t.reset_input()
        self._log.debug(f"-> {self._hex(frame)}")
        try:
            t.write(frame)
            leftover = self._consume_echo(frame) if t.echoes else b""
        except TransportError as exc:
            raise ProtocolError(str(exc)) from exc
        collect = self._collect_multi if multi else self._collect
        raw = leftover + collect(timeout)
        if not raw:
            raise ProtocolError("no OBD response (timeout)")
        return raw

    def _frames(self, raw: bytes) -> List[ObdFrame]:
        """Split ``raw`` into whole, checksum-verified frames — or fail.

        Anything the splitter could not account for is discarded rather than
        decoded: a bad checksum, a truncated tail, and leading line noise all
        end up outside a frame, so corrupt bytes can never reach a decoder.
        """
        cfg = self.config
        frames, junk = split_response_frames(
            raw,
            fmt=cfg.response_format,
            target=cfg.response_target,
            max_data=cfg.max_data_bytes,
        )
        if junk:
            self._log.debug(
                f"discarded {len(junk)} unframed byte(s): {self._hex(junk)}"
            )
        if not frames:
            if bytes((cfg.response_format, cfg.response_target)) in raw:
                raise ProtocolError(
                    f"OBD response failed framing/checksum validation: {self._hex(raw)}"
                )
            raise ProtocolError(f"no OBD response frame in: {self._hex(raw)}")
        if len(frames) > cfg.max_frames:
            raise ProtocolError(
                f"OBD response exceeded {cfg.max_frames} frames "
                f"({len(frames)} received)"
            )
        return frames

    def _match(self, frames: List[ObdFrame], request: bytes) -> List[bytes]:
        """The payloads of ``frames`` that actually answer ``request``.

        Three things have to hold before a frame's data reaches a decoder: it
        must not be a rejection of this request, it must carry this request's
        positive response mode and echoed PID, and it must come from the module
        this session is talking to. A well-formed frame failing the middle test
        is unrelated traffic (another tester's answer, a late reply to an
        earlier request) and is skipped, not mistaken for this answer.
        """
        mode = request[0]
        pid = request[1] if len(request) > 1 else None
        self._raise_on_negative(frames, mode)
        related = [f for f in frames if self._answers(f.payload, mode, pid)]
        skipped = len(frames) - len(related)
        if skipped:
            self._log.debug(f"ignored {skipped} unrelated OBD frame(s)")
        if not related:
            raise ProtocolError(
                f"no Mode {mode:02X} response in: "
                + "; ".join(self._hex(f.raw) for f in frames)
            )
        expected = self._ecu_address
        if expected is None:
            expected = related[0].source
            self._ecu_address = expected
            self._log.debug(f"ECU source address for this session: {expected:02X}")
        mine = [f for f in related if f.source == expected]
        if not mine:
            raise ProtocolError(
                f"Mode {mode:02X} response came from module "
                f"{related[0].source:02X}, expected {expected:02X}"
            )
        if len(mine) != len(related):
            self._log.debug(
                f"ignored {len(related) - len(mine)} frame(s) from another module"
            )
        return [f.payload for f in mine]

    @staticmethod
    def _answers(payload: bytes, mode: int, pid: Optional[int]) -> bool:
        """Whether ``payload`` is the positive response to this mode + PID."""
        if not payload or payload[0] != mode + POSITIVE_OFFSET:
            return False
        if pid is None:
            return True
        return len(payload) >= 2 and payload[1] == pid

    def _raise_on_negative(self, frames: List[ObdFrame], mode: int) -> None:
        """Turn a negative response to *this* request into a clear error.

        ``7F <mode> <nrc>`` is the ECU refusing the request — an answer, not
        silence, so it is worth reporting as such instead of letting the request
        fall through to a timeout-shaped error.
        """
        for frame in frames:
            p = frame.payload
            if len(p) >= 3 and p[0] == NEGATIVE_RESPONSE and p[1] == mode:
                name = _NRC_NAMES.get(p[2], "unknown")
                raise ProtocolError(
                    f"ECU rejected Mode {mode:02X}: {name} (NRC {p[2]:02X})"
                )

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
    def _read_status(self) -> Tuple[bool, int]:
        """Mode 01 PID 01 -> (MIL on?, stored DTC count); raises if unanswered.

        On the real Sagem ECU, Mode 01 PID 01 answers reliably where Mode 03
        does not, so it is the authority for whether faults exist. A missing or
        malformed reply therefore means the *session is dead*, not that there
        are zero faults — so raise rather than silently return ``(False, 0)``.
        """
        resp = self.obd_request(bytes((MODE_CURRENT_DATA, PID_STATUS)))
        if len(resp) < 3:  # mode + PID present already; A is the byte we need
            raise ProtocolError(f"short Mode 01 PID 01 response: {self._hex(resp)}")
        a = resp[2]
        return (bool(a & 0x80), a & 0x7F)

    def read_live(self, pids: Iterable[int]) -> Dict[int, bytes]:
        """Poll OBD Mode 01 PIDs; return ``{pid: data_bytes}`` for those answered.

        Capability-aware: the request list is the caller's PIDs filtered through
        :meth:`live_plan`, so a PID this ECU never advertised costs no request
        and no timeout. One Mode 01 request per PID after that — widely supported
        and how other live-data tools poll too.

        An advertised PID the ECU doesn't answer, rejects, or answers with a
        corrupt frame is simply omitted, so a partial dict is normal; the caller
        decodes whatever came back via the PID table. A PID that *is* answered
        with all-zero or all-``FF`` data is a real answer and is kept — the
        absence of a key means "no response", never "the response looked empty".
        """
        out: Dict[int, bytes] = {}
        for pid in self.live_plan(pids):
            try:
                resp = self.obd_request(
                    bytes((MODE_CURRENT_DATA, pid)), timeout=self.config.live_timeout
                )
            except ProtocolError as exc:
                self._log.debug(f"live PID {pid:02X} unanswered: {exc}")
                continue
            # payload: 41 <pid> <data...>, mode and PID already validated
            if len(resp) >= 3:
                out[pid] = bytes(resp[2:])
        return out

    def read_identification(self) -> EcuInfo:
        """Read ECU identity via OBD Mode 09 (VIN / Calibration ID / ECU name).

        Best-effort: many motorcycle ECUs don't implement Mode 09, so each PID
        is queried with a short timeout and a missing reply yields an empty
        field rather than an error. A field is either **complete or empty** —
        an answer whose fragments don't reassemble cleanly is reported as
        unavailable rather than as the plausible ASCII half of a VIN.
        """
        raw: Dict[int, bytes] = {}
        vin = self._read_vehicle_info(VI_PID_VIN, raw)
        calibration = self._read_vehicle_info(VI_PID_CALIBRATION_ID, raw)
        ecu_name = self._read_vehicle_info(VI_PID_ECU_NAME, raw)
        return EcuInfo(
            vin=vin, calibration_id=calibration, ecu_name=ecu_name, raw=raw
        )

    def _read_vehicle_info(self, pid: int, raw: Dict[int, bytes]) -> str:
        """One Mode 09 field, reassembled from its numbered response frames."""
        try:
            payloads = self.obd_request_multi(
                bytes((MODE_VEHICLE_INFO, pid)), timeout=self.config.id_timeout
            )
        except ProtocolError as exc:
            self._log.debug(f"Mode 09 PID {pid:02X} unavailable: {exc}")
            return ""
        try:
            # Payloads are 49 <pid> <seq> <data...>; mode and PID are already
            # validated, so reassembly only has to police the sequence.
            data = reassemble_identification(
                payloads, max_frames=self.config.max_frames
            )
        except ProtocolError as exc:
            self._log.warning(f"Mode 09 PID {pid:02X} discarded: {exc}")
            return ""
        raw[pid] = data
        return decode_identification_ascii(data)

    def _request_dtcs(
        self, mode: int, status: int, timeout: Optional[float] = None
    ) -> List[Tuple[int, int, int]]:
        """One DTC request: triples on a positive response (possibly empty).

        A frame holds at most three DTC pairs, so a longer list arrives as
        several frames; their pairs are concatenated in order and de-duplicated
        (a retransmitted frame must not inflate the count the caller reconciles
        against). Raises :class:`ProtocolError` on no/invalid response, so the
        caller can tell "the ECU said zero codes" apart from "the ECU never
        answered".
        """
        payloads = self.obd_request_multi(bytes((mode,)), timeout=timeout)
        out: List[Tuple[int, int, int]] = []
        seen = set()
        for payload in payloads:
            for hi, lo, sts in parse_obd_dtc_pairs(payload[1:], status):
                if (hi, lo) in seen:
                    continue
                seen.add((hi, lo))
                out.append((hi, lo, sts))
        return out

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
                self._log.debug(
                    f"Mode 03 attempt {attempt + 1}/{attempts} failed: {exc}"
                )
                result = []
            if len(result) >= expected:  # expected==0 accepts an empty result
                return result
        if not result and expected > 0:
            raise ProtocolError(
                f"status reports {expected} stored DTC(s) but Mode 03 returned "
                f"none after {attempts} attempts"
            )
        if len(result) < expected:
            self._log.warning(
                f"status reports {expected} stored DTC(s), read {len(result)}"
            )
        return result

    def read_dtcs(self) -> List[Tuple[int, int, int]]:
        """Read stored (Mode 03) + pending (Mode 07) DTCs as (hi, lo, status).

        Mode 01 PID 01 (MIL + count) is this ECU's reliable authority, so it is
        read first: its count drives the Mode 03 retry/reconcile, and a missing
        PID 01 reply surfaces as a hard error (dead session) instead of a false
        "no codes". Pending (Mode 07) is best-effort — unsupported on some ECUs.
        """
        mil, count = self._read_status()
        self._log.debug(
            f"OBD ECU status: MIL={'on' if mil else 'off'}, "
            f"{count} stored DTC(s) expected"
        )
        stored = self._read_stored(count)
        try:
            pending = self._request_dtcs(
                MODE_PENDING_DTC, STATUS_PENDING, timeout=self.config.pending_timeout
            )
        except ProtocolError as exc:
            self._log.debug(f"OBD pending-DTC read unavailable: {exc}")
            pending = []
        seen = {(h, l) for h, l, _ in stored}
        result = stored + [
            (h, l, s) for (h, l, s) in pending if (h, l) not in seen
        ]
        self._log.debug(
            f"OBD DTC read complete: {len(stored)} stored, "
            f"{len(result) - len(stored)} additional pending"
        )
        return result

    def clear_dtcs(self) -> None:
        """Clear stored DTCs and turn off the MIL (Mode 04)."""
        self._log.debug("OBD clearing all stored DTCs")
        try:
            self.obd_request(bytes((MODE_CLEAR_DTC,)))
        except ProtocolError as exc:
            raise ProtocolError(f"clear (Mode 04) not acknowledged: {exc}") from exc
        self._log.debug("OBD clear-DTC request acknowledged")
