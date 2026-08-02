"""The 5-baud slow init as a *shared* seam, exercised through both clients.

The handshake itself (sync, key bytes, the required inverted-address close) is
covered per-protocol in ``test_iso9141_obd.py`` / ``test_kwp_triumph.py``. What
is asserted here is that the init is one thing and not two: both clients init
through :func:`slow_init_with_retries` with the same :class:`SlowInitConfig`
section, so the flaky-5-baud lesson from the real bike (retry, and reject a
garbled init rather than proceed on a half-open link) cannot regress on one
path while the other stays fixed.
"""

import pytest

from trecu.protocol.iso9141 import Iso9141Client, Iso9141Config
from trecu.protocol.kwp2000 import (
    Kwp2000Client,
    Kwp2000Config,
    ProtocolError,
    SlowInitConfig,
    slow_init_with_retries,
)
from trecu.transport.mock_kline import MockKLineTransport
from trecu.transport.mock_obd import MockObdTransport

_NO_WAIT = dict(retry_wait=0.0, sync_timeout=0.05, byte_timeout=0.05)


class _GarbledInit:
    """Mixin: the first ``fail_times`` inits come back without the inv-addr.

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


class FlakyObd(_GarbledInit, MockObdTransport):
    pass


class FlakyKLine(_GarbledInit, MockKLineTransport):
    pass


def _iso(fail_times: int, init_retries: int):
    t = FlakyObd(fail_times=fail_times)  # the OBD mock is slow-init only
    cfg = SlowInitConfig(init_retries=init_retries, **_NO_WAIT)
    return t, Iso9141Client(t, Iso9141Config(slow_init=cfg))


def _kwp(fail_times: int, init_retries: int):
    t = FlakyKLine(fail_times=fail_times, supports_slow_init=True)
    cfg = SlowInitConfig(init_retries=init_retries, **_NO_WAIT)
    return t, Kwp2000Client(t, Kwp2000Config(init_mode="slow", slow_init=cfg))


# Both slow-init clients, driven through the one shared retry loop.
_CLIENTS = pytest.mark.parametrize("build", (_iso, _kwp), ids=("iso9141", "kwp-slow"))


# -- one config section, not two ----------------------------------------------
def test_both_protocol_configs_carry_the_same_slow_init_section() -> None:
    """The five handshake fields are defined once and shared by composition."""
    assert Iso9141Config().slow_init == Kwp2000Config().slow_init == SlowInitConfig()


def test_slow_init_section_is_not_shared_between_config_instances() -> None:
    """default_factory, not a mutable class-level default: overriding one
    protocol's retry policy must not move the other's."""
    iso = Iso9141Config(slow_init=SlowInitConfig(init_retries=1))
    assert iso.slow_init.init_retries == 1
    assert Iso9141Config().slow_init.init_retries == 4
    assert Kwp2000Config().slow_init.init_retries == 4


# -- the retry loop both clients connect through -------------------------------
@_CLIENTS
def test_connect_recovers_from_a_garbled_first_init(build) -> None:
    t, client = build(fail_times=1, init_retries=4)
    t.open()
    assert client.connect().key_bytes  # second attempt takes
    assert t.init_attempts == 2
    t.close()


@_CLIENTS
def test_connect_gives_up_after_init_retries_attempts(build) -> None:
    t, client = build(fail_times=99, init_retries=3)
    t.open()
    with pytest.raises(ProtocolError, match="5-baud init failed"):
        client.connect()
    assert t.init_attempts == 3  # the budget, spent exactly once each
    t.close()


def test_zero_retries_still_makes_one_attempt() -> None:
    """A retry budget of 0 means "don't retry", not "don't try" — the iso9141
    loop used to read it as the latter and fail with a bare 'None'."""
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
    t = MockKLineTransport()  # fast-init only
    t.open()
    with pytest.raises(ProtocolError, match="does not support 5-baud"):
        slow_init_with_retries(t, 0xD5, SlowInitConfig(**_NO_WAIT))
    t.close()
