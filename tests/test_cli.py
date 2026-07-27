"""Public command-line interface."""

import pytest

from trecu import __version__
from trecu import cli
from trecu.cli import _build_parser, main


COMMANDS = (
    "tui",
    "ports",
    "faults",
    "info",
    "sensors",
    "clear",
    "version",
    "help",
)


@pytest.mark.parametrize("command", COMMANDS)
def test_parser_accepts_public_commands(command: str) -> None:
    assert _build_parser().parse_args([command]).command == command


def test_help_prints_public_usage(capsys) -> None:
    assert main(["help"]) == 0
    output = capsys.readouterr().out
    assert output.startswith(
        "usage: trecu [tui|ports|faults|info|sensors|clear|version|help]"
    )
    assert "--protocol {auto,iso9141,kwp-slow,kwp-fast}" in output
    assert "--init-address INIT_ADDRESS" in output
    assert "--ecu-address ECU_ADDRESS" in output
    assert "--tester-address TESTER_ADDRESS" in output
    assert "--timeout TIMEOUT" in output
    assert "[-y] [--debug]" in output
    assert "--mock" not in output
    assert "--port" not in output
    assert "--baud" not in output


def test_no_command_launches_tui(monkeypatch) -> None:
    launched = []
    monkeypatch.setattr(cli, "_cmd_tui", lambda args: launched.append(args) or 0)

    assert main([]) == 0
    assert len(launched) == 1


def test_version_command(capsys) -> None:
    assert main(["version"]) == 0
    assert capsys.readouterr().out.strip() == f"TrECU {__version__}"


def test_info_command_reports_identity(capsys) -> None:
    assert main(["info", "--mock", "--protocol", "iso9141"]) == 0
    output = capsys.readouterr().out
    assert "Connected via" not in output
    assert "Field" in output
    assert "Value" in output
    assert "VIN" in output


def test_faults_command_omits_identity_and_table_title(capsys) -> None:
    assert main(["faults", "--mock", "--protocol", "iso9141"]) == 0
    output = capsys.readouterr().out
    assert "Connected via" not in output
    assert "VIN:" not in output
    assert "Calibration:" not in output
    assert "ECU:" not in output
    assert "stored fault code" not in output
    assert "Code" in output
    assert "Status" in output
    assert "Description" in output


def test_sensors_command_omits_table_title(capsys) -> None:
    assert main(["sensors", "--mock", "--protocol", "iso9141"]) == 0
    output = capsys.readouterr().out
    assert "Live data snapshot" not in output
    assert "Sensor" in output
    assert "Value" in output
    assert "Unit" in output


def test_ports_command_shows_table(monkeypatch, capsys) -> None:
    from trecu.transport import serial_kline

    monkeypatch.setattr(
        serial_kline,
        "list_serial_ports",
        lambda: [
            {
                "device": "/dev/ttyUSB0",
                "vid": 0x0403,
                "pid": 0x6001,
                "description": "FT232R USB UART",
                "likely_kkl": True,
            }
        ],
    )

    assert main(["ports"]) == 0
    output = capsys.readouterr().out
    assert "Device" in output
    assert "VID:PID" in output
    assert "Description" in output
    assert "/dev/ttyUSB0" in output
    assert "0403:6001" in output
    assert "likely KKL cable" in output


def test_tui_command_launches_ui(monkeypatch) -> None:
    launched = []
    monkeypatch.setattr(cli, "_cmd_tui", lambda args: launched.append(args) or 0)

    assert main(["tui", "--protocol", "kwp-fast"]) == 0
    assert launched[0].protocol == "kwp-fast"


def test_old_action_flags_are_rejected() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--read"])


@pytest.mark.parametrize("option", ("-v", "--verbose"))
def test_old_verbose_flags_are_rejected(option: str) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["faults", option])
