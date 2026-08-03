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
    "pids",
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
        "usage: trecu [tui|ports|faults|info|sensors|pids|clear|version|help]"
    )
    assert "--init-address INIT_ADDRESS" in output
    assert "--timeout TIMEOUT" in output
    # Removed with the KWP path: the CLI speaks one protocol, so there is
    # nothing to select and no KWP addressing to override.
    assert "--protocol" not in output
    assert "--ecu-address" not in output
    assert "--tester-address" not in output
    assert "[-y] [--debug]" in output
    # --port is public (naming a cable auto-detection can't pick, and skipping
    # the TUI's port picker); --baud and --mock stay hidden dev hooks.
    assert "[-p PORT]" in output
    assert "-p PORT, --port PORT" in output
    assert "--mock" not in output
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


def test_pids_command_shows_the_three_states_apart(capsys) -> None:
    """The headless capability view: advertised, decodable, and answered are
    three columns, never one verdict — and a PID outside the bitmap is shown as
    unadvertised rather than quietly missing from the table."""
    assert main(["pids", "--mock"]) == 0
    output = capsys.readouterr().out
    assert "Advertised" in output and "Decodable" in output and "Answered" in output
    assert "Engine RPM" in output          # advertised, decodable, answered
    assert "Control module" in output      # PID 42: decodable, but not advertised


def test_clear_command_confirms_on_stdout(capsys) -> None:
    assert main(["clear", "--mock", "-y"]) == 0
    assert "Fault codes cleared." in capsys.readouterr().out


# -- the shared service wrapper ------------------------------------------------
@pytest.mark.parametrize("command", ("faults", "info", "sensors", "pids", "clear"))
def test_ecu_commands_share_one_failure_path(command: str, capsys) -> None:
    """All four ECU subcommands run through ``_with_service``, so an
    unreachable cable is exit 2 + ``error:`` on stderr for every one of them —
    no command can drift onto its own error handling."""
    argv = [command, "--port", "/dev/nonexistent-trecu"]
    if command == "clear":
        argv.append("-y")  # don't block on the confirmation prompt

    assert main(argv) == 2
    captured = capsys.readouterr()
    assert "error: could not open /dev/nonexistent-trecu" in captured.err
    assert captured.out == ""  # nothing printed when the operation never ran


@pytest.mark.parametrize("command", ("faults", "info", "sensors", "pids", "clear"))
def test_ecu_commands_announce_the_port_on_stderr(command: str, capsys) -> None:
    """Every ECU subcommand names the device before talking to it.

    On stderr, with the rest of the diagnostics: a result table piped into
    another tool must not grow a port line at the top.
    """
    argv = [command, "--mock"]
    if command == "clear":
        argv.append("-y")

    assert main(argv) == 0
    captured = capsys.readouterr()
    # First thing said, before any protocol traffic or result.
    assert captured.err.splitlines()[0] == "Using port: mock ECU (no hardware)"
    assert "Using port:" not in captured.out


def test_announced_port_is_the_one_that_gets_opened(capsys) -> None:
    """The announced device is the one the run then fails to open — resolution
    happens once, so an auto-detected cable can't differ from the printed one."""
    assert main(["faults", "--port", "/dev/nonexistent-trecu"]) == 2
    err = capsys.readouterr().err
    assert err.splitlines()[0] == "Using port: /dev/nonexistent-trecu"
    assert "error: could not open /dev/nonexistent-trecu" in err


def test_clear_announces_the_port_before_asking_to_confirm(monkeypatch, capsys) -> None:
    """The destructive command names the ECU *before* the prompt, so the user
    answers knowing which one it lands on."""
    asked = []

    def fake_input(prompt: str) -> str:
        # stderr written so far is what the user could see when answering.
        asked.append(capsys.readouterr().err)
        return "n"

    monkeypatch.setattr("builtins.input", fake_input)

    assert main(["clear", "--mock"]) == 1
    assert asked == ["Using port: mock ECU (no hardware)\n"]
    assert "Aborted." in capsys.readouterr().out


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


def _capture_tui_app(monkeypatch, ports):
    """Run `trecu tui` far enough to capture how the app was wired, not run it.

    ``TrecuApp`` is patched out entirely: the question here is which arguments
    the CLI hands it (a fixed transport factory, or a port lister so the picker
    opens), and building the real app would start a UI.
    """
    from trecu.transport import serial_kline
    from trecu.tui import app as tui_app

    built = {}

    class _FakeApp:
        def __init__(self, **kw):
            built.update(kw)

        def run(self) -> None:
            pass

    monkeypatch.setattr(serial_kline, "list_serial_ports", lambda: list(ports))
    monkeypatch.setattr(tui_app, "TrecuApp", _FakeApp)
    return built


_ONE_CABLE = [
    {
        "device": "/dev/ttyUSB0",
        "vid": 0x0403,
        "pid": 0x6001,
        "description": "FT232R USB UART",
        "likely_kkl": True,
    },
]


def test_tui_opens_port_picker_even_with_one_obvious_cable(monkeypatch) -> None:
    # A single likely-KKL cable used to be auto-selected; the user picks now.
    built = _capture_tui_app(monkeypatch, _ONE_CABLE)

    assert main(["tui"]) == 0
    assert built["transport_factory"] is None  # no port fixed => picker opens
    assert built["list_ports"] is not None
    assert built["transport_for_port"] is not None
    assert built.get("port") is None


def test_tui_with_explicit_port_skips_the_picker(monkeypatch) -> None:
    built = _capture_tui_app(monkeypatch, _ONE_CABLE)

    assert main(["tui", "--port", "/dev/ttyUSB9"]) == 0
    assert built["port"] == "/dev/ttyUSB9"
    assert built["transport_factory"] is not None
    assert "list_ports" not in built


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
