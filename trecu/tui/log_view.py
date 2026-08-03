"""The Log tab's protocol log: follows the tail, but yields to a manual scroll.

``RichLog.auto_scroll`` is all-or-nothing.  Left on — the stock setting — every
write yanks the view back to the newest line, so scrolling up to read an earlier
frame is undone by the next log line, and the protocol logger writes constantly
(a single read emits a line per request, and live polling never stops).  Left
off, the log stops following at all.

:class:`LogView` *derives* the flag from where the view is instead: scrolling
away from the bottom turns following off, scrolling back to the bottom turns it
on again — the tail-follow behaviour of a terminal pager.  What is written and
how it is styled stays in :mod:`trecu.tui.app`.
"""

from __future__ import annotations

from textual.widgets import RichLog


class LogView(RichLog):
    """A ``RichLog`` that follows new lines only while parked at the bottom."""

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        # Every vertical scroll ends up here — wheel, keys, scrollbar drag, and
        # the scroll_end() a write performs while following — so this one test
        # decides following for all of them.  It can't fight a write: while
        # following is off a write only grows the content, which moves
        # max_scroll_y and not scroll_y, so the watcher doesn't even fire.
        self.auto_scroll = self.is_vertical_scroll_end
