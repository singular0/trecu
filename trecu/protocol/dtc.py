"""Decode raw DTC bytes into SAE J2012 codes and human-readable descriptions."""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from importlib import resources
from typing import Iterable, Optional

# First two bits of the high byte select the code's letter (SAE J2012).
_DTC_LETTERS = ("P", "C", "B", "U")

# ISO 14229-1 statusOfDTC bit meanings. ECUs vary, but this mask is the most
# widely documented reference; treat it as a hint, not gospel, for a given
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
    """Convert a two-byte DTC into its letter + four hex digits form.

    The SAE J2012 structural decode: the top two bits of the high byte select
    ``P/C/B/U``, e.g. ``0x01 0x07`` -> ``"P0107"``. This is how OBD Mode 03/07
    responses encode a code, and it is the only scheme TrECU reads.
    """
    letter = _DTC_LETTERS[(high >> 6) & 0x03]
    d1 = (high >> 4) & 0x03
    d2 = high & 0x0F
    d3 = (low >> 4) & 0x0F
    d4 = low & 0x0F
    return f"{letter}{d1}{d2:X}{d3:X}{d4:X}"


def encode_dtc_code(code: str) -> tuple[int, int]:
    """Inverse of :func:`decode_dtc_bytes`.

    ``"P1108"`` -> ``(0x11, 0x08)``. Only SAE J2012 ``P/C/B/U`` codes whose first
    digit is ``0-3`` round-trip; anything else (a first digit above 3, non-hex
    digits, wrong length) has no byte pair that decodes back to it, and raises
    :class:`ValueError`.
    """
    if len(code) != 5:
        raise ValueError(f"not a 4-digit DTC: {code!r}")
    letter = code[0].upper()
    if letter not in _DTC_LETTERS:
        raise ValueError(f"not a structural DTC letter (P/C/B/U): {code!r}")
    idx = _DTC_LETTERS.index(letter)
    try:
        d1, d2, d3, d4 = (int(c, 16) for c in code[1:])
    except ValueError:
        raise ValueError(f"non-hex DTC digits: {code!r}") from None
    if d1 > 3:
        raise ValueError(f"first digit must be 0-3 to encode: {code!r}")
    return (idx << 6) | (d1 << 4) | d2, (d3 << 4) | d4


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
    extract (415 codes across the ``P``/``C``/``U`` families — every code an
    OBD Mode 03/07 response can structurally decode to).
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

    def __len__(self) -> int:
        return len(self._entries)

    def describe(self, code: str) -> str:
        """Return the description for ``code`` (a fallback string if absent)."""
        entry = self._entries.get(code.upper())
        if entry is None:
            return "Unknown code — consult the model service manual"
        return entry

    def make_dtc(self, high: int, low: int, status: int, raw: bytes = b"") -> Dtc:
        code = decode_dtc_bytes(high, low)
        return Dtc(code=code, status=status, description=self.describe(code), raw=raw)

    def _structural_codes_by_family(self) -> dict[str, list[str]]:
        """DB codes that round-trip through the structural decode, grouped by letter.

        Only these can seed a mock ECU: the bike answers with raw bytes, so a
        seed code is usable only if :func:`encode_dtc_code` /
        :func:`decode_dtc_bytes` round-trips it back to a code the DB describes.
        """
        by_family: dict[str, list[str]] = {}
        for code in self._entries:
            try:
                high, low = encode_dtc_code(code)
            except ValueError:
                continue
            if decode_dtc_bytes(high, low) != code:
                continue
            by_family.setdefault(code[0].upper(), []).append(code)
        return by_family

    def random_dtcs(
        self,
        *,
        rng: Optional[random.Random] = None,
        min_count: int = 2,
        max_count: int = 6,
    ) -> list[tuple[int, int]]:
        """Pick a random, type-varied set of DTC byte pairs from the database.

        Lets ``--mock`` show a plausible spread of stored faults instead of the
        single canned code: a random count in ``[min_count, max_count]`` of
        distinct codes, each chosen by first picking a family uniformly so the
        letters (P/C/B/U) actually vary rather than being swamped by the huge P
        range. Every pair decodes back — via the structural
        :func:`decode_dtc_bytes` — to a code the DB describes, so the mock's
        faults read like real ones. Returns ``[]`` if the DB has no structural
        codes to draw from.
        """
        rng = rng or random
        by_family = self._structural_codes_by_family()
        total = sum(len(codes) for codes in by_family.values())
        if total == 0:
            return []
        families = list(by_family)
        hi_count = min(max_count, total)
        count = rng.randint(min(min_count, hi_count), hi_count)
        chosen: list[str] = []
        seen: set[str] = set()
        while len(chosen) < count:
            code = rng.choice(by_family[rng.choice(families)])
            if code not in seen:
                seen.add(code)
                chosen.append(code)
        return [encode_dtc_code(code) for code in chosen]

    def decode_all(self, triples: Iterable[tuple[int, int, int]]) -> list[Dtc]:
        """Decode ``(high, low, status)`` DTC triples via :func:`decode_dtc_bytes`."""
        return [self.make_dtc(h, l, s, bytes((h, l, s))) for (h, l, s) in triples]
