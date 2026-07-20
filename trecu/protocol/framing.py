"""ISO 14230-2 (KWP2000) message framing — pure, transport-independent.

Message layout::

    [ FMT ] [ TGT ] [ SRC ] [ LEN ] [ ..DATA.. ] [ CS ]
      |        |       |       |         |          |
      |        |       |       |         |          +- checksum: sum(all prior) & 0xFF
      |        |       |       |         +- service id + parameters
      |        |       |       +- extra length byte, only when FMT length bits == 0
      |        +-------+- address bytes, only when FMT address bits != 00
      +- format: bits 7..6 = addressing mode, bits 5..0 = length (0 => LEN byte)
"""

from __future__ import annotations

from dataclasses import dataclass

# Addressing mode (top two bits of the format byte).
ADDR_NONE = 0x00        # no address bytes present
ADDR_PHYSICAL = 0x80    # 0b10 — physical addressing, TGT/SRC present
ADDR_FUNCTIONAL = 0xC0  # 0b11 — functional addressing, TGT/SRC present

_ADDR_MASK = 0xC0
_LEN_MASK = 0x3F


class ChecksumError(ValueError):
    """The trailing checksum did not match the computed value."""


class IncompleteFrame(ValueError):
    """The buffer does not (yet) contain a whole frame."""


@dataclass(frozen=True)
class ParsedFrame:
    payload: bytes          # service id + parameters
    target: int | None
    source: int | None
    addr_mode: int
    raw: bytes              # exact bytes consumed, including checksum

    @property
    def service(self) -> int | None:
        return self.payload[0] if self.payload else None


def checksum(data: bytes) -> int:
    """KWP2000 checksum: 8-bit sum of every preceding byte."""
    return sum(data) & 0xFF


def build_frame(
    payload: bytes,
    target: int,
    source: int,
    addr_mode: int = ADDR_PHYSICAL,
) -> bytes:
    """Build a complete KWP2000 frame around ``payload`` (service id + params)."""
    n = len(payload)
    if not 1 <= n <= 255:
        raise ValueError(f"payload length must be 1..255, got {n}")
    if addr_mode not in (ADDR_NONE, ADDR_PHYSICAL, ADDR_FUNCTIONAL):
        raise ValueError(f"invalid addressing mode 0x{addr_mode:02X}")

    header = bytearray()
    if n <= _LEN_MASK:
        header.append(addr_mode | n)
        if addr_mode != ADDR_NONE:
            header += bytes((target, source))
    else:
        header.append(addr_mode)  # length bits zero -> separate length byte
        if addr_mode != ADDR_NONE:
            header += bytes((target, source))
        header.append(n)

    body = bytes(header) + bytes(payload)
    return body + bytes((checksum(body),))


def frame_length_hint(header: bytes) -> int:
    """Given the start of a frame, return the total frame length in bytes.

    Raises :class:`IncompleteFrame` if ``header`` is too short to tell yet.
    Useful for an incremental reader that must know how many bytes to expect.
    """
    if len(header) < 1:
        raise IncompleteFrame("need the format byte")
    fmt = header[0]
    addr_mode = fmt & _ADDR_MASK
    length = fmt & _LEN_MASK
    idx = 1
    if addr_mode != ADDR_NONE:
        idx += 2  # target + source
    if length == 0:
        if len(header) < idx + 1:
            raise IncompleteFrame("need the separate length byte")
        length = header[idx]
        idx += 1
    return idx + length + 1  # + checksum


def parse_frame(buf: bytes) -> tuple[ParsedFrame, int]:
    """Parse the first frame in ``buf``.

    Returns the parsed frame plus the number of bytes consumed.  Raises
    :class:`IncompleteFrame` if ``buf`` is too short and :class:`ChecksumError`
    on a bad checksum.
    """
    total = frame_length_hint(buf)  # may raise IncompleteFrame
    if len(buf) < total:
        raise IncompleteFrame(f"need {total} bytes, have {len(buf)}")

    fmt = buf[0]
    addr_mode = fmt & _ADDR_MASK
    length = fmt & _LEN_MASK
    idx = 1
    target = source = None
    if addr_mode != ADDR_NONE:
        target, source = buf[1], buf[2]
        idx = 3
    if length == 0:
        length = buf[idx]
        idx += 1

    payload = bytes(buf[idx : idx + length])
    cs_index = idx + length
    expected = checksum(buf[:cs_index])
    if buf[cs_index] != expected:
        raise ChecksumError(
            f"checksum 0x{buf[cs_index]:02X} != expected 0x{expected:02X}"
        )
    frame = ParsedFrame(
        payload=payload,
        target=target,
        source=source,
        addr_mode=addr_mode,
        raw=bytes(buf[:total]),
    )
    return frame, total
