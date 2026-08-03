# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`trecu` is a cross-platform TUI (Textual) that reads and decodes ECU fault codes (DTCs)
from Triumph motorcycles over a cheap KKL / FT232RL K-line cable. It speaks
**ISO 9141-2 / OBD-II** — the one endpoint confirmed on a real bike — decodes
DTCs per SAE J2012, and can clear them. Engine-ECU diagnostics only: no CAN
modules, ABS, manufacturer service functions, tuning, or programming.
`README.md` is the user-facing guide; the byte-level K-line handshake lives in
the **K-line protocol reference** section below — read it before touching the
protocol layer.

The 5-baud init, OBD framing, DTC services, and SAE J2012 decoding are all
hand-rolled here on purpose: a KKL cable is a *dumb* FTDI serial adapter, **not**
an ELM327, so `python-OBD` does not apply, and there is no maintained general
Python OBD client for a raw K-line. Dependencies are kept small and to
well-maintained libraries (see `pyproject.toml`): `textual` (TUI), `rich`
(formatting, via textual), and `pyserial` (FT232RL VCP access).

## Commands

Python is a mise-managed 3.11 in `.venv`. Always drive the venv explicitly:

```bash
./.venv/bin/python -m pytest              # full suite (167 tests, ~60s, no hardware)
./.venv/bin/python -m pytest tests/test_iso9141_obd.py::test_obd_read_decode_clear_cycle
./.venv/bin/trecu --mock                  # the default command is `tui`: TUI vs a simulated ECU
./.venv/bin/trecu faults --mock           # headless read + print + exit
./.venv/bin/trecu info --mock             # headless ECU identification
./.venv/bin/trecu sensors --mock          # headless live-sensor snapshot + exit
./.venv/bin/trecu clear --mock -y         # clear, skipping the confirmation prompt
./.venv/bin/trecu ports                   # find a real cable's /dev/cu.usbserial-*
```

The CLI is a **subcommand** surface (`cli.py:27-86`), not a flag soup: one
optional positional out of `tui|ports|faults|info|sensors|clear|version|help`,
defaulting to `tui`. `--port`, `--baud`, and `--mock` are
`argparse.SUPPRESS`-hidden development hooks — the public surface auto-detects
the cable.

Tests run **entirely against the in-memory mock ECU** — never require hardware,
and any new test must follow suit. Use `--debug` on the CLI to dump raw byte
traffic when debugging a protocol (it also auto-opens the TUI's Log tab).

Shared test scaffolding lives in two files, so a new test doesn't rebuild it:
`tests/conftest.py` has the fixtures — `mock_app` (a `TrecuApp` on a fixed mock
ECU: simulated port, no keepalive ticker), `picker_app` (no port yet, so it
opens the picker), and `wait_for`
(`await wait_for(cond, pilot.pause)` — reads/clears/live polls run off the event
loop in `asyncio.to_thread`, so poll for the result, never `sleep` a guess).
`tests/mock_ecus.py` has the ECU doubles more than one file needs — counting /
gated (5-baud init blocks on an `Event`, for mid-connect assertions) / failing /
`BytePipeOnly` (a bare transport that cannot slow-init) — plus `FAIL_FAST` (one
init attempt, no settle wait) and `TWO_PORTS`.

**`tests/test_iso9141_framing.py` is where traffic that must *not* decode
lives** — bad checksums, foreign modules, wrong modes/PIDs, noise, truncation,
concatenated frames, echo damage, bad Mode 09 sequences. It scripts raw bytes
onto the wire (`RawReplyEcu` and friends, local to that file, since
`MockObdTransport` frames correctly by construction), so put a new negative
framing case there rather than teaching the shared mock to misbehave.

## Releasing

The package **version is not stored in `pyproject.toml`** — it's derived from the
git tag at build time by `hatch-vcs` (`[tool.hatch.version] source = "vcs"`), so
`trecu.__version__` (and `trecu version`) read it back from installed metadata
via `importlib.metadata`. To cut a release, push a **semver tag** (`vMAJOR.MINOR.PATCH`,
optionally `-prerelease`/`+build`): `.github/workflows/release.yml` fires **only**
on those tags. It first runs the **mock-only test suite as a gate** (a `test` job
the build `needs`), and only if that passes builds the sdist + wheel (whose version
therefore equals the tag, re-checked by a "Verify built version matches the tag"
step). Non-semver tags are ignored by the tag filter, and a strict-semver guard
step fails anything that slips through.

Two **sibling publish jobs** then fan out from that one build (both `needs:
build`, so neither rebuilds and both ship identical artifacts):

- **`pypi`** — uploads to PyPI via **Trusted Publishing**
  (`pypa/gh-action-pypi-publish`, the `pypi` GitHub environment, `id-token:
  write`), so no long-lived API token is stored. This is the documented install
  path: `pip install trecu` / `pipx install trecu` (see README).
- **`release`** — attaches the same sdist + wheel to a **GitHub Release**
  (created via the `gh` CLI, no third-party action; a `-prerelease` tag is
  marked pre-release).

## Architecture

Four layers, top to bottom. Adding a protocol or transport means slotting into
one of these seams, not rewiring the app:

```
CLI (cli.py) / TUI (tui/app.py)
        │                        ← the TUI's session/connect state machine
        │                          lives in tui/session.py (no Textual import)
DiagnosticService (service.py)   ← owns lifecycle; builds the client
        │
Iso9141Client (protocol/iso9141.py)  ← the one protocol client
        │
Transport (transport/base.py)    ← half-duplex byte pipe: serial or mock
```

**There is one protocol client, and no client abstraction over it.** TrECU used
to carry two duck-typed peers (`Iso9141Client` / `Kwp2000Client`) behind an
`EcuClient` `typing.Protocol`; the KWP path was community speculation that never
touched a bike, so it is gone, and with it the `EcuClient` seam, `live_source`,
`dtc_family`, and the per-attempt protocol sweep. `DiagnosticService` types
directly against `Iso9141Client` and calls its members **directly** — no
`getattr` probes, so a missing method fails loudly rather than being swallowed.
The surface is `connect() -> ConnectionInfo`, `read_dtcs() -> list[(hi, lo,
status)]`, `read_identification() -> EcuInfo`, `read_live(pids) -> dict[pid,
data_bytes]`, `clear_dtcs()`, `keepalive()`, `stop_communication()`.
`read_live()` polls live sensors (Phase 3) with one OBD **Mode 01** request per
PID, returning *raw* data bytes per requested PID (a PID the ECU doesn't answer
is simply omitted) — decoding to physical values is the service's job via the
sensor-decode layer, not the client's. `keepalive()` holds a persistent session
open (F1): OBD-II has no TesterPresent service, so it pokes the link with a
cheap read-only Mode 01 PID 00. `read_identification()` is best-effort (OBD
Mode 09) — a missing reply yields empty fields, not an error, and a field is
**complete or empty**, never the plausible ASCII half of a VIN.

**Nothing reaches a decoder unvalidated.** Every response goes through one seam
— `Iso9141Client._exchange` — which collects raw bytes, splits them into whole
checksum-verified frames, and only then matches them to the request in flight.
The rules it enforces, in order: a `7F <mode> <nrc>` rejection of *this* request
raises (naming the NRC) rather than falling through to a timeout; a frame must
carry this request's positive-response mode **and** its echoed PID, or it is
unrelated traffic and is skipped, not mistaken for the answer; and it must come
from the module this session is addressing. That module is `Iso9141Config.
ecu_address` when set, otherwise the address **latched from the first ECU that
answers** (reset by `connect()`), so a second module on the bus is rejected
instead of decoded. A bad checksum, a truncated frame, leading line noise, and a
data field longer than the ISO limit all end up *outside* a frame and are
discarded with a debug line — there is no "continue best-effort" path any more,
and no trimming to the first `0x48`. `obd_request()` returns the single matching
payload; `obd_request_multi()` returns every matching frame's payload, for the
answers that legitimately span frames (see below).

**A frame holds 7 data bytes, so long answers span frames.** `MAX_DATA_BYTES`
is the ISO 9141-2 data-field limit (the mode byte plus its data), which caps one
Mode 03 frame at **three DTC pairs** and makes every Mode 09 string multi-frame.
The client reassembles both: `_request_dtcs` concatenates the pairs of all `43`
frames and de-duplicates them (a retransmitted frame must not inflate the count
`_read_stored` reconciles against), and `_read_vehicle_info` feeds the `49 <pid>
<seq> <data>` fragments to `reassemble_identification`. Because the frames of
one answer can be a full P2 gap apart — which looks exactly like end-of-message
to `_collect` — a multi-frame request waits one extra `frame_gap` window after
each batch (`_collect_multi`); only those requests pay it, so live polling and
PID 01 stay at one collect.

**`protocol/common.py` is the home of the shared vocabulary** — everything that
sits *below* the OBD service layer: `ProtocolError`, `ConnectionInfo`, `EcuInfo`,
`decode_identification_ascii`, `parse_obd_dtc_pairs` +
`STATUS_CONFIRMED`/`STATUS_PENDING` for Mode 03/07 responses, the data-link
framing (`split_response_frames` → `ObdFrame`s + leftover junk, `MAX_DATA_BYTES`,
and `reassemble_identification` for numbered Mode 09 fragments), and the whole
5-baud slow init (`slow_init_handshake`, the validated one-shot handshake;
`slow_init_with_retries`, the loop `connect()` calls, which owns the
retry/settle policy and the transport-capability refusal; and `SlowInitConfig`,
the timing + retry section `Iso9141Config` *composes* rather than inlines).
`iso9141.py` imports all of it and stays about OBD requests and responses. Keep
any new shared type or shared helper in `common.py`.

**`split_response_frames` proves each frame's boundary; it does not guess.**
ISO 9141-2 has no length byte, so a frame's end is the shortest data length
whose trailing byte equals the running sum of everything before it — preferring
a length that also lands on either the end of the buffer or another frame's
header, so a concatenated response splits where the frames really end rather
than at the first length whose checksum happens to add up. A checksum-valid
length that leaves unexplained bytes behind is only a fallback (trailing noise);
a candidate whose checksum never validates has no end, and its bytes go to
`junk`. `reassemble_identification` is the strict half of Mode 09: fragments are
sorted by sequence, an exact duplicate is a retransmission and is ignored, but a
conflicting duplicate, a gap in the sequence, a sequence not starting at 1, or
more than `max_frames` fragments all raise — the caller then reports that field
as unavailable.

**`DiagnosticService` builds exactly one client.** `_build_client()` returns an
`Iso9141Client` over this session's device and config; `_connect()` calls it
once and raises `ProtocolError("could not connect: …")` if the init fails —
there is no candidate list and no sweep to walk. A caller can inject a pre-built
`client=` to bypass construction entirely (used by tests). `active_protocol` /
`ReadResult.protocol` still report the label `"iso9141"` (the `PROTOCOL_ISO9141`
constant) because the TUI shows it on the Dashboard.

**The service holds its device as a factory, not an instance.** One transport is
built per session (lazily, on first use) and released by `close()`, so `close()`
→ `open()`/`start_session()` reconnects over a **fresh** device — reconnect is a
service operation rather than something the caller rebuilds the service for.
`as_transport_factory` normalizes either shape:
handing over a **`Transport` instance** instead pins that one device for every
session, which is exactly what `trecu tui --mock` wants — one simulated ECU, so
codes the user clears stay cleared across connects. `service.transport` is the
device currently held, `None` once closed. The TUI's `SessionController` still
builds **one service per connect attempt** — not because a service is single-use,
but because a cancelled attempt keeps running inside *its* service's `_io_lock`
and must not tear down a newer session; the attempt-isolation rationale is
documented beside `SessionController.build_service`.

**Two lifecycle modes.** One-shot (`with service:` → `open`/`close`) still
connects lazily on the first operation — that's the path the CLI's
`faults`/`info`/`sensors`/`clear` subcommands take (all four through
`cli._with_service`). **Persistent (F1)** is `service.session()` (a context manager) or
`start_session()`: it connects *up front* and runs a background keepalive
ticker (`_Keepalive`, a daemon thread) sending `client.keepalive()` every
`DEFAULT_KEEPALIVE_INTERVAL` (2 s) so the ECU doesn't drop the session while
idle — pass `keepalive_interval=0` to disable it. Because the K-line is
half-duplex, every operation *and* every keepalive beat runs under one
`_io_lock`, so a beat can never interleave with a read/clear. This is the seam
Phase 3 live-polling builds on.

**Transports advertise capabilities via class flags**, and the protocol layer
branches on them rather than on concrete types:
- `echoes` — single-wire K-line reflects every TX byte into RX; the client
  discards that echo before parsing. Real serial echoes; mocks don't.
- `supports_slow_init` — the client refuses to `connect()` over a transport that
  can't drive the 5-baud init. `MockObdTransport` and `KLineSerialTransport`
  both can; `tests/mock_ecus.py`'s `BytePipeOnly` deliberately can't.
  **`five_baud_init` is not abstract**: a transport implements the waveform only
  if its device can drive it and the flag is what declares that, so a plain byte
  pipe doesn't implement-and-raise it — `Transport`'s own raise is only the
  backstop for a caller that ignored the flag.

**One mock ECU** (`transport/mock_obd.py`). `MockObdTransport` is what `--mock`
builds, and it emulates the real bike observed over the cable: 5-baud init, key
bytes `08 08`, and — with no `dtcs=` — one stored `P1108` with MIL on. That
single-fault default is the deterministic ground truth the tests assert against;
if you change the client, update this mock to match and vice versa. The
**`--mock` CLI seeds it with a random, type-varied fault set** from
`DtcDatabase.random_dtcs` (see the DTC-decoding section) so a demo run shows a
plausible spread rather than one canned code — hence its Mode 03 serves *every*
stored DTC, never capping the list, so a >3-fault read still reconciles against
the Mode 01 PID 01 count. **It frames like a real ECU, because the client now
validates framing**: `_emit` rejects a payload over `MAX_DATA_BYTES` outright
(emitting one would be the mock inventing framing no bike produces), so a longer
answer goes out as several back-to-back frames — three DTC pairs per Mode 03
frame with the last padded out, and Mode 09 as numbered `49 <pid> <seq> <4
bytes>` fragments (the 17-character VIN front-padded to a whole frame, as J1979
specifies). It also serves
**placeholder** ECU identity (Mode 09) so identification is testable — those
VIN/calibration strings are invented, not real-bike facts — and answers
**live-data** requests (Phase 3) with plausible, *moving* values from
`transport/_mock_live.py`, whose encoders are the inverse of the
`obd_sensors.json` formulas; keep the two in sync. An unmodelled PID gets no
reply, so `read_live` omits it.

**DTC decoding (`protocol/dtc.py`) has one labelling scheme.**
`decode_dtc_bytes(hi, lo)` is the structural SAE J2012 decode: `P/C/B/U` from
the top two bits of the high byte, then four hex digits — how OBD Mode 03/07
responses encode a code, and the only scheme TrECU reads. (A `family=` parameter
used to select a raw non-J2012 labelling for Keihin `0x18` responses; it went
with the KWP path, along with the 134 `K` and 8 `L` codes in the database that
no structural decode could ever produce.) Descriptions come from
`data/triumph_dtc.json` — a flat `{code: description}` map imported wholesale
from the official-service-manual wording in a community-sourced extract (415 codes:
360 `P`, 30 `C`, 25 `U`). Codes vary by model/year — extend
the JSON, don't hardcode; an unknown code still decodes and shows a generic
message. `encode_dtc_code` is the inverse of the *structural* decode (`"P1108"`
→ `(0x11, 0x08)`; only `P/C/B/U` with a first digit `0-3` round-trip, else
`ValueError`), and `DtcDatabase.random_dtcs` uses it to draw a random,
family-varied set of real DB codes as byte pairs — the seed for the random
`--mock` fault set.

**Sensor decoding (`protocol/pids.py`)** is the Phase 3 parallel to `dtc.py`: it
turns a PID's raw data bytes into a named, unit-bearing `SensorReading` using
the model-value table `data/obd_sensors.json`. Each PID carries a **formula**
— an expression over the data bytes `A, B, C, D` (A = first byte, big-endian) as
SAE J1979 writes it — evaluated by a tiny arithmetic interpreter (`compile_formula`)
restricted to `+ - * /`, unary sign, parens, and those four names; **never Python
`eval`**. A bad formula raises `FormulaError` at *load*, not mid-poll.
`obd_sensors.json` is a flat `{hex-pid: entry}` map of the standardized OBD PIDs,
loaded into `PidDatabase` as `{int pid -> PidDef}`. (There used to be a second,
community-reverse-engineered Keihin channel table decoded from one packed frame,
with a `_SensorTable` base and a `frame_offset` field to share the load/lookup
half between them — all removed with the KWP path.)
`DiagnosticService.read_live(pids=None)` runs the client's `read_live` under
`_io_lock`, then decodes outside the lock into ordered `SensorReading`s
(dropping any PID the ECU didn't answer or the table can't decode); `None`
means `DEFAULT_LIVE_PIDS`.

**TUI layout (`tui/app.py`) uses Textual's one-row `Header` title bar over a
`TabbedContent` body.** The title bar shows the app name and version together
with a colored liveness dot + state label (`disconnected`/`connecting`/`reading`/
`clearing`/`connected`/`error`, keyed off `_CONNECTION_STATES`, hex-colored;
grey when disconnected, yellow while connecting, dim green connected, bright
green during a read/clear, red on error). The **Faults tab itself is the fault
indicator** — `_mark_faults_tab` toggles a `-has-faults` class (CSS `color:
$error; text-style: bold`) on the tab so its label turns red whenever the last
read found stored codes (there is no separate MIL lamp in the title bar). The
body has four
tabs: **Dashboard** (three summary `Static` cards — Faults, Connection, ECU
identity), **Faults** (the DTC `DataTable`, always visible — with no codes it
just shows its column headers and no rows; the "no faults" wording lives on the
Dashboard's Faults card, not a separate widget swap), **Live Data** (the Phase 3
streaming table — sensor / value / unit / running min / max / trend
sparkline), and **Log** (the raw protocol `RichLog`; error lines are red, and the
app auto-switches here under `--debug` and on error).
Footer bindings are *contextual* via `check_action`: `r` Read shows on
Dashboard/Faults, `c` Clear on Faults only, `space` Freeze on Live Data only;
`←`/`→` step tabs (app-level `priority=True` bindings, because `TabbedContent`'s
own arrow bindings are hidden — but because they're priority they'd otherwise
fire *through* a modal, so `_step_tab` no-ops while a modal owns the screen).
On each tab switch `_focus_active_tab` focuses
that tab's primary control (`_TAB_FOCUS` maps each tab to a single selector —
Dashboard→`#card-faults`, Faults→`#dtcs`, Live Data→`#live`, Log→`#log`) so the
row cursor / scroll lands where the user is looking; it no-ops only when a modal
owns focus. Because every tab's focus target is always visible (the DTC table is
never hidden), focus always lands inside the newly active pane — there's no
hidden-widget strand for `TabbedContent`'s focus-follows-pane handler to snap
back from. While the Live Data poll loop runs the dot reads `streaming...`
(bright green) or `frozen` (blue).

**`app.py` holds only app logic; the views it isn't are their own modules.**
The four modal screens live in `tui/screens.py` (`ConfirmScreen`,
`ConnectingScreen`, `ConnectErrorScreen`) and `tui/port_select.py`
(`PortSelectScreen`) — each a pure dialog that reports its outcome back and owns
no ECU operation. The Live Data table is a `DataTable` subclass,
`LiveTable` (`tui/live_table.py`), owning *everything* about how a snapshot is
displayed: its columns (fixed-width numerics so values don't jitter, auto-width
name/trend), the per-sensor `_Stats` (running min/max + a `_HISTORY`-deep
`deque`), the `sparkline()` block-glyph ramp, and its own `DEFAULT_CSS`. The app
hands it decoded readings (`update_readings`) and tells it when a fresh stream
starts (`reset`); rows update **in place** keyed by PID, so a PID the ECU skips
in one snapshot keeps its last row and the row cursor doesn't jump. Numbers are
formatted by the shared `pids.format_value` — the same helper behind
`SensorReading.formatted()`, so a reading and its derived min/max round
identically. Keep new presentation logic in these modules, not in `app.py`.

**The session lives in `SessionController` (`tui/session.py`), not in the app.**
That module is deliberately **Textual-free**: it owns the
*one* long-lived `DiagnosticService` (`_ecu.session`), is the only place the TUI
constructs one (`build_service`), and holds the **single connect path** — an
`async connect()` whose blocking work runs in `asyncio.to_thread`, returning a
`ConnectResult` of `CONNECTED` / `CANCELLED` / `FAILED`. Connected once and held
open with a background keepalive ticker, it's reused by re-reads, clears, *and*
live polls instead of re-initialising the K-line per keypress; a failed operation
calls `_ecu.close()` so the next connect starts clean, and `on_unmount` closes it
on exit (`shutdown()` on the exit-without-a-port path force-closes an in-flight
attempt too). Both entry points — `action_read` and the live-poll worker — go
through the app's thin `_connect_with_modal`, so **entering Live Data while
disconnected gets the same spinner, Cancel, and port-picker fallback as a Read**
(they used to diverge: the poll loop connected silently and blocked). Concurrent
callers share one attempt rather than opening the port twice.

A **fresh** connect (`_ecu.connected` false) runs behind a `ConnectingScreen`
modal — a standard `LoadingIndicator` spinner + Cancel button, raised by the
controller's one-shot `on_start` hook; re-reads over the held session skip it.
The modal names the **target port** and, on a fixed line, what it is doing on it
(`_CONNECT_DETAIL`, "ISO 9141-2 · 5-baud init..."). That line used to be live —
the service took a `progress` callback that fired per candidate so the
auto-sweep's progression showed — but with one protocol there is nothing to
report, and the init's retries all happen behind it. Because the blocking
connect runs off the event loop in `asyncio.to_thread`, it **can't be interrupted
cleanly**, but it *is* blocked in serial I/O — so Cancel (`_request_cancel_connect`
→ `SessionController.cancel`) **force-closes the in-flight service** to unblock
that read and release the port, drops the modal, and **hands straight back to the
port picker** (when a port lister is configured; the ready state otherwise)
*without* waiting for the thread — a 5-baud init working through its retry
budget can take many seconds, so waiting would make Cancel feel dead. Cancel also **detaches** the
attempt immediately, so a connect requested *after* it starts fresh instead of
inheriting the doomed outcome; each attempt therefore carries its **own** cancel
flag (`_Attempt`), since the abandoned one keeps running and must discard itself
rather than publish over a newer session. Crucially a service becomes
`_ecu.session` **only on full success**, so a re-picked (even different)
port always gets a clean, non-overlapping session. A connect that *fails* (all
protocol candidates refused) surfaces a `ConnectErrorScreen` modal — the error
text + an OK button — via `_on_connect_error`; dismissing it (`_on_connect_error_ack`)
**hands back to the port picker** (or the ready state without a lister), the same
fallback Cancel uses, so the user can pick a different port and retry. This is
distinct from `_on_error` (a read/clear/live failure over an *already-established*
session), which just tears the session down and shows the Log — a connect failure
blocks the whole session, so it gets a modal that routes back to port selection.

**TUI threading:** Textual is async but the protocol stack is blocking. The app
runs reads/clears via `asyncio.to_thread` inside `@work` workers, and the
protocol logger uses `call_from_thread` to marshal log lines back to the UI
thread. Don't call blocking transport code directly on the event loop.
**Live-data polling (Phase 3)** is a `set_interval` timer created paused and
resumed only while the Live Data tab is active (`_sync_live_polling` →
`_start_live_polling`/`_stop_live_polling`, tracked by `_live_running` so only a
real stopped→running transition resets the table — the "active view *is* what
the session is doing" model); each tick kicks a
`@work(group="live")` reader (guarded by `_live_busy` so ticks can't stack) that
connects if needed (behind the shared modal) and calls `read_live` off-thread. A
cancelled or failed connect there **stops** the loop rather than re-attempting
every tick behind the modal; a later successful read restarts it (`action_read`
re-syncs). It replaces nothing — the one-shot Read worker
(`group="ecu"`) still handles DTCs — but both funnel through the service's
`_io_lock`, so a poll and a Read serialize on the single wire.

## Protocol values vary by model — this is a real constraint

Triumph diagnostics were community-reverse-engineered; addresses and timings
differ across Keihin vs Sagem ECUs and model years — some Sagem models are
reported to use the 5-baud init address `0x43` rather than the OBD-standard
`0x33`. Every such value lives in the one `@dataclass` config, `Iso9141Config`,
with documented defaults, overridable via CLI flags (`--init-address`,
`--timeout`). When a value might differ per bike, add it to that config rather
than inlining a constant. **The response header is a per-bike value too**:
`response_format` / `response_target` are fixed by ISO 9141-2 (`48 6B`), but the
third byte is the answering module's own address — `0xD1` on the tested Triumph,
`0x10`/`0x11` on many cars. That is why `ecu_address` defaults to `None`
(*latch* whichever module answers first) instead of hardcoding `0xD1`: strict
addressing without demanding the user know their ECU's address. Set it to pin
one module from the first request.

**One config, one section.** `DiagnosticService(config=...)` takes an
`Iso9141Config` or `None` (all defaults) — there is no `EcuConfig` wrapper or
`as_ecu_config()` normalizer any more; both existed only to carry an iso9141
*and* a kwp2000 section through a per-attempt protocol sweep. In the CLI the two
flags parse as `None` when unset, meaning "leave the config's documented default
alone", and `_make_transport` builds the **mock ECU from the same config**, so
an override moves the simulated ECU and the tester together.

## Known real-hardware facts (from a live bike, not derivable from code)

- The tester's bike — a **Triumph Bonneville 865 EFI (2009)**, the *only* bike
  trecu has been tested against — is on `/dev/cu.usbserial-3` and is a
  **Sagem-style ECU requiring 5-baud SLOW init** (ISO 14230 fast-init got no
  data when it was still implemented). After the handshake it speaks **standard
  OBD-II over ISO 9141-2** (header `68 6A F1` out / `48 6B D1` in), not a
  proprietary protocol — which is why ISO 9141-2 is the *only* path TrECU ships.
- Live-confirmed read: one stored `P1108` (ambient-pressure sensor) with MIL on.
- The ECU needs a few seconds to settle between back-to-back 5-baud init
  attempts; `Iso9141Client` retries (`init_retries`, `retry_wait`) cover this.
- **The 5-baud init is timing-flaky on macOS**: roughly half of first attempts
  return garbled key bytes (e.g. `08 00`, `08 88`) because the FTDI break-toggle
  timing is coarse. `_slow_init` therefore **validates the handshake** — it
  requires the ECU's inverted-address reply (`0xCC` for init address `0x33`) and
  rejects a garbled/incomplete init so the `connect()` retry loop tries again,
  rather than proceeding on a half-open link. A validated init is `08 08`/`CC`;
  observed to recover to a good read within one or two retries, every time.
  To reduce the flakiness at the source, `KLineSerialTransport.five_baud_init`
  drives a carefully-matched waveform: 100 ms pre-init settle + one full
  bit-period of guaranteed idle-high before the start bit (11 bit-periods,
  ~2.2 s), absolute-deadline bit scheduling, and break-ioctl calls only on
  actual line-level changes.
- **This ECU answers OBD Mode 03 only while the fault is currently latched**
  (MIL on). When latched it returns `43 11 08 …` → `P1108`; when the sensor
  reads OK the MIL clears and Mode 03 goes *silent* (no `43` frame at all). By
  contrast **Mode 01 PID 01 (MIL + DTC count) answers reliably** — so it is the
  authority. `read_dtcs` reads PID 01 first, uses its count to drive a Mode 03
  retry/reconcile, and **raises** if the count says codes exist but Mode 03 will
  not enumerate them. A bare Mode-03 timeout must never be reported as "no
  codes" — that false-negative is exactly what made reads look flaky.

## K-line protocol reference

The single-wire K-line **echoes** everything the tester transmits; the client
reads back exactly that many bytes and *checks* them (`_consume_echo`) — an
exact match, optionally behind leading noise, is discarded as the echo, and
anything else is handed on as inbound traffic rather than thrown away, so a
missing or garbled echo can't swallow the ECU's reply. Each DTC is
decoded per **SAE J2012** (`P/C/B/U` + 4 hex) and looked up in
`data/triumph_dtc.json`. One path:

**ISO 9141-2 + OBD (`iso9141`, the confirmed Triumph case):**

1. **5-baud slow init** at address `0x33` → ECU replies with sync `0x55` + key
   bytes; the tester answers with the inverted key byte and the ECU returns the
   inverted address. The client **requires** that inverted-address byte to
   accept the session (a garbled 5-baud frame otherwise looks "connected").
2. **Mode 09** → vehicle info (PID 02 VIN, 04 calibration ID, 0A ECU name), each
   reassembled from its numbered `49 <pid> <seq> <4 bytes>` fragments.
3. **Mode 01 PID 01** → MIL status + DTC count (the reliable authority), read
   *first*; then **Mode 03** (`68 6A F1 03`) → stored DTCs (retried and
   reconciled against the count, three pairs per frame); **Mode 07** → pending
   (best-effort).
4. **Mode 01** per PID → live sensor data (Phase 3); **Mode 04** → clear codes.

Every response frame is `48 6B <ecu> <mode+0x40> <data…> <cs>`, at most
`MAX_DATA_BYTES` (7) of data field, and is validated on all of those before it
is decoded — see the "Nothing reaches a decoder unvalidated" section above for
what gets rejected and why.
