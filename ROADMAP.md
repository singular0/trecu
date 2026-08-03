# trecu roadmap

From "read fault codes" toward a full-featured diagnostic tool. Ordered by
increasing risk and increasing dependence on model-specific reverse engineering.
Each phase says what already exists, what new work lands in which architectural
seam (see `CLAUDE.md` for the four layers), and what gates it.

Effort key: **S** ≈ a session, **M** ≈ a few days, **L** ≈ a week+, **XL** ≈ multi-week / research-bound.

---

## Where we are today

Built and tested (181 tests, all against the mock ECUs):

- **Two duck-typed protocol clients** — `Iso9141Client` (5-baud slow init + OBD-II
  Modes 01/03/04/07/09) and `Kwp2000Client` (fast-init + KWP2000 services incl.
  ReadEcuIdentification 0x1A).
- **`DiagnosticService`** with `auto` protocol selection and a connect-per-call
  lifecycle.
- **DTC decode** per SAE J2012 + JSON description DB (`data/triumph_dtc.json`).
- **ECU identification** (VIN / calibration / ECU name) surfaced on `ReadResult`,
  in `trecu info` / `trecu faults` output, and on the TUI's Dashboard + log.
- **CLI** (`tui|ports|faults|info|sensors|clear|version|help`) and **Textual TUI**
  (read / clear / live data / port picker).
- **Persistent session + keepalive** (`DiagnosticService.session()`, a
  `TesterPresent` ticker, half-duplex I/O lock; the TUI owns one long-lived
  session) — the F1 foundation for everything continuous.
- **Two mock ECUs**, one per protocol path, as the ground truth for tests.

So **Phase 1 is done**, **Phase 2 is done**, the **F1 foundation is done**, and
**Phase 3 (live-data streaming) is done** on the confirmed OBD path — plus the
first slice of **F2** (`obd_sensors.json` + `keihin_sensors.json`).

---

## Cross-cutting foundations (build these once; they unlock the later phases)

These are not user features but they gate almost everything past Phase 2. Doing
them deliberately, early, avoids reworking each later phase.

### F1 — Persistent session + `TesterPresent` keepalive  · **M** · unlocks 3–7 · **done**
Delivered:

- **`DiagnosticService.session()`** (context manager) + **`start_session()`** —
  connect once, hold the client open, and run a keepalive ticker for the life of
  the session. `keepalive_interval=0` disables the ticker (one-shot use).
- **Keepalive ticker** (`_Keepalive`, a daemon thread) beats every
  `DEFAULT_KEEPALIVE_INTERVAL` (2 s): KWP `TesterPresent` (`0x3E`, response
  suppressed) / iso9141 a cheap read-only Mode 01 PID 00 poke — both exposed as
  a duck-typed `client.keepalive()`.
- **Half-duplex serialization** — every operation and every beat runs under one
  `DiagnosticService._io_lock`, so keepalive never interleaves with a read/clear
  on the single-wire K-line.
- **TUI owns one long-lived session** — held by `SessionController`
  (`tui/session.py`), reused across reads/clears/live polls, torn down on error
  for a clean reconnect and on `on_unmount`. The title bar's liveness dot reads
  `connected` for the life of the held session.
- Covered by `tests/test_session.py` (lifecycle, ticker cadence, lock
  serialization, teardown, both clients' keepalive, TUI session reuse).

The CLI stays one-shot (`trecu faults`/`clear` use `with service:`, no
keepalive); the persistent session is the TUI/continuous path.

### F2 — Model-value data files, not constants  · **S, incremental** · unlocks 3–7 · **started**
`data/triumph_dtc.json` is the pattern: model-specific values live in data, not
code. Live-data PIDs/record layouts, actuator routine IDs, and memory maps are
all model/ECU-specific in exactly the same way. Extend the `data/` +
`@dataclass` config approach (per `CLAUDE.md`) rather than hardcoding — one file
per concern (e.g. `obd_sensors.json`, `keihin_sensors.json`, `triumph_actuators.json`).

Delivered: **`data/obd_sensors.json`** — the standardized OBD **Mode 01** PID
set (name, group, unit, byte count, SAE J1979 formula, gauge bounds), loaded as
`PidDatabase` — *and* its own **`data/keihin_sensors.json`**: a
community-reverse-engineered 53-channel Keihin sensor table
(names/kind/decimals/offset/fullscale from that community-sourced data), loaded
as a separate `KwpLocalTable` and wired into decoding via `decode_frame` over the
one packed `21 80` frame. Its frame layout and divisors are a **draft** pending
an F4 capture — fixing them is a data-only edit. Also delivered (2026-07):
**`data/triumph_dtc.json` replaced wholesale** with a community-sourced
service-manual import — 557 codes across `P`/`K`/`C`/`U`/`L` (was 95
P-codes), flat `{code: description}` schema — plus **source-aware DTC
labelling** (`dtc_family`): Keihin `0x18` responses carry raw non-J2012 fault
numbers and decode as `K`-codes, not a bogus structural `P/C/B/U`. Still to
come (hardware-gated): real `kwp_local` divisors/offsets and
`triumph_actuators.json`.

### F3 — Security access (seed/key) spike  · **M, research** · gates 5, 6, 7
KWP `SecurityAccess` (SID `0x27`: request seed → return computed key) almost
certainly guards actuator control and all memory read/write on Triumph ECUs. The
algorithm is community-known but **not derivable from this codebase**.
Do this spike *before* committing to phases 5–7 — it determines whether they're
feasible at all. Needs real hardware + a captured seed/key exchange to validate.

### F4 — Advanced-service path for the real (Sagem) bike  · **M, research** · gates 5–7 on hardware
Important tension: the confirmed real bike does **5-baud init then plain OBD-II**
(`iso9141` path) — and OBD-II has *no* actuator control, memory read, or reflash.
Those are KWP2000-proprietary services. But that bike gets **no data from
fast-init** (`kwp-fast` won't connect to it). ISO 14230 permits KWP2000 services
*after a 5-baud init* — and that combination **now exists as the `kwp-slow`
client** (`Kwp2000Client` with `init_mode="slow"`, the Keihin K-line path:
5-baud at the ECU address `0xD5`, then `10 02` + KWP services; in the `auto`
sweep between `iso9141` and `kwp-fast`). What remains hardware-gated: confirm
which service set the *Sagem* ECU exposes and at which init address (`0x33` OBD
vs a KWP-capable address) — the answer decides whether phases 5–7 ride on
`kwp-slow` as-is (address override) or an extended `iso9141` client. Also still
pending hardware: a real `21 80` capture to fix the draft `kwp_local` frame
layout + per-channel divisors (correlate against known idle values), and which
`0x1A` RLI carries which identity field.

---

## Phase 1 — Connect/handshake + ECU identification  · **done**

**Goal:** confirm the protocol layer round-trips by reading ECU identity (part
number, software/calibration version, VIN).

Delivered:

- *Handshake:* both clients `connect()` and return `ConnectionInfo`.
- *Identification:* `read_identification() -> EcuInfo` on both duck-typed
  clients — `iso9141` via OBD **Mode 09** (PID 02 VIN / 04 Calibration ID / 0A
  ECU name), `kwp` via **ReadEcuIdentification** (SID `0x1A`, model-specific RLIs
  in `Kwp2000Config`). Best-effort with a short per-record timeout so an ECU that
  doesn't implement it just yields empty fields.
- `EcuInfo` lives in `kwp2000.py` beside `ConnectionInfo` (shared vocabulary);
  surfaced on `ReadResult`, cached per session in `DiagnosticService`.
- Both mocks serve placeholder identity; shown by `trecu info` and on the TUI's
  Dashboard identity card + log. Covered by `tests/test_identification.py`.

**Follow-ups (deferred, not blocking):** real Mode 09 responses on K-line can
span multiple frames — the current decoder reads a single frame, which is what
the mock emits; confirm on hardware. Triumph KWP RLIs are guesses until captured
from a real bike (that's the F2/F4 data-vs-constants point).

## Phase 2 — Read / clear DTCs  · **done**

Both paths implemented, decoded, and tested (`test_iso9141_obd.py`,
`test_mock_roundtrip.py`). Code dictionary + source-aware `K`-code labelling
delivered under F2 (2026-07, 557 codes). Ongoing: extend `triumph_dtc.json`
coverage as new model codes surface. No new architecture.

**Hardened against the real bike (2026-07):** the confirmed OBD read was
silently reporting comms failures as "no stored codes." Three fixes, validated
live (6/6 reads of `P1108`, was 0/6): (1) absolute-deadline 5-baud bit timing;
(2) `_slow_init` now requires the inverted-address handshake byte, so a garbled
init retries instead of proceeding on a dead link; (3) `read_dtcs` reads the
reliable Mode 01 PID 01 (MIL + count) first and reconciles Mode 03 against it,
retrying and **raising** rather than returning a false empty. See CLAUDE.md
"Known real-hardware facts" for the ECU behavior these address.

## Phase 3 — Live sensor data streaming  · **L**  · needs F1 (+ F2) · **done** (OBD path; Keihin hardware validation pending)

**Goal:** continuously poll and display RPM, TPS, MAP, O2, coolant temp, battery
voltage.

> **UI concept:** see [TUI: the tabbed session shell](#tui-the-tabbed-session-shell-and-where-its-headed)
> below — reframes the TUI from a one-shot DTC reader into a persistent
> diagnostic session with the DTC list as one view among several (Dashboard /
> Live Data / Throttle Sync). That redesign is the UI side of F1 and the front
> end for this phase and Phase 4.

Delivered:

- **Sensor-decode layer** (`protocol/pids.py`): `PidDatabase` +
  `SensorReading`, formulas evaluated by a restricted interpreter (no `eval`),
  backed by `obd_sensors.json` / `keihin_sensors.json` (F2).
- **`read_live(pids)`** on both duck-typed clients and on `DiagnosticService`
  (serialized on `_io_lock`, decodes to ordered `SensorReading`s). Defaults:
  `DEFAULT_LIVE_PIDS` (RPM, coolant, TPS, MAP, O2, battery) + `DEFAULT_POLL_INTERVAL`.
  - `iso9141`: one OBD **Mode 01** request per PID — standardized (SAE J1979),
    the confirmed path.
  - `kwp`: **ReadDataByLocalIdentifier** `21 80` (the Keihin MODE_READ_SENSORS RLI)
    — one packed frame carrying all channels, split per the `kwp_local` table
    (F2 draft layout; real divisors/offsets need a hardware capture, F4).
- **Poll loop UI**: a **Live Data** tab (streaming table with running min/max +
  trend sparklines) driven by a `set_interval` timer that resumes only while the
  tab is active and kicks a `@work(group="live")` reader; `space` freezes it.
- **Mocks** answer live requests with plausible *moving* values from one shared
  generator (`transport/_mock_live.py`).
- **CLI** `trecu sensors` (headless one-shot snapshot). Tests in `test_pids.py` +
  `test_live.py` (formula eval, both client paths, service decode/ordering, mock
  movement, TUI poll loop).

### Keihin live sensor data streaming  · **M, hardware-gated**

**Goal:** stream the full Keihin sensor set over KWP using the packed
`ReadDataByLocalIdentifier 21 80` response, with trustworthy engineering units
and the same CLI/TUI experience as the confirmed OBD path.

The client, packed-frame decoder, 53-channel data table, mock stream, and UI
integration already exist. Remaining work:

- Capture real `21 80` frames from a Keihin ECU at known operating points
  (key-on/engine-off, cold idle, warm idle, and controlled throttle changes).
- Confirm channel offsets, widths, signedness, divisors, and units against the
  observed sensor values; correct `data/keihin_sensors.json` without changing
  protocol code.
- Verify sustained polling, keepalive coexistence, unsupported-channel
  handling, and achievable refresh rate on the single-wire K-line.
- Add capture-derived regression fixtures and mark the Keihin path supported
  only after the decoded values agree with known measurements.

**Still ahead / deferred niceties** (not blocking Phase 4): user-driven PID
selection, CSV record, adjustable poll rate, a Dashboard gauge cluster, and the
real KWP record layouts (F4). Effective K-line OBD poll rate is ~1–2 Hz (each
PID is a separate paced round-trip), not the aspirational 6 Hz in the mockups.

## Phase 4 — Throttle body sync display  · **M**  · needs Phase 3

**Goal:** per-cylinder intake-pressure readout for balancing throttle bodies —
the kind of "Adjustments" screen other Triumph diagnostic tools offer.

- Mostly a **specialized consumer of the Phase 3** stream + a purpose-built UI: a
  balance visualization (per-cylinder bar graph, "matched?" indicator at idle).
- *Protocol:* identify which live records carry per-cylinder MAP/pressure
  (model-specific; data-driven per F2).
- **Risk:** low protocol risk (read-only), higher UI-design effort. Cheap once
  Phase 3 exists.

## Phase 5 — Actuator tests (fuel pump, injectors)  · **L**  · needs F1, F3, F4

**Goal:** command ECU outputs — prime fuel pump, click injectors — for
diagnosis.

- **First *write*/command phase.** No longer read-only.
- *Protocol:* KWP **InputOutputControlByLocalIdentifier** (SID `0x2F`) or a
  **StartRoutineByLocalIdentifier** (SID `0x31`); model-specific IDs (F2).
  Requires the right diagnostic session and almost certainly **SecurityAccess
  (F3)**. **OBD-II cannot do this**, so this rides on the KWP/F4 path.
- **Safety (mandatory):** verify engine off; strictly time-limited actuation;
  hold-to-activate semantics; prominent confirmation modals; auto-stop on
  disconnect.
- **Mocks:** model actuator state so tests can assert activation without
  hardware.
- **Risk:** high. Gated by the F3 seed/key spike and the F4 path question. Do not
  start before both are resolved.

## Phase 6 — Map read (upload / backup from ECU)  · **L**  · needs F3, F4

**Goal:** dump the ECU's calibration / flash image to a file. Read-only, safe —
and the prerequisite backup for any future write.

- *Protocol:* KWP **ReadMemoryByAddress** (SID `0x23`), or the upload sequence
  **RequestUpload `0x35` → TransferData `0x36` → RequestTransferExit `0x37`**.
  Needs a programming session + **SecurityAccess (F3)**.
- **Seam:** a new map/flash module (block-transfer loop, progress, checksum
  verify) + a file format (raw `.bin`, ideally compatible with other
  community tools) + a CLI
  `--dump-map FILE` command.
- **Risk:** high reverse-engineering dependency — correct memory addresses and
  block sizing are model-specific and not in this codebase. Read-only, so no
  bricking risk, but useless without F3/F4.

## Phase 7 — Map write / reflash  · **XL**  · needs Phase 6 proven first

**Goal:** write a calibration back to the ECU. **The dangerous one — a failed or
interrupted write can brick the ECU.**

- *Protocol:* **RequestDownload `0x34` → TransferData `0x36` → RequestTransferExit
  `0x37`**, plus an erase routine, checksum correction, and read-back verify.
  Programming session + SecurityAccess **mandatory**.
- **Preconditions:** a *proven* Phase 6 read round-trip; a known-good backup
  before every write; checksum handling; a documented recovery path; extensive
  bench testing before any real bike.
- **Changes the project's safety posture.** The README currently states trecu
  "reads/clears DTCs only — it does not flash or tune the ECU." Shipping this
  means rewriting that section and the Safety section.
- **Risk:** maximum. Absolute last. Consider whether it belongs in this tool at
  all versus deferring to existing community tools.

---

## Suggested sequencing

1. **Phase 1 (ECU ID)** — small, safe, proves the stack; ✅ done.
2. **F1 (persistent session)** — the unlock for everything continuous; ✅ done.
3. **Phase 3 → Phase 4** — live data, then throttle sync rides on it for cheap.
4. **F3 + F4 spikes** (need real hardware) — resolve *before* committing to 5–7;
   the `kwp-slow` client now exists, so the open question is which service
   set/address the real Sagem ECU answers (5–7 may still be blocked).
5. **Phase 5 → 6** once security access is understood.
6. **Phase 7** only after 6 is proven and with eyes open on the risk.

Everything from Phase 3 on leans on F1; everything from Phase 5 on leans on F3
and F4. The two research spikes are the real schedule risk — front-load them.

---

## TUI: the tabbed session shell, and where it's headed

Status: the **tabbed session *shell* is built** (`tui/app.py`), so is the
**persistent-session backend** (F1 — the app now holds one long-lived
connection with a keepalive ticker), and so is the **Live Data view + poll loop**
(Phase 3 — see "Live Data" below). The actuator/throttle views the shell
is shaped for are still ahead. This section records both — what ships today, and
the direction that shaped it. It is the UI side of **F1** (persistent session
+ `TesterPresent` keepalive) and the front end for **Phase 3** (live sensor
streaming, now built) and **Phase 4** (throttle sync). See the phases above for
the layer plan and `CLAUDE.md` for the four architectural seams.

### The core reframe (the direction)

trecu began as a pure DTC reader: one screen, one `DataTable`, connect-read-
disconnect per keypress. That model is right for a one-shot snapshot and wrong
for anything continuous. The re-think, in one sentence:

> **trecu becomes a persistent diagnostic *session*, and the DTC list becomes
> one *view* over it — not the app itself.**

The tabbed shell below is the *visual* half of that reframe. The other half —
one long-lived session with keepalive, replacing connect-per-action — is F1 and
**is now built** (see "The persistent session (F1)"). What's still ahead
is the poll loop and live views that make the session *visibly* continuous.

### The organizing constraint

The K-line is **half-duplex, single-wire, one conversation at a time**. You
cannot poll live sensors *and* read DTCs *and* pulse an actuator at once — there
is one wire and one session actor serializing every exchange. That becomes the
UI's mental model:

> **The active view *is* what the ECU session is doing right now.**

The session is held open on keepalive between operations (F1), and the poll loop
(Phase 3) now makes the *active view retask the ECU* literally true: switching to
**Live Data** resumes a `set_interval` poll that streams PIDs; leaving it pauses
the stream (`_sync_live_polling`); **Faults** reads DTCs then idles on keepalive;
**Actuators** (later) commands outputs. One wire, one activity, mirrored by one
visible tab — switching views *retasks the ECU*.

### What's built today

#### The shell

- **Title bar** — Textual's stock one-row `Header` (`show_clock=False`, no
  icon), rendered by a `format_title` override into one assembled line:
  `TrECU v0.1.0 — ● connected`. The dot's color and the label come from
  `_CONNECTION_STATES` (`disconnected` / `connecting` / `reading` / `clearing` /
  `connected` / `error`, plus `streaming...` / `frozen` while the Live Data poll
  loop runs). Deliberately *thinner* than the originally proposed spine:
  protocol / port / ECU identity live in the Dashboard cards.
  - The two lamps the early drafts put here are **gone**, replaced by cheaper
    signals. The **MIL lamp** is now the **Faults tab itself** — `_mark_faults_tab`
    tints its label red (`-has-faults`) when the last read found stored codes, so
    the fault indicator sits on the thing you'd click. The **`⚡` keepalive lamp**
    is subsumed by the state label: a held session simply reads `connected`, and
    the keepalive ticker is an implementation detail with no lamp of its own.
- **`TabbedContent` body** — four tabs: **Dashboard**, **Faults**, **Live Data**,
  **Log**. `←` / `→` move between them, shown in the footer as *Prev tab* /
  *Next tab*. These are app-level `priority=True` bindings: `TabbedContent`
  already binds the arrows but with `show=False`, so re-declaring them with
  priority is what makes them win the binding chain *and* appear in the footer.
  Each switch also focuses that tab's primary control (`_TAB_FOCUS`).
- **Contextual footer** — `check_action` gates the action bindings per tab:
  `r` Read appears on Dashboard and Faults; `c` Clear on Faults only; `space`
  Freeze on Live Data only; `q` Quit everywhere. On the Log tab only the tab-nav
  + quit remain.
- **Log is a permanent tab**, not the toggle originally proposed. The app
  auto-switches to it under `--debug` and on any error (plus a `bell()`), and
  error lines (`[error] …`) render red.
- **Clear is guarded** by a modal `ConfirmScreen` whose default/focused button
  is *Cancel*; Enter or Esc cancels, avoiding an accidental wipe.

#### Mockups (current)

Dashboard — three summary cards, the landing view. `Faults` is red because the
last read found codes; that tint *is* the MIL indicator:

```
┌───────────────────── TrECU v0.1.0 — ● connected ─────────────────────┐
├[ Dashboard ]─ Faults ─ Live Data ─ Log ──────────────────────────────┤
│ ╭ Faults ──────────╮ ╭ Connection ──────╮ ╭ ECU identity ─────────╮  │
│ │ 1 stored fault    │ │ Mode     Mock    │ │ VIN      SMT…1234     │  │
│ │ code(s)           │ │ Port     mock ECU│ │ Cal      1234567      │  │
│ │                   │ │ Protocol iso9141 │ │ SW       2.11         │  │
│ │ P1108             │ │                  │ │                       │  │
│ ╰───────────────────╯ ╰──────────────────╯ ╰───────────────────────╯  │
├───────────────────────────────────────────────────────────────────────┤
│ ← Prev tab   → Next tab   r Read   q Quit                             │
└───────────────────────────────────────────────────────────────────────┘
```

Faults — the DTC table, always shown (just column headers and no rows when
there are no codes; the "no faults" wording lives on the Dashboard's Faults card):

```
├─ Dashboard ─[ Faults ]─ Live Data ─ Log ─────────────────────────────┤
│ Code    Status         Description                                    │
│ P1108   stored, MIL    Ambient pressure sensor circuit …              │
├───────────────────────────────────────────────────────────────────────┤
│ ← Prev tab   → Next tab   r Read   c Clear   q Quit                   │
```

Log — timestamped protocol trace (errors in red), auto-shown under `--debug` /
on error:

```
├─ Dashboard ─ Faults ─ Live Data ─[ Log ]─────────────────────────────┤
│ 14:32:07  trecu ready — MOCK ECU (no hardware) mode. Press 'r' to read.│
│ 14:32:08  read complete: 1 fault code(s) via iso9141                   │
├───────────────────────────────────────────────────────────────────────┤
│ ← Prev tab   → Next tab   q Quit                                      │
```

#### The persistent session (F1) — now built

The session is now mechanism, not framing. `action_read` / `_run_clear` no
longer build a fresh service per keypress: **one long-lived `DiagnosticService`**
is owned by a Textual-free `SessionController` (`tui/session.py`), connected once
behind a cancelable modal and held open by a background keepalive ticker.
Re-reads, clears, and live polls reuse it rather than re-initialising the K-line,
and all of them connect through that controller's single connect path. A failed
operation tears the session down (`_ecu.close()`) so the
next read reconnects cleanly; `on_unmount` closes it on exit. The **`⚡`
keepalive lamp** these drafts proposed was dropped: a held session is exactly
what the title bar's `connected` state means, so a second glyph for it added
noise, not information.

Reads still run in `asyncio.to_thread` inside an `exclusive` `@work(group="ecu")`
so a Read and a Clear can't overlap; the half-duplex constraint is *also* now
enforced one layer down, in `DiagnosticService._io_lock`, which serializes every
operation and every keepalive beat on the single wire.

**Still ahead (Phase 3):** the `set_interval` poll loop and reactive streaming
that make the *active view retasks the ECU* model literally true. F1 is the
lifecycle those sit on; the poll loop replaces the one-shot `@work` reader.

### Views

#### Live Data — the centerpiece — **built** (Phase 3)

Value + unit + running min/max + trend sparkline per PID is **shipped**, driven
by the `set_interval` poll loop over `DiagnosticService.read_live`; `space`
freezes the stream and the title bar reads `streaming...` / `frozen`. Still
open from the mockup below: user-driven PID selection (`p`), CSV record (`R`),
and adjustable rate (`+/-`) — deferred, not blocking Phase 4. (The mockup's
"6 Hz" is aspirational; K-line OBD polls each PID as a separate paced round-trip,
so the real cadence is ~1–2 Hz. Its poll-rate and `⚡keepalive` indicators were
not built — the title bar carries one state label, nothing more.)

```
┌───────────────────── TrECU v0.1.0 — ● streaming... ─────────────────────┐
├─ Dashboard ─ Faults ─[ Live Data ]─ Log ─ Throttle Sync ────────────────┤
│ Sensor              Value    Unit   Min    Max    Trend                  │
│ Engine speed        1248     rpm    1180   1310   ▁▂▄▇▆▄▂▁▂▃             │
│ Coolant temp          92     °C       88     93   ▅▅▆▆▆▇▇▇▇▇             │
│ Throttle position      4.0   %         3.6    5.1  ▂▁▂▂▁▂▃▂▁▂            │
│ Intake MAP            38     kPa      36     41   ▃▄▃▂▃▄▃▂▃▄             │
│ O2 sensor 1            0.45  V         0.1    0.9  ▂▇▁▇▂▇▁▇▂▇            │
├──────────────────────────────────────────────────────────────────────────┤
│ space freeze · p pick PIDs · R record CSV · +/- rate · q quit           │
└──────────────────────────────────────────────────────────────────────────┘
```

#### Throttle Sync (Phase 4) — a purpose-built consumer of the same stream

```
┌ TrECU ──────────────────────────────  THROTTLE SYNC · idle · engine warm ┐
├─ Dashboard ─ Faults ─ Live Data ─[ Throttle Sync ]──────────────────────┤
│   Cyl 1   38.2 kPa   ██████████████████░░░░                             │
│   Cyl 2   37.9 kPa   █████████████████▉░░░░                             │
│   Cyl 3   38.4 kPa   ██████████████████▏░░░                             │
│                                                                          │
│   spread 0.5 kPa    ✔ BALANCED  (within 1.0 kPa)                        │
├──────────────────────────────────────────────────────────────────────────┤
│ space freeze · q quit                                                    │
└──────────────────────────────────────────────────────────────────────────┘
```

#### Textual widgets these map onto (no invention needed)

- **`Digits`** — big RPM / voltage readouts if the Dashboard grows live tiles.
- **`Sparkline`** — trend columns; needs a small ring-buffer of history per PID.
- **`ProgressBar`** (or a thin custom `Static`) as horizontal gauges — which is
  *why* the sensor descriptor must carry **min/max/redline bounds**, not just a
  decode formula (a hard dependency on F2's data shape including *display*
  metadata).
- **reactive attributes + `set_interval`** — the poll ticker writes reactives;
  widgets re-render themselves. Replaces the one-shot `@work` reader.

#### What the planned views ask of the layers below

- **Session lifecycle (F1):** one long-lived worker owns the connection; a poll
  `set_interval` drives live reads; keepalive runs when idle. The
  connect-per-`action_read` model is retired.
- **Serialization:** every view's ECU traffic funnels through that single
  session actor — the half-duplex constraint enforced in code, not just
  honored by convention. Tab switches pause/resume the poll loop.
- **Sensor-decode layer + `obd_sensors.json` / `keihin_sensors.json`** (Phase 3 / F2): id, name, unit,
  formula, **and gauge bounds**.
- **Mocks must emit *varying* values** or the live view looks dead.

### Open decisions

1. **Nav style** — *decided:* `TabbedContent` (discoverable, scales to ~6
   modes, cheap). A left nav-rail "cockpit" was the rejected alternative.
2. **Does Dashboard earn its keep now** — *decided:* yes, shipped as three
   summary cards. The richer gauge-cluster version is deferred to Phase 3.
3. **Live-data presentation** — *open:* dense table-with-sparklines (above) vs.
   a gauge cluster. Current lean: gauges on Dashboard, dense table on Live Data.
