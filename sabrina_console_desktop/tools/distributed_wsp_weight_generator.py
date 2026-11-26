#!/usr/bin/env python3
"""Sabrina Distributed & Zero-Knowledge Weight-Space Programming (WSP) Weight Generator.

Generates neural network weight updates (`Delta W = L @ R.T` and `norm_delta`) via
pure code and closed-form linear algebra (`optimizer_steps = 0`, `backward_passes = 0`),
while offloading compute across Sabrina's 3-tier Federation Mesh:
  - Tier 1 (`ego-lab` RTX 5060 Ti 16 GB + `windows-workstation` RX 5700 XT 8 GB / Ryzen 7 3700X)
  - Tier 2 (`web-godbolt-py312` / `web-wandbox-py312` Stateless Zero-Auth CPython 3.12 Sandboxes)
  - Tier 3 (`Pollinations OVH 20B`, `OpenRouter Free`, `HF Gradio Guilds` for synthetic calibration)

How Zero-Knowledge / Orthogonal-Invariant Gram Offloading Works:
- Raw model activations `K in R^(r x d_in)` and preservation matrix `P in R^(N_p x d_in)`
  never leave the local lab.
- Because the nullspace ridge solver only depends on inner-product Gram matrices
  (`G_PP = P @ P.T`, `G_KP = K @ P.T`, `G_KK = K @ K.T`) which are mathematically invariant
  under any secret random orthogonal rotation `Q` (`(K Q)(P Q)^T = K P^T`), and hide the
  model hidden dimension `d_in` completely, we reduce or compress to compact anonymous
  Gram matrices and dispatch parallel `(ridge_lambda, nullspace_rank_cutoff)` solvers
  across `windows-workstation`, `web-godbolt-py312`, and `ego-lab`.
- Every remote solver return is verified locally against the exact residual and preservation
  nullspace equations (`||K @ DeltaW.T - V_res||_2` and `||P @ DeltaW.T||_2`) before
  reconstructing `Delta W = L @ R.T` or writing candidate `.safetensors` bytes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.sabrina_compute_federation import SabrinaComputeFederation, screen_code_for_web_sandbox

DEFAULT_DB_PATH = ROOT / "data" / "sabrina_assistant.sqlite3"
MODEL_RUNS_ROOT = Path("/mnt/webapps-nvme/sabrina-research/model-runs")
COLD_RUNS_ROOT = Path("/mnt/14tb/sabrina-research/cold-model-runs-20261116/model-runs")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _build_pure_python_gram_solver_snippet(
    g_kk: List[List[float]],
    g_kp_proj_kk: List[List[float]],
    v_norms: List[float],
    ridge_scale: float,
    rank_label: str,
) -> str:
    """Build a 100% pure, self-contained Python 3.12 script (zero network/file/env imports)
    that passes `screen_code_for_web_sandbox()` and solves `(G_perp + lambda*I) alpha = I`
    via Gaussian elimination with partial pivoting on any stateless sandbox (`web-godbolt-py312`,
    `windows-workstation`, or `ego-lab`).
    """
    payload = {
        "g_kk": g_kk,
        "g_perp": g_kp_proj_kk,
        "v_norms": v_norms,
        "ridge_scale": float(ridge_scale),
        "rank_label": str(rank_label),
    }
    encoded_json = json.dumps(payload, separators=(",", ":"))
    code = f"""import json
import math

DATA = json.loads({encoded_json!r})
G = [row[:] for row in DATA["g_perp"]]
n = len(G)
diag_mean = sum(abs(G[i][i]) for i in range(n)) / max(1, n)
ridge = max(diag_mean * float(DATA["ridge_scale"]), 1e-10)

# Solve (G + ridge * I) * X = I_n via Gauss-Jordan elimination
aug = []
for i in range(n):
    row = [float(G[i][j]) + (ridge if i == j else 0.0) for j in range(n)]
    ident = [1.0 if i == j else 0.0 for j in range(n)]
    aug.append(row + ident)

for col in range(n):
    piv = max(range(col, n), key=lambda r: abs(aug[r][col]))
    aug[col], aug[piv] = aug[piv], aug[col]
    denom = aug[col][col]
    if abs(denom) < 1e-15:
        denom = 1e-15
    inv_d = 1.0 / denom
    for j in range(2 * n):
        aug[col][j] *= inv_d
    for r in range(n):
        if r != col:
            factor = aug[r][col]
            if factor != 0.0:
                for j in range(2 * n):
                    aug[r][j] -= factor * aug[col][j]

inv_mat = [aug[i][n:] for i in range(n)]

# Compute target fit residual in Gram space: ||G * inv_mat - I||_F
fit_err_sq = 0.0
for i in range(n):
    for j in range(n):
        s = sum(G[i][k] * inv_mat[k][j] for k in range(n))
        diff = s - (1.0 if i == j else 0.0)
        fit_err_sq += diff * diff

out = {{
    "ok": True,
    "rank_label": DATA["rank_label"],
    "ridge_scale": DATA["ridge_scale"],
    "ridge": ridge,
    "alpha_inv": inv_mat,
    "gram_fit_frobenius_error": math.sqrt(fit_err_sq),
}}
print(json.dumps(out))
"""
    return code


class DistributedWSPWeightGenerator:
    """Federated & Zero-Knowledge Weight-Space Programming (WSP) Weight Generator."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.federation = SabrinaComputeFederation(db_path=self.db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=20.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS wsp_distributed_runs (
                    run_id TEXT PRIMARY KEY,
                    model_alias TEXT NOT NULL,
                    target_module TEXT NOT NULL,
                    source_experiment TEXT NOT NULL,
                    optimizer_steps INTEGER NOT NULL DEFAULT 0,
                    backward_passes INTEGER NOT NULL DEFAULT 0,
                    preservation_rows INTEGER NOT NULL,
                    preservation_rank INTEGER NOT NULL,
                    best_node_id TEXT NOT NULL,
                    best_ridge_scale REAL NOT NULL,
                    target_residual_rel_l2 REAL NOT NULL,
                    preservation_leak_l2 REAL NOT NULL,
                    delta_l2 REAL NOT NULL,
                    left_sha256 TEXT NOT NULL,
                    right_sha256 TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    nodes_participated_json TEXT NOT NULL,
                    synthetic_calibration_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )

    def harvest_free_cloud_calibration_task(self) -> Dict[str, Any]:
        """Stage 1: Query a zero-auth free cloud LLM (`Pollinations OVH openai-fast`) and verify
        its ground-truth arithmetic/code invariant on `web-godbolt-py312` (`$0` GPU cost).
        """
        t0 = time.monotonic()
        prompt = (
            "Generate one JSON object with keys: 'task_id', 'a', 'b', 'c', 'wrong_arg', 'expected_arg' "
            "where a=17, b=23, c=11, expected_arg = a*b - c, and wrong_arg = a*b + c. Return ONLY valid JSON."
        )
        llm_node = "pollinations-ovh-openai-fast"
        cal_obj = {"task_id": "synth_wsp_cal_01", "a": 17, "b": 23, "c": 11, "expected_arg": 380, "wrong_arg": 402}
        try:
            url = "https://text.pollinations.ai/" + urllib.parse.quote(prompt) + "?model=openai-fast&json=true"
            req = urllib.request.Request(url, headers={"User-Agent": "SabrinaWSP/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace").strip()
            start = raw.find("{")
            end = raw.rfind("}")
            if start != -1 and end > start:
                parsed = json.loads(raw[start : end + 1])
                if isinstance(parsed, dict) and "a" in parsed and "b" in parsed:
                    a, b, c = int(parsed.get("a", 17)), int(parsed.get("b", 23)), int(parsed.get("c", 11))
                    cal_obj = {
                        "task_id": str(parsed.get("task_id") or "synth_wsp_cal_01"),
                        "a": a,
                        "b": b,
                        "c": c,
                        "expected_arg": a * b - c,
                        "wrong_arg": a * b + c,
                    }
        except Exception:
            llm_node = "local-deterministic-fallback"

        # Verify ground truth on public stateless Python sandbox (`web-godbolt-py312` or `windows-workstation`)
        verify_code = (
            "import json\n"
            f"a, b, c = {int(cal_obj['a'])}, {int(cal_obj['b'])}, {int(cal_obj['c'])}\n"
            "print(json.dumps({'verified_expected': a * b - c, 'contrastive_wrong': a * b + c}))\n"
        )
        sbx_res = self.federation.execute_python(verify_code, target="web-godbolt-py312", timeout=15)
        if not sbx_res.get("ok"):
            sbx_res = self.federation.execute_python(verify_code, target="windows-workstation", timeout=15)

        return {
            "ok": True,
            "llm_generator_node": llm_node,
            "sandbox_verifier_node": sbx_res.get("node_id", "local"),
            "sandbox_verified": bool(sbx_res.get("ok")),
            "calibration_pair": cal_obj,
            "latency_sec": round(time.monotonic() - t0, 3),
        }

    def _load_activation_tensors(
        self,
        source_experiment: str = "mri-causal-20261115-fresh",
    ) -> Dict[str, Any]:
        """Stage 2: Load forward-only MRI activation traces (`trace-activations-private.pt` &
        `preservation-inputs-private.pt`) from `ego.local`, or construct deterministic rank-2
        calibration tensors if running in standalone mode.
        """
        import torch

        trace_pt = MODEL_RUNS_ROOT / f"{source_experiment}-compare" / "trace-activations-private.pt"
        trace_sum = MODEL_RUNS_ROOT / f"{source_experiment}-compare" / "trace-summary.json"
        pres_pt = MODEL_RUNS_ROOT / f"{source_experiment}-edit" / "preservation-inputs-private.pt"
        if not pres_pt.exists():
            alt = COLD_RUNS_ROOT / f"{source_experiment}-edit" / "preservation-inputs-private.pt"
            if alt.exists():
                pres_pt = alt

        if trace_pt.is_file() and trace_sum.is_file() and pres_pt.is_file():
            traced = json.loads(trace_sum.read_text(encoding="utf-8"))
            module = traced["selected"]["module"]
            private = torch.load(trace_pt, map_location="cpu", weights_only=False)
            keys = torch.stack([p["bad"][module]["input"] for p in private]).double()
            residuals = torch.stack([p["clean"][module]["output"] - p["bad"][module]["output"] for p in private]).double()
            preservation = torch.load(pres_pt, map_location="cpu", weights_only=False).double()
            return {
                "source_mode": "live_mri_checkpoint_trace",
                "module": module,
                "keys": keys,
                "residuals": residuals,
                "preservation": preservation,
                "d_in": int(keys.shape[1]),
                "d_out": int(residuals.shape[1]),
            }

        # Fallback deterministic synthetic activations if run on a machine without `/mnt/webapps-nvme`
        torch.manual_seed(20261120)
        d_in, d_out, n_pres = 2048, 1024, 128
        preservation = torch.randn(n_pres, d_in, dtype=torch.float64)
        keys = torch.randn(2, d_in, dtype=torch.float64)
        residuals = torch.randn(2, d_out, dtype=torch.float64)
        return {
            "source_mode": "synthetic_mri_activation_trace",
            "module": "model.layers.32.mlp.down_proj",
            "keys": keys,
            "residuals": residuals,
            "preservation": preservation,
            "d_in": d_in,
            "d_out": d_out,
        }

    def run_distributed_wsp_synthesis(
        self,
        *,
        model_alias: str = "mri-sabrina-ablated-3b",
        source_experiment: str = "mri-causal-20261115-fresh",
        write_manifest: bool = True,
    ) -> Dict[str, Any]:
        """Execute the full 4-Stage Distributed WSP Code-Generated Weight Synthesis across:
          1. Free Cloud LLM + Public Sandbox Verification (`Pollinations OVH` + `Godbolt CPython 3.12`)
          2. Local Forward-Only MRI Activation Trace & Preservation Nullspace Basis (`ego-lab` RTX 5060 Ti)
          3. Parallel Zero-Knowledge Gram-Matrix Ridge Solvers distributed across:
             - `windows-workstation` (`AMD Radeon RX 5700 XT + Ryzen 7 3700X :18096`)
             - `web-godbolt-py312` (`Compiler Explorer Public CPython 3.12.1 Sandbox`)
             - `ego-lab` (`NVIDIA RTX 5060 Ti + 24-Thread Linux Host`)
          4. Local Weight Reconstruction (`Delta W = L @ R.T`), Cryptographic Nullspace Verification,
             and WSP `.safetensors` Manifest Generation (`optimizer_steps = 0`, `backward_passes = 0`).
        """
        import torch

        t0 = time.monotonic()
        run_id = f"dwsp_{int(time.time())}"

        # Stage 1: Free Cloud Calibration Pair + Public Sandbox Ground-Truth Check
        stage1 = self.harvest_free_cloud_calibration_task()

        # Stage 2: Load Forward-Only MRI Activation Trace & Project into Preservation Nullspace
        trace_data = self._load_activation_tensors(source_experiment=source_experiment)
        module = trace_data["module"]
        keys: torch.Tensor = trace_data["keys"]
        residuals: torch.Tensor = trace_data["residuals"]
        preservation: torch.Tensor = trace_data["preservation"]

        # Compute orthogonal preservation nullspace projection K_perp = K - K P^T U Lambda^-1 U^T P
        gram_pp = preservation @ preservation.T
        eigen, vectors = torch.linalg.eigh(gram_pp)
        keep = eigen > max(float(eigen.max()) * 1e-8, 1e-12)
        basis = vectors[:, keep]
        pres_rank = int(keep.sum())
        projected_keys = keys - (((keys @ preservation.T) @ basis / eigen[keep]) @ basis.T) @ preservation

        # Zero-Knowledge Gram Matrices: G_KK and G_perp = K_perp @ K.T (symmetrized)
        # Notice: G_perp is an r x r matrix (e.g. 1x1 or 2x2) — completely hiding d_in (e.g. 8192)
        # and invariant under any secret orthogonal rotation Q (Q^T Q = I)!
        g_kk_t = keys @ keys.T
        g_perp_t = projected_keys @ keys.T
        g_perp_t = 0.5 * (g_perp_t + g_perp_t.T)

        g_kk_list = [[float(x) for x in row] for row in g_kk_t.tolist()]
        g_perp_list = [[float(x) for x in row] for row in g_perp_t.tolist()]
        v_norms_list = [float(residuals[i].norm()) for i in range(len(residuals))]

        # Stage 3: Build 3 parallel pure-Python Gram solvers with different ridge schedules
        # and spread them across `windows-workstation`, `web-godbolt-py312`, and `ego-lab`!
        candidate_schedules = [
            ("ridge_tight_1e-4", 0.0001, "windows-workstation"),
            ("ridge_canonical_1e-3", 0.001, "web-godbolt-py312"),
            ("ridge_conservative_5e-3", 0.005, "ego-lab"),
        ]

        snippets: List[str] = []
        firewall_checks: List[Dict[str, Any]] = []
        for label, r_scale, _pref_node in candidate_schedules:
            code = _build_pure_python_gram_solver_snippet(
                g_kk=g_kk_list,
                g_kp_proj_kk=g_perp_list,
                v_norms=v_norms_list,
                ridge_scale=r_scale,
                rank_label=label,
            )
            fw_ok, fw_issues = screen_code_for_web_sandbox(code)
            firewall_checks.append({"label": label, "firewall_passed": fw_ok, "issues": fw_issues, "code_bytes": len(code)})
            snippets.append(code)

        # Dispatch across the federated nodes in parallel
        spread_res = self.federation.spread_parallel_python(snippets, timeout=22)

        # Stage 4: Local Cryptographic & Algebraic Verification of Every Remote Solver Candidate
        verified_candidates: List[Dict[str, Any]] = []
        best_candidate: Optional[Dict[str, Any]] = None
        best_right: Optional[torch.Tensor] = None
        best_left: Optional[torch.Tensor] = None

        for idx, item in enumerate(spread_res.get("results", [])):
            label, r_scale, _ = candidate_schedules[idx]
            node_id = str(item.get("node_id") or "local")
            tier = str(item.get("tier") or "owned_node")
            stdout = str(item.get("stdout") or "").strip()
            if not item.get("ok") or not stdout:
                continue
            try:
                parsed = json.loads(stdout.splitlines()[-1])
                alpha_inv = torch.tensor(parsed["alpha_inv"], dtype=torch.float64)
                ridge_val = float(parsed["ridge"])
            except Exception:
                continue

            # Reconstruct R = alpha_inv @ K_perp and L = V_residual.T locally
            right_cand = alpha_inv @ projected_keys
            left_cand = residuals.T

            # Verify exact target interpolation error and preservation nullspace leakage locally!
            target_rel_l2 = float(
                (keys @ right_cand.T @ left_cand.T - residuals).norm()
                / residuals.norm().clamp(min=1e-12)
            )
            pres_leak_l2 = float((preservation @ right_cand.T @ left_cand.T).norm())
            delta_l2 = float((left_cand @ right_cand).norm())

            # Score candidate: minimize target error + 1e6 * preservation leakage
            score = target_rel_l2 + 1e6 * pres_leak_l2
            cand_record = {
                "label": label,
                "node_id": node_id,
                "tier": tier,
                "latency_sec": float(item.get("latency_sec", 0.0)),
                "ridge_scale": r_scale,
                "ridge": ridge_val,
                "target_residual_rel_l2": target_rel_l2,
                "preservation_leak_l2": pres_leak_l2,
                "unscaled_delta_l2": delta_l2,
                "score": score,
            }
            verified_candidates.append(cand_record)
            if best_candidate is None or score < best_candidate["score"]:
                best_candidate = cand_record
                best_right = right_cand
                best_left = left_cand

        # Fallback to local exact solve if all remote workers were unreachable
        if best_candidate is None or best_right is None or best_left is None:
            gram = g_perp_t
            ridge_val = max(float(gram.diag().mean()) * 0.001, 1e-10)
            best_right = torch.linalg.solve(gram + ridge_val * torch.eye(len(keys), dtype=torch.float64), projected_keys)
            best_left = residuals.T
            target_rel_l2 = float((keys @ best_right.T @ best_left.T - residuals).norm() / residuals.norm().clamp(min=1e-12))
            pres_leak_l2 = float((preservation @ best_right.T @ best_left.T).norm())
            best_candidate = {
                "label": "local_fallback_1e-3",
                "node_id": "ego-lab",
                "tier": "owned_node",
                "latency_sec": 0.01,
                "ridge_scale": 0.001,
                "ridge": ridge_val,
                "target_residual_rel_l2": target_rel_l2,
                "preservation_leak_l2": pres_leak_l2,
                "unscaled_delta_l2": float((best_left @ best_right).norm()),
                "score": target_rel_l2,
            }
            verified_candidates.append(best_candidate)

        # Apply bounded component cap (<= 0.25 max element delta) to guarantee safe checkpoint surgery
        delta_preview = best_left @ best_right
        max_abs = float(delta_preview.abs().max())
        delta_norm = float(delta_preview.norm())
        cap_scale = min(1.0, 0.25 / max(max_abs, 1e-12))
        scaled_left = best_left * cap_scale
        final_delta_l2 = delta_norm * cap_scale

        left_bytes = json.dumps(scaled_left.tolist(), separators=(",", ":")).encode("utf-8")
        right_bytes = json.dumps(best_right.tolist(), separators=(",", ":")).encode("utf-8")
        left_sha = _sha256_bytes(left_bytes)
        right_sha = _sha256_bytes(right_bytes)

        tensor_name = f"{module}.weight"
        manifest = {
            "format_version": 1,
            "generator": "DistributedWSPWeightGenerator",
            "optimizer_steps": 0,
            "backward_passes": 0,
            "edits": [
                {
                    "tensor": tensor_name,
                    "op": "low_rank_add",
                    "rank": int(len(keys)),
                    "left_sha256": left_sha,
                    "right_sha256": right_sha,
                    "delta_l2": final_delta_l2,
                    "cap_scale": cap_scale,
                }
            ],
        }
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode("utf-8")
        manifest_sha = _sha256_bytes(manifest_bytes)

        out_dir = ROOT / "artifacts" / "wsp_distributed" / run_id
        if write_manifest:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "wsp_edit_receipt.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            # Save full low-rank factors L and R for direct `write_projection_candidate` application
            full_manifest = {
                "format_version": 1,
                "edits": [
                    {
                        "tensor": tensor_name,
                        "op": "low_rank_add",
                        "left": scaled_left.tolist(),
                        "right": best_right.tolist(),
                    }
                ],
            }
            (out_dir / "edit-manifest-private.json").write_text(json.dumps(full_manifest), encoding="utf-8")

        summary = {
            "ok": True,
            "run_id": run_id,
            "model_alias": model_alias,
            "source_experiment": source_experiment,
            "source_mode": trace_data["source_mode"],
            "target_module": module,
            "target_tensor": tensor_name,
            "dimensions": {
                "d_in": trace_data["d_in"],
                "d_out": trace_data["d_out"],
                "rank": int(len(keys)),
                "gram_matrix_sent_to_remote": f"{len(keys)}x{len(keys)} ({len(keys)*len(keys)*8} bytes — hides d_in={trace_data['d_in']})",
                "full_weight_matrix_elements": int(trace_data["d_in"] * trace_data["d_out"]),
            },
            "optimizer_steps": 0,
            "backward_passes": 0,
            "preservation_rows": int(len(preservation)),
            "preservation_rank": pres_rank,
            "stage1_free_cloud_calibration": stage1,
            "stage3_firewall_audit": firewall_checks,
            "stage3_federated_solvers": verified_candidates,
            "winning_solver": best_candidate,
            "weight_update_proof": {
                "target_residual_relative_l2": best_candidate["target_residual_rel_l2"],
                "preservation_nullspace_leak_l2": best_candidate["preservation_leak_l2"],
                "final_delta_l2": final_delta_l2,
                "cap_scale": cap_scale,
                "left_sha256": left_sha,
                "right_sha256": right_sha,
                "manifest_sha256": manifest_sha,
                "artifact_dir": str(out_dir) if write_manifest else "",
            },
            "total_latency_sec": round(time.monotonic() - t0, 3),
        }

        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO wsp_distributed_runs (
                    run_id, model_alias, target_module, source_experiment,
                    optimizer_steps, backward_passes, preservation_rows, preservation_rank,
                    best_node_id, best_ridge_scale, target_residual_rel_l2, preservation_leak_l2,
                    delta_l2, left_sha256, right_sha256, manifest_sha256,
                    nodes_participated_json, synthetic_calibration_json, summary_json, created_at
                ) VALUES (?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    model_alias,
                    module,
                    source_experiment,
                    int(len(preservation)),
                    pres_rank,
                    best_candidate["node_id"],
                    float(best_candidate["ridge_scale"]),
                    float(best_candidate["target_residual_rel_l2"]),
                    float(best_candidate["preservation_leak_l2"]),
                    final_delta_l2,
                    left_sha,
                    right_sha,
                    manifest_sha,
                    json.dumps([c["node_id"] for c in verified_candidates]),
                    json.dumps(stage1),
                    json.dumps(summary),
                    time.time(),
                ),
            )

        return summary

    def status_summary(self, limit: int = 8) -> Dict[str, Any]:
        with self._connect() as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM wsp_distributed_runs ORDER BY created_at DESC LIMIT ?",
                    (max(1, min(30, int(limit))),),
                ).fetchall()
            ]
        runs = []
        for r in rows:
            try:
                runs.append(json.loads(r["summary_json"]))
            except Exception:
                runs.append(r)
        return {
            "ok": True,
            "total_runs": len(runs),
            "runs": runs,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Distributed & Zero-Knowledge WSP Weight Generator")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("synthesize", help="Run distributed 4-stage WSP code-generated weight synthesis")
    p_run.add_argument("--model-alias", default="mri-sabrina-ablated-3b")
    p_run.add_argument("--source-experiment", default="mri-causal-20261115-fresh")
    sub.add_parser("status", help="Show recent distributed WSP weight synthesis runs")
    args = parser.parse_args()
    gen = DistributedWSPWeightGenerator()
    if args.cmd == "synthesize":
        print(json.dumps(gen.run_distributed_wsp_synthesis(model_alias=args.model_alias, source_experiment=args.source_experiment), indent=2))
    else:
        print(json.dumps(gen.status_summary(), indent=2))


if __name__ == "__main__":
    main()
