"""Connection flags reach the protocol client.

`--init-address` and `--timeout` are the two per-bike overrides the CLI still
exposes; both have to survive the trip from argv into the `Iso9141Config` the
client reads, and `--init-address` has to move the *simulated* ECU too, or a
`--mock` run with an override would fail to sync. Mock-only: no hardware.
"""

import pytest

from trecu.cli import _build_parser, _make_config, _make_transport, main
from trecu.protocol.iso9141 import Iso9141Client, Iso9141Config
from trecu.service import DiagnosticService
from trecu.transport.mock_obd import MockObdTransport


def _config(*argv: str) -> Iso9141Config:
    return _make_config(_build_parser().parse_args(list(argv)))


# -- CLI: flags reach the config ---------------------------------------------
def test_flags_land_in_the_iso_config() -> None:
    cfg = _config("faults", "--init-address", "0x43", "--timeout", "2.5")
    assert cfg.init_address == 0x43
    assert cfg.p2_timeout == 2.5


def test_unset_flags_leave_the_documented_defaults_alone() -> None:
    # Unset flags parse as None, which means "don't touch it" — a CLI default
    # would silently overwrite whatever Iso9141Config documents.
    assert _config("faults") == Iso9141Config()


def test_mock_ecu_moves_with_the_address_override() -> None:
    # The simulated ECU reads the same config the client will, so an override
    # moves both together instead of leaving the mock behind at the default.
    args = _build_parser().parse_args(["faults", "--mock", "--init-address", "0x43"])
    transport = _make_transport(args, _make_config(args))
    assert isinstance(transport, MockObdTransport)
    assert transport.init_address == 0x43


def test_faults_command_honours_init_address(capsys) -> None:
    # The reported repro: this used to exit 2 with "no 0x55 sync byte" — the
    # mock ECU moved to 0x43, the client stayed at 0x33.
    assert main(["faults", "--mock", "--init-address", "0x43"]) == 0
    assert "error:" not in capsys.readouterr().err


# -- CLI: the removed KWP options are gone -----------------------------------
@pytest.mark.parametrize(
    "argv",
    (
        ["faults", "--protocol", "iso9141"],
        ["faults", "--protocol", "auto"],
        ["faults", "--ecu-address", "0xD5"],
        ["faults", "--tester-address", "0xF5"],
    ),
    ids=("protocol-named", "protocol-auto", "ecu-address", "tester-address"),
)
def test_removed_kwp_options_are_rejected(argv) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(argv)


# -- service: the config reaches the one client it builds --------------------
def test_the_client_gets_the_service_config() -> None:
    cfg = Iso9141Config(init_address=0x43)
    svc = DiagnosticService(MockObdTransport(), cfg)

    client = svc._build_client()
    assert isinstance(client, Iso9141Client)
    assert client.config.init_address == 0x43


def test_service_defaults_its_config_when_given_none() -> None:
    svc = DiagnosticService(MockObdTransport())
    assert svc.config == Iso9141Config()
    assert svc._build_client().config == Iso9141Config()
