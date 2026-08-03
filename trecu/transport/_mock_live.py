"""Synthetic, *moving* live-sensor values shared by both mock ECUs.

Phase 3 needs the mocks to answer live-data requests with plausible values that
visibly change, or the live view looks dead (roadmap: "both ECUs must answer
PID/record requests with plausible, *varying* values"). Both mock ECUs encode
the same sensor set the same way — so the two protocol paths behave alike in
tests — and this module is that single source of truth.

Values are deterministic functions of a ``tick`` counter (a smooth sine wobble
around a realistic base), so tests can assert ranges and movement without
wall-clock flakiness. Each encoder is the **inverse** of the decode formula in
the matching ``trecu/data/`` table (``obd_sensors.json`` for the OBD sensors,
``keihin_sensors.json`` for the Keihin channels) — keep the two in sync (this
mirrors the DTC mock/DB sync note in ``CLAUDE.md``).
"""

from __future__ import annotations

import math
from typing import Callable, Dict, Optional, Tuple


def _u8(v: float) -> bytes:
    """Encode a single unsigned byte (clamped)."""
    return bytes((max(0, min(255, round(v))),))


def _u16(v: float) -> bytes:
    """Encode a big-endian unsigned 16-bit value (clamped)."""
    n = max(0, min(0xFFFF, round(v)))
    return bytes(((n >> 8) & 0xFF, n & 0xFF))


def _o2(v: float) -> bytes:
    """PID 14: byte A = voltage×200, byte B = fuel trim (unused here → 0xFF)."""
    return bytes((max(0, min(255, round(v * 200))), 0xFF))


# pid -> (base value, amplitude, angular frequency, encoder(value) -> data bytes).
# Distinct frequencies keep the sensors visibly out of phase with one another.
_SENSORS: Dict[int, Tuple[float, float, float, Callable[[float], bytes]]] = {
    0x05: (90.0, 4.0, 0.05, lambda t: _u8(t + 40)),        # coolant temp °C
    0x0B: (38.0, 4.0, 0.11, lambda k: _u8(k)),             # intake MAP kPa
    0x0C: (1300.0, 180.0, 0.09, lambda r: _u16(r * 4)),    # engine RPM
    0x0F: (26.0, 2.0, 0.03, lambda t: _u8(t + 40)),        # intake air temp °C
    0x11: (5.0, 3.0, 0.13, lambda p: _u8(p * 2.55)),       # throttle position %
    0x14: (0.45, 0.35, 0.70, _o2),                         # O2 sensor 1 voltage
    0x33: (101.0, 1.5, 0.02, lambda k: _u8(k)),            # barometric pressure kPa
    0x42: (13.8, 0.4, 0.06, lambda v: _u16(v * 1000)),     # battery voltage V
}


# Keihin packed live frame (kwp_local, the KWP path): the Keihin
# MODE_READ_SENSORS RLI (21 80) answers with *every* channel in one frame. Slot
# positions and encoders here are the inverse of the draft kwp_local
# layout/formulas in ``keihin_sensors.json`` (sequential 2-byte big-endian slots
# in listed order) — keep the two in sync. Unmodelled slots stay zero, which
# still decodes (to the channel's offset), like a quiescent sensor.
_KWP_FRAME_SLOTS = 53
# frame slot -> (base, amplitude, angular freq, physical value -> raw count).
_KWP_CHANNELS: Dict[int, Tuple[float, float, float, Callable[[float], float]]] = {
    0: (1300.0, 180.0, 0.09, lambda rpm: rpm),       # ch 0  RPM
    1: (5.0, 3.0, 0.13, lambda pct: pct * 10),       # ch 1  TPS (0.1 %)
    3: (90.0, 4.0, 0.05, lambda t: t + 25),          # ch 3  water temp (-25 offset)
    5: (3.0, 2.0, 0.04, lambda gear: gear),          # ch 5  gear
    15: (0.0, 0.0, 0.0, lambda mil: mil + 1),        # ch 17 MIL flag (-1 offset)
    25: (13.8, 0.4, 0.06, lambda v: (v - 8) * 10),   # ch 50 battery (+8 V, 0.1 V)
}


def kwp_live_frame(tick: int) -> bytes:
    """The packed ``21 80`` response body a mock Keihin serves at ``tick``."""
    frame = bytearray(2 * _KWP_FRAME_SLOTS)
    for slot, (base, amplitude, freq, to_raw) in _KWP_CHANNELS.items():
        raw = _u16(to_raw(base + amplitude * math.sin(tick * freq)))
        frame[2 * slot : 2 * slot + 2] = raw
    return bytes(frame)


def sensor_data(pid: int, tick: int) -> Optional[bytes]:
    """Data bytes a mock ECU should reply for ``pid`` at ``tick``.

    Returns ``None`` when the mock doesn't model the PID — the caller then
    answers as unsupported (a zeroed reply or a negative response), like a real
    ECU would for a PID it doesn't implement.
    """
    entry = _SENSORS.get(pid)
    if entry is None:
        return None
    base, amplitude, freq, encode = entry
    return encode(base + amplitude * math.sin(tick * freq))
