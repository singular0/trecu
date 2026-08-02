# trecu — architecture review & cleanup backlog

An architecture review of the current tree (~2,900 lines of `trecu/`, 153 tests
passing in ~50 s), captured as actionable work. This is **maintenance and
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
4. **`--protocol auto` config overrides** (§4) — the only user-visible defect.
5. **CLI `_with_service` + slow-init retry extraction** (§5) — mechanical, low risk.
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

`config` stays `Optional[object]` — which config type is legitimate depends on
the protocol, and §4 is where that gets resolved.

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

## 3. `DiagnosticService` binds a Transport instance, not a factory

`close()` closes the transport, so a service is effectively single-use — which is
why the TUI holds a `transport_factory` and rebuilds the whole service to
reconnect (`SessionController.build_service`), and why `_cmd_tui` has to
hand-share one mock instance across connects (`cli.py:303-308`).

- [ ] Take `Callable[[], Transport]` instead of a `Transport`, making reconnect a
      service operation and deleting the TUI-side workaround.

## 4. `--protocol auto` silently discards connection flags

`cli.py:98` returns `None` for auto, so `--init-address`, `--ecu-address`,
`--tester-address`, and `--timeout` are dropped in the **default** mode.
Meanwhile `_make_transport` *does* honour `--init-address` for the mock.
Verified:

```
$ trecu faults --mock --init-address 0x43
error: could not connect: iso9141: 5-baud init failed: no 0x55 sync byte ...
```

The mock ECU moved to `0x43`; the client stayed at `0x33`. On real hardware the
failure mode is worse — the flag is just quietly inert. This contradicts
`CLAUDE.md`'s own rule that per-bike values must be overridable.

- [ ] Carry the overrides through auto mode: either one config object holding
      both protocol sections, or an override dict applied per candidate in
      `_build_client`.

## 5. Duplication worth collapsing

| Where | What |
|---|---|
| `cli.py:187-288` | `_cmd_read`/`_cmd_info`/`_cmd_live`/`_cmd_clear` repeat the same transport+service build, `with service:`, and `except (TransportError, ProtocolError) → return 2`. |
| `iso9141.py:132-148` vs `kwp2000.py:481-516` | Near-identical slow-init retry loops. |
| `Iso9141Config:59-67` vs `Kwp2000Config:243-248` | `w4`, `sync_timeout`, `byte_timeout`, `init_retries`, `retry_wait` duplicated field-for-field. |
| `pids.py:152-293` | `PidDatabase` and `KwpLocalTable` are two wrappers over `Dict[int, PidDef]` with the same four constructors and dunders. |
| ~~`app.py:53-56` vs `pids.py:146-149`~~ | ~~`_fmt_value` re-implements `SensorReading.formatted()`.~~ **done** — see §8. |

- [ ] One `_with_service()` helper in `cli.py` covering all four commands (also
      drops four `logger = lambda …` E731s).
- [ ] Compose a shared `SlowInitConfig` and a `slow_init_with_retries()` beside
      `slow_init_handshake` in `kwp2000.py`.
- [ ] Give `PidDatabase` / `KwpLocalTable` a shared base for the load/dunder half.
- [x] Delete `app._fmt_value` in favour of a shared `pids.format_value()`
      (`SensorReading.formatted()` delegates to it) — the live table's running
      min/max are derived numbers, not readings, so they need the plain helper.

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
- [ ] "88 tests, ~21s" → 153 tests, ~50 s.
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
