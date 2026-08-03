"""High-level diagnostic service used by both the CLI and the TUI.

One protocol path: ``iso9141`` — a 5-baud slow init at 0x33 followed by
standard OBD-II, the endpoint confirmed on a real Triumph.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable, FrozenSet, Iterable, Iterator, List, Optional, Union

from .logging import Logger, LoggerLike, as_logger
from .protocol.common import ConnectionInfo, EcuInfo, ProtocolError
from .protocol.dtc import Dtc, DtcDatabase
from .protocol.iso9141 import Iso9141Client, Iso9141Config
from .protocol.pids import FormulaError, PidDatabase, SensorReading
from .transport.base import Transport, TransportError

#: The one protocol TrECU speaks; reported back on a connected session.
PROTOCOL_ISO9141 = "iso9141"

# Keepalive cadence for a persistent session. The ISO 9141-2 idle timeout is
# ~5 s, so beat comfortably under that.
DEFAULT_KEEPALIVE_INTERVAL = 2.0

# Live streaming. The default poll set is the core dashboard
# sensors; the cadence is the poll loop's target interval (the TUI's own timer).
# RPM, coolant, throttle, MAP, O2 sensor 1, battery voltage.
#
# It is a *wish list*, not a promise: every request is filtered through the
# session's advertised capability set, so on the tested Triumph — whose bitmap
# does not claim PID 42 — battery voltage is silently dropped from the plan
# instead of costing a timeout every poll, while an ECU that does advertise it
# still gets it polled.
DEFAULT_LIVE_PIDS = (0x0C, 0x05, 0x11, 0x0B, 0x14, 0x42)
DEFAULT_POLL_INTERVAL = 0.5


class _Keepalive:
    """Background ticker that beats the ECU on an interval to hold a session.

    Each beat runs the supplied ``beat`` callable, which the service wraps in
    its I/O lock — so a keepalive never overlaps a real read/clear on the
    half-duplex K-line. Beats are best-effort: a failure is logged and the loop
    keeps ticking; the next real operation surfaces a hard error.
    """

    def __init__(self, beat: Callable[[], None], interval: float, logger: Logger):
        self._beat = beat
        self._interval = interval
        self._log = logger
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="trecu-keepalive", daemon=True
        )
        # The count is written by the ticker thread and read from whichever
        # thread asks, so both ends go through this lock — `+= 1` on an int
        # attribute is not atomic.
        self._count_lock = threading.Lock()
        self._beats = 0

    @property
    def beats(self) -> int:
        """Completed beats so far; safe to read from any thread."""
        with self._count_lock:
            return self._beats

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        # Event.wait doubles as the sleep and the stop signal: it returns True
        # the instant stop() is called, so join() below is near-immediate.
        while not self._stop.wait(self._interval):
            try:
                self._beat()
            except Exception as exc:  # noqa: BLE001 - keepalive is best-effort
                self._log.warning(f"keepalive failed: {exc}")
            else:
                with self._count_lock:
                    self._beats += 1

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)


#: How a service gets its device: a factory it calls once per session.
TransportFactory = Callable[[], Transport]
#: What callers may hand to ``DiagnosticService(transport)`` — see
#: :func:`as_transport_factory` for what passing an instance means.
TransportSource = Union[Transport, TransportFactory]


def as_transport_factory(transport: TransportSource) -> TransportFactory:
    """Normalize a transport *or* a transport factory into a factory.

    A factory is the service's own shape: :meth:`DiagnosticService.close`
    releases the device, so the next :meth:`~DiagnosticService.open` builds a
    fresh one and reconnecting is an operation the service performs rather than
    something the caller has to rebuild the service for.

    Passing a **transport instance** binds that one device for the service's
    whole life: every session reopens the same object. That is what a caller
    wants when the device carries state across sessions — the ``--mock`` TUI
    shares one simulated ECU so cleared codes stay cleared, like a real bike.
    """
    if isinstance(transport, Transport):
        return lambda: transport
    if callable(transport):
        return transport
    raise TypeError(f"unsupported transport type: {type(transport).__name__}")


@dataclass
class ReadResult:
    key_bytes: bytes
    dtcs: List[Dtc] = field(default_factory=list)
    session_started: bool = False
    protocol: str = ""
    ecu_info: Optional[EcuInfo] = None
    #: PIDs the ECU advertised for this session, or ``None`` when it never
    #: reported them. Carried on the result so a UI can show what the session is
    #: capable of without taking the I/O lock to ask again.
    supported_pids: Optional[FrozenSet[int]] = None

    @property
    def count(self) -> int:
        return len(self.dtcs)


@dataclass(frozen=True)
class PidStatus:
    """What one live-data PID is, in the three states that must not be conflated.

    * ``advertised`` — the ECU's Mode 01 capability bitmap claims it. ``None``
      when the ECU never reported a usable bitmap: capability *unknown*, which
      is not the same as "unsupported".
    * ``decodable`` — TrECU's sensor table can turn its bytes into a value.
      An advertised PID TrECU cannot decode is still readable as raw data.
    * ``answered`` — it actually replied when asked; ``None`` means it was never
      asked. Data of ``00``/``FF`` is an *answer* (``raw`` holds the bytes);
      absence of a reply is the only thing that makes this False.

    They are independent: a PID can be advertised and undecodable, decodable and
    unadvertised, or advertised and silent — and each of those is a different
    fact about the session.
    """

    pid: int
    name: str
    unit: str = ""
    advertised: Optional[bool] = None
    decodable: bool = False
    answered: Optional[bool] = None
    raw: bytes = b""

    @property
    def polled(self) -> bool:
        """Whether a live poll would request this PID.

        Unknown capability polls: with no bitmap to filter on, an unfiltered
        request is the only honest move.
        """
        return self.advertised is not False and self.decodable


class DiagnosticService:
    """Owns the transport + protocol client + code database lifecycle.

    The device is held as a **factory**, not as an instance: one transport is
    built per session and released on :meth:`close`, so ``close()`` followed by
    :meth:`open` / :meth:`start_session` reconnects over a *fresh* device
    without the caller rebuilding the service. Handing a transport instance
    instead pins that one device for every session (see
    :func:`as_transport_factory`).
    """

    def __init__(
        self,
        transport: TransportSource,
        config: Optional[Iso9141Config] = None,
        db: Optional[DtcDatabase] = None,
        logger: Optional[LoggerLike] = None,
        client: Optional[Iso9141Client] = None,
        pids: Optional[PidDatabase] = None,
        verbose: bool = False,
    ):
        self._transport_factory = as_transport_factory(transport)
        # The device for the current session: built on first use, dropped by
        # close() so the next session starts from a fresh one.
        self._transport: Optional[Transport] = None
        self.config = config or Iso9141Config()
        self.db = db or DtcDatabase.load_default()
        self.pids = pids or PidDatabase.load_default()
        self._logger = as_logger(logger, verbose=verbose)
        self._explicit_client = client
        self._active: Optional[Iso9141Client] = None   # connected client
        self._active_info: Optional[ConnectionInfo] = None
        self._active_proto = ""
        self._ecu_info: Optional[EcuInfo] = None  # cached for the session
        # Serializes every exchange on the half-duplex K-line: real operations
        # and the keepalive ticker all take this before touching the wire.
        self._io_lock = threading.Lock()
        self._keepalive: Optional[_Keepalive] = None

    @property
    def active_protocol(self) -> str:
        """Protocol selected for the current connection, if connected."""
        return self._active_proto

    @property
    def transport(self) -> Optional[Transport]:
        """The device held for the current session (``None`` once closed)."""
        return self._transport

    @property
    def supported_pids(self) -> Optional[FrozenSet[int]]:
        """PIDs the connected ECU advertises; ``None`` when that isn't known.

        Reads the connected client's session cache — no wire traffic, and no
        connect: before a session exists the answer is simply unknown. ``None``
        never means "supports nothing"; that would be an empty set.
        """
        return self._active.supported_pids if self._active is not None else None

    # -- lifecycle -----------------------------------------------------------
    def _device(self) -> Transport:
        """This session's transport, built from the factory on first use."""
        if self._transport is None:
            self._transport = self._transport_factory()
        return self._transport

    def _release_device(self) -> None:
        """Close the current device and forget it, so the next open is fresh."""
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def open(self) -> None:
        self._device().open()

    def _clear_active(self) -> None:
        """Forget all state that belongs to the current transport session."""
        self._active = None
        self._active_info = None
        self._active_proto = ""
        self._ecu_info = None

    def close(self, *, force: bool = False) -> None:
        """End the ECU session and release the transport.

        The device is dropped along with it, so a later :meth:`open` /
        :meth:`start_session` builds a fresh one from the factory — that is what
        makes reconnecting a service operation rather than a rebuild.

        Ordinary close is serialized with reads, clears, and keepalives. The
        wire-level stop is best-effort so an unsupported service cannot prevent
        the serial port being released.

        ``force=True`` is reserved for cancelling a blocked connection attempt:
        it closes the transport without waiting for the I/O lock, allowing the
        blocked read to unwind.
        """
        self._stop_keepalive()
        if force:
            self._clear_active()
            self._release_device()
            return

        with self._io_lock:
            client = self._active
            try:
                if client is not None:
                    try:
                        client.stop_communication()
                    except Exception as exc:  # best-effort shutdown
                        self._logger.warning(f"stop-communication ignored: {exc}")
            finally:
                self._clear_active()
                self._release_device()

    # -- persistent session (F1) --------------------------------------------
    def start_session(
        self, keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL
    ) -> "DiagnosticService":
        """Open the transport, connect once, and start the keepalive ticker.

        This is the persistent-session lifecycle: unlike a one-shot ``open()``
        + ``read_faults()``, the connection is established up front and held
        open, with a background ticker sending a cheap OBD poke every
        ``keepalive_interval`` seconds so the ECU doesn't drop the session while
        idle. Pass ``0`` to disable the ticker (e.g. one-shot use). Pair with
        :meth:`close`, or use :meth:`session`.
        """
        self.open()
        try:
            with self._io_lock:
                self._connect()
        except Exception:
            self.close()
            raise
        self._start_keepalive(keepalive_interval)
        return self

    @contextmanager
    def session(
        self, keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL
    ) -> Iterator["DiagnosticService"]:
        """Context manager wrapping :meth:`start_session` + :meth:`close`.

        Yields the service; call ``read_faults`` / ``clear_faults`` /
        ``read_identification`` on it as usual — the connection persists and
        the keepalive ticker runs for the life of the ``with`` block.
        """
        self.start_session(keepalive_interval)
        try:
            yield self
        finally:
            self.close()

    def _start_keepalive(self, interval: float) -> None:
        if not interval or interval <= 0:
            self._keepalive = None
            return
        self._keepalive = _Keepalive(self._keepalive_beat, interval, self._logger)
        self._keepalive.start()

    def _stop_keepalive(self) -> None:
        ka, self._keepalive = self._keepalive, None
        if ka is not None:
            ka.stop()

    def _keepalive_beat(self) -> None:
        """One keepalive exchange, serialized against real operations."""
        with self._io_lock:
            client = self._active
            if client is None:
                return
            client.keepalive()

    # -- client construction / connect --------------------------------------
    def _build_client(self) -> Iso9141Client:
        """The session's client: an injected one, or a fresh ISO 9141 client."""
        if self._explicit_client is not None:
            return self._explicit_client
        return Iso9141Client(self._device(), self.config, self._logger)

    def _connect(self) -> Iso9141Client:
        if self._active is not None:
            return self._active
        client = self._build_client()
        label = (
            PROTOCOL_ISO9141
            if self._explicit_client is None
            else client.__class__.__name__
        )
        self._logger.debug(f"connecting to ECU with protocol {label}")
        try:
            info = client.connect()
        except (ProtocolError, TransportError) as exc:
            self._logger.debug(f"[{label}] connect failed: {exc}")
            raise ProtocolError(f"could not connect: {label}: {exc}") from exc
        self._active = client
        self._active_info = info
        self._active_proto = label
        self._logger.debug(f"connected via {label}")
        return client

    # -- operations ----------------------------------------------------------
    # Public operations take ``_io_lock`` so they can't interleave with the
    # keepalive ticker (or each other) on the single-wire K-line. Private
    # helpers (_connect, _read_identification) run under that held lock and must
    # not re-acquire it.
    def read_faults(self) -> ReadResult:
        with self._io_lock:
            client = self._connect()
            self._logger.debug("ECU operation: reading identification and fault codes")
            ecu_info = self._read_identification(client)
            triples = client.read_dtcs()
            dtcs = self.db.decode_all(triples)
            self._logger.debug(f"ECU operation complete: decoded {len(dtcs)} fault(s)")
            info = self._active_info
            return ReadResult(
                key_bytes=info.key_bytes if info else b"",
                dtcs=dtcs,
                session_started=info.session_started if info else False,
                protocol=self._active_proto,
                ecu_info=ecu_info if ecu_info and not ecu_info.is_empty else None,
                supported_pids=client.supported_pids,
            )

    def read_identification(self) -> Optional[EcuInfo]:
        """Read (and cache) ECU identity for the active session."""
        with self._io_lock:
            return self._read_identification(self._connect())

    def _read_identification(self, client: Iso9141Client) -> Optional[EcuInfo]:
        """Best-effort identity read, cached so re-reads don't re-query the ECU."""
        if self._ecu_info is not None:
            self._logger.debug("ECU identification: using session cache")
            return self._ecu_info
        try:
            self._logger.debug("ECU operation: reading identification")
            self._ecu_info = client.read_identification()
        except (ProtocolError, TransportError) as exc:
            self._logger.warning(f"identification skipped: {exc}")
            self._ecu_info = EcuInfo()
        return self._ecu_info

    def clear_faults(self) -> None:
        with self._io_lock:
            client = self._connect()
            self._logger.debug("ECU operation: clearing fault codes")
            client.clear_dtcs()
            self._logger.debug("ECU operation complete: fault codes cleared")

    def live_plan(self, pids: Optional[Iterable[int]] = None) -> List[int]:
        """The PIDs a live poll would actually put on the wire, in order.

        The intersection of three independent things: what the caller asked for,
        what the ECU advertised (:attr:`supported_pids` — the client's filter,
        skipped entirely when capability is unknown), and what TrECU's sensor
        table can decode. Connects lazily like any other operation, because the
        advertised half of the answer only exists once there is a session.
        """
        requested = list(DEFAULT_LIVE_PIDS) if pids is None else list(pids)
        with self._io_lock:
            return self._plan(self._connect(), requested)

    def _plan(self, client: Iso9141Client, requested: List[int]) -> List[int]:
        """``requested`` narrowed to advertised (client) then decodable (table).

        Each layer applies the rule it alone owns: the client knows what the ECU
        claims, this knows what the sensor table understands. Runs under a held
        ``_io_lock``; it costs no traffic (the capability set is cached).
        """
        advertised = client.live_plan(requested)
        plan = [pid for pid in advertised if pid in self.pids]
        dropped = [pid for pid in advertised if pid not in self.pids]
        if dropped:
            self._logger.debug(
                "live plan drops "
                + " ".join(f"{pid:02X}" for pid in dropped)
                + ": no decoder for them in this build"
            )
        return plan

    def read_live(
        self, pids: Optional[Iterable[int]] = None
    ) -> List[SensorReading]:
        """Read one live-data snapshot (Phase 3): the requested PIDs' values.

        Reads one snapshot over the active session (connecting lazily if
        needed), serialized on the half-duplex ``_io_lock`` like every other
        operation, then decodes the raw bytes into :class:`SensorReading`s
        ordered as requested. Decoding runs outside the lock (it's pure
        computation, no wire traffic). Meant to be called repeatedly by the
        TUI's poll loop.

        One snapshot is one OBD Mode 01 request per PID in :meth:`live_plan` —
        so an unadvertised or undecodable PID costs no request and no timeout.
        ``pids=None`` uses :data:`DEFAULT_LIVE_PIDS`. A planned PID the ECU
        didn't answer this time is dropped from the snapshot, not from the plan.
        """
        requested = list(DEFAULT_LIVE_PIDS) if pids is None else list(pids)
        with self._io_lock:
            client = self._connect()
            plan = self._plan(client, requested)
            self._logger.debug(
                f"ECU operation: reading live data ({len(plan)} PID(s))"
            )
            raw = client.read_live(plan)
        readings: List[SensorReading] = []
        for pid in plan:
            data = raw.get(pid)
            if data is None:
                continue
            try:
                readings.append(self.pids.decode(pid, data))
            except (KeyError, FormulaError) as exc:
                self._logger.warning(f"live decode skipped for 0x{pid:02X}: {exc}")
        self._logger.debug(
            f"ECU live-data operation complete: {len(readings)} reading(s)"
        )
        return readings

    def pid_capabilities(self, probe: bool = False) -> List[PidStatus]:
        """Every PID this session knows of, in its three independent states.

        The union of what the ECU advertises and what TrECU can decode, ascending
        by PID — so an advertised PID with no local decoder and a decodable PID
        the ECU never claimed are both visible, each labelled for what it is.

        ``probe=True`` additionally asks the ECU for its advertised PIDs once and
        fills in ``answered`` + the raw bytes, which is the only way to tell an
        advertised PID that really replies from one that stays silent. Nothing
        outside the advertised set is requested, so an unsupported PID costs
        nothing; an advertised PID that never answers costs one timeout, which is
        precisely what establishes that it is silent.
        """
        with self._io_lock:
            client = self._connect()
            advertised = client.supported_pids
            answers = (
                client.read_live(sorted(advertised)) if probe and advertised else {}
            )
        known = set(advertised) if advertised is not None else set()
        out: List[PidStatus] = []
        for pid in sorted(known | set(self.pids.pids())):
            definition = self.pids.get(pid)
            is_advertised = None if advertised is None else pid in advertised
            out.append(
                PidStatus(
                    pid=pid,
                    name=definition.name if definition else f"PID 0x{pid:02X}",
                    unit=definition.unit if definition else "",
                    advertised=is_advertised,
                    decodable=definition is not None,
                    answered=(pid in answers) if (probe and is_advertised) else None,
                    raw=answers.get(pid, b""),
                )
            )
        return out

    def __enter__(self) -> "DiagnosticService":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
