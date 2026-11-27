---
name: debugging-failures
description: Debug software failures, exceptions, timeouts, slow responses, and crashes by reproducing a bug and tracing its root cause before changing code.
---

# Debugging failures

1. Record expected and observed behavior, environment, timing and the smallest safe reproduction. Capture the actual error or measurement; do not invent one.
2. Follow the failing request through callers, async boundaries, state, dependencies and outputs. Compare competing causes with targeted observations; distinguish a hypothesis from a confirmed cause.
3. Fix the cause in the responsible layer while preserving existing data and permissions. Avoid masking errors with silent fallbacks or fabricated results.
4. Re-run the same reproduction and a focused regression or boundary check. For performance, compare like-for-like settings and report variability. If reproduction or verification is unavailable, say what remains unconfirmed.
