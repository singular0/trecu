import pytest

from trecu.protocol.framing import (
    ADDR_NONE,
    ADDR_PHYSICAL,
    ChecksumError,
    IncompleteFrame,
    build_frame,
    checksum,
    frame_length_hint,
    parse_frame,
)


def test_checksum_is_8bit_sum():
    assert checksum(b"\x80\x11\xf1\x81") == (0x80 + 0x11 + 0xF1 + 0x81) & 0xFF


def test_build_parse_roundtrip_physical():
    payload = bytes((0x18, 0x00, 0xFF, 0x00))
    frame = build_frame(payload, target=0x11, source=0xF1, addr_mode=ADDR_PHYSICAL)
    parsed, consumed = parse_frame(frame)
    assert consumed == len(frame)
    assert parsed.payload == payload
    assert parsed.target == 0x11
    assert parsed.source == 0xF1
    assert parsed.service == 0x18


def test_build_frame_short_form_header():
    # 4-byte payload -> format byte 0x80|4, then TGT, SRC, payload, checksum
    frame = build_frame(b"\x01\x02\x03\x04", 0x11, 0xF1)
    assert frame[0] == 0x84
    assert frame[1:3] == b"\x11\xf1"
    assert frame[-1] == checksum(frame[:-1])


def test_no_address_mode():
    frame = build_frame(b"\x3e\x01", 0x11, 0xF1, addr_mode=ADDR_NONE)
    assert frame[0] == 0x02  # length in low bits, no address bits
    parsed, _ = parse_frame(frame)
    assert parsed.target is None
    assert parsed.payload == b"\x3e\x01"


def test_long_payload_uses_separate_length_byte():
    payload = bytes(range(70))  # > 0x3F
    frame = build_frame(payload, 0x11, 0xF1)
    assert frame[0] == 0x80  # address bits set, length bits zero
    assert frame[3] == 70    # separate length byte
    parsed, _ = parse_frame(frame)
    assert parsed.payload == payload


def test_parse_detects_bad_checksum():
    frame = bytearray(build_frame(b"\x81", 0x11, 0xF1))
    frame[-1] ^= 0xFF
    with pytest.raises(ChecksumError):
        parse_frame(bytes(frame))


def test_incomplete_frame_raises():
    full = build_frame(b"\x18\x00\xff\x00", 0x11, 0xF1)
    with pytest.raises(IncompleteFrame):
        parse_frame(full[:-2])


def test_frame_length_hint_matches_full_length():
    full = build_frame(b"\x18\x00\xff\x00", 0x11, 0xF1)
    assert frame_length_hint(full) == len(full)
    # Enough bytes to compute the hint even from a prefix.
    assert frame_length_hint(full[:3]) == len(full)
