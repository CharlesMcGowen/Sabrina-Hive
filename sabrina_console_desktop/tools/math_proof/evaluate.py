from concurrent.futures import ThreadPoolExecutor
from statistics import median
import time

from .common import digest, load, save, compute
from .goals import validate
from .lean import Checker, HEADER, proof
from .goals import ACTIONS
from .policy import ProofPolicy
from .search import solve


def evaluate_panel(run, candidate, split, panel, client):
    manifest = load(run / "fixtures.json")
    corpus = load(run / f"{split}.json")
    if digest(corpus) != manifest[split]:
        raise ValueError("Frozen corpus was changed")
    policy = ProofPolicy.load(candidate / "policy.safetensors")
    import torch
    zero_policy = ProofPolicy({name: torch.zeros_like(tensor) for name, tensor in policy.named_buffers()})
    checker = Checker(run / "lean-cache")
    modes = ("policy", "fixed", "uniform", "zero", "reset")
    items = [item for item in corpus["problems"] if item["panel"] == panel]
    def check_item(item):
        path = candidate / split / f"{item['id']}.json"
        execution_hash = digest(dict(goal=item["goal"], header=HEADER,
                              scripts=[proof(item["goal"], a) for a in ACTIONS],
                              checkpoint=load(candidate / "manifest.json")["checkpoint_sha256"]))
        if path.exists():
            prior = load(path)
            if prior.get("execution_hash") == execution_hash:
                return prior
        results = {mode: solve(item["goal"], zero_policy if mode == "zero" else policy,
                              checker, "policy" if mode == "zero" else mode) for mode in modes}
        record = dict(id=item["id"], panel=panel, results=results, execution_hash=execution_hash)
        save(path, record)
        return record
    with compute(client):
        with ThreadPoolExecutor(max_workers=2) as pool:
            result = list(pool.map(check_item, items))
    return result


def evaluate(run, candidate, split, client):
    if split not in ("development", "holdout"):
        raise ValueError("Unknown split")
    weight_hash = digest(list((candidate / "policy.safetensors").read_bytes()))
    if split == "holdout":
        finalist = load(run / "finalist.json")
        if finalist["candidate"] != candidate.name or finalist["weight_hash"] != weight_hash:
            raise ValueError("Only the frozen finalist may see holdout")
    results = []
    for panel in range(8):
        save(candidate / "progress.json", dict(split=split, panel=panel, checked_at=time.time()))
        results.extend(evaluate_panel(run, candidate, split, panel, client))
    checker = Checker(run / "lean-cache")
    corpus = load(run / f"{split}.json")
    policy = ProofPolicy.load(candidate / "policy.safetensors")
    with compute(client):
        rejected = [not solve(item["goal"], policy, checker, budget=6)["solved"] for item in corpus["negatives"]]
    modes = ("policy", "fixed", "uniform", "zero", "reset")
    summary = {}
    for mode in modes:
        summary[mode] = dict(solved=sum(r["results"][mode]["solved"] for r in results),
                             median_attempts=median(r["results"][mode]["attempts"] for r in results))
    shared = [r for r in results if r["results"]["policy"]["solved"] and r["results"]["uniform"]["solved"]]
    reduction = 1 - median(r["results"]["policy"]["attempts"] for r in shared) / median(r["results"]["uniform"]["attempts"] for r in shared) if shared else 0
    import torch
    from .goals import features
    sample_features = torch.tensor([features(x["goal"]) for x in corpus["problems"]], dtype=torch.float32)
    with torch.inference_mode():
        cold, _ = policy(sample_features)
        warm, _ = policy(sample_features, torch.ones(len(sample_features), 6))
    history_effect = float((warm - cold).abs().max())
    verdict = dict(split=split, count=len(results), metrics=summary,
                   median_attempt_reduction_vs_uniform=reduction, negative_rejections=sum(rejected),
                   negative_count=len(rejected),
                   max_history_logit_change=history_effect,
                   passes=summary["policy"]["solved"] >= .9*len(results)
                          and summary["policy"]["solved"] >= summary["uniform"]["solved"]
                          and reduction >= .2 and all(rejected),
                   note="Attempts count separately checked Lean declarations. Six actions are batched per process; comparators reuse the same checker cache.")
    save(candidate / f"{split}-summary.json", verdict)
    return verdict
