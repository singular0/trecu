# trecu

A macOS terminal (TUI) app that reads and decodes **ECU fault codes from Triumph
motorcycles** over a cheap **KKL (FT232RL) K-line cable**.

It talks to the ECU over the K-line using either of the two protocols Triumphs
use, identifies the ECU (VIN / calibration / part), reads stored Diagnostic
Trouble Codes (DTCs), decodes them to human-readable descriptions, lets you
clear them, and **streams live sensor data** (RPM, coolant, throttle, MAP, O2,
battery voltage) in a continuously-updating table — all from a keyboard-driven
terminal UI.

Two protocol paths, auto-detected by default:

- **`iso9141`** — ISO 9141-2 **5-baud slow init** + standard **OBD-II** modes
  (Mode 03 read / Mode 04 clear). This is what most Triumphs actually use, and
  it is confirmed working on a real bike.
- **`kwp-fast`** — KWP2000 (ISO 14230) **fast-init** + ReadDTCByStatus, for ECUs
  that prefer it.

```
┌ trecu — Triumph ECU fault-code reader ─────────────────────────┐
│ SERIAL  ·  3 fault code(s)  ·  key bytes EA 8F                  │
├────────┬───────────────────┬──────────────┬────────────────────┤
│ Code   │ Status            │ Subsystem    │ Description         │
│ P0107  │ testFailed        │ MAP sensor   │ MAP circuit low …   │
│ P0201  │ confirmed         │ Fuel inject… │ Injector 1 circuit  │
│ P1176  │ testFailed        │ Fuel trim    │ Closed-loop / CO …  │
├────────┴───────────────────┴──────────────┴────────────────────┤
│ -> 82 11 F1 …   <- C1 EA 8F …   (protocol log)                  │
└─ r Read  c Clear  q Quit ──────────────────────────────────────┘
```

## What it uses (well-maintained libraries)

| Concern | Library |
| --- | --- |
| TUI | [Textual](https://github.com/Textualize/textual) |
| Rich text / tables | [Rich](https://github.com/Textualize/rich) |
| Serial / FTDI VCP access | [pyserial](https://github.com/pyserial/pyserial) |
| Direct FTDI (optional) | [pyftdi](https://github.com/eblot/pyftdi) |

The KWP2000 framing, fast-init, DTC services, and SAE J2012 code decoding are
implemented in this repo (there is no maintained, general Python KWP2000 client
for a "dumb" K-line cable — a KKL cable is **not** an ELM327, so `python-OBD`
does not apply).

## Requirements

- macOS with Python 3.10+
- A KKL diagnostic cable based on the **FTDI FT232RL** chip
- The **FTDI VCP driver** (recent macOS includes an Apple FTDI driver; the cable
  appears as `/dev/cu.usbserial-XXXX`). If yours doesn't enumerate, install
  FTDI's [VCP driver](https://ftdichip.com/drivers/vcp-drivers/).
- The correct Triumph diagnostic adapter/pinout for your bike's diagnostic
  connector (K-line, +12V, ground). Ignition ON.

## Install

```bash
git clone <this repo> && cd trecu
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # add ".[dev]" for tests, ".[ftdi]" for pyftdi
```

## Usage

Try it with **no hardware** using the simulated ECU:

```bash
trecu --mock                 # launch the TUI against a fake ECU
trecu --mock --read          # headless: print codes and exit
trecu --mock --live          # headless: print a live-sensor snapshot and exit
```

In the TUI, the **Live Data** tab streams sensor values (value · unit · running
min/max · trend sparkline); `space` freezes the stream, `←`/`→` switch tabs.

With a real cable:

```bash
trecu --list-ports           # find your /dev/cu.usbserial-XXXX
trecu --port /dev/cu.usbserial-A1B2C3   # launch the TUI
trecu --port /dev/cu.usbserial-A1B2C3 --read -v   # one-shot read + raw traffic
trecu --port /dev/cu.usbserial-A1B2C3 --live      # one-shot live-sensor snapshot
trecu --port /dev/cu.usbserial-A1B2C3 --clear     # clear codes (asks first)
```

Protocol selection (`--protocol`, default `auto`):

```bash
trecu --port /dev/cu.usbserial-XXX                      # auto: iso9141 then kwp-fast
trecu --port /dev/cu.usbserial-XXX --protocol iso9141   # force 5-baud + OBD (typical Triumph)
trecu --port /dev/cu.usbserial-XXX --protocol kwp-fast  # force KWP2000 fast-init
trecu --port /dev/cu.usbserial-XXX --protocol iso9141 --init-address 0x33
```

Port selection when launching the TUI:

- `--port` given → uses it.
- `--port` omitted and exactly one FTDI/KKL cable present → auto-selected.
- `--port` omitted and multiple (or no) ports present → the TUI opens an
  **interactive port picker** (KKL candidates listed first and marked ★; press
  `r` to rescan after plugging in the cable, `Enter` to select, `Esc` to cancel).

### Keys (TUI)

| Key | Action |
| --- | --- |
| `r` | Connect and (re-)read fault codes |
| `c` | Clear stored fault codes (with confirmation) |
| `q` | Quit |

## How it works

```
 TUI / CLI ─▶ DiagnosticService ─▶ Iso9141Client  ─┐
                    │              └ Kwp2000Client  ─┼▶ Transport ─▶ ECU
              DtcDatabase                            │
             (decode codes)          serial_kline.py (real) / mock*.py (simulated)
```

**ISO 9141-2 + OBD path** (`iso9141`, the usual Triumph case):

1. **5-baud slow init** at address `0x33` → ECU replies with sync `0x55` + key
   bytes; the tester answers with the inverted key byte and the ECU returns the
   inverted address. Session open.
2. **Mode 09** → vehicle info (PID 02 VIN, 04 calibration ID, 0A ECU name).
3. **Mode 03** (`68 6A F1 03`) → stored DTCs; **Mode 07** → pending; **Mode 01
   PID 01** → MIL status + count.
4. **Mode 04** clears codes on request.

**KWP2000 fast path** (`kwp-fast`):

1. **Fast-init** (K-line low 25 ms / high 25 ms via the UART break) →
   **StartCommunication** (`0x81`) → key bytes → **ReadEcuIdentification**
   (`0x1A`) for VIN/part/version → **ReadDTCByStatus** (`0x18`);
   **ClearDiagnosticInformation** (`0x14`) to clear.

Either way, each DTC is decoded per **SAE J2012** (`P/C/B/U` + 4 hex digits) and
looked up in `trecu/data/triumph_dtc.json`. The single-wire K-line echoes
everything the tester transmits; the client discards that echo before parsing.

## ⚠️ Important: protocol values vary by model

Triumph diagnostics were reverse-engineered by the community (see
**[TuneECU](https://tuneecu.net/)**), and the exact parameters can differ by
model, year, and ECU supplier (**Keihin** vs **Sagem**). `auto` tries both
protocols; if your bike doesn't respond, force one and tweak its parameters:

```bash
# 5-baud + OBD (most Triumphs); change the init address if needed
trecu --port /dev/cu.usbserial-XXX --protocol iso9141 --init-address 0x33 -v
# KWP2000 fast-init
trecu --port /dev/cu.usbserial-XXX --protocol kwp-fast --ecu-address 0x10 -v
```

Configs live in `trecu/protocol/iso9141.py` (`Iso9141Config`) and
`trecu/protocol/kwp2000.py` (`Kwp2000Config`). Use `-v` to see the raw byte
traffic. The **`--mock` mode exercises the whole pipeline** without a bike (it
emulates a real Triumph: one stored `P1108` with the MIL on).

The bundled code database covers standardized SAE J2012 generic (`P0xxx`) codes
plus Triumph `P1xxx`/`P2xxx` codes sourced from official service manuals
(Daytona 675, Thunderbird, Street Twin). Descriptions can still vary by model;
unknown codes decode structurally and show a generic message.

## Safety

Clearing codes only erases stored faults; it does not fix the underlying
problem, and some codes return immediately if the fault is still present. This
tool reads/clears DTCs only — it does not flash or tune the ECU.

## Development

```bash
pip install -e ".[dev]"
pytest                 # runs entirely against the mock ECU, no hardware needed
```

## License

MIT
