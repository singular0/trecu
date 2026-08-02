# trecu — architecture review & cleanup backlog

An architecture review of the current tree (~5,000 lines of `trecu/`, 182 tests
passing in ~56 s), captured as actionable work. This is **maintenance and
structural debt**, distinct from `ROADMAP.md`, which tracks *features* by phase.
Nothing here changes what the tool does; it changes how cheaply the roadmap
phases can be built on top.

The four-layer split (CLI/TUI → `DiagnosticService` → protocol clients →
transport, see `CLAUDE.md`) is sound and is **not** being questioned. What
follows is where the seams have frayed.

## What's holding up well — don't "fix" these

- `protocol/framing.py` as a pure, I/O-free module, testable in isolation.
- The restricted formula interpreter instead of `eval` (`pids.py:56-86`), with
  load-time validation so a bad table entry fails on startup, not mid-poll.
- Model-specific values in `@dataclass` configs + `data/*.json` rather than
  inlined constants.
- The `_io_lock` discipline, which makes the half-duplex K-line constraint
  structural rather than conventional.
- The "Known real-hardware facts" section of `CLAUDE.md` — knowledge that is not
  derivable from the code and would be lost without it.

---

## Suggested order

1. **CI on push/PR** (§7) — minutes of work, protects everything else.
2. **`CLAUDE.md` sync** (§10) — cheap, and every later session reads it first.
3. ~~**`EcuClient` Protocol** (§1)~~ — **done**: the `getattr` layer is gone.
4. ~~**`--protocol auto` config overrides** (§4)~~ — **done**: the only
   user-visible defect; `EcuConfig` carries both protocol sections through the
   sweep.
5. ~~**CLI `_with_service` + slow-init retry extraction** (§5)~~ — **done**:
   all four duplicate pairs collapsed.
6. ~~**`SessionController` extraction** (§2)~~ and ~~**`app.py` breakup** (§8)~~
   — **done**: modal screens and the live table are now their own modules, so
   Phase 4's extra tab lands in a 736-line `app.py`.
7. **`LiveDecoder` seam** (§6) — do it *with* the F4 Keihin capture, not before;
   the capture will tell you what the seam actually needs.

---

## 1. The duck-typed client contract is unnamed and unenforced — **done**

`CLAUDE.md` described a precise client interface, but nothing in the code stated
it, so the service defended against its own peers at runtime:

- `service.py:256,336,380,384` — four `getattr(client, ...)` probes for
  `keepalive`, `read_identification`, `read_live`, `live_source`. **Both**
  clients define all four. The probes were dead defensiveness that also silently
  swallowed a genuinely missing method.
- `service.py:112,116` — `config: Optional[object]`, `client: Optional[object]`.
- `service.py:272,274` — `isinstance(self.config, Iso9141Config)` sniffing to
  decide which config a caller meant.
- Same pattern one layer down: `getattr(self.transport, "supports_slow_init",
  False)` at `iso9141.py:133`, `kwp2000.py:151,491` — but `Transport` already
  defines that attribute (`base.py:27`).

- [x] Defined `class EcuClient(typing.Protocol)` in `kwp2000.py` (the stated home
      of shared vocabulary), `@runtime_checkable`, covering `connect`,
      `read_dtcs`, `read_identification`, `read_live`, `clear_dtcs`,
      `keepalive`, `stop_communication`, plus `live_source` / `dtc_family` —
      those two declared read-only so a class attribute *or* a computed property
      (`Kwp2000Client.dtc_family`) satisfies them. Nothing inherits from it.
- [x] Typed `_build_client` / `_connect` / `_active` / the `client=` injection as
      `EcuClient`; all four `getattr` probes are now direct calls.
      `stop_diagnostic_session` stays a `getattr` probe **on purpose** — it is
      KWP-only, outside the contract, and now says so in a comment.
- [x] Dropped the `getattr(transport, "supports_slow_init", False)` defaults —
      the base class guarantees the attribute.
- [x] Covered by `tests/test_client_contract.py` (both clients + the 0x18
      property variant conform; a client missing `keepalive` does not), plus
      `SpyClient` in `tests/test_session.py` now implements the full contract and
      asserts it.

`config` stayed `Optional[object]` here — which config type is legitimate depends
on the protocol. §4 resolved it: `Optional[ConfigLike]`.

Payoff: adding a third protocol client gets a checkable checklist instead of a
prose one.

## 2. The TUI has two different connect paths — **done**

`action_read` connected behind a cancelable modal. But `_do_poll_live` →
`_session_read_live` → `_ensure_session()` **also** connected — synchronously,
no spinner, no Cancel, no port-picker fallback. `_do_poll_live` even set
`connecting` state, acknowledging the path existed.

**Repro:** let the initial read fail or cancel it, then arrow onto Live Data —
silent multi-second block with no way out.

Both paths also constructed `DiagnosticService` from an identical 8-argument
list — a copy-paste pair waiting to drift.

- [x] Extract a `SessionController` (`tui/session.py`, **no Textual import**)
      owning the session, the in-flight attempt, and the single connect path.
      `async connect()` returns a `ConnectResult`
      (`CONNECTED`/`CANCELLED`/`FAILED`); `build_service()` is now the only
      place the TUI constructs a `DiagnosticService`.
- [x] Route the live-poll path through it: entering Live Data while
      disconnected gets the same modal + Cancel + port-picker fallback as Read,
      and a cancelled/failed connect there stops the poll loop instead of
      retrying every tick behind the modal.
- [x] Cancel **detaches** its attempt (per-attempt cancel flag) so a later
      connect starts fresh rather than inheriting the doomed outcome; concurrent
      callers otherwise share one attempt instead of opening the port twice.
- [x] Covered by `tests/test_session_controller.py` (9 tests, no TUI) plus two
      TUI regressions in `tests/test_tui_connecting_modal.py`.

Payoff: shrinks `app.py` and makes the connect/cancel state machine testable
without driving a TUI.

## 3. `DiagnosticService` binds a Transport instance, not a factory — **done**

`close()` closed the transport, so reconnecting worked only because *every*
transport happens to be re-openable — a promise the `Transport` ABC never made.
That left the device's lifecycle half-owned by the caller: the TUI held a
`transport_factory` and rebuilt the whole service to reconnect
(`SessionController.build_service`), and `_cmd_tui` hand-shared one mock
instance across connects (`cli.py:303-308`).

- [x] `DiagnosticService(transport)` now takes `Callable[[], Transport]` **or** a
      `Transport`, normalized by `as_transport_factory()` beside `as_ecu_config()`.
      The device is built lazily (`_device()`), released by `close()`
      (`_release_device()`), and exposed as `service.transport` — the device
      currently held, `None` once closed.
- [x] Reconnect is therefore a service operation: `close()` →
      `open()`/`start_session()` gets a **fresh** device from the factory, with
      no caller-side rebuild and no reliance on `Transport.open()` being
      re-entrant after a close.
- [x] `SessionController.build_service` hands the factory over instead of
      calling it, so the service owns the whole device lifecycle and an attempt
      that never runs never builds a port.
- [x] Covered in `tests/test_session.py` (fresh device per session, an instance
      pinned across sessions, no device built when a service never opens, and
      the `TypeError` on a non-transport).

Two parts of the original item turned out **not** to be deletable, and are now
documented as intentional rather than left to be "fixed" again later:

- `SessionController` still builds one service **per connect attempt**. That is
  §2's cancel isolation, not a single-use workaround: a cancelled attempt keeps
  running (blocked in serial I/O) inside its own service's `_io_lock`, and
  sharing one service would let its teardown land on a newer session.
- `_cmd_tui`'s shared mock stays. It is the *point* of the instance form — a
  factory that built a fresh mock ECU per session would resurrect the codes the
  user just cleared.

## 4. `--protocol auto` silently discards connection flags — **done**

`_make_config` returned `None` for auto, so `--init-address`, `--ecu-address`,
`--tester-address`, and `--timeout` were dropped in the **default** mode.
Meanwhile `_make_transport` *did* honour `--init-address` for the mock:

```
$ trecu faults --mock --init-address 0x43
error: could not connect: iso9141: 5-baud init failed: no 0x55 sync byte ...
```

The mock ECU moved to `0x43`; the client stayed at `0x33`. On real hardware the
failure mode was worse — the flag was just quietly inert. This contradicted
`CLAUDE.md`'s own rule that per-bike values must be overridable.

- [x] Added `EcuConfig` (`service.py`) — one object with an `iso9141` and a
      `kwp2000` section, so both survive a sweep that builds a fresh client per
      candidate. `_build_client` reads its candidate's section; the
      `isinstance(self.config, …)` sniffing is gone.
- [x] `as_ecu_config()` normalizes in `DiagnosticService.__init__`, so a bare
      `Iso9141Config` / `Kwp2000Config` (what tests and the TUI pass) still
      works — it fills its own section and leaves the other at its defaults.
- [x] The four CLI flags now default to `None` = "leave that protocol's default
      alone" (their `--help` text still names the default), and `_make_config`
      fills **both** sections in every mode. Without this the CLI's own
      `--timeout` default would have flattened the two protocols' genuinely
      different `p2_timeout` defaults (0.8 vs 1.0) in auto mode.
- [x] `_make_transport(args, config)` builds the mock ECU from that same
      config, so an override moves the simulated ECU and the tester together
      rather than only one of them.
- [x] Covered by `tests/test_connection_config.py` (10 tests: flag→section
      mapping, normalization, per-candidate section selection, the CLI repro
      above, and an auto sweep that only reaches `kwp-slow` if the
      `--ecu-address` override survived it).

This also resolves §1's leftover: `config` is now typed `Optional[ConfigLike]`
(`EcuConfig | Iso9141Config | Kwp2000Config`) rather than `Optional[object]`,
in the service and in the TUI's `SessionController` / `TrecuApp`.

## 5. Duplication worth collapsing — **done**

| Where | What |
|---|---|
| ~~`cli.py:187-288`~~ | ~~`_cmd_read`/`_cmd_info`/`_cmd_live`/`_cmd_clear` repeat the same transport+service build, `with service:`, and `except (TransportError, ProtocolError) → return 2`.~~ |
| ~~`iso9141.py:132-148` vs `kwp2000.py:481-516`~~ | ~~Near-identical slow-init retry loops.~~ |
| ~~`Iso9141Config:59-67` vs `Kwp2000Config:243-248`~~ | ~~`w4`, `sync_timeout`, `byte_timeout`, `init_retries`, `retry_wait` duplicated field-for-field.~~ |
| ~~`pids.py:152-293`~~ | ~~`PidDatabase` and `KwpLocalTable` are two wrappers over `Dict[int, PidDef]` with the same four constructors and dunders.~~ |
| ~~`app.py:53-56` vs `pids.py:146-149`~~ | ~~`_fmt_value` re-implements `SensorReading.formatted()`.~~ **done** — see §8. |

- [x] One `_with_service(args, operation, show)` in `cli.py` behind all four ECU
      commands, each now a single line. It owns the config, the transport built
      from that same config, the `with service:` lifecycle, and the exit-2
      mapping; `show` runs *after* the session closes, so a formatting bug can't
      read as a connection error. The four `logger = lambda …` E731s are one
      `_stderr` function, and `_cmd_info`'s inline table is a `_print_info`
      beside the other two printers.
- [x] Composed `SlowInitConfig` (the five handshake/retry fields) and
      `slow_init_with_retries()` beside `slow_init_handshake` in `kwp2000.py`.
      Both configs now carry a `slow_init` section instead of repeating the
      fields, mirroring how `EcuConfig` sections its two protocols; only the
      init *address* stays per-protocol, because that is a protocol fact and not
      handshake timing. `Iso9141Client.connect` and `Kwp2000Client._slow_connect`
      are each a single call into the shared loop, which also absorbed the
      `supports_slow_init` refusal both were doing themselves.
- [x] Gave `PidDatabase` / `KwpLocalTable` a `_SensorTable` base for the
      load/lookup half (`data_file` + `load_default`/`load_file`, `__len__`,
      `__contains__`, `get`, `ids`). Each subclass keeps its own `from_dict`
      (the two files have different shapes), its own id vocabulary
      (`pids()`/`channels()`), and its own decode surface.
- [x] Delete `app._fmt_value` in favour of a shared `pids.format_value()`
      (`SensorReading.formatted()` delegates to it) — the live table's running
      min/max are derived numbers, not readings, so they need the plain helper.
- [x] Covered by `tests/test_slow_init.py` (8 tests driving *both* clients
      through the shared loop: one config section, garbled-first-init recovery,
      an exactly-spent retry budget, and the up-front capability refusal), plus
      a parametrized "all four ECU commands share one failure path" in
      `tests/test_cli.py` and two `_SensorTable` tests in `tests/test_pids.py`.

Two behaviours changed slightly, both fixes:

- `init_retries=0` now means "don't *retry*", one attempt — the iso9141 loop
  read it as "don't try" and failed with a bare `5-baud init failed: None`.
  (kwp2000's loop already had the `max(1, …)`.)
- A transport that can't slow-init is refused by the shared loop *before* the
  retry budget, so the message stays unwrapped rather than arriving as
  `5-baud init failed: transport does not support …` after N settle waits.

The retry loop is now also the single place §7's injectable sleeper goes.

## 6. The live-data seam is the weakest part of the design

`service.read_live` (`service.py:354-413`) branches on a stringly-typed
`live_source`, then juggles `frame` / `raw` / `requested` across the lock
boundary such that each variable is bound on only one branch. It works, but it's
the one place where adding a third live path means editing the service rather
than slotting into a seam.

Also:

- `PidDef.frame_offset` (`pids.py:104`) is a `kwp_local`-only field living on the
  descriptor both paths share.
- The TUI pre-loads `PidDatabase` but not `KwpLocalTable`, so all 53 Keihin
  formulas recompile on every connect (`service.py:124`).

- [ ] Introduce a `LiveDecoder` per source — `{source: decoder}` — where the
      decoder owns both "what to request" and "how to split the answer". The
      service then calls `decoder.request_ids(pids)` / `decoder.decode(raw)`.
- [ ] Pass `kwp_local=` from the TUI like `pids=`.

**Timing:** do this alongside the F4 Keihin `21 80` capture (see `ROADMAP.md`),
which is what will push hardest on this seam.

## 7. No CI outside the release gate

`.github/workflows/` contains only `release.yml`. The mock-only suite is a
genuine asset and runs *only* when a semver tag is pushed — so a regression is
discovered at release time.

- [ ] Add `test.yml` running the suite on push/PR. Cheapest item in this file.
- [ ] Runtime (~45 s) is dominated by real `time.sleep` (`retry_wait: float =
      2.0` × 4 attempts × 3 auto candidates). Inject the sleeper into the retry
      helper — cuts the suite to a few seconds *and* lets tests assert retry
      counts.

## 8. `tui/app.py` is 988 lines — **done** (now 736)

- [x] Moved the three modal screens (~170 lines) to `tui/screens.py`, next to
      the existing `tui/port_select.py`.
- [x] Extracted the live-table machinery into a `DataTable` subclass,
      `LiveTable` (`tui/live_table.py`): columns, per-sensor stats, `sparkline`,
      `update_readings`, `reset`, and the `#live` CSS now live with the widget.
      `app.py` only hands it readings and resets it on a fresh stream.
- [x] Replaced `_live_stats`' `{"min","max","hist"}` string-keyed dict with a
      `_Stats` dataclass (`minimum`/`maximum`/`history`, `seed()` + `add()`).
- [x] Also closed the §5 `_fmt_value` duplicate rather than relocating it:
      `pids.format_value()` is now the one formatter, and
      `SensorReading.formatted()` delegates to it.

## 9. Dead / speculative API

No callers outside their own module (or tests only):

- `PidDatabase.decode_all` (`pids.py:287`)
- `load_file` classmethods — `pids.py:174,246`, `dtc.py:125`
- `_mock_live.supported_pids()`
- `EcuInfo.summary()` (`kwp2000.py:294`) — the spine it fed is gone
- `Iso9141Client.read_status` (`iso9141.py:241`) — exists only for one test

- [ ] Delete, **except** `load_file`: keep it only if a `--pid-table FILE` flag
      gets wired, which F4's data-only-fix story would actually benefit from.

## 10. `CLAUDE.md` has drifted from the code

It steers every future session here, so its errors compound.

- [ ] CLI section documents `trecu --mock --read` / `--live` / `--list-ports` /
      `-v`. The CLI is now subcommands: `tui|ports|faults|info|sensors|clear|
      version|help` with `--debug` (`cli.py:36-78`). `README.md` is correct;
      `CLAUDE.md` is not.
- [x] "88 tests, ~21s" → 182 tests, ~56 s (fixed with §5, which moved the
      count; the other three bullets here are untouched).
- [ ] "There is **no PyPI publish** — install is from the release wheel URL" —
      but `release.yml:109-128` has a Trusted-Publishing PyPI job (commit
      f3e3797).
- [ ] `ROADMAP.md` still describes the spine's `⚡` keepalive lamp and MIL lamp,
      which the Faults-tab tinting replaced.

## Minor

- [ ] `Transport.fast_init` is `@abstractmethod` while `five_baud_init` is
      optional-with-raise (`base.py:53-67`), so `MockObdTransport` must
      implement-and-raise (`mock_obd.py:68`). Make both optional, gated by the
      capability flags that already exist.
- [ ] `_Keepalive.beats` (`service.py:79`) is incremented from the ticker thread
      and read elsewhere without synchronization.
- [ ] No `conftest.py` and no fixtures anywhere; six test files construct
      `TrecuApp` independently.
- [ ] `pids.py:108` — a table entry missing `"formula"` raises `KeyError`, not
      the `FormulaError` the module docstring promises for load-time failures.
