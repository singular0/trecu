import pytest

from trecu.protocol.iso9141 import Iso9141Client, Iso9141Config
from trecu.protocol.kwp2000 import ProtocolError, SlowInitConfig
from trecu.service import DiagnosticService
from trecu.transport.mock_obd import MockObdTransport

# Fast timeouts so the no-response paths don't drag the suite out.
_FAST = dict(p2_timeout=0.05, pending_timeout=0.05, dtc_retry_wait=0.0)


class SilentMode03(MockObdTransport):
    """ECU that reports a fault via PID 01 but never answers Mode 03 — the real
    Sagem flakiness that used to read back as a false 'no stored codes'."""

    def _respond(self, payload):
        if payload and payload[0] == 0x03:
            return  # no reply, like the real ECU intermittently does
        super()._respond(payload)


class FlakyMode03(SilentMode03):
    """Answers Mode 03 only from the second attempt — exercises the retry."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._m03 = 0

    def _respond(self, payload):
        if payload and payload[0] == 0x03:
            self._m03 += 1
            if self._m03 < 2:
                return
        MockObdTransport._respond(self, payload)


class NoInvAddr(MockObdTransport):
    """5-baud init that never returns the inverted-address handshake byte."""

    def write(self, data):
        b = bytes(data)
        if self._await_inv and len(b) == 1:
            self._await_inv = False
            return  # swallow the inverted-key reply -> no inv-addr comes back
        super().write(b)


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


def test_mode03_silence_with_faults_raises_not_false_no_codes():
    """Status says a code exists but Mode 03 never answers -> hard error, not
    a silent 'no stored codes' (the bug behind 'reads codes once in a while')."""
    t = SilentMode03()  # default: MIL on, count 1
    client = Iso9141Client(t, Iso9141Config(dtc_retries=2, **_FAST))
    t.open()
    client.connect()
    with pytest.raises(ProtocolError):
        client.read_dtcs()
    t.close()


def test_mode03_retry_recovers_intermittent_answer():
    t = FlakyMode03()  # silent on attempt 1, answers on attempt 2
    client = Iso9141Client(t, Iso9141Config(dtc_retries=3, **_FAST))
    t.open()
    client.connect()
    assert client.read_dtcs() == [(0x11, 0x08, 0x08)]
    t.close()


def test_no_faults_still_reads_clean_when_status_says_zero():
    """MIL off / count 0 + a positive-empty Mode 03 is a genuine clean read."""
    t = MockObdTransport(dtcs=[], mil=False)
    client = Iso9141Client(t, Iso9141Config(dtc_retries=2, **_FAST))
    t.open()
    client.connect()
    assert client.read_dtcs() == []
    t.close()


def test_service_surfaces_read_failure_end_to_end():
    t = SilentMode03()
    cfg = Iso9141Config(dtc_retries=2, **_FAST)
    with DiagnosticService(t, config=cfg, protocol="iso9141") as svc:
        with pytest.raises(ProtocolError):
            svc.read_faults()


def test_slow_init_rejects_incomplete_handshake():
    """A missing inverted-address reply must fail connect (and drive a retry),
    not be accepted as a live session."""
    t = NoInvAddr()
    cfg = Iso9141Config(slow_init=SlowInitConfig(init_retries=2, retry_wait=0.0))
    client = Iso9141Client(t, cfg)
    t.open()
    with pytest.raises(ProtocolError):
        client.connect()
    t.close()
