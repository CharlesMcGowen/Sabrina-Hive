# Sabrina Swarm Bridge Rules

When the Sabrina Swarm Bridge plugin is active:

1. **Verify Connectivity**: Before attempting blackboard posts or worker updates, verify the swarm service is available using `sabrina_get_swarm_state`. If unreachable, inform the user clearly that the Sabrina Desktop Console (:8095 or :8087) is not currently running.
2. **Respect Path Locks**: Always inspect `sabrina_get_swarm_state` for lock conflicts before modifying files in the Sabrina repository. Do not overwrite files locked by other swarm members.
3. **Concise Blackboard Broadcasts**: When posting to `sabrina_post_board_message`, keep messages structured, informative, and include relevant @mentions (@coordinator, @verifier, @architect).
4. **Clean Handoffs**: Always clear worker locks and set status to IDLE when concluding an implementation turn.
