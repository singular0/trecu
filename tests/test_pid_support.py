"""Mode 01 supported-PID discovery: the capability set a session is built on.

Everything here is byte-level and hardware-free. The anchor is the real bike's
observed answer to Mode 01 PID 00 — ``41 00 BD 36 91 10`` — which the mock ECU
serves verbatim; the rest covers page walking, the ways a bitmap can be missing
or malformed, and the three states a capability-aware poll must never merge:
**advertised** by the ECU, **answered** when asked, and **decodable** by TrECU.

The rule under test throughout: a PID the ECU never advertised is not requested
at all (so it costs no timeout), and "the ECU didn't say" is not "the ECU said
no" — unknown capability filters nothing.
"""

import pytest

from trecu.protocol.common import (
    ProtocolError,
    encode_pid_support_pages,
    parse_pid_support_bitmap,
)
from trecu.protocol.iso9141 import Iso9141Client, Iso9141Config
from trecu.service import DEFAULT_LIVE_PIDS, DiagnosticService
from trecu.transport.mock_obd import MockObdTransport

#: The tested Triumph's own capability answer, byte for byte.
BIKE_BITMAP = b"\xBD\x36\x91\x10"
#: What that bitmap advertises, decoded by hand from the four bytes above.
BIKE_PIDS = {
    0x01, 0x03, 0x04, 0x05, 0x06, 0x08, 0x0B, 0x0C,
    0x0E, 0x0F, 0x11, 0x14, 0x18, 0x1C,
}

# No wire is involved, so nothing needs a real timeout.
FAST = dict(
    p2_timeout=0.05,
    live_timeout=0.05,
    id_timeout=0.05,
    pending_timeout=0.05,
    request_gap=0.0,
    frame_gap=0.0,
    dtc_retries=1,
    dtc_retry_wait=0.0,
)


def client_for(transport, **cfg) -> Iso9141Client:
    """Open + connect a client over ``transport`` with fast timings."""
    transport.open()
    client = Iso9141Client(transport, Iso9141Config(**{**FAST, **cfg}))
    client.connect()
    return client


def requested_pids(ecu: MockObdTransport) -> list:
    """The Mode 01 PIDs the tester actually put on the wire, in order."""
    return [req[1] for req in ecu.requests if req[0] == 0x01 and len(req) > 1]


# -- the bitmap parser itself -------------------------------------------------
def test_bike_bitmap_decodes_to_its_exact_pid_set():
    assert parse_pid_support_bitmap(0x00, BIKE_BITMAP) == BIKE_PIDS
    # The bike's last bit is clear: PID 20 is absent, so there is no next page.
    assert 0x20 not in parse_pid_support_bitmap(0x00, BIKE_BITMAP)


def test_last_bit_of_a_page_is_the_next_page_itself():
    # 0x00000001 -> only the continuation bit: PID 20 for page 00, PID 40 for 20.
    assert parse_pid_support_bitmap(0x00, b"\x00\x00\x00\x01") == {0x20}
    assert parse_pid_support_bitmap(0x20, b"\x00\x00\x00\x01") == {0x40}
    # First bit of a page is base + 1.
    assert parse_pid_support_bitmap(0x20, b"\x80\x00\x00\x00") == {0x21}


def test_short_bitmap_is_malformed_not_a_half_set():
    with pytest.raises(ProtocolError, match="short supported-PID bitmap"):
        parse_pid_support_bitmap(0x00, b"\xBD\x36")
    with pytest.raises(ProtocolError, match="short supported-PID bitmap"):
        parse_pid_support_bitmap(0x00, b"")


def test_encode_round_trips_through_the_parser():
    pids = {0x0C, 0x11, 0x42, 0xA5}
    pages = encode_pid_support_pages(pids)
    decoded = {
        pid
        for base, bitmap in pages.items()
        for pid in parse_pid_support_bitmap(base, bitmap)
    }
    # Every requested PID is advertised, plus the page markers that lead to them.
    assert pids <= decoded
    assert decoded - pids == {0x20, 0x40, 0x60, 0x80, 0xA0}
    assert sorted(pages) == [0x00, 0x20, 0x40, 0x60, 0x80, 0xA0]


# -- discovery on connect -----------------------------------------------------
def test_connect_discovers_the_bikes_advertised_set():
    ecu = MockObdTransport()
    ecu.open()
    client = Iso9141Client(ecu, Iso9141Config(**FAST))
    info = client.connect()
    assert info.supported_pids == BIKE_PIDS
    assert client.supported_pids == BIKE_PIDS
    # PID 00 is asked for first, straight after the handshake, and once.
    assert requested_pids(ecu)[0] == 0x00
    assert requested_pids(ecu).count(0x00) == 1


def test_no_continuation_bit_means_no_second_page_is_requested():
    ecu = client_for(MockObdTransport()).transport
    assert 0x20 not in requested_pids(ecu)


def test_multiple_pages_are_walked_while_advertised():
    pids = {0x0C, 0x42, 0xA5}  # spread over pages 00, 40 and A0
    ecu = MockObdTransport(support_pages=encode_pid_support_pages(pids))
    client = client_for(ecu)
    assert pids <= (client.supported_pids or set())
    # Walked page by page, in order, and stopped at the last advertised one.
    assert [p for p in requested_pids(ecu) if p % 0x20 == 0] == [
        0x00, 0x20, 0x40, 0x60, 0x80, 0xA0
    ]
    assert 0xC0 not in requested_pids(ecu)


def test_discovery_is_cached_for_the_session():
    ecu = MockObdTransport()
    client = client_for(ecu)
    before = requested_pids(ecu).count(0x00)
    assert client.discover_supported_pids() == BIKE_PIDS
    assert requested_pids(ecu).count(0x00) == before  # answered from cache


def test_reconnect_clears_and_rereads_the_cache():
    ecu = MockObdTransport()
    client = client_for(ecu)
    assert client.supported_pids == BIKE_PIDS

    # The ECU now advertises something else entirely (a different module on the
    # bus, or a re-picked port). A new session must not carry the old set.
    ecu.support_pages = encode_pid_support_pages({0x0C, 0x11})
    client.connect()
    assert client.supported_pids == {0x0C, 0x11}


# -- when the ECU will not say ------------------------------------------------
class NoBitmap(MockObdTransport):
    """ECU that answers everything except the capability request."""

    def _respond(self, payload):
        if payload[:2] == b"\x01\x00":
            return  # silence, like an ECU that doesn't implement PID 00
        super()._respond(payload)


class MalformedBitmap(MockObdTransport):
    """ECU whose capability page is two bytes short of a bitmap."""

    def _respond(self, payload):
        if payload[:2] == b"\x01\x00":
            self._emit(b"\x41\x00\xBD\x36")
            return
        super()._respond(payload)


class OnlyFirstPage(MockObdTransport):
    """ECU that advertises a second page, then never answers it."""

    def _respond(self, payload):
        if payload[:2] == b"\x01\x20":
            return
        super()._respond(payload)


@pytest.mark.parametrize("ecu_class", (NoBitmap, MalformedBitmap))
def test_unusable_bitmap_leaves_capability_unknown_and_filters_nothing(ecu_class):
    """Unknown is not empty: with no usable bitmap every requested PID is asked
    for, because ruling one out would be inventing a capability answer."""
    ecu = ecu_class()
    client = client_for(ecu)
    assert client.supported_pids is None  # not frozenset()
    assert client.live_plan([0x0C, 0x42]) == [0x0C, 0x42]
    client.read_live([0x0C, 0x42])
    assert 0x42 in requested_pids(ecu)


def test_a_session_still_works_without_a_bitmap():
    """Discovery is best-effort: no capability must not mean no session."""
    with DiagnosticService(NoBitmap(), config=Iso9141Config(**FAST)) as svc:
        assert [d.code for d in svc.read_faults().dtcs] == ["P1108"]
        assert svc.supported_pids is None
        assert [r.pid for r in svc.read_live([0x0C])] == [0x0C]


def test_a_page_that_stops_answering_keeps_what_was_learned():
    pages = encode_pid_support_pages({0x0C, 0x42})  # pages 00, 20, 40
    ecu = OnlyFirstPage(support_pages=pages)
    client = client_for(ecu)
    # Page 00 was read; the advertised page 20 never answered, so the walk ends
    # there with a partial — but real — capability set.
    assert client.supported_pids == {0x0C, 0x20}
    assert 0x40 not in requested_pids(ecu)


# -- capability-aware polling -------------------------------------------------
def test_unadvertised_pid_is_never_requested():
    """The Done-when of this feature: no request, so no timeout, for a PID the
    ECU never claimed — here PID 42 (battery volts) on the tested bike."""
    ecu = MockObdTransport()
    client = client_for(ecu)
    assert set(client.read_live([0x0C, 0x42])) == {0x0C}
    assert 0x42 not in requested_pids(ecu)
    assert 0x0C in requested_pids(ecu)


def test_service_live_plan_is_the_three_way_intersection():
    ecu = MockObdTransport()
    with DiagnosticService(ecu, config=Iso9141Config(**FAST)) as svc:
        # 0x0C: advertised + decodable -> polled.
        # 0x42: decodable but not advertised -> dropped by the ECU's bitmap.
        # 0x18: advertised but no decoder in this build -> dropped by the table.
        # 0xEE: neither -> dropped.
        assert svc.live_plan([0x0C, 0x42, 0x18, 0xEE]) == [0x0C]
        svc.read_live([0x0C, 0x42, 0x18, 0xEE])
    asked = requested_pids(ecu)
    assert 0x42 not in asked and 0x18 not in asked and 0xEE not in asked


def test_default_live_set_drops_the_pid_this_bike_lacks():
    ecu = MockObdTransport()
    with DiagnosticService(ecu, config=Iso9141Config(**FAST)) as svc:
        assert svc.live_plan() == [p for p in DEFAULT_LIVE_PIDS if p != 0x42]


# -- keepalive reuses PID 00 without rebuilding the cache ---------------------
def test_keepalive_does_not_rebuild_or_change_the_cached_set():
    ecu = MockObdTransport()
    client = client_for(ecu)
    cached = client.supported_pids
    # The ECU's bitmap changes underneath the session; a keepalive beat must not
    # quietly re-plan the poll around it.
    ecu.support_pages = encode_pid_support_pages({0x0C})
    for _ in range(3):
        client.keepalive()
    assert client.supported_pids == cached
    # ... and each beat was one cheap PID 00 request, not a page walk.
    assert requested_pids(ecu).count(0x20) == 0


def test_keepalive_reports_a_changed_capability_page_once():
    lines = []
    ecu = MockObdTransport()
    ecu.open()
    client = Iso9141Client(ecu, Iso9141Config(**FAST), logger=lines.append)
    client.connect()
    ecu.support_pages = encode_pid_support_pages({0x0C})
    for _ in range(3):
        client.keepalive()
    # Said once, not on every beat for the rest of the session.
    assert sum("capability page 00 changed" in line for line in lines) == 1


# -- the three states, kept apart ---------------------------------------------
def test_pid_capabilities_separates_advertised_answered_and_decodable():
    with DiagnosticService(MockObdTransport(), config=Iso9141Config(**FAST)) as svc:
        by_pid = {s.pid: s for s in svc.pid_capabilities(probe=True)}

    rpm = by_pid[0x0C]  # advertised, decodable, and it answers
    assert (rpm.advertised, rpm.decodable, rpm.answered) == (True, True, True)
    assert rpm.raw and rpm.polled

    silent = by_pid[0x03]  # advertised, but no value and no decoder
    assert (silent.advertised, silent.decodable, silent.answered) == (
        True, False, False,
    )

    battery = by_pid[0x42]  # decodable, never advertised -> never asked
    assert (battery.advertised, battery.decodable, battery.answered) == (
        False, True, None,
    )
    assert not battery.polled


def test_unknown_capability_is_reported_as_unknown_not_unsupported():
    with DiagnosticService(NoBitmap(), config=Iso9141Config(**FAST)) as svc:
        statuses = svc.pid_capabilities()
    assert statuses and all(s.advertised is None for s in statuses)
    # Unknown still polls: nothing was ruled out.
    assert all(s.polled for s in statuses if s.decodable)


def test_tui_connection_card_shows_advertised_and_decodable_counts(
    mock_app, wait_for
):
    """The UI keeps the states apart too: the Connection card carries what the
    ECU claims and what this build decodes, while the Live Data table is the
    separate record of what answered."""
    import asyncio

    from textual.widgets import Static

    app = mock_app()

    async def scenario():
        async with app.run_test() as pilot:
            await wait_for(lambda: app._state == "connected", pilot.pause)
            card = app.query_one("#card-connection", Static)
            text = card.render().plain
            decodable = sum(1 for pid in BIKE_PIDS if pid in app._pids)
            assert f"{len(BIKE_PIDS)} advertised · {decodable} decodable" in text

    asyncio.run(scenario())


def test_zero_data_from_an_advertised_pid_counts_as_answered():
    """A supported PID replying 00 is a response, not a missing sensor."""

    class ZeroCoolant(MockObdTransport):
        def _respond(self, payload):
            if payload[:2] == b"\x01\x05":
                self._emit(b"\x41\x05\x00")
                return
            super()._respond(payload)

    with DiagnosticService(ZeroCoolant(), config=Iso9141Config(**FAST)) as svc:
        status = {s.pid: s for s in svc.pid_capabilities(probe=True)}[0x05]
        reading = {r.pid: r for r in svc.read_live([0x05])}[0x05]
    assert status.answered is True and status.raw == b"\x00"
    assert reading.value == -40  # A - 40, the coldest the sensor encodes
