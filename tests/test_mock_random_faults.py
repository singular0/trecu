"""Random multi-fault mock mode (`--mock` seeds a varied set of real codes).

All mock-only: exercises the DB's random-DTC generator, the byte-code encoder,
the OBD mock's un-capped Mode 03 enumeration, and the CLI wiring — no hardware.
"""

import random

import pytest

from trecu.cli import _build_parser, _make_config, _make_transport
from trecu.protocol.dtc import DtcDatabase, decode_dtc_bytes, encode_dtc_code
from trecu.service import DiagnosticService
from trecu.transport.mock_obd import MockObdTransport


def test_encode_dtc_code_roundtrips_structural():
    for code in ("P1108", "P0107", "C1611", "U0001"):
        high, low = encode_dtc_code(code)
        assert decode_dtc_bytes(high, low) == code


def test_encode_dtc_code_rejects_nonstructural():
    with pytest.raises(ValueError):
        encode_dtc_code("K1535")  # K is not a J2012 letter — no byte pair
    with pytest.raises(ValueError):
        encode_dtc_code("P4000")  # first digit above 3 can't be encoded
    with pytest.raises(ValueError):
        encode_dtc_code("PZ0Z")  # wrong length / non-hex


def test_random_dtcs_are_distinct_known_codes():
    db = DtcDatabase.load_default()
    pairs = db.random_dtcs(rng=random.Random(1234))
    assert 2 <= len(pairs) <= 6
    codes = [decode_dtc_bytes(hi, lo) for hi, lo in pairs]
    assert len(set(codes)) == len(codes)  # no duplicates
    assert all(not db.describe(code).startswith("Unknown") for code in codes)


def test_random_dtcs_count_bounds_are_honoured():
    db = DtcDatabase.load_default()
    for seed in range(30):
        pairs = db.random_dtcs(rng=random.Random(seed), min_count=3, max_count=3)
        assert len(pairs) == 3


def test_random_dtcs_vary_the_type():
    # Family-first sampling should surface more than just the dominant P range.
    db = DtcDatabase.load_default()
    letters = set()
    for seed in range(20):
        for hi, lo in db.random_dtcs(rng=random.Random(seed)):
            letters.add(decode_dtc_bytes(hi, lo)[0])
    assert {"P", "C", "U"} <= letters


def test_random_dtcs_empty_when_no_structural_codes():
    assert DtcDatabase({"K1535": "raw", "L0001": "raw"}).random_dtcs() == []


def test_mock_obd_enumerates_more_than_three_dtcs():
    # Mode 03 must serve every stored DTC, not cap at three, so a big set still
    # reconciles against the Mode 01 PID 01 count instead of looking mismatched.
    pairs = [(0x01, 0x07), (0x02, 0x01), (0x11, 0x05), (0x01, 0x22), (0x01, 0x14)]
    with DiagnosticService(MockObdTransport(dtcs=pairs)) as svc:
        result = svc.read_faults()
    assert result.count == len(pairs)
    assert [decode_dtc_bytes(h, l) for (h, l) in pairs] == [
        d.code for d in result.dtcs
    ]


def test_cli_mock_transport_gets_multiple_random_faults():
    args = _build_parser().parse_args(["faults", "--mock"])
    transport = _make_transport(args, _make_config(args))
    assert isinstance(transport, MockObdTransport)
    assert len(transport._dtcs) >= 2


def test_cli_rejects_removed_db_option():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--db", "faults.json"])
