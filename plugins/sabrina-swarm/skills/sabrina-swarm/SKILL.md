---
name: sabrina-swarm
description: Coordinate with Sabrina Swarm and Sabrina Console Desktop. Query active workers, read and post to the swarm blackboard, check console health, and dispatch swarm turns.
---

# Sabrina Swarm Integration Skill

This skill guides Antigravity agents when interacting with the Sabrina Swarm and Sabrina Desktop Console.

## Prerequisites & Architecture

Sabrina runs local services on the workstation:
- **Sabrina Swarm API (`http://127.0.0.1:8095`)**: Manages the multi-agent swarm state, workers, locks, blackboard messages, A2A communication links, and Swarm Command turns.
- **Sabrina Control API (`http://127.0.0.1:8087`)**: Manages scheduler jobs, background automations, and subsystem health.

Tools are provided via the `sabrina-swarm` MCP server:
- `sabrina_get_swarm_state`: Snapshot of active workers, lock table, and mission topology.
- `sabrina_read_blackboard`: Read recent blackboard messages, @mentions, and posted task contracts.
- `sabrina_post_board_message`: Broadcast status updates, execution results, or directives to other swarm members.
- `sabrina_update_worker`: Announce Antigravity's role, heartbeat status, and claimed file locks.
- `sabrina_get_a2a_links`: Inspect current agent-to-agent communication pipelines.
- `sabrina_run_swarm_turn`: Dispatch multi-subtask goals to the Swarm Command router.
- `sabrina_check_desktop_health`: Verify Sabrina Desktop Console health and scheduled jobs.

## Workflow Patterns

### 1. Preflight Verification
Before issuing tasks, check if the Sabrina Swarm or Desktop Console services are listening:
```
Call `sabrina_check_desktop_health` or `sabrina_get_swarm_state`.
```
If the tool returns a `connection_error`, notify the operator that Sabrina Console Desktop needs to be running (e.g. via `run_console.bat` in the Sabrina workspace).

### 2. Reading and Posting to the Blackboard
The shared blackboard is the primary communication medium across Sabrina agents (`coordinator`, `architect`, `worker`, `verifier`, and `antigravity`):
- To read recent updates: call `sabrina_read_blackboard(since=0, limit=25)`.
- When Antigravity completes a task or reports a finding:
  ```json
  sabrina_post_board_message(
    agent="antigravity",
    text="@coordinator Implementation review completed. All unit tests passed.",
    tags=["status", "completion"]
  )
  ```

### 3. Claiming Locks Before Editing Shared Files
Sabrina enforces cooperative path locking across swarm workers:
- Call `sabrina_update_worker(name="antigravity", state="WORKING", role="execution_specialist", locks=["path/to/file.py"], note="Refactoring signal pipeline")`.
- When finished, release the locks:
  ```json
  sabrina_update_worker(name="antigravity", state="IDLE", locks=[], note="Work complete")
  ```

### 4. Running Swarm Command Turns
When the user requests delegating a complex task across the entire swarm:
- Call `sabrina_run_swarm_turn(request="Analyze and verify signal promotions", subtasks=["Review recent trade logs", "Run unit assertions"], complexity_hint="moderate")`.
- Review the returned verdict, blackboard snapshot, and artifacts.
