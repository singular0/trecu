"""Transport abstraction for the single-wire K-line.

A KKL cable exposes the ISO 9141 / ISO 14230 K-line as a plain serial port.
The line is half-duplex and single-wire, so anything the tester transmits is
electrically reflected back into its own receiver (the "echo").  Concrete
transports advertise whether they echo via :attr:`Transport.echoes`; the
protocol layer discards that echo before parsing a reply.
"""

from __future__ import annotations

import abc


class TransportError(Exception):
    """Raised for connection / IO problems at the transport level."""


class Transport(abc.ABC):
    """Abstract half-duplex byte transport with KWP2000 init timing."""

    #: True when bytes written are reflected back into the read buffer.
    echoes: bool = False
    #: Whether this transport can perform the KWP2000 fast-init line pulse.
    supports_fast_init: bool = True
    #: Whether this transport can perform the ISO 9141 5-baud slow init.
    supports_slow_init: bool = False

    @abc.abstractmethod
    def open(self) -> None:
        """Open the underlying device."""

    @abc.abstractmethod
    def close(self) -> None:
        """Close the underlying device."""

    @abc.abstractmethod
    def reset_input(self) -> None:
        """Discard any buffered inbound bytes."""

    @abc.abstractmethod
    def write(self, data: bytes) -> None:
        """Transmit ``data`` on the K-line."""

    @abc.abstractmethod
    def read(self, count: int, timeout: float) -> bytes:
        """Read up to ``count`` bytes, blocking at most ``timeout`` seconds.

        Returns as many bytes as arrived; may be shorter than ``count`` on
        timeout.
        """

    @abc.abstractmethod
    def fast_init(self, low_ms: int = 25, high_ms: int = 25) -> None:
        """Perform the ISO 14230-2 fast-init wake-up pattern on the K-line.

        Holds the line low for ``low_ms`` then high for ``high_ms`` before the
        caller sends the StartCommunication request.
        """

    def five_baud_init(self, address: int) -> None:  # pragma: no cover - optional
        """Perform a 5-baud slow-init address handshake (optional).

        Not all ECUs need this; fast-init is the common Triumph path.  Concrete
        transports may override; the default declares it unsupported.
        """
        raise TransportError("5-baud slow init is not supported by this transport")

    # Convenience context-manager support -------------------------------------
    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
