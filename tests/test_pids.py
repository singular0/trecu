"""Sensor-decode layer (Phase 3): PID formula evaluation + the PID database."""

import pytest

from trecu.protocol.pids import (
    FormulaError,
    KwpLocalTable,
    PidDatabase,
    PidDef,
    SensorReading,
    compile_formula,
)


# -- formula language --------------------------------------------------------
def test_compile_and_eval_linear_and_multibyte():
    # Coolant temp: A - 40.
    f = compile_formula("A - 40")
    assert f({"A": 130, "B": 0, "C": 0, "D": 0}) == 90
    # RPM: (256*A + B) / 4.
    rpm = compile_formula("(256 * A + B) / 4")
    assert rpm({"A": 0x14, "B": 0x50, "C": 0, "D": 0}) == 1300


def test_formula_rejects_disallowed_constructs():
    for bad in ("A ** 2", "__import__('os')", "A % 3", "abs(A)", "A << 1"):
        with pytest.raises(FormulaError):
            compile_formula(bad)


def test_formula_rejects_unknown_name():
    with pytest.raises(FormulaError):
        compile_formula("A + Z")  # Z is not a data byte


# -- PidDef ------------------------------------------------------------------
def test_piddef_decode_and_byte_count_guard():
    d = PidDef.from_entry(
        0x0C,
        {"name": "Engine RPM", "unit": "rpm", "bytes": 2, "formula": "(256 * A + B) / 4"},
    )
    assert d.decode(bytes((0x14, 0x50))) == 1300
    with pytest.raises(FormulaError):
        d.decode(bytes((0x14,)))  # needs 2 bytes, got 1


# -- PidDatabase -------------------------------------------------------------
def test_database_loads_default_pids():
    db = PidDatabase.load_default()
    assert len(db) >= 15
    for pid in (0x0C, 0x05, 0x11, 0x0B, 0x14, 0x42):  # roadmap's core dashboard set
        assert pid in db


def test_database_decode_roundtrip_and_reading_shape():
    db = PidDatabase.load_default()
    reading = db.decode(0x05, bytes((130,)))  # coolant: 130 - 40 = 90 °C
    assert isinstance(reading, SensorReading)
    assert reading.pid == 0x05
    assert reading.value == 90
    assert reading.unit == "°C"
    assert reading.formatted() == "90"


def test_database_decode_unknown_pid_raises():
    db = PidDatabase.load_default()
    with pytest.raises(KeyError):
        db.decode(0xFE, b"\x00")


def test_every_default_formula_stays_within_declared_bounds():
    # Endpoints of each 1-byte / 2-byte formula must land inside [min, max].
    db = PidDatabase.load_default()
    for pid in db.pids():
        d = db.get(pid)
        data = bytes([0x00] * d.num_bytes)
        hi = bytes([0xFF] * d.num_bytes)
        lo_v, hi_v = d.decode(data), d.decode(hi)
        assert d.min - 1e-6 <= min(lo_v, hi_v)
        assert max(lo_v, hi_v) <= d.max + 1e-6


def test_reading_formatting_integer_vs_decimal():
    assert SensorReading(0, "x", 1300.0, "rpm").formatted() == "1300"
    assert SensorReading(0, "x", 0.45, "V").formatted() == "0.45"
    assert SensorReading(0, "x", 13.80, "V").formatted() == "13.8"


# -- KwpLocalTable (packed Keihin 21 80 frame, draft layout) ------------------
def test_kwp_local_table_loads_from_its_own_file():
    assert len(PidDatabase.load_default()) == 19   # mode01 file is independent
    table = KwpLocalTable.load_default()
    assert table.lid == 0x80                   # Keihin's MODE_READ_SENSORS RLI
    assert len(table) == 53                    # full Keihin channel table
    for idx in (0, 3, 5, 50):                  # RPM, water temp, gear, battery
        assert idx in table


def test_kwp_local_decode_frame_applies_draft_formulas():
    table = KwpLocalTable.load_default()
    frame = bytearray(106)
    frame[6:8] = (115).to_bytes(2, "big")      # ch 3 water temp: 115 - 25 = 90
    frame[10:12] = (4).to_bytes(2, "big")      # ch 5 gear
    frame[50:52] = (58).to_bytes(2, "big")     # ch 50 battery: 5.8 + 8 = 13.8
    by_ch = {r.pid: r for r in table.decode_frame(bytes(frame))}
    assert len(by_ch) == 53
    assert by_ch[3].value == 90 and by_ch[3].unit == "°C"
    assert by_ch[5].value == 4
    assert by_ch[50].value == 13.8 and by_ch[50].unit == "V"
    assert by_ch[66].value == -1024            # zero slot -> the channel's offset


def test_kwp_local_decode_frame_filters_and_orders_by_request():
    table = KwpLocalTable.load_default()
    readings = table.decode_frame(bytes(106), channels=[50, 3, 999])
    assert [r.pid for r in readings] == [50, 3]  # unknown channel dropped


def test_kwp_local_decode_frame_drops_channels_beyond_short_frame():
    table = KwpLocalTable.load_default()
    readings = table.decode_frame(bytes(10))   # only the first 5 slots present
    assert {r.pid for r in readings} == {0, 1, 2, 3, 4}
