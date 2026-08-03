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
    assert "--init-address INIT_ADDRESS" in output
    assert "--timeout TIMEOUT" in output
    # Removed with the KWP path: the CLI speaks one protocol, so there is
    # nothing to select and no KWP addressing to override.
    assert "--protocol" not in output
    assert "--ecu-address" not in output
    assert "--tester-address" not in output
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
    assert main(["info", "--mock"]) == 0
    output = capsys.readouterr().out
    assert "Connected via" not in output
    assert "Field" in output
    assert "Value" in output
    assert "VIN" in output


def test_faults_command_omits_identity_and_table_title(capsys) -> None:
    assert main(["faults", "--mock"]) == 0
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
    assert main(["sensors", "--mock"]) == 0
    output = capsys.readouterr().out
    assert "Live data snapshot" not in output
    assert "Sensor" in output
    assert "Value" in output
    assert "Unit" in output


def test_clear_command_confirms_on_stdout(capsys) -> None:
    assert main(["clear", "--mock", "-y"]) == 0
    assert "Fault codes cleared." in capsys.readouterr().out


# -- the shared service wrapper ------------------------------------------------
@pytest.mark.parametrize("command", ("faults", "info", "sensors", "clear"))
def test_ecu_commands_share_one_failure_path(command: str, capsys) -> None:
    """All four ECU subcommands run through ``_with_service``, so an
    unreachable cable is exit 2 + ``error:`` on stderr for every one of them —
    no command can drift onto its own error handling."""
    argv = [command, "--port", "/dev/nonexistent-trecu"]
    if command == "clear":
        argv.append("-y")  # don't block on the confirmation prompt

    assert main(argv) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: could not open /dev/nonexistent-trecu")
    assert captured.out == ""  # nothing printed when the operation never ran


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
            },
            {
                "device": "/dev/ttyS0",
                "vid": None,
                "pid": None,
                "description": "Built-in serial port",
                "likely_kkl": False,
            },
        ],
    )

    assert main(["ports"]) == 0
    output = capsys.readouterr().out
    assert "Cable" in output
    assert "Device" in output
    assert "VID:PID" in output
    assert "Description" in output
    assert "/dev/ttyUSB0" in output
    assert "0403:6001" in output
    assert "likely KKL cable" not in output
    rows = {
        device: next(line for line in output.splitlines() if device in line)
        for device in ("/dev/ttyUSB0", "/dev/ttyS0")
    }
    assert "*" in rows["/dev/ttyUSB0"]
    assert "*" not in rows["/dev/ttyS0"]


def test_tui_command_launches_ui(monkeypatch) -> None:
    launched = []
    monkeypatch.setattr(cli, "_cmd_tui", lambda args: launched.append(args) or 0)

    assert main(["tui", "--init-address", "0x43"]) == 0
    assert launched[0].init_address == 0x43


def test_keyboard_interrupt_exits_cleanly(monkeypatch, capsys) -> None:
    def interrupt(_args) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_cmd_tui", interrupt)

    assert main(["tui"]) == 130
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "\nInterrupted.\n"


def test_old_action_flags_are_rejected() -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--read"])


@pytest.mark.parametrize("option", ("-v", "--verbose"))
def test_old_verbose_flags_are_rejected(option: str) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["faults", option])
