"""Modal screen that lets the user pick a serial port for the K-line cable."""

from __future__ import annotations

from typing import Callable, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, Static

PortLister = Callable[[], List[dict]]

# Sentinel row key for the "no ports found" placeholder row; guards selection.
_EMPTY_KEY = "__empty__"


class PortSelectScreen(ModalScreen[Optional[str]]):
    """Choose a serial port; dismisses with the device path (or None if cancelled)."""

    DEFAULT_CSS = """
    PortSelectScreen {
        align: center middle;
    }
    #picker {
        width: 90;
        max-width: 95%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #title { text-style: bold; width: 100%; margin-bottom: 1; }
    #ports { height: auto; max-height: 16; border: round $primary; }
    #buttons { width: 100%; height: auto; margin-top: 1; }
    #buttons #spacer { width: 1fr; height: 1; }
    #buttons #cancel, #buttons #connect { margin-left: 2; }
    """

    BINDINGS = [
        Binding("r", "rescan", "Rescan ports"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, list_ports: PortLister):
        super().__init__()
        self._list_ports = list_ports
        self._highlighted: Optional[str] = None

    def compose(self) -> ComposeResult:
        with Container(id="picker"):
            yield Label("Select interface cable serial port (★ = likely candidate)", id="title")
            table = DataTable(id="ports", zebra_stripes=True)
            table.cursor_type = "row"
            yield table
            with Horizontal(id="buttons"):
                yield Button("Refresh list", id="refresh")
                yield Static(id="spacer")
                yield Button("Cancel", id="cancel")
                yield Button("Connect", id="connect", variant="primary")

    def on_mount(self) -> None:
        table = self.query_one("#ports", DataTable)
        table.add_columns("", "Port", "VID:PID", "Description")
        self._refresh()

    def _refresh(self) -> None:
        ports = self._list_ports()
        table = self.query_one("#ports", DataTable)
        table.clear()

        if not ports:
            self._highlighted = None
            table.add_row(
                "",
                "No serial ports found — press Refresh list once the cable is connected",
                "",
                "",
                key=_EMPTY_KEY,
            )
            return

        # KKL/FTDI candidates first, then by device name.
        ports = sorted(ports, key=lambda p: (not p.get("likely_kkl"), p["device"]))
        for p in ports:
            vid, pid = p.get("vid"), p.get("pid")
            vidpid = f"{vid:04x}:{pid:04x}" if vid and pid else ""
            marker = "★" if p.get("likely_kkl") else ""
            table.add_row(
                marker,
                p["device"],
                vidpid,
                p.get("description") or "",
                key=p["device"],
            )

        self._highlighted = ports[0]["device"]
        table.focus()
        table.move_cursor(row=0)

    def _connect(self, device: Optional[str]) -> None:
        if device and device != _EMPTY_KEY:  # ignore the placeholder row
            self.dismiss(device)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._highlighted = event.row_key.value

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._connect(event.row_key.value)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh":
            self._refresh()
        elif event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "connect":
            self._connect(self._highlighted)

    def action_rescan(self) -> None:
        self._refresh()

    def action_cancel(self) -> None:
        self.dismiss(None)
