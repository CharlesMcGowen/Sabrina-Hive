# Sabrina Swarm Bridge for Antigravity

This plugin connects Google Antigravity to the Sabrina Swarm and Sabrina Desktop Console runtime.

## What's Included
- **MCP Server (`sabrina-swarm`)**: Exposes tools for:
  - `sabrina_get_swarm_state`: Live worker states, locks, and mission topology.
  - `sabrina_read_blackboard`: Read recent board messages and agent-to-agent exchanges.
  - `sabrina_post_board_message`: Broadcast status reports or @mentions.
  - `sabrina_update_worker`: Update agent state, heartbeat, and acquire/release locks.
  - `sabrina_get_a2a_links`: Inspect agent-to-agent routing rules.
  - `sabrina_run_swarm_turn`: Dispatch decomposed goals to the Swarm Command router.
  - `sabrina_check_desktop_health`: Check control plane (:8087) health and schedules.
- **Skill (`sabrina-swarm`)**: Procedural instructions for swarm cooperation.
- **Agent (`sabrina-liaison`)**: Dedicated subagent to interface with the Sabrina swarm.
- **Rules (`AGENTS.md`)**: Enforces cooperative file locking and concise blackboard posting.

## Requirements
- Sabrina Desktop Console running locally (`http://127.0.0.1:8095` for swarm API, `http://127.0.0.1:8087` for control plane).
