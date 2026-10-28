# Team view backend implementation plan

## Goal and acceptance

Implement the AC-65 project-scoped team organization metadata API. A user with the existing workflow edit capability can read configured role identities, locally stored organization edges, and a bounded projection of active tasks, then replace the edge set with optimistic concurrency and idempotent operation receipts. Restarting against the same Sim journal preserves the graph. Metadata must never create agents, mutate Sim workflows, affect authorization, expose task output/evidence/paths/secrets, or change task state.

## Scope

- Add `agent_line/team_view.py` for strict request validation, journal-backed revision/edge state, and safe active-task projection.
- Add team routes and optional task store injection to `agent_line/authoring_api.py`.
- Wire the existing `TaskStore` into authoring in `agent_line/service.py`.
- Add focused API and persistence coverage in `agent_line/tests/test_team_view.py`.
- Reuse `SimJournal.operations`; use a `team-<sha256(project)>` namespace and `team-view` context.

## Design and constraints

- All routes use existing `AuthoringAccess`, project scopes, and the authoring `scoped` helper. Writes recheck the actor within the journal's guarded `BEGIN IMMEDIATE` transaction.
- The latest accepted receipt defines the revision. Journal rows are validated as a contiguous immutable sequence; malformed, incomplete, or gapped history fails closed. Operation replay returns the original receipt, while GET continues to report current state.
- Edges are canonical sorted unique directed pairs with no self-links. New endpoints must identify configured role agent IDs for this project, including disabled bindings; unchanged dangling edges can remain until removed.
- The task projection is optional, bounded to 500 rows plus one truncation probe, and selects only allowlisted status and identity fields for this project. It reads the existing store without mutation.
- No new schema, dependency, network call, Sim write, role creation, workflow save, or permission/dispatch behavior.

## Verification

Run the focused `tests/test_team_view.py` suite and flake8 on touched Python files. Cover restart persistence, CAS contention, idempotent replay and conflict, scope/auth/revocation/maintenance checks, stale and deleted role bindings, task privacy/bounds/read-only behavior, and untouched workflow/task operations.

## Limitations

This delivers the backend contract only. It does not implement a team graph UI, prove configured roles are natively online, or change task scheduling/permissions. Active task visibility is a bounded snapshot and can change immediately after the read.
