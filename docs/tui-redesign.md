# TUI: the tabbed session shell, and where it's headed

Status: the **tabbed session *shell* is built** (`tui/app.py`) and so is the
**persistent-session backend** (roadmap F1 — the app now holds one long-lived
connection with a keepalive ticker; see below). The live-data / actuator views
the shell is shaped for are still ahead. This doc records both — what ships
today, and the direction that shaped it. It is the UI side of roadmap **F1**
(persistent session + `TesterPresent` keepalive) and the front end for **Phase
3** (live sensor streaming) and **Phase 4** (throttle sync). See `ROADMAP.md`
for the layer plan and `CLAUDE.md` for the four architectural seams.

## The core reframe (the direction)

trecu began as a pure DTC reader: one screen, one `DataTable`, connect-read-
disconnect per keypress. That model is right for a one-shot snapshot and wrong
for anything continuous. The re-think, in one sentence:

> **trecu becomes a persistent diagnostic *session*, and the DTC list becomes
> one *view* over it — not the app itself.**

The tabbed shell below is the *visual* half of that reframe. The other half —
one long-lived session with keepalive, replacing connect-per-action — is roadmap
F1 and **is now built** (see "The persistent session (F1)"). What's still ahead
is the poll loop and live views that make the session *visibly* continuous.

## The organizing constraint

The K-line is **half-duplex, single-wire, one conversation at a time**. You
cannot poll live sensors *and* read DTCs *and* pulse an actuator at once — there
is one wire and one session actor serializing every exchange. That becomes the
UI's mental model:

> **The active view *is* what the ECU session is doing right now.**

The session is now held open on keepalive between operations (F1), but the
*views* don't yet retask it continuously — that's the poll loop (Phase 3). Once
it exists, switching to **Live Data** streams PIDs; switching to **Faults**
pauses the stream, reads DTCs, then idles on keepalive; **Actuators** (later)
commands outputs. One wire, one activity, mirrored by one visible tab —
switching views *retasks the ECU*.

## What's built today

### The shell

- **Session spine** — a one-row header (`#spine`). Brand (`TrECU`) on the left;
  right-aligned, a colored liveness dot + state label driven by `_SPINE`
  (`ready` / `connecting` / `reading` / `clearing` / `connected` / `error`),
  preceded by a **synthetic MIL lamp** — a red dot shown only when the last read
  reported stored faults, and (F1) a green **`⚡` keepalive lamp** while a
  long-lived session is held open. This is deliberately *thinner* than the
  originally proposed spine: protocol / port / ECU identity live in the
  Dashboard cards, and there is no poll-rate indicator yet (no poll loop).
- **`TabbedContent` body** — three tabs: **Dashboard**, **Faults**, **Log**.
  `←` / `→` move between them, shown in the footer as *Prev tab* / *Next tab*.
  These are app-level `priority=True` bindings: `TabbedContent` already binds
  the arrows but with `show=False`, so re-declaring them with priority is what
  makes them win the binding chain *and* appear in the footer.
- **Contextual footer** — `check_action` gates the action bindings per tab:
  `r` Read appears on Dashboard and Faults; `c` Clear on Faults only; `q` Quit
  everywhere. On the Log tab only the tab-nav + quit remain.
- **Log is a permanent tab**, not the toggle originally proposed. The app
  auto-switches to it under `-v` and on any error (plus a `bell()`), and error
  lines (`[error] …`) render red.
- **Clear is guarded** by a modal `ConfirmScreen` whose default/focused button
  is *Cancel*; Enter or Esc cancels, avoiding an accidental wipe.

### Mockups (current)

Dashboard — three summary cards, the landing view:

```
┌ TrECU ─────────────────────────────────────────────── ● ● connected ┐
├[ Dashboard ]─ Faults ─ Log ──────────────────────────────────────────┤
│ ╭ Faults ──────────╮ ╭ Connection ──────╮ ╭ ECU identity ─────────╮  │
│ │ 1 stored fault    │ │ Mode     Mock    │ │ VIN      SMT…1234     │  │
│ │ code(s)           │ │ Port     mock ECU│ │ Cal      1234567      │  │
│ │                   │ │ Protocol iso9141 │ │ SW       2.11         │  │
│ │ P1108             │ │                  │ │                       │  │
│ ╰───────────────────╯ ╰──────────────────╯ ╰───────────────────────╯  │
├───────────────────────────────────────────────────────────────────────┤
│ r Read   ← Prev tab   → Next tab   q Quit                             │
└───────────────────────────────────────────────────────────────────────┘
```

Faults — the DTC table (or a centered "no faults" state when empty):

```
├─ Dashboard ─[ Faults ]─ Log ─────────────────────────────────────────┤
│ Code    Status         Subsystem              Description             │
│ P1108   stored, MIL    Ambient pressure       Sensor circuit …        │
├───────────────────────────────────────────────────────────────────────┤
│ r Read   c Clear   ← Prev tab   → Next tab   q Quit                   │
```

Log — timestamped protocol trace (errors in red), auto-shown under `-v`/on error:

```
├─ Dashboard ─ Faults ─[ Log ]─────────────────────────────────────────┤
│ 14:32:07  trecu ready — MOCK ECU (no hardware) mode. Press 'r' to read.│
│ 14:32:08  read complete: 1 fault code(s) via iso9141                   │
├───────────────────────────────────────────────────────────────────────┤
│ ← Prev tab   → Next tab   q Quit                                      │
```

### The persistent session (F1) — now built

The session is now mechanism, not framing. `action_read` / `_run_clear` no
longer build a fresh service per keypress: the app owns **one long-lived
`DiagnosticService`**, built lazily on the first read (`_ensure_session`),
connected once and held open by a background keepalive ticker. Re-reads and
clears reuse it (`_session_read` / `_session_clear`) rather than re-initialising
the K-line. A failed operation tears the session down (`_close_session`) so the
next read reconnects cleanly; `on_unmount` closes it on exit. The spine grows a
green **`⚡` keepalive lamp** while a session is live — the first honest
indicator of the "session is doing something" model.

Reads still run in `asyncio.to_thread` inside an `exclusive` `@work(group="ecu")`
so a Read and a Clear can't overlap; the half-duplex constraint is *also* now
enforced one layer down, in `DiagnosticService._io_lock`, which serializes every
operation and every keepalive beat on the single wire.

**Still ahead (Phase 3):** the `set_interval` poll loop and reactive streaming
that make the *active view retasks the ECU* model literally true. F1 is the
lifecycle those sit on; the poll loop replaces the one-shot `@work` reader.

## Planned views (not built)

These stay as the target the shell was shaped for. Value + unit + running
min/max + trend sparkline per PID; user-driven PID selection; freeze + CSV
record.

### Live Data — the centerpiece

```
┌ TrECU ─────────────────────────────  polling 8 PIDs @ 6 Hz  ⚡keepalive ┐
├─ Dashboard ─ Faults ─[ Live Data ]─ Throttle Sync ──────────────────────┤
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

### Throttle Sync (Phase 4) — a purpose-built consumer of the same stream

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

### Textual widgets these map onto (no invention needed)

- **`Digits`** — big RPM / voltage readouts if the Dashboard grows live tiles.
- **`Sparkline`** — trend columns; needs a small ring-buffer of history per PID.
- **`ProgressBar`** (or a thin custom `Static`) as horizontal gauges — which is
  *why* the sensor descriptor must carry **min/max/redline bounds**, not just a
  decode formula (a hard dependency on F2's data shape including *display*
  metadata).
- **reactive attributes + `set_interval`** — the poll ticker writes reactives;
  widgets re-render themselves. Replaces the one-shot `@work` reader.

### What the planned views ask of the layers below

- **Session lifecycle (F1):** one long-lived worker owns the connection; a poll
  `set_interval` drives live reads; keepalive runs when idle. The
  connect-per-`action_read` model is retired.
- **Serialization:** every view's ECU traffic funnels through that single
  session actor — the half-duplex constraint enforced in code, not just
  honored by convention. Tab switches pause/resume the poll loop.
- **Sensor-decode layer + `triumph_pids.json`** (Phase 3 / F2): id, name, unit,
  formula, **and gauge bounds**.
- **Mocks must emit *varying* values** or the live view looks dead.

## Open decisions

1. **Nav style** — *decided:* `TabbedContent` (discoverable, scales to ~6
   modes, cheap). A left nav-rail "cockpit" was the rejected alternative.
2. **Does Dashboard earn its keep now** — *decided:* yes, shipped as three
   summary cards. The richer gauge-cluster version is deferred to Phase 3.
3. **Live-data presentation** — *open:* dense table-with-sparklines (above) vs.
   a gauge cluster. Current lean: gauges on Dashboard, dense table on Live Data.
