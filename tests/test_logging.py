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


def test_verbose_cli_enables_protocol_debug_messages(capsys) -> None:
    args = ["--mock", "--protocol", "iso9141", "--read"]

    assert main(args) == 0
    normal = capsys.readouterr()
    assert "-> " not in normal.err
    assert "<- " not in normal.err
    assert "OBD request:" not in normal.err
    assert "ECU operation:" not in normal.err

    assert main([*args, "-v"]) == 0
    verbose = capsys.readouterr()
    assert "-> " in verbose.err
    assert "<- " in verbose.err
    assert "OBD request: Mode 09 (vehicle information)" in verbose.err
    assert "OBD ECU status:" in verbose.err
    assert "ECU operation complete: decoded" in verbose.err


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
