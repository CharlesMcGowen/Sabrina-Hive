---
name: recover-workflows
description: Recover interrupted workflows, resume long-running jobs, diagnose retry failures, and safely reconcile checkpoints and partial results.
---

# Recover workflows

- Inspect the actual job state, durable checkpoints, timestamps, ownership and error before restarting. Distinguish queued, running, waiting for input, failed, and completed work.
- Separate recorded facts from assumptions. `pending` means completion is not recorded: the step may have started or left partial output. Check before resuming, and do not invent dependencies.
- Preserve existing or partial files; do not recommend deleting them merely because they are incomplete. Choose recovery only after inspecting the state and confirming any authority needed for destructive actions.
- Avoid duplicate payments, messages, and writes. A completion label or receipt is evidence, not proof of settlement: verify it before calling the payment settled. Missing email logs alone do not prove non-delivery; do not resend until the outcome is resolved or an idempotent retry is verified safe.
- Retry only a verified transient failure with a bounded attempt and time budget. Stop on authorization failures, conflicting state, unknown side effects, or repeated identical failures, and explain the next useful action.
- Verify the final state independently where possible. Record what was recovered, what was skipped, and any uncertainty.
