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
    desc = db.describe("P0107")
    assert "MAP" in desc or "manifold" in desc.lower()

    assert db.describe("P3FFF").startswith("Unknown code")


def test_database_covers_service_manual_import():
    # The bundled DB is the community-sourced service-manual import: 557 codes
    # across the P/K/C/U/L families.
    db = DtcDatabase.load_default()
    assert len(db) >= 550
    assert db.describe("K1535") == "ETV Actuator Failure"
    assert db.describe("P1108") == (
        "Barometric pressure sensor - High voltage - Open circuit"
    )
    assert db.describe("C1611").startswith("Front wheel sensor")
    assert not db.describe("L0001").startswith("Unknown")  # ABS wheel unit family
    assert not db.describe("U0001").startswith("Unknown")  # network family


def test_decode_dtc_family_prefix_uses_raw_hex_digits():
    # Keihin raw fault numbers: bytes are literal hex digits, no bit-fields.
    assert decode_dtc_bytes(0x15, 0x35, family="K") == "K1535"
    # Structural decode of the same bytes would give a different, wrong label.
    assert decode_dtc_bytes(0x15, 0x35) == "P1535"


def test_decode_all_with_family():
    db = DtcDatabase.load_default()
    dtcs = db.decode_all([(0x15, 0x35, 0x21)], family="K")
    assert [d.code for d in dtcs] == ["K1535"]
    assert dtcs[0].is_known


def test_make_dtc_populates_description():
    db = DtcDatabase.load_default()
    dtc = db.make_dtc(0x01, 0x07, 0x21)
    assert dtc.code == "P0107"
    assert dtc.is_known
    row = dtc.as_row()
    assert row[0] == "P0107"
    assert len(row) == 3
