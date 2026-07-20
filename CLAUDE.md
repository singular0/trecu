# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`trecu` is a macOS TUI (Textual) that reads and decodes ECU fault codes (DTCs)
from Triumph motorcycles over a cheap KKL / FT232RL K-line cable. It speaks the
two protocols Triumphs use, decodes DTCs per SAE J2012, and can clear them.
`README.md` documents the user-facing behavior and the K-line protocol details;
read it before touching the protocol layer.

## Commands

This directory is **not a git repo** and Python is a mise-managed 3.11 in
`.venv`. Always drive the venv explicitly:

```bash
./.venv/bin/python -m pytest              # full suite (23 tests, ~4s, no hardware)
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
`clear_dtcs()`, and `stop_communication()`. `iso9141.py` imports the *shared*
types (`ConnectionInfo`, `Logger`, `ProtocolError`) *from* `kwp2000.py` — so
kwp2000 is effectively the home of the common protocol vocabulary even though
the two speak entirely different wire protocols. Keep any new shared type there.

**`DiagnosticService` is the only place that knows about protocol selection.**
`protocol="auto"` (the default) tries `iso9141` then `kwp-fast` in order,
building a fresh client per attempt and keeping the first that `connect()`s.
`iso9141` is first because it's the confirmed real-Triumph path (5-baud slow
init + OBD-II). A caller can also inject a pre-built `client=` to bypass
selection entirely (used by tests).

**Transports advertise capabilities via class flags**, and the protocol layer
branches on them rather than on concrete types:
- `echoes` — single-wire K-line reflects every TX byte into RX; the client
  discards that echo before parsing. Real serial echoes; mocks don't.
- `supports_fast_init` / `supports_slow_init` — a client refuses to `connect()`
  over a transport that can't do its init. `MockObdTransport` is slow-init only;
  `MockKLineTransport` is fast-init only; `KLineSerialTransport` does both.

**Two mock ECUs, one per protocol path** (`transport/mock_obd.py`,
`transport/mock.py`). `MockObdTransport` is the default `--mock` and emulates the
real bike observed over the cable: 5-baud init, key bytes `08 08`, one stored
`P1108` with MIL on. It's the ground truth for the iso9141 path — if you change
that client, update this mock to match and vice versa.

**KWP2000 framing (`protocol/framing.py`) is pure and transport-independent** —
build/parse ISO 14230 frames, checksum, incremental length hints. Test it in
isolation; it has no I/O.

**DTC decoding (`protocol/dtc.py`)** turns `(hi, lo, status)` triples into SAE
J2012 codes (`P/C/B/U` + 4 hex) and looks up descriptions in
`data/triumph_dtc.json`. Generic `P0xxx` codes are standardized; `P1xxx`/`P2xxx`
are Triumph-specific and vary by model/year — extend the JSON, don't hardcode.

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
