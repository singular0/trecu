"""Transport abstraction for the single-wire K-line.

A KKL cable exposes the ISO 9141-2 K-line as a plain serial port.
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
    """Abstract half-duplex byte transport with the 5-baud init waveform."""

    #: True when bytes written are reflected back into the read buffer.
    echoes: bool = False
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

    # -- Init waveform: optional, gated by the capability flag ------------------
    # The init is not abstract.  A transport implements the waveform only if its
    # device can actually drive it and advertises that through
    # :attr:`supports_slow_init`, which is what the protocol layer branches on —
    # a client refuses to ``connect()`` over a transport whose flag is False
    # rather than calling and catching.  The default below is only the backstop
    # for a caller that ignored the flag, so a plain byte-pipe transport does not
    # have to implement-and-raise it.

    def five_baud_init(self, address: int) -> None:
        """Perform the ISO 9141-2 5-baud slow-init address handshake.

        Implemented only by transports declaring :attr:`supports_slow_init`.
        """
        raise TransportError("5-baud slow init is not supported by this transport")

    # Convenience context-manager support -------------------------------------
    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
