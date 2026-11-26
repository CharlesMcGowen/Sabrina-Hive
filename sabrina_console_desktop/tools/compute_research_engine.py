"""Universal Autonomous Compute & Scientific Research Engine for Sabrina.

Enables domain-agnostic execution of compute-intensive scientific workflows:
1. Weight Space Programming (WSP): Task-vector arithmetic, TIES/DARE/SLERP model merging,
   and representation diagnostics via PyTorch.
2. High-Throughput Numerical Simulations: Vectorized Monte Carlo, stochastic differential
   equations, and statistical risk modeling via NumPy.
3. Multi-Tier Distributed Placement: Intelligent routing across Local CPU process pools,
   Lab RTX 5060 Ti GPU (:8090 coordinator lease), and Cloud GPU runtimes (Modal, Kaggle, Colab).
4. Durable SQLite Experiment Tracking: Automatic logging to `assistant_research_experiments`
   for 100% scientific reproducibility and zero duplicated compute.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
import uuid

import numpy as np
import torch

LOGGER = logging.getLogger("sabrina.compute_research_engine")
DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "sabrina_assistant.sqlite3"


# -----------------------------------------------------------------------------
# Database Schema & Initialization
# -----------------------------------------------------------------------------

def ensure_research_experiments_schema(conn: sqlite3.Connection) -> None:
    """Ensure assistant_research_experiments table and indexes exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS assistant_research_experiments (
            experiment_id TEXT PRIMARY KEY,
            timestamp REAL NOT NULL,
            project TEXT NOT NULL,
            job_type TEXT NOT NULL,
            code_fingerprint TEXT NOT NULL,
            runtime_host TEXT NOT NULL,
            hyperparameters_json TEXT DEFAULT '{}',
            metrics_json TEXT DEFAULT '{}',
            artifact_paths_json TEXT DEFAULT '[]',
            status TEXT NOT NULL,
            latency_ms REAL DEFAULT 0.0,
            error_message TEXT DEFAULT '',
            created REAL NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_proj_job ON assistant_research_experiments(project, job_type)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_timestamp ON assistant_research_experiments(timestamp)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_status ON assistant_research_experiments(status)"
    )
    conn.commit()


def record_research_experiment(
    conn: sqlite3.Connection,
    spec: ScientificJobSpec,
    runtime_host: str,
    metrics: Dict[str, Any],
    status: str = "completed",
    latency_ms: float = 0.0,
    error_message: str = "",
) -> str:
    """Directly persist an experiment execution record to assistant_research_experiments."""
    ensure_research_experiments_schema(conn)
    now_ts = time.time()
    fingerprint = spec.compute_fingerprint()
    conn.execute(
        """
        INSERT OR REPLACE INTO assistant_research_experiments (
            experiment_id, timestamp, project, job_type, code_fingerprint,
            runtime_host, hyperparameters_json, metrics_json, artifact_paths_json,
            status, latency_ms, error_message, created
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            spec.job_id,
            now_ts,
            spec.project,
            spec.job_type,
            fingerprint,
            runtime_host,
            json.dumps(spec.hyperparameters, default=str),
            json.dumps(metrics, default=str),
            json.dumps(spec.artifact_outputs),
            status,
            latency_ms,
            error_message,
            now_ts,
        ),
    )
    conn.commit()
    return spec.job_id


# -----------------------------------------------------------------------------
# Universal Job Specification
# -----------------------------------------------------------------------------

@dataclass
class ScientificJobSpec:
    """Standardized contract for compute-intensive research jobs."""
    job_type: str  # 'weight_space_math', 'monte_carlo', 'data_science', 'parameter_sweep'
    project: str = "scientific_research"
    job_id: str = field(default_factory=lambda: f"exp_{uuid.uuid4().hex[:12]}")
    code_entry: Optional[Union[Callable[..., Dict[str, Any]], str]] = None
    hyperparameters: Dict[str, Any] = field(default_factory=dict)
    runtime_preference: str = "auto"  # 'local_cpu', 'lab_gpu', 'cloud_serverless', 'auto'
    verification_criteria: Dict[str, Any] = field(default_factory=dict)
    artifact_outputs: List[str] = field(default_factory=list)
    timeout_sec: float = 300.0

    def compute_fingerprint(self) -> str:
        """Deterministically hash the job definition and hyperparameters."""
        hp_str = json.dumps(
            self.hyperparameters,
            sort_keys=True,
            default=lambda x: f"Tensor_{list(x.shape)}" if hasattr(x, "shape") else str(x),
        )
        content = f"{self.job_type}:{self.project}:{hp_str}"
        if isinstance(self.code_entry, str):
            content += f":{self.code_entry}"
        return hashlib.sha256(content.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Weight Space Programming (WSP) Toolkit
# -----------------------------------------------------------------------------

class WeightSpaceToolkit:
    """Mathematical operators for Weight Space Programming and model surgery."""

    @staticmethod
    def compute_task_vector(
        base_weights: Dict[str, torch.Tensor],
        fine_tuned_weights: Dict[str, torch.Tensor],
    ) -> Dict[str, torch.Tensor]:
        """Compute task vector tau = theta_ft - theta_base for each matching layer."""
        task_vector: Dict[str, torch.Tensor] = {}
        for key, base_param in base_weights.items():
            if key in fine_tuned_weights:
                ft_param = fine_tuned_weights[key]
                if base_param.shape != ft_param.shape:
                    raise ValueError(f"Shape mismatch at layer {key}: {base_param.shape} vs {ft_param.shape}")
                task_vector[key] = ft_param - base_param
        return task_vector

    @staticmethod
    def ties_merge(
        base_weights: Dict[str, torch.Tensor],
        task_vectors: List[Dict[str, torch.Tensor]],
        weights: Optional[List[float]] = None,
        top_k_ratio: float = 0.2,
    ) -> Dict[str, torch.Tensor]:
        """TIES Merging: Trim redundant parameters, Elect parameter signs, Merge consensus.
        
        Reference: Yadav et al., 'Resolving Interference When Merging Models' (NeurIPS 2023).
        """
        if not task_vectors:
            return {k: v.clone() for k, v in base_weights.items()}

        num_tasks = len(task_vectors)
        if weights is None:
            weights = [1.0 / num_tasks] * num_tasks
        else:
            w_sum = sum(weights)
            weights = [w / w_sum for w in weights]

        merged_weights: Dict[str, torch.Tensor] = {}

        for key, base_param in base_weights.items():
            # Gather task deltas for this key
            deltas = [tv[key] for tv in task_vectors if key in tv]
            if not deltas:
                merged_weights[key] = base_param.clone()
                continue

            # 1. Trim: Keep top_k fraction by magnitude, zero the rest
            trimmed_deltas = []
            for d in deltas:
                flat_d = d.view(-1)
                k = max(1, int(len(flat_d) * top_k_ratio))
                threshold = torch.topk(flat_d.abs(), k).values[-1]
                mask = flat_d.abs() >= threshold
                trimmed_flat = flat_d * mask
                trimmed_deltas.append(trimmed_flat.view_as(d))

            # 2. Elect Sign: Consensus sign direction
            stacked_trimmed = torch.stack(trimmed_deltas, dim=0)  # [N, ...]
            sign_consensus = torch.sign(stacked_trimmed.sum(dim=0))

            # 3. Disjoint Merge: Average parameters that agree with the consensus sign
            final_delta = torch.zeros_like(base_param)
            total_active_weight = torch.zeros_like(base_param)

            for i, td in enumerate(trimmed_deltas):
                agree_mask = (torch.sign(td) == sign_consensus) & (td != 0)
                final_delta += weights[i] * (td * agree_mask)
                total_active_weight += weights[i] * agree_mask.float()

            safe_active = torch.clamp(total_active_weight, min=1e-8)
            final_delta = final_delta / safe_active

            # Replace NaNs / infs with 0
            final_delta = torch.nan_to_num(final_delta, nan=0.0, posinf=0.0, neginf=0.0)

            # 4. Add consensus delta back to base weights
            merged_weights[key] = base_param + final_delta

        return merged_weights

    @staticmethod
    def dare_merge(
        base_weights: Dict[str, torch.Tensor],
        task_vectors: List[Dict[str, torch.Tensor]],
        weights: Optional[List[float]] = None,
        drop_rate: float = 0.4,
    ) -> Dict[str, torch.Tensor]:
        """DARE Merging: Drop And REscale parameter updates.
        
        Reference: Yu et al., 'Language Models are Super Mario' (2024).
        """
        if not task_vectors:
            return {k: v.clone() for k, v in base_weights.items()}

        num_tasks = len(task_vectors)
        if weights is None:
            weights = [1.0 / num_tasks] * num_tasks
        else:
            w_sum = sum(weights)
            weights = [w / w_sum for w in weights]

        rescale_factor = 1.0 / (1.0 - drop_rate) if drop_rate < 1.0 else 1.0
        merged_weights: Dict[str, torch.Tensor] = {}

        for key, base_param in base_weights.items():
            deltas = [tv[key] for tv in task_vectors if key in tv]
            if not deltas:
                merged_weights[key] = base_param.clone()
                continue

            accumulated_delta = torch.zeros_like(base_param)
            for i, d in enumerate(deltas):
                # Bernoulli mask: keep with probability (1 - drop_rate)
                mask = (torch.rand_like(d) > drop_rate).float()
                pruned_d = d * mask * rescale_factor
                accumulated_delta += weights[i] * pruned_d

            merged_weights[key] = base_param + accumulated_delta

        return merged_weights

    @staticmethod
    def slerp(t: float, tensor_a: torch.Tensor, tensor_b: torch.Tensor, eps: float = 1e-7) -> torch.Tensor:
        """Spherical Linear Interpolation (SLERP) between two weight tensors."""
        flat_a = tensor_a.view(-1)
        flat_b = tensor_b.view(-1)

        norm_a = torch.norm(flat_a)
        norm_b = torch.norm(flat_b)
        if norm_a < eps or norm_b < eps:
            return (1.0 - t) * tensor_a + t * tensor_b

        u_a = flat_a / norm_a
        u_b = flat_b / norm_b

        dot = torch.clamp(torch.dot(u_a, u_b), -1.0 + eps, 1.0 - eps)
        omega = torch.acos(dot)
        sin_omega = torch.sin(omega)

        if sin_omega < eps:
            return (1.0 - t) * tensor_a + t * tensor_b

        scale_a = torch.sin((1.0 - t) * omega) / sin_omega
        scale_b = torch.sin(t * omega) / sin_omega

        flat_slerp = (scale_a * flat_a + scale_b * flat_b)
        return flat_slerp.view_as(tensor_a)

    @staticmethod
    def layer_cosine_diagnostics(
        weights_a: Dict[str, torch.Tensor],
        weights_b: Dict[str, torch.Tensor],
    ) -> Dict[str, float]:
        """Compute layer-by-layer cosine similarity between two weight states."""
        similarities: Dict[str, float] = {}
        for key in weights_a:
            if key in weights_b:
                fa = weights_a[key].view(-1).float()
                fb = weights_b[key].view(-1).float()
                sim = torch.cosine_similarity(fa, fb, dim=0).item()
                similarities[key] = round(sim, 4)
        return similarities

    @staticmethod
    def frobenius_distance(
        weights_a: Dict[str, torch.Tensor],
        weights_b: Dict[str, torch.Tensor],
    ) -> Dict[str, float]:
        """Compute layer-by-layer Frobenius norm distance ||W_A - W_B||_F."""
        distances: Dict[str, float] = {}
        for key in weights_a:
            if key in weights_b:
                diff = (weights_a[key] - weights_b[key]).float()
                dist = torch.norm(diff, p="fro").item()
                distances[key] = round(dist, 4)
        return distances

    @staticmethod
    def representation_diagnostics(weight_matrix: torch.Tensor) -> Dict[str, float]:
        """Compute SVD spectral norm, condition number, and Shannon effective rank of a weight tensor."""
        mat = weight_matrix.detach().float()
        if mat.ndim > 2:
            mat = mat.reshape(mat.shape[0], -1)
        svals = torch.linalg.svdvals(mat)
        probs = svals / torch.clamp(svals.sum(), min=1e-9)
        shannon_h = -torch.sum(probs * torch.log(torch.clamp(probs, min=1e-12))).item()
        return {
            "effective_rank": round(math.exp(shannon_h), 2),
            "spectral_norm": round(svals[0].item(), 4),
            "condition_number": round((svals[0] / torch.clamp(svals[-1], min=1e-6)).item(), 2),
        }

    @classmethod
    def ties_merging(
        cls,
        task_vectors: List[Dict[str, torch.Tensor]],
        density: float = 0.35,
    ) -> Dict[str, torch.Tensor]:
        """Convenience wrapper around `ties_merge` when merging task vectors around a zero origin."""
        if not task_vectors:
            return {}
        zero_base = {k: torch.zeros_like(v) for k, v in task_vectors[0].items()}
        return cls.ties_merge(zero_base, task_vectors, top_k_ratio=density)

    @staticmethod
    def segment_blt_entropy_patches(
        text_or_bytes: Union[str, bytes],
        theta_h: float = 2.85,
        theta_delta: float = 0.95,
        max_patch_bytes: int = 12,
    ) -> Dict[str, Any]:
        """Byte Latent Transformer (BLT) dynamic Shannon-entropy byte patching (Pagnoni et al.).
        Segments raw UTF-8 bytes into variable-length patches whenever next-byte conditional
        entropy H(b_t | b_{<t}) > theta_h or monotonic jump Delta H_t > theta_delta.
        """
        raw = text_or_bytes.encode("utf-8") if isinstance(text_or_bytes, str) else bytes(text_or_bytes)
        if not raw:
            return {"total_bytes": 0, "total_patches": 0, "compression_ratio": 1.0, "patches": [], "byte_entropies": []}

        def _b_cls(b: int) -> str:
            if 97 <= b <= 122:
                return "lower"
            if 65 <= b <= 90:
                return "upper"
            if 48 <= b <= 57:
                return "digit"
            if b in (32, 9, 10, 13):
                return "space"
            return "utf8_high" if b >= 128 else "punct"

        entropies: List[float] = []
        local_ctx: Dict[bytes, Dict[int, int]] = {}
        for t, b_cur in enumerate(raw):
            if t == 0:
                entropies.append(4.25)
            else:
                b_prev = raw[t - 1]
                c_prev, c_cur = _b_cls(b_prev), _b_cls(b_cur)
                if (b_cur & 0xC0) == 0x80:
                    h_val = 1.15
                elif c_prev == c_cur and c_cur in ("lower", "space"):
                    h_val = 2.10
                elif c_prev == "upper" and c_cur == "lower":
                    h_val = 2.35
                elif c_prev == "space":
                    h_val = 3.85
                elif c_prev != c_cur:
                    h_val = 3.45
                elif c_cur == "digit":
                    h_val = 3.32
                else:
                    h_val = 2.95

                for order in (3, 2, 1):
                    if t >= order:
                        ctx = raw[t - order : t]
                        bucket = local_ctx.get(ctx)
                        if bucket:
                            tot = sum(bucket.values())
                            h_emp = -sum((c / tot) * math.log2(max(c / tot, 1e-9)) for c in bucket.values())
                            h_val = 0.55 * h_val + 0.45 * (h_emp + min(2.6, 2.0 / math.sqrt(tot + 1.0)))
                            break
                entropies.append(round(min(8.0, max(0.25, h_val)), 4))

            for order in (1, 2, 3):
                if t >= order:
                    ctx = raw[t - order : t]
                    bkt = local_ctx.setdefault(ctx, {})
                    bkt[b_cur] = bkt.get(b_cur, 0) + 1

        patches: List[Dict[str, Any]] = []
        p_start = 0
        cur_trig = "initial"
        for t in range(1, len(raw)):
            cur_len = t - p_start
            if (raw[t] & 0xC0) == 0x80 and cur_len < max_patch_bytes + 3:
                continue
            dh = entropies[t] - entropies[t - 1]
            trig = None
            if entropies[t] > theta_h and dh >= 0.0:
                trig = "global_entropy"
            elif dh > theta_delta:
                trig = "monotonic_spike"
            elif cur_len >= max_patch_bytes:
                trig = "max_patch_cap"
            if trig:
                chunk = raw[p_start:t]
                ch_h = entropies[p_start:t]
                patches.append({
                    "patch_index": len(patches),
                    "start_byte_offset": p_start,
                    "end_byte_offset": t,
                    "byte_length": len(chunk),
                    "text_repr": chunk.decode("utf-8", errors="replace"),
                    "hex_repr": chunk.hex(),
                    "mean_entropy_bits": round(float(np.mean(ch_h)), 4),
                    "max_entropy_bits": round(float(np.max(ch_h)), 4),
                    "boundary_trigger": cur_trig,
                })
                p_start = t
                cur_trig = trig

        chunk = raw[p_start:]
        ch_h = entropies[p_start:]
        patches.append({
            "patch_index": len(patches),
            "start_byte_offset": p_start,
            "end_byte_offset": len(raw),
            "byte_length": len(chunk),
            "text_repr": chunk.decode("utf-8", errors="replace"),
            "hex_repr": chunk.hex(),
            "mean_entropy_bits": round(float(np.mean(ch_h)), 4),
            "max_entropy_bits": round(float(np.max(ch_h)), 4),
            "boundary_trigger": cur_trig,
        })
        comp = round(len(raw) / max(1, len(patches)), 3)
        rel_flops = round(min(1.0, (len(patches) / max(1, len(raw))) ** 1.65 + 0.12), 4)
        return {
            "total_bytes": len(raw),
            "total_patches": len(patches),
            "compression_ratio": comp,
            "estimated_flop_savings_pct": round((1.0 - rel_flops) * 100.0, 2),
            "mean_entropy_bits": round(float(np.mean(entropies)), 4),
            "patches": patches,
            "byte_entropies": entropies,
        }

    @classmethod
    def build_blt_manifold_context(cls, target_string: str, question: str) -> Tuple[str, Dict[str, Any]]:
        """Build a pure, zero-leakage Hierarchical BLT Patch-to-Byte Manifold
        (organizing raw UTF-8 byte offsets and character properties under their dynamic Shannon-entropy patches)."""
        seg = cls.segment_blt_entropy_patches(target_string)
        char_entries: List[Dict[str, Any]] = []
        b_cur = 0
        for idx, ch in enumerate(target_string):
            cb = ch.encode("utf-8")
            b_end = b_cur + len(cb)
            if len(cb) > 1:
                c_type = "utf8_multibyte"
            elif ch.isalpha():
                c_type = "ascii_letter"
            elif ch.isdigit():
                c_type = "ascii_digit"
            else:
                c_type = "ascii_punct_or_space"
            char_entries.append({
                "char_idx": idx,
                "parity": "EVEN" if idx % 2 == 0 else "ODD",
                "ch": ch,
                "b_start": b_cur,
                "b_end": b_end,
                "b_len": len(cb),
                "hex": cb.hex(" "),
                "type": c_type,
            })
            b_cur = b_end

        tree_lines: List[str] = []
        for p in seg["patches"]:
            ps, pe = p["start_byte_offset"], p["end_byte_offset"]
            plen = p["byte_length"]
            tree_lines.append(
                f"Patch #{p['patch_index']} [byte_span={ps}:{pe}, byte_length={plen}, "
                f"H_mean={p['mean_entropy_bits']}b, trigger={p['boundary_trigger']}] text={p['text_repr']!r}"
            )
            for ce in char_entries:
                if ce["b_start"] >= ps and ce["b_start"] < pe:
                    tree_lines.append(
                        f"  |- char[{ce['char_idx']}] ({ce['parity']}) = {ce['ch']!r} "
                        f"| byte_span={ce['b_start']}:{ce['b_end']} (len={ce['b_len']}B, hex={ce['hex']}, type={ce['type']})"
                    )

        ctx = (
            "### [BLT Hierarchical Patch-to-Byte Manifold (Tokenizer-Free Structural View)]\n"
            f"- **Raw Input Stream**: `{target_string}` (`{len(target_string)}` chars, `{b_cur}` UTF-8 bytes across `{seg['total_patches']}` dynamic entropy patches)\n"
            + "\n".join(tree_lines)
            + "\n\n### Instructions\n"
            "1. First write a brief 2-4 line `BLT_TRACE:` verifying the exact byte_span or char[i] indices from the manifold tree above.\n"
            "2. On your VERY LAST line, output `FINAL_ANSWER: <exact_answer>`.\n\n"
            f"### Question\n{question}"
        )
        return ctx, seg

    @classmethod
    def run_blt_wsp_experiment(cls, sample_texts: Optional[List[str]] = None, seed: int = 42) -> Dict[str, Any]:
        """Run a PyTorch Byte Latent Transformer (BLT) + DARE-TIES Weight Space Programming experiment."""
        torch.manual_seed(seed)
        np.random.seed(seed)
        samples = sample_texts or [
            "def verify_blt_patch_invariants(raw_utf8: bytes, theta_h: float = 2.85) -> bool:\n"
            "    return len(raw_utf8) > 0 and all(0 <= b <= 255 for b in raw_utf8)",
            "SELECT provider_id, weekly_budget_tokens, used_tokens FROM frontier_weekly_budgets WHERE enabled = 1;",
            "Adversarial Unicode test: café_π_🚀_naïve_admin_аuth_token",
        ]
        d_byte, d_latent = 128, 256
        byte_emb = torch.nn.Embedding(256, d_byte)
        ngram_emb = torch.nn.Embedding(2048, d_byte)
        up_proj = torch.nn.Linear(d_byte, d_latent)
        latent_attn = torch.nn.MultiheadAttention(d_latent, num_heads=4, batch_first=True)
        down_proj = torch.nn.Linear(d_latent, d_byte)
        lm_head = torch.nn.Linear(d_byte, 256)

        # Merge two domain byte-adapters (Code & Security) via TIES + DARE in weight space
        base_w = {"up": up_proj.weight.detach().clone(), "down": down_proj.weight.detach().clone()}
        ft_code = {k: v + 0.08 * torch.randn_like(v) for k, v in base_w.items()}
        ft_sec = {k: v + 0.10 * torch.randn_like(v) for k, v in base_w.items()}
        tv_code = cls.compute_task_vector(base_w, ft_code)
        tv_sec = cls.compute_task_vector(base_w, ft_sec)
        merged_ties = cls.ties_merge(base_w, [tv_code, tv_sec], weights=[0.55, 0.45], top_k_ratio=0.35)
        merged_dare = cls.dare_merge(base_w, [tv_code, tv_sec], weights=[0.55, 0.45], drop_rate=0.25)
        with torch.no_grad():
            up_proj.weight.copy_(merged_ties["up"])
            down_proj.weight.copy_(merged_ties["down"])

        params = (
            list(byte_emb.parameters())
            + list(ngram_emb.parameters())
            + list(up_proj.parameters())
            + list(latent_attn.parameters())
            + list(down_proj.parameters())
            + list(lm_head.parameters())
        )
        opt = torch.optim.AdamW(params, lr=4e-3)
        bpb_pre: List[float] = []
        bpb_post: List[float] = []
        patch_stats: List[Dict[str, Any]] = []

        for txt in samples:
            seg = cls.segment_blt_entropy_patches(txt)
            raw = txt.encode("utf-8")
            x = torch.tensor([list(raw)], dtype=torch.long)
            hashes = (x * 257 + torch.roll(x, 1, dims=1)) % 2048
            spans = [(p["start_byte_offset"], p["end_byte_offset"]) for p in seg["patches"]]

            def _fwd() -> torch.Tensor:
                hb = byte_emb(x) + 0.4 * ngram_emb(hashes)
                hu = up_proj(hb)
                p_list = [hu[:, s:e, :].mean(dim=1, keepdim=True) for s, e in spans]
                pq = torch.cat(p_list, dim=1)
                pl, _ = latent_attn(pq, pq, pq, need_weights=False)
                # Broadcast patch latent states back to byte positions
                expanded = torch.cat(
                    [pl[:, idx : idx + 1, :].expand(-1, e - s, -1) for idx, (s, e) in enumerate(spans)],
                    dim=1,
                )
                ho = hb + down_proj(expanded)
                return lm_head(ho)

            targets = x[:, 1:].reshape(-1)
            with torch.no_grad():
                ce0 = torch.nn.functional.cross_entropy(_fwd()[:, :-1, :].reshape(-1, 256), targets).item()
                bpb_pre.append(round(ce0 / math.log(2.0), 4))
            for _ in range(6):
                opt.zero_grad()
                loss = torch.nn.functional.cross_entropy(_fwd()[:, :-1, :].reshape(-1, 256), targets)
                loss.backward()
                opt.step()
            with torch.no_grad():
                ce1 = torch.nn.functional.cross_entropy(_fwd()[:, :-1, :].reshape(-1, 256), targets).item()
                bpb_post.append(round(ce1 / math.log(2.0), 4))
            patch_stats.append({
                "total_bytes": seg["total_bytes"],
                "total_patches": seg["total_patches"],
                "compression_ratio": seg["compression_ratio"],
                "flop_savings_pct": seg["estimated_flop_savings_pct"],
            })

        spec_diag = cls.representation_diagnostics(up_proj.weight)
        cos_diag = cls.layer_cosine_diagnostics(base_w, merged_ties)
        frob_diag = cls.frobenius_distance(merged_ties, merged_dare)
        pre_m = round(float(np.mean(bpb_pre)), 4)
        post_m = round(float(np.mean(bpb_post)), 4)
        return {
            "architecture": "WeightSpaceToolkit.BLT (d_byte=128, d_latent=256, n_heads=4, DARE-TIES Merged)",
            "mean_bpb_pre_adaptation": pre_m,
            "mean_bpb_post_adaptation": post_m,
            "bpb_reduction_pct": round((1.0 - post_m / max(1e-6, pre_m)) * 100.0, 2),
            "mean_compression_ratio_bytes_per_patch": round(float(np.mean([p["compression_ratio"] for p in patch_stats])), 3),
            "mean_flop_savings_vs_byte_transformer_pct": round(float(np.mean([p["flop_savings_pct"] for p in patch_stats])), 2),
            "latent_spectral_diagnostics": spec_diag,
            "wsp_merge_cosine_mean": round(float(np.mean(list(cos_diag.values()))), 4),
            "wsp_ties_vs_dare_frobenius_mean": round(float(np.mean(list(frob_diag.values()))), 4),
            "patch_summaries": patch_stats,
        }

    @classmethod
    def train_and_merge_blt_gpu_adapter(
        cls,
        checkpoint_paths: Optional[List[str]] = None,
        steps_per_domain: int = 40,
        lr: float = 2e-3,
        seed: int = 42,
        save_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Train a strictly causal, leak-free PyTorch Byte-Latent Encoder/Adapter on GPU (CUDA)
        attached to local HuggingFace causal LM checkpoints, verify autoregressive causality via
        future-perturbation invariance, select merges on a disjoint validation split, and evaluate
        Bits-Per-Byte (BPB), Perplexity, and warmed-up end-to-end vs. adapter-head throughput on a
        strictly disjoint held-out corpus.
        """
        from transformers import AutoModelForCausalLM, AutoTokenizer

        torch.manual_seed(seed)
        np.random.seed(seed)
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        gpu_name = torch.cuda.get_device_name(0) if device.type == "cuda" else "CPU"

        if not checkpoint_paths:
            hub_root = Path("/home/ego/.cache/huggingface/hub")
            candidates = [
                hub_root / "models--Qwen--Qwen2.5-Coder-1.5B-Instruct/snapshots/2e1fd397ee46e1388853d2af2c993145b0f1098a",
                hub_root / "models--ibm-granite--granite-3.1-2b-instruct/snapshots/bbc2aed595bd38bd770263dc3ab831db9794441d",
            ]
            checkpoint_paths = [str(p) for p in candidates if p.exists()]

        # Strictly disjoint 4-way split: code_train, sec_utf8_train, val_selection, heldout_eval
        code_corpus = [
            "def ties_merge(base_w: dict, task_vecs: list, top_k_ratio: float = 0.35) -> dict:\n"
            "    merged = {}\n"
            "    for k, w0 in base_w.items():\n"
            "        deltas = torch.stack([tv[k] for tv in task_vecs if k in tv], dim=0)\n"
            "        sign_consensus = torch.sign(deltas.sum(dim=0))\n"
            "        merged[k] = w0 + deltas.mean(dim=0) * (torch.sign(deltas.mean(dim=0)) == sign_consensus)\n"
            "    return merged\n",
            "class DynamicEntropyPatcher:\n"
            "    def __init__(self, theta_h: float = 2.85, theta_delta: float = 0.95):\n"
            "        self.theta_h = theta_h\n"
            "        self.theta_delta = theta_delta\n"
            "    def is_boundary(self, h_cur: float, h_prev: float) -> bool:\n"
            "        return (h_cur > self.theta_h) or ((h_cur - h_prev) > self.theta_delta)\n",
            "async def verify_lease_epoch(conn, resource_id: str, expected_epoch: int) -> bool:\n"
            "    row = await conn.fetchone('SELECT epoch, owner FROM leases WHERE id=?', (resource_id,))\n"
            "    return bool(row and int(row['epoch']) == expected_epoch)\n",
            "def safe_report_path(root: Path, relative_name: str) -> Path:\n"
            "    resolved_root = root.resolve()\n"
            "    candidate = (resolved_root / relative_name).resolve()\n"
            "    if not candidate.is_relative_to(resolved_root):\n"
            "        raise ValueError('Path escapes root boundary')\n"
            "    return candidate\n",
            "SELECT portal_id, role, model_claim, expires FROM portal_role_qualifications WHERE expires > :now_ts ORDER BY qualified_at DESC LIMIT 25;",
            "def ridge_projection(design: torch.Tensor, target: torch.Tensor, lam: float = 1e-2) -> torch.Tensor:\n"
            "    gram = design @ design.T + lam * torch.eye(design.shape[0], device=design.device)\n"
            "    return design.T @ torch.linalg.solve(gram, target)\n",
        ]
        sec_utf8_corpus = [
            "UTF-8 boundary check: café_π_🚀_naïve | byte_offsets=[0,3,5,6,8,9,13,14,16,18,20] | valid_prefix_16=14",
            "Homoglyph audit: identifier='check_admin_аuth_token' | cyrillic_u0430_hex='d0 b0' | ascii_a_hex='61'",
            "HTTP/1.1 200 OK\r\nContent-Security-Policy: default-src 'self'\r\nX-Frame-Options: DENY\r\nContent-Type: application/json",
            "TLS alert inspection: sni=api-λ-node.example.org | cipher=TLS_AES_256_GCM_SHA384 | utf8_len=24 | status=verified",
            "Container sandbox trace: workdir=/workspace | ulimit_core=0 | cap_drop=ALL | net=cybergym-internal | rc=0",
            "Unicode normalization guard: raw='Ångström_Ω_⚡' | nfkc_bytes=15 | multibyte_seq_count=3 | ascii_fallback=false",
        ]
        val_selection_corpus = [
            "def shannon_effective_rank(singular_values: torch.Tensor) -> float:\n"
            "    probs = singular_values / torch.clamp(singular_values.sum(), min=1e-9)\n"
            "    return float(torch.exp(-torch.sum(probs * torch.log(torch.clamp(probs, min=1e-12)))).item())\n",
            "Audit receipt: path='/srv/logs/αudit_2026.jsonl' | utf8_multibyte=true | sha256_prefix='9c5ae00e' | verdict='clean'",
            "SELECT task_id, status, reserved_tokens FROM frontier_research_tasks WHERE status = 'queued' ORDER BY created_at ASC;",
        ]
        heldout_eval_corpus = [
            "def compute_bpb_from_nats(total_nats: float, utf8_byte_len: int) -> float:\n"
            "    import math\n"
            "    return total_nats / max(1.0, utf8_byte_len * math.log(2.0))\n",
            "SELECT provider_id, weekly_budget_tokens, used_tokens, reserved_tokens FROM frontier_weekly_budgets WHERE enabled = 1 ORDER BY used_tokens ASC;",
            "Packet inspection: usr=αβγ;op=🚀🔥;id=99 | max_utf8_prefix_le_16=14 | non_ascii_bytes=14 | checksum=0x08af",
            "def parse_committed_tool_call(raw_json: str, allowed_tools: set[str]) -> dict:\n"
            "    payload = json.loads(raw_json)\n"
            "    if payload.get('name') not in allowed_tools:\n"
            "        raise ValueError('Disallowed tool call')\n"
            "    return payload\n",
            "HTTP/1.1 403 Forbidden\r\nWWW-Authenticate: Bearer error=\"invalid_token\"\r\nX-Content-Type-Options: nosniff\r\nContent-Length: 0",
            "Bidi & zero-width scan: label='invoice_µs_✓_2026.pdf' | utf8_hex_sample='c2 b5 73 5f e2 9c 93' | bidi_override=0",
        ]

        train_and_val_set = set(code_corpus) | set(sec_utf8_corpus) | set(val_selection_corpus)
        if not set(heldout_eval_corpus).isdisjoint(train_and_val_set):
            raise RuntimeError("Data leakage guard tripped: heldout_eval_corpus overlaps train/validation splits")
        if not set(val_selection_corpus).isdisjoint(set(code_corpus) | set(sec_utf8_corpus)):
            raise RuntimeError("Data leakage guard tripped: val_selection_corpus overlaps domain training splits")

        out_dir = Path(save_dir or (Path(__file__).resolve().parent.parent / "models"))
        out_dir.mkdir(parents=True, exist_ok=True)
        model_reports: List[Dict[str, Any]] = []

        for ckpt in checkpoint_paths:
            ckpt_path = Path(ckpt)
            model_label = (
                ckpt_path.parent.parent.name.replace("models--", "").replace("--", "/")
                if "snapshots" in str(ckpt_path)
                else ckpt_path.name
            )
            tokenizer = AutoTokenizer.from_pretrained(str(ckpt_path), local_files_only=True)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token
            dtype = torch.bfloat16 if (device.type == "cuda" and torch.cuda.is_bf16_supported()) else torch.float16
            backbone = AutoModelForCausalLM.from_pretrained(
                str(ckpt_path),
                local_files_only=True,
                torch_dtype=dtype,
            ).to(device)
            backbone.eval()
            for p in backbone.parameters():
                p.requires_grad_(False)

            d_model = int(backbone.config.hidden_size)
            vocab_size = int(backbone.config.vocab_size)

            def _token_cumulative_end_bytes(text: str, input_ids_1d: torch.Tensor) -> List[int]:
                """Map each BPE token index k -> cumulative UTF-8 byte end offset (monotone in 1..L_b)."""
                raw_len = len(text.encode("utf-8"))
                ends: List[int] = []
                prev_end = 1
                ids_list = input_ids_1d.tolist()
                for k in range(len(ids_list)):
                    prefix_txt = tokenizer.decode(ids_list[: k + 1], skip_special_tokens=False)
                    cur_end = min(raw_len, max(prev_end, len(prefix_txt.encode("utf-8"))))
                    ends.append(cur_end)
                    prev_end = cur_end
                return ends

            def _prepare_bank(texts: List[str]):
                bank = []
                with torch.no_grad():
                    for txt in texts:
                        rb = txt.encode("utf-8")
                        enc = tokenizer(txt, return_tensors="pt").to(device)
                        i_ids = enc["input_ids"]
                        out = backbone(input_ids=i_ids, output_hidden_states=True)
                        lh = out.hidden_states[-1].detach().float()
                        sg = cls.segment_blt_entropy_patches(txt)
                        bt = torch.tensor([list(rb)], dtype=torch.long, device=device)
                        tok_ends = _token_cumulative_end_bytes(txt, i_ids[0])
                        bank.append((txt, lh, bt, sg, i_ids, tok_ends))
                return bank

            # Warm up CUDA kernels before timing BPE baseline so cold-launch overhead never skews tok/s
            with torch.no_grad():
                warm_enc = tokenizer(val_selection_corpus[0], return_tensors="pt").to(device)
                for _ in range(2):
                    _ = backbone(input_ids=warm_enc["input_ids"], output_hidden_states=True)
                if device.type == "cuda":
                    torch.cuda.synchronize()

            # 1. Measure Baseline Standard BPE Layer Metrics on strictly held-out corpus (warmed-up)
            bpe_total_nats = 0.0
            bpe_total_tokens = 0
            bpe_total_bytes = 0
            heldout_enc_cache = []
            with torch.no_grad():
                for text in heldout_eval_corpus:
                    raw_b = text.encode("utf-8")
                    enc = tokenizer(text, return_tensors="pt").to(device)
                    input_ids = enc["input_ids"]
                    heldout_enc_cache.append((text, raw_b, input_ids))
                    n_tok = int(input_ids.shape[1])
                    out = backbone(input_ids=input_ids, output_hidden_states=False)
                    logits = out.logits[:, :-1, :].float()
                    targets = input_ids[:, 1:]
                    nats = torch.nn.functional.cross_entropy(
                        logits.reshape(-1, vocab_size), targets.reshape(-1), reduction="sum"
                    ).item()
                    bpe_total_nats += nats
                    bpe_total_tokens += max(1, n_tok - 1)
                    bpe_total_bytes += len(raw_b)

            n_timing_repeats = 5
            if device.type == "cuda":
                torch.cuda.synchronize()
            t_bpe0 = time.perf_counter()
            with torch.no_grad():
                for _ in range(n_timing_repeats):
                    for _, _, input_ids in heldout_enc_cache:
                        _ = backbone(input_ids=input_ids, output_hidden_states=False)
            if device.type == "cuda":
                torch.cuda.synchronize()
            bpe_elapsed = max(1e-6, (time.perf_counter() - t_bpe0) / n_timing_repeats)

            bpe_mean_nats_per_tok = bpe_total_nats / max(1, bpe_total_tokens)
            bpe_ppl = round(math.exp(min(20.0, bpe_mean_nats_per_tok)), 4)
            # Normalize BPB over predicted target bytes (excluding the unpredicted first token's bytes or total stream consistently)
            bpe_bpb = round(bpe_total_nats / max(1.0, bpe_total_bytes * math.log(2.0)), 4)
            bpe_tok_per_s = round(bpe_total_tokens / bpe_elapsed, 1)
            bpe_bytes_per_s = round(bpe_total_bytes / bpe_elapsed, 1)

            code_bank = _prepare_bank(code_corpus)
            sec_bank = _prepare_bank(sec_utf8_corpus)
            val_bank = _prepare_bank(val_selection_corpus)
            heldout_bank = _prepare_bank(heldout_eval_corpus)

            # 2. Define Strictly Causal PyTorch Byte-Latent Encoder/Adapter (`d_byte=256` <-> `d_model`) on GPU
            d_byte = 256
            logits_scaling = float(getattr(backbone.config, "logits_scaling", 1.0) or 1.0)
            lm_head_weight_fp32 = backbone.get_output_embeddings().weight.detach().float() / logits_scaling  # [V, d_model]

            def _init_blt_adapter_state() -> Dict[str, torch.Tensor]:
                g = torch.Generator(device=device).manual_seed(seed)
                scale_b = 1.0 / math.sqrt(d_byte)
                scale_m = 1.0 / math.sqrt(d_model)
                return {
                    "byte_embed": torch.randn((256, d_byte), generator=g, device=device) * scale_b,
                    "ngram3_embed": torch.randn((4096, d_byte), generator=g, device=device) * scale_b,
                    "ngram4_embed": torch.randn((4096, d_byte), generator=g, device=device) * scale_b,
                    "byte_to_latent_w": torch.randn((d_model, d_byte), generator=g, device=device) * scale_b,
                    "bos_patch_latent": torch.randn((1, 1, d_model), generator=g, device=device) * scale_m,
                    "latent_causal_proj": torch.eye(d_model, device=device) * 0.20
                    + torch.randn((d_model, d_model), generator=g, device=device) * (0.02 * scale_m),
                    "latent_to_byte_w": torch.randn((d_byte, d_model), generator=g, device=device) * scale_m,
                    "byte_lm_head": torch.randn((256, d_byte), generator=g, device=device) * scale_b,
                    "backbone_lora_down": torch.randn((64, d_model), generator=g, device=device) * scale_m,
                    "backbone_lora_up": torch.zeros((d_model, 64), device=device),
                }

            base_adapter_w = _init_blt_adapter_state()

            def _blt_forward(
                params: Dict[str, torch.Tensor],
                last_h: torch.Tensor,
                b_ids: torch.Tensor,
                seg: Dict[str, Any],
                tok_ends: List[int],
            ) -> Tuple[torch.Tensor, torch.Tensor]:
                """Strictly causal byte-patch + BPE residual forward pass.
                At byte index t (predicting b_{t+1}), uses ONLY bytes <= t and tokens ending <= t+1.
                At token index k (predicting tok_{k+1}), uses ONLY tokens <= k and bytes < tok_ends[k].
                """
                # Causal left-padded 3-gram & 4-gram polynomial hashes (no circular wraparound!)
                padded_b = torch.nn.functional.pad(b_ids, (3, 0), value=0)  # [1, L_b + 3]
                b0 = padded_b[:, 3:]       # b_t
                b1 = padded_b[:, 2:-1]     # b_{t-1}
                b2 = padded_b[:, 1:-2]     # b_{t-2}
                b3 = padded_b[:, 0:-3]     # b_{t-3}
                h3 = (b0 * 257 + b1 * 17 + b2) % 4096
                h4 = (h3 * 31 + b3) % 4096

                hb = (
                    torch.nn.functional.embedding(b0, params["byte_embed"])
                    + 0.35 * torch.nn.functional.embedding(h3, params["ngram3_embed"])
                    + 0.35 * torch.nn.functional.embedding(h4, params["ngram4_embed"])
                )  # [1, L_b, d_byte]
                hu = torch.matmul(hb, params["byte_to_latent_w"].t())  # [1, L_b, d_model]

                spans = [(p["start_byte_offset"], p["end_byte_offset"]) for p in seg["patches"]]
                n_patches = len(spans)

                # Completed patch representations (patch idx uses bytes [s, e); only visible AFTER byte e-1)
                tok_ends_t = torch.tensor(tok_ends, dtype=torch.long, device=device)  # [T_tok]
                p_completed_list: List[torch.Tensor] = []
                intra_causal_chunks: List[torch.Tensor] = []
                for s, e in spans:
                    patch_slice = hu[:, s:e, :]  # [1, plen, d_model]
                    plen = max(1, e - s)
                    # Causal intra-patch prefix mean at each byte offset j in [0, plen): mean(hu[:, s : s+j+1])
                    denom = torch.arange(1, plen + 1, dtype=hu.dtype, device=device).view(1, plen, 1)
                    intra_prefix = torch.cumsum(patch_slice, dim=1) / denom  # [1, plen, d_model]
                    intra_causal_chunks.append(intra_prefix)

                    # Completed patch vector (available ONLY to subsequent patches > idx, where e <= s_next <= t)
                    p_vec = patch_slice.mean(dim=1, keepdim=True)  # [1, 1, d_model]
                    # Condition completed patch only on BPE tokens whose span ended <= e
                    valid_tok_mask = tok_ends_t <= e
                    if bool(valid_tok_mask.any()):
                        causal_tok_ctx = last_h[:, valid_tok_mask, :].mean(dim=1, keepdim=True)
                        p_vec = p_vec + 0.20 * causal_tok_ctx
                    p_completed_list.append(p_vec)

                intra_causal = torch.cat(intra_causal_chunks, dim=1)  # [1, L_b, d_model]

                # Shift completed patches right by 1 (patch 0 sees only learned BOS; patch idx sees 0..idx-1)
                if n_patches > 1:
                    prev_patches = torch.cat([params["bos_patch_latent"], torch.cat(p_completed_list[:-1], dim=1)], dim=1)
                else:
                    prev_patches = params["bos_patch_latent"]  # [1, 1, d_model]

                # Causal lower-triangular prefix mixing across prior patches 0..idx
                tril_weights = torch.tril(torch.ones((n_patches, n_patches), dtype=hu.dtype, device=device))
                tril_weights = tril_weights / torch.clamp(tril_weights.sum(dim=1, keepdim=True), min=1.0)
                mixed_prev_patches = torch.matmul(tril_weights, prev_patches.squeeze(0)).unsqueeze(0)  # [1, n_patches, d_model]
                prior_patch_ctx = mixed_prev_patches + torch.matmul(mixed_prev_patches, params["latent_causal_proj"].t())

                expanded_prior = torch.cat(
                    [prior_patch_ctx[:, idx : idx + 1, :].expand(-1, e - s, -1) for idx, (s, e) in enumerate(spans)],
                    dim=1,
                )  # [1, L_b, d_model]

                causal_latent_at_byte = 0.55 * intra_causal + 0.45 * expanded_prior  # strictly depends on bytes <= t
                hb_dec = hb + torch.matmul(causal_latent_at_byte, params["latent_to_byte_w"].t())
                byte_logits = torch.matmul(hb_dec, params["byte_lm_head"].t())  # [1, L_b, 256]

                # Causal byte-to-token alignment: token k (ending at byte tok_ends[k]) sees ONLY bytes < tok_ends[k]
                byte_prefix_mean = torch.cumsum(causal_latent_at_byte, dim=1) / torch.arange(
                    1, causal_latent_at_byte.shape[1] + 1, dtype=hu.dtype, device=device
                ).view(1, -1, 1)
                gather_idx = torch.clamp(tok_ends_t - 1, min=0, max=causal_latent_at_byte.shape[1] - 1)
                tok_byte_ctx = byte_prefix_mean[:, gather_idx, :]  # [1, T_tok, d_model], strictly causal per token!

                lora_in = last_h + 0.20 * tok_byte_ctx
                lora_delta = torch.matmul(
                    torch.matmul(lora_in, params["backbone_lora_down"].t()),
                    params["backbone_lora_up"].t(),
                )
                refined_h = last_h + lora_delta
                tok_logits = torch.matmul(refined_h[:, :-1, :], lm_head_weight_fp32.t())
                return byte_logits, tok_logits

            def _verify_strict_causality(params: Dict[str, torch.Tensor]) -> Dict[str, Any]:
                """End-to-end perturbation test: mutate future bytes/tokens and re-segment from scratch;
                verify prefix byte_logits and tok_logits remain invariant within fp32 precision."""
                probe_txt = heldout_eval_corpus[0]
                probe_bytes = bytearray(probe_txt.encode("utf-8"))
                t_cut = len(probe_bytes) // 2
                # Create a counterfactual suffix after t_cut while keeping bytes <= t_cut identical
                mut_bytes = bytearray(probe_bytes)
                for idx_b in range(t_cut + 1, len(mut_bytes)):
                    mut_bytes[idx_b] = ord("Z") if (idx_b % 2 == 0) else ord("7")
                mut_txt = mut_bytes.decode("utf-8", errors="replace")

                with torch.no_grad():
                    enc_a = tokenizer(probe_txt, return_tensors="pt").to(device)
                    enc_b = tokenizer(mut_txt, return_tensors="pt").to(device)
                    out_a = backbone(input_ids=enc_a["input_ids"], output_hidden_states=True)
                    out_b = backbone(input_ids=enc_b["input_ids"], output_hidden_states=True)
                    seg_a = cls.segment_blt_entropy_patches(bytes(probe_bytes))
                    seg_b = cls.segment_blt_entropy_patches(bytes(mut_bytes))
                    bt_a = torch.tensor([list(probe_bytes)], dtype=torch.long, device=device)
                    bt_b = torch.tensor([list(mut_bytes)], dtype=torch.long, device=device)
                    te_a = _token_cumulative_end_bytes(probe_txt, enc_a["input_ids"][0])
                    te_b = _token_cumulative_end_bytes(mut_txt, enc_b["input_ids"][0])

                    bl_a, tl_a = _blt_forward(params, out_a.hidden_states[-1].float(), bt_a, seg_a, te_a)
                    bl_b, tl_b = _blt_forward(params, out_b.hidden_states[-1].float(), bt_b, seg_b, te_b)

                    # Compare all byte positions <= t_cut whose token conditioning is also within shared prefix tokens
                    shared_tok = 0
                    ids_a = enc_a["input_ids"][0].tolist()
                    ids_b = enc_b["input_ids"][0].tolist()
                    while (
                        shared_tok < min(len(ids_a), len(ids_b))
                        and ids_a[shared_tok] == ids_b[shared_tok]
                        and te_a[shared_tok] <= t_cut
                    ):
                        shared_tok += 1
                    safe_byte_cut = te_a[shared_tok - 1] if shared_tok > 0 else min(8, t_cut)
                    max_byte_diff = float((bl_a[:, :safe_byte_cut, :] - bl_b[:, :safe_byte_cut, :]).abs().max().item())
                    max_tok_diff = (
                        float((tl_a[:, : max(1, shared_tok - 1), :] - tl_b[:, : max(1, shared_tok - 1), :]).abs().max().item())
                        if shared_tok > 1
                        else 0.0
                    )
                # bf16 SDPA backbone prefix jitter is ~1e-3..1e-2; isolate pure _blt_forward causality with identical prefix hidden states too
                with torch.no_grad():
                    lh_clone = out_a.hidden_states[-1].float().clone()
                    if shared_tok < lh_clone.shape[1]:
                        lh_clone[:, shared_tok:, :] = torch.randn_like(lh_clone[:, shared_tok:, :]) * 50.0
                    bl_pure, tl_pure = _blt_forward(params, lh_clone, bt_b, seg_b, te_a)
                    pure_byte_diff = float((bl_a[:, :safe_byte_cut, :] - bl_pure[:, :safe_byte_cut, :]).abs().max().item())
                    pure_tok_diff = (
                        float((tl_a[:, : max(1, shared_tok - 1), :] - tl_pure[:, : max(1, shared_tok - 1), :]).abs().max().item())
                        if shared_tok > 1
                        else 0.0
                    )
                verified = (pure_byte_diff < 1e-4) and (pure_tok_diff < 1e-4)
                if not verified:
                    raise RuntimeError(
                        f"Causal leakage detected in _blt_forward: pure_byte_diff={pure_byte_diff}, pure_tok_diff={pure_tok_diff}"
                    )
                return {
                    "causality_verified": True,
                    "pure_adapter_prefix_max_abs_diff_bytes": round(pure_byte_diff, 8),
                    "pure_adapter_prefix_max_abs_diff_tokens": round(pure_tok_diff, 8),
                    "end_to_end_bf16_prefix_max_abs_diff_bytes": round(max_byte_diff, 6),
                    "tested_prefix_bytes": int(safe_byte_cut),
                    "tested_prefix_tokens": int(max(1, shared_tok - 1)),
                }

            def _eval_adapter(params: Dict[str, torch.Tensor], bank, measure_end_to_end: bool = False) -> Dict[str, float]:
                # Warm up adapter kernels once before timing
                with torch.no_grad():
                    _, lh0, bt0, sg0, _, te0 = bank[0]
                    _ = _blt_forward(params, lh0, bt0, sg0, te0)
                if device.type == "cuda":
                    torch.cuda.synchronize()

                tot_byte_nats = 0.0
                tot_tok_nats = 0.0
                tot_pred_bytes = 0
                tot_stream_bytes = 0
                tot_patches = 0
                tot_tokens = 0
                with torch.no_grad():
                    for _, lh, bt, sg, i_ids, te in bank:
                        b_logits, t_logits = _blt_forward(params, lh, bt, sg, te)
                        b_targets = bt[:, 1:].reshape(-1)
                        b_nats = torch.nn.functional.cross_entropy(
                            b_logits[:, :-1, :].reshape(-1, 256), b_targets, reduction="sum"
                        ).item()
                        t_targets = i_ids[:, 1:].reshape(-1)
                        t_nats = torch.nn.functional.cross_entropy(
                            t_logits.reshape(-1, vocab_size), t_targets, reduction="sum"
                        ).item()
                        tot_byte_nats += b_nats
                        tot_tok_nats += t_nats
                        tot_pred_bytes += int(bt.shape[1] - 1)
                        tot_stream_bytes += int(bt.shape[1])
                        tot_patches += int(sg["total_patches"])
                        tot_tokens += int(i_ids.shape[1] - 1)

                # Adapter-head-only throughput (averaged over n_timing_repeats on cached backbone states)
                if device.type == "cuda":
                    torch.cuda.synchronize()
                t0_head = time.perf_counter()
                with torch.no_grad():
                    for _ in range(n_timing_repeats):
                        for _, lh, bt, sg, _, te in bank:
                            _ = _blt_forward(params, lh, bt, sg, te)
                if device.type == "cuda":
                    torch.cuda.synchronize()
                dt_head = max(1e-6, (time.perf_counter() - t0_head) / n_timing_repeats)

                # Honest End-to-End throughput (full backbone forward + segmentation + _blt_forward)
                e2e_bytes_per_s = 0.0
                if measure_end_to_end:
                    if device.type == "cuda":
                        torch.cuda.synchronize()
                    t0_e2e = time.perf_counter()
                    with torch.no_grad():
                        for _ in range(n_timing_repeats):
                            for txt, _, bt, _, i_ids, te in bank:
                                out = backbone(input_ids=i_ids, output_hidden_states=True)
                                sg_live = cls.segment_blt_entropy_patches(txt)
                                _ = _blt_forward(params, out.hidden_states[-1].float(), bt, sg_live, te)
                    if device.type == "cuda":
                        torch.cuda.synchronize()
                    dt_e2e = max(1e-6, (time.perf_counter() - t0_e2e) / n_timing_repeats)
                    e2e_bytes_per_s = round(tot_stream_bytes / dt_e2e, 1)

                blt_bpb = round(tot_byte_nats / max(1.0, tot_pred_bytes * math.log(2.0)), 4)
                hybrid_tok_bpb = round(tot_tok_nats / max(1.0, tot_stream_bytes * math.log(2.0)), 4)
                hybrid_ppl = round(math.exp(min(20.0, tot_tok_nats / max(1, tot_tokens))), 4)
                comp_ratio = round(tot_stream_bytes / max(1, tot_patches), 3)
                eff_patch_per_s = round(tot_patches / dt_head, 1)
                head_byte_per_s = round(tot_stream_bytes / dt_head, 1)
                res_metrics = {
                    "blt_byte_bpb": blt_bpb,
                    "hybrid_backbone_bpb": hybrid_tok_bpb,
                    "hybrid_token_perplexity": hybrid_ppl,
                    "patch_compression_ratio": comp_ratio,
                    "blt_adapter_head_patches_per_sec": eff_patch_per_s,
                    "blt_adapter_head_bytes_per_sec": head_byte_per_s,
                    # Preserve legacy key names mapped to honest end-to-end throughput when measured
                    "blt_patches_per_sec": eff_patch_per_s,
                    "blt_bytes_per_sec": e2e_bytes_per_s if measure_end_to_end else head_byte_per_s,
                }
                if measure_end_to_end:
                    res_metrics["hybrid_end_to_end_bytes_per_sec"] = e2e_bytes_per_s
                return res_metrics

            pre_eval = _eval_adapter(base_adapter_w, heldout_bank, measure_end_to_end=True)

            def _train_domain_adapter(domain_bank) -> Dict[str, torch.Tensor]:
                """Train strictly on domain_bank ONLY (never touching val_bank or heldout_bank).
                Uses a conservative step size on backbone_lora_* so few-shot domain slices do not
                overfit the pretrained BPE LM head."""
                trainable = {k: v.detach().clone().requires_grad_(True) for k, v in base_adapter_w.items()}
                byte_head_params = [v for k, v in trainable.items() if not k.startswith("backbone_lora_")]
                lora_params = [v for k, v in trainable.items() if k.startswith("backbone_lora_")]
                opt = torch.optim.AdamW(
                    [
                        {"params": byte_head_params, "lr": lr, "weight_decay": 1e-2},
                        {"params": lora_params, "lr": lr * 0.02, "weight_decay": 5e-2},
                    ]
                )
                for _ in range(steps_per_domain):
                    opt.zero_grad()
                    step_loss = torch.tensor(0.0, device=device)
                    for _, lh, bt, sg, i_ids, te in domain_bank:
                        b_logits, t_logits = _blt_forward(trainable, lh, bt, sg, te)
                        loss_b = torch.nn.functional.cross_entropy(
                            b_logits[:, :-1, :].reshape(-1, 256), bt[:, 1:].reshape(-1)
                        )
                        loss_t = torch.nn.functional.cross_entropy(
                            t_logits.reshape(-1, vocab_size), i_ids[:, 1:].reshape(-1)
                        )
                        step_loss = step_loss + loss_b + 0.5 * loss_t
                    (step_loss / max(1, len(domain_bank))).backward()
                    torch.nn.utils.clip_grad_norm_(list(trainable.values()), max_norm=1.0)
                    opt.step()
                return {k: v.detach().clone() for k, v in trainable.items()}

            # 3. Train Domain Adapters strictly on disjoint domain corpora (zero eval leakage)
            t_train0 = time.perf_counter()
            code_ft_w = _train_domain_adapter(code_bank)
            sec_ft_w = _train_domain_adapter(sec_bank)
            if device.type == "cuda":
                torch.cuda.synchronize()
            train_elapsed_sec = round(time.perf_counter() - t_train0, 3)

            # 4. Merge Domain Task Vectors via WeightSpaceToolkit & select ONLY on disjoint val_bank
            tv_code = cls.compute_task_vector(base_adapter_w, code_ft_w)
            tv_sec = cls.compute_task_vector(base_adapter_w, sec_ft_w)
            ties_merged_w = cls.ties_merge(base_adapter_w, [tv_code, tv_sec], weights=[0.55, 0.45], top_k_ratio=0.45)
            dare_merged_w = cls.dare_merge(base_adapter_w, [tv_code, tv_sec], weights=[0.55, 0.45], drop_rate=0.20)
            slerp_merged_w = {
                k: cls.slerp(0.50, ties_merged_w[k], sec_ft_w[k])
                for k in base_adapter_w
            }

            candidates_map = {
                "ties": ties_merged_w,
                "dare": dare_merged_w,
                "slerp_ties_sec": slerp_merged_w,
            }
            val_scores = {
                name: _eval_adapter(w_cand, val_bank, measure_end_to_end=False)
                for name, w_cand in candidates_map.items()
            }
            selected_merge_name = min(
                val_scores,
                key=lambda n: val_scores[n]["hybrid_backbone_bpb"] + 0.25 * val_scores[n]["blt_byte_bpb"],
            )
            best_merged_w = candidates_map[selected_merge_name]

            # Verify strict causality on the trained + merged adapter before touching heldout_bank
            causality_proof = _verify_strict_causality(best_merged_w)

            # Single blind evaluation on strictly disjoint heldout_bank
            best_eval = _eval_adapter(best_merged_w, heldout_bank, measure_end_to_end=True)

            # Diagnostics on top transformer layer o_proj with merged LoRA delta
            top_layer = backbone.model.layers[-1]
            orig_o_proj = top_layer.self_attn.o_proj.weight.detach().float()
            lora_delta_mat = torch.matmul(best_merged_w["backbone_lora_up"], best_merged_w["backbone_lora_down"])
            merged_o_proj = orig_o_proj + 0.05 * lora_delta_mat[: orig_o_proj.shape[0], : orig_o_proj.shape[1]]
            top_layer_diag = cls.representation_diagnostics(merged_o_proj)

            cos_diag = cls.layer_cosine_diagnostics(base_adapter_w, best_merged_w)
            frob_diag = cls.frobenius_distance(ties_merged_w, dare_merged_w)

            # Save merged checkpoint artifact
            safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", model_label)
            artifact_file = out_dir / f"blt_wsp_merged_{safe_name}.pt"
            torch.save(
                {
                    "model_label": model_label,
                    "d_model": d_model,
                    "d_byte": d_byte,
                    "selected_merge_method": selected_merge_name,
                    "causality_proof": causality_proof,
                    "merged_blt_adapter_state_dict": {k: v.cpu() for k, v in best_merged_w.items()},
                    "top_layer_o_proj_delta": (0.05 * lora_delta_mat).cpu().to(torch.float16),
                    "validation_scores": val_scores,
                    "heldout_metrics": best_eval,
                },
                str(artifact_file),
            )

            del backbone
            if device.type == "cuda":
                torch.cuda.empty_cache()

            model_reports.append({
                "model": model_label,
                "device": f"{device} ({gpu_name})",
                "d_model": d_model,
                "vocab_size": vocab_size,
                "train_elapsed_sec": train_elapsed_sec,
                "selected_merge_method": selected_merge_name,
                "saved_checkpoint": str(artifact_file),
                "leakage_and_causality_audit": {
                    "train_eval_disjoint": True,
                    "val_eval_disjoint": True,
                    "merge_selected_on": "val_selection_corpus",
                    "code_train_examples": len(code_corpus),
                    "sec_train_examples": len(sec_utf8_corpus),
                    "val_examples": len(val_selection_corpus),
                    "heldout_examples": len(heldout_eval_corpus),
                    **causality_proof,
                },
                "baseline_standard_bpe": {
                    "bpb": bpe_bpb,
                    "token_perplexity": bpe_ppl,
                    "tokens_per_sec": bpe_tok_per_s,
                    "bytes_per_sec": bpe_bytes_per_s,
                },
                "blt_adapter_untrained_init": pre_eval,
                "validation_merge_candidates": val_scores,
                "blt_wsp_merged_checkpoint": best_eval,
                "improvements": {
                    "backbone_bpb_delta": round(best_eval["hybrid_backbone_bpb"] - bpe_bpb, 4),
                    "backbone_bpb_improvement_pct": round(
                        (1.0 - best_eval["hybrid_backbone_bpb"] / max(1e-6, bpe_bpb)) * 100.0, 2
                    ),
                    "backbone_perplexity_delta": round(best_eval["hybrid_token_perplexity"] - bpe_ppl, 4),
                    "backbone_perplexity_improvement_pct": round(
                        (1.0 - best_eval["hybrid_token_perplexity"] / max(1e-6, bpe_ppl)) * 100.0, 2
                    ),
                    "blt_byte_bpb_reduction_pct": round(
                        (1.0 - best_eval["blt_byte_bpb"] / max(1e-6, pre_eval["blt_byte_bpb"])) * 100.0, 2
                    ),
                    "end_to_end_throughput_ratio_x": round(
                        best_eval["hybrid_end_to_end_bytes_per_sec"] / max(1.0, bpe_bytes_per_s), 3
                    ),
                    "adapter_head_only_throughput_ratio_x": round(
                        best_eval["blt_adapter_head_bytes_per_sec"] / max(1.0, bpe_bytes_per_s), 2
                    ),
                    "throughput_speedup_bytes_per_sec_x": round(
                        best_eval["hybrid_end_to_end_bytes_per_sec"] / max(1.0, bpe_bytes_per_s), 3
                    ),
                },
                "wsp_diagnostics": {
                    "top_layer_spectral": top_layer_diag,
                    "mean_adapter_cosine_sim": round(float(np.mean(list(cos_diag.values()))), 4),
                    "ties_vs_dare_frobenius_mean": round(float(np.mean(list(frob_diag.values()))), 4),
                },
            })

        return {
            "gpu_device": gpu_name,
            "cuda_enabled": device.type == "cuda",
            "models_trained_and_merged": len(model_reports),
            "reports": model_reports,
        }


# -----------------------------------------------------------------------------
# Vectorized Monte Carlo & Numerical Simulation Toolkit
# -----------------------------------------------------------------------------

class MonteCarloToolkit:
    """High-throughput vectorized stochastic algorithms and numerical simulation."""

    @staticmethod
    def simulate_gbm(
        s0: float,
        mu: float,
        sigma: float,
        t_years: float,
        n_steps: int,
        n_paths: int,
        seed: Optional[int] = None,
    ) -> np.ndarray:
        """Simulate Geometric Brownian Motion paths vectorized across all paths."""
        if seed is not None:
            np.random.seed(seed)

        dt = t_years / max(1, n_steps)
        # Vectorized Gaussian random shocks [n_steps, n_paths]
        z = np.random.standard_normal((n_steps, n_paths))
        drift = (mu - 0.5 * sigma ** 2) * dt
        vol = sigma * math.sqrt(dt)

        # Log returns cumsum
        log_returns = drift + vol * z
        log_paths = np.vstack([np.zeros((1, n_paths)), np.cumsum(log_returns, axis=0)])
        paths = s0 * np.exp(log_paths)
        return paths

    @staticmethod
    def calculate_distribution_stats(
        paths: np.ndarray,
        alpha: float = 0.05,
    ) -> Dict[str, float]:
        """Calculate terminal statistics, Value at Risk (VaR), and Expected Shortfall."""
        terminal_prices = paths[-1, :]
        initial_price = paths[0, 0]
        returns = (terminal_prices - initial_price) / initial_price

        # Sort returns for empirical quantile evaluation
        sorted_returns = np.sort(returns)
        var_index = int(alpha * len(sorted_returns))
        var = -sorted_returns[var_index]
        cvar = -np.mean(sorted_returns[:var_index]) if var_index > 0 else var

        # Max drawdown across paths
        peak = np.maximum.accumulate(paths, axis=0)
        drawdown = (paths - peak) / peak
        max_dd = -np.min(drawdown)

        return {
            "initial_price": round(float(initial_price), 2),
            "terminal_mean": round(float(np.mean(terminal_prices)), 2),
            "terminal_std": round(float(np.std(terminal_prices)), 2),
            "expected_return_pct": round(float(np.mean(returns) * 100.0), 2),
            "value_at_risk_5pct": round(float(var * 100.0), 2),
            "expected_shortfall_5pct": round(float(cvar * 100.0), 2),
            "max_drawdown_pct": round(float(max_dd * 100.0), 2),
            "num_paths": int(paths.shape[1]),
            "num_steps": int(paths.shape[0]),
        }



# -----------------------------------------------------------------------------
# Data Science & Statistical Decomposition Toolkit
# -----------------------------------------------------------------------------

class DataScienceToolkit:
    """Algorithmic data science and statistical decomposition primitives."""

    @staticmethod
    def pca_decomposition(matrix: np.ndarray, n_components: int = 4) -> Dict[str, Any]:
        """Perform Principal Component Analysis via covariance eigen-decomposition."""
        x_centered = matrix - np.mean(matrix, axis=0)
        cov = np.cov(x_centered, rowvar=False)
        eigenvalues, eigenvectors = np.linalg.eigh(cov)
        idx = np.argsort(eigenvalues)[::-1]
        sorted_evals = eigenvalues[idx]
        sorted_evecs = eigenvectors[:, idx]

        total_var = max(1e-8, float(np.sum(sorted_evals)))
        explained_ratio = sorted_evals / total_var

        top_k = min(n_components, len(sorted_evals))
        return {
            "eigenvalues": [round(float(v), 6) for v in sorted_evals[:top_k]],
            "explained_variance_ratio": [round(float(r), 6) for r in explained_ratio[:top_k]],
            "cumulative_variance_pct": round(float(np.sum(explained_ratio[:top_k])) * 100.0, 2),
            "top_eigenvalue": round(float(sorted_evals[0]), 6),
            "condition_number": round(float(np.linalg.cond(cov)), 2),
            "eigenvectors": sorted_evecs[:, :top_k].tolist(),
        }

    @staticmethod
    def correlation_matrix(matrix: np.ndarray) -> np.ndarray:
        """Compute Pearson correlation matrix."""
        return np.corrcoef(matrix, rowvar=False)


# -----------------------------------------------------------------------------
# Compute Research Runner & Placement Router
# -----------------------------------------------------------------------------

class ComputeResearchRunner:
    """Manages placement, execution, and SQLite recording of scientific jobs."""

    def __init__(self, db_path: Path = DEFAULT_DB):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(self.db_path)) as conn:
            ensure_research_experiments_schema(conn)

    def route_placement(self, spec: ScientificJobSpec) -> str:
        """Determine optimal compute hardware based on requirements."""
        if spec.runtime_preference != "auto":
            return spec.runtime_preference

        # Hardware mapping rules
        if spec.job_type in ("weight_space_math", "sft_tuning") and torch.cuda.is_available():
            return "local_gpu"
        if spec.hyperparameters.get("requires_cloud_batch"):
            return "cloud_serverless"
        return "local_cpu"

    def execute_job(self, spec: ScientificJobSpec) -> Dict[str, Any]:
        """Execute scientific job, enforce invariants, and persist telemetry."""
        runtime = self.route_placement(spec)
        fingerprint = spec.compute_fingerprint()
        t0 = time.time()
        error_msg = ""
        metrics: Dict[str, Any] = {}
        status = "failed"

        try:
            if callable(spec.code_entry):
                # Execute Python callable directly
                metrics = spec.code_entry(**spec.hyperparameters)
                status = "completed"
            elif spec.job_type == "weight_space_math":
                # Built-in WSP execution
                base_w = spec.hyperparameters.get("base_weights", {})
                task_vecs = spec.hyperparameters.get("task_vectors", [])
                algorithm = spec.hyperparameters.get("algorithm", "ties")
                weights = spec.hyperparameters.get("weights")

                if algorithm == "ties":
                    merged = WeightSpaceToolkit.ties_merge(base_w, task_vecs, weights=weights)
                elif algorithm == "dare":
                    merged = WeightSpaceToolkit.dare_merge(base_w, task_vecs, weights=weights)
                else:
                    raise ValueError(f"Unknown merge algorithm: {algorithm}")

                metrics = {
                    "merged_layers_count": len(merged),
                    "layer_diagnostics": WeightSpaceToolkit.layer_cosine_diagnostics(base_w, merged),
                    "frobenius_distance": WeightSpaceToolkit.frobenius_distance(base_w, merged),
                }
                status = "completed"

            elif spec.job_type == "monte_carlo":
                # Built-in Monte Carlo execution
                s0 = spec.hyperparameters.get("s0", 100.0)
                mu = spec.hyperparameters.get("mu", 0.05)
                sigma = spec.hyperparameters.get("sigma", 0.20)
                t_years = spec.hyperparameters.get("t_years", 1.0)
                n_steps = spec.hyperparameters.get("n_steps", 252)
                n_paths = spec.hyperparameters.get("n_paths", 10000)

                paths = MonteCarloToolkit.simulate_gbm(s0, mu, sigma, t_years, n_steps, n_paths)
                metrics = MonteCarloToolkit.calculate_distribution_stats(paths)
                status = "completed"

            elif spec.job_type == "blt_byte_latent":
                sample_texts = spec.hyperparameters.get("sample_texts")
                seed = int(spec.hyperparameters.get("seed", 42))
                metrics = WeightSpaceToolkit.run_blt_wsp_experiment(sample_texts=sample_texts, seed=seed)
                status = "completed"

            elif spec.job_type == "blt_gpu_adapter_train":
                ckpt_paths = spec.hyperparameters.get("checkpoint_paths")
                steps = int(spec.hyperparameters.get("steps_per_domain", 50))
                lr = float(spec.hyperparameters.get("lr", 3e-3))
                seed = int(spec.hyperparameters.get("seed", 42))
                metrics = WeightSpaceToolkit.train_and_merge_blt_gpu_adapter(
                    checkpoint_paths=ckpt_paths,
                    steps_per_domain=steps,
                    lr=lr,
                    seed=seed,
                )
                status = "completed"

            else:
                raise ValueError(f"No executable entry provided for job type: {spec.job_type}")

            # Verify criteria if specified
            for k, expected_v in spec.verification_criteria.items():
                actual_v = metrics.get(k)
                if actual_v is not None and isinstance(expected_v, (int, float)) and isinstance(actual_v, (int, float)):
                    if k.endswith("_max") and actual_v > expected_v:
                        status = "criteria_failed"
                        error_msg = f"Metric {k} ({actual_v}) exceeded max threshold ({expected_v})"
                        break
                    elif k.endswith("_min") and actual_v < expected_v:
                        status = "criteria_failed"
                        error_msg = f"Metric {k} ({actual_v}) fell below min threshold ({expected_v})"
                        break

        except Exception as ex:
            LOGGER.error("Job %s execution failed: %s", spec.job_id, ex, exc_info=True)
            error_msg = str(ex)
            status = "failed"

        latency_ms = (time.time() - t0) * 1000.0

        # Persist experiment record to SQLite
        now_ts = time.time()
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO assistant_research_experiments (
                    experiment_id, timestamp, project, job_type, code_fingerprint,
                    runtime_host, hyperparameters_json, metrics_json, artifact_paths_json,
                    status, latency_ms, error_message, created
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    spec.job_id,
                    now_ts,
                    spec.project,
                    spec.job_type,
                    fingerprint,
                    runtime,
                    json.dumps(spec.hyperparameters, default=str),
                    json.dumps(metrics, default=str),
                    json.dumps(spec.artifact_outputs),
                    status,
                    latency_ms,
                    error_msg,
                    now_ts,
                ),
            )
            conn.commit()

        return {
            "ok": status == "completed",
            "job_id": spec.job_id,
            "project": spec.project,
            "job_type": spec.job_type,
            "runtime_used": runtime,
            "latency_ms": round(latency_ms, 2),
            "status": status,
            "metrics": metrics,
            "error": error_msg,
            "artifact_paths": spec.artifact_outputs,
        }

    def get_experiment(self, experiment_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve an experiment record by ID."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM assistant_research_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if not row:
                return None
            res = dict(row)
            res["hyperparameters"] = json.loads(res.get("hyperparameters_json") or "{}")
            res["metrics"] = json.loads(res.get("metrics_json") or "{}")
            res["artifacts"] = json.loads(res.get("artifact_paths_json") or "[]")
            return res

    def list_experiments(self, project: Optional[str] = None, limit: int = 10) -> List[Dict[str, Any]]:
        """List recent experiment records."""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            if project:
                cur = conn.execute(
                    "SELECT * FROM assistant_research_experiments WHERE project = ? ORDER BY timestamp DESC LIMIT ?",
                    (project, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM assistant_research_experiments ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
            results = []
            for r in cur.fetchall():
                d = dict(r)
                d["metrics"] = json.loads(d.get("metrics_json") or "{}")
                results.append(d)
            return results


# Global singleton runner
RESEARCH_ENGINE = ComputeResearchRunner()
