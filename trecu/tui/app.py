"""Textual application: connect, read, decode, and clear Triumph fault codes.

The UI is a persistent *session* with a title bar over a set of tabbed views:
a **Dashboard** (faults + ECU identity cards), the **Fault Codes** table, and
the raw protocol **Log**. ``TODO.md`` tracks the remaining live-data controls
and throttle-sync view.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Callable, List, Optional

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from .. import __version__
from ..protocol.dtc import DtcDatabase
from ..protocol.iso9141 import Iso9141Config
from ..protocol.pids import PidDatabase
from ..service import (
    DEFAULT_KEEPALIVE_INTERVAL,
    DEFAULT_POLL_INTERVAL,
    PROTOCOL_ISO9141,
    ReadResult,
)
from ..transport.base import Transport
from .live_table import LiveTable
from .port_select import PortSelectScreen
from .screens import ConfirmScreen, ConnectErrorScreen, ConnectingScreen
from .session import ConnectOutcome, SessionController, TransportFactory

PortLister = Callable[[], list]
TransportForPort = Callable[[str], Transport]

# Session state -> (dot colour, glyph, label) for the title bar.
_CONNECTION_STATES = {
    "disconnected": ("#9ca3af", "○", "disconnected"),
    "connecting": ("#eab308", "●", "connecting..."),
    "reading": ("#4ade80", "●", "reading..."),
    "clearing": ("#4ade80", "●", "clearing codes..."),
    "connected": ("#16a34a", "●", "connected"),
    "error": ("#ef4444", "●", "error"),
}


class TrecuApp(App):
    """Read and decode Triumph ECU fault codes."""

    TITLE = "TrECU"
    SUB_TITLE = "disconnected"

    # No command palette: trecu's whole surface is the two-key footer, and the
    # Ctrl+P palette only adds Textual's stock actions (screenshot, theme, …)
    # that don't belong in a diagnostics tool.
    ENABLE_COMMAND_PALETTE = False

    CSS = """
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
    /* The focusable lead card (Dashboard's landing spot) shows the accent
       border when it holds focus, mirroring the datatable cursor cue. */
    .card:focus { border: round $accent; }
    /* Faults tab turns red when the last read found stored codes (replaces
       the old spine MIL lamp). Keep the active-tab underline visible. */
    Tab.-has-faults { color: $error; text-style: bold; }
    #dtcs { height: 1fr; }
    #dtcs > .datatable--cursor { background: $accent; }
    /* The Live Data table styles itself — see LiveTable.DEFAULT_CSS. */
    #log { height: 1fr; background: $surface-darken-1; }
    """

    # Footer order is BINDINGS order (filtered per-tab by check_action): arrows
    # first, then the contextual commands, then quit — a stable left-to-right
    # shape on every tab even as the middle commands come and go.
    BINDINGS = [
        # TabbedContent's own left/right bindings switch tabs but are hidden
        # (show=False). Re-declare them at app level with priority so they win
        # the binding chain and appear in the footer.
        Binding("left", "prev_tab", "Prev tab", priority=True),
        Binding("right", "next_tab", "Next tab", priority=True),
        Binding("r", "read", "Read"),
        Binding("c", "clear", "Clear"),
        Binding("space", "toggle_freeze", "Freeze"),
        Binding("q", "quit", "Quit"),
    ]

    # On tab switch, focus the tab's primary control so keyboard input (row
    # cursor, scroll) lands where the user is looking. Dashboard has no natural
    # cursor widget, so its lead summary card is made focusable (see on_mount)
    # to give the tab a landing spot like the others. The Faults tab always shows
    # its DTC table (empty, headers only, when there are no codes) — never hidden
    # — so focus always has somewhere to land inside the pane.
    _TAB_FOCUS = {
        "tab-dashboard": "#card-faults",
        "tab-faults": "#dtcs",
        "tab-live": "#live",
        "tab-log": "#log",
    }

    def __init__(
        self,
        transport_factory: Optional[TransportFactory] = None,
        config: Optional[Iso9141Config] = None,
        db: Optional[DtcDatabase] = None,
        mock: bool = False,
        port: Optional[str] = None,
        list_ports: Optional[PortLister] = None,
        transport_for_port: Optional[TransportForPort] = None,
        verbose: bool = False,
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
        pids: Optional[PidDatabase] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        live_pids: Optional[List[int]] = None,
    ):
        super().__init__()
        self._db = db or DtcDatabase.load_default()
        self._pids = pids or PidDatabase.load_default()
        self._mock = mock
        self._poll_interval = poll_interval
        # F1: one long-lived session, connected once and held open with a
        # keepalive ticker — reused across reads/clears/live polls instead of
        # the old connect-per-keypress model. The controller owns that session
        # and the single connect/cancel path both Read and the live-poll loop
        # go through (see tui/session.py).
        self._ecu = SessionController(
            transport_factory=transport_factory,
            config=config,
            db=self._db,
            pids=self._pids,
            logger=self._ecu_logger,
            verbose=verbose,
            keepalive_interval=keepalive_interval,
        )
        # None = let the service use its own DEFAULT_LIVE_PIDS.
        self._live_pids = list(live_pids) if live_pids is not None else None
        self._port = port or ("mock ECU" if mock else None)
        # Used only when no port is known yet and the user must choose one.
        self._list_ports = list_ports
        self._transport_for_port = transport_for_port
        self._state = "disconnected"
        self._last: Optional[ReadResult] = None
        # The modals raised around a *fresh* connect: the "connecting..."
        # spinner and the failure dialog. Tracked so a second caller sharing
        # the same attempt can't stack a duplicate on top.
        self._connecting_screen: Optional[ConnectingScreen] = None
        self._connect_error_screen: Optional[ConnectErrorScreen] = None
        # Phase 3 live streaming: a paused poll timer (started on entering the
        # Live Data tab), a running flag + re-entrancy guard, and a manual
        # freeze toggle. `_streaming` drives the title-bar label; the rows,
        # per-sensor stats, and sparklines belong to the LiveTable widget.
        self._live_timer = None
        self._live_running = False
        self._live_busy = False
        self._live_frozen = False
        self._streaming = False

    def format_title(self, title: str, sub_title: str) -> Text:
        """Render the app identity and live connection state in the title bar."""
        color, dot, label = _CONNECTION_STATES.get(
            self._state, _CONNECTION_STATES["disconnected"]
        )
        if self._streaming and self._state == "connected":
            color = "#3b82f6" if self._live_frozen else "#4ade80"
            label = "frozen" if self._live_frozen else "streaming..."
        return Text.assemble(
            (title, "bold"),
            (f" v{__version__}", "dim"),
            (" — ", "dim"),
            (dot, color),
            (f" {label}", "bold"),
        )

    # -- layout --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False, icon="")
        with TabbedContent(initial="tab-dashboard"):
            with TabPane("Dashboard", id="tab-dashboard"):
                with Horizontal(id="dashboard"):
                    yield Static(id="card-faults", classes="card")
                    yield Static(id="card-connection", classes="card")
                    yield Static(id="card-identity", classes="card")
            with TabPane("Faults", id="tab-faults"):
                yield DataTable(id="dtcs", zebra_stripes=True)
            with TabPane("Live Data", id="tab-live"):
                yield LiveTable(id="live")
            with TabPane("Log", id="tab-log"):
                yield RichLog(id="log", markup=False, wrap=True, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#dtcs", DataTable)
        table.cursor_type = "row"
        table.add_columns("Code", "Status", "Description")
        # Phase 3 poll loop: a repeating timer, created paused and resumed only
        # while the Live Data tab is active (see _sync_live_polling). It kicks a
        # background reader rather than touching the wire on the event loop.
        self._live_timer = self.set_interval(
            self._poll_interval, self._poll_live, pause=True
        )
        # Static isn't focusable by default; opt every card in so Tab can step
        # across them. The lead card is the tab's focus landing spot (_TAB_FOCUS).
        for card in self.query(".card").results(Static):
            card.can_focus = True
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
        if self._ecu.can_connect:
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

    # -- title bar (persistent session status) -------------------------------
    def _set_state(self, state: str) -> None:
        self._state = state
        self._refresh_titlebar()

    def _refresh_titlebar(self) -> None:
        _, _, label = _CONNECTION_STATES.get(
            self._state, _CONNECTION_STATES["disconnected"]
        )
        if self._streaming and self._state == "connected":
            label = "frozen" if self._live_frozen else "streaming..."
        # Header watches App.sub_title, so assigning the displayed state asks
        # the native title bar to call format_title and redraw itself.
        self.sub_title = label

    # -- helpers -------------------------------------------------------------
    def _append_log(self, msg: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        # Level prefixes come from the shared logger; operational messages
        # written directly by the TUI remain unstyled.
        if msg.startswith("[error]"):
            style = "bold red"
        elif msg.startswith("[warning]"):
            style = "yellow"
        else:
            style = ""
        line = Text.assemble((f"{stamp}  ", "dim"), (msg, style))
        self.query_one("#log", RichLog).write(line)

    def _ecu_logger(self, msg: str) -> None:
        """Visible ECU logger — invoked from the worker thread."""
        self.call_from_thread(self._append_log, msg)

    def action_show_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def _step_tab(self, delta: int) -> None:
        # The ←/→ bindings are app-level priority=True, so they still fire while a
        # modal (Confirm/Connecting/ConnectError) owns the screen — switching tabs
        # behind the dialog. Ignore them until the modal is dismissed.
        if self.screen is not self.screen_stack[0]:
            return
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
        # is doing" model).
        self._sync_live_polling()
        self._focus_active_tab()

    def _focus_active_tab(self) -> None:
        """Focus the active tab's primary control (``_TAB_FOCUS``).

        Landing focus inside the newly active pane keeps the row cursor / scroll
        where the user is looking. No-ops when a modal owns focus.
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
            self.query_one(selector).focus()
        except Exception:
            pass  # pane content not mounted yet

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

    # -- live streaming (Phase 3) --------------------------------------------
    def _sync_live_polling(self) -> None:
        """Run the poll loop exactly while the Live Data tab is active.

        Called on every tab switch and after a successful read, so a stream that
        was stopped by a failed/cancelled connect starts again once the session
        is back and the user is still looking at Live Data.
        """
        if self._live_timer is None:
            return
        try:
            active = self.query_one(TabbedContent).active
        except Exception:
            return
        if active == "tab-live" and self._ecu.can_connect:
            self._start_live_polling()
        else:
            self._stop_live_polling()

    def _start_live_polling(self) -> None:
        # Idempotent: only a real stopped -> running transition resets the
        # table, so re-syncing mid-stream doesn't throw away the history.
        if self._live_running:
            return
        self._live_running = True
        self._live_frozen = False
        self.query_one("#live", LiveTable).reset()
        self._live_timer.resume()

    def _stop_live_polling(self) -> None:
        if not self._live_running:
            return
        self._live_running = False
        self._live_timer.pause()
        if self._streaming:
            self._streaming = False
            self._refresh_titlebar()

    def action_toggle_freeze(self) -> None:
        """Pause/resume the live stream in place (keeps the last snapshot)."""
        self._live_frozen = not self._live_frozen
        self._append_log("live stream " + ("frozen" if self._live_frozen else "resumed"))
        self._refresh_titlebar()

    def _poll_live(self) -> None:
        """Timer tick: kick a background live read unless one is in flight."""
        if self._live_busy or self._live_frozen:
            return
        if not self._ecu.can_connect:
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
        try:
            # Entering Live Data while disconnected connects through the *same*
            # path as Read — cancelable spinner modal, port-picker fallback —
            # rather than silently blocking the poll on a fresh handshake.
            if not self._ecu.connected:
                if not await self._connect_with_modal():
                    # Cancelled, or failed (already surfaced). Stop the loop
                    # instead of re-attempting every tick behind the picker /
                    # error modal; a later successful read restarts it.
                    self._stop_live_polling()
                    return
            readings = await asyncio.to_thread(self._ecu.read_live, self._live_pids)
        except Exception as exc:  # transport/protocol errors surface here
            await asyncio.to_thread(self._ecu.close)  # reconnect on next entry
            self._on_error(exc)
            return
        finally:
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
        self.query_one("#live", LiveTable).update_readings(readings)
        self._streaming = True
        if self._state != "connected":
            self._set_state("connected")
        else:
            self._refresh_titlebar()

    # -- rendering results ---------------------------------------------------
    def _populate(self, result: ReadResult) -> None:
        self._last = result
        table = self.query_one("#dtcs", DataTable)
        table.clear()
        for dtc in result.dtcs:
            table.add_row(*dtc.as_row())
        # The DTC table stays visible even with no codes (headers only) — the
        # "no faults" state lives on the Dashboard's Faults card, not a separate
        # widget swap. The Faults *tab* still tints red when codes are present.
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
            # An empty circle (no colour) signals a clean read.
            text = "○  No stored fault codes"
        else:
            # A red MIL dot flags stored faults; the count itself stays neutral.
            noun = "code" if result.count == 1 else "codes"
            # Codes go on one comma-separated line that wraps when there are many.
            codes = ", ".join(f"[b]{dtc.code}[/]" for dtc in result.dtcs)
            text = f"[red]●[/]  [b]{result.count}[/] stored fault {noun}\n\n{codes}"
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
        proto = (self._last.protocol if self._last else "") or PROTOCOL_ISO9141
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
        self._ecu.transport_factory = lambda: self._transport_for_port(device)
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
        self._ecu.shutdown()
        self.exit()

    # -- connecting modal ----------------------------------------------------
    def _show_connecting(self) -> None:
        # Idempotent: concurrent callers (a Read and a live poll) share one
        # connect attempt, so only the first raises the spinner.
        if self._connecting_screen is not None:
            return
        scr = ConnectingScreen(self._request_cancel_connect, self._port or "—")
        self._connecting_screen = scr
        self.push_screen(scr)

    def _dismiss_connecting(self) -> None:
        scr, self._connecting_screen = self._connecting_screen, None
        if scr is not None and scr in self.screen_stack:
            try:
                scr.dismiss()
            except Exception:
                pass

    def _request_cancel_connect(self) -> None:
        """Modal Cancel: abandon the in-flight connect and hand back at once.

        :meth:`SessionController.cancel` force-closes the in-flight transport so
        the connect thread's blocked read unwinds; here we drop the modal and
        hand straight back to the **port picker** (if a port lister is
        configured) or the ready state, *without* waiting for that thread — a
        5-baud init working through its retry budget can take many seconds,
        which is what would make Cancel feel dead. The doomed connect is discarded by the
        controller, which never publishes it as the session, so a re-picked
        (even different) port still gets a clean, non-overlapping session.
        """
        if not self._ecu.cancel():
            return
        self._dismiss_connecting()
        self._append_log("connect cancelled")
        # Leaving the connecting modal always leaves the connecting state too.
        # In particular, keep the title bar accurate while the port picker is
        # shown after cancelling a real serial-port connection attempt.
        self._set_state("disconnected")
        if self._list_ports is not None:
            # ConnectingScreen calls us from its own button handler. Pushing
            # the picker synchronously there makes Textual attach the picker's
            # result callback to that soon-to-be-dismissed screen. The picker
            # still closes, but its callback is then dropped — notably when
            # both dialogs are cancelled with their buttons. Queue the push on
            # the App's message pump so the result callback remains live.
            self.call_later(self._choose_port)

    def _begin_connect(self) -> None:
        """Controller hook: an attempt is starting — show it as such."""
        self._set_state("connecting")
        self._show_connecting()

    async def _connect_with_modal(self) -> bool:
        """Establish a fresh session behind a cancelable spinner modal.

        The one connect path for the whole TUI: both ``action_read`` and the
        live-poll loop come through here. Returns ``True`` once connected,
        ``False`` if the user cancelled or the connect failed (the error is
        surfaced in that case). Runs on the event loop from a worker; the
        blocking connect is off-thread inside the controller so the Cancel
        button stays responsive.
        """
        result = await self._ecu.connect(on_start=self._begin_connect)
        self._dismiss_connecting()
        if result.outcome is ConnectOutcome.CANCELLED:
            return False  # cancel already restored the UI and routed onward
        if result.outcome is ConnectOutcome.FAILED:
            self._on_connect_error(result.error)
            return False
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
        # Idempotent for the same reason as _show_connecting: callers sharing
        # one attempt each see the failure, but it's one dialog.
        if self._connect_error_screen is not None:
            return
        self._append_log(f"[error] {exc}")
        self._set_state("error")
        self.bell()
        scr = ConnectErrorScreen(str(exc))
        self._connect_error_screen = scr
        self.push_screen(scr, self._on_connect_error_ack)

    def _on_connect_error_ack(self, _result=None) -> None:
        """Error modal dismissed: return to port selection (or the ready state
        when no port lister is configured)."""
        self._connect_error_screen = None
        if self._list_ports is not None:
            self._choose_port()
        else:
            self._set_state("disconnected")

    # -- actions -------------------------------------------------------------
    @work(exclusive=True, group="ecu")
    async def action_read(self) -> None:
        if not self._ecu.can_connect:
            self._choose_port()
            return
        # A fresh connect runs behind a cancelable "connecting..." spinner modal;
        # a re-read over the held session skips it and is just "reading...".
        if not self._ecu.connected:
            if not await self._connect_with_modal():
                return  # cancelled, or connect failed (already surfaced)
        self._set_state("reading")
        try:
            result = await asyncio.to_thread(self._ecu.read_faults)
        except Exception as exc:  # transport/protocol errors surface here
            await asyncio.to_thread(self._ecu.close)  # reconnect on next read
            self._on_error(exc)
            return
        self._populate(result)
        # A session is up again: restart the stream if the user is sitting on
        # Live Data (e.g. this read followed a cancelled connect there).
        self._sync_live_polling()

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
            await asyncio.to_thread(self._ecu.clear_faults)
        except Exception as exc:
            await asyncio.to_thread(self._ecu.close)  # reconnect on next read
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
        self._ecu.close()
