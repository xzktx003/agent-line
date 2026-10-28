# Run execution budget implementation plan

## Scope and contract

Add optional `runBudget` support without changing TaskStore schema 2 or adding dependencies, tables, or services. A configured value must be an exact object with strict integer `maxSubmissions` (1–10000) and `maxExecutionSeconds` (1–604800). A run is scoped by `projectId`, `workflow.id`, and `workflow.runId`; revision, node, role, and attempt do not create separate accounts. `None` means no configuration at the validator boundary, while a payload containing `runBudget: null` is invalid.

## Implementation steps

1. Add `agent_line/run_budget.py` with strict validation, scope extraction, and history projection helpers that reuse `execution_limits` event-order and unstarted-history validators.
2. In `TaskStore.register`, freeze the first run declaration, including absence, under `BEGIN IMMEDIATE`; enforce that all rows for the scope carry the same declaration. Existing idempotent registrations return before reinterpreting payload.
3. Add read-only `run_budget(task_id)` and `bind_run_budget(payload, configured)` methods. The latter copies the payload and inherits the first persisted declaration, including an absent declaration, without writing data.
4. Apply run admission and run deadline checks within `claim`'s existing immediate transaction. Use `run_submission_limit`, `run_execution_deadline`, and `state_unknown`; ensure FIFO skips only clearly exhausted or expired registered rows without leases, while retaining role-revision and unknown-history blocking behavior.
5. Extend `execution_deadline_exceeded` to observe the task deadline OR the run deadline for nonterminal tasks; the budget projection retains the actual run deadline, and cancellation remains owned by the existing dispatcher flow.
6. At the workflow boundary, freeze the project budget in the existing run ledger snapshot. New runs inherit the project declaration; resumed runs use the ledger's original value or absence. Discovery should pass the run snapshot through `bind_run_budget`, which also protects direct TaskStore registration against races and project edits.

## Static review checklist

- Preserve schema version 2 and existing task behavior when no run budget is configured.
- Count each valid durable `submission_intent` once, including failed, uncertain, and canceled-after-intent submissions; do not count cancellation proven to precede submission.
- Validate event order and positive timestamps, treat inconsistent histories as unknown, and avoid claiming provider token/model-call/cost measurements.
- Confirm service and task discovery can use `validate_run_budget(value)` and `bind_run_budget(payload, configured)`.
- Deadline observation can expose elapsed run time, but terminal task state must not request cancellation.
- Do not run tests, services, browser checks, probes, or lint per the current user instruction; perform source and AST inspection only.
