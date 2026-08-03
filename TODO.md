# trecu TODO

This is the single backlog for the project. It contains only unfinished work,
ordered by priority, dependency, and safety risk. A protocol or model-specific
feature is not considered supported until a sanitized hardware capture has been
turned into a replay fixture and a byte-exact test.

TrECU's target is now deliberately narrow: reliable, read-oriented diagnostics
for the tested 2009 Triumph Bonneville 865 Keihin ECU over its confirmed
ISO 9141-2 / OBD-II endpoint. The KWP2000/Keihin path has been removed outright
(it was never validated on a bike). Enhanced manufacturer diagnostics, ABS,
actuator control, map access, and reflashing are outside the project scope.

Priority key: **P0** correctness/release safety, **P1** validated read-only
capability, **P2** user-facing diagnostics.

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

2. **P1 — Capture and replay the complete non-destructive ISO path.**

   - Record sanitized raw traces for slow init at `0x33`, including a clean
     `55 08 08` / inverted-address handshake and representative garbled or
     incomplete attempts observed on macOS.
   - Capture supported-PID discovery, keepalive, Mode `01` PID `01`, Mode `03`,
     Mode `07`, Mode `09`, and every advertised live PID the bike answers.
   - Capture live sensors at key-on/engine-off, cold idle, warm idle, controlled
     throttle changes, and engine shutdown with simultaneous trusted reference
     values. Record raw `00`/`FF` sentinel-looking values rather than assigning
     semantics without evidence.
   - Measure response latency, sustainable K-line refresh rate, session timeout,
     keepalive interaction, and recovery after disconnect. Do not promise the
     mock's cadence on hardware.
   - Convert captures into sanitized replay fixtures and byte-exact contract
     tests. Strip VIN and other identifying data without changing framing,
     lengths, checksums, or sequencing behavior.
   - **Done when:** the full ISO read-only sequence replays without hardware and
     decoded values agree with trusted references within documented tolerances.

3. **P1 — Make live polling paced and auditable.**

   The poll plan is already capability-aware: the ECU's advertised PID set is
   read at connect and intersected with the caller's selection and the locally
   decodable PIDs, so nothing unsupported reaches the wire. What is missing is
   pacing and per-reading provenance.

   - Replace the single all-sensors cadence with a *tiered* poll plan over that
     same intersection: RPM, TPS, and MAP at the fastest sustainable tier;
     oxygen/fuel-trim data at a medium tier; temperatures and status at a slow
     tier.
   - Derive cadence from measured round-trip latency and the selected sensor
     count. Display achieved sample age/rate rather than an aspirational timer
     interval, and never overlap requests on the half-duplex K-line.
   - Validate every formula and width in `obd_sensors.json` against captured
     bike data. Add structured decoders for bitfields/enums such as monitor
     status, fuel-system status, and OBD-standard identity instead of forcing
     them through numeric formulas.
   - Carry timestamp, raw bytes, decoded value/unit, response latency, and a
     quality/state marker with each reading. Distinguish valid zero, saturated
     raw data, stale data, unsupported, decode failure, timeout, and lost
     session.
   - Support partial snapshots without making one optional PID timeout look like
     a dead connection; confirm liveness with a known reliable request before
     reconnecting.
   - **Done when:** live data stays responsive at the selected sensor count,
     every displayed value is traceable to raw bytes, and absence/failure states
     are explicit.

4. **P1 — Improve serial discovery and ISO session recovery.**

   - Deduplicate macOS device aliases that share the same VID, PID, FTDI serial
     number, USB location, and interface, while preserving genuinely separate
     multi-interface devices. Prefer and remember one stable usable alias.
   - Log slow-init attempt number, address, timing outcome, sync/key bytes, and
     inverted-address validation without making normal output noisy.
   - Classify a missing versus a mismatched echo separately from a silent ECU
     response. The echo is already read back and compared exactly, with
     non-echo bytes handed on as inbound traffic instead of discarded; what is
     left is surfacing those two failures distinctly rather than as one warning.
   - Define session-loss criteria using a reliable liveness request, then add a
     bounded reconnect policy that rebuilds the transport, repeats PID
     discovery, and resumes polling without racing the old session.
   - Test alias deduplication, port disappearance, busy/permission failures,
     garbled first init, retry exhaustion, mid-poll disconnect, reconnect, and
     cancellation entirely against mocks.
   - **Done when:** one physical FTDI cable is presented once, transient init
     failures recover, and a lost session cannot be mistaken for an unsupported
     sensor.

5. **P2 — Add live-data selection and verifiable recording.**

   - Add a sensor picker driven by the session's advertised PID set, showing
     which PIDs TrECU can decode and which are available as raw data only.
   - Add CSV recording with monotonic and wall-clock timestamps, PID, raw bytes,
     decoded value/unit, response latency, and connection/quality state.
   - Add JSONL protocol recording that preserves request and response frames,
     session metadata, and tool version for later replay and decoder audits.
     Sanitize identifying Mode `09` data before fixtures are committed.
   - Flush and close recordings cleanly on stop, disconnect, error, and app
     exit. Use a temporary output and publish the final file only after a clean
     close.
   - Add adjustable polling tiers bounded by measured K-line capacity, plus a
     compact raw-data/debug view for investigating unfamiliar advertised PIDs.
   - **Done when:** a user can select discovered sensors, record a session whose
     decoded values remain auditable from raw frames, and recover a valid file
     after routine disconnects.

6. **P2 — Validate ISO DTC reads, identification, and clearing on hardware.**

   - Preserve Mode `01` PID `01` as the authority for MIL state and stored-DTC
     count, with Mode `03` retries reconciled against it and Mode `07` pending
     codes best-effort. Add replay coverage for inconsistent counts, silence,
     duplicate codes, and recovery after a transient timeout.
   - Validate the bounded multi-frame Mode `09` parser against *captured* bike
     responses before claiming identification is supported. Its strictness is
     already in place — a field is complete or reported unavailable, never
     plausible partial ASCII — but no real ECU has answered it yet.
   - Validate ISO Mode `04` clear-DTC separately on a controlled bike or bench
     target with a known recoverable fault. Preserve the confirmation guard and
     require a valid acknowledgement followed by reconnect/status verification
     before reporting success.
   - Keep clear-DTC out of connection discovery and automated hardware probes.
     Test refusal, timeout, disconnect, ambiguous acknowledgement, and failed
     verification paths with replay fixtures first.
   - **Done when:** reads cannot report a false clean state, identification is
     complete or explicitly unavailable, and clearing reports success only
     after verified ECU state agrees.
