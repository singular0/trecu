# trecu — architecture review & cleanup backlog

An architecture review of the tree (~5,000 lines of `trecu/`, 187 tests passing
in ~56 s), captured as actionable work. This is **maintenance and structural
debt**, distinct from `ROADMAP.md`, which tracks *features* by phase. Nothing
here changes what the tool does; it changes how cheaply the roadmap phases can
be built on top.

The four-layer split (CLI/TUI → `DiagnosticService` → protocol clients →
transport, see `CLAUDE.md`) is sound and is **not** being questioned. What
follows is where the seams have frayed.

**Two items are open** (§6, §7); the other nine are closed and archived at the
bottom. Section numbers are stable — closed items keep theirs, so old references
still resolve.

## What's holding up well — don't "fix" these

- `protocol/framing.py` as a pure, I/O-free module, testable in isolation.
- The restricted formula interpreter instead of `eval` (`pids.py:69-100`), with
  load-time validation so a bad table entry fails on startup, not mid-poll.
- Model-specific values in `@dataclass` configs + `data/*.json` rather than
  inlined constants.
- The `_io_lock` discipline, which makes the half-duplex K-line constraint
  structural rather than conventional.
- The "Known real-hardware facts" section of `CLAUDE.md` — knowledge that is not
  derivable from the code and would be lost without it.

---

# Open

Do **§7 first** — it's minutes of work and protects everything else. **§6** waits
on the F4 Keihin capture (see below).

## 6. The live-data seam is the weakest part of the design

`service.read_live` (`service.py:461-515`) branches on a stringly-typed
`live_source`, then juggles `frame` / `raw` / `requested` across the lock
boundary such that each variable is bound on only one branch. It works, but it's
the one place where adding a third live path means editing the service rather
than slotting into a seam.

Also:

- `PidDef.frame_offset` (`pids.py:117`) is a `kwp_local`-only field living on the
  descriptor both paths share.
- The TUI pre-loads `PidDatabase` but not `KwpLocalTable`
  (`tui/session.py:154` passes `pids=` only), so all 53 Keihin formulas
  recompile on every connect (`service.py:214`).

- [ ] Introduce a `LiveDecoder` per source — `{source: decoder}` — where the
      decoder owns both "what to request" and "how to split the answer". The
      service then calls `decoder.request_ids(pids)` / `decoder.decode(raw)`.
- [ ] Pass `kwp_local=` from the TUI like `pids=`.

**Timing:** do this alongside the F4 Keihin `21 80` capture (see `ROADMAP.md`),
which is what will push hardest on this seam — the capture will tell you what
the seam actually needs.

## 7. No CI outside the release gate

`.github/workflows/` contains only `release.yml`. The mock-only suite is a
genuine asset and runs *only* when a semver tag is pushed — so a regression is
discovered at release time.

- [ ] Add `test.yml` running the suite on push/PR. Cheapest item in this file.
- [ ] Runtime (~56 s) is dominated by real `time.sleep` (`SlowInitConfig`'s
      `retry_wait: 2.0` × `init_retries: 4` × 3 auto candidates, slept at
      `kwp2000.py:254`). Inject the sleeper into `slow_init_with_retries` — the
      single retry loop since §5 — which cuts the suite to a few seconds *and*
      lets tests assert retry counts.

---

# Closed

Full write-ups are in git history (`git log -- TODO.md`); the durable rationale
that outlived each item lives in `CLAUDE.md`.

| § | Item | Outcome |
|---|---|---|
| 1 | The duck-typed client contract was unnamed and unenforced | `EcuClient`, a `@runtime_checkable` `typing.Protocol` in `kwp2000.py`; all four `getattr` probes are direct calls (`stop_diagnostic_session` stays a probe on purpose — KWP-only, outside the contract). `tests/test_client_contract.py`. |
| 2 | The TUI had two different connect paths | `SessionController` (`tui/session.py`, Textual-free) owns the session and the single connect path; the live-poll path gets the same modal + Cancel + picker fallback. Cancel **detaches** its attempt, which is why the controller still builds one service per attempt. |
| 3 | `DiagnosticService` bound a Transport instance, not a factory | Takes `Callable[[], Transport]` **or** a `Transport` via `as_transport_factory()`; device built lazily, released by `close()`, so reconnect is a service operation. `_cmd_tui`'s shared mock instance stays — a fresh mock per session would resurrect cleared codes. |
| 4 | `--protocol auto` silently discarded connection flags | `EcuConfig` carries an `iso9141` **and** a `kwp2000` section through the sweep; the four CLI flags default to `None` = "leave that protocol's default alone". Also typed `config` as `ConfigLike`, closing §1's leftover. |
| 5 | Duplication worth collapsing | One `cli._with_service` behind all four ECU commands; `SlowInitConfig` + `slow_init_with_retries()` behind both clients' slow init; a `_SensorTable` base under `PidDatabase`/`KwpLocalTable`; one `pids.format_value()`. Two behaviour fixes fell out: `init_retries=0` now means one attempt, and a transport that can't slow-init is refused before the retry budget. |
| 8 | `tui/app.py` was 988 lines | Now 737: modal screens → `tui/screens.py`, the live table → `LiveTable` in `tui/live_table.py`, and the stats dict → a `_Stats` dataclass. |
| 9 | Dead / speculative API | Five members deleted: `PidDatabase.decode_all`, the three `load_file` classmethods (with the `--pid-table FILE` idea — re-add both together if a path override ever earns its keep), `_mock_live.supported_pids()`, `EcuInfo.summary()`, `Iso9141Client.read_status`. |
| 10 | `CLAUDE.md` had drifted from the code | CLI section rewritten to the real subcommand surface, test counts refreshed, the PyPI Trusted-Publishing job documented, and `ROADMAP.md`'s retired spine lamps rewritten as *dropped, and why*. |
| — | Minor | Both `Transport` inits are optional-with-raise behind the capability flags; `_Keepalive.beats` is lock-guarded; `tests/conftest.py` + `tests/mock_ecus.py` hold the shared fixtures/doubles; `PidDef.from_entry` raises `FormulaError` for a missing formula. |
