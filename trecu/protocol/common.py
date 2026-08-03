"""Shared vocabulary for the K-line diagnostic stack.

Everything here sits *below* the OBD-II service layer in ``iso9141.py``: the
error type, the connection/identity dataclasses the service and UI pass around,
the 5-baud slow init (an ISO 9141-2 physical-layer handshake, not a J1979
service), and the small parsers that turn raw response bytes into structured
values. Keeping them apart from ``iso9141.py`` keeps that module about OBD
requests and responses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..logging import LoggerLike, as_logger
from ..transport.base import Transport, TransportError

# Synthetic per-DTC status bytes for OBD Mode 03/07 reads, which carry no
# statusOfDTC byte of their own. Values chosen so decode_status() yields a
# meaningful label.
STATUS_CONFIRMED = 0x08  # decode_status -> "confirmed"
STATUS_PENDING = 0x04    # decode_status -> "pending"


def decode_identification_ascii(data: bytes) -> str:
    """Best-effort ASCII text from an identification payload.

    OBD Mode 09 responses wrap the text in a leading count/NODI byte and may
    zero-pad it.  Keeping only printable ASCII drops both without needing to
    know the exact framing.
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
