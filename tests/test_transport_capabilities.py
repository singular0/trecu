"""Transport capability flags, and the two init waveforms they gate.

A transport implements whichever init its device can drive and advertises that
through ``supports_fast_init`` / ``supports_slow_init``; the protocol layer
branches on the flags rather than on concrete types. Neither init is abstract,
so a transport that does one does not have to implement-and-raise the other —
the base class already refuses it.
"""

import pytest

from trecu.transport.base import Transport, TransportError
from trecu.transport.mock_kline import MockKLineTransport
from trecu.transport.mock_obd import MockObdTransport


class BytePipeOnly(Transport):
    """The minimum a concrete transport must define: the byte pipe, no init."""

    def open(self) -> None: ...

    def close(self) -> None: ...

    def reset_input(self) -> None: ...

    def write(self, data: bytes) -> None: ...

    def read(self, count: int, timeout: float) -> bytes:
        return b""


def test_neither_init_is_required_to_subclass_transport():
    # Instantiating at all is the assertion: an @abstractmethod init would make
    # this a TypeError.
    t = BytePipeOnly()
    with pytest.raises(TransportError, match="fast init is not supported"):
        t.fast_init()
    with pytest.raises(TransportError, match="5-baud slow init is not supported"):
        t.five_baud_init(0x33)


@pytest.mark.parametrize(
    "transport",
    (MockObdTransport(), MockKLineTransport(), MockKLineTransport(supports_slow_init=True)),
    ids=("mock-obd", "mock-kline", "mock-kline-slow"),
)
def test_mock_flags_match_the_inits_they_actually_serve(transport):
    """Each flag is honest: True means the call works, False means it refuses."""
    transport.open()
    if transport.supports_fast_init:
        transport.fast_init()
    else:
        with pytest.raises(TransportError):
            transport.fast_init()

    # The init address is a protocol fact, so it is named per mock: 0x33 for
    # the OBD path, the ECU's own address for the Keihin one.
    address = (
        transport.init_address
        if isinstance(transport, MockObdTransport)
        else transport.ecu_address
    )
    if transport.supports_slow_init:
        transport.five_baud_init(address)
        assert transport.read(3, 0.1)  # sync + key bytes are waiting
    else:
        with pytest.raises(TransportError):
            transport.five_baud_init(address)
    transport.close()
