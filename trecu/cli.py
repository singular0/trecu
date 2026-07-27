"""Command-line entry point for trecu."""

from __future__ import annotations

import argparse
import sys
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
)
from .transport.base import Transport, TransportError


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trecu",
        description="Read and decode Triumph motorcycle ECU fault codes over a "
        "KKL (FT232RL) K-line cable.",
    )
    p.add_argument("--version", action="version", version=f"TrECU {__version__}")

    conn = p.add_argument_group("connection")
    conn.add_argument("-p", "--port", help="serial port (e.g. /dev/ttyUSB0, /dev/cu.usbserial-XXXX, or COM3)")
    conn.add_argument("--baud", type=int, default=10400, help="K-line baud rate (default 10400)")
    conn.add_argument("--mock", action="store_true", help=argparse.SUPPRESS)
    conn.add_argument(
        "--protocol",
        choices=[PROTOCOL_AUTO, PROTOCOL_ISO9141, PROTOCOL_KWP_SLOW, PROTOCOL_KWP_FAST],
        default=PROTOCOL_AUTO,
        help="diagnostic protocol: auto (default), iso9141 (5-baud + OBD, "
        "confirmed Sagem Triumph), kwp-slow (KWP2000 with 5-baud init, "
        "Keihin K-line), or kwp-fast (KWP2000 fast-init)",
    )
    conn.add_argument("--init-address", type=lambda x: int(x, 0), default=0x33, help="ISO 9141 5-baud init address (default 0x33; some Sagem models use 0x43)")
    conn.add_argument("--ecu-address", type=lambda x: int(x, 0), default=0xD5, help="KWP2000 ECU target address (default 0xD5, the Triumph engine ECU)")
    conn.add_argument("--tester-address", type=lambda x: int(x, 0), default=0xF5, help="KWP2000 tester source address (default 0xF5)")
    conn.add_argument("--timeout", type=float, default=1.0, help="response timeout seconds (default 1.0)")
    conn.add_argument("--db", help="path to a custom fault-code JSON database")

    action = p.add_argument_group("actions (default: launch the TUI)")
    action.add_argument("-l", "--list-ports", action="store_true", help="list serial ports and exit")
    action.add_argument("-r", "--read", action="store_true", help="read codes once, print them, and exit (no TUI)")
    action.add_argument("--live", action="store_true", help="read one live-data (sensor) snapshot, print it, and exit (no TUI)")
    action.add_argument("-c", "--clear", action="store_true", help="clear stored codes and exit")
    action.add_argument("-y", "--yes", action="store_true", help="do not prompt for confirmation on --clear")
    action.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="show debug logging, including raw protocol byte traffic",
    )
    return p


def _make_config(args: argparse.Namespace):
    """Build a protocol-specific config, or None for auto (service uses defaults)."""
    if args.protocol in (PROTOCOL_KWP_FAST, PROTOCOL_KWP_SLOW):
        return Kwp2000Config(
            ecu_address=args.ecu_address,
            tester_address=args.tester_address,
            baudrate=args.baud,
            p2_timeout=args.timeout,
            init_mode="slow" if args.protocol == PROTOCOL_KWP_SLOW else "fast",
        )
    if args.protocol == PROTOCOL_ISO9141:
        return Iso9141Config(
            init_address=args.init_address,
            baudrate=args.baud,
            p2_timeout=args.timeout,
        )
    return None  # auto: let the service pick per-attempt defaults


def _make_transport(args: argparse.Namespace) -> Transport:
    if args.mock:
        # Seed the simulated ECU with a random, type-varied set of real DB codes
        # so `--mock` shows a plausible spread of faults, not one canned code.
        pairs = _load_db(args).random_dtcs()
        if args.protocol in (PROTOCOL_KWP_FAST, PROTOCOL_KWP_SLOW):
            from .transport.mock_kline import MockKLineTransport

            triples = [(hi, lo, STATUS_CONFIRMED) for hi, lo in pairs]
            return MockKLineTransport(
                dtcs=triples or None,
                ecu_address=args.ecu_address,
                tester_address=args.tester_address,
                supports_slow_init=(args.protocol == PROTOCOL_KWP_SLOW),
            )
        from .transport.mock_obd import MockObdTransport

        return MockObdTransport(init_address=args.init_address, dtcs=pairs or None)
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
            "No FTDI/KKL cable detected. Plug it in, or pass --port, or use "
            "--mock. Run `trecu --list-ports` to see what is available."
        )
    raise SystemExit(
        "Multiple FTDI devices found; specify one with --port. Candidates: "
        + ", ".join(c["device"] for c in candidates)
    )


def _load_db(args: argparse.Namespace) -> DtcDatabase:
    if args.db:
        return DtcDatabase.load_file(args.db)
    return DtcDatabase.load_default()


def _cmd_list_ports() -> int:
    from .transport.serial_kline import list_serial_ports

    ports = list_serial_ports()
    if not ports:
        print("No serial ports found.")
        return 0
    print(f"{'DEVICE':<28} {'VID:PID':<10} DESCRIPTION")
    for p in ports:
        vidpid = (
            f"{p['vid']:04x}:{p['pid']:04x}" if p["vid"] and p["pid"] else "-"
        )
        marker = "  <- likely KKL cable" if p["likely_kkl"] else ""
        print(f"{p['device']:<28} {vidpid:<10} {p['description']}{marker}")
    return 0


def _print_dtcs(result) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    proto = f" via {result.protocol}" if result.protocol else ""
    console.print(f"[bold]Connected{proto}.[/bold]")
    if result.ecu_info:
        for label, value in result.ecu_info.as_rows():
            console.print(f"  {label}: [cyan]{value}[/cyan]")
    if not result.dtcs:
        console.print("[green]No stored fault codes.[/green]")
        return
    noun = "code" if result.count == 1 else "codes"
    table = Table(title=f"{result.count} stored fault {noun}")
    table.add_column("Code", style="bold red")
    table.add_column("Status")
    table.add_column("Description")
    for d in result.dtcs:
        code, status, desc = d.as_row()
        table.add_row(code, status, desc)
    console.print(table)


def _cmd_read(args: argparse.Namespace) -> int:
    logger = lambda m: print(m, file=sys.stderr)
    transport = _make_transport(args)
    service = DiagnosticService(
        transport, _make_config(args), _load_db(args), logger,
        protocol=args.protocol, verbose=args.verbose
    )
    try:
        with service:
            result = service.read_faults()
    except (TransportError, ProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _print_dtcs(result)
    return 0


def _print_live(readings) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not readings:
        console.print("[yellow]No live data reported by this ECU.[/yellow]")
        return
    table = Table(title="Live data snapshot")
    table.add_column("Sensor")
    table.add_column("Value", justify="right", style="bold cyan")
    table.add_column("Unit")
    table.add_column("Range", style="dim")
    for r in readings:
        table.add_row(r.name, r.formatted(), r.unit, f"{r.min:g}–{r.max:g}")
    console.print(table)


def _cmd_live(args: argparse.Namespace) -> int:
    logger = lambda m: print(m, file=sys.stderr)
    transport = _make_transport(args)
    service = DiagnosticService(
        transport, _make_config(args), _load_db(args), logger,
        protocol=args.protocol, verbose=args.verbose
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
    transport = _make_transport(args)
    service = DiagnosticService(
        transport, _make_config(args), _load_db(args), logger,
        protocol=args.protocol, verbose=args.verbose
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

    common = dict(
        config=_make_config(args),
        db=_load_db(args),
        protocol=args.protocol,
        verbose=args.verbose,
    )

    if args.mock:
        # Share one simulated ECU across connects so clearing codes persists,
        # mirroring how a real ECU retains state between sessions.
        shared = _make_transport(args)
        app = TrecuApp(transport_factory=lambda: shared, mock=True, port="mock", **common)
        app.run()
        return 0

    from .transport.serial_kline import KLineSerialTransport, list_serial_ports

    baud = args.baud
    transport_for_port = lambda port: KLineSerialTransport(port, baud)  # noqa: E731

    # Determine the port. Explicit --port wins; a single obvious FTDI cable is
    # auto-selected; otherwise (multiple or none) let the user pick in the TUI.
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
            transport_factory=None,  # triggers the in-TUI port picker
            mock=False,
            list_ports=list_serial_ports,
            transport_for_port=transport_for_port,
            **common,
        )
    app.run()
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.list_ports:
        return _cmd_list_ports()
    if args.clear:
        return _cmd_clear(args)
    if args.read:
        return _cmd_read(args)
    if args.live:
        return _cmd_live(args)
    return _cmd_tui(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
