# Tick runtime owns cycle sequencing; adapters own HA-specific work

The Tick runtime's driver (`custom_components/intentional/tick_driver.py`) owns
cycle ordering, mutation-lock phases, revision fences, pulse acknowledgment,
persistence gating, stage cadence, and liveness. Integration setup supplies the
existing collaborators and four HA-specific callbacks, then starts `async_run`;
`async_step` exercises the same cycle without timers or a Home Assistant boot.

## Trade-off

Moving the loop into another setup helper would leave ordering knowledge in its
caller. A generic phase pipeline would expose that ordering through a larger
interface and allow invalid combinations. The concrete driver instead fixes
the sequence and accepts existing collaborators; tests use real Engine,
Reconciliation and LifecycleWriter implementations with an in-memory adapter
and store. Its callbacks handle input preparation, activity diagnostics,
Reconciliation-event application, and scene/Effect dispatch, not cycle policy.

## Consequences

- State ingest and Rule evaluation stay outside Reconciliation (ADR-0001).
  Alerting remains a sibling module with its own delivery/deadline driver
  (ADR-0005).
- Every attempted cycle restores the idle signal on return, exception, or
  cancellation. Pulses are acknowledged only by a current completed cycle;
  newer pulses arriving during dispatch survive for the next cycle.
- Evaluation-failure rollback is scheduled after releasing the mutation lock.
  The driver never awaits a Rule reload while holding that lock.
- Shutdown attempts one final forced lifecycle checkpoint. Snapshot format,
  coalescing, and retries remain owned by the existing lifecycle modules.
- `Engine.load_rules` returns unchanged Rule IDs from its transactional
  compilation. Reload coordination uses that result rather than independently
  hashing definitions and repeating the Engine's identity decision.
