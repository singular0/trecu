"""Small level-aware logging boundary shared by the CLI, TUI, and protocols."""

from __future__ import annotations

from enum import IntEnum
from typing import Callable, Optional, Union


class LogLevel(IntEnum):
    DEBUG = 10
    INFO = 20
    WARNING = 30
    ERROR = 40


LogHandler = Callable[[str], None]
LoggerLike = Union["Logger", LogHandler]


class Logger:
    """Send messages to a UI-provided handler, filtering debug by verbosity.

    The project deliberately owns presentation (stderr in headless mode and a
    RichLog in the TUI), so this is a lightweight adapter rather than global
    ``logging`` configuration. Warning/error prefixes let either presentation
    style those levels without changing the existing one-argument callback API.
    """

    def __init__(
        self, handler: Optional[LogHandler] = None, *, verbose: bool = False
    ) -> None:
        self._handler = handler or (lambda _message: None)
        self.verbose = verbose

    def log(self, level: LogLevel, message: str) -> None:
        if level == LogLevel.DEBUG and not self.verbose:
            return
        prefix = {
            LogLevel.WARNING: "[warning] ",
            LogLevel.ERROR: "[error] ",
        }.get(level, "")
        self._handler(prefix + message)

    def debug(self, message: str) -> None:
        self.log(LogLevel.DEBUG, message)

    def info(self, message: str) -> None:
        self.log(LogLevel.INFO, message)

    def warning(self, message: str) -> None:
        self.log(LogLevel.WARNING, message)

    def error(self, message: str) -> None:
        self.log(LogLevel.ERROR, message)


def as_logger(logger: Optional[LoggerLike], *, verbose: bool = False) -> Logger:
    """Return ``logger`` as a :class:`Logger`, preserving an existing instance."""
    if isinstance(logger, Logger):
        return logger
    return Logger(logger, verbose=verbose)
