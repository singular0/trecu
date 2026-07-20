"""Textual application: connect, read, decode, and clear Triumph fault codes.

The UI is a persistent *session* (a status "spine") over a set of tabbed views:
a **Dashboard** (faults + ECU identity cards), the **Fault Codes** table, and
the raw protocol **Log**. See ``docs/tui-redesign.md`` for the concept and how
live-data / throttle-sync tabs slot in later.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Callable, Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Middle
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from ..protocol.dtc import DtcDatabase
from ..service import DiagnosticService, ReadResult
from ..transport.base import Transport
from .port_select import PortSelectScreen

TransportFactory = Callable[[], Transport]
PortLister = Callable[[], list]
TransportForPort = Callable[[str], Transport]

# Session state -> (dot colour, glyph, label) for the spine.
_SPINE = {
    "ready": ("grey62", "○", "ready"),
    "select": ("grey62", "○", "select a port"),
    "connecting": ("yellow", "●", "connecting…"),
    "reading": ("yellow", "●", "reading…"),
    "clearing": ("yellow", "●", "clearing codes…"),
    "connected": ("green", "●", "connected"),
    "error": ("red", "●", "error"),
}


class ConfirmScreen(ModalScreen[bool]):
    """A small yes/no modal. Cancel is the default; escape cancels."""

    BINDINGS = [("escape", "cancel", "Cancel")]

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

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


class TrecuApp(App):
    """Read and decode Triumph ECU fault codes."""

    TITLE = "trecu"
    SUB_TITLE = "Triumph ECU fault-code reader"

    CSS = """
    #spine {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    #brand { width: auto; text-style: bold; }
    #conn { width: 1fr; content-align: right middle; }
    /* Fill the leftover height instead of auto-sizing: an auto TabbedContent
       lets the 1fr Fault Codes / Log widgets overflow the screen by a row,
       which shows a full-height screen scrollbar. */
    TabbedContent { height: 1fr; }
    #dashboard { height: auto; padding: 1 1 0 1; }
    .card {
        width: 1fr;
        height: auto;
        min-height: 10;
        border: round $primary;
        padding: 1 2;
        margin: 0 1;
    }
    #dtcs { height: 1fr; }
    #dtcs > .datatable--cursor { background: $accent; }
    #empty {
        height: 1fr;
        content-align: center middle;
        color: $success;
        text-style: bold;
        display: none;
    }
    #log { height: 1fr; background: $surface-darken-1; }
    """

    BINDINGS = [
        Binding("r", "read", "Read"),
        Binding("c", "clear", "Clear"),
        # TabbedContent's own left/right bindings switch tabs but are hidden
        # (show=False). Re-declare them at app level with priority so they win
        # the binding chain and appear in the footer.
        Binding("left", "prev_tab", "Prev tab", priority=True),
        Binding("right", "next_tab", "Next tab", priority=True),
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
        verbose: bool = False,
    ):
        super().__init__()
        self._transport_factory = transport_factory
        self._config = config
        self._db = db or DtcDatabase.load_default()
        self._mock = mock
        self._protocol = protocol
        self._verbose = verbose
        self._port = port or ("mock ECU" if mock else None)
        # Used only when no port is known yet and the user must choose one.
        self._list_ports = list_ports
        self._transport_for_port = transport_for_port
        self._state = "ready"
        self._last: Optional[ReadResult] = None

    # -- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Horizontal(id="spine"):
            yield Static("TrECU", id="brand")
            yield Static(id="conn")
        with TabbedContent(initial="tab-dashboard"):
            with TabPane("Dashboard", id="tab-dashboard"):
                with Horizontal(id="dashboard"):
                    yield Static(id="card-faults", classes="card")
                    yield Static(id="card-connection", classes="card")
                    yield Static(id="card-identity", classes="card")
            with TabPane("Faults", id="tab-faults"):
                yield DataTable(id="dtcs", zebra_stripes=True)
                yield Static("✓  No stored fault codes", id="empty")
            with TabPane("Log", id="tab-log"):
                yield RichLog(id="log", markup=False, wrap=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#dtcs", DataTable)
        table.cursor_type = "row"
        table.add_columns("Code", "Status", "Subsystem", "Description")
        self.query_one("#card-faults", Static).border_title = "Faults"
        self.query_one("#card-connection", Static).border_title = "Connection"
        self.query_one("#card-identity", Static).border_title = "ECU identity"
        self.query_one("#card-faults", Static).update(
            Text.from_markup("[dim]Press r to read fault codes.[/]")
        )
        self.query_one("#card-identity", Static).update(
            Text.from_markup("[dim]Not connected.[/]")
        )
        self._refresh_connection_card()
        self._set_state("ready")
        if self._verbose:
            self.action_show_tab("tab-log")
        if self._transport_factory is not None:
            mode = "MOCK ECU (no hardware)" if self._mock else "serial K-line"
            self._append_log(f"trecu ready — {mode} mode. Press 'r' to read.")
            # Defer until the TabbedContent has finished re-parenting its panes,
            # so the worker's _populate can find #dtcs (mirrors _choose_port).
            self.call_after_refresh(self.action_read)
        else:
            # No definite port yet — ask the user to choose one.
            self._append_log("Multiple/no serial ports — choose one to begin.")
            self._set_state("select")
            self.call_after_refresh(self._choose_port)

    # -- spine (persistent session status) -----------------------------------
    def _set_state(self, state: str) -> None:
        self._state = state
        self._refresh_spine()

    def _refresh_spine(self) -> None:
        color, dot, label = _SPINE.get(self._state, _SPINE["ready"])
        # Synthetic MIL lamp: red dot only when the ECU reports stored faults.
        mil = "[red]●[/]  " if (self._last and self._last.count) else ""
        conn = f"{mil}[{color}]{dot}[/] [b]{label}[/]"
        self.query_one("#conn", Static).update(Text.from_markup(conn))

    # -- helpers -------------------------------------------------------------
    def _append_log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        # Error lines use an "[error]" prefix (see _on_error); render them red.
        style = "bold red" if msg.startswith("[error]") else ""
        line = Text.assemble((f"{stamp}  ", "dim"), (msg, style))
        self.query_one("#log", RichLog).write(line)

    def _logger(self, msg: str) -> None:
        """Protocol logger — invoked from the worker thread."""
        self.call_from_thread(self._append_log, msg)

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def _step_tab(self, delta: int) -> None:
        tabs = self.query_one(TabbedContent)
        order = [pane.id for pane in tabs.query(TabPane)]
        if not order:
            return
        try:
            idx = order.index(tabs.active)
        except ValueError:
            idx = 0
        tabs.active = order[(idx + delta) % len(order)]

    def action_prev_tab(self) -> None:
        self._step_tab(-1)

    def action_next_tab(self) -> None:
        self._step_tab(1)

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        # Read/Clear are tab-specific; refresh the footer when the tab changes.
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        """Gate Read/Clear to the tabs where they make sense (hide elsewhere).

        Read populates the Dashboard and Fault Codes views; Clear acts on the
        fault list and belongs only to Fault Codes. Returning ``False`` (not
        ``None``, which would merely dim it) removes the binding from the footer
        and makes the key inert on other tabs.
        """
        if action in ("read", "clear"):
            try:
                active = self.query_one(TabbedContent).active
            except Exception:
                return True
            if action == "read":
                return active in ("tab-dashboard", "tab-faults")
            return active == "tab-faults"
        return True

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

    # -- rendering results ---------------------------------------------------
    def _populate(self, result: ReadResult) -> None:
        self._last = result
        table = self.query_one("#dtcs", DataTable)
        table.clear()
        for dtc in result.dtcs:
            table.add_row(*dtc.as_row())
        empty = self.query_one("#empty", Static)
        table.display = bool(result.count)
        empty.display = not result.count
        self._update_faults_card(result)
        self._refresh_connection_card()
        self._update_identity_card(result)
        self._set_state("connected")
        proto = f" via {result.protocol}" if result.protocol else ""
        self._append_log(f"read complete: {result.count} fault code(s){proto}")

    def _update_faults_card(self, result: ReadResult) -> None:
        if not result.count:
            text = "[green]✓  No stored fault codes[/]\n\n[dim]Nothing reported by the ECU.[/]"
        else:
            lines = [f"[b red]{result.count}[/] stored fault code(s)", ""]
            for dtc in result.dtcs:
                lines.append(f"[b]{dtc.code}[/]")
            text = "\n".join(lines)
        self.query_one("#card-faults", Static).update(Text.from_markup(text))

    def _update_identity_card(self, result: ReadResult) -> None:
        lines = []
        if result.ecu_info:
            for label, value in result.ecu_info.as_rows():
                lines.append(f"[b]{label:<12}[/]{value}")
        else:
            lines.append("[dim]Identity not reported by this ECU.[/]")
        self.query_one("#card-identity", Static).update(
            Text.from_markup("\n".join(lines))
        )

    def _refresh_connection_card(self) -> None:
        mode = "Mock" if self._mock else "Serial"
        port = self._port or "—"
        if self._last and self._last.protocol:
            proto = self._last.protocol
        else:
            proto = self._protocol or "—"
        lines = [
            f"[b]{'Mode':<10}[/]{mode}",
            f"[b]{'Port':<10}[/]{port}",
            f"[b]{'Protocol':<10}[/]{proto}",
        ]
        self.query_one("#card-connection", Static).update(
            Text.from_markup("\n".join(lines))
        )

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
        self._refresh_connection_card()
        self._append_log(f"using port {device}")
        self.action_read()

    # -- actions -------------------------------------------------------------
    @work(exclusive=True, group="ecu")
    async def action_read(self) -> None:
        if self._transport_factory is None:
            self._choose_port()
            return
        self._set_state("connecting")
        try:
            result = await asyncio.to_thread(self._blocking_read)
        except Exception as exc:  # transport/protocol errors surface here
            self._on_error(exc)
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
        self._set_state("clearing")
        try:
            await asyncio.to_thread(self._blocking_clear)
        except Exception as exc:
            self._on_error(exc)
            return
        self._append_log("fault codes cleared; re-reading…")
        self.action_read()

    def _on_error(self, exc: Exception) -> None:
        self._append_log(f"[error] {exc}")
        self._set_state("error")
        self.action_show_tab("tab-log")
        self.bell()
