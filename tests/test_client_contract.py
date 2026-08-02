"""The EcuClient contract every protocol client implements.

The two clients are duck-typed peers with no shared base class, so nothing but
these tests keeps a third client (or a test double) honest about the surface
:class:`~trecu.service.DiagnosticService` calls. Conformance is structural:
``EcuClient`` is a runtime-checkable ``typing.Protocol``, so ``isinstance``
checks that every member exists without anything inheriting from it.
"""

import pytest

from trecu.protocol.iso9141 import Iso9141Client
from trecu.protocol.kwp2000 import EcuClient, Kwp2000Client, Kwp2000Config
from trecu.transport.mock_kline import MockKLineTransport
from trecu.transport.mock_obd import MockObdTransport


def _clients():
    return [
        Iso9141Client(MockObdTransport()),
        Kwp2000Client(MockKLineTransport()),
        # The 0x18 variant: dtc_family is a computed property here, not a
        # class attribute — the contract must accept either.
        Kwp2000Client(MockKLineTransport(), Kwp2000Config(read_dtc_service=0x18)),
    ]


@pytest.mark.parametrize("client", _clients())
def test_real_clients_satisfy_the_contract(client):
    assert isinstance(client, EcuClient)


@pytest.mark.parametrize("client", _clients())
def test_decode_steering_attributes_are_readable(client):
    # The service reads both directly (no getattr defaults) to pick a decode
    # table and a DTC labelling scheme.
    assert client.live_source in ("obd_mode01", "kwp_local")
    assert client.dtc_family is None or isinstance(client.dtc_family, str)


def test_a_client_missing_a_member_does_not_satisfy_the_contract():
    # Proves the check has teeth: keepalive() is the one the keepalive ticker
    # calls unguarded, and the probe that used to hide its absence is gone.
    class Incomplete:
        live_source = "obd_mode01"
        dtc_family = None

        def connect(self): ...
        def read_dtcs(self): ...
        def read_identification(self): ...
        def read_live(self, pids): ...
        def clear_dtcs(self): ...
        def stop_communication(self): ...

    assert not isinstance(Incomplete(), EcuClient)
