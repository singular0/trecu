"""High-level diagnostic service used by both the CLI and the TUI.

Supports three protocol paths and an ``auto`` mode that tries them in order
(the same sweep other K-line Triumph tools walk):

* ``iso9141`` — 5-baud slow init at 0x33 + OBD-II (confirmed on real Triumphs)
* ``kwp-slow`` — KWP2000 with 5-baud init at the ECU address 0xD5 (the
  Keihin K-line fallback)
* ``kwp-fast`` — KWP2000 fast-init + StartCommunication
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Iterator, List, Optional

from .protocol.dtc import Dtc, DtcDatabase
from .protocol.iso9141 import Iso9141Client, Iso9141Config
from .protocol.pids import FormulaError, KwpLocalTable, PidDatabase, SensorReading
from .protocol.kwp2000 import (
    ConnectionInfo,
    EcuInfo,
    Kwp2000Client,
    Kwp2000Config,
    ProtocolError,
)
from .transport.base import Transport, TransportError

Logger = Callable[[str], None]

PROTOCOL_ISO9141 = "iso9141"
PROTOCOL_KWP_SLOW = "kwp-slow"
PROTOCOL_KWP_FAST = "kwp-fast"
PROTOCOL_AUTO = "auto"
# Order tried in auto mode: ISO 9141 first (the confirmed Triumph path), then
# KWP slow init (the Keihin K-line fallback), then KWP fast init.
_AUTO_ORDER = (PROTOCOL_ISO9141, PROTOCOL_KWP_SLOW, PROTOCOL_KWP_FAST)

# Keepalive cadence for a persistent session. Both protocols' idle timeout
# (KWP2000 P3max, ISO 9141-2) is ~5 s, so beat comfortably under that.
DEFAULT_KEEPALIVE_INTERVAL = 2.0

# Phase 3 live streaming. The default poll set is the roadmap's core dashboard
# sensors; the cadence is the poll loop's target interval (the TUI's own timer).
# RPM, coolant, throttle, MAP, O2 sensor 1, battery voltage.
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
        self.beats = 0

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        # Event.wait doubles as the sleep and the stop signal: it returns True
        # the instant stop() is called, so join() below is near-immediate.
        while not self._stop.wait(self._interval):
            try:
                self._beat()
                self.beats += 1
            except Exception as exc:  # noqa: BLE001 - keepalive is best-effort
                self._log(f"keepalive failed: {exc}")

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)


@dataclass
class ReadResult:
    key_bytes: bytes
    dtcs: List[Dtc] = field(default_factory=list)
    session_started: bool = False
    protocol: str = ""
    ecu_info: Optional[EcuInfo] = None

    @property
    def count(self) -> int:
        return len(self.dtcs)


class DiagnosticService:
    """Owns the transport + protocol client + code database lifecycle."""

    def __init__(
        self,
        transport: Transport,
        config: Optional[object] = None,
        db: Optional[DtcDatabase] = None,
        logger: Optional[Logger] = None,
        protocol: str = PROTOCOL_AUTO,
        client: Optional[object] = None,
        pids: Optional[PidDatabase] = None,
        kwp_local: Optional[KwpLocalTable] = None,
        progress: Optional[Logger] = None,
    ):
        self.transport = transport
        self.config = config
        self.db = db or DtcDatabase.load_default()
        self.pids = pids or PidDatabase.load_default()
        # The KWP/Keihin packed-frame channel table (its own data file); used
        # only on the ``kwp_local`` live path, but loaded up front like ``pids``.
        self.kwp_local = kwp_local or KwpLocalTable.load_default()
        self._logger = logger or (lambda _m: None)
        # Connect-progress hook: called with each protocol label *before* it's
        # probed, so a UI can show which one the auto-sweep is currently trying.
        self._progress = progress or (lambda _p: None)
        self.protocol = protocol
        self._explicit_client = client
        self._active = None            # connected client
        self._active_info: Optional[ConnectionInfo] = None
        self._active_proto = ""
        self._ecu_info: Optional[EcuInfo] = None  # cached for the session
        # Serializes every exchange on the half-duplex K-line: real operations
        # and the keepalive ticker all take this before touching the wire.
        self._io_lock = threading.Lock()
        self._keepalive: Optional[_Keepalive] = None

    # -- lifecycle -----------------------------------------------------------
    def open(self) -> None:
        self.transport.open()

    def close(self) -> None:
        self._stop_keepalive()
        try:
            if self._active is not None:
                self._active.stop_communication()
        except Exception:
            pass
        finally:
            self.transport.close()

    # -- persistent session (F1) --------------------------------------------
    def start_session(
        self, keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL
    ) -> "DiagnosticService":
        """Open the transport, connect once, and start the keepalive ticker.

        This is the persistent-session lifecycle: unlike a one-shot ``open()``
        + ``read_faults()``, the connection is established up front and held
        open, with a background ticker sending ``TesterPresent`` (KWP) / a cheap
        OBD poke (ISO 9141) every ``keepalive_interval`` seconds so the ECU
        doesn't drop the session while idle. Pass ``0`` to disable the ticker
        (e.g. one-shot use). Pair with :meth:`close`, or use :meth:`session`.
        """
        self.open()
        try:
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
            fn = getattr(client, "keepalive", None)
            if fn is not None:
                fn()

    # -- client construction / connect --------------------------------------
    def _candidate_protocols(self) -> List[str]:
        if self._explicit_client is not None:
            return [""]
        if self.protocol == PROTOCOL_AUTO:
            return list(_AUTO_ORDER)
        return [self.protocol]

    def _build_client(self, proto: str):
        if self._explicit_client is not None:
            return self._explicit_client
        if proto == PROTOCOL_ISO9141:
            cfg = self.config if isinstance(self.config, Iso9141Config) else Iso9141Config()
            return Iso9141Client(self.transport, cfg, self._logger)
        cfg = self.config if isinstance(self.config, Kwp2000Config) else Kwp2000Config()
        # One base config serves both KWP variants (e.g. in auto mode); pin the
        # init style to the protocol actually being attempted.
        want = "slow" if proto == PROTOCOL_KWP_SLOW else "fast"
        if cfg.init_mode != want:
            cfg = replace(cfg, init_mode=want)
        return Kwp2000Client(self.transport, cfg, self._logger)

    def _connect(self):
        if self._active is not None:
            return self._active
        errors: List[str] = []
        for proto in self._candidate_protocols():
            client = self._build_client(proto)
            label = proto or client.__class__.__name__
            self._progress(label)
            try:
                info = client.connect()
            except (ProtocolError, TransportError) as exc:
                errors.append(f"{label}: {exc}")
                self._logger(f"[{label}] connect failed: {exc}")
                continue
            self._active = client
            self._active_info = info
            self._active_proto = proto or client.__class__.__name__
            self._logger(f"connected via {self._active_proto}")
            return client
        raise ProtocolError("could not connect: " + "; ".join(errors))

    # -- operations ----------------------------------------------------------
    # Public operations take ``_io_lock`` so they can't interleave with the
    # keepalive ticker (or each other) on the single-wire K-line. Private
    # helpers (_connect, _read_identification) run under that held lock and must
    # not re-acquire it.
    def read_faults(self) -> ReadResult:
        with self._io_lock:
            client = self._connect()
            ecu_info = self._read_identification(client)
            triples = client.read_dtcs()
            dtcs = self.db.decode_all(triples, family=client.dtc_family)
            info = self._active_info
            return ReadResult(
                key_bytes=info.key_bytes if info else b"",
                dtcs=dtcs,
                session_started=info.session_started if info else False,
                protocol=self._active_proto,
                ecu_info=ecu_info if ecu_info and not ecu_info.is_empty else None,
            )

    def read_identification(self) -> Optional[EcuInfo]:
        """Read (and cache) ECU identity for the active session."""
        with self._io_lock:
            return self._read_identification(self._connect())

    def _read_identification(self, client) -> Optional[EcuInfo]:
        """Best-effort identity read, cached so re-reads don't re-query the ECU."""
        if self._ecu_info is not None:
            return self._ecu_info
        fn = getattr(client, "read_identification", None)
        if fn is None:
            return None
        try:
            self._ecu_info = fn()
        except (ProtocolError, TransportError) as exc:
            self._logger(f"identification skipped: {exc}")
            self._ecu_info = EcuInfo()
        return self._ecu_info

    def clear_faults(self) -> None:
        with self._io_lock:
            client = self._connect()
            client.clear_dtcs()

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

        What one snapshot *is* depends on the client's ``live_source``:

        * ``obd_mode01`` (iso9141) — one Mode 01 request per PID; ``pids=None``
          uses :data:`DEFAULT_LIVE_PIDS`. A PID the ECU didn't answer — or the
          table can't decode — is dropped.
        * ``kwp_local`` (KWP / Keihin) — a single request for the packed
          multi-channel frame (LID ``0x80``), split per the ``kwp_local``
          channel table; here ``pids`` are ``kwp_local`` channel indices and ``None``
          means every channel in the table.
        """
        requested = None if pids is None else list(pids)
        with self._io_lock:
            client = self._connect()
            read = getattr(client, "read_live", None)
            if read is None:
                return []
            kwp_table = None
            if getattr(client, "live_source", "obd_mode01") == "kwp_local":
                kwp_table = self.kwp_local
                if kwp_table is None:
                    return []
                frame = read([kwp_table.lid]).get(kwp_table.lid)
            else:
                if requested is None:
                    requested = list(DEFAULT_LIVE_PIDS)
                raw = read(requested)
        if kwp_table is not None:
            return [] if frame is None else kwp_table.decode_frame(frame, requested)
        readings: List[SensorReading] = []
        for pid in requested:
            data = raw.get(pid)
            if data is None or pid not in self.pids:
                continue
            try:
                readings.append(self.pids.decode(pid, data))
            except (KeyError, FormulaError) as exc:
                self._logger(f"live decode skipped for 0x{pid:02X}: {exc}")
        return readings

    def __enter__(self) -> "DiagnosticService":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
