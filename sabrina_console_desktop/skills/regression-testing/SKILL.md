---
name: regression-testing
description: Turn audit findings into focused pytest unit and integration tests. Use for defect reproduction, runner lifecycle regressions, SQLite locking and triggers, and before-and-after test comparisons.
---

# Regression testing

1. State the contract in observable terms. Name the starting state, the operation and the result a caller or a fresh database reader should see. Trace the actual entry point through its return, exception and retry paths. Check existing tests and SQL constraints. If a guard already covers the reported input, cite its exact location and show how it prevents the failure.
2. Choose the smallest test that exercises the cause. Call pure functions directly for geometry or normalization. For persistence or scheduling, drive the registry or runner with a database under `tmp_path`. A scripted provider can deliver cancellation or a transient error while SQLite still enforces its own transactions and triggers.
3. Force the failing sequence. Use distinct connections for lock contention and a barrier or event for ordering. Assert both the caller's result and the state another operation observes. For example, after settlement meets a writer lock, release the lock, wait with a deadline for `JOINED`, then replay the claim and check that dispatch is denied.
4. Run the same pytest case on the pre-fix and patched sources. Inspect the failing assertion, then verify the fix passes it. Map each confirmed fix to its focused case.

For runnable examples, use the [Join-Cut case guide](references/join-cut-cases.md).

## Interpreting the result

Use the same interpreter, dependencies, flags and equivalent fixtures on both runs. Save the node ID, including any parameter, and check that the baseline reached the intended failure. A collection error leaves the behavior untested. Record a source-only finding as unverified if execution is unavailable.

Run the affected callers after the focused case. When reporting a wider suite, compare failing test IDs, subtests and collection errors across the two runs. Equal totals can hide a new failure replacing an old one. For concurrent stress tests, count successful operations as well as checking final invariants.
