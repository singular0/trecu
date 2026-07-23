import pytest

from trecu.protocol.kwp2000 import Kwp2000Client, NegativeResponse
from trecu.service import DiagnosticService
from trecu.transport.mock_kline import MockKLineTransport


def test_full_read_decode_clear_cycle():
    with DiagnosticService(MockKLineTransport()) as svc:
        result = svc.read_faults()
        assert result.key_bytes == b"\xEA\x8F"
        codes = {d.code for d in result.dtcs}
        assert codes == {"P0107", "P0201", "P1105"}
        # descriptions resolved from the bundled DB
        assert all(d.description for d in result.dtcs)

        svc.clear_faults()
        after = svc.read_faults()
        assert after.count == 0


def test_connect_returns_key_bytes():
    t = MockKLineTransport(key_bytes=b"\x12\x34")
    t.open()
    client = Kwp2000Client(t)
    info = client.connect()
    assert info.key_bytes == b"\x12\x34"
    t.close()


def test_unknown_service_raises_negative_response():
    t = MockKLineTransport()
    t.open()
    client = Kwp2000Client(t)
    client.start_communication()
    with pytest.raises(NegativeResponse) as excinfo:
        client.request(bytes((0x99,)))
    assert excinfo.value.nrc == 0x11  # serviceNotSupported
    t.close()


def test_custom_dtc_set():
    faults = [(0x01, 0x22, 0x08)]  # P0122
    with DiagnosticService(MockKLineTransport(dtcs=faults)) as svc:
        result = svc.read_faults()
        assert result.count == 1
        assert result.dtcs[0].code == "P0122"
