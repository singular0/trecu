# trecu TODO

This is the single backlog for the project. It contains only unfinished work,
ordered by priority, dependency, and safety risk. A protocol or model-specific
feature is not considered supported until a sanitized hardware capture has been
turned into a replay fixture and a byte-exact test.

Priority key: **P0** correctness/release safety, **P1** validated read-only
capability, **P2** user-facing diagnostics, **P3** controlled ECU commands,
**P4** research-heavy flash access, **P5** explicitly optional/highest risk.

1. **P0 — Add continuous integration and make the mock suite fast.**

   - Add `.github/workflows/test.yml` for pushes and pull requests. Install the
     package with development dependencies and run the entire mock-only suite on
     every supported Python version chosen for CI; keep `release.yml`'s test job
     as the independent release gate.
   - Inject a sleeper into `slow_init_with_retries`, defaulting to
     `time.sleep`, and use a no-op/recording sleeper in retry tests. Assert the
     number of attempts, requested delays, final error, and the
     `init_retries=0` one-attempt behavior without real waits.
   - Keep all automated tests hardware-independent and cache only disposable
     dependency data.
   - **Done when:** a branch push and pull request both run the suite, and the
     local mock suite completes in a few seconds rather than spending most of
     its time in configured retry sleeps.

2. **P0 — Introduce an explicit 2009 Bonneville 865 Keihin profile.**

   - Model the tested Keihin ECU as two endpoints owned by one named profile:
     ISO 9141-2/OBD slow init at `0x33`, and enhanced KWP slow init at physical
     ECU/tester addresses `0xD5`/`0xF5` at 10,400 baud.
   - Move `0xD5`, `0xF5`, diagnostic session `10 03`, service choices,
     keepalive choice, identification records, and any programming timing into
     that profile. Do not leave Bonneville values as protocol-wide KWP
     defaults; retain `0x33` as the emissions-OBD default.
   - Stop sending `83 03 1E 02 0A 14 00` during an ordinary diagnostic
     connection. Make it an opt-in part of the high-speed/programming
     transition that actually requires those values.
   - Choose and implement one consistent session policy: optional setup must
     tolerate both negative responses and timeouts, while required setup must
     fail the connection. Until SecurityAccess succeeds, report the enhanced
     connection as limited rather than fully established.
   - Describe auto detection precisely in CLI help, user docs, code comments,
     and tests: ISO 9141 first, KWP slow second, and KWP fast only as an extra
     unvalidated probe. Remove claims that the tested bike is Sagem; keep Sagem
     references only where they describe a distinct, unvalidated model or
     address override.
   - Add byte-exact profile and connection tests so later generic KWP changes
     cannot silently alter Bonneville traffic.
   - **Done when:** all tested-bike configuration comes from the named profile,
     ordinary connection traffic contains no programming-only timing request,
     and no documentation or UI identifies the tested ECU incorrectly.

3. **P0 — Harden response parsing before adding enhanced services.**

   - Validate every KWP response's target and source against the configured
     tester/ECU addresses (`F5`/`D5` for the Bonneville profile), in addition to
     its checksum and positive service ID. Reject wrong-address frames rather
     than accepting traffic for another module.
   - Reject an ISO 9141 response with a bad checksum. Do not warn and continue
     with data that may decode into a plausible but false value.
   - Add bounded multi-frame Mode `09` reassembly for VIN, calibration ID, and
     ECU name. Define handling for sequence/count fields, duplicate or missing
     fragments, inter-frame timeouts, and unrelated frames.
   - Keep KWP records `0xA0`, `0xAE`, and `0x8C` as raw, labelled values until a
     capture proves which is VIN, hardware, software, or calibration data.
   - Add negative tests for wrong addresses, bad checksums, truncated frames,
     incomplete/out-of-order Mode `09` records, and unexpected service IDs.
   - **Done when:** corrupt, misaddressed, or incomplete traffic cannot surface
     as an ECU identity, DTC, or sensor reading.

4. **P0 — Correct the Bonneville diagnostic-service defaults.**

   - Keep Mode `03` as the stored-DTC request on both ISO and enhanced KWP
     paths. Keep KWP `0x18` opt-in and experimental until a real response proves
     its count, status bytes, and code encoding.
   - Send the legacy Keihin single-byte `3F` keepalive through the profile
     instead of generic `3E 02`; define whether a response is expected and how
     a missing response affects session liveness.
   - Encode the enhanced clear-DTC request as the legacy Keihin all-groups form
     `14 FF FF FF`, not `14 FF 00`. Preserve the confirmation gate and do not
     label the enhanced clear path validated before step 8.
   - Update mocks and byte-exact tests for Mode `03`, experimental `0x18`, `3F`,
     and the four-byte clear request without weakening the standard ISO path.
   - **Done when:** safe read-only defaults match the known profile, destructive
     traffic remains explicitly gated, and every default request has an exact
     protocol test.

5. **P1 — Replace the stringly typed live-data branch with profile-driven decoders.**

   - Define a `LiveDecoder` contract per source that owns both request planning
     and response decoding, for example `request_ids(selection)` and
     `decode(raw, selection)`. Register decoders by source so adding another
     source does not add another branch to `DiagnosticService.read_live`.
   - Let a request plan describe individual `0x22` records and Mode-`01` PIDs,
     including identifier, expected width, formula, units, bounds, and display
     metadata. Support partial replies and preserve requested display order.
   - Treat `21 80` as an identification/probing request unless a real trace
     proves that it is a packed sensor response for this ECU. Remove the
     invented sequential 53-by-2-byte layout and move `frame_offset` out of the
     shared `PidDef` once no evidence-backed decoder needs it.
   - Ensure `SessionController` passes the same caller-supplied KWP table or
     decoder registry into each `DiagnosticService`, just as it passes the OBD
     PID database; do not recompile the bundled table on every TUI connection.
   - Replace the mock's synthetic packed frame with per-identifier behavior and
     test mixed request types, unsupported identifiers, short payloads,
     formula failures, ordering, and decoder injection through the TUI.
   - **Done when:** the service has no `live_source == ...` decode branch, no
     generic descriptor carries source-only layout state, and speculative
     offsets cannot appear as real sensor values.

6. **P1 — Capture and replay the non-destructive Bonneville protocol.**

   - Record a sanitized trace for ISO `0x33` and KWP slow `0xD5` initialization,
     `10 03`, the `0x27` seed/key exchange, raw identification records,
     keepalive, Mode `03`, representative Mode-`01`/`0x22` sensor requests, and
     the `21 80` probe. Record the lack of a KWP-fast response as an observation,
     not as a supported path.
   - Capture sensors at key-on/engine-off, cold idle, warm idle, and controlled
     throttle changes with simultaneous trusted reference values. Derive
     widths, signedness, scales, offsets, units, and per-cylinder MAP identifiers
     only from those paired observations.
   - Verify sustained polling, keepalive coexistence, unsupported-identifier
     handling, session timeout behavior, and the achievable K-line refresh
     rate. Do not promise the mock's cadence on hardware.
   - Convert captures into sanitized replay fixtures and byte-exact contract
     tests. Add newly observed DTC descriptions or record mappings only when
     their source and encoding are known.
   - **Done when:** the full non-destructive sequence can be replayed without
     hardware and decoded values agree with the reference measurements within
     documented tolerances.

7. **P1 — Implement profile-owned KWP SecurityAccess and capability gating.**

   - Put the `0x27` seed/key algorithm behind a profile-owned strategy rather
     than inside the generic KWP client. Support the observed access level,
     seed request, key response, response-pending flow, retry delay, denial,
     and lockout behavior.
   - Validate the implementation against captured seed/key vectors without
     storing secrets or unsanitized bike data in the repository.
   - Track authentication separately from transport connection and diagnostic
     session state. Refuse actuator, map, and programming operations unless the
     profile declares the capability and the required authentication completed.
   - Apply programming-only timing parameters only after the authenticated
     transition that needs them, and restore/close the session safely on error.
   - **Done when:** a replay proves successful and failed authentication paths,
     the UI/CLI cannot overstate session capability, and protected services are
     unreachable without the gate.

8. **P1 — Validate enhanced clear-DTC on a controlled target.**

   - Use a bench ECU or a bike with a known, recoverable stored fault only after
     the read-only sequence in step 6 is stable. Keep this out of routine
     protocol discovery.
   - Capture the confirmation, exact `14 FF FF FF` request, positive/negative
     response, follow-up Mode `03` read, and behavior after reconnect.
   - Test refusal, timeout, disconnect, and ambiguous-response paths so the UI
     never reports success unless a valid acknowledgement and verification read
     agree.
   - **Done when:** the sanitized clear cycle is a replay fixture and the
     enhanced path is labelled validated only for the tested profile.

9. **P2 — Add live-data controls and recording.**

   - Add a sensor picker driven by the active profile/decoder, with clear
     unsupported-state feedback and a useful profile-specific default set.
   - Add CSV recording with monotonic and wall-clock timestamps, sensor IDs,
     engineering values/units, and enough raw data/profile metadata to audit a
     decode later. Flush and close cleanly on stop, disconnect, and error.
   - Add adjustable poll intervals bounded by what the half-duplex K-line and
     selected sensor count can sustain; display measured refresh rate rather
     than an aspirational target.
   - Decide the remaining presentation split, then test it at narrow and wide
     terminal sizes. The current direction is to retain the dense table and
     sparklines in Live Data and reserve a small gauge cluster for Dashboard.
   - **Done when:** a user can choose sensors, record a verifiable session, and
     change cadence without overlapping ECU traffic or corrupting the output.

10. **P2 — Build the throttle-body synchronization view.**

    - Start only after step 6 identifies and calibrates the profile's
      per-cylinder intake-pressure records.
    - Add a purpose-built view showing each cylinder, spread, units, trend, and
      a profile-owned balance tolerance. Include freeze/resume behavior and
      clear guidance for engine temperature and idle preconditions.
    - Reuse the single serialized live session; entering/leaving the view must
      retask polling without racing DTC reads, keepalive, or another live view.
    - Test balance calculations, missing cylinders, outliers, stale data,
      reconnects, terminal layout, and moving mock values.
    - **Done when:** replayed/reference pressures produce the expected balance
      result and the view degrades safely when any required channel is absent.

11. **P3 — Add guarded actuator tests.**

    - Start only after steps 6 and 7 prove the enhanced path and SecurityAccess.
      Capture whether each actuator uses `0x2F`, `0x31`, or another service, and
      store model-specific routine IDs and limits in profile data such as
      `triumph_actuators.json`.
    - Implement a service boundary for fuel-pump, injector, and later actuator
      commands; extend the mock with observable actuator state and exact request
      tests before touching hardware.
    - Require engine-off and other profile preconditions, an explicit
      confirmation, hold-to-activate semantics, a strict maximum duration, and
      automatic stop on release, timeout, disconnect, exception, or app exit.
    - Prove stop behavior on a bench ECU before a controlled bike test, and
      never infer success from transmission alone.
    - **Done when:** every actuator has captured start/stop acknowledgements,
      enforced safety limits, failure-path tests, and profile-specific support
      labelling.

12. **P4 — Implement a verified map-backup workflow.**

    - Begin only after authenticated programming access is proven. Capture the
      ECU's actual read mechanism (`0x23` or `0x35` -> `0x36` -> `0x37`), memory
      ranges, block sizes, timing, and checksum rules in model-profile data.
    - Build a dedicated transfer module with bounded reads, progress, retry
      policy, cancellation, and checksum verification. Write to a temporary
      file and publish the final backup only after a complete verified transfer.
    - Add a `dump-map` CLI subcommand that produces a raw, community-compatible
      `.bin` plus a manifest containing profile, ECU identity, size, hashes,
      capture time, and tool version.
    - Compare repeated dumps byte-for-byte and against an independent trusted
      tool before treating the backup as usable.
    - **Done when:** replay tests cover interruption and corruption, and multiple
      controlled dumps are identical and independently verified.

13. **P5 — Make and, only if justified, execute a reflash go/no-go decision.**

    - Decide explicitly whether reflashing belongs in trecu or should remain
      delegated to mature community tools. A no-go decision should close this
      item with documented rationale; do not keep a permanently implied
      promise.
    - A go decision requires step 12 to be proven, a known-good backup before
      every write, checksum correction, stable external power, a documented and
      tested recovery path, and extensive bench testing on expendable hardware.
    - Only then implement erase plus `0x34` -> `0x36` -> `0x37`, interruption
      handling, read-back verification, strict profile/identity matching, and
      confirmations that make the bricking risk unmistakable.
    - Update the README's scope and safety claims before any release containing
      write support.
    - **Done when:** either the no-go decision is recorded, or an independently
      recoverable bench reflash passes full write/read-back verification and a
      separate safety review.
