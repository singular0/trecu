# TrECU

**TrECU** is a macOS terminal app (TUI) that reads, decodes, and clears **fault
codes from Triumph motorcycle ECUs** — and streams **live sensor data** — over a
cheap **KKL diagnostic cable based on the FTDI FT232RL** chip. It talks to the
ECU directly on the single-wire K-line, so there is no ELM327 or OBD dongle in
the middle: just a plain FTDI KKL cable and the right Triumph adapter for your
bike's diagnostic connector.

## Features

- **Auto-detected protocol** — ISO 9141-2 (5-baud slow init + OBD-II, the usual
  Triumph path) or KWP2000 fast-init, chosen automatically or forced.
- **ECU identification** — VIN, calibration ID, part number, and ECU name.
- **Read fault codes** — stored and pending DTCs, decoded to SAE J2012 codes
  (`P/C/B/U` + 4 hex digits) with human-readable, Triumph-specific descriptions.
- **Clear fault codes** — behind a confirmation guard.
- **Live sensor streaming** — engine RPM, coolant temp, throttle position,
  intake MAP, O2, and battery voltage in a continuously-updating table with
  running min/max and trend sparklines.
- **Keyboard-driven TUI** — Dashboard, Faults, Live Data, and protocol Log tabs
  over one persistent, kept-alive ECU session.
- **Headless CLI** — one-shot read, live snapshot, clear, and port listing for
  scripting or a quick check.

## Disclaimer & background

**This is a hobby, "vibe-coded" project.** It was written quickly with heavy AI
assistance and has only ever been tested against **one motorcycle: a Triumph
Bonneville 865 EFI (2009)**. It may or may not work on your bike.

Triumph diagnostics are not officially documented — everything here rests on
community reverse engineering, and the exact protocol parameters differ by
model, year, and ECU supplier (**Keihin** vs **Sagem**). Codes read from an
untested model may be wrong, incomplete, or fail to read at all.

If you want a proven, mature tool with years of real research and broad model
coverage, use one of these instead — TrECU is not a replacement:

- **[TuneECU](https://tuneecu.net/)** — the established community tool for
  Triumph (and other) ECUs: diagnostics, live data, tuning, and reflashing,
  refined over many years and across many models.

Clearing codes only erases stored faults; it does not fix the underlying problem,
and a code returns if the fault is still present. TrECU **reads/clears DTCs and
reads live data only — it never flashes or tunes the ECU.** Use at your own risk.

## Usage

### Requirements

- macOS with **Python 3.10+**
- A KKL diagnostic cable based on the **FTDI FT232RL** chip, plus the correct
  Triumph adapter/pinout for your bike's connector (K-line, +12V, ground)
- The **FTDI VCP driver**, so the cable enumerates as `/dev/cu.usbserial-XXXX`
  (recent macOS bundles one; otherwise install FTDI's
  [VCP driver](https://ftdichip.com/drivers/vcp-drivers/))
- Ignition **ON**

### Install

```bash
git clone <this repo> && cd trecu
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # add ".[ftdi]" for direct pyftdi access
```

### Launch the TUI

```bash
trecu --list-ports                        # find your /dev/cu.usbserial-XXXX
trecu                                      # auto-select the cable (or open a picker)
trecu --port /dev/cu.usbserial-A1B2C3      # use a specific port
```

With no `--port`, trecu auto-selects when exactly one FTDI/KKL cable is present,
and otherwise opens an interactive port picker. In the TUI: `r` read fault codes,
`c` clear them (with confirmation), `←`/`→` switch tabs, `space` freeze the Live
Data stream, `q` quit.

### Headless CLI

```bash
trecu --port /dev/cu.usbserial-A1B2C3 --read       # read + print codes, then exit
trecu --port /dev/cu.usbserial-A1B2C3 --live       # print a live-sensor snapshot
trecu --port /dev/cu.usbserial-A1B2C3 --clear      # clear codes (asks first)
trecu --port /dev/cu.usbserial-A1B2C3 --read -v    # same, plus raw byte traffic
```

If auto-detection fails on your bike, force a protocol and adjust its parameters
— for example `--protocol iso9141 --init-address 0x33`, or `--protocol kwp-fast`.
Run with `-v` to see the raw K-line traffic.

## License

TrECU is licensed under the **GNU General Public License, version 3 or later
(GPL-3.0-or-later)**. See the [`LICENSE`](LICENSE) file or
<https://www.gnu.org/licenses/gpl-3.0.html>.
