"""Command-line entry point for trecu."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from typing import List, Optional

from . import __version__
from .protocol.dtc import DtcDatabase
from .protocol.iso9141 import Iso9141Config
from .protocol.kwp2000 import STATUS_CONFIRMED, Kwp2000Config, ProtocolError
from .service import (
    PROTOCOL_AUTO,
    PROTOCOL_ISO9141,
    PROTOCOL_KWP_FAST,
    PROTOCOL_KWP_SLOW,
    DiagnosticService,
    EcuConfig,
)
from .transport.base import Transport, TransportError


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trecu",
        add_help=False,
        usage=(
            "trecu [tui|ports|faults|info|sensors|clear|version|help] "
            "[--protocol {auto,iso9141,kwp-slow,kwp-fast}] "
            "[--init-address INIT_ADDRESS] [--ecu-address ECU_ADDRESS] "
            "[--tester-address TESTER_ADDRESS] [--timeout TIMEOUT] [-y] [--debug]"
        ),
        description="Read and decode Triumph motorcycle ECU fault codes over a "
        "KKL (FT232RL) K-line cable.",
    )
    p.add_argument(
        "command",
        nargs="?",
        default="tui",
        choices=(
            "tui",
            "ports",
            "faults",
            "info",
            "sensors",
            "clear",
            "version",
            "help",
        ),
        help="operation to perform",
    )

    conn = p.add_argument_group("connection")
    # Kept as hidden development hooks. The public CLI auto-detects the serial
    # port and uses the standard K-line baud rate.
    conn.add_argument("-p", "--port", help=argparse.SUPPRESS)
    conn.add_argument("--baud", type=int, default=10400, help=argparse.SUPPRESS)
    conn.add_argument("--mock", action="store_true", help=argparse.SUPPRESS)
    conn.add_argument(
        "--protocol",
        choices=[PROTOCOL_AUTO, PROTOCOL_ISO9141, PROTOCOL_KWP_SLOW, PROTOCOL_KWP_FAST],
        default=PROTOCOL_AUTO,
        help="diagnostic protocol: auto (default), iso9141 (5-baud + OBD, "
        "confirmed Sagem Triumph), kwp-slow (KWP2000 with 5-baud init, "
        "Keihin K-line), or kwp-fast (KWP2000 fast-init)",
    )
    # These four default to None rather than to a value: unset means "leave the
    # protocol config's documented default alone", so only what the user
    # actually passed overrides it — in every protocol mode, auto included.
    conn.add_argument("--init-address", type=lambda x: int(x, 0), default=None, help="ISO 9141 5-baud init address (default 0x33; some Sagem models use 0x43)")
    conn.add_argument("--ecu-address", type=lambda x: int(x, 0), default=None, help="KWP2000 ECU target address (default 0xD5, the Triumph engine ECU)")
    conn.add_argument("--tester-address", type=lambda x: int(x, 0), default=None, help="KWP2000 tester source address (default 0xF5)")
    conn.add_argument("--timeout", type=float, default=None, help="response timeout seconds (default: the protocol's own, 0.8 ISO 9141 / 1.0 KWP2000)")

    options = p.add_argument_group("options")
    options.add_argument("-y", "--yes", action="store_true", help="do not prompt for confirmation when clearing faults")
    options.add_argument(
        "--debug",
        action="store_true",
        help="show debug logging, including raw protocol byte traffic",
    )
    return p


def _make_config(args: argparse.Namespace) -> EcuConfig:
    """Build the connection config for *every* protocol the run may try.

    Both sections are always filled, because `auto` builds a fresh client per
    candidate: an override has to be present in whichever section the winning
    protocol reads. Only flags the user actually passed override a protocol's
    own documented default (they parse as None otherwise).
    """
    iso = Iso9141Config(baudrate=args.baud)
    kwp = Kwp2000Config(baudrate=args.baud)
    if args.init_address is not None:
        iso = replace(iso, init_address=args.init_address)
    if args.ecu_address is not None:
        kwp = replace(kwp, ecu_address=args.ecu_address)
    if args.tester_address is not None:
        kwp = replace(kwp, tester_address=args.tester_address)
    if args.timeout is not None:
        iso = replace(iso, p2_timeout=args.timeout)
        kwp = replace(kwp, p2_timeout=args.timeout)
    if args.protocol in (PROTOCOL_KWP_FAST, PROTOCOL_KWP_SLOW):
        # In auto mode the service pins init_mode per candidate itself.
        kwp = replace(
            kwp, init_mode="slow" if args.protocol == PROTOCOL_KWP_SLOW else "fast"
        )
    return EcuConfig(iso9141=iso, kwp2000=kwp)


def _make_transport(args: argparse.Namespace, config: EcuConfig) -> Transport:
    if args.mock:
        # Seed the simulated ECU with a random, type-varied set of real DB codes
        # so `--mock` shows a plausible spread of faults, not one canned code.
        # Addresses come from the same config the client will use, so an
        # override moves the simulated ECU and the tester together.
        pairs = DtcDatabase.load_default().random_dtcs()
        if args.protocol in (PROTOCOL_KWP_FAST, PROTOCOL_KWP_SLOW):
            from .transport.mock_kline import MockKLineTransport

            triples = [(hi, lo, STATUS_CONFIRMED) for hi, lo in pairs]
            return MockKLineTransport(
                dtcs=triples or None,
                ecu_address=config.kwp2000.ecu_address,
                tester_address=config.kwp2000.tester_address,
                supports_slow_init=(args.protocol == PROTOCOL_KWP_SLOW),
            )
        from .transport.mock_obd import MockObdTransport

        return MockObdTransport(
            init_address=config.iso9141.init_address, dtcs=pairs or None
        )
    from .transport.serial_kline import KLineSerialTransport

    port = args.port or _autodetect_port()
    return KLineSerialTransport(port=port, baudrate=args.baud)


def _autodetect_port() -> str:
    """Pick the single obvious FTDI/KKL port, or fail with guidance."""
    from .transport.serial_kline import list_serial_ports

    ports = list_serial_ports()
    candidates = [p for p in ports if p["likely_kkl"]]
    if len(candidates) == 1:
        return candidates[0]["device"]
    if not candidates:
        raise SystemExit(
            "No FTDI/KKL cable detected. Plug it in, or pass --port. "
            "Run `trecu ports` to see what is available."
        )
    raise SystemExit(
        "Multiple FTDI devices found; specify one with --port. Candidates: "
        + ", ".join(c["device"] for c in candidates)
    )


def _cmd_list_ports() -> int:
    from rich.console import Console
    from rich.table import Table

    from .transport.serial_kline import list_serial_ports

    ports = list_serial_ports()
    if not ports:
        print("No serial ports found.")
        return 0
    table = Table()
    table.add_column("Cable")
    table.add_column("Device")
    table.add_column("VID:PID")
    table.add_column("Description")
    for p in ports:
        vidpid = (
            f"{p['vid']:04x}:{p['pid']:04x}" if p["vid"] and p["pid"] else "-"
        )
        marker = "*" if p["likely_kkl"] else ""
        table.add_row(marker, p["device"], vidpid, p["description"])
    Console().print(table)
    return 0


def _print_dtcs(result) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not result.dtcs:
        console.print("[green]No stored fault codes.[/green]")
        return
    table = Table()
    table.add_column("Code", style="bold red")
    table.add_column("Status")
    table.add_column("Description")
    for d in result.dtcs:
        code, status, desc = d.as_row()
        table.add_row(code, status, desc)
    console.print(table)


def _cmd_read(args: argparse.Namespace) -> int:
    logger = lambda m: print(m, file=sys.stderr)
    config = _make_config(args)
    service = DiagnosticService(
        _make_transport(args, config), config, logger=logger,
        protocol=args.protocol, verbose=args.debug
    )
    try:
        with service:
            result = service.read_faults()
    except (TransportError, ProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_dtcs(result)
    return 0


def _cmd_info(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.table import Table

    logger = lambda m: print(m, file=sys.stderr)
    config = _make_config(args)
    service = DiagnosticService(
        _make_transport(args, config), config, logger=logger,
        protocol=args.protocol, verbose=args.debug
    )
    try:
        with service:
            info = service.read_identification()
    except (TransportError, ProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    console = Console()
    if info and not info.is_empty:
        table = Table()
        table.add_column("Field")
        table.add_column("Value", style="cyan")
        for label, value in info.as_rows():
            table.add_row(label, value)
        console.print(table)
    else:
        console.print("[yellow]No ECU identification reported.[/yellow]")
    return 0


def _print_live(readings) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not readings:
        console.print("[yellow]No live data reported by this ECU.[/yellow]")
        return
    table = Table()
    table.add_column("Sensor")
    table.add_column("Value", justify="right", style="bold cyan")
    table.add_column("Unit")
    table.add_column("Range", style="dim")
    for r in readings:
        table.add_row(r.name, r.formatted(), r.unit, f"{r.min:g}–{r.max:g}")
    console.print(table)


def _cmd_live(args: argparse.Namespace) -> int:
    logger = lambda m: print(m, file=sys.stderr)
    config = _make_config(args)
    service = DiagnosticService(
        _make_transport(args, config), config, logger=logger,
        protocol=args.protocol, verbose=args.debug
    )
    try:
        with service:
            readings = service.read_live()
    except (TransportError, ProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_live(readings)
    return 0


def _cmd_clear(args: argparse.Namespace) -> int:
    if not args.yes:
        reply = input("Clear all stored fault codes? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 1
    logger = lambda m: print(m, file=sys.stderr)
    config = _make_config(args)
    service = DiagnosticService(
        _make_transport(args, config), config, logger=logger,
        protocol=args.protocol, verbose=args.debug
    )
    try:
        with service:
            service.clear_faults()
    except (TransportError, ProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print("Fault codes cleared.")
    return 0


def _cmd_tui(args: argparse.Namespace) -> int:
    from .tui.app import TrecuApp

    config = _make_config(args)
    common = dict(
        config=config,
        protocol=args.protocol,
        verbose=args.debug,
    )

    if args.mock:
        # Share one simulated ECU across connects so clearing codes persists,
        # mirroring how a real ECU retains state between sessions. Deliberate,
        # not a workaround: a factory that built a fresh mock per session would
        # resurrect the codes the user just cleared (see as_transport_factory).
        shared = _make_transport(args, config)
        app = TrecuApp(
            transport_factory=lambda: shared,
            mock=True,
            port="mock",
            **common,
        )
        app.run()
        return 0

    from .transport.serial_kline import KLineSerialTransport, list_serial_ports

    baud = args.baud
    transport_for_port = lambda port: KLineSerialTransport(port, baud)  # noqa: E731

    # An explicit port or a single obvious FTDI cable connects immediately.
    # With multiple/no candidates, the TUI presents its port picker.
    chosen: Optional[str] = args.port
    if chosen is None:
        candidates = [p for p in list_serial_ports() if p["likely_kkl"]]
        if len(candidates) == 1:
            chosen = candidates[0]["device"]

    if chosen is not None:
        app = TrecuApp(
            transport_factory=lambda: transport_for_port(chosen),
            mock=False,
            port=chosen,
            **common,
        )
    else:
        app = TrecuApp(
            transport_factory=None,
            mock=False,
            list_ports=list_serial_ports,
            transport_for_port=transport_for_port,
            **common,
        )
    app.run()
    return 0


def _run(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "help":
        _build_parser().print_help()
        return 0
    if args.command == "version":
        print(f"TrECU {__version__}")
        return 0
    if args.command == "tui":
        return _cmd_tui(args)
    if args.command == "ports":
        return _cmd_list_ports()
    if args.command == "clear":
        return _cmd_clear(args)
    if args.command == "faults":
        return _cmd_read(args)
    if args.command == "info":
        return _cmd_info(args)
    if args.command == "sensors":
        return _cmd_live(args)
    raise AssertionError(f"unhandled command: {args.command}")


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return _run(argv)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
