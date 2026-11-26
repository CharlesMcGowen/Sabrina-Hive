"""build_thinking_model.py -- Sabrina's end-to-end "train a reasoning model" capability.

This is the tool Sabrina invokes to DEMONSTRATE that her system can take a stock instruct model and
turn it into a domain reasoning ("thinking") model -- and prove the gain with a held-out before/after
report card a non-ML stakeholder can read. It composes three pieces already proven on the GPU pod:

  1. SFT cold-start (rl/train_sft.py)     -- teaches the <think>...</think> + <action> format from
     verified traces. The "learn to reason out loud, then commit to one action" step.
  2. GRPO reinforcement (rl/train_grpo.py) -- a VERIFIABLE reward (did it name the right weakness
     class / did the action match) with a never-worse eval gate that only keeps a checkpoint when
     held-out accuracy actually rises. The "get the reasoning RIGHT" step.
  3. Report card (rl/report_card.py)      -- scores base vs each checkpoint on the SAME held-out
     tasks and reports two numbers: % well-formed reasoning, and task accuracy.

The 7B here is a DEMO -- the point is that the RECIPE works, so the same steps scale to a larger
model. The heavy steps run on the pod (this file emits the exact commands and, given a finished
report_card.json, renders the stakeholder card). It intentionally does not train locally: a Windows
dev box has no business loading a 7B, and the pod already holds the data + scripts.

CLI:
    python tools/build_thinking_model.py plan            # print the full recipe + pod commands
    python tools/build_thinking_model.py render <json>   # render a finished report_card.json
    python tools/build_thinking_model.py stages          # emit the pipeline as JSON (for the queue)
"""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Where the training scripts + data live on the pod's persistent volume.
POD_RL = "/workspace/rl"
DEFAULT_BASE = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_SFT_DATA = f"{POD_RL}/sqlmap_sft.jsonl"           # verified <think>+<action> reasoning traces
DEFAULT_CLS_DATA = f"{POD_RL}/bugbounty_vuln_tasks.jsonl"  # held-out public-report classification set


def pipeline_stages(base=DEFAULT_BASE, sft_data=DEFAULT_SFT_DATA, cls_data=DEFAULT_CLS_DATA,
                    sft_epochs=6, grpo_steps=600, gpu=0):
    """The three ordered stages, each with the exact pod command. A scheduler (train_orchestrator.py
    / gpu_job_queue.py) can consume this; a human can read it. Adapters chain: SFT writes an adapter,
    the report card scores base vs SFT vs GRPO on the same held-out tasks."""
    tag = base.split("/")[-1]
    sft_best = f"{POD_RL}/sft_best/{tag}"
    grpo_best = f"{POD_RL}/best/{tag}"
    return [
        {
            "stage": "sft_coldstart",
            "why": "teach structured <think>...</think> + one <action> from verified traces",
            "cmd": (f"cd {POD_RL} && CUDA_VISIBLE_DEVICES={gpu} python -u train_sft.py "
                    f"--model {base} --data {sft_data} --epochs {sft_epochs} --eval-holdout 16"),
            "produces": sft_best,
        },
        {
            "stage": "grpo_reinforce",
            "why": "verifiable reward + never-worse gate: keep a checkpoint only if held-out acc rises",
            "cmd": (f"cd {POD_RL} && CUDA_VISIBLE_DEVICES={gpu} python -u train_grpo.py "
                    f"--model {base} --max-steps {grpo_steps} --eval-steps 50 --num-generations 6"),
            "produces": grpo_best,
        },
        {
            "stage": "report_card",
            "why": "before/after proof on held-out tasks: reasoning-structure rate + task accuracy",
            "cmd": (f"cd {POD_RL} && CUDA_VISIBLE_DEVICES={gpu} python -u report_card.py "
                    f"--base {base} --sft {sft_best} --grpo {grpo_best} "
                    f"--sft-data {sft_data} --cls-data {cls_data} "
                    f"--gpu 0 --out {POD_RL}/report_card.json"),
            "produces": f"{POD_RL}/report_card.json",
        },
    ]


def render_plan(stages):
    lines = ["# Sabrina: build a thinking model (demo pipeline)", ""]
    lines.append("Stock instruct model -> reasoning model, with a held-out proof at the end.")
    lines.append("Heavy steps run on the GPU pod; each stage's adapter feeds the next.\n")
    for i, s in enumerate(stages, 1):
        lines.append(f"## Stage {i}: {s['stage']}")
        lines.append(f"_{s['why']}_")
        lines.append("```bash")
        lines.append(s["cmd"])
        lines.append("```")
        lines.append(f"produces: `{s['produces']}`\n")
    return "\n".join(lines)


def _pct(x):
    return f"{x * 100:.1f}%"


def render_report_card(path):
    """Turn a finished report_card.json into a stakeholder-readable before/after table. Two columns:
    well-formed reasoning (does it think in structure) and task accuracy (is the answer right)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    variants = data.get("variants", {})
    if not variants:
        return "report_card.json has no variants yet -- did the eval finish?"

    rows = []
    for name, v in variants.items():
        struct = v.get("structure", {})
        acc = v.get("accuracy", {})
        rows.append((name, struct.get("wellformed_rate", 0.0), struct.get("think_rate", 0.0),
                     acc.get("accuracy", 0.0), struct.get("n", 0), acc.get("n", 0)))

    w = max(len(r[0]) for r in rows) + 2
    out = [f"# Report card: {data.get('base', '?')}", ""]
    out.append(f"{'variant'.ljust(w)}  reasoning(wellformed)   thinks   task-accuracy")
    out.append("-" * (w + 48))
    base_acc = rows[0][3] if rows else 0.0
    for name, wf, th, acc, ns, nc in rows:
        delta = ""
        if name != rows[0][0]:
            d = acc - base_acc
            delta = f"  ({'+' if d >= 0 else ''}{d * 100:.1f} pts vs base)"
        out.append(f"{name.ljust(w)}  {_pct(wf).rjust(10)}          {_pct(th).rjust(6)}   "
                   f"{_pct(acc).rjust(6)}{delta}")
    out.append("")
    out.append(f"reasoning sample n={rows[0][4]} tasks; accuracy n={rows[0][5]} held-out reports")
    out.append("")
    out.append("Reading it: 'wellformed' = emitted a <think>...</think> block AND committed to one")
    out.append("<action>. 'task-accuracy' = named the correct weakness class on unseen public reports")
    out.append("(the exact reward the RL stage optimizes). A stock model rarely does the first and")
    out.append("guesses the second; the trained checkpoints should move both up.")

    # Show one reasoning sample from the strongest structured variant as concrete evidence.
    best = max(variants.items(), key=lambda kv: kv[1].get("structure", {}).get("wellformed_rate", 0))
    ex = (best[1].get("structure", {}).get("examples") or [])
    if ex:
        out.append(f"\n--- sample reasoning from '{best[0]}' ---")
        out.append(ex[0].get("sample", "")[:600])
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="print the full recipe + pod commands")
    p_plan.add_argument("--base", default=DEFAULT_BASE)
    p_plan.add_argument("--grpo-steps", type=int, default=600)
    sub.add_parser("stages", help="emit the pipeline as JSON")
    p_render = sub.add_parser("render", help="render a finished report_card.json")
    p_render.add_argument("json")
    a = ap.parse_args()

    if a.cmd == "plan":
        print(render_plan(pipeline_stages(base=a.base, grpo_steps=a.grpo_steps)))
    elif a.cmd == "stages":
        print(json.dumps(pipeline_stages(), indent=2))
    elif a.cmd == "render":
        print(render_report_card(a.json))


if __name__ == "__main__":
    main()
