# TrECU

**TrECU** is a terminal app (TUI) that reads, decodes, and clears **fault
codes from Triumph motorcycle ECUs** — and streams **live sensor data** — over a
cheap **KKL diagnostic cable based on the FTDI FT232RL** chip. It talks to the
ECU directly on the single-wire K-line, so there is no ELM327 or OBD dongle in
the middle: just a plain FTDI KKL cable and the right Triumph adapter for your
bike's diagnostic connector.

## Features

- **ISO 9141-2 / OBD-II over the K-line** — a 5-baud slow init at `0x33`, then
  standard OBD-II services. One protocol, the one confirmed on a real Triumph.
- **ECU identification** — VIN, calibration ID, part number, and ECU name.
- **Read fault codes** — stored and pending DTCs, decoded to SAE J2012 codes
  (`P/C/B/U` + 4 hex digits) and described with **official service-manual
  wording** — the bundled dictionary covers 415 Triumph codes across the
  `P`/`C`/`U` families.
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
Bonneville 865 EFI (2009)**, on **Intel macOS**. The code is pure Python on
cross-platform libraries (pyserial, textual), so it should also run on Linux and
Windows — but that is untested. It may or may not work on your bike.

Triumph diagnostics are not officially documented — everything here rests on
community reverse engineering, and the exact protocol parameters differ by
model, year, and ECU supplier (**Keihin** vs **Sagem**). Codes read from an
untested model may be wrong, incomplete, or fail to read at all. (For example,
some Sagem models are reported to use 5-baud init address `0x43` instead of
the OBD-standard `0x33` — if ISO 9141-2 won't connect, try
`--init-address 0x43`.)

**Scope: engine-ECU ISO 9141-2 / OBD-II diagnostics only.** No CAN modules, no
ABS, no manufacturer-specific service functions, no tuning, no programming.
Modern CAN-based Triumphs, and the ABS and instrument modules even on K-line
bikes, talk CAN and are **out of reach for this hardware** — that's an
ELM327/CAN-interface job (e.g. TuneECU), not a TrECU one. TrECU deliberately
does not implement the community-derived KWP2000/Keihin path either: it was
never validated against a bike, so it is not shipped as if it were.

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

- **Python 3.10+**
- A KKL diagnostic cable based on the **FTDI FT232RL** chip, plus the correct
  Triumph adapter/pinout for your bike's connector (K-line, +12V, ground)
- An **FTDI serial (VCP) driver**, so the cable enumerates as an ordinary serial
  port — `/dev/ttyUSB0` (Linux), `/dev/cu.usbserial-XXXX` (macOS), or `COMx`
  (Windows). Linux and recent macOS ship one in-box; on Windows install FTDI's
  [VCP driver](https://ftdichip.com/drivers/vcp-drivers/)
- Ignition **ON**

### Install

Install the latest release from [PyPI](https://pypi.org/project/trecu/):

```bash
pip install trecu
```

Prefer an isolated install for a command-line tool?
[`pipx`](https://pypa.github.io/pipx/) installs it into a dedicated environment:

```bash
pipx install trecu
```

<details>
<summary>Install from source (for development)</summary>

```bash
git clone https://github.com/singular0/trecu.git && cd trecu
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

The package version is derived from the git tag at build time (via `hatch-vcs`),
so an editable checkout reports a development version until you build from a
tagged commit.

</details>

### CLI

```bash
trecu                      # launch the interactive terminal UI (default)
trecu tui                  # launch it explicitly
trecu ports                # list detected serial ports
trecu faults               # read and print stored fault codes
trecu info                 # print ECU identification
trecu sensors              # print a live-sensor snapshot
trecu clear                # clear stored fault codes (asks first)
trecu clear -y             # clear without prompting
trecu faults --debug       # include raw protocol traffic
trecu version
trecu help
```

Diagnostic commands auto-select the cable when exactly one FTDI/KKL device is
present. Use `--init-address` and `--timeout` to override connection parameters.

When `trecu tui` cannot auto-select a single cable, it opens the port picker.
Inside the UI, use `r` to read faults, `c` to clear them, `←`/`→` to switch
tabs, `space` to freeze live data, and `q` to quit.

## License

TrECU is licensed under the **GNU General Public License, version 3 or later
(GPL-3.0-or-later)**. See the [`LICENSE`](LICENSE) file or
<https://www.gnu.org/licenses/gpl-3.0.html>.
