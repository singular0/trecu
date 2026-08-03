"""The ``supports_slow_init`` capability flag and the init waveform it gates.

A transport implements the 5-baud init only if its device can drive it, and
advertises that through ``supports_slow_init``; the protocol layer branches on
the flag rather than on concrete types. The init is not abstract, so a plain
byte-pipe transport does not have to implement-and-raise it — the base class
already refuses the call.
"""

import pytest

from trecu.transport.base import TransportError
from trecu.transport.mock_obd import MockObdTransport

from mock_ecus import BytePipeOnly


def test_the_init_is_not_required_to_subclass_transport():
    # Instantiating at all is the assertion: an @abstractmethod init would make
    # this a TypeError.
    t = BytePipeOnly()
    assert t.supports_slow_init is False
    with pytest.raises(TransportError, match="5-baud slow init is not supported"):
        t.five_baud_init(0x33)


@pytest.mark.parametrize(
    "transport", (MockObdTransport(), BytePipeOnly()), ids=("mock-obd", "byte-pipe")
)
def test_flags_match_the_init_each_transport_actually_serves(transport):
    """Each flag is honest: True means the call works, False means it refuses."""
    transport.open()
    if transport.supports_slow_init:
        transport.five_baud_init(transport.init_address)
        assert transport.read(3, 0.1)  # sync + key bytes are waiting
    else:
        with pytest.raises(TransportError):
            transport.five_baud_init(0x33)
    transport.close()
