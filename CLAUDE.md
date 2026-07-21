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
./.venv/bin/python -m pytest              # full suite (58 tests, ~15s, no hardware)
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
uses **ReadDataByLocalIdentifier** (0x21). Both return *raw* data bytes per PID
(a PID the ECU doesn't answer is simply omitted) — decoding to physical values
is the service's job via the sensor-decode layer, not the client's. The KWP
`read_live` maps each id 1:1 to a single record as a **placeholder**; real
Triumph records pack several sensors per model-specific id and await a hardware
capture (F4). `keepalive()` holds a persistent session open (F1):
KWP sends `TesterPresent` (0x3E, response suppressed), iso9141 has no such
service so it pokes the link with a cheap read-only Mode 01 PID 00.
`read_identification()` is best-effort (OBD Mode 09 / KWP ReadEcuIdentification
0x1A) — a missing reply yields empty fields, not an error. `iso9141.py` imports
the *shared* types (`ConnectionInfo`, `EcuInfo`, `Logger`, `ProtocolError`,
`decode_identification_ascii`) *from* `kwp2000.py` — so
kwp2000 is effectively the home of the common protocol vocabulary even though
the two speak entirely different wire protocols. Keep any new shared type there.

**`DiagnosticService` is the only place that knows about protocol selection.**
`protocol="auto"` (the default) tries `iso9141` then `kwp-fast` in order,
building a fresh client per attempt and keeping the first that `connect()`s.
`iso9141` is first because it's the confirmed real-Triumph path (5-baud slow
init + OBD-II). A caller can also inject a pre-built `client=` to bypass
selection entirely (used by tests).

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
  `MockKLineTransport` is fast-init only; `KLineSerialTransport` does both.

**Two mock ECUs, one per protocol path** (`transport/mock_obd.py`,
`transport/mock_kline.py`). `MockObdTransport` is the default `--mock` and emulates the
real bike observed over the cable: 5-baud init, key bytes `08 08`, one stored
`P1108` with MIL on. It's the ground truth for the iso9141 path — if you change
that client, update this mock to match and vice versa. Both mocks also serve
**placeholder** ECU identity (Mode 09 / RLI records) so identification is
testable — those VIN/calibration strings are invented, not real-bike facts.
Both also answer **live-data** requests (Phase 3) with plausible, *moving*
values, drawn from one shared generator (`transport/_mock_live.py`) so the two
protocol paths behave identically; its byte encoders are the inverse of the
`triumph_pids.json` formulas — keep them in sync. An unmodelled PID gets no
reply, like a real ECU, so `read_live` omits it.

**KWP2000 framing (`protocol/framing.py`) is pure and transport-independent** —
build/parse ISO 14230 frames, checksum, incremental length hints. Test it in
isolation; it has no I/O.

**DTC decoding (`protocol/dtc.py`)** turns `(hi, lo, status)` triples into SAE
J2012 codes (`P/C/B/U` + 4 hex) and looks up descriptions in
`data/triumph_dtc.json`. Generic `P0xxx` codes are standardized; `P1xxx`/`P2xxx`
are Triumph-specific and vary by model/year — extend the JSON, don't hardcode.
The bundled DB covers the standardized generics plus Triumph codes sourced from
official service manuals (Daytona 675, Thunderbird, Street Twin); an unknown code
still decodes structurally and shows a generic message.

**Sensor decoding (`protocol/pids.py`)** is the Phase 3 parallel to `dtc.py`: it
turns a PID's raw data bytes into a named, unit-bearing `SensorReading` using the
model-value table in `data/triumph_pids.json` (F2). Each PID carries a **formula**
— an expression over the data bytes `A, B, C, D` (A = first byte, big-endian) as
SAE J1979 writes it — evaluated by a tiny arithmetic interpreter (`compile_formula`)
restricted to `+ - * /`, unary sign, parens, and those four names; **never Python
`eval`**. A bad formula raises `FormulaError` at *load*, not mid-poll. The
`obd_mode01` section holds the standardized OBD PIDs (the confirmed path); the
file is structured so a future model-specific `kwp_local` section slots in without
touching the loader. `DiagnosticService.read_live(pids=None)` runs a client's
`read_live` under `_io_lock`, then decodes the raw bytes into ordered
`SensorReading`s (dropping any PID the ECU didn't answer or the table can't
decode); `None` uses `DEFAULT_LIVE_PIDS`.

**TUI layout (`tui/app.py`) is a one-row session "spine" over a
`TabbedContent` body.** The spine shows the brand on the left and, right-aligned,
a colored liveness dot + state label (`ready`/`connecting`/`reading`/`clearing`/
`connected`/`error`, keyed off `_SPINE`) plus a synthetic MIL lamp — a red dot
that lights only when the last read found stored faults. The body has three
tabs: **Dashboard** (three summary `Static` cards — Faults, Connection, ECU
identity), **Faults** (the DTC `DataTable` with a centered "no faults" empty
state), **Live Data** (the Phase 3 streaming `DataTable` — sensor / value / unit
/ running min / max / trend sparkline), and **Log** (the raw protocol `RichLog`;
error lines are red, and the app auto-switches here under `-v` and on error).
Footer bindings are *contextual* via `check_action`: `r` Read shows on
Dashboard/Faults, `c` Clear on Faults only, `space` Freeze on Live Data only;
`←`/`→` step tabs (app-level `priority=True` bindings, because `TabbedContent`'s
own arrow bindings are hidden). **The session is now mechanism, not just framing
(roadmap F1 is done):** the app owns *one* long-lived `DiagnosticService`, built
lazily on the first read (`_ensure_session`), connected once and held open with
a background keepalive ticker; re-reads and clears reuse it instead of
re-initialising the K-line per keypress. A failed operation tears the session
down (`_close_session`) so the next read reconnects cleanly; `on_unmount` closes
it on exit. The spine shows a green `⚡` keepalive lamp while a session is live,
and reads `streaming N sensors` (or `frozen`) while the Live Data poll loop runs.

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

Triumph diagnostics were community-reverse-engineered (see TuneECU); addresses,
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

## K-line protocol reference

The single-wire K-line **echoes** everything the tester transmits; the client
discards that echo before parsing (see the `echoes` transport flag). Each DTC is
decoded per **SAE J2012** (`P/C/B/U` + 4 hex) and looked up in
`data/triumph_dtc.json`. Two paths, auto-tried `iso9141` then `kwp-fast`:

**ISO 9141-2 + OBD (`iso9141`, the confirmed Triumph case):**

1. **5-baud slow init** at address `0x33` → ECU replies with sync `0x55` + key
   bytes; the tester answers with the inverted key byte and the ECU returns the
   inverted address. Session open.
2. **Mode 09** → vehicle info (PID 02 VIN, 04 calibration ID, 0A ECU name).
3. **Mode 03** (`68 6A F1 03`) → stored DTCs; **Mode 07** → pending; **Mode 01
   PID 01** → MIL status + count.
4. **Mode 01** per PID → live sensor data (Phase 3); **Mode 04** → clear codes.

**KWP2000 fast path (`kwp-fast`):**

1. **Fast-init** (K-line low 25 ms / high 25 ms via the UART break) →
   **StartCommunication** (`0x81`) → key bytes → **ReadEcuIdentification**
   (`0x1A`) for VIN/part/version → **ReadDTCByStatus** (`0x18`);
   **ReadDataByLocalIdentifier** (`0x21`) for live data;
   **ClearDiagnosticInformation** (`0x14`) to clear.
