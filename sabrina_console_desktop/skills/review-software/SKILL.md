---
name: review-software
description: Review code changes, diffs, pull requests, and APIs for actionable defects, regressions, and missing coverage. Use for review-only requests rather than implementation.
---

# Software review

- Establish the intended behavior and compare the changed code with its callers, data contracts, permissions, and existing tests. Follow a concrete input to its observable outcome.
- Prioritize defects introduced by the change: incorrect results, failures, security boundaries, compatibility, concurrency, data loss, and missing recovery. Check whether another guard already prevents the suspected issue.
- Report each finding with a precise location, trigger, consequence, and a practical correction. State uncertainty and test gaps. Separate confirmed issues from speculative risks.
- Do not modify the implementation when the request is review-only, and do not invent findings to fill a quota. If no actionable defect is found, say so.
