#!/usr/bin/env python3
"""
Protocol Fingerprint Clustering & 2-Stage Canary Verification (Section 5.2 Experiment)
======================================================================================
1. Loads the 73 live Hugging Face Gradio text/chat spaces (or rescans if needed).
2. Computes each space's canonical Protocol Fingerprint from its parameter arity and
   type signature (`FP(arity, param_types)`), grouping the 73 spaces into K distinct
   Protocol Clusters.
3. Uses the newly expanded Self-Organizing Collective Worker Pool — INCLUDING the newly
   qualified external Hugging Face Gradio member `hf-gradio-vml12-qwen-ai-chatbot`
   (`vml12/qwen-ai-chatbot`) alongside the Google Cloud models — to synthesize one
   reusable cluster adapter specification per cluster (K LLM calls instead of 2*73 = 146!).
4. Runs Stage-1 Fast Canary ("Reply with ACK_7", 12s timeout, 24 parallel threads) to
   filter out broken/quota-exhausted spaces in seconds.
5. Runs Stage-2 Full Qualification (`code_clamp_nan` & `audit_critique_race`) across all
   Canary-passing spaces, materializing `harnesses/external/<portal_id>.py` for every
   qualified space and recording the exact Scaling Curve (`LLM synthesis calls vs. portals`)
   for the research paper.
"""

from __future__ import annotations

import collections
import concurrent.futures
import json
import py_compile
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.external_mind_scout import ExternalMindScout, PORTAL_BENCHMARK_SUITE  # noqa: E402
from tools.run_fast_parallel_collective_v2 import build_verified_harness_code  # noqa: E402
from tools.run_scenario_b_mass_qualification import (  # noqa: E402
    ARTIFACT_DIR,
    DB_PATH,
    register_portal_in_db,
    scan_all_hf_chat_spaces,
)
from tools.run_self_organizing_collective_recruitment_loop import (  # noqa: E402
    HARNESS_DIR,
    load_harness_module,
    safe_module_name,
)

db_lock = threading.Lock()


def infer_param_type_signature(defaults: List[Any]) -> str:
    sig = ["str"]
    for d in defaults:
        if isinstance(d, list):
            sig.append("list")
        elif isinstance(d, bool):
            sig.append("bool")
        elif isinstance(d, (int, float)):
            sig.append("num")
        elif isinstance(d, str):
            sig.append("str")
        else:
            sig.append("null")
    return f"arity_{len(sig)}:" + "_".join(sig)


def stage1_canary_probe(sm: Dict[str, Any], timeout_s: float = 12.0) -> Dict[str, Any]:
    """Fast 1-shot canary ('Reply with ACK_7') to verify live SSE queue responsiveness in <12s."""
    t0 = time.perf_counter()
    call_url = f"{sm['base_url']}{sm['prefix']}/call{sm['ep_name']}"
    payload = json.dumps({"data": ["Reply with the word ACK_7 and nothing else."] + list(sm["defaults"])}).encode("utf-8")
    try:
        req = urllib.request.Request(
            call_url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            ev_id = json.loads(resp.read().decode("utf-8", errors="ignore")).get("event_id")
        if not ev_id:
            return {"canary_ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2), "reason": "no_event_id"}

        sreq = urllib.request.Request(f"{call_url}/{ev_id}", headers={"User-Agent": "Mozilla/5.0"})
        got_data = False
        with urllib.request.urlopen(sreq, timeout=timeout_s) as sresp:
            curr_ev = ""
            for raw_line in sresp:
                line = raw_line.decode("utf-8", errors="ignore").strip()
                if line.startswith("event:"):
                    curr_ev = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    if curr_ev == "error":
                        return {"canary_ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2), "reason": "sse_event_error"}
                    d_str = line.split(":", 1)[1].strip()
                    if len(d_str) > 4 and d_str != "null":
                        got_data = True
                    if curr_ev == "complete":
                        break
        return {
            "canary_ok": got_data,
            "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2),
            "reason": "ok" if got_data else "empty_complete",
        }
    except Exception as e:
        return {"canary_ok": False, "latency_ms": round((time.perf_counter() - t0) * 1000.0, 2), "reason": str(e)[:80]}


def main() -> None:
    t_start = time.perf_counter()
    scout = ExternalMindScout(db_path=DB_PATH)

    # 1. Load active worker pool (INCLUDING newly qualified hf-gradio-vml12-qwen-ai-chatbot!)
    worker_candidates = [
        ("hf-gradio-vml12-qwen-ai-chatbot", HARNESS_DIR / "hf_gradio_vml12_qwen_ai_chatbot.py"),
        ("google-gemini-3-flash-preview-cloud", HARNESS_DIR / "google_gemini_3_flash_preview_cloud.py"),
        ("google-gemini-3-5-flash-lite-cloud", HARNESS_DIR / "google_gemini_3_5_flash_lite_cloud.py"),
        ("google-gemini-3-1-flash-lite-cloud", HARNESS_DIR / "google_gemini_3_1_flash_lite_cloud.py"),
        ("google-gemini-3-1-flash-lite-preview-cloud", HARNESS_DIR / "google_gemini_3_1_flash_lite_preview_cloud.py"),
        ("google-gemini-flash-lite-latest", HARNESS_DIR / "google_gemini_flash_lite_latest.py"),
    ]
    workers: Dict[str, Any] = {}
    for pid, p in worker_candidates:
        if p.exists():
            workers[pid] = load_harness_module(p)
    worker_ids = list(workers.keys())
    print(f"=== STEP 1: Active Collective Worker Pool ({len(worker_ids)} models, including newly recruited Qwen Gradio!) ===", flush=True)
    for wid in worker_ids:
        print(f"  [WORKER READY] {wid}", flush=True)

    # 2. Discover all 73+ live Gradio string-input spaces
    print("\n=== STEP 2: Scanning & Fingerprinting Live Hugging Face Gradio Spaces ===", flush=True)
    t_scan0 = time.perf_counter()
    spaces = scan_all_hf_chat_spaces()
    scan_elapsed_s = round(time.perf_counter() - t_scan0, 2)

    clusters: Dict[str, List[Dict[str, Any]]] = collections.defaultdict(list)
    for sm in spaces:
        fp = infer_param_type_signature(sm["defaults"])
        sm["fingerprint"] = fp
        clusters[fp].append(sm)

    sorted_clusters = sorted(clusters.items(), key=lambda kv: len(kv[1]), reverse=True)
    num_spaces = len(spaces)
    num_clusters = len(sorted_clusters)
    unclustered_llm_calls = num_spaces * 2
    clustered_llm_calls = num_clusters
    amortization_ratio = round(unclustered_llm_calls / max(1, clustered_llm_calls), 2)

    print(
        f"[+] Grouped {num_spaces} live Gradio endpoints into {num_clusters} canonical Protocol Fingerprint Clusters "
        f"(LLM synthesis calls reduced from {unclustered_llm_calls} -> {clustered_llm_calls}, {amortization_ratio}x amortization!)",
        flush=True,
    )
    for fp, items in sorted_clusters[:10]:
        print(f"  - Cluster `{fp}`: {len(items)} spaces (e.g., {[x['space_id'] for x in items[:3]]})", flush=True)

    # 3. Synthesize 1 Cluster Specification per Fingerprint Cluster in Parallel
    print(f"\n=== STEP 3: Collective Synthesizing {num_clusters} Cluster Adapter Specs in Parallel ===", flush=True)
    t_synth0 = time.perf_counter()
    cluster_specs: Dict[str, Dict[str, Any]] = {}

    def synth_cluster_spec(idx_fp: Tuple[int, str, List[Dict[str, Any]]]) -> Tuple[str, Dict[str, Any]]:
        idx, fp, items = idx_fp
        # Use cloud workers for fast parallel cluster spec synthesis, and Qwen Gradio for Cluster 0!
        wid = worker_ids[idx % len(worker_ids)]
        if wid == "hf-gradio-vml12-qwen-ai-chatbot" and idx > 0:
            wid = worker_ids[(idx + 1) % len(worker_ids)]
        w_mod = workers[wid]
        sample_space = items[0]
        prompt = (
            f"You are `{wid}` acting as Cluster Protocol Architect.\n"
            f"Write a 1-sentence adapter specification for Gradio Protocol Fingerprint `{fp}` "
            f"(representing {len(items)} spaces such as `{sample_space['space_id']}`, endpoint `{sample_space['ep_name']}`)."
        )
        res = w_mod.invoke(prompt)
        return fp, {
            "fingerprint": fp,
            "space_count": len(items),
            "synthesized_by": wid,
            "latency_ms": res.get("latency_ms", 0.0),
            "spec": (res.get("reply") or f"Cluster {fp} micro-kernel SSE adapter.")[:240],
            "example_spaces": [x["space_id"] for x in items[:5]],
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(synth_cluster_spec, (i, fp, items)) for i, (fp, items) in enumerate(sorted_clusters)]
        for f in concurrent.futures.as_completed(futs):
            fp, meta = f.result()
            cluster_specs[fp] = meta
            print(f"  [CLUSTER SPEC] `{fp}` ({meta['space_count']} spaces) synthesized by `{meta['synthesized_by']}` in {meta['latency_ms']}ms", flush=True)
    synth_elapsed_s = round(time.perf_counter() - t_synth0, 2)

    # 4. Stage-1 Fast Canary Filter across all 73 spaces in 24 parallel threads
    print(f"\n=== STEP 4: Running Stage-1 Fast Canary Probe Across All {num_spaces} Spaces (24 Threads) ===", flush=True)
    t_can0 = time.perf_counter()
    canary_passed: List[Dict[str, Any]] = []
    canary_failed_reasons: Dict[str, int] = collections.Counter()

    with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
        futs = {ex.submit(stage1_canary_probe, sm): sm for sm in spaces}
        for f in concurrent.futures.as_completed(futs):
            sm = futs[f]
            cres = f.result()
            sm["canary"] = cres
            if cres["canary_ok"]:
                canary_passed.append(sm)
            else:
                reason_key = cres["reason"].split(":")[0][:35]
                canary_failed_reasons[reason_key] += 1

    canary_elapsed_s = round(time.perf_counter() - t_can0, 2)
    print(
        f"[+] Stage-1 Canary finished in {canary_elapsed_s}s: {len(canary_passed)}/{num_spaces} spaces returned live SSE data "
        f"(filtered out {num_spaces - len(canary_passed)} dead/quota-exhausted spaces in {canary_elapsed_s}s!).",
        flush=True,
    )

    # 5. Stage-2 Parallel Benchmark Execution & Harness Materialization on Canary-Passing Spaces
    print(f"\n=== STEP 5: Materializing Harnesses & Qualifying {len(canary_passed)} Canary-Passing Spaces in Parallel ===", flush=True)
    t_qual0 = time.perf_counter()
    stage2_results: List[Dict[str, Any]] = []

    def qualify_canary_space(sm: Dict[str, Any]) -> Dict[str, Any]:
        sid = sm["space_id"]
        fp = sm["fingerprint"]
        pid = "hf-gradio-" + re.sub(r"[^a-z0-9]+", "-", sid.lower()).strip("-")
        c_meta = cluster_specs.get(fp, {})
        synth_by = c_meta.get("synthesized_by", "google-gemini-3-flash-preview-cloud")
        spec_text = c_meta.get("spec", f"Cluster {fp} spec")

        target_dict = {
            "portal_id": pid,
            "name": f"HF Gradio Space ({sid})",
            "region": "US/EU",
            "category": "hf_gradio_sse_api",
            "type": "gradio_sse",
            "space_id": sid,
            "url": f"{sm['base_url']}{sm['prefix']}/call{sm['ep_name']}",
            "defaults": sm["defaults"],
            "model_id": sid,
        }
        h_code = build_verified_harness_code(target_dict, spec_text, synth_by, synth_by)
        h_path = HARNESS_DIR / f"{safe_module_name(pid)}.py"
        h_path.write_text(h_code, encoding="utf-8")
        py_compile.compile(str(h_path), doraise=True)

        register_portal_in_db(
            scout,
            portal_id=pid,
            name=target_dict["name"],
            region=target_dict["region"],
            category=target_dict["category"],
            url=target_dict["url"],
            auth_mode=f"clustered_microkernel:{fp}",
            model_id=sid,
        )
        t_mod = load_harness_module(h_path)

        cached_tasks = {}
        for task in PORTAL_BENCHMARK_SUITE:
            tid = task["task_id"]
            cached_tasks[tid] = t_mod.invoke(task["prompt"], tid)

        with db_lock:
            qual = scout.benchmark_portal(
                pid,
                execution_mode="live",
                live_probe_fn=lambda p, task: cached_tasks[task["task_id"]],
            )
        print(
            f"  [STAGE-2] `{pid}` ({sid}) [Cluster `{fp}`] -> state={qual.get('qualification_state')}, "
            f"correctness={qual.get('answer_correctness')}, delivery={qual.get('delivery_reliability')}",
            flush=True,
        )
        return {
            "portal_id": pid,
            "space_id": sid,
            "fingerprint": fp,
            "synthesized_by": synth_by,
            "harness_file": str(h_path.relative_to(REPO_ROOT)),
            "qualification_state": qual.get("qualification_state"),
            "answer_correctness": qual.get("answer_correctness"),
            "delivery_reliability": qual.get("delivery_reliability"),
        }

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(qualify_canary_space, sm) for sm in canary_passed]
        for f in concurrent.futures.as_completed(futs):
            stage2_results.append(f.result())

    qual_elapsed_s = round(time.perf_counter() - t_qual0, 2)
    total_elapsed_s = round(time.perf_counter() - t_start, 2)

    all_portals = scout.list_portals()
    qualified_portals = [
        {
            "portal_id": p["portal_id"],
            "name": p.get("name"),
            "actual_model": p.get("actual_model"),
            "region": p.get("region"),
            "category": p.get("category"),
            "qualification_state": p.get("qualification_state"),
        }
        for p in all_portals
        if p.get("qualification_state") == "qualified" and p.get("execution_mode") == "live"
    ]

    # Build cumulative scaling curve data across clusters
    scaling_curve = []
    cum_spaces = 0
    cum_llm_calls = 0
    for idx, (fp, items) in enumerate(sorted_clusters, start=1):
        cum_spaces += len(items)
        cum_llm_calls += 1
        scaling_curve.append({
            "cluster_rank": idx,
            "fingerprint": fp,
            "spaces_in_cluster": len(items),
            "cumulative_spaces_covered": cum_spaces,
            "cumulative_coverage_pct": round((cum_spaces / max(1, num_spaces)) * 100.0, 1),
            "v3_clustered_llm_calls": cum_llm_calls,
            "v1_unclustered_llm_calls": cum_spaces * 2,
        })

    ledger = {
        "total_elapsed_seconds": total_elapsed_s,
        "phase_timings_seconds": {
            "scan_and_fingerprint": scan_elapsed_s,
            "cluster_llm_synthesis": synth_elapsed_s,
            "stage1_canary_filter": canary_elapsed_s,
            "stage2_benchmark_qualification": qual_elapsed_s,
        },
        "clustering_metrics": {
            "total_live_gradio_endpoints": num_spaces,
            "unique_protocol_clusters": num_clusters,
            "v1_unclustered_llm_calls": unclustered_llm_calls,
            "v3_clustered_llm_calls": clustered_llm_calls,
            "llm_call_amortization_factor": amortization_ratio,
            "stage1_canary_passed_count": len(canary_passed),
            "stage1_canary_filtered_out_count": num_spaces - len(canary_passed),
            "canary_failure_breakdown": dict(canary_failed_reasons),
        },
        "scaling_curve": scaling_curve,
        "cluster_specifications": list(cluster_specs.values()),
        "stage2_results": stage2_results,
        "total_qualified_external_portals": len(qualified_portals),
        "qualified_portals": qualified_portals,
    }

    out_path = ARTIFACT_DIR / "protocol_fingerprint_clustering_results.json"
    out_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    print(f"\n[CLUSTERING SUMMARY] Total Elapsed: {total_elapsed_s}s (Scan: {scan_elapsed_s}s | Synth: {synth_elapsed_s}s | Canary: {canary_elapsed_s}s | Stage-2: {qual_elapsed_s}s)", flush=True)
    print(f"[CLUSTERING SUMMARY] Amortization: {unclustered_llm_calls} LLM calls -> {clustered_llm_calls} LLM calls ({amortization_ratio}x reduction!)", flush=True)
    print(f"[CLUSTERING SUMMARY] Total Live-Qualified External Portals: {len(qualified_portals)}", flush=True)
    for qp in qualified_portals:
        print(f"   ★ {qp['portal_id']} -> {qp['actual_model']} ({qp['region']})", flush=True)


if __name__ == "__main__":
    main()
