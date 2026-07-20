"""ECU identification (Phase 1): OBD Mode 09 and KWP ReadEcuIdentification."""

from trecu.protocol.iso9141 import Iso9141Client
from trecu.protocol.kwp2000 import (
    EcuInfo,
    Kwp2000Client,
    decode_identification_ascii,
)
from trecu.service import DiagnosticService
from trecu.transport.mock import MockKLineTransport
from trecu.transport.mock_obd import MockObdTransport


def test_decode_ascii_strips_count_and_padding():
    # leading NODI byte (0x01) and trailing NUL padding are dropped
    raw = b"\x01" + b"SMTA469N4KT700001" + b"\x00\x00"
    assert decode_identification_ascii(raw) == "SMTA469N4KT700001"
    assert decode_identification_ascii(b"") == ""


def test_iso9141_reads_mode09_identity():
    t = MockObdTransport(
        vin="SMT12345678901234", calibration_id="CAL-9", ecu_name="Sagem X"
    )
    t.open()
    client = Iso9141Client(t)
    client.connect()
    info = client.read_identification()
    assert info.vin == "SMT12345678901234"
    assert info.calibration_id == "CAL-9"
    assert info.ecu_name == "Sagem X"
    assert not info.is_empty
    t.close()


def test_iso9141_unsupported_mode09_yields_empty():
    # Empty strings => the mock ECU does not answer those PIDs.
    t = MockObdTransport(vin="", calibration_id="", ecu_name="")
    t.open()
    client = Iso9141Client(t)
    client.connect()
    info = client.read_identification()
    assert info.is_empty
    t.close()


def test_kwp_reads_ecu_identification():
    t = MockKLineTransport(vin="VIN0000000000001", hardware="HW-1", software="SW-2")
    t.open()
    client = Kwp2000Client(t)
    client.start_communication()
    info = client.read_identification()
    assert info.vin == "VIN0000000000001"
    assert info.ecu_name == "HW-1"       # KWP hardware number -> ecu_name slot
    assert info.calibration_id == "SW-2"  # KWP software version -> calibration slot
    t.close()


def test_service_surfaces_identity_on_read_iso9141():
    with DiagnosticService(MockObdTransport(), protocol="iso9141") as svc:
        result = svc.read_faults()
        assert result.ecu_info is not None
        assert result.ecu_info.vin == "SMTA469N4KT700001"
        assert result.ecu_info.ecu_name == "Sagem MC2000"


def test_service_surfaces_identity_on_read_kwp():
    with DiagnosticService(MockKLineTransport(), protocol="kwp-fast") as svc:
        result = svc.read_faults()
        assert result.ecu_info is not None
        assert result.ecu_info.vin == "SMTA469N4KT700001"


def test_service_omits_identity_when_empty():
    t = MockObdTransport(vin="", calibration_id="", ecu_name="")
    with DiagnosticService(t, protocol="iso9141") as svc:
        result = svc.read_faults()
        assert result.ecu_info is None


def test_ecu_info_rows_and_summary():
    info = EcuInfo(vin="V1", calibration_id="C1", ecu_name="E1")
    assert info.as_rows() == [("ECU", "E1"), ("VIN", "V1"), ("Calibration", "C1")]
    assert info.summary() == "E1 · V1"
    assert EcuInfo().is_empty
