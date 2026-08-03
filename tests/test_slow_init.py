"""The 5-baud slow init: its own seam, below the OBD service layer.

The handshake itself (sync, key bytes, the required inverted-address close) is
covered end-to-end in ``test_iso9141_obd.py``. What is asserted here is the
policy *around* it — the retry loop and its config section — because that is
where the flaky-5-baud lesson from the real bike lives: retry with a settle gap,
and reject a garbled init rather than proceed on a half-open link.
"""

import pytest

from trecu.protocol.common import (
    ProtocolError,
    SlowInitConfig,
    slow_init_with_retries,
)
from trecu.protocol.iso9141 import Iso9141Client, Iso9141Config
from trecu.transport.mock_obd import MockObdTransport

from mock_ecus import BytePipeOnly

_NO_WAIT = dict(retry_wait=0.0, sync_timeout=0.05, byte_timeout=0.05)


class FlakyObd(MockObdTransport):
    """The first ``fail_times`` inits come back without the inv-addr.

    Reproduces the observed macOS failure — the ECU answers, but the handshake
    never closes — which must drive a retry, not a half-open session.
    """

    def __init__(self, *a, fail_times: int = 0, **k):
        super().__init__(*a, **k)
        self.fail_times = fail_times
        self.init_attempts = 0

    def five_baud_init(self, address: int) -> None:
        self.init_attempts += 1
        super().five_baud_init(address)
        if self.init_attempts <= self.fail_times:
            self._await_inv = False  # swallow the handshake's closing byte


def _client(fail_times: int, init_retries: int):
    t = FlakyObd(fail_times=fail_times)
    cfg = SlowInitConfig(init_retries=init_retries, **_NO_WAIT)
    return t, Iso9141Client(t, Iso9141Config(slow_init=cfg))


# -- the init's timing/retry policy is its own config section ------------------
def test_iso_config_composes_the_slow_init_section() -> None:
    """The handshake fields live in SlowInitConfig, not inline on Iso9141Config."""
    assert Iso9141Config().slow_init == SlowInitConfig()


def test_slow_init_section_is_not_shared_between_config_instances() -> None:
    """default_factory, not a mutable class-level default: overriding one
    config's retry policy must not move every other config's."""
    iso = Iso9141Config(slow_init=SlowInitConfig(init_retries=1))
    assert iso.slow_init.init_retries == 1
    assert Iso9141Config().slow_init.init_retries == 4


# -- the retry loop the client connects through --------------------------------
def test_connect_recovers_from_a_garbled_first_init() -> None:
    t, client = _client(fail_times=1, init_retries=4)
    t.open()
    assert client.connect().key_bytes  # second attempt takes
    assert t.init_attempts == 2
    t.close()


def test_connect_gives_up_after_init_retries_attempts() -> None:
    t, client = _client(fail_times=99, init_retries=3)
    t.open()
    with pytest.raises(ProtocolError, match="5-baud init failed"):
        client.connect()
    assert t.init_attempts == 3  # the budget, spent exactly once each
    t.close()


def test_zero_retries_still_makes_one_attempt() -> None:
    """A retry budget of 0 means "don't retry", not "don't try" — the loop used
    to read it as the latter and fail with a bare 'None'."""
    t = FlakyObd(fail_times=0)
    t.open()
    key = slow_init_with_retries(
        t, 0x33, SlowInitConfig(init_retries=0, **_NO_WAIT)
    )
    assert key == b"\x08\x08"
    assert t.init_attempts == 1
    t.close()


def test_helper_refuses_a_transport_that_cannot_slow_init() -> None:
    """Refused up front, unwrapped — not retried for the full budget first."""
    t = BytePipeOnly()  # supports_slow_init is False
    t.open()
    with pytest.raises(ProtocolError, match="does not support 5-baud"):
        slow_init_with_retries(t, 0x33, SlowInitConfig(**_NO_WAIT))
    t.close()
