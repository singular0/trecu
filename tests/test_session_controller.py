"""The TUI's connect/cancel state machine, exercised without driving a TUI.

``SessionController`` owns the one long-lived session and the single connect
path both the Read action and the live-poll loop go through. Everything here
runs against the in-memory mock ECUs on a bare asyncio loop — no Textual.
"""

import asyncio
import threading

from trecu.tui.session import ConnectOutcome, SessionController

from mock_ecus import (
    FAIL_FAST,
    CountingObdTransport,
    FailingObdTransport,
    GatedObdTransport,
)


def _controller(factory, **kw) -> SessionController:
    kw.setdefault("keepalive_interval", 0)
    return SessionController(transport_factory=factory, **kw)


# -- connect -----------------------------------------------------------------
def test_connect_publishes_session_and_serves_operations():
    built = []

    def factory():
        t = CountingObdTransport()
        built.append(t)
        return t

    ctl = _controller(factory)

    async def scenario():
        result = await ctl.connect()
        assert result.outcome is ConnectOutcome.CONNECTED
        assert result.connected and ctl.connected
        assert ctl.session.active_protocol == "iso9141"
        # A live session serves operations, and a second connect is a no-op
        # that reuses it rather than opening the port again.
        assert (await asyncio.to_thread(ctl.read_faults)).count == 1
        assert (await ctl.connect()).connected
        assert len(built) == 1
        await asyncio.to_thread(ctl.close)
        assert not ctl.connected and built[0].closes == 1

    asyncio.run(scenario())


def test_connect_fires_on_start_once_per_attempt():
    starts = []
    ctl = _controller(lambda: CountingObdTransport())

    async def scenario():
        await ctl.connect(on_start=lambda: starts.append(1))
        await ctl.connect(on_start=lambda: starts.append(1))  # already connected
        assert starts == [1]

    asyncio.run(scenario())


def test_failed_connect_reports_the_error_and_closes_the_transport():
    built = []

    def factory():
        t = FailingObdTransport()
        built.append(t)
        return t

    ctl = _controller(factory, config=FAIL_FAST)

    async def scenario():
        result = await ctl.connect()
        assert result.outcome is ConnectOutcome.FAILED
        assert "5-baud init failure" in str(result.error)
        assert not ctl.connected
        assert built[0].closes >= 1  # the port is released, not left held
        # The failure is not sticky: the next connect attempts afresh.
        assert (await ctl.connect()).outcome is ConnectOutcome.FAILED
        assert len(built) == 2

    asyncio.run(scenario())


def test_connect_without_a_port_fails_rather_than_raising():
    ctl = _controller(None)
    assert not ctl.can_connect
    result = asyncio.run(ctl.connect())
    assert result.outcome is ConnectOutcome.FAILED
    assert isinstance(result.error, RuntimeError)


# -- concurrent callers share one attempt ------------------------------------
def test_concurrent_connects_share_one_attempt(wait_for):
    """A Read and a live poll racing must not open the port twice."""
    gate = threading.Event()
    built = []
    starts = []

    def factory():
        t = GatedObdTransport(gate)
        built.append(t)
        return t

    ctl = _controller(factory)

    async def scenario():
        first = asyncio.ensure_future(ctl.connect(on_start=lambda: starts.append(1)))
        await wait_for(lambda: ctl.connecting)
        # Second caller joins the in-flight attempt: no second spinner, no
        # second transport.
        second = asyncio.ensure_future(ctl.connect(on_start=lambda: starts.append(1)))
        await asyncio.sleep(0.05)
        gate.set()
        results = await asyncio.gather(first, second)
        assert all(r.connected for r in results)
        assert len(built) == 1 and starts == [1]
        await asyncio.to_thread(ctl.close)

    asyncio.run(scenario())


# -- cancel ------------------------------------------------------------------
def test_cancel_abandons_the_in_flight_connect(wait_for):
    gate = threading.Event()
    built = []

    def factory():
        t = GatedObdTransport(gate)
        built.append(t)
        return t

    ctl = _controller(factory)

    async def scenario():
        pending = asyncio.ensure_future(ctl.connect())
        await wait_for(lambda: ctl.connecting)
        # Cancel returns at once — while the handshake is still stalled — and
        # force-closes the transport so that blocked read can unwind.
        assert ctl.cancel() is True
        assert not pending.done()
        assert built[0].closes >= 1
        assert ctl.cancel() is False  # nothing left to cancel
        gate.set()
        assert (await pending).outcome is ConnectOutcome.CANCELLED
        assert not ctl.connected  # the abandoned session is never published

    asyncio.run(scenario())


def test_connect_after_cancel_starts_a_fresh_attempt(wait_for):
    """A cancelled attempt must not be adopted by the next connect request.

    Regression guard for the live-poll path: cancelling the spinner and then
    re-entering Live Data has to open a *new* attempt, not inherit the doomed
    one's CANCELLED outcome (nor its session, once it finally unwinds).
    """
    gate = threading.Event()
    built = []

    def factory():
        t = GatedObdTransport(gate)
        built.append(t)
        return t

    ctl = _controller(factory)

    async def scenario():
        doomed = asyncio.ensure_future(ctl.connect())
        await wait_for(lambda: ctl.connecting)
        assert ctl.cancel() is True
        assert not ctl.connecting  # detached: the next request starts over
        gate.set()
        retry = await ctl.connect()
        assert retry.connected and ctl.connected
        assert len(built) == 2
        # The abandoned attempt finishes into its own teardown and leaves the
        # freshly published session alone.
        assert (await doomed).outcome is ConnectOutcome.CANCELLED
        assert ctl.session is not None and ctl.session.transport is built[1]
        await asyncio.to_thread(ctl.close)

    asyncio.run(scenario())


def test_shutdown_drops_both_the_attempt_and_the_session():
    gate = threading.Event()
    gate.set()
    ctl = _controller(lambda: GatedObdTransport(gate))

    async def scenario():
        assert (await ctl.connect()).connected
        held = ctl.session.transport
        ctl.shutdown()
        assert not ctl.connected and held.closes >= 1

    asyncio.run(scenario())


# -- operations require a session --------------------------------------------
def test_operations_without_a_session_raise():
    ctl = _controller(lambda: CountingObdTransport())
    for op in (ctl.read_faults, ctl.clear_faults, ctl.read_live):
        try:
            op()
        except RuntimeError as exc:
            assert "not connected" in str(exc)
        else:
            raise AssertionError(f"{op.__name__} should require a session")
