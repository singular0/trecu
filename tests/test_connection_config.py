"""Connection flags reach the protocol client — in every mode, `auto` included.

`--protocol auto` used to build its clients from per-protocol defaults, so
`--init-address` / `--ecu-address` / `--tester-address` / `--timeout` were
silently inert in the *default* mode (they only applied when a protocol was
named). `EcuConfig` carries both protocol sections through the auto sweep.
Mock-only: no hardware.
"""

import pytest

from trecu.cli import _build_parser, _make_config, _make_transport, main
from trecu.protocol.iso9141 import Iso9141Client, Iso9141Config
from trecu.protocol.kwp2000 import Kwp2000Client, Kwp2000Config
from trecu.service import DiagnosticService, EcuConfig, as_ecu_config
from trecu.transport.mock_kline import MockKLineTransport
from trecu.transport.mock_obd import MockObdTransport


def _config(*argv: str) -> EcuConfig:
    return _make_config(_build_parser().parse_args(list(argv)))


# -- CLI: flags fill both sections regardless of --protocol -------------------
def test_auto_mode_carries_every_connection_flag() -> None:
    cfg = _config(
        "faults",
        "--init-address", "0x43",
        "--ecu-address", "0xD6",
        "--tester-address", "0xF6",
        "--timeout", "2.5",
    )
    assert cfg.iso9141.init_address == 0x43
    assert cfg.iso9141.p2_timeout == 2.5
    assert cfg.kwp2000.ecu_address == 0xD6
    assert cfg.kwp2000.tester_address == 0xF6
    assert cfg.kwp2000.p2_timeout == 2.5


def test_unset_flags_leave_each_protocol_default_alone() -> None:
    cfg = _config("faults")
    assert cfg.iso9141 == Iso9141Config()
    assert cfg.kwp2000 == Kwp2000Config()
    # The two protocols' response timeouts genuinely differ; a CLI default
    # would have flattened them.
    assert cfg.iso9141.p2_timeout != cfg.kwp2000.p2_timeout


@pytest.mark.parametrize(
    "protocol,init_mode",
    (("kwp-slow", "slow"), ("kwp-fast", "fast")),
)
def test_named_kwp_protocol_pins_init_mode(protocol: str, init_mode: str) -> None:
    cfg = _config("faults", "--protocol", protocol, "--ecu-address", "0xD6")
    assert cfg.kwp2000.init_mode == init_mode
    assert cfg.kwp2000.ecu_address == 0xD6


def test_mock_ecu_moves_with_the_address_overrides() -> None:
    # The simulated ECU reads the same config the client will, so an override
    # moves both together instead of leaving the mock behind at the default.
    args = _build_parser().parse_args(["faults", "--mock", "--init-address", "0x43"])
    transport = _make_transport(args, _make_config(args))
    assert isinstance(transport, MockObdTransport)
    assert transport.init_address == 0x43

    args = _build_parser().parse_args(
        ["faults", "--mock", "--protocol", "kwp-slow", "--ecu-address", "0xD6"]
    )
    transport = _make_transport(args, _make_config(args))
    assert isinstance(transport, MockKLineTransport)
    assert transport.ecu_address == 0xD6


def test_faults_command_honours_init_address_in_auto_mode(capsys) -> None:
    # The reported repro: in the default (auto) mode this exited 2 with
    # "no 0x55 sync byte" — the mock ECU moved, the client stayed at 0x33.
    assert main(["faults", "--mock", "--init-address", "0x43"]) == 0
    assert "error:" not in capsys.readouterr().err


# -- service: normalization + per-candidate section selection ----------------
def test_as_ecu_config_accepts_every_shape() -> None:
    iso = Iso9141Config(init_address=0x43)
    kwp = Kwp2000Config(ecu_address=0xD6)
    assert as_ecu_config(None) == EcuConfig()
    assert as_ecu_config(iso).iso9141 is iso
    assert as_ecu_config(iso).kwp2000 == Kwp2000Config()  # other half defaulted
    assert as_ecu_config(kwp).kwp2000 is kwp
    whole = EcuConfig(iso9141=iso, kwp2000=kwp)
    assert as_ecu_config(whole) is whole
    with pytest.raises(TypeError):
        as_ecu_config("iso9141")


def test_each_candidate_client_gets_its_own_section() -> None:
    cfg = EcuConfig(
        iso9141=Iso9141Config(init_address=0x43),
        kwp2000=Kwp2000Config(ecu_address=0xD6),
    )
    svc = DiagnosticService(MockObdTransport(), cfg, protocol="auto")

    iso_client = svc._build_client("iso9141")
    assert isinstance(iso_client, Iso9141Client)
    assert iso_client.config.init_address == 0x43

    for proto, init_mode in (("kwp-slow", "slow"), ("kwp-fast", "fast")):
        kwp_client = svc._build_client(proto)
        assert isinstance(kwp_client, Kwp2000Client)
        assert kwp_client.config.ecu_address == 0xD6
        assert kwp_client.config.init_mode == init_mode


def test_auto_sweep_connects_at_an_overridden_ecu_address() -> None:
    # A Keihin moved to 0xD6 answers a 5-baud init only at that address, so
    # reaching kwp-slow (rather than falling through to kwp-fast, which the
    # mock accepts at any address) proves the override survived the sweep.
    transport = MockKLineTransport(ecu_address=0xD6, supports_slow_init=True)
    cfg = EcuConfig(
        # Keep the doomed iso9141 candidate from burning its retry budget.
        iso9141=Iso9141Config(init_retries=1, retry_wait=0.0),
        kwp2000=Kwp2000Config(ecu_address=0xD6),
    )
    with DiagnosticService(transport, cfg, protocol="auto") as svc:
        svc.read_faults()
        assert svc.active_protocol == "kwp-slow"


def test_auto_sweep_without_the_override_never_reaches_kwp_slow() -> None:
    transport = MockKLineTransport(ecu_address=0xD6, supports_slow_init=True)
    cfg = EcuConfig(
        iso9141=Iso9141Config(init_retries=1, retry_wait=0.0),
        # Left at the default 0xD5, where this ECU no longer answers.
        kwp2000=Kwp2000Config(init_retries=1, retry_wait=0.0),
    )
    with DiagnosticService(transport, cfg, protocol="auto") as svc:
        svc.read_faults()
        assert svc.active_protocol == "kwp-fast"
