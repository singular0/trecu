"""High-level diagnostic service used by both the CLI and the TUI.

Supports two protocol paths and an ``auto`` mode that tries them in order:

* ``iso9141`` — 5-baud slow init + OBD-II (confirmed on real Triumphs)
* ``kwp-fast`` — KWP2000 fast-init + ReadDTCByStatus
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .protocol.dtc import Dtc, DtcDatabase
from .protocol.iso9141 import Iso9141Client, Iso9141Config
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
PROTOCOL_KWP_FAST = "kwp-fast"
PROTOCOL_AUTO = "auto"
# Order tried in auto mode: ISO 9141 first (the confirmed Triumph path).
_AUTO_ORDER = (PROTOCOL_ISO9141, PROTOCOL_KWP_FAST)


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
    ):
        self.transport = transport
        self.config = config
        self.db = db or DtcDatabase.load_default()
        self._logger = logger or (lambda _m: None)
        self.protocol = protocol
        self._explicit_client = client
        self._active = None            # connected client
        self._active_info: Optional[ConnectionInfo] = None
        self._active_proto = ""
        self._ecu_info: Optional[EcuInfo] = None  # cached for the session

    # -- lifecycle -----------------------------------------------------------
    def open(self) -> None:
        self.transport.open()

    def close(self) -> None:
        try:
            if self._active is not None:
                self._active.stop_communication()
        except Exception:
            pass
        finally:
            self.transport.close()

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
        return Kwp2000Client(self.transport, cfg, self._logger)

    def _connect(self):
        if self._active is not None:
            return self._active
        errors: List[str] = []
        for proto in self._candidate_protocols():
            client = self._build_client(proto)
            label = proto or client.__class__.__name__
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
    def read_faults(self) -> ReadResult:
        client = self._connect()
        ecu_info = self._read_identification(client)
        triples = client.read_dtcs()
        dtcs = self.db.decode_all(triples)
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
        client = self._connect()
        client.clear_dtcs()

    def __enter__(self) -> "DiagnosticService":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
