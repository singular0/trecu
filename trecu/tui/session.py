"""The TUI's ECU session lifecycle: one connect path, one held session.

Deliberately **Textual-free**. This is the connect/cancel state machine the app
used to carry inline — and in two divergent copies: ``action_read`` connected
behind a cancelable spinner modal, while the live-poll loop connected silently
on its own, with no spinner, no Cancel, and no port-picker fallback. Both now go
through :meth:`SessionController.connect`, which is also the single place a
:class:`~trecu.service.DiagnosticService` is constructed for the TUI.

The protocol stack is blocking, so every wire operation here runs off the event
loop via ``asyncio.to_thread``: ``connect`` is a coroutine safe to await from a
Textual worker, and the plain (blocking) ``read_faults`` / ``clear_faults`` /
``read_live`` / ``close`` are meant to be handed to ``asyncio.to_thread`` by the
caller — the app's workers already do exactly that.

**Cancel** is the subtle part. A connect in flight is blocked in serial I/O and
cannot be interrupted cleanly, so :meth:`cancel` force-closes the in-flight
service to unblock that read and release the port, and returns *immediately* —
which is what makes Cancel feel instant on a slow ``auto`` init sweep. The
doomed connect then finishes into a closed transport and is discarded: a service
is published as :attr:`SessionController.session` **only** on full success, so a
re-picked (even different) port always gets a clean, non-overlapping session.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, List, Optional

from ..logging import LoggerLike
from ..protocol.dtc import DtcDatabase
from ..protocol.pids import PidDatabase, SensorReading
from ..service import (
    DEFAULT_KEEPALIVE_INTERVAL,
    PROTOCOL_AUTO,
    DiagnosticService,
    ReadResult,
)
from ..transport.base import Transport

TransportFactory = Callable[[], Transport]


class ConnectOutcome(Enum):
    """How a :meth:`SessionController.connect` attempt ended."""

    CONNECTED = "connected"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True)
class ConnectResult:
    """Outcome of a connect attempt, with the failure when there was one."""

    outcome: ConnectOutcome
    error: Optional[Exception] = None

    @property
    def connected(self) -> bool:
        return self.outcome is ConnectOutcome.CONNECTED


@dataclass
class _Attempt:
    """One in-flight connect: its service and *its own* cancel flag.

    Per-attempt rather than per-controller because a cancelled attempt keeps
    running — it's blocked in serial I/O — while a fresh one may already have
    started. Each must decide independently whether to publish or discard.
    """

    service: Optional[DiagnosticService] = None
    cancelled: bool = False


class SessionController:
    """Owns the one long-lived :class:`DiagnosticService` the TUI talks through.

    The session is built on the first connect, then reused: a re-read, a clear,
    or a live poll reuses the open connection rather than re-initialising the
    K-line. A failed operation is the caller's cue to :meth:`close`, so the next
    connect starts clean.
    """

    def __init__(
        self,
        *,
        transport_factory: Optional[TransportFactory] = None,
        config: Optional[object] = None,
        db: Optional[DtcDatabase] = None,
        pids: Optional[PidDatabase] = None,
        logger: Optional[LoggerLike] = None,
        protocol: str = PROTOCOL_AUTO,
        verbose: bool = False,
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
        progress: Optional[Callable[[str], None]] = None,
    ):
        # Rebound once the user picks a port (see TrecuApp._on_port_chosen);
        # None until then, which is what `can_connect` reports on.
        self.transport_factory = transport_factory
        self._config = config
        self._db = db
        self._pids = pids
        self._logger = logger
        self._protocol = protocol
        self._verbose = verbose
        self._keepalive_interval = keepalive_interval
        self._progress = progress
        # The connected service — published only once fully connected.
        self.session: Optional[DiagnosticService] = None
        # The in-flight connect: its per-attempt state (so cancel has a handle
        # on the service to force closed) and the task other callers share.
        self._current: Optional[_Attempt] = None
        self._pending: Optional["asyncio.Future[ConnectResult]"] = None

    # -- state ---------------------------------------------------------------
    @property
    def can_connect(self) -> bool:
        """True once a port (transport factory) is known."""
        return self.transport_factory is not None

    @property
    def connected(self) -> bool:
        return self.session is not None

    @property
    def connecting(self) -> bool:
        return self._pending is not None

    # -- connect / cancel ----------------------------------------------------
    def build_service(self) -> DiagnosticService:
        """Construct a service for one connect attempt (the only such place)."""
        if self.transport_factory is None:
            raise RuntimeError("no port selected")
        return DiagnosticService(
            self.transport_factory(),
            self._config,
            self._db,
            self._logger,
            protocol=self._protocol,
            pids=self._pids,
            progress=self._progress,
            verbose=self._verbose,
        )

    async def connect(
        self, on_start: Optional[Callable[[], None]] = None
    ) -> ConnectResult:
        """Establish the session if it isn't already, and report how it went.

        Returns immediately when already connected. ``on_start`` fires once, on
        the caller that actually begins an attempt, before any blocking work —
        the app uses it to raise the spinner modal. Concurrent callers (a Read
        and a live poll racing) *share* one attempt rather than opening the port
        twice, so the second caller simply awaits the first one's outcome.
        """
        if self.session is not None:
            return ConnectResult(ConnectOutcome.CONNECTED)
        pending = self._pending
        if pending is None:
            if on_start is not None:
                on_start()
            attempt = self._current = _Attempt()
            pending = self._pending = asyncio.ensure_future(self._run(attempt))
        # Shield: a caller's worker being cancelled (Textual replaces exclusive
        # workers) must not kill the attempt another caller is waiting on.
        return await asyncio.shield(pending)

    async def _run(self, attempt: _Attempt) -> ConnectResult:
        """One connect attempt: build, connect off-thread, publish or discard."""
        try:
            try:
                svc = self.build_service()
            except Exception as exc:  # no port, or the factory itself refused
                return ConnectResult(ConnectOutcome.FAILED, exc)
            attempt.service = svc
            error: Optional[Exception] = None
            try:
                await asyncio.to_thread(svc.start_session, self._keepalive_interval)
            except Exception as exc:  # transport/protocol errors surface here
                error = exc
            attempt.service = None
            if attempt.cancelled:
                # Cancel already force-closed the transport; make sure the
                # abandoned session is fully torn down, and never publish it —
                # a newer attempt may own `self.session` by now.
                await asyncio.to_thread(svc.close)
                return ConnectResult(ConnectOutcome.CANCELLED)
            if error is not None:
                await asyncio.to_thread(svc.close)  # reconnect on the next try
                return ConnectResult(ConnectOutcome.FAILED, error)
            self.session = svc
            return ConnectResult(ConnectOutcome.CONNECTED)
        finally:
            self._detach(attempt)

    def _detach(self, attempt: _Attempt) -> None:
        """Stop treating ``attempt`` as the in-flight one (if it still is)."""
        if self._current is attempt:
            self._current = None
            self._pending = None

    def cancel(self) -> bool:
        """Abandon an in-flight connect; ``False`` if there was nothing to cancel.

        Force-closes the in-flight service without waiting for the I/O lock, so
        the connect thread's blocked read unwinds and the port is released, and
        **detaches** the attempt straight away: a connect requested after a
        cancel must start fresh rather than inherit the doomed one's outcome.
        The detached attempt still runs to completion — see the module docstring
        for why it can't just be killed — and discards itself when it gets there.
        """
        attempt = self._current
        if attempt is None or attempt.cancelled:
            return False
        attempt.cancelled = True
        svc, attempt.service = attempt.service, None
        self._detach(attempt)
        if svc is not None:
            try:
                svc.close(force=True)
            except Exception:
                pass
        return True

    # -- teardown ------------------------------------------------------------
    def close(self) -> None:
        """Tear the session down (keepalive off, stop_communication, port shut).

        Blocking — call it from a thread. Safe when nothing is connected.
        """
        svc, self.session = self.session, None
        if svc is not None:
            try:
                svc.close()
            except Exception:
                pass

    def shutdown(self) -> None:
        """Drop everything for app exit: an in-flight connect *and* the session.

        The in-flight service is force-closed rather than closed politely: its
        connect thread may still be suspended mid-handshake on a serial read
        that can't be interrupted, and exit shouldn't wait on it.
        """
        self.cancel()
        self.close()

    # -- operations (blocking; run them off the event loop) ------------------
    def _require(self) -> DiagnosticService:
        svc = self.session
        if svc is None:
            raise RuntimeError("not connected to the ECU")
        return svc

    def read_faults(self) -> ReadResult:
        return self._require().read_faults()

    def clear_faults(self) -> None:
        self._require().clear_faults()

    def read_live(self, pids: Optional[Iterable[int]] = None) -> List[SensorReading]:
        return self._require().read_live(pids)
