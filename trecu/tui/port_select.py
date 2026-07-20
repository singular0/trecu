"""Modal screen that lets the user pick a serial port for the K-line cable."""

from __future__ import annotations

from typing import Callable, List, Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option

PortLister = Callable[[], List[dict]]


class PortSelectScreen(ModalScreen[Optional[str]]):
    """Choose a serial port; dismisses with the device path (or None if cancelled)."""

    DEFAULT_CSS = """
    PortSelectScreen {
        align: center middle;
    }
    #picker {
        width: 80;
        max-width: 90%;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #title { text-style: bold; width: 100%; margin-bottom: 1; }
    #ports { height: auto; max-height: 16; border: round $primary; }
    #hint { color: $text-muted; width: 100%; margin-top: 1; }
    """

    BINDINGS = [
        Binding("r", "rescan", "Rescan ports"),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, list_ports: PortLister):
        super().__init__()
        self._list_ports = list_ports

    def compose(self) -> ComposeResult:
        with Container(id="picker"):
            yield Label("Select the K-line serial port", id="title")
            yield OptionList(id="ports")
            yield Label("↑/↓ move · Enter select · r rescan · Esc cancel", id="hint")

    def on_mount(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        ports = self._list_ports()
        option_list = self.query_one("#ports", OptionList)
        option_list.clear_options()

        if not ports:
            option_list.add_option(
                Option("No serial ports found — connect the cable and press 'r'", disabled=True)
            )
            return

        # KKL/FTDI candidates first, then by device name.
        ports = sorted(ports, key=lambda p: (not p.get("likely_kkl"), p["device"]))
        for p in ports:
            vid, pid = p.get("vid"), p.get("pid")
            vidpid = f"{vid:04x}:{pid:04x}" if vid and pid else ""
            marker = "  ★ likely KKL cable" if p.get("likely_kkl") else ""
            desc = p.get("description") or ""
            label = f"{p['device']}   {vidpid}  {desc}{marker}".rstrip()
            option_list.add_option(Option(label, id=p["device"]))

        option_list.focus()
        option_list.highlighted = 0

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        device = event.option.id
        if device:  # ignore the disabled placeholder (no id)
            self.dismiss(device)

    def action_rescan(self) -> None:
        self._refresh()

    def action_cancel(self) -> None:
        self.dismiss(None)
