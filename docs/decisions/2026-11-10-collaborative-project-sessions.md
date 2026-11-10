# Collaborative project sessions: ownership and admission boundary

Status: **Proposed; implementation boundary awaiting discussion in #8010**.

Baseline: `c612531213b2505857bf733a6a332fd7653f84b4` (2026-11-10).

Tracking: [#8010](https://github.com/bytedance/deer-flow/issues/8010),
[scope/ownership request](https://github.com/bytedance/deer-flow/issues/8010#issuecomment-6756490947),
[workspace RFC #7652](https://github.com/bytedance/deer-flow/issues/7652).

## Context verified on main

- `ProjectRow.user_id` owns a project. `ProjectRepository` filters by the
  current user; a different user's project returns no row.
- Thread metadata, run records, event queries and artifact paths use user
  identity as an ownership scope. Adding a thread ACL alone does not make
  these other consumers safe for another authenticated member.
- The application `User` model has no workspace membership. Channel
  connections have a `workspace_id`, but that identifies an external channel
  workspace and is not an application authorization grant.
- `RunManager.create_or_reject` supports `reject`, `interrupt` and `rollback`.
  `enqueue` is explicitly unsupported. Active-run interactive inputs do not
  provide durable admission for independent new turns.
- #7652 discusses a trusted workspace execution contract. It is a dependency
  proposal, not an available main-branch implementation. Its existing
  ownership request must be coordinated before duplicating foundational work.

These are baseline behavior and design dependencies, not a newly demonstrated
authorization vulnerability. No collaboration capability is enabled by this ADR.

## Proposed invariants

1. Workspace identity comes from a verified server-side membership decision.
   A requested workspace, metadata field, channel name or deployment identifier
   cannot create membership or resource permission.
2. Storage ownership and execution identity remain distinct. The conversation
   retains a stable owner/scope; a new run executes as its authenticated
   submitter. Continuing Bob's new turn must never run as Alice merely to
   reach Alice's thread files. Resuming an existing run retains that run's
   original execution principal and requires a separately authorized caller.
3. A grant names one project conversation and a member. Read and continue
   permissions are separate; a grant never implicitly shares the owner's
   other conversations, project documents, agents, connections or credentials.
4. The runtime resolves model permissions, tools and credentials using the
   execution principal. Shared conversation context excludes personal memory
   on both reads and writes. Resource authorization cannot be bypassed by a
   cached run configuration or by switching the ambient user ContextVar.
5. User-message attribution is server-authenticated and persisted. Assistant
   messages remain assistant-authored and refer to their initiating run.
6. Every history, checkpoint, event replay and artifact path intersects
   workspace access, object grant and route/credential permissions. PAT scopes
   remain an additional restriction. Administrative role alone must not be
   introduced as a blanket cross-user storage bypass.
7. Revocation is checked at new request admission, before a queued turn starts,
   on resume, at credential resolution and on subsequent result access. Define
   the stream revalidation boundary explicitly. Revocation cannot undo an
   external tool effect already issued.

Sharing an existing private conversation also needs an explicit transition
policy. The owner intentionally grants its selected history, but old hidden
checkpoint channels, cached agent/MCP state and owner-scoped runtime objects
must not be inherited merely because they exist. Specify which state can be
reused or must be rebuilt before the first member turn. Disabling future
personal-memory reads cannot remove information already present in history;
the UI must make the history being shared clear, and tests must distinguish
explicitly shared history from new private-memory retrieval.

## Decision needed before API/schema implementation

**Preferred:** build on #7652's agreed trusted workspace and durable execution
identity contracts. Coordinate the context/persistence work rather than adding
another parallel workspace model in #8010.

**Alternative requiring an explicit scope change:** an opt-in project-member
handoff without application workspace semantics. This must be described as a
project-only permission boundary, specify its trust source and migration path,
and must not be advertised as satisfying #8010's same-workspace requirement.
It is not an implicit fallback if workspace resolution fails.

The discussion must settle the dependency, owner of each foundational slice,
and the smallest supported deployment profile before public API names, tables
or resource access behavior are fixed. Proposed feature flags must default off
and cannot compensate for missing authorization enforcement.

## Durable new-turn admission

The queue accepts independent new turns; it does not reuse the active run's
interactive-input queue. Queue entries need a durable order, a deduplication
identity, the authenticated initiator, thread incarnation, verified workspace
binding, requested configuration and a versioned input envelope. Exact storage
fields remain subject to the foundational contract.

- Accept only after initial authorization. A repeated idempotency key cannot
  change the submitter or input and cannot admit the turn twice.
- Start one entry at a time after the previous turn settles. Resolve the new
  turn against the latest settled checkpoint, not a checkpoint captured while
  the previous turn was still running.
- Revalidate membership, thread incarnation, permission and resource use
  before execution. A revoked entry is denied without invoking a model/tool.
- A cancelled queued entry never starts. Run cancellation and queue-entry
  cancellation remain distinct authorized operations.
- Define whether a failed turn blocks later entries or permits continuation
  before implementing a dispatcher; failure must not silently change order.
- Claim/start/outcome transitions need durable fencing. After a crash at an
  uncertain tool-effect boundary, do not automatically replay the prompt as
  though the previous attempt had never executed.
- Initial single-Gateway support, if chosen, must still survive restart;
  PostgreSQL/multi-Gateway support needs a separately verified claim policy.

## Smallest vertical deliverable after agreement

An explicit grant/revoke flow and a shared-conversation UI for two synthetic
members, covering a new turn by each member, persistent attribution, history,
SSE replay and an authorized artifact. All enabled resource paths must use the
same trusted scope before the flag may be enabled. The first PR must list
unsupported entry points explicitly; schedulers, channels, subagent delegation
and sensitive approval/resume cannot silently inherit shared access.

Use core changes for mandatory admission/execution enforcement. Evaluate the
existing authorization/extension contributions for membership management and
UI before adding new core management APIs. Do not put an app-owned policy
dependency inside the publishable harness.

## Acceptance matrix

| Case | Required observation |
| --- | --- |
| Owner grants read to B | B can read only the granted conversation and permitted artifacts |
| Read-only B submits | Denied; no run, checkpoint mutation or model invocation |
| Owner grants continue to B | B's new turn uses B's model/tool authorization and credentials |
| A and B submit concurrently | Durable order, one active execution, B sees the settled preceding turn |
| B loses membership while queued | Entry denied before execution; no external invocation |
| B's queued turn is cancelled | Entry never starts, later behavior follows the documented queue policy |
| Member accesses another project/thread/workspace | Denied without disclosing resource existence |
| Client forges initiator/owner/workspace metadata | Trusted identity and storage ownership unchanged |
| Personal-memory canaries for A and B | Neither enters shared model context or shared memory writes |
| Different per-user credentials | Controlled tool endpoint observes the correct initiating identity |
| Restart before queue claim | One ordered admission restored from durable records |
| Crash after tool effect but before recorded outcome | No blind duplicate execution; explicit recovery outcome |
| Grant revoked after a run completes | New history/replay/artifact requests denied at documented boundaries |
| B requests resume of A's paused run | Caller permission checked; execution principal remains A |
| Conversation deleted/recreated at the same ID | Old grants/queue entries cannot attach to the new incarnation |
| Feature disabled and existing data | Existing owner isolation, PAT scopes and API responses preserved |

Use real authentication and SQLite/PostgreSQL persistence for ownership claims,
controlled local tool endpoints for credential claims and deterministic model
fixtures for context/ordering claims. Stub-auth router tests alone do not prove
the end-to-end profile. Full frontend lint/type checks, build, backend offline
and blocking-I/O suites remain required before a functional implementation is
represented as complete. Independent review targets a stable commit SHA.

## Current validation status

This document records a proposal and source inspection. Baseline probes and
existing regression results belong in bounded local evidence with the tested
SHA. No completed collaborative workflow, full test-suite pass, maintainer
acceptance, assignment or merge is claimed.
