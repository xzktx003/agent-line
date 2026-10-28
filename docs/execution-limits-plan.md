# Execution limits implementation plan

## Goal

Make the existing per-task `limits` payload enforceable at the durable task admission boundary, without adding a table, dependency, or changing the database schema. Keep the existing legacy behavior for records without `limits`.

## Behavior

- Validate a present `limits` object as exactly two strict integer fields: `maxExecutionSeconds` (1–86400) and `maxBusinessAttempts` (1–100). Reject booleans, malformed types, extra/missing keys, and out-of-range values.
- A limited task must have complete `projectId` and workflow `id`, `revision`, `runId`, and `nodeId`. Group attempts by project, workflow ID, run ID, and node ID; revision, role, node execution ID, and attempt ID do not create a new quota group.
- The first limited registration freezes both limit values. Reject a later registration in that group with different limits. Reject adding either a limited task to a legacy group or a legacy task to a limited group; existing legacy groups remain readable and retain their existing behavior.
- During `claim`, under the existing `BEGIN IMMEDIATE`, count every persisted `submission_intent` across the group. Once the frozen count is exhausted, leave the task registered and expose `business_attempt_limit`; do not reserve or call the adapter. A committed intent is never refunded, including unknown outcomes, failure, or cancellation. Cancellation before intent consumes nothing.
- Derive one attempt's deadline from its unique `submission_intent` timestamp plus `maxExecutionSeconds`. Missing intent on a running limited task, duplicate/invalid/future timestamps, or invalid limits fail closed with `ValueError`. Terminal tasks retain their derived deadline but are never reported as deadline-exceeded. This records the boundary; it does not claim cancellation stopped the upstream task or release its lease.
- Treat invalid group history as `state_unknown` for queue/claim checks. A clearly exhausted, unstarted, unblocked registered attempt does not hold the role/workspace FIFO position for viable work on another node.
- Expose a read-only per-task projection. Actual token, model-call, and monetary usage remain `None` because this layer has no trusted usage source.

## Verification

Add focused regression tests for validation, grouped/frozen attempts across revision and role changes, concurrency, legacy compatibility, cancellation before claim, permanent intent consumption, per-node independence, and deadline edge cases. Run the new test module plus the existing task-store and execution-queue tests; do not run the full suite for this bounded change.

## Scope limits

This implements business-attempt count and elapsed-time limits at local task admission. It does not implement token limits, provider/model call counts, actual usage settlement, cost estimates, or full AC66. Those require trusted adapter usage evidence and service/UI integration.
