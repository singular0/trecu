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
    # The bundled DB is the community-sourced service-manual import, narrowed to
    # the 415 codes an OBD Mode 03/07 response can structurally decode to: the
    # P/C/U families. Non-structural K/L labels are not reachable and not carried.
    db = DtcDatabase.load_default()
    assert len(db) >= 410
    assert db.describe("P1108") == (
        "Barometric pressure sensor - High voltage - Open circuit"
    )
    assert db.describe("C1611").startswith("Front wheel sensor")
    assert not db.describe("U0001").startswith("Unknown")  # network family
    assert db.describe("K1535").startswith("Unknown code")  # Keihin raw: gone
