"""Decode raw DTC bytes into SAE J2012 codes and human-readable descriptions."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Iterable

# First two bits of the high byte select the code's letter (SAE J2012).
_DTC_LETTERS = ("P", "C", "B", "U")

# ISO 14229-1 statusOfDTC bit meanings. KWP2000 ECUs vary, but this mask is the
# most widely documented reference; treat it as a hint, not gospel, for a given
# Triumph ECU. Bit -> short label.
_STATUS_BITS = {
    0x01: "testFailed",
    0x02: "testFailedThisCycle",
    0x04: "pending",
    0x08: "confirmed",
    0x10: "testNotCompletedSinceClear",
    0x20: "testFailedSinceClear",
    0x40: "testNotCompletedThisCycle",
    0x80: "warningLampRequested",
}


def decode_dtc_bytes(high: int, low: int) -> str:
    """Convert a two-byte DTC into its ``P/C/B/U`` + four hex digits form.

    e.g. ``0x01 0x07`` -> ``"P0107"``.
    """
    letter = _DTC_LETTERS[(high >> 6) & 0x03]
    d1 = (high >> 4) & 0x03
    d2 = high & 0x0F
    d3 = (low >> 4) & 0x0F
    d4 = low & 0x0F
    return f"{letter}{d1}{d2:X}{d3:X}{d4:X}"


def decode_status(status: int) -> list[str]:
    """Return the set labels for a statusOfDTC byte (best-effort, ISO 14229)."""
    return [label for bit, label in _STATUS_BITS.items() if status & bit]


@dataclass(frozen=True)
class Dtc:
    """A single decoded diagnostic trouble code."""

    code: str                       # e.g. "P0107"
    status: int                     # raw statusOfDTC byte
    description: str = "Unknown code — consult the model service manual"
    subsystem: str = ""
    raw: bytes = b""                # the raw DTC + status bytes as received

    @property
    def status_flags(self) -> list[str]:
        return decode_status(self.status)

    @property
    def is_known(self) -> bool:
        return not self.description.startswith("Unknown code")

    def as_row(self) -> tuple[str, str, str, str]:
        """Columns for tabular display: code, status, subsystem, description."""
        flags = ", ".join(self.status_flags) or f"0x{self.status:02X}"
        return (self.code, flags, self.subsystem, self.description)


class DtcDatabase:
    """Lookup table mapping DTC codes to descriptions.

    Backed by ``trecu/data/triumph_dtc.json``.  Generic SAE J2012 powertrain
    codes are standardized and reliable; Triumph-specific ``P1xxx`` entries are
    community-sourced and may vary by model/year — the file is meant to be
    extended.
    """

    def __init__(self, entries: dict[str, dict] | None = None):
        self._entries: dict[str, dict] = entries or {}

    @classmethod
    def load_default(cls) -> "DtcDatabase":
        with resources.files("trecu.data").joinpath("triumph_dtc.json").open(
            "r", encoding="utf-8"
        ) as fh:
            data = json.load(fh)
        return cls(data.get("codes", {}))

    @classmethod
    def load_file(cls, path: str) -> "DtcDatabase":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return cls(data.get("codes", data))

    def __len__(self) -> int:
        return len(self._entries)

    def describe(self, code: str) -> tuple[str, str]:
        """Return ``(description, subsystem)`` for ``code`` (fallbacks if absent)."""
        entry = self._entries.get(code.upper())
        if entry is None:
            return ("Unknown code — consult the model service manual", "")
        return (entry.get("desc", ""), entry.get("subsystem", ""))

    def make_dtc(self, high: int, low: int, status: int, raw: bytes = b"") -> Dtc:
        code = decode_dtc_bytes(high, low)
        desc, subsystem = self.describe(code)
        return Dtc(code=code, status=status, description=desc, subsystem=subsystem, raw=raw)

    def decode_all(self, triples: Iterable[tuple[int, int, int]]) -> list[Dtc]:
        """Decode an iterable of ``(high, low, status)`` DTC triples."""
        return [self.make_dtc(h, l, s, bytes((h, l, s))) for (h, l, s) in triples]
