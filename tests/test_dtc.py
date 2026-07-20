from trecu.protocol.dtc import DtcDatabase, decode_dtc_bytes, decode_status


def test_decode_dtc_letters():
    assert decode_dtc_bytes(0x01, 0x07) == "P0107"
    assert decode_dtc_bytes(0x41, 0x23) == "C0123"
    assert decode_dtc_bytes(0x81, 0x23) == "B0123"
    assert decode_dtc_bytes(0xC1, 0x23) == "U0123"


def test_decode_dtc_hex_digits():
    # High byte 0x11 -> first digit 1, second digit 1; low 0x76 -> 7,6
    assert decode_dtc_bytes(0x11, 0x76) == "P1176"


def test_decode_status_bits():
    flags = decode_status(0x09)  # bit0 testFailed + bit3 confirmed
    assert "testFailed" in flags
    assert "confirmed" in flags


def test_database_known_and_unknown():
    db = DtcDatabase.load_default()
    assert len(db) > 0
    desc, subsystem = db.describe("P0107")
    assert "MAP" in desc or "manifold" in desc.lower()
    assert subsystem

    unknown_desc, _ = db.describe("P3FFF")
    assert unknown_desc.startswith("Unknown code")


def test_make_dtc_populates_description():
    db = DtcDatabase.load_default()
    dtc = db.make_dtc(0x01, 0x07, 0x21)
    assert dtc.code == "P0107"
    assert dtc.is_known
    row = dtc.as_row()
    assert row[0] == "P0107"
    assert len(row) == 4
