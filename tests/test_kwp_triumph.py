"""Community-derived Triumph Keihin KWP2000 behaviour.

Covers the Triumph-correct defaults (D5/F5 addressing, session 10 02, DTCs via
OBD Mode 03 over KWP framing, community-documented identification RLIs), the
AccessTimingParameter step, the kwp-slow 5-baud path (shared handshake +
retry/validation), and the shared OBD DTC pair parser.
"""

import pytest

from trecu.protocol.kwp2000 import (
    STATUS_CONFIRMED,
    Kwp2000Client,
    Kwp2000Config,
    ProtocolError,
    SlowInitConfig,
    parse_obd_dtc_pairs,
)
from trecu.service import _AUTO_ORDER, DiagnosticService
from trecu.transport.mock_kline import MockKLineTransport

# Slow-init failure paths shouldn't drag the suite out.
_FAST_RETRY = SlowInitConfig(
    init_retries=2, retry_wait=0.0, sync_timeout=0.05, byte_timeout=0.05
)


# -- Triumph-correct defaults (community reference) ---------------------------
def test_config_defaults_match_reference_triumph():
    cfg = Kwp2000Config()
    assert (cfg.ecu_address, cfg.tester_address) == (0xD5, 0xF5)
    assert cfg.diagnostic_session == 0x02          # 10 02, not the bogus 10 81
    assert cfg.read_dtc_service == 0x03            # OBD Mode 03 over KWP framing
    assert cfg.timing_params == bytes((0x1E, 0x02, 0x0A, 0x14, 0x00))
    # The community Triumph identifier list (per-field mapping pending hardware).
    assert (cfg.id_vin_rli, cfg.id_hardware_rli, cfg.id_software_rli) == (0xA0, 0xAE, 0x8C)
    assert cfg.init_mode == "fast"


def test_read_dtcs_uses_mode_03_with_synthetic_status():
    t = MockKLineTransport()
    t.open()
    client = Kwp2000Client(t)
    client.connect()
    triples = client.read_dtcs()
    # Mock's stored pairs come back with the synthetic confirmed status —
    # Mode 03 carries no per-DTC status byte.
    assert triples == [
        (0x01, 0x07, STATUS_CONFIRMED),
        (0x02, 0x01, STATUS_CONFIRMED),
        (0x11, 0x05, STATUS_CONFIRMED),
    ]
    t.close()


def test_read_dtcs_legacy_0x18_path_keeps_real_status():
    t = MockKLineTransport()
    t.open()
    client = Kwp2000Client(t, Kwp2000Config(read_dtc_service=0x18))
    client.connect()
    # ReadDTCByStatus returns the ECU's real statusOfDTC bytes.
    assert client.read_dtcs() == [(0x01, 0x07, 0x21), (0x02, 0x01, 0x2F), (0x11, 0x05, 0x20)]
    t.close()


# -- source-aware DTC labelling (Keihin raw fault numbers) --------------------
def test_dtc_family_none_on_mode_03_and_k_on_0x18():
    t = MockKLineTransport()
    assert Kwp2000Client(t).dtc_family is None                # J2012-encoded
    assert Kwp2000Client(t, Kwp2000Config(read_dtc_service=0x18)).dtc_family == "K"


def test_service_labels_0x18_faults_with_keihin_family():
    # A real Keihin answers 0x18 with raw fault numbers: bytes 15 35 are the
    # literal digits of K1535, not a J2012 bit-field (which would misread as
    # "P1535"). The service must label them via the client's dtc_family.
    t = MockKLineTransport(dtcs=[(0x15, 0x35, 0x21)])
    client = Kwp2000Client(t, Kwp2000Config(read_dtc_service=0x18))
    with DiagnosticService(t, client=client) as svc:
        result = svc.read_faults()
        assert [d.code for d in result.dtcs] == ["K1535"]
        assert "ETV" in result.dtcs[0].description


def test_connect_sends_timing_parameters():
    t = MockKLineTransport()
    t.open()
    client = Kwp2000Client(t)
    info = client.connect()
    assert info.session_started is True
    assert t.timing_params == bytes((0x1E, 0x02, 0x0A, 0x14, 0x00))
    t.close()


def test_connect_survives_timing_parameter_refusal():
    class NoAtp(MockKLineTransport):
        def _handle(self, payload):
            if payload and payload[0] == 0x83:
                return bytes((0x7F, 0x83, 0x11))  # serviceNotSupported
            return super()._handle(payload)

    t = NoAtp()
    t.open()
    client = Kwp2000Client(t)
    info = client.connect()                # best-effort: refusal is not fatal
    assert info.session_started is True
    assert t.timing_params is None
    assert client.read_dtcs()              # session still usable
    t.close()


# -- kwp-slow: 5-baud init at the ECU address ---------------------------------
def test_kwp_slow_connect_via_5_baud_handshake():
    t = MockKLineTransport(supports_slow_init=True, key_bytes=b"\x6B\x8F")
    t.open()
    client = Kwp2000Client(t, Kwp2000Config(init_mode="slow"))
    info = client.connect()
    assert info.key_bytes == b"\x6B\x8F"   # key bytes come from the handshake
    assert info.session_started is True
    assert client.read_dtcs()              # normal KWP services follow the init
    t.close()


def test_kwp_slow_refuses_transport_without_slow_init():
    t = MockKLineTransport()               # fast-init only
    t.open()
    client = Kwp2000Client(t, Kwp2000Config(init_mode="slow", slow_init=_FAST_RETRY))
    with pytest.raises(ProtocolError):
        client.connect()
    t.close()


def test_kwp_slow_rejects_bad_inverted_address():
    """The shared handshake validation applies to the KWP path too: a garbled
    inverted-address close must fail connect, not yield a half-open session."""

    class BadInvAddr(MockKLineTransport):
        def write(self, data):
            b = bytes(data)
            if self._await_inv and len(b) == 1:
                self._await_inv = False
                self._rx.append(0x00)      # wrong inverted address
                return
            super().write(b)

    t = BadInvAddr(supports_slow_init=True)
    t.open()
    client = Kwp2000Client(t, Kwp2000Config(init_mode="slow", slow_init=_FAST_RETRY))
    with pytest.raises(ProtocolError):
        client.connect()
    t.close()


def test_service_kwp_slow_end_to_end():
    t = MockKLineTransport(supports_slow_init=True)
    with DiagnosticService(t, protocol="kwp-slow") as svc:
        result = svc.read_faults()
        assert result.protocol == "kwp-slow"
        assert {d.code for d in result.dtcs} == {"P0107", "P0201", "P1105"}
        svc.clear_faults()
        assert svc.read_faults().count == 0


def test_auto_order_matches_reference_sweep():
    # iso9141 (confirmed Sagem) -> kwp-slow (Keihin K-line) -> kwp-fast.
    assert _AUTO_ORDER == ("iso9141", "kwp-slow", "kwp-fast")


def test_auto_falls_through_to_kwp_fast_on_fast_only_transport():
    # Both 5-baud paths refuse a fast-only transport up front (no retry
    # sleeps), leaving kwp-fast to connect.
    with DiagnosticService(MockKLineTransport(), protocol="auto") as svc:
        result = svc.read_faults()
        assert result.protocol == "kwp-fast"


# -- shared OBD DTC pair parser ----------------------------------------------
def test_parse_obd_dtc_pairs_skips_padding_and_odd_tail():
    body = bytes((0x11, 0x08, 0x00, 0x00, 0x01, 0x07, 0x99))  # pad pair + odd byte
    assert parse_obd_dtc_pairs(body, 0x08) == [(0x11, 0x08, 0x08), (0x01, 0x07, 0x08)]
    assert parse_obd_dtc_pairs(b"", 0x08) == []
