from trecu.protocol.iso9141 import Iso9141Client
from trecu.service import DiagnosticService
from trecu.transport.mock_obd import MockObdTransport


def test_obd_read_decode_clear_cycle():
    with DiagnosticService(MockObdTransport(), protocol="iso9141") as svc:
        result = svc.read_faults()
        assert result.protocol == "iso9141"
        assert result.key_bytes == b"\x08\x08"
        assert [d.code for d in result.dtcs] == ["P1108"]
        # P1108 resolves to the ambient-air-pressure description from the DB
        assert "pressure" in result.dtcs[0].description.lower()

        svc.clear_faults()
        after = svc.read_faults()
        assert after.count == 0


def test_auto_selects_iso9141_for_obd_ecu():
    with DiagnosticService(MockObdTransport(), protocol="auto") as svc:
        result = svc.read_faults()
        assert result.protocol == "iso9141"
        assert [d.code for d in result.dtcs] == ["P1108"]


def test_client_connect_and_status():
    t = MockObdTransport()
    t.open()
    client = Iso9141Client(t)
    info = client.connect()
    assert info.key_bytes == b"\x08\x08"
    mil, count = client.read_status()
    assert mil is True and count == 1
    assert client.read_dtcs() == [(0x11, 0x08, 0x08)]
    t.close()


def test_custom_dtcs_and_mil_off():
    t = MockObdTransport(dtcs=[(0x01, 0x07)], mil=False)  # P0107, MIL off
    with DiagnosticService(t, protocol="iso9141") as svc:
        result = svc.read_faults()
        assert [d.code for d in result.dtcs] == ["P0107"]
