"""Shared vocabulary for the K-line diagnostic stack.

Everything here sits *below* the OBD-II service layer in ``iso9141.py``: the
error type, the connection/identity dataclasses the service and UI pass around,
the 5-baud slow init (an ISO 9141-2 physical-layer handshake, not a J1979
service), the data-link framing that turns a collected buffer into exact,
checksum-verified frames, and the small parsers that turn raw response bytes
into structured values. Keeping them apart from ``iso9141.py`` keeps that
module about OBD requests and responses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ..logging import LoggerLike, as_logger
from ..transport.base import Transport, TransportError

# Synthetic per-DTC status bytes for OBD Mode 03/07 reads, which carry no
# statusOfDTC byte of their own. Values chosen so decode_status() yields a
# meaningful label.
STATUS_CONFIRMED = 0x08  # decode_status -> "confirmed"
STATUS_PENDING = 0x04    # decode_status -> "pending"

#: Bytes ahead of a response's data field: format, target, ECU source address.
RESPONSE_HEADER_LEN = 3
#: Largest data field ISO 9141-2 carries in one frame — the service/mode byte
#: plus its data. A longer answer (a >3-DTC Mode 03, any Mode 09 string) is not
#: one long frame: it arrives as several back-to-back frames, which is why the
#: splitter below refuses to read past this and the client reassembles.
MAX_DATA_BYTES = 7


def decode_identification_ascii(data: bytes) -> str:
    """Best-effort ASCII text from an identification payload.

    Keeps only printable ASCII, dropping the NUL padding ECUs use to fill the
    last fragment of a Mode 09 string out to a whole frame.
    """
    return "".join(chr(b) for b in data if 0x20 <= b <= 0x7E).strip()


class ProtocolError(Exception):
    """Framing, timeout, or unexpected-response error."""


def parse_obd_dtc_pairs(body: bytes, status: int) -> List[Tuple[int, int, int]]:
    """Parse OBD Mode 03/07 DTC byte pairs into ``(hi, lo, status)`` triples.

    ``body`` is the response payload *after* the mode byte. All-zero pairs
    (frame padding on ISO 9141-2) are skipped; ``status`` is the synthetic
    status attached to every triple.
    """
    out: List[Tuple[int, int, int]] = []
    for i in range(0, len(body) - 1, 2):
        hi, lo = body[i], body[i + 1]
        if hi == 0 and lo == 0:
            continue
        out.append((hi, lo, status))
    return out


# -- ISO 9141-2 response framing ---------------------------------------------


@dataclass(frozen=True)
class ObdFrame:
    """One exact, checksum-valid ISO 9141-2 response frame.

    ``payload`` is the data field — the positive/negative response mode byte
    and everything after it — with the three header bytes and the trailing
    checksum already removed and verified.
    """

    source: int      # third header byte: the module that sent this frame
    payload: bytes   # mode byte + data
    raw: bytes       # the frame exactly as it appeared on the wire


def _is_frame_start(raw: bytes, at: int, fmt: int, target: int) -> bool:
    """Whether a response header plausibly begins at ``at``."""
    return (
        at + RESPONSE_HEADER_LEN <= len(raw)
        and raw[at] == fmt
        and raw[at + 1] == target
    )


def _frame_end(
    raw: bytes, start: int, fmt: int, target: int, max_data: int
) -> Optional[int]:
    """Exact end offset of the frame starting at ``start``, or ``None``.

    ISO 9141-2 carries no length byte, so a frame's end has to be *proved*: the
    only accepted end is a data length whose trailing byte equals the running
    sum of every byte before it. A length that also lands on either the end of
    the buffer or the header of another frame is an exact boundary and wins
    outright, so a concatenated multi-frame response splits where the frames
    really end rather than at the first length whose checksum happens to add up.
    A checksum-valid length that leaves unexplained bytes behind is kept only as
    a fallback (trailing line noise), and a frame whose checksum never validates
    has no end at all — the caller must reject it, never decode it.
    """
    fallback: Optional[int] = None
    for data_len in range(1, max_data + 1):
        end = start + RESPONSE_HEADER_LEN + data_len + 1
        if end > len(raw):
            break
        if (sum(raw[start : end - 1]) & 0xFF) != raw[end - 1]:
            continue
        if end == len(raw) or _is_frame_start(raw, end, fmt, target):
            return end
        if fallback is None:
            fallback = end
    return fallback


def split_response_frames(
    raw: bytes,
    *,
    fmt: int,
    target: int,
    max_data: int = MAX_DATA_BYTES,
) -> Tuple[List[ObdFrame], bytes]:
    """Split a collected buffer into whole response frames plus leftover bytes.

    Returns ``(frames, junk)``. Every frame is header-matched (``fmt`` +
    ``target``), length-bounded, and checksum-verified; ``junk`` is every byte
    that was not part of one — leading line noise, a stray echo, a truncated
    tail, or a frame whose checksum failed. Callers must treat a non-empty
    ``junk`` as bytes that were *discarded*, and an empty ``frames`` as "no
    usable response arrived", rather than decoding whatever is left over.
    """
    frames: List[ObdFrame] = []
    junk = bytearray()
    at = 0
    while at < len(raw):
        end = (
            _frame_end(raw, at, fmt, target, max_data)
            if _is_frame_start(raw, at, fmt, target)
            else None
        )
        if end is None:
            junk.append(raw[at])
            at += 1
            continue
        frames.append(
            ObdFrame(
                source=raw[at + 2],
                payload=bytes(raw[at + RESPONSE_HEADER_LEN : end - 1]),
                raw=bytes(raw[at:end]),
            )
        )
        at = end
    return frames, bytes(junk)


def reassemble_identification(
    payloads: Sequence[bytes], *, max_frames: int
) -> bytes:
    """Join the fragments of a multi-frame Mode 09 response into its data bytes.

    A Mode 09 string does not fit :data:`MAX_DATA_BYTES`, so J1979 splits it
    over numbered frames: ``49 <pid> <seq> <up to 4 data bytes>``, ``seq``
    counting from 1. This rebuilds the data in sequence order and is strict
    about what it accepts, because a half-read VIN that still decodes to
    plausible ASCII is worse than no VIN at all:

    * out-of-order fragments are fine — they are sorted by ``seq``;
    * an exact duplicate is a retransmission and is ignored, but a duplicate
      ``seq`` carrying *different* data is a conflict and rejects the read;
    * a gap in the sequence (or a sequence not starting at 1) rejects the read
      — that is the "missing fragment" case;
    * more than ``max_frames`` fragments rejects the read, bounding what one
      response can make the client buffer.

    Raises :class:`ProtocolError` on any of those; the caller reports the field
    as unavailable.
    """
    fragments: Dict[int, bytes] = {}
    for payload in payloads:
        # 49 <pid> <seq> + at least one data byte
        if len(payload) < 4:
            raise ProtocolError(
                "Mode 09 fragment too short: "
                + " ".join(f"{b:02X}" for b in payload)
            )
        seq, data = payload[2], bytes(payload[3:])
        if seq in fragments:
            if fragments[seq] != data:
                raise ProtocolError(f"conflicting Mode 09 fragment {seq}")
            continue  # exact duplicate: a retransmission, already have it
        fragments[seq] = data
    if not fragments:
        raise ProtocolError("no Mode 09 fragments")
    if len(fragments) > max_frames:
        raise ProtocolError(
            f"Mode 09 response exceeded {max_frames} fragments"
        )
    order = sorted(fragments)
    if order != list(range(1, len(order) + 1)):
        expected = set(range(1, max(order) + 1))
        missing = ", ".join(str(n) for n in sorted(expected - set(order)))
        raise ProtocolError(
            f"incomplete Mode 09 response: fragment(s) {missing or '?'} missing"
        )
    return b"".join(fragments[seq] for seq in order)


@dataclass
class SlowInitConfig:
    """Timing and retry policy for the 5-baud slow init.

    Its own section rather than five more fields on
    :class:`~trecu.protocol.iso9141.Iso9141Config`, because this is the
    *physical-layer* wake-up — a bit-banged waveform and the discipline around
    it — while that config is otherwise about J1979 request/response timing.

    The retry policy belongs here for a hardware reason: the 5-baud init is
    timing-flaky on macOS (see the notes in ``CLAUDE.md``), so retrying with a
    settle gap is part of the init itself, not a caller's nicety.
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
    """Run the ISO 9141-2 5-baud slow-init handshake at ``address``.

    Drives the transport's ``five_baud_init``, waits for the ``0x55`` sync + two
    key bytes, answers with the inverted second key byte, and **requires** the
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
    which is why the whole policy travels in :class:`SlowInitConfig` and no
    client owns it.

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
class ConnectionInfo:
    key_bytes: bytes
    session_started: bool


@dataclass
class EcuInfo:
    """ECU identity, populated best-effort from OBD Mode 09."""

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
