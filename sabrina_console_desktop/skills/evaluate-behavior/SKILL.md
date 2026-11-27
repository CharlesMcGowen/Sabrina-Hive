---
name: evaluate-behavior
description: Evaluate agent behavior, tool calls, model outputs, benchmarks, and regressions with reproducible test cases and evidence-based comparisons.
---

# Agent evaluation

- Define the task, environment, expected observable outcome, permitted actions, and meaningful failure cases before running an evaluation. Use disposable fixtures for side effects.
- Hold model, inputs, tools, permissions, and scoring constant in a baseline-versus-candidate comparison unless the changed factor is the subject of the test. Record versions, seeds where supported, timings, and costs if relevant.
- Check both the resulting state and the action trajectory: correct output alone does not prove the agent used required evidence or respected boundaries. Include unavailable-tool, error, and cancellation cases when relevant.
- Report individual results, sample size, failures and limits; distinguish scripted fixtures from model-backed or live tests. Do not tune to a hidden answer or treat unrun checks as passes.

Reference: https://codelabs.developers.google.com/adk-eval/instructions
