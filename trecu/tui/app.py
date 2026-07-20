"""Textual application: connect, read, decode, and clear Triumph fault codes."""

from __future__ import annotations

import asyncio
from typing import Callable, Optional

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Middle
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, RichLog, Static

from ..protocol.dtc import DtcDatabase
from ..protocol.kwp2000 import Kwp2000Config
from ..service import DiagnosticService, ReadResult
from ..transport.base import Transport
from .port_select import PortSelectScreen

TransportFactory = Callable[[], Transport]
PortLister = Callable[[], list]
TransportForPort = Callable[[str], Transport]


class ConfirmScreen(ModalScreen[bool]):
    """A small yes/no modal."""

    DEFAULT_CSS = """
    ConfirmScreen {
        align: center middle;
    }
    #dialog {
        width: 52;
        height: auto;
        padding: 1 2;
        border: thick $warning;
        background: $surface;
    }
    #dialog Label { width: 100%; content-align: center middle; margin-bottom: 1; }
    #buttons { width: 100%; height: auto; align: center middle; }
    #buttons Button { margin: 0 1; }
    """

    def __init__(self, question: str):
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        with Middle(id="dialog"):
            yield Label(self._question)
            with Center(id="buttons"):
                yield Button("Yes, clear", variant="error", id="yes")
                yield Button("Cancel", variant="primary", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class TrecuApp(App):
    """Read and decode Triumph ECU fault codes."""

    TITLE = "trecu"
    SUB_TITLE = "Triumph ECU fault-code reader"

    CSS = """
    #status {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
        text-style: bold;
    }
    #dtcs { height: 1fr; }
    #dtcs > .datatable--cursor { background: $accent; }
    #log {
        height: 12;
        border-top: solid $primary;
        background: $surface-darken-1;
    }
    """

    BINDINGS = [
        Binding("r", "read", "Read codes"),
        Binding("c", "clear", "Clear codes"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        transport_factory: Optional[TransportFactory] = None,
        config: Optional[object] = None,
        db: Optional[DtcDatabase] = None,
        mock: bool = False,
        port: Optional[str] = None,
        list_ports: Optional[PortLister] = None,
        transport_for_port: Optional[TransportForPort] = None,
        protocol: str = "auto",
    ):
        super().__init__()
        self._transport_factory = transport_factory
        self._config = config
        self._db = db or DtcDatabase.load_default()
        self._mock = mock
        self._protocol = protocol
        self._port = port or ("mock ECU" if mock else None)
        # Used only when no port is known yet and the user must choose one.
        self._list_ports = list_ports
        self._transport_for_port = transport_for_port
        self._shown_ecu: Optional[str] = None  # last ECU identity logged

    # -- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(self._status_text("starting"), id="status")
        yield DataTable(id="dtcs", zebra_stripes=True)
        yield RichLog(id="log", markup=False, wrap=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#dtcs", DataTable)
        table.cursor_type = "row"
        table.add_columns("Code", "Status", "Subsystem", "Description")
        if self._transport_factory is not None:
            mode = "MOCK ECU (no hardware)" if self._mock else "serial K-line"
            self._append_log(f"trecu ready — {mode} mode. Press 'r' to read.")
            self.action_read()
        else:
            # No definite port yet — ask the user to choose one.
            self._append_log("Multiple/no serial ports — choose one to begin.")
            self._set_status("select a port")
            self.call_after_refresh(self._choose_port)

    # -- helpers -------------------------------------------------------------
    def _status_text(self, state: str, extra: str = "") -> str:
        mode = "MOCK" if self._mock else "SERIAL"
        where = f" {self._port}" if self._port else ""
        tail = f"  ·  {extra}" if extra else ""
        return f"{mode}{where}  ·  {state}{tail}"

    def _set_status(self, state: str, extra: str = "") -> None:
        self.query_one("#status", Static).update(self._status_text(state, extra))

    def _append_log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def _logger(self, msg: str) -> None:
        """Protocol logger — invoked from the worker thread."""
        self.call_from_thread(self._append_log, msg)

    def _make_service(self) -> DiagnosticService:
        return DiagnosticService(
            self._transport_factory(),
            self._config,
            self._db,
            self._logger,
            protocol=self._protocol,
        )

    def _blocking_read(self) -> ReadResult:
        with self._make_service() as svc:
            return svc.read_faults()

    def _blocking_clear(self) -> None:
        with self._make_service() as svc:
            svc.clear_faults()

    def _populate(self, result: ReadResult) -> None:
        table = self.query_one("#dtcs", DataTable)
        table.clear()
        for dtc in result.dtcs:
            table.add_row(*dtc.as_row())
        key = " ".join(f"{b:02X}" for b in result.key_bytes) or "-"
        state = f"{result.count} fault code(s)" if result.count else "no fault codes"
        extra = f"key bytes {key}"
        if result.ecu_info:
            summary = result.ecu_info.summary()
            if summary:
                extra = f"{extra}  ·  {summary}"
            # Log the full identity once per distinct ECU.
            if summary != self._shown_ecu:
                self._shown_ecu = summary
                for label, value in result.ecu_info.as_rows():
                    self._append_log(f"{label}: {value}")
        self._set_status(state, extra)

    # -- port selection ------------------------------------------------------
    def _choose_port(self) -> None:
        if self._list_ports is None:
            self._append_log("[error] no port lister configured")
            return
        self.push_screen(PortSelectScreen(self._list_ports), self._on_port_chosen)

    def _on_port_chosen(self, device: Optional[str]) -> None:
        if not device:
            self._append_log("No port selected — exiting.")
            self.exit()
            return
        if self._transport_for_port is None:
            self._append_log("[error] cannot build a transport for the chosen port")
            return
        self._port = device
        self._transport_factory = lambda: self._transport_for_port(device)
        self._append_log(f"using port {device}")
        self.action_read()

    # -- actions -------------------------------------------------------------
    @work(exclusive=True, group="ecu")
    async def action_read(self) -> None:
        if self._transport_factory is None:
            self._choose_port()
            return
        self._set_status("connecting…")
        try:
            result = await asyncio.to_thread(self._blocking_read)
        except Exception as exc:  # transport/protocol errors surface here
            self._append_log(f"[error] {exc}")
            self._set_status("error — see log")
            self.bell()
            return
        self._populate(result)

    def action_clear(self) -> None:
        self.push_screen(
            ConfirmScreen("Clear ALL stored fault codes from the ECU?"),
            self._on_clear_confirmed,
        )

    def _on_clear_confirmed(self, confirmed: bool) -> None:
        if confirmed:
            self._run_clear()

    @work(exclusive=True, group="ecu")
    async def _run_clear(self) -> None:
        self._set_status("clearing…")
        try:
            await asyncio.to_thread(self._blocking_clear)
        except Exception as exc:
            self._append_log(f"[error] {exc}")
            self._set_status("error — see log")
            self.bell()
            return
        self._append_log("fault codes cleared; re-reading…")
        self.action_read()
