# Owner-authorized workflow reconciliation

Status: proposed implementation for #8519. Baseline: `ae53197923e58402bca5cecdcff5f95bb312ba89`.

## Decision

Add a browser-session-only `reconcile` action to the existing workflow extension.
It uses the existing owner-bound AgentRuns capability and does not introduce an
administrator bypass, model tool, stored credential, or independent execution
engine. The UI displays the exact workflow, step, attempt, child identity and
current status before accepting a decision and bounded evidence reference.

The request includes an idempotent decision ID, expected workflow revision,
step/attempt and one outcome: `completed`, `no_effect`, `compensated`, or `unknown`.
The authenticated owner is the actor. Completed outcomes require an observed
successful child and an attributable, bounded result collected through the same
checks as ordinary execution; caller-supplied output is never accepted. A safe
retry requires a terminal observed child (or a stored pre-admission state that
proves dispatch did not begin). Active or unidentifiable admission stays blocked.
The owner verifies absence/compensation of external effects; a terminal run alone
does not prove that business effects did not occur. Unknown outcomes retain their
reservation and do not authorize continuation.

The store serializes reconciliation against workflow mutation and lease claims.
In one transaction it compares the exact revision and attempt, appends the
decision/actor/time/evidence receipt, changes the step and workflow state, and
releases that step's retained PostgreSQL child slot. SQLite retains its existing
exclusive service lock and uses an immediate transaction; PostgreSQL locks the
workflow execution lease and rejects a still-live owner. No host IO takes place
inside the store transaction. Same-ID/same-decision retries return the saved
receipt; changed payloads, competing decisions and stale attempts fail closed.

Completion retains an immutable copy of the verified result. A retry archives
the previous attempt and allocates fresh child thread, input-message and
idempotency identities. Attempts count against the existing total-call bound.
Reconciliation never itself launches a child: after all uncertainty is resolved,
the existing explicit Recover action revalidates session/configuration/runtime
and claims an execution generation before replay. The bridge journal is rewound
to the resolved step's request; existing completed siblings are reused and input
checks reject changed downstream work. There is no blind replay of unknown work.
The elapsed operator wait does not consume a live worker's wall deadline; the
remaining execution allowance is preserved, not reset.

## Compatibility and limits

Existing workflow JSON records gain bounded reconciliation receipts and prior
attempts. No host schema changes are required. Add a read-only
`AgentRuns.validate_run_identity(thread_id, run_id, idempotency_key)` capability:
the host validates the current owner/read authority, plugin namespace, persisted
scoped admission key and server-owned extension origin, without dispatching,
canceling or requiring artifact capture. Existing assembly evidence and mutable
thread markers cannot prove that a caller-supplied terminal run is the lost keyed
admission. The additive API is versioned with a matching workflow dependency;
older hosts fail closed. Completion still requires the matching visible input
and valid result; safe retry requires terminal admission identity and explicit
external-effect attestation, not a successful output or empty interrupt state.
After any reconciliation, unattended grants cannot dispatch work until a fresh
session explicitly calls Recover. That action consumes the manual-recovery guard;
it does not silently renew or widen the original standing grant. Runtime
compatibility hashing includes the reconciliation implementation, so an old
worker cannot resume a record written under changed lifecycle semantics.
Deleting unresolved workflow records cannot discard outstanding uncertainty.
Human output-approval checkpoints remain a separate decision and are never
implicitly approved by reconciliation.
Cancellation remains binding. Reconciliation after cancellation cleanup may
record a completed child or verified no-effect/compensated outcome and release
its reservation, but cannot create a retry or resume dependent work. A pending
cancellation must finish its cleanup first.

A child whose admission receipt was lost must be identified and observed before
completion or retry. Without an observable child or durable pre-dispatch proof,
the safe action remains `unknown`; this API does not infer non-execution from a
timeout, a missing result or a user-supplied assertion alone.

## Verification

Extend the owning workflow recovery and PostgreSQL suites with real store
transactions, controlled admission/result failures, repeated and conflicting
decisions, stale attempts, revocation, invalid results, restart persistence and
capacity observations. Exercise the production JS worker and a real process
death after an external synthetic write, proving that completed reconciliation
does not repeat it. Browser checks cover the operator form, exact request binding,
safe error display and explicit recovery. Run extension packaging, lint/format,
blocking IO and the relevant host boundaries; report unsupported deployed checks
separately rather than treating a simulated AgentRuns handle as real auth proof.
