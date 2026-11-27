# Join-Cut regression cases

The pytest functions below live in `sabrina_console_desktop/test_join_cut_integration.py`. Select the example that shares the suspected failure mechanism. From the Sabrina repository root, run a case with the project's Python environment.

```powershell
python -m pytest -q sabrina_console_desktop/test_join_cut_integration.py::test_locked_settlement_retries_after_real_writer_releases
```

## SQLite and concurrency

| Scenario | Arrange and assert | Pytest function |
|---|---|---|
| Claim under a writer lock | Hold `BEGIN IMMEDIATE` on a second connection to the temporary database. Claim through the runner. Check the fail-open result and warning, the absence of a partial node, and a successful claim after unlocking. | `test_runner_fails_open_with_real_sqlite_writer_lock` |
| Settlement under a writer lock | Claim first, then take the lock and complete the task. Observe `LIVE` while locked, `JOINED` after unlocking, and a denied replay. | `test_locked_settlement_retries_after_real_writer_releases` |
| Lease time under contention | Signal when the waiting connection attempts its write. Hold the lock past the requested lease duration. After unlocking, check that both acquisition entry points grant the full lease. | `test_lease_clock_starts_after_acquiring_sqlite_writer` |
| Atomic startup and immutable tombstones | Make a real CHECK constraint reject root insertion. Reopen the database and compare its schema to the initial schema. For a tombstone, attempt a direct delete and resurrection on a real connection and check the specific trigger errors. | `test_bootstrap_rolls_back_all_ddl_when_root_seed_violates_a_constraint`, `test_tombstone_immutability_triggers` |
| Consistent reads | Commit a prune from a second connection between the verifier's node and dedup reads. Check that the commit happened and that the verifier saw a consistent snapshot. | `test_verify_invariants_reads_one_snapshot_across_a_committed_prune` |
| Duplicate submissions and stress coverage | Give every claimant a worker and synchronize their start. The 120-caller fixture expects one dispatch and 119 duplicate results. In randomized runs, require a successful CUT, JOIN and PRUNE before accepting clean final invariants. | `test_120_concurrent_identical_spawns_collapse_to_one_and_emit_dedup`, `test_randomized_concurrent_cut_join_prune_preserves_invariants` |

## Identity and lifecycle

| Scenario | Arrange and assert | Pytest function |
|---|---|---|
| Replay after a lost response | Repeat the successful spawn with its original request ID and parent version. Expect the same result and a single version increment. A different request using that stale version must fail. Replay a worker claim while its lease is held, then after release. Only the latter may dispatch, with a higher lease epoch. | `test_spawn_replay_accepts_the_original_parent_version`, `test_request_replay_requires_an_owned_lease_before_dispatch` |
| Reuse after release or upgrade | Save an old version and lease epoch, release the path, then bind new work at the same path. Send the old prune and settlement. Verify that the replacement survives and the old calls fail on both current and pre-upgrade database fixtures. | `test_reused_path_fences_stale_prune_and_settlement` |
| Allocation at capacity | Lower the branch capacity, retain a released task and submit more distinct claims. Complete those claims, reclaim the retained task at its original path and verify the tree collapses to the root. | `test_runner_overflow_preserves_running_and_released_leaves` |
| Root protection and unresolved waits | Attempt leaf completion on the root. Create a live dependency and attempt both leaf and parent completion. Check the exact refusals, then settle the dependency and retry the parent. Repeating an existing wait edge must preserve the waiter's version. | `test_leaf_completion_cannot_finalize_root`, `test_unresolved_wait_blocks_leaf_and_parent_completion`, `test_duplicate_wait_edge_preserves_waiter_version` |
| Cancellation, work stealing and recovery | Cancel a started provider call and check propagation, lease release and retry count. Give two tasks distinct outcomes on one worker. Check each task's stored state by its own key, including the recovery path. | `test_provider_cancellation_releases_the_started_workstream`, `test_stolen_work_settles_its_own_claim_and_final_result` |
| Dependencies and aliases | Schedule a prerequisite, its dependent and a task blocked before start. Verify which tasks ran, which joined and which released their claim. Then name a deduplicated alias as a prerequisite and check the result delivered to its consumer. | `test_dependency_scheduler_settles_completed_and_releases_unstarted_claims`, `test_collapsed_prerequisite_uses_the_canonical_workstream_result` |
| Semantic task identity | Reorder the same target files and expect a duplicate. Change a target or prerequisite and expect a distinct claim. For numeric keys, use known canonical bytes and pre-existing hashes, reopen the database and check replay against held leases and tombstones. | `test_runner_claim_distinguishes_normalized_targets_and_prerequisites`, `test_rfc8785_numbers_deduplicate_against_persisted_legacy_hashes` |
| Feature flags and mission isolation | Exercise explicit disablement and inspect the untouched registry. Enable the feature without an injected registry, submit work in two missions and check each mission's database for its own completion. | `test_runner_regression_disabled_bypasses_registry_completely`, `test_explicit_enable_opens_a_separate_sidecar_for_each_mission` |

## Numerical unit tests

Use an independent numerical check. Perturb the input frame and assert unit vector lengths and pairwise dot products near zero. Follow a deep path that changes direction to expose accumulated error. Give the public constructor a first-level radius below its precision floor and check that it rejects the input before creating a database. See `test_embedding_corrects_perturbed_parent_frame`, `test_multi_level_cuts_zero_overlap_and_frame_orthonormality` and `test_first_level_below_precision_floor_is_rejected_before_seeding`.
