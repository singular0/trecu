"""Response framing: what the client must refuse to decode.

Every test here feeds the ISO 9141-2 client traffic that is corrupt,
misaddressed, incomplete, or simply not an answer to the request in flight, and
asserts it cannot surface as an ECU identity, DTC, or sensor reading. The
positive cases live in ``test_iso9141_obd.py`` / ``test_identification.py``;
this file is the other half — a frame that fails any check is discarded, never
best-effort decoded.

The doubles below script *raw* bytes onto the wire (the mock ECU frames
correctly by construction), so a test can put an exact byte sequence in front of
the parser.
"""

from typing import List

import pytest

from trecu.protocol.common import (
    ProtocolError,
    reassemble_identification,
    split_response_frames,
)
from trecu.protocol.iso9141 import Iso9141Client, Iso9141Config
from trecu.transport.mock_obd import MockObdTransport

ECU = 0xD1        # the tested Triumph's ECU source address
OTHER = 0x10      # some other module on the bus

# No wire is involved, so no timeout needs to be long. dtc_retries=1 keeps a
# deliberately-unanswerable Mode 03 from spending the retry budget.
FAST = dict(
    p2_timeout=0.05,
    pending_timeout=0.05,
    id_timeout=0.05,
    live_timeout=0.05,
    request_gap=0.0,
    frame_gap=0.0,
    dtc_retries=1,
    dtc_retry_wait=0.0,
)


def frame(
    payload: bytes, *, source: int = ECU, fmt: int = 0x48, target: int = 0x6B
) -> bytes:
    """A well-formed response frame: header + payload + checksum."""
    head = bytes((fmt, target, source)) + payload
    return head + bytes((sum(head) & 0xFF,))


class RawReplyEcu(MockObdTransport):
    """Mock ECU whose OBD answers are scripted raw bytes.

    Everything below the OBD layer still behaves like the real mock — the
    5-baud init works — so a test connects normally and only the response bytes
    are under its control.
    """

    def __init__(self, reply, **kw):
        super().__init__(**kw)
        self.reply = reply  # bytes, or callable(request_payload) -> bytes

    def _respond(self, payload: bytes) -> None:
        data = self.reply(payload) if callable(self.reply) else self.reply
        if data:
            self._rx.extend(data)


class GappedEcu(RawReplyEcu):
    """Scripted ECU that leaves a quiet gap between its response frames.

    Each chunk is delivered on its own ``read``, with one empty read in
    between — which is exactly what an end-of-message idle gap looks like to the
    collector. A multi-frame answer therefore only arrives in full if the client
    waits past the first gap.
    """

    def __init__(self, chunks: List[bytes], **kw):
        super().__init__(b"", **kw)
        self._script = list(chunks)
        self._chunks: List[bytes] = []
        self._gap = False

    def _respond(self, payload: bytes) -> None:
        self._chunks = list(self._script)
        self._gap = False

    def read(self, count: int, timeout: float) -> bytes:
        if not self._rx:
            if self._gap:
                self._gap = False
                return b""
            if self._chunks:
                self._rx.extend(self._chunks.pop(0))
                self._gap = bool(self._chunks)
        return super().read(count, timeout)


class EchoingEcu(RawReplyEcu):
    """K-line double that reflects transmitted bytes, like the single wire does.

    ``corrupt`` reflects something *other* than what was sent, standing in for
    the echo arriving garbled; the ECU's real answer still follows it.
    """

    echoes = True

    def __init__(self, reply, *, corrupt: bool = False, drop: bool = False, **kw):
        super().__init__(reply, **kw)
        self._corrupt = corrupt
        self._drop = drop

    def write(self, data: bytes) -> None:
        b = bytes(data)
        if self._await_inv and len(b) == 1:  # 5-baud handshake: echo it cleanly
            self._rx.extend(b)
            super().write(b)
            return
        if len(b) >= 5 and b[0] == 0x68:
            if not self._drop:
                self._rx.extend(b"\xAA" * len(b) if self._corrupt else b)
            self._respond(b[3:-1])
            return
        super().write(b)


def connect(transport, **cfg) -> Iso9141Client:
    """Open + 5-baud-init a client over ``transport``, with fast timings."""
    transport.open()
    client = Iso9141Client(transport, Iso9141Config(**{**FAST, **cfg}))
    client.connect()
    return client


# -- the splitter itself ------------------------------------------------------
def test_split_finds_one_exact_frame():
    frames, junk = split_response_frames(
        frame(b"\x41\x01\x81\x00\x00\xFF"), fmt=0x48, target=0x6B
    )
    assert junk == b""
    assert len(frames) == 1
    assert frames[0].source == ECU
    assert frames[0].payload == b"\x41\x01\x81\x00\x00\xFF"


def test_split_rejects_a_bad_checksum_as_junk():
    bad = bytearray(frame(b"\x41\x01\x81\x00\x00\xFF"))
    bad[-1] ^= 0xFF
    frames, junk = split_response_frames(bytes(bad), fmt=0x48, target=0x6B)
    assert frames == []
    assert junk == bytes(bad)  # nothing was salvaged for decoding


def test_split_separates_concatenated_frames_at_the_real_boundary():
    first = frame(b"\x43\x11\x08\x01\x07\x00\x00")
    second = frame(b"\x43\x02\x21\x00\x00\x00\x00")
    frames, junk = split_response_frames(first + second, fmt=0x48, target=0x6B)
    assert junk == b""
    assert [f.raw for f in frames] == [first, second]


def test_split_keeps_noise_and_a_truncated_tail_out_of_the_frame():
    good = frame(b"\x41\x0C\x1A\xF8")
    raw = b"\x00\xFF" + good + good[:4]  # break noise, a frame, then a cut-off one
    frames, junk = split_response_frames(raw, fmt=0x48, target=0x6B)
    assert [f.raw for f in frames] == [good]
    assert junk == b"\x00\xFF" + good[:4]


def test_split_refuses_a_data_field_longer_than_the_iso_limit():
    # 8 data bytes: no bike sends this, and reading it would mean trusting a
    # length the protocol never states.
    frames, junk = split_response_frames(
        frame(b"\x49\x02\x01ABCDE"), fmt=0x48, target=0x6B
    )
    assert frames == []
    assert junk  # discarded whole


# -- checksum / header / addressing through the client ------------------------
def test_bad_checksum_is_rejected_not_decoded():
    bad = bytearray(frame(b"\x41\x01\x81\x00\x00\xFF"))
    bad[-1] ^= 0x01
    client = connect(RawReplyEcu(bytes(bad)))
    with pytest.raises(ProtocolError, match="checksum"):
        client.read_dtcs()


def test_wrong_response_header_is_not_a_frame():
    # Right format byte, wrong target: traffic that is not addressed to a tester.
    client = connect(RawReplyEcu(frame(b"\x41\x01\x81\x00\x00\xFF", target=0x6A)))
    with pytest.raises(ProtocolError):
        client.read_dtcs()


def test_frame_from_another_module_is_rejected():
    client = connect(
        RawReplyEcu(frame(b"\x41\x01\x81\x00\x00\xFF", source=OTHER)),
        ecu_address=ECU,
    )
    with pytest.raises(ProtocolError, match="module"):
        client.read_dtcs()


def test_session_latches_the_first_ecu_and_then_ignores_other_modules():
    """With no configured address the first answering module is the session's;
    a later answer from a different one is another module's traffic, not ours."""
    status = frame(b"\x41\x01\x81\x00\x00\xFF")
    replies = iter([status, frame(b"\x41\x01\x81\x00\x00\xFF", source=OTHER)])
    client = connect(RawReplyEcu(lambda _payload: next(replies)))

    assert client._read_status() == (True, 1)  # latches 0xD1
    with pytest.raises(ProtocolError, match="module"):
        client._read_status()


def test_answer_is_picked_out_of_traffic_from_several_modules():
    other = frame(b"\x41\x01\x00\x00\x00\x00", source=OTHER)
    mine = frame(b"\x41\x01\x81\x00\x00\xFF")
    client = connect(RawReplyEcu(other + mine), ecu_address=ECU)
    assert client._read_status() == (True, 1)


# -- mode / PID matching ------------------------------------------------------
def test_unrelated_mode_is_not_taken_as_the_answer():
    # Well-formed, but it answers Mode 03 — not the Mode 01 request in flight.
    client = connect(RawReplyEcu(frame(b"\x43\x11\x08\x00\x00\x00\x00")))
    with pytest.raises(ProtocolError, match="Mode 01"):
        client._read_status()


def test_wrong_pid_is_not_taken_as_the_answer():
    # A Mode 01 answer, but for PID 05 while PID 0C was asked for.
    client = connect(RawReplyEcu(frame(b"\x41\x05\x5A")))
    assert client.read_live([0x0C]) == {}  # dropped, not decoded as RPM


def test_late_reply_does_not_answer_the_next_request():
    """A leftover frame for an earlier PID must not stand in for this one."""
    stale = frame(b"\x41\x05\x5A")
    fresh = frame(b"\x41\x0C\x1A\xF8")
    client = connect(RawReplyEcu(stale + fresh))
    assert client.read_live([0x0C]) == {0x0C: b"\x1A\xF8"}


def test_negative_response_is_reported_as_a_rejection():
    client = connect(RawReplyEcu(frame(b"\x7F\x01\x12")))
    with pytest.raises(ProtocolError, match="rejected Mode 01"):
        client._read_status()


def test_zero_and_ff_data_are_answers_not_absences():
    client = connect(RawReplyEcu(lambda p: frame(bytes((0x41, p[1], 0x00, 0xFF)))))
    assert client.read_live([0x0C]) == {0x0C: b"\x00\xFF"}


# -- noise, truncation, extra bytes -------------------------------------------
def test_leading_line_noise_before_the_frame_is_ignored():
    client = connect(RawReplyEcu(b"\x00\x00\xFF" + frame(b"\x41\x01\x81\x00\x00\xFF")))
    assert client._read_status() == (True, 1)


def test_trailing_bytes_after_the_frame_are_discarded():
    client = connect(RawReplyEcu(frame(b"\x41\x01\x81\x00\x00\xFF") + b"\x00\x13"))
    assert client._read_status() == (True, 1)


def test_truncated_frame_is_rejected():
    client = connect(RawReplyEcu(frame(b"\x41\x01\x81\x00\x00\xFF")[:-2]))
    with pytest.raises(ProtocolError):
        client._read_status()


# -- echo handling ------------------------------------------------------------
def test_echo_is_consumed_and_the_answer_still_parses():
    client = connect(EchoingEcu(frame(b"\x41\x01\x81\x00\x00\xFF")))
    assert client._read_status() == (True, 1)


def test_garbled_echo_does_not_eat_the_answer():
    client = connect(EchoingEcu(frame(b"\x41\x01\x81\x00\x00\xFF"), corrupt=True))
    assert client._read_status() == (True, 1)


def test_missing_echo_does_not_eat_the_answer():
    client = connect(EchoingEcu(frame(b"\x41\x01\x81\x00\x00\xFF"), drop=True))
    assert client._read_status() == (True, 1)


# -- multi-frame answers ------------------------------------------------------
def test_more_than_three_dtcs_arrive_as_several_frames():
    pairs = [(0x11, 0x08), (0x01, 0x07), (0x02, 0x21), (0x03, 0x35), (0x11, 0x71)]
    t = MockObdTransport(dtcs=list(pairs))
    client = connect(t)
    assert [(hi, lo) for hi, lo, _ in client.read_dtcs()] == pairs


def test_repeated_dtc_frame_does_not_inflate_the_count():
    """A retransmitted frame must not read as more codes than the ECU reports."""
    one = frame(b"\x43\x11\x08\x00\x00\x00\x00")
    status = frame(b"\x41\x01\x81\x00\x00\xFF")  # MIL on, exactly 1 stored
    client = connect(
        RawReplyEcu(lambda p: status if p[0] == 0x01 else one + one)
    )
    assert client.read_dtcs() == [(0x11, 0x08, 0x08)]


def test_frames_after_a_quiet_gap_are_still_part_of_the_answer():
    """The frames of one answer may be a full P2 gap apart — the collector has
    to wait past the first gap or a long DTC list reads short."""
    chunks = [
        frame(b"\x43\x11\x08\x01\x07\x02\x21"),
        frame(b"\x43\x03\x35\x00\x00\x00\x00"),
    ]
    client = connect(GappedEcu(chunks), frame_gap=0.05)
    assert len(client._request_dtcs(0x03, 0x08)) == 4

    # ... and with no follow-on window, only the first frame is seen.
    impatient = connect(GappedEcu(chunks), frame_gap=0.0)
    assert len(impatient._request_dtcs(0x03, 0x08)) == 3


# -- Mode 09 reassembly -------------------------------------------------------
def _vi(seq: int, data: bytes, pid: int = 0x02) -> bytes:
    return frame(bytes((0x49, pid, seq)) + data)


def test_mode09_reassembles_a_vin_from_its_fragments():
    client = connect(
        RawReplyEcu(
            _vi(1, b"\x00\x00\x00S")
            + _vi(2, b"MTA4")
            + _vi(3, b"69N4")
            + _vi(4, b"KT70")
            + _vi(5, b"0001")
        )
    )
    info = client.read_identification()
    assert info.vin == "SMTA469N4KT700001"


def test_mode09_sorts_out_of_order_fragments():
    client = connect(
        RawReplyEcu(_vi(2, b"MTA4") + _vi(1, b"\x00\x00\x00S") + _vi(3, b"6789"))
    )
    assert client.read_identification().vin == "SMTA46789"


def test_mode09_ignores_an_exact_duplicate_fragment():
    first = _vi(1, b"\x00\x00\x00S")
    client = connect(RawReplyEcu(first + first + _vi(2, b"MTA4")))
    assert client.read_identification().vin == "SMTA4"


def test_mode09_missing_fragment_yields_no_field_not_partial_ascii():
    client = connect(RawReplyEcu(_vi(1, b"\x00\x00\x00S") + _vi(3, b"69N4")))
    info = client.read_identification()
    assert info.vin == ""
    assert info.is_empty


def test_mode09_conflicting_duplicate_yields_no_field():
    client = connect(RawReplyEcu(_vi(1, b"AAAA") + _vi(1, b"BBBB")))
    assert client.read_identification().vin == ""


def test_reassemble_identification_rules():
    assert reassemble_identification(
        [b"\x49\x02\x02DEF", b"\x49\x02\x01ABC"], max_frames=8
    ) == b"ABCDEF"
    with pytest.raises(ProtocolError, match="missing"):
        reassemble_identification([b"\x49\x02\x02DEF"], max_frames=8)
    with pytest.raises(ProtocolError, match="conflicting"):
        reassemble_identification(
            [b"\x49\x02\x01ABC", b"\x49\x02\x01XYZ"], max_frames=8
        )
    with pytest.raises(ProtocolError, match="too short"):
        reassemble_identification([b"\x49\x02\x01"], max_frames=8)
    with pytest.raises(ProtocolError, match="exceeded"):
        reassemble_identification(
            [bytes((0x49, 0x02, n)) + b"AB" for n in range(1, 5)], max_frames=3
        )


def test_mode09_frame_bound_rejects_an_endless_response():
    fragments = b"".join(_vi(n, b"ABCD") for n in range(1, 6))
    client = connect(RawReplyEcu(fragments), max_frames=3)
    with pytest.raises(ProtocolError, match="exceeded"):
        client.obd_request_multi(b"\x09\x02")


# -- nothing corrupt reaches the service --------------------------------------
def test_corrupt_traffic_never_becomes_a_reading_or_a_code():
    """One end-to-end sweep: a bad-checksum bus produces errors, never data."""
    bad = bytearray(frame(b"\x41\x01\x81\x00\x00\xFF"))
    bad[-1] ^= 0x80
    client = connect(RawReplyEcu(bytes(bad)))
    with pytest.raises(ProtocolError):
        client.read_dtcs()
    assert client.read_live([0x0C, 0x05]) == {}
    assert client.read_identification().is_empty
