---
name: tools-routing
description: Discover and use available tools and APIs for an authorized task. Use when an agent must choose tools, inspect tool access, execute actions, or diagnose tool errors.
---

# Tool selection and use

- Inspect the runtime's actual tool catalog, parameters, current policy, and relevant state. Prefer a purpose-built tool over a less direct interface when it can do the job.
- In Sabrina's native chat, use `get_console_info` with section `tools` when available. A registry skill is guidance, not a callable tool or a permission grant.
- Select the smallest useful calls, pass only required arguments, and use read-only inspection before a state change when needed. Follow the user's authorization and the tool's permission checks; a listed capability is not proof it is enabled.
- Check each result and distinguish completed actions, staged actions, simulations, unavailable services, and failures. Do not claim an action happened merely because it was requested.
- For errors, diagnose the observed response, use bounded retries only for transient failures, and report a concrete blocker instead of repeating a harmful or unsupported operation.

Reference: https://www.anthropic.com/research/writing-tools-for-agents
