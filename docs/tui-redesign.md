# TUI redesign — from DTC reader to live diagnostic session

Status: **proposal / concept** — no code changed yet. This is the UI side of
roadmap **F1** (persistent session + `TesterPresent` keepalive) and the front
end for **Phase 3** (live sensor streaming) and **Phase 4** (throttle sync).
See `ROADMAP.md` for the layer-by-layer plan and `CLAUDE.md` for the four
architectural seams.

## The core reframe

Today the app *is* a DTC reader: one screen, one `DataTable`, and a
connect-read-disconnect cycle per keypress (`TrecuApp.action_read` builds a
fresh `DiagnosticService`, connects, reads, tears down). That model is correct
for a one-shot snapshot and fundamentally wrong for anything continuous.

The re-think, in one sentence:

> **trecu becomes a persistent diagnostic *session*, and the DTC list becomes
> one *view* over it — not the app itself.**

This is not cosmetic. It is the UI expression of roadmap F1; the two are the
same reorganization seen from opposite ends. This concept assumes F1 lands
underneath it (one long-lived session + keepalive, replacing connect-per-action).

## The organizing constraint

The K-line is **half-duplex, single-wire, one conversation at a time**. You
cannot poll live sensors *and* read DTCs *and* pulse an actuator at once — there
is one wire and one session actor serializing every exchange.

That constraint becomes the UI's mental model, and it is a clean one:

> **The active view *is* what the ECU session is doing right now.**

- On **Live Data** → the session is streaming PIDs.
- Switch to **Fault Codes** → streaming pauses, it reads DTCs, then idles on
  keepalive.
- Switch to **Actuators** (later) → it is commanding outputs.

One wire, one activity, mirrored by one visible tab. Switching views is not just
navigation — it *retasks the ECU*. The poll loop pauses/resumes on tab change.

## Proposed shape: persistent session shell + tabbed body

1. **A session "spine"** replaces the one-line status bar — always visible in
   every view, showing the connection as a *living* thing:
   - colored liveness dot (green LIVE / yellow connecting / red error)
   - protocol · port · ECU identity · MIL state
   - a keepalive heartbeat + poll-rate indicator so the session's aliveness is
     visible, not inferred.
2. **`TabbedContent` body** — the DTC table stops being the whole screen and
   becomes one tab. Tabs grow as roadmap phases land (Actuators, Map appear only
   when built — don't advertise vaporware).
3. **Toggleable protocol log** (`l`), hidden by default. It is currently a
   permanent 12-row `RichLog` eating a third of the screen; live data needs that
   space. Auto-shown under `-v`, the one time raw bytes matter.
4. **Contextual footer bindings** — `r`/`c` as always-present globals stop making
   sense: reading is continuous now, and *clear* belongs only to Fault Codes.
   Bindings become per-tab.

## Mockups

### Dashboard — landing view ("is the bike OK right now?")

```
┌ trecu ───────────────────────────────────────────────────────  14:32:07 ┐
│ ● LIVE   iso9141 · usbserial-3    Sagem ECU · VIN SMT…1234    MIL ● ON   │
├──[ Dashboard ]─ Fault Codes ─ Live Data ─ Throttle Sync ────────────────┤
│ ┌ ENGINE ─────────────┐ ┌ TEMP ───────┐ ┌ BATTERY ────┐ ┌ FAULTS ─────┐ │
│ │   1 2 4 8  rpm      │ │    92 °C    │ │   13.8 V    │ │  1 stored   │ │
│ │  ▁▂▃▅▇▆▄▂ idle      │ │  ███████░░  │ │  ████████░  │ │  P1108  ●   │ │
│ └─────────────────────┘ └─────────────┘ └─────────────┘ └─────────────┘ │
│ ┌ THROTTLE ───────────┐ ┌ MAP ────────┐ ┌ O2 / TRIM ──┐ ┌ IDENTITY ───┐ │
│ │  4%  ░░░░░░░░░░      │ │  38 kPa     │ │  0.45 V     │ │ Cal 1234567 │ │
│ │                     │ │  ███░░░░░░   │ │  short +2%  │ │ SW 2.11     │ │
│ └─────────────────────┘ └─────────────┘ └─────────────┘ └─────────────┘ │
├─────────────────────────────────────────────────────────────────────────┤
│ 1-4 view · space freeze · k disconnect · l log · q quit                 │
└─────────────────────────────────────────────────────────────────────────┘
```

### Live Data — the centerpiece

Value + unit + running min/max + trend sparkline per PID. PID selection is
user-driven; the stream can be frozen to inspect a value and recorded to CSV.

```
┌ trecu ───────────────────────────────────────────────────────  14:32:11 ┐
│ ● LIVE   iso9141 · usbserial-3   polling 8 PIDs @ 6 Hz   ⚡keepalive ok  │
├─ Dashboard ─ Fault Codes ─[ Live Data ]─ Throttle Sync ─────────────────┤
│ Sensor              Value    Unit   Min    Max    Trend                  │
│ Engine speed        1248     rpm    1180   1310   ▁▂▄▇▆▄▂▁▂▃             │
│ Coolant temp          92     °C       88     93   ▅▅▆▆▆▇▇▇▇▇             │
│ Throttle position      4.0   %         3.6    5.1  ▂▁▂▂▁▂▃▂▁▂            │
│ Intake MAP            38     kPa      36     41   ▃▄▃▂▃▄▃▂▃▄             │
│ O2 sensor 1            0.45  V         0.1    0.9  ▂▇▁▇▂▇▁▇▂▇            │
│ Short fuel trim       +2.0   %        -1      +4   ▄▅▄▆▄▅▄▆▄▅            │
│ Battery voltage       13.8   V        13.6   14.0 ▇▇▇▆▇▇▇▆▇▇            │
├─────────────────────────────────────────────────────────────────────────┤
│ space freeze · p pick PIDs · R record CSV · +/- rate · 1-4 view · q quit│
└─────────────────────────────────────────────────────────────────────────┘
```

### Throttle Sync (Phase 4) — a purpose-built consumer of the same stream

```
┌ trecu ───────────────────────────────────────────────────────  14:33:02 ┐
│ ● LIVE   iso9141 · usbserial-3   THROTTLE SYNC · idle · engine warm     │
├─ Dashboard ─ Fault Codes ─ Live Data ─[ Throttle Sync ]─────────────────┤
│                                                                          │
│   Cyl 1   38.2 kPa   ██████████████████░░░░                             │
│   Cyl 2   37.9 kPa   █████████████████▉░░░░                             │
│   Cyl 3   38.4 kPa   ██████████████████▏░░░                             │
│                                                                          │
│   spread 0.5 kPa    ✔ BALANCED  (within 1.0 kPa)                        │
│                                                                          │
├─────────────────────────────────────────────────────────────────────────┤
│ space freeze · 1-4 view · q quit                                        │
└─────────────────────────────────────────────────────────────────────────┘
```

## Textual widgets this maps onto (no invention needed)

- **`Digits`** — big RPM / voltage readouts on dashboard tiles.
- **`Sparkline`** — trend columns and the RPM idle-trace. Needs a small
  ring-buffer of history per PID.
- **`ProgressBar`** (or a thin custom `Static`) as horizontal gauges for
  temp/TPS/MAP/battery — which is *why* the sensor descriptor must carry
  **min/max/redline bounds**, not just a decode formula. The gauge design has a
  hard dependency on F2's data shape including *display* metadata.
- **`TabbedContent` / `TabPane`** — the nav.
- **reactive attributes + `set_interval`** — the poll ticker writes reactives;
  widgets re-render themselves. Replaces the one-shot `@work` reader.

## What this asks of the layers below (so it is buildable)

- **Session lifecycle (F1):** one long-lived worker owns the connection; a poll
  `set_interval` drives live reads; keepalive runs when idle. The
  connect-per-`action_read` model is retired.
- **Serialization:** every view's ECU traffic funnels through that single
  session actor — the half-duplex constraint enforced in code, not just honored
  by convention. Tab switches pause/resume the poll loop.
- **Sensor-decode layer + `triumph_pids.json`** (Phase 3 / F2): id, name, unit,
  formula, **and gauge bounds**.
- **Mocks must emit *varying* values** (already flagged in the Phase 3 roadmap
  notes) or the live view looks dead.

## Open decisions (need a call before build)

1. **Nav style** — recommend `TabbedContent` (discoverable, scales to ~6 modes,
   cheap). Alternative: a left **nav-rail + content pane** ("cockpit" feel, lets
   identity/session live permanently in the rail) at higher layout cost.
2. **Live data presentation** — dense table-with-sparklines (above) vs. a
   gauge-cluster. Lean: gauges on Dashboard, dense table on Live Data.
3. **Does Dashboard earn its keep now**, or start with just Fault Codes + Live
   Data and add Dashboard later? It is the nicest landing view but also the most
   work and the most dependent on Phase 3 being done.
