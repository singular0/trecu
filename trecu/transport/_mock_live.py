"""Synthetic, *moving* live-sensor values for the mock ECU.

The mock answers live-data requests with plausible values that visibly change,
or the live view looks dead.

Values are deterministic functions of a ``tick`` counter (a smooth sine wobble
around a realistic base), so tests can assert ranges and movement without
wall-clock flakiness. Each encoder is the **inverse** of the decode formula for
that PID in ``trecu/data/obd_sensors.json`` — keep the two in sync (this
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
