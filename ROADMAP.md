# trecu roadmap

From "read fault codes" toward a TuneECU-class diagnostic tool. Ordered by
increasing risk and increasing dependence on model-specific reverse engineering.
Each phase says what already exists, what new work lands in which architectural
seam (see `CLAUDE.md` for the four layers), and what gates it.

Effort key: **S** ≈ a session, **M** ≈ a few days, **L** ≈ a week+, **XL** ≈ multi-week / research-bound.

---

## Where we are today

Built and tested (31 tests, all against the mock ECUs):

- **Two duck-typed protocol clients** — `Iso9141Client` (5-baud slow init + OBD-II
  Modes 01/03/04/07/09) and `Kwp2000Client` (fast-init + KWP2000 services incl.
  ReadEcuIdentification 0x1A).
- **`DiagnosticService`** with `auto` protocol selection and a connect-per-call
  lifecycle.
- **DTC decode** per SAE J2012 + JSON description DB (`data/triumph_dtc.json`).
- **ECU identification** (VIN / calibration / ECU name) surfaced on `ReadResult`,
  in `--read` output, and in the TUI status bar + log.
- **CLI** (`--read`, `--clear`, `--list-ports`, `--mock`) and **Textual TUI**
  (read / clear / port picker).
- **Persistent session + keepalive** (`DiagnosticService.session()`, a
  `TesterPresent` ticker, half-duplex I/O lock; the TUI owns one long-lived
  session) — the F1 foundation for everything continuous.
- **Two mock ECUs**, one per protocol path, as the ground truth for tests.

So **Phase 1 is done**, **Phase 2 is done**, and the **F1 foundation is done**
(Phase 3 live-data streaming is now unblocked).

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
- **TUI owns one long-lived session** — built lazily on first read
  (`_ensure_session`), reused across reads/clears, torn down on error for a
  clean reconnect and on `on_unmount`. The spine shows a `⚡` keepalive lamp.
- Covered by `tests/test_session.py` (lifecycle, ticker cadence, lock
  serialization, teardown, both clients' keepalive, TUI session reuse).

The CLI stays one-shot (`--read`/`--clear` use `with service:`, no keepalive);
the persistent session is the TUI/continuous path.

### F2 — Model-value data files, not constants  · **S, incremental** · unlocks 3–7
`data/triumph_dtc.json` is the pattern: model-specific values live in data, not
code. Live-data PIDs/record layouts, actuator routine IDs, and memory maps are
all model/ECU-specific in exactly the same way. Extend the `data/` +
`@dataclass` config approach (per `CLAUDE.md`) rather than hardcoding — one file
per concern (e.g. `triumph_pids.json`, `triumph_actuators.json`).

### F3 — Security access (seed/key) spike  · **M, research** · gates 5, 6, 7
KWP `SecurityAccess` (SID `0x27`: request seed → return computed key) almost
certainly guards actuator control and all memory read/write on Triumph ECUs. The
algorithm is community-known (TuneECU) but **not derivable from this codebase**.
Do this spike *before* committing to phases 5–7 — it determines whether they're
feasible at all. Needs real hardware + a captured seed/key exchange to validate.

### F4 — Advanced-service path for the real (Sagem) bike  · **M, research** · gates 5–7 on hardware
Important tension: the confirmed real bike does **5-baud init then plain OBD-II**
(`iso9141` path) — and OBD-II has *no* actuator control, memory read, or reflash.
Those are KWP2000-proprietary services. But that bike gets **no data from
fast-init** (`kwp-fast` won't connect to it). ISO 14230 permits KWP2000 services
*after a 5-baud init*, which is the likely real path for advanced features — a
third combination this codebase doesn't have yet: **5-baud init + KWP services**.
Confirm on hardware which service set the Sagem ECU exposes; the answer decides
whether phases 5–7 ride on a new `kwp-slow` client or an extended `iso9141` one.

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
- Both mocks serve placeholder identity; shown in `--read` output and the TUI
  status bar + log. Covered by `tests/test_identification.py`.

**Follow-ups (deferred, not blocking):** real Mode 09 responses on K-line can
span multiple frames — the current decoder reads a single frame, which is what
the mock emits; confirm on hardware. Triumph KWP RLIs are guesses until captured
from a real bike (that's the F2/F4 data-vs-constants point).

## Phase 2 — Read / clear DTCs  · **done**

Both paths implemented, decoded, and tested (`test_iso9141_obd.py`,
`test_mock_roundtrip.py`). Ongoing: extend `triumph_dtc.json` coverage as new
model codes surface. No new architecture.

## Phase 3 — Live sensor data streaming  · **L**  · needs F1 (+ F2)

**Goal:** continuously poll and display RPM, TPS, MAP, O2, coolant temp, battery
voltage.

> **UI concept:** see [`docs/tui-redesign.md`](docs/tui-redesign.md) — reframes
> the TUI from a one-shot DTC reader into a persistent diagnostic session with
> the DTC list as one view among several (Dashboard / Live Data / Throttle Sync).
> That redesign is the UI side of F1 and the front end for this phase and Phase 4.

- **First real streaming feature** — this is where F1 (persistent session +
  keepalive) becomes mandatory.
- *Protocol:*
  - `iso9141`: OBD **Mode 01** PIDs — 0C RPM, 05 coolant, 11 TPS, 0B MAP,
    14/24… O2, 42 module voltage. Formulas are standardized (SAE J1979).
  - `kwp`: **ReadDataByLocalIdentifier** (SID `0x21`) / ByCommonIdentifier
    (`0x22`) — Triumph packs sensors into model-specific record layouts (this is
    what TuneECU's live display reads).
- **Seam:** a new **sensor-decode layer** (PID/record → name, unit, formula),
  data-driven per F2; a `read_live(pids)` service method; a poll loop.
- **UI:** new live view (updating table or gauges). TUI threading changes — a
  repeating poll via `set_interval` feeding a `@work` reader, not the one-shot
  worker used for DTCs.
- **Mocks:** both ECUs must answer PID/record requests with plausible, *varying*
  values so the stream visibly moves.
- **Config/tests:** poll interval + PID list in config; pure formula unit tests +
  mock round-trip per PID.
- **Risk:** medium. Standard OBD PIDs are safe/known; KWP record layouts are
  model-specific (needs F2 + hardware capture).

## Phase 4 — Throttle body sync display  · **M**  · needs Phase 3

**Goal:** per-cylinder intake-pressure readout for balancing throttle bodies —
TuneECU's "Adjustments" screen.

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
  verify) + a file format (raw `.bin`, ideally TuneECU-compatible) + a CLI
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
  all versus deferring to TuneECU.

---

## Suggested sequencing

1. **Phase 1 (ECU ID)** — small, safe, proves the stack; ✅ done.
2. **F1 (persistent session)** — the unlock for everything continuous; ✅ done.
3. **Phase 3 → Phase 4** — live data, then throttle sync rides on it for cheap.
4. **F3 + F4 spikes** (need real hardware) — resolve *before* committing to 5–7;
   they may reveal 5–7 are blocked or need a new `kwp-slow` client.
5. **Phase 5 → 6** once security access is understood.
6. **Phase 7** only after 6 is proven and with eyes open on the risk.

Everything from Phase 3 on leans on F1; everything from Phase 5 on leans on F3
and F4. The two research spikes are the real schedule risk — front-load them.
