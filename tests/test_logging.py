"""Logging levels and command-line verbosity."""

from trecu.cli import main
from trecu.logging import Logger
from trecu.protocol.kwp2000 import Kwp2000Client
from trecu.transport.mock_kline import MockKLineTransport


def test_logger_filters_debug_but_keeps_warnings() -> None:
    messages = []
    logger = Logger(messages.append)

    logger.debug("raw frame")
    logger.warning("recoverable problem")

    assert messages == ["[warning] recoverable problem"]


def test_debug_cli_enables_protocol_debug_messages(capsys) -> None:
    args = ["faults", "--mock", "--protocol", "iso9141"]

    assert main(args) == 0
    normal = capsys.readouterr()
    assert "-> " not in normal.err
    assert "<- " not in normal.err
    assert "OBD request:" not in normal.err
    assert "ECU operation:" not in normal.err
    assert "Connected via iso9141." not in normal.out
    assert "ECU key bytes" not in normal.out

    assert main([*args, "--debug"]) == 0
    debug = capsys.readouterr()
    assert "-> " in debug.err
    assert "<- " in debug.err
    assert "OBD request: Mode 09 (vehicle information)" in debug.err
    assert "OBD ECU status:" in debug.err
    assert "ECU operation complete: decoded" in debug.err
    assert "Connected via iso9141." not in debug.out


def test_kwp_debug_identifies_services_and_results() -> None:
    messages = []
    transport = MockKLineTransport()
    transport.open()
    try:
        client = Kwp2000Client(transport, logger=messages.append)
        client.connect()
        client.read_dtcs()
    finally:
        transport.close()

    assert any("KWP request: StartCommunication (0x81)" in m for m in messages)
    assert any("KWP response: StartDiagnosticSession acknowledged" in m for m in messages)
    assert any("KWP ECU reported 3 stored DTC(s)" in m for m in messages)
