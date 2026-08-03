"""ECU identification (Phase 1): OBD Mode 09 (VIN / calibration ID / ECU name)."""

from trecu.protocol.common import EcuInfo, decode_identification_ascii
from trecu.protocol.iso9141 import Iso9141Client
from trecu.service import DiagnosticService
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


def test_service_surfaces_identity_on_read():
    with DiagnosticService(MockObdTransport()) as svc:
        result = svc.read_faults()
        assert result.ecu_info is not None
        assert result.ecu_info.vin == "SMTA469N4KT700001"
        assert result.ecu_info.ecu_name == "Sagem MC2000"


def test_service_omits_identity_when_empty():
    t = MockObdTransport(vin="", calibration_id="", ecu_name="")
    with DiagnosticService(t) as svc:
        result = svc.read_faults()
        assert result.ecu_info is None


def test_ecu_info_rows():
    info = EcuInfo(vin="V1", calibration_id="C1", ecu_name="E1")
    assert info.as_rows() == [("ECU", "E1"), ("VIN", "V1"), ("Calibration", "C1")]
    assert EcuInfo().is_empty
