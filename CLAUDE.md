# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`trecu` is a macOS TUI (Textual) that reads and decodes ECU fault codes (DTCs)
from Triumph motorcycles over a cheap KKL / FT232RL K-line cable. It speaks the
two protocols Triumphs use, decodes DTCs per SAE J2012, and can clear them.
`README.md` documents the user-facing behavior and the K-line protocol details;
read it before touching the protocol layer.

## Commands

Python is a mise-managed 3.11 in `.venv`. Always drive the venv explicitly:

```bash
./.venv/bin/python -m pytest              # full suite (31 tests, ~10s, no hardware)
./.venv/bin/python -m pytest tests/test_iso9141_obd.py::test_obd_read_decode_clear_cycle
./.venv/bin/trecu --mock                  # launch the TUI against a simulated ECU
./.venv/bin/trecu --mock --read           # headless read + print + exit
./.venv/bin/trecu --list-ports            # find a real cable's /dev/cu.usbserial-*
```

Tests run **entirely against the in-memory mock ECUs** — never require hardware,
and any new test must follow suit. Use `-v` on the CLI to dump raw byte traffic
when debugging a protocol.

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
`read_identification() -> EcuInfo`, `clear_dtcs()`, `keepalive()`, and
`stop_communication()`. `keepalive()` holds a persistent session open (F1):
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

**KWP2000 framing (`protocol/framing.py`) is pure and transport-independent** —
build/parse ISO 14230 frames, checksum, incremental length hints. Test it in
isolation; it has no I/O.

**DTC decoding (`protocol/dtc.py`)** turns `(hi, lo, status)` triples into SAE
J2012 codes (`P/C/B/U` + 4 hex) and looks up descriptions in
`data/triumph_dtc.json`. Generic `P0xxx` codes are standardized; `P1xxx`/`P2xxx`
are Triumph-specific and vary by model/year — extend the JSON, don't hardcode.

**TUI layout (`tui/app.py`) is a one-row session "spine" over a
`TabbedContent` body.** The spine shows the brand on the left and, right-aligned,
a colored liveness dot + state label (`ready`/`connecting`/`reading`/`clearing`/
`connected`/`error`, keyed off `_SPINE`) plus a synthetic MIL lamp — a red dot
that lights only when the last read found stored faults. The body has three
tabs: **Dashboard** (three summary `Static` cards — Faults, Connection, ECU
identity), **Faults** (the DTC `DataTable` with a centered "no faults" empty
state), and **Log** (the raw protocol `RichLog`; error lines are red, and the
app auto-switches here under `-v` and on error). Footer bindings are *contextual*
via `check_action`: `r` Read shows on Dashboard/Faults, `c` Clear on Faults only;
`←`/`→` step tabs (app-level `priority=True` bindings, because `TabbedContent`'s
own arrow bindings are hidden). **The session is now mechanism, not just framing
(roadmap F1 is done):** the app owns *one* long-lived `DiagnosticService`, built
lazily on the first read (`_ensure_session`), connected once and held open with
a background keepalive ticker; re-reads and clears reuse it instead of
re-initialising the K-line per keypress. A failed operation tears the session
down (`_close_session`) so the next read reconnects cleanly; `on_unmount` closes
it on exit. The spine shows a green `⚡` keepalive lamp while a session is live.

**TUI threading:** Textual is async but the protocol stack is blocking. The app
runs reads/clears via `asyncio.to_thread` inside `@work` workers, and the
protocol logger uses `call_from_thread` to marshal log lines back to the UI
thread. Don't call blocking transport code directly on the event loop.

## Protocol values vary by model — this is a real constraint

Triumph diagnostics were community-reverse-engineered (see TuneECU); addresses,
session sub-functions, and DTC services differ across Keihin vs Sagem ECUs and
model years. Every such value lives in a `@dataclass` config (`Iso9141Config`,
`Kwp2000Config`) with documented defaults, overridable via CLI flags
(`--init-address`, `--ecu-address`, …). When a value might differ per bike, add
it to the config rather than inlining a constant.

## Known real-hardware facts (from a live bike, not derivable from code)

- The tester's bike is on `/dev/cu.usbserial-3` and is a **Sagem-style ECU
  requiring 5-baud SLOW init** (fast-init gets no data). After the handshake it
  speaks **standard OBD-II over ISO 9141-2** (header `68 6A F1` out / `48 6B D1`
  in), not proprietary KWP — hence `iso9141` is the default `auto` first choice.
- Live-confirmed read: one stored `P1108` (ambient-pressure sensor) with MIL on.
- The ECU needs a few seconds to settle between back-to-back 5-baud init
  attempts; `Iso9141Client` retries (`init_retries`, `retry_wait`) cover this.
