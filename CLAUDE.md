# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`trecu` is a cross-platform TUI (Textual) that reads and decodes ECU fault codes (DTCs)
from Triumph motorcycles over a cheap KKL / FT232RL K-line cable. It speaks the
two protocols Triumphs use, decodes DTCs per SAE J2012, and can clear them.
`README.md` is the user-facing guide; the byte-level K-line handshake lives in
the **K-line protocol reference** section below — read it before touching the
protocol layer.

The KWP2000 framing, fast-init, DTC services, and SAE J2012 decoding are all
hand-rolled here on purpose: a KKL cable is a *dumb* FTDI serial adapter, **not**
an ELM327, so `python-OBD` does not apply, and there is no maintained general
Python KWP2000 client for a raw K-line. Dependencies are kept small and to
well-maintained libraries (see `pyproject.toml`): `textual` (TUI), `rich`
(formatting, via textual), `pyserial` (FT232RL VCP access), and optional
`pyftdi` (direct FTDI/libusb bit-bang init).

## Commands

Python is a mise-managed 3.11 in `.venv`. Always drive the venv explicitly:

```bash
./.venv/bin/python -m pytest              # full suite (88 tests, ~21s, no hardware)
./.venv/bin/python -m pytest tests/test_iso9141_obd.py::test_obd_read_decode_clear_cycle
./.venv/bin/trecu --mock                  # launch the TUI against a simulated ECU
./.venv/bin/trecu --mock --read           # headless read + print + exit
./.venv/bin/trecu --mock --live           # headless live-sensor snapshot + exit
./.venv/bin/trecu --list-ports            # find a real cable's /dev/cu.usbserial-*
```

Tests run **entirely against the in-memory mock ECUs** — never require hardware,
and any new test must follow suit. Use `-v` on the CLI to dump raw byte traffic
when debugging a protocol.

## Releasing

The package **version is not stored in `pyproject.toml`** — it's derived from the
git tag at build time by `hatch-vcs` (`[tool.hatch.version] source = "vcs"`), so
`trecu.__version__` (and `trecu --version`) read it back from installed metadata
via `importlib.metadata`. To cut a release, push a **semver tag** (`vMAJOR.MINOR.PATCH`,
optionally `-prerelease`/`+build`): `.github/workflows/release.yml` fires **only**
on those tags. It first runs the **mock-only test suite as a gate** (a `test` job
the build `needs`), and only if that passes builds the sdist + wheel (whose version
therefore equals the tag) and publishes them as assets on a **GitHub Release**
(created via the `gh` CLI, no third-party action; a `-prerelease` tag is marked
pre-release). Non-semver tags
are ignored by the tag filter, and a strict-semver guard step fails anything that
slips through. There is **no PyPI publish** — install is from the release wheel
URL (see README).

## Architecture

Four layers, top to bottom. Adding a protocol or transport means slotting into
one of these seams, not rewiring the app:

```
CLI (cli.py) / TUI (tui/app.py)
        │
DiagnosticService (service.py)   ← owns lifecycle; picks the protocol
        │
Iso9141Client | Kwp2000Client    ← protocol/*.py; duck-typed, interchangeable
        │
Transport (transport/base.py)    ← half-duplex byte pipe: serial or mock
```

**The two protocol clients are duck-typed peers, not a class hierarchy.** Both
expose `connect() -> ConnectionInfo`, `read_dtcs() -> list[(hi, lo, status)]`,
`read_identification() -> EcuInfo`, `read_live(pids) -> dict[pid, data_bytes]`,
`clear_dtcs()`, `keepalive()`, and `stop_communication()`. `read_live()` polls
live sensors (Phase 3): iso9141 sends one OBD **Mode 01** request per PID; KWP
uses **ReadDataByLocalIdentifier** (0x21). Both return *raw* data bytes per
requested id (an id the ECU doesn't answer is simply omitted) — decoding to
physical values is the service's job via the sensor-decode layer, not the
client's. Each client also carries two decode-steering attributes the service
reads: `live_source` (`"obd_mode01"` vs `"kwp_local"`) names which decode table
(`obd_sensors.json` vs `keihin_sensors.json`) decodes its live data — on the KWP
path the service requests the **one packed `21 80` frame** (the Keihin
MODE_READ_SENSORS RLI) and splits it per the `kwp_local` channel table — and
`dtc_family` (`None` vs a
letter like `"K"`) selects the DTC labelling scheme, because Keihin `0x18`
responses carry **raw fault numbers that are not SAE-J2012 bit-encoded**.
`keepalive()` holds a persistent session open (F1):
KWP sends `TesterPresent` (0x3E, response suppressed), iso9141 has no such
service so it pokes the link with a cheap read-only Mode 01 PID 00.
`read_identification()` is best-effort (OBD Mode 09 / KWP ReadEcuIdentification
0x1A) — a missing reply yields empty fields, not an error. `iso9141.py` imports
the *shared* types (`ConnectionInfo`, `EcuInfo`, `Logger`, `ProtocolError`,
`decode_identification_ascii`) **and shared service logic**
(`slow_init_handshake` — the validated 5-baud handshake both init paths use —
and `parse_obd_dtc_pairs` + `STATUS_CONFIRMED`/`STATUS_PENDING` for OBD Mode
03/07 responses) *from* `kwp2000.py` — so kwp2000 is effectively the home of
the common protocol vocabulary even though the two speak entirely different
wire protocols. Keep any new shared type or shared helper there.

**`DiagnosticService` is the only place that knows about protocol selection.**
`protocol="auto"` (the default) tries `iso9141` → `kwp-slow` → `kwp-fast` in
order (the same sweep other K-line Triumph tools walk), building a fresh
client per attempt and keeping the first that `connect()`s. `iso9141` is first
because it's the confirmed real-Triumph path (5-baud slow init + OBD-II);
`kwp-slow` is `Kwp2000Client` with `init_mode="slow"` (5-baud init at the ECU
address `0xD5`, the Keihin K-line fallback — the service pins `init_mode`
per attempt via `dataclasses.replace`). A caller can also inject a pre-built
`client=` to bypass selection entirely (used by tests). An optional `progress`
callback fires with each candidate label *before* it's probed, so a UI can show
which protocol the sweep is currently trying (the TUI's connecting modal does).

**Two lifecycle modes.** One-shot (`with service:` → `open`/`close`) still
connects lazily on the first operation — that's the CLI's `--read`/`--clear`
path. **Persistent (F1)** is `service.session()` (a context manager) or
`start_session()`: it connects *up front* and runs a background keepalive
ticker (`_Keepalive`, a daemon thread) sending `client.keepalive()` every
`DEFAULT_KEEPALIVE_INTERVAL` (2 s) so the ECU doesn't drop the session while
idle — pass `keepalive_interval=0` to disable it. Because the K-line is
half-duplex, every operation *and* every keepalive beat runs under one
`_io_lock`, so a beat can never interleave with a read/clear. This is the seam
that Phase 3 live-polling / Phase 5 actuator tests build on.

**Transports advertise capabilities via class flags**, and the protocol layer
branches on them rather than on concrete types:
- `echoes` — single-wire K-line reflects every TX byte into RX; the client
  discards that echo before parsing. Real serial echoes; mocks don't.
- `supports_fast_init` / `supports_slow_init` — a client refuses to `connect()`
  over a transport that can't do its init. `MockObdTransport` is slow-init only;
  `MockKLineTransport` is fast-init only *by default* (pass
  `supports_slow_init=True` to emulate the Keihin 5-baud init for `kwp-slow`);
  `KLineSerialTransport` does both.

**Two mock ECUs, one per protocol path** (`transport/mock_obd.py`,
`transport/mock_kline.py`). `MockObdTransport` is the default `--mock` and emulates the
real bike observed over the cable: 5-baud init, key bytes `08 08`, and — with no
`dtcs=` — one stored `P1108` with MIL on. That single-fault default is the
deterministic ground truth for the iso9141 path (tests assert it; if you change
that client, update this mock to match and vice versa), but the **`--mock` CLI
seeds each mock with a random, type-varied fault set** from
`DtcDatabase.random_dtcs` (see the DTC-decoding section) so a demo run shows a
plausible spread rather than one canned code — hence the OBD mock's Mode 03
serves *every* stored DTC (padded to a 3-pair frame when fewer), never capping
at three, so a >3-fault read still reconciles against the Mode 01 PID 01 count.
`MockKLineTransport`
mirrors the community-documented Keihin K-line ECU (address `D5`/`F5`, DTCs via
OBD Mode 03 over KWP framing *and* legacy `0x18`, AccessTimingParameter
recorded in `timing_params`, ident on the Keihin RLIs) — same sync rule vs.
`Kwp2000Client`/`Kwp2000Config`. Both mocks also serve
**placeholder** ECU identity (Mode 09 / RLI records) so identification is
testable — those VIN/calibration strings are invented, not real-bike facts.
Both also answer **live-data** requests (Phase 3) with plausible, *moving*
values from `transport/_mock_live.py`, whose encoders are the inverse of the
`obd_sensors.json` / `keihin_sensors.json` formulas — keep them in sync. The OBD mock answers per-PID
(an unmodelled PID gets no reply, so `read_live` omits it); the K-line mock
serves only LID `0x80` — one packed frame in the draft `kwp_local` layout
(`kwp_live_frame`), a handful of channels moving and the rest zero — and
rejects any other record. Its default DTC triples are J2012-encoded for the
Mode 03 path; tests for the `0x18` path pass Keihin-style raw fault numbers
via `dtcs=` instead.

**KWP2000 framing (`protocol/framing.py`) is pure and transport-independent** —
build/parse ISO 14230 frames, checksum, incremental length hints. Test it in
isolation; it has no I/O.

**DTC decoding (`protocol/dtc.py`) is source-aware.** `decode_dtc_bytes(hi, lo,
family=None)` has two labelling schemes: `family=None` is the structural SAE
J2012 decode (`P/C/B/U` from the top two bits — correct for OBD Mode 03/07,
including Mode 03 over KWP framing), while a family letter (`"K"`) prepends it
to the four *raw* hex digits — the community labelling convention for Keihin `0x18`
ReadDTCByStatus responses, whose fault numbers are **not** J2012 bit-encoded
(bytes `15 35` are `K1535`, not `P1535`). The service passes each client's
`dtc_family` into `DtcDatabase.decode_all`; `Kwp2000Config.dtc_family`
(default `"K"`) applies only on the `0x18` path. Descriptions come from
`data/triumph_dtc.json` — a flat `{code: description}` map imported wholesale
from the official-service-manual wording in a community-sourced extract (557 codes:
360 `P`, 134 `K`, 30 `C`, 25 `U`, 8 `L`). Codes vary by model/year — extend
the JSON, don't hardcode; an unknown code still decodes and shows a generic
message. `encode_dtc_code` is the inverse of the *structural* decode (`"P1108"`
→ `(0x11, 0x08)`; only `P/C/B/U` with a first digit `0-3` round-trip, else
`ValueError`), and `DtcDatabase.random_dtcs` uses it to draw a random,
family-varied set of real DB codes as byte pairs — the seed for the random
`--mock` fault set.

**Sensor decoding (`protocol/pids.py`)** is the Phase 3 parallel to `dtc.py`: it
turns a PID's raw data bytes into a named, unit-bearing `SensorReading` using
two model-value tables under `data/`, one per live path (F2). Each PID carries a **formula**
— an expression over the data bytes `A, B, C, D` (A = first byte, big-endian) as
SAE J1979 writes it — evaluated by a tiny arithmetic interpreter (`compile_formula`)
restricted to `+ - * /`, unary sign, parens, and those four names; **never Python
`eval`**. A bad formula raises `FormulaError` at *load*, not mid-poll.
`obd_sensors.json` holds the standardized OBD PIDs (the confirmed path), a flat
`{hex-pid: entry}` map loaded as `PidDatabase`; `keihin_sensors.json` is a
community-reverse-engineered 53-channel Keihin table loaded as a **separate**
`KwpLocalTable` (the service holds it as `self.kwp_local`, alongside `self.pids`):
channel keys are *decimal* indices from that table, each entry carries
`frame_offset`/`bytes` locating it inside the one packed `21 80` frame, and
`decode_frame` splits such a frame into readings.
**The kwp_local layout and divisors are a DRAFT**:
names/kind/decimals/offset/fullscale come from that community-sourced data, but the real
per-channel divisors and frame byte offsets need an F4 hardware capture —
fixing them is a data-only JSON edit. `DiagnosticService.read_live(pids=None)`
runs a client's `read_live` under `_io_lock`, then decodes outside the lock
into ordered `SensorReading`s (dropping any id the ECU didn't answer or the
table can't decode); on the OBD path `None` means `DEFAULT_LIVE_PIDS`, on the
KWP path `pids` are channel indices and `None` means every channel.

**TUI layout (`tui/app.py`) is a one-row session "spine" over a
`TabbedContent` body.** The spine shows the brand on the left and, right-aligned,
a colored liveness dot + state label (`disconnected`/`connecting`/`reading`/
`clearing`/`connected`/`error`, keyed off `_SPINE`, hex-colored; grey when
disconnected, yellow while connecting, dim green connected, bright green during
a read/clear, red on error). The **Faults tab itself is the fault
indicator** — `_mark_faults_tab` toggles a `-has-faults` class (CSS `color:
$error; text-style: bold`) on the tab so its label turns red whenever the last
read found stored codes (there is no separate MIL lamp in the spine). The body
has three
tabs: **Dashboard** (three summary `Static` cards — Faults, Connection, ECU
identity), **Faults** (the DTC `DataTable`, always visible — with no codes it
just shows its column headers and no rows; the "no faults" wording lives on the
Dashboard's Faults card, not a separate widget swap), **Live Data** (the Phase 3
streaming `DataTable` — sensor / value / unit / running min / max / trend
sparkline), and **Log** (the raw protocol `RichLog`; error lines are red, and the
app auto-switches here under `-v` and on error).
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
back from. **The session is now mechanism, not just framing
(roadmap F1 is done):** the app owns *one* long-lived `DiagnosticService`, built
lazily on the first read (`_ensure_session`), connected once and held open with
a background keepalive ticker; re-reads and clears reuse it instead of
re-initialising the K-line per keypress. A failed operation tears the session
down (`_close_session`) so the next read reconnects cleanly; `on_unmount` closes
it on exit. While the Live Data poll loop runs the dot reads `streaming...`
(bright green) or `frozen` (blue).
A **fresh** connect (session is `None`) runs behind a `ConnectingScreen` modal —
a standard `LoadingIndicator` spinner + Cancel button (`_connect_with_modal`);
re-reads over the held session skip it. The modal names the **target port** and
the **protocol currently being probed**: the service takes a `progress` callback
that fires with each candidate label *before* it's tried, and the app marshals
it onto the UI thread (`_on_connect_probe` → `set_probing`) so the auto-sweep's
`iso9141 → kwp-slow → kwp-fast` progression is visible live. Because the blocking
connect runs off the event loop in `asyncio.to_thread`, it **can't be interrupted
cleanly**, but it *is* blocked in serial I/O — so Cancel (`_request_cancel_connect`,
`_cancelled_connect`) **force-closes the in-flight service** to unblock that read
and release the port, drops the modal, and **hands straight back to the port
picker** (when a port lister is configured; the ready state otherwise) *without*
waiting for the thread — which on a slow `auto` init sweep can be many seconds,
so waiting would make Cancel feel dead. To get that handle, `_connect_with_modal`
builds the `DiagnosticService` on the UI thread (not in the worker) and stashes
it as `_connecting_service`. The doomed connect then finishes into a closed
transport and `_connect_with_modal` discards it; crucially the service is
published as `_session` **only on full success**, so a re-picked (even different)
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
resumed only while the Live Data tab is active (`_sync_live_polling` — the
"active view *is* what the session is doing" model); each tick kicks a
`@work(group="live")` reader (guarded by `_live_busy` so ticks can't stack) that
calls `read_live` off-thread. It replaces nothing — the one-shot Read worker
(`group="ecu"`) still handles DTCs — but both funnel through the service's
`_io_lock`, so a poll and a Read serialize on the single wire.

## Protocol values vary by model — this is a real constraint

Triumph diagnostics were community-reverse-engineered; addresses,
session sub-functions, and DTC services differ across Keihin vs Sagem ECUs and
model years. Every such value lives in a `@dataclass` config (`Iso9141Config`,
`Kwp2000Config`) with documented defaults, overridable via CLI flags
(`--init-address`, `--ecu-address`, …). When a value might differ per bike, add
it to the config rather than inlining a constant.

## Known real-hardware facts (from a live bike, not derivable from code)

- The tester's bike — a **Triumph Bonneville 865 EFI (2009)**, the *only* bike
  trecu has been tested against — is on `/dev/cu.usbserial-3` and is a
  **Sagem-style ECU requiring 5-baud SLOW init** (fast-init gets no data). After
  the handshake it speaks **standard OBD-II over ISO 9141-2** (header `68 6A F1`
  out / `48 6B D1` in), not proprietary KWP — hence `iso9141` is the default
  `auto` first choice.
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
discards that echo before parsing (see the `echoes` transport flag). Each DTC is
decoded per **SAE J2012** (`P/C/B/U` + 4 hex) — except Keihin `0x18` responses,
labelled `K` + raw hex (see the DTC-decoding section) — and looked up in
`data/triumph_dtc.json`. Three paths, auto-tried `iso9141` → `kwp-slow` →
`kwp-fast` (the standard Triumph K-line sweep):

**ISO 9141-2 + OBD (`iso9141`, the confirmed Triumph case):**

1. **5-baud slow init** at address `0x33` → ECU replies with sync `0x55` + key
   bytes; the tester answers with the inverted key byte and the ECU returns the
   inverted address. The client **requires** that inverted-address byte to
   accept the session (a garbled 5-baud frame otherwise looks "connected").
2. **Mode 09** → vehicle info (PID 02 VIN, 04 calibration ID, 0A ECU name).
3. **Mode 01 PID 01** → MIL status + DTC count (the reliable authority), read
   *first*; then **Mode 03** (`68 6A F1 03`) → stored DTCs (retried and
   reconciled against the count); **Mode 07** → pending (best-effort).
4. **Mode 01** per PID → live sensor data (Phase 3); **Mode 04** → clear codes.

**KWP2000 paths (`kwp-slow`, `kwp-fast`) — Triumph Keihin, community-derived
(addressing `D5`/`F5`, request headers `81/82 D5 F5`):**

1. **Init.** `kwp-slow`: 5-baud init at the **ECU address `0xD5`** (same
   waveform + inverted-address validation as iso9141's, via the shared
   `slow_init_handshake`); the handshake's key bytes *are* the session's — no
   StartCommunication follows. `kwp-fast`: K-line low 25 ms / high 25 ms via
   the UART break → **StartCommunication** (`0x81`) → key bytes.
2. **StartDiagnosticSession** `10 02`, then **AccessTimingParameter**
   `83 03 1E 02 0A 14 00` (P-timing 30/2/10/20/0) — both best-effort; a
   refusal is logged, not fatal.
3. **ReadEcuIdentification** (`0x1A`) on the community-documented Triumph RLIs
   `0xA0`/`0xAE`/`0x8C` (which record carries which field is unconfirmed on
   hardware — F4; standard `0x90/0x91/0x94` remain config overrides).
4. **DTCs:** OBD **Mode 03 over KWP framing** (`read_dtc_service=0x03`, the
   standard K-line default; response `43 <hi lo>…`, synthetic confirmed status,
   J2012-decoded) or legacy **ReadDTCByStatus** (`0x18`, real status bytes —
   and **raw Keihin fault numbers**, labelled with the `dtc_family` letter
   `K` instead of the J2012 bit-decode). **ReadDataByLocalIdentifier**
   (`21 80`, the Keihin MODE_READ_SENSORS RLI) → one packed frame carrying all
   live channels, split per the `kwp_local` table;
   **ClearDiagnosticInformation** (`0x14`, group `FF 00`) to clear.
