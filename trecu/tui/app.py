"""Textual application: connect, read, decode, and clear Triumph fault codes.

The UI is a persistent *session* (a status "spine") over a set of tabbed views:
a **Dashboard** (faults + ECU identity cards), the **Fault Codes** table, and
the raw protocol **Log**. See the "TUI: the tabbed session shell" section of
``ROADMAP.md`` for the concept and how live-data / throttle-sync tabs slot in
later.
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime
from typing import Callable, List, Optional

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
    LoadingIndicator,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from .. import __version__
from ..protocol.dtc import DtcDatabase
from ..protocol.pids import PidDatabase, SensorReading
from ..service import (
    DEFAULT_KEEPALIVE_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    DiagnosticService,
    ReadResult,
)
from ..transport.base import Transport
from .port_select import PortSelectScreen

# Trend sparkline: history length per PID and the block ramp used to draw it.
_HISTORY = 24
_SPARK = "▁▂▃▄▅▆▇█"


def _fmt_value(v: float) -> str:
    """Compact numeric string for the live table (integers stay whole)."""
    v = round(v, 2)
    return str(int(v)) if v == int(v) else f"{v:g}"


def _sparkline(values) -> str:
    """Render a value history as unicode block glyphs, autoscaled to its range."""
    vals = list(values)
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return _SPARK[3] * len(vals)  # flat line -> a mid-level bar
    span = hi - lo
    steps = len(_SPARK) - 1
    return "".join(_SPARK[round((v - lo) / span * steps)] for v in vals)

TransportFactory = Callable[[], Transport]
PortLister = Callable[[], list]
TransportForPort = Callable[[str], Transport]

# Session state -> (dot colour, glyph, label) for the spine.
_SPINE = {
    "disconnected": ("#9ca3af", "○", "disconnected"),
    "connecting": ("#eab308", "●", "connecting..."),
    "reading": ("#4ade80", "●", "reading..."),
    "clearing": ("#4ade80", "●", "clearing codes..."),
    "connected": ("#16a34a", "●", "connected"),
    "error": ("#ef4444", "●", "error"),
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


class ConnectingScreen(ModalScreen):
    """A modal shown while the K-line session is being established.

    Purely a status overlay: it does *not* own the connect (the app's ``ecu``
    worker does). It shows the target port and the protocol currently being
    probed (the auto-sweep tries several in turn — see ``set_probing``), with a
    standard ``LoadingIndicator`` spinner. Cancel (or escape) calls back into
    the app. The background connect can't be interrupted mid-handshake — it's
    blocked in serial I/O — so "cancel" means *stop waiting on it*: the app
    drops the modal, restores the UI, and tears the session down once the
    handshake finally returns.
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    ConnectingScreen {
        align: center middle;
    }
    #dialog {
        width: 56;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #title { width: 100%; content-align: center middle; text-style: bold; }
    #detail { width: 100%; content-align: center middle; margin-top: 1; }
    #spinner { height: 1; margin: 1 0; }
    #buttons { width: 100%; height: auto; align: center middle; }
    """

    def __init__(
        self,
        on_cancel: Callable[[], None],
        port: str,
    ):
        super().__init__()
        self._on_cancel = on_cancel
        self._port = port
        self._protocol = ""

    def compose(self) -> ComposeResult:
        with Middle(id="dialog"):
            yield Label(f"Connecting to ECU via {self._port}", id="title")
            yield Static(self._detail(), id="detail")
            yield LoadingIndicator(id="spinner")
            with Center(id="buttons"):
                yield Button("Cancel", variant="primary", id="cancel")

    def _detail(self) -> str:
        if not self._protocol:
            return "Probing protocol..."
        return f"Probing {self._protocol} protocol..."

    def set_probing(self, protocol: str) -> None:
        """Update the 'probing' line as the auto-sweep moves between protocols."""
        self._protocol = protocol
        try:
            self.query_one("#detail", Static).update(self._detail())
        except Exception:
            pass  # modal already dismissed

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._on_cancel()

    def action_cancel(self) -> None:
        self._on_cancel()


class ConnectErrorScreen(ModalScreen):
    """Shown when a *fresh* connect fails: the error text + an OK button.

    Dismissing it (OK or escape) hands the app back to the port picker — or the
    ready state when no port lister is configured — the same fallback a connect
    Cancel uses (see :meth:`TrecuApp._on_connect_error_ack`). The modal only
    reports the failure; the app owns what happens next via the dismiss callback.
    """

    BINDINGS = [("escape", "ok", "OK")]

    DEFAULT_CSS = """
    ConnectErrorScreen {
        align: center middle;
    }
    #dialog {
        width: 60;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        border: thick $error;
        background: $surface;
    }
    #title {
        width: 100%;
        content-align: center middle;
        text-style: bold;
        color: $error;
        margin-bottom: 1;
    }
    #message { width: 100%; content-align: center middle; margin-bottom: 1; }
    #buttons { width: 100%; height: auto; align: center middle; }
    """

    def __init__(self, message: str):
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Middle(id="dialog"):
            yield Label("Connection failed", id="title")
            yield Static(self._message, id="message")
            with Center(id="buttons"):
                yield Button("OK", variant="primary", id="ok")

    def on_mount(self) -> None:
        self.query_one("#ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_ok(self) -> None:
        self.dismiss()


class TrecuApp(App):
    """Read and decode Triumph ECU fault codes."""

    TITLE = "TrECU"
    SUB_TITLE = "Triumph ECU fault-code reader"

    CSS = """
    #spine {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    #brand { width: auto; }
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
    /* Faults tab turns red when the last read found stored codes (replaces
       the old spine MIL lamp). Keep the active-tab underline visible. */
    Tab.-has-faults { color: $error; text-style: bold; }
    #dtcs { height: 1fr; }
    #dtcs > .datatable--cursor { background: $accent; }
    #live { height: 1fr; }
    #live > .datatable--cursor { background: $accent; }
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
        Binding("space", "toggle_freeze", "Freeze"),
        # TabbedContent's own left/right bindings switch tabs but are hidden
        # (show=False). Re-declare them at app level with priority so they win
        # the binding chain and appear in the footer.
        Binding("left", "prev_tab", "Prev tab", priority=True),
        Binding("right", "next_tab", "Next tab", priority=True),
        Binding("q", "quit", "Quit"),
    ]

    # On tab switch, focus the tab's primary control so keyboard input (row
    # cursor, scroll) lands where the user is looking. Dashboard has no such
    # control and is omitted (focus is left alone there).
    _TAB_FOCUS = {
        "tab-faults": "#dtcs",
        "tab-live": "#live",
        "tab-log": "#log",
    }

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
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
        pids: Optional[PidDatabase] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        live_pids: Optional[List[int]] = None,
    ):
        super().__init__()
        self._transport_factory = transport_factory
        self._config = config
        self._db = db or DtcDatabase.load_default()
        self._pids = pids or PidDatabase.load_default()
        self._mock = mock
        self._protocol = protocol
        self._verbose = verbose
        self._keepalive_interval = keepalive_interval
        self._poll_interval = poll_interval
        # None = let the service pick the source-appropriate default set (OBD
        # DEFAULT_LIVE_PIDS, or every kwp_local channel on the KWP path).
        self._live_pids = list(live_pids) if live_pids is not None else None
        self._port = port or ("mock ECU" if mock else None)
        # Used only when no port is known yet and the user must choose one.
        self._list_ports = list_ports
        self._transport_for_port = transport_for_port
        self._state = "disconnected"
        self._last: Optional[ReadResult] = None
        # F1: one long-lived session, connected once and held open with a
        # keepalive ticker — reused across reads/clears instead of the old
        # connect-per-keypress model. Built lazily on the first read.
        self._session: Optional[DiagnosticService] = None
        # The "connecting..." spinner modal shown during a *fresh* connect, the
        # in-flight service (so Cancel can force it closed), and a flag set when
        # the user cancels it (see _connect_with_modal).
        self._connecting_screen: Optional[ConnectingScreen] = None
        self._connecting_service: Optional[DiagnosticService] = None
        self._cancelled_connect = False
        # Phase 3 live streaming: a paused poll timer (started on entering the
        # Live Data tab), a re-entrancy guard, per-PID running stats + history,
        # and a manual freeze toggle. `_streaming` drives the spine label.
        self._live_timer = None
        self._live_busy = False
        self._live_frozen = False
        self._streaming = False
        self._live_stats: dict = {}

    # -- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Horizontal(id="spine"):
            brand = Text.assemble(
                ("TrECU", "bold"), (f" v{__version__}", "dim")
            )
            yield Static(brand, id="brand")
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
            with TabPane("Live Data", id="tab-live"):
                yield DataTable(id="live", zebra_stripes=True)
            with TabPane("Log", id="tab-log"):
                yield RichLog(id="log", markup=False, wrap=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#dtcs", DataTable)
        table.cursor_type = "row"
        table.add_columns("Code", "Status", "Description")
        live = self.query_one("#live", DataTable)
        live.cursor_type = "row"
        live.add_columns("Sensor", "Value", "Unit", "Min", "Max", "Trend")
        # Phase 3 poll loop: a repeating timer, created paused and resumed only
        # while the Live Data tab is active (see _sync_live_polling). It kicks a
        # background reader rather than touching the wire on the event loop.
        self._live_timer = self.set_interval(
            self._poll_interval, self._poll_live, pause=True
        )
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
        self._set_state("disconnected")
        if self._verbose:
            self.action_show_tab("tab-log")
        if self._transport_factory is not None:
            mode = "MOCK ECU (no hardware)" if self._mock else "serial K-line"
            self._append_log(f"TrECU ready — {mode} mode. Press 'r' to read.")
            # Defer until the TabbedContent has finished re-parenting its panes,
            # so the worker's _populate can find #dtcs (mirrors _choose_port).
            self.call_after_refresh(self.action_read)
        else:
            # No definite port yet — ask the user to choose one.
            self._append_log("Multiple/no serial ports — choose one to begin.")
            self._set_state("disconnected")
            self.call_after_refresh(self._choose_port)

    # -- spine (persistent session status) -----------------------------------
    def _set_state(self, state: str) -> None:
        self._state = state
        self._refresh_spine()

    def _refresh_spine(self) -> None:
        color, dot, label = _SPINE.get(self._state, _SPINE["disconnected"])
        # While the poll loop is running, the spine reports what the session is
        # actually doing: streaming (bright green) or frozen (blue).
        if self._streaming and self._state == "connected":
            color = "#3b82f6" if self._live_frozen else "#4ade80"
            label = "frozen" if self._live_frozen else "streaming..."
        conn = f"[{color}]{dot}[/] [b]{label}[/]"
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
        # Switching *to* Live Data retasks the ECU to streaming; leaving it
        # pauses the poll loop (the half-duplex "active view is what the session
        # is doing" model — see the TUI section of ROADMAP.md).
        self._sync_live_polling()
        self._focus_active_tab()

    def _focus_active_tab(self) -> None:
        """Focus the active tab's primary control (see ``_TAB_FOCUS``).

        No-ops when a modal is on top (don't steal its focus) or when the target
        is hidden — e.g. the Faults table gives way to the "no faults" empty
        state, which isn't focusable, so focus is simply left where it is.
        """
        if self.screen is not self.screen_stack[0]:
            return  # a modal owns focus right now
        try:
            active = self.query_one(TabbedContent).active
        except Exception:
            return
        selector = self._TAB_FOCUS.get(active)
        if selector is None:
            return
        try:
            widget = self.query_one(selector)
        except Exception:
            return  # pane content not mounted yet
        if widget.display:
            widget.focus()

    def check_action(self, action: str, parameters: tuple) -> Optional[bool]:
        """Gate Read/Clear to the tabs where they make sense (hide elsewhere).

        Read populates the Dashboard and Fault Codes views; Clear acts on the
        fault list and belongs only to Fault Codes. Returning ``False`` (not
        ``None``, which would merely dim it) removes the binding from the footer
        and makes the key inert on other tabs.
        """
        if action in ("read", "clear", "toggle_freeze"):
            try:
                active = self.query_one(TabbedContent).active
            except Exception:
                return True
            if action == "read":
                return active in ("tab-dashboard", "tab-faults")
            if action == "clear":
                return active == "tab-faults"
            return active == "tab-live"  # freeze only makes sense while streaming
        return True

    # -- persistent session (F1) ---------------------------------------------
    # These run on the worker thread (via asyncio.to_thread). The session is
    # built once on first use, then reused: a re-read or clear reuses the open
    # connection rather than re-initialising the K-line every keypress.
    def _ensure_session(self) -> DiagnosticService:
        if self._session is None:
            svc = DiagnosticService(
                self._transport_factory(),
                self._config,
                self._db,
                self._logger,
                protocol=self._protocol,
                pids=self._pids,
                progress=self._on_connect_probe,
            )
            svc.start_session(self._keepalive_interval)
            self._session = svc
        return self._session

    def _close_session(self) -> None:
        """Tear the session down (stop keepalive, stop_communication, close)."""
        svc, self._session = self._session, None
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass

    def _session_read(self) -> ReadResult:
        return self._ensure_session().read_faults()

    def _session_clear(self) -> None:
        self._ensure_session().clear_faults()

    def _session_read_live(self) -> List[SensorReading]:
        return self._ensure_session().read_live(self._live_pids)

    # -- live streaming (Phase 3) --------------------------------------------
    def _sync_live_polling(self) -> None:
        """Resume the poll loop only while the Live Data tab is active."""
        if self._live_timer is None:
            return
        try:
            active = self.query_one(TabbedContent).active
        except Exception:
            return
        if active == "tab-live" and self._transport_factory is not None:
            self._live_frozen = False
            self._reset_live_table()
            self._live_timer.resume()
        else:
            self._live_timer.pause()
            if self._streaming:
                self._streaming = False
                self._refresh_spine()

    def _reset_live_table(self) -> None:
        """Clear the table + per-PID history for a fresh streaming session."""
        self._live_stats = {}
        self.query_one("#live", DataTable).clear()

    def action_toggle_freeze(self) -> None:
        """Pause/resume the live stream in place (keeps the last snapshot)."""
        self._live_frozen = not self._live_frozen
        self._append_log("live stream " + ("frozen" if self._live_frozen else "resumed"))
        self._refresh_spine()

    def _poll_live(self) -> None:
        """Timer tick: kick a background live read unless one is in flight."""
        if self._live_busy or self._live_frozen:
            return
        if self._transport_factory is None:
            return
        try:
            if self.query_one(TabbedContent).active != "tab-live":
                return
        except Exception:
            return
        # Claim the guard synchronously so a second tick can't also launch a
        # reader before the worker starts (the event loop is single-threaded).
        self._live_busy = True
        self._do_poll_live()

    @work(group="live")
    async def _do_poll_live(self) -> None:
        if self._session is None:
            self._set_state("connecting")
        try:
            readings = await asyncio.to_thread(self._session_read_live)
        except Exception as exc:  # transport/protocol errors surface here
            self._live_busy = False
            await asyncio.to_thread(self._close_session)  # reconnect on next entry
            self._on_error(exc)
            return
        self._live_busy = False
        # A poll can outlive the Live Data tab (it blocks on the shared I/O lock
        # behind a read/keepalive). If the user has since left the tab,
        # _sync_live_polling already cleared _streaming — don't resurrect it.
        try:
            still_live = self.query_one(TabbedContent).active == "tab-live"
        except Exception:
            still_live = False
        if not still_live:
            return
        self._update_live_table(readings)
        self._streaming = True
        if self._state != "connected":
            self._set_state("connected")
        else:
            self._refresh_spine()

    def _update_live_table(self, readings: List[SensorReading]) -> None:
        table = self.query_one("#live", DataTable)
        table.clear()  # keeps columns; re-add the (few) rows each snapshot
        for r in readings:
            st = self._live_stats.get(r.pid)
            if st is None:
                st = {"min": r.value, "max": r.value, "hist": deque(maxlen=_HISTORY)}
                self._live_stats[r.pid] = st
            st["min"] = min(st["min"], r.value)
            st["max"] = max(st["max"], r.value)
            st["hist"].append(r.value)
            table.add_row(
                r.name,
                r.formatted(),
                r.unit,
                _fmt_value(st["min"]),
                _fmt_value(st["max"]),
                _sparkline(st["hist"]),
                key=str(r.pid),
            )

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
        self._mark_faults_tab(bool(result.count))
        self._update_faults_card(result)
        self._refresh_connection_card()
        self._update_identity_card(result)
        self._set_state("connected")
        proto = f" via {result.protocol}" if result.protocol else ""
        noun = "code" if result.count == 1 else "codes"
        self._append_log(f"read complete: {result.count} fault {noun}{proto}")

    def _mark_faults_tab(self, has_faults: bool) -> None:
        """Tint the Faults tab red when the last read found stored codes.

        Replaces the old spine MIL lamp: the tab itself is the fault indicator.
        Styled via the ``-has-faults`` class on the tab (see CSS).
        """
        try:
            tab = self.query_one(TabbedContent).get_tab("tab-faults")
        except Exception:
            return  # tabs not mounted yet
        tab.set_class(has_faults, "-has-faults")

    def _update_faults_card(self, result: ReadResult) -> None:
        if not result.count:
            text = "[green]✓  No stored fault codes[/]\n\n[dim]Nothing reported by the ECU.[/]"
        else:
            # A red MIL dot flags stored faults; the count itself stays neutral.
            noun = "code" if result.count == 1 else "codes"
            lines = [f"[red]●[/]  [b]{result.count}[/] stored fault {noun}", ""]
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
            # Cancelling the picker means "no port" — and the app can't do
            # anything without one, so quit (identically whether the picker
            # appeared at startup or after a connect was cancelled).
            self._append_log("No port selected — exiting.")
            self._exit_no_port()
            return
        if self._transport_for_port is None:
            self._append_log("[error] cannot build a transport for the chosen port")
            return
        self._port = device
        self._transport_factory = lambda: self._transport_for_port(device)
        self._refresh_connection_card()
        self._append_log(f"using port {device}")
        self.action_read()

    def _exit_no_port(self) -> None:
        """Quit the app after the port picker was cancelled.

        If the picker came up *after* a connect was cancelled, the ``ecu``
        connect worker may still be suspended on a serial thread that can't be
        interrupted (a 5-baud init waveform runs to completion). ``App.exit()``
        would wait on that worker, leaving the app visibly running on the main
        window for seconds. So cancel the worker and force the in-flight / held
        transport closed first, then exit — the same clean quit as at startup.
        """
        self.workers.cancel_all()
        svc, self._connecting_service = self._connecting_service, None
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass
        self._close_session()
        self.exit()

    # -- connecting modal ----------------------------------------------------
    def _show_connecting(self) -> None:
        scr = ConnectingScreen(self._request_cancel_connect, self._port or "—")
        self._connecting_screen = scr
        self.push_screen(scr)

    def _on_connect_probe(self, protocol: str) -> None:
        """Service connect-progress hook (runs on the worker thread).

        Marshals the protocol label the auto-sweep is about to try onto the UI
        thread, so the modal reflects which one is being probed right now.
        """
        self.call_from_thread(self._update_connecting_protocol, protocol)

    def _update_connecting_protocol(self, protocol: str) -> None:
        scr = self._connecting_screen
        if scr is not None:
            scr.set_probing(protocol)

    def _dismiss_connecting(self) -> None:
        scr, self._connecting_screen = self._connecting_screen, None
        if scr is not None and scr in self.screen_stack:
            try:
                scr.dismiss()
            except Exception:
                pass

    def _request_cancel_connect(self) -> None:
        """Modal Cancel: abandon the in-flight connect and hand back at once.

        The connect thread can't be interrupted cleanly, but it *is* blocked in
        serial I/O — so we force its transport closed to unblock that read and
        release the port, then drop the modal and hand straight back to the
        **port picker** (if a port lister is configured) or the ready state.
        Doing this here — rather than waiting for the thread to unwind, which on
        a slow ``auto`` init sweep can be many seconds — is what makes Cancel
        feel instant. The now-doomed connect finishes into a closed transport
        and is discarded by :meth:`_connect_with_modal` (``_cancelled_connect``);
        because we never publish its service as ``_session``, a re-picked (even
        different) port still gets a clean, non-overlapping session.
        """
        if self._cancelled_connect:
            return
        self._cancelled_connect = True
        self._dismiss_connecting()
        self._append_log("connect cancelled")
        svc = self._connecting_service
        if svc is not None:
            try:
                svc.close()  # unblock the connect thread's read; release the port
            except Exception:
                pass
        if self._list_ports is not None:
            self._choose_port()
        else:
            self._set_state("disconnected")

    def _do_connect(self, svc: DiagnosticService) -> None:
        """Worker-thread body: open + connect ``svc`` (blocking)."""
        svc.start_session(self._keepalive_interval)

    async def _connect_with_modal(self) -> bool:
        """Establish a fresh session behind a cancelable spinner modal.

        Returns ``True`` once connected, ``False`` if the user cancelled or the
        connect failed (the error is surfaced in that case). Runs on the event
        loop from the ``ecu`` worker; the blocking connect is off-thread so the
        Cancel button stays responsive. The service is built *here* (on the UI
        thread) rather than in the worker so Cancel has a handle to force it
        closed — see :meth:`_request_cancel_connect`.
        """
        self._set_state("connecting")
        self._cancelled_connect = False
        self._show_connecting()
        svc = DiagnosticService(
            self._transport_factory(),
            self._config,
            self._db,
            self._logger,
            protocol=self._protocol,
            pids=self._pids,
            progress=self._on_connect_probe,
        )
        self._connecting_service = svc
        error: Optional[Exception] = None
        try:
            await asyncio.to_thread(self._do_connect, svc)
        except Exception as exc:  # transport/protocol errors surface here
            error = exc
        self._connecting_service = None
        self._dismiss_connecting()
        if self._cancelled_connect:
            # Cancel already dropped the modal, closed `svc`, and handed back to
            # the picker. Ensure the (doomed) session is closed and unpublished.
            await asyncio.to_thread(svc.close)
            self._session = None
            return False
        if error is not None:
            await asyncio.to_thread(svc.close)  # reconnect on next read
            self._on_connect_error(error)
            return False
        self._session = svc  # publish only once fully connected
        return True

    def _on_connect_error(self, exc: Exception) -> None:
        """A *fresh* connect failed: log it, then show a modal with the error
        and an OK button that hands back to the port picker (or the ready state
        when no port lister is configured — the same fallback a connect Cancel
        uses). Unlike :meth:`_on_error` (read/clear/live failures over an
        established session, which just surface in the Log), a connect failure
        blocks the whole session, so it gets a dismissable modal that routes the
        user back to choosing a port.
        """
        self._append_log(f"[error] {exc}")
        self._set_state("error")
        self.bell()
        self.push_screen(ConnectErrorScreen(str(exc)), self._on_connect_error_ack)

    def _on_connect_error_ack(self, _result=None) -> None:
        """Error modal dismissed: return to port selection (or the ready state
        when no port lister is configured)."""
        if self._list_ports is not None:
            self._choose_port()
        else:
            self._set_state("disconnected")

    # -- actions -------------------------------------------------------------
    @work(exclusive=True, group="ecu")
    async def action_read(self) -> None:
        if self._transport_factory is None:
            self._choose_port()
            return
        # A fresh connect runs behind a cancelable "connecting..." spinner modal;
        # a re-read over the held session skips it and is just "reading...".
        if self._session is None:
            if not await self._connect_with_modal():
                return  # cancelled, or connect failed (already surfaced)
        self._set_state("reading")
        try:
            result = await asyncio.to_thread(self._session_read)
        except Exception as exc:  # transport/protocol errors surface here
            await asyncio.to_thread(self._close_session)  # reconnect on next read
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
            await asyncio.to_thread(self._session_clear)
        except Exception as exc:
            await asyncio.to_thread(self._close_session)  # reconnect on next read
            self._on_error(exc)
            return
        self._append_log("fault codes cleared; re-reading...")
        self.action_read()

    def _on_error(self, exc: Exception) -> None:
        self._append_log(f"[error] {exc}")
        self._set_state("error")
        self.action_show_tab("tab-log")
        self.bell()

    def on_unmount(self) -> None:
        # Close the held session on exit: stop the keepalive ticker, send
        # stop_communication, and release the port.
        self._close_session()
