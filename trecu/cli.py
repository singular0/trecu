"""Command-line entry point for trecu."""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from typing import Callable, List, Optional, TypeVar

from . import __version__
from .protocol.common import ProtocolError
from .protocol.dtc import DtcDatabase
from .protocol.iso9141 import Iso9141Config
from .service import DiagnosticService
from .transport.base import Transport, TransportError

T = TypeVar("T")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="trecu",
        add_help=False,
        usage=(
            "trecu [tui|ports|faults|info|sensors|clear|version|help] "
            "[--init-address INIT_ADDRESS] [--timeout TIMEOUT] [-y] [--debug]"
        ),
        description="Read and decode Triumph motorcycle ECU fault codes over a "
        "KKL (FT232RL) K-line cable, using ISO 9141-2 / OBD-II.",
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
    # Both default to None rather than to a value: unset means "leave
    # Iso9141Config's documented default alone", so only what the user actually
    # passed overrides it.
    conn.add_argument("--init-address", type=lambda x: int(x, 0), default=None, help="ISO 9141 5-baud init address (default 0x33; some Sagem models use 0x43)")
    conn.add_argument("--timeout", type=float, default=None, help="response timeout seconds (default 0.8)")

    options = p.add_argument_group("options")
    options.add_argument("-y", "--yes", action="store_true", help="do not prompt for confirmation when clearing faults")
    options.add_argument(
        "--debug",
        action="store_true",
        help="show debug logging, including raw protocol byte traffic",
    )
    return p


def _make_config(args: argparse.Namespace) -> Iso9141Config:
    """Build the ISO 9141-2 connection config for this run.

    Only flags the user actually passed override the config's own documented
    default (they parse as None otherwise).
    """
    iso = Iso9141Config(baudrate=args.baud)
    if args.init_address is not None:
        iso = replace(iso, init_address=args.init_address)
    if args.timeout is not None:
        iso = replace(iso, p2_timeout=args.timeout)
    return iso


def _make_transport(args: argparse.Namespace, config: Iso9141Config) -> Transport:
    if args.mock:
        # Seed the simulated ECU with a random, type-varied set of real DB codes
        # so `--mock` shows a plausible spread of faults, not one canned code.
        # The init address comes from the same config the client will use, so an
        # override moves the simulated ECU and the tester together.
        pairs = DtcDatabase.load_default().random_dtcs()
        from .transport.mock_obd import MockObdTransport

        return MockObdTransport(
            init_address=config.init_address, dtcs=pairs or None
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


def _stderr(message: str) -> None:
    """Protocol logger sink: diagnostics to stderr, results to stdout."""
    print(message, file=sys.stderr)


def _with_service(
    args: argparse.Namespace,
    operation: Callable[[DiagnosticService], T],
    show: Callable[[T], None],
) -> int:
    """Run one ECU ``operation`` for ``args`` and print it with ``show``.

    The four ECU subcommands differ only in those two callables; everything
    around them — the config, the transport built from that same config, the
    one-shot ``with service:`` lifecycle, and mapping a transport/protocol
    failure onto exit code 2 — is identical, and is here so it can only drift
    in one place. ``show`` runs after the session closes: printing is not an
    ECU operation, and a formatting bug should not read as a connection error.
    """
    config = _make_config(args)
    service = DiagnosticService(
        _make_transport(args, config),
        config,
        logger=_stderr,
        verbose=args.debug,
    )
    try:
        with service:
            result = operation(service)
    except (TransportError, ProtocolError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    show(result)
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


def _print_info(info) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not info or info.is_empty:
        console.print("[yellow]No ECU identification reported.[/yellow]")
        return
    table = Table()
    table.add_column("Field")
    table.add_column("Value", style="cyan")
    for label, value in info.as_rows():
        table.add_row(label, value)
    console.print(table)


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


def _print_cleared(_: None) -> None:
    print("Fault codes cleared.")


def _cmd_read(args: argparse.Namespace) -> int:
    return _with_service(args, DiagnosticService.read_faults, _print_dtcs)


def _cmd_info(args: argparse.Namespace) -> int:
    return _with_service(args, DiagnosticService.read_identification, _print_info)


def _cmd_live(args: argparse.Namespace) -> int:
    return _with_service(args, DiagnosticService.read_live, _print_live)


def _cmd_clear(args: argparse.Namespace) -> int:
    if not args.yes:
        reply = input("Clear all stored fault codes? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 1
    return _with_service(args, DiagnosticService.clear_faults, _print_cleared)


def _cmd_tui(args: argparse.Namespace) -> int:
    from .tui.app import TrecuApp

    config = _make_config(args)
    common = dict(
        config=config,
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
