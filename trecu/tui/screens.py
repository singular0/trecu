"""Modal screens for the TUI: clear confirmation, connect spinner, connect error.

Each one is a pure dialog — it renders a question or a status and reports the
outcome back to the app (by ``dismiss``, or by a callback for the ones the app
must keep updating). None of them own an ECU operation: the session lives in
:mod:`trecu.tui.session` and the workers driving it live in
:mod:`trecu.tui.app`. The port picker is next door in
:mod:`trecu.tui.port_select`.
"""

from __future__ import annotations

from typing import Callable

from textual.app import ComposeResult
from textual.containers import Center, Horizontal, Middle
from textual.screen import ModalScreen
from textual.widgets import Button, Label, LoadingIndicator, Static


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
            with Horizontal(id="buttons"):
                yield Button("Yes, clear", variant="error", id="yes")
                yield Button("Cancel", variant="primary", id="no")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        self.dismiss(False)


#: What the connecting modal says under the port name. Static, because there is
#: one protocol to try — the 5-baud init's retries all happen behind this line.
_CONNECT_DETAIL = "ISO 9141-2 · 5-baud init..."


class ConnectingScreen(ModalScreen):
    """A modal shown while the K-line session is being established.

    Purely a status overlay: it does *not* own the connect (the app's ``ecu``
    worker does). It names the target port and the one protocol TrECU speaks,
    with a standard ``LoadingIndicator`` spinner. Cancel (or escape) calls back
    into the app. The background connect can't be interrupted mid-handshake — it's
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

    def compose(self) -> ComposeResult:
        with Middle(id="dialog"):
            yield Label(f"Connecting to ECU via {self._port}", id="title")
            yield Static(_CONNECT_DETAIL, id="detail")
            yield LoadingIndicator(id="spinner")
            with Center(id="buttons"):
                yield Button("Cancel", variant="primary", id="cancel")

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
    Cancel uses (see ``TrecuApp._on_connect_error_ack``). The modal only
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
