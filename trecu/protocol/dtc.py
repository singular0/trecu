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


def decode_dtc_bytes(high: int, low: int, family: str | None = None) -> str:
    """Convert a two-byte DTC into its letter + four hex digits form.

    ``family=None`` (the SAE J2012 structural decode): the top two bits of the
    high byte select ``P/C/B/U``, e.g. ``0x01 0x07`` -> ``"P0107"``. This is
    correct for OBD Mode 03/07 responses (and Mode 03 carried over KWP).

    ``family="K"`` (or another letter): the bytes are a *raw* fault number that
    is not J2012 bit-encoded — Keihin ECUs answering KWP ``0x18``
    ReadDTCByStatus return these — so the given letter is prepended to the four
    raw hex digits, e.g. ``0x15 0x35`` -> ``"K1535"`` (the community labelling
    convention: the family comes from which ECU/service answered, not from
    the bytes).
    """
    if family is not None:
        return f"{family}{high:02X}{low:02X}"
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
    raw: bytes = b""                # the raw DTC + status bytes as received

    @property
    def status_flags(self) -> list[str]:
        return decode_status(self.status)

    @property
    def is_known(self) -> bool:
        return not self.description.startswith("Unknown code")

    def as_row(self) -> tuple[str, str, str]:
        """Columns for tabular display: code, status, description."""
        flags = ", ".join(self.status_flags) or f"0x{self.status:02X}"
        return (self.code, flags, self.description)


class DtcDatabase:
    """Lookup table mapping DTC codes to descriptions.

    Backed by ``trecu/data/triumph_dtc.json`` — a flat ``{code: description}``
    map imported from the official-service-manual wording in a community-sourced
    extract (557 codes across the ``P``/``K``/``C``/``U``/``L`` families).
    Descriptions may vary by model/year — the file is meant to be extended.
    """

    def __init__(self, entries: dict[str, str] | None = None):
        self._entries: dict[str, str] = entries or {}

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

    def describe(self, code: str) -> str:
        """Return the description for ``code`` (a fallback string if absent)."""
        entry = self._entries.get(code.upper())
        if entry is None:
            return "Unknown code — consult the model service manual"
        return entry

    def make_dtc(
        self, high: int, low: int, status: int, raw: bytes = b"", family: str | None = None
    ) -> Dtc:
        code = decode_dtc_bytes(high, low, family)
        return Dtc(code=code, status=status, description=self.describe(code), raw=raw)

    def decode_all(
        self, triples: Iterable[tuple[int, int, int]], family: str | None = None
    ) -> list[Dtc]:
        """Decode ``(high, low, status)`` DTC triples.

        ``family`` selects the labelling scheme (see :func:`decode_dtc_bytes`):
        ``None`` for the structural SAE J2012 decode, a letter (e.g. ``"K"``)
        when the source service returns raw non-J2012 fault numbers.
        """
        return [self.make_dtc(h, l, s, bytes((h, l, s)), family) for (h, l, s) in triples]
