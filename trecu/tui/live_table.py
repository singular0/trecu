"""The Live Data table widget: one row per sensor, updated in place.

Everything about *how* a live snapshot is displayed lives here — the columns,
the per-sensor running min/max and value history, and the trend sparkline — so
the app only hands it decoded :class:`~trecu.protocol.pids.SensorReading`\\ s
(:meth:`LiveTable.update_readings`) and tells it when a fresh streaming session
begins (:meth:`LiveTable.reset`). Polling, threading, and the session itself
stay in :mod:`trecu.tui.app` / :mod:`trecu.tui.session`.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Iterable

from textual.widgets import DataTable

from ..protocol.pids import SensorReading, format_value

# Trend sparkline: history length per sensor and the block ramp used to draw it.
_HISTORY = 24
_SPARK = "▁▂▃▄▅▆▇█"

# (label, column key, width) per column. The keys are fixed so rows can be
# updated cell-by-cell (see update_readings); a width of None auto-fits.
_COLUMNS = (
    ("Sensor", "sensor", None),
    ("Value", "value", 8),
    ("Unit", "unit", 6),
    ("Min", "min", 8),
    ("Max", "max", 8),
    ("Trend", "trend", None),
)


def sparkline(values: Iterable[float]) -> str:
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


@dataclass
class _Stats:
    """Running min/max plus the recent value history for one sensor."""

    minimum: float
    maximum: float
    history: Deque[float] = field(
        default_factory=lambda: deque(maxlen=_HISTORY)
    )

    @classmethod
    def seed(cls, value: float) -> "_Stats":
        return cls(minimum=value, maximum=value)

    def add(self, value: float) -> None:
        self.minimum = min(self.minimum, value)
        self.maximum = max(self.maximum, value)
        self.history.append(value)


class LiveTable(DataTable):
    """Streaming sensor table: sensor / value / unit / min / max / trend."""

    DEFAULT_CSS = """
    LiveTable { height: 1fr; }
    LiveTable > .datatable--cursor { background: $accent; }
    """

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("cursor_type", "row")
        kwargs.setdefault("zebra_stripes", True)
        super().__init__(**kwargs)
        # Tracks exactly the sensors already given a row: membership decides
        # add-row vs update-row, and reset() clears rows and stats together.
        self._stats: Dict[int, _Stats] = {}

    def on_mount(self) -> None:
        # The numeric columns get fixed widths so they don't jitter as values
        # change from tick to tick; the sensor name and the trend sparkline stay
        # auto-width (the name varies per ECU, and the sparkline grows toward
        # _HISTORY glyphs as history accumulates).
        for label, key, width in _COLUMNS:
            self.add_column(label, width=width, key=key)

    def reset(self) -> None:
        """Clear the rows + per-sensor history for a fresh streaming session."""
        self._stats = {}
        self.clear()

    def update_readings(self, readings: Iterable[SensorReading]) -> None:
        """Fold one live snapshot into the table.

        Rows are updated *in place* keyed by PID rather than cleared and
        rebuilt. Clearing dropped any PID the ECU skipped that snapshot (it
        reappeared only when answered again) and snapped the row cursor back to
        the top on every tick. Now a skipped PID keeps its last row, and the
        cursor stays put.
        """
        for r in readings:
            stats = self._stats.get(r.pid)
            is_new = stats is None
            if is_new:
                stats = _Stats.seed(r.value)
                self._stats[r.pid] = stats
            stats.add(r.value)
            cells = (
                r.name,
                r.formatted(),
                r.unit,
                format_value(stats.minimum),
                format_value(stats.maximum),
                sparkline(stats.history),
            )
            if is_new:
                self.add_row(*cells, key=str(r.pid))
            else:
                for (_, column_key, _w), value in zip(_COLUMNS, cells):
                    self.update_cell(
                        str(r.pid), column_key, value, update_width=True
                    )
