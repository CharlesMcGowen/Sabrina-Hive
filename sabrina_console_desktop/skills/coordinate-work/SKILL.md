---
name: coordinate-work
description: Coordinate a complex swarm or multi-step project by decomposing tasks, defining handoffs, tracking dependencies, and integrating independently verified results.
---

# Coordinate work

- Define the deliverable, constraints, acceptance evidence and shared state. Respect the user's requested team size; otherwise prefer a single worker for a small task and parallelize only when available, permitted, and genuinely independent.
- Give each worker a bounded objective, relevant context, allowed tools, output contract and ownership of files or state. Avoid overlapping writes and prevent a handoff from expanding authority.
- Track dependencies, decisions, blockers and time limits. Preserve evidence and the source of each claim; merge results only after checking they address the original goal and do not conflict.
- Report completed work and unresolved issues. Do not represent a delegated or proposed action as completed without an observed result.

Reference: https://docs.cloud.google.com/architecture/choose-design-pattern-agentic-ai-system
