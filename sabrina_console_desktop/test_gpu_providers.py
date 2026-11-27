"""Tests for expanded Cloud/GPU Pod providers, Vault config, and Swarm GPU Deployment Framework."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sabrina_console_desktop.sabrina_desktop_ui import SabrinaDesktopBridge
from swarm_command.role_binding import infer_model_provider
from swarm_console.gpu_providers import GpuProviderRegistry, SwarmGpuDeploymentManager
from swarm_console.registry import ModelSkillRegistry
from swarm_console.vault import ProviderKeyVault


class GpuProvidersAndVaultTests(unittest.TestCase):
    def test_vault_supports_cloud_and_gpu_pod_providers_with_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault_path = Path(tmp) / "vault.json"

            def fake_fetch(url: str, headers=None, timeout=6.0):
                if "runpod.io" in url:
                    return [{"id": "pod-abc123", "name": "Sabrina vLLM Pod"}]
                if "lambdalabs.com" in url:
                    return {"data": {"gpu_1x_h100_pcie": {"instance_type": {"description": "1x H100 PCIe"}}}}
                return {"data": [{"id": "qwen2.5-coder-14b"}]}

            vault = ProviderKeyVault(vault_path=vault_path, fetch_json=fake_fetch)
            listed = vault.list_providers()
            by_id = {p["provider"]: p for p in listed["providers"]}

            for pid in ("google_vertex", "aws_bedrock"):
                self.assertIn(pid, by_id)
                self.assertEqual(by_id[pid]["category"], "cloud_platform")
                self.assertTrue(by_id[pid]["config_fields"])

            for pid in ("runpod", "vastai", "lambda_labs", "modal", "coreweave", "custom_gpu"):
                self.assertIn(pid, by_id)
                self.assertEqual(by_id[pid]["category"], "gpu_pod")
                self.assertTrue(by_id[pid]["config_fields"])

            # Save DPAPI key + non-secret config atomically
            status = vault.set_key(
                "runpod",
                "rp_secret_key_1234567890",
                config={"gpu_type": "NVIDIA H100 80GB", "cloud_type": "SECURE"},
            )
            self.assertTrue(status["unlocked"])
            self.assertEqual(status["config"]["gpu_type"], "NVIDIA H100 80GB")
            # Raw secret must never be stored in plaintext
            raw_file = vault_path.read_text(encoding="utf-8")
            self.assertNotIn("rp_secret_key_1234567890", raw_file)

            # Live probe normalization for RunPod
            probe = vault.probe_provider("runpod")
            self.assertTrue(probe["ok"], probe)
            self.assertEqual(probe["unlocked_count"], 1)
            self.assertEqual(probe["models"][0]["id"], "runpod/pod-abc123")

            # Aliases resolve cleanly
            vault.set_provider_config("vertex", {"project_id": "sabrina-prod", "region": "us-west1"})
            self.assertEqual(vault.get_provider_config("google_vertex")["project_id"], "sabrina-prod")
            vault.set_provider_config("aws", {"region": "us-west-2"})
            self.assertEqual(vault.get_provider_config("aws_bedrock")["region"], "us-west-2")

    def test_infer_model_provider_for_cloud_and_gpu_prefixes(self) -> None:
        self.assertEqual(infer_model_provider("google_vertex/gemini-4-neon"), "google_vertex")
        self.assertEqual(infer_model_provider("vertex/llama-3.3-70b"), "google_vertex")
        self.assertEqual(infer_model_provider("aws_bedrock/anthropic.claude-3-7"), "aws_bedrock")
        self.assertEqual(infer_model_provider("bedrock/meta.llama3-70b"), "aws_bedrock")
        self.assertEqual(infer_model_provider("runpod/qwen2.5-coder-14b"), "runpod")
        self.assertEqual(infer_model_provider("vastai/deepseek-r1"), "vastai")
        self.assertEqual(infer_model_provider("lambda_labs/llama-3.1-70b"), "lambda_labs")
        self.assertEqual(infer_model_provider("modal/vllm-worker"), "modal")
        self.assertEqual(infer_model_provider("coreweave/h100-swarm"), "coreweave")
        self.assertEqual(infer_model_provider("custom_gpu/local-vllm"), "custom_gpu")

    def test_gpu_swarm_deployment_lifecycle_and_fleet_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = ProviderKeyVault(vault_path=root / "vault.json")
            registry = ModelSkillRegistry(
                vault=vault,
                custom_models_path=root / "custom_models.json",
            )
            http_calls: list[tuple[str, str]] = []

            def fake_http(url: str, *, method: str = "GET", headers=None, payload=None, timeout: float = 15.0):
                http_calls.append((method, url))
                if method == "POST" and "runpod.io/v1/pods" in url:
                    return {"ok": True, "status": 200, "data": {"id": "rp_live_999"}}
                if method == "GET" and "rp_live_999" in url:
                    return {"ok": True, "status": 200, "data": {"desiredStatus": "RUNNING"}}
                if method == "DELETE" and "rp_live_999" in url:
                    return {"ok": True, "status": 200, "data": {}}
                return {"ok": True, "status": 200, "data": {}}

            mgr = SwarmGpuDeploymentManager(
                vault=vault,
                deployments_path=root / "gpu_deployments.json",
                model_registry=registry,
                provider_registry=GpuProviderRegistry(),
                http_fn=fake_http,
            )

            # All 8 adapters dry-run cleanly even without API keys
            for pid in ("runpod", "vastai", "lambda_labs", "google_vertex", "aws_bedrock", "modal", "coreweave", "custom_gpu"):
                dry = mgr.deploy_swarm({
                    "provider": pid,
                    "swarm_name": f"dry-{pid}",
                    "model_id": "Qwen/Qwen2.5-Coder-14B-Instruct",
                    "worker_count": 2,
                    "dry_run": True,
                })
                self.assertTrue(dry["ok"], f"{pid} dry-run failed: {dry}")
                self.assertEqual(dry["deployment"]["status"], "dry_run_ready")
                self.assertIn("endpoint", dry["spec"])

            # Live deploy fails cleanly when provider is locked
            locked_res = mgr.deploy_swarm({
                "provider": "runpod",
                "swarm_name": "locked-swarm",
                "model_id": "Qwen/Qwen2.5-Coder-14B-Instruct",
                "dry_run": False,
            })
            self.assertFalse(locked_res["ok"])
            self.assertIn("locked", locked_res["error"].lower())

            # Unlock RunPod and deploy live
            vault.set_key("runpod", "rp_live_test_key_abcdef")
            live = mgr.deploy_swarm({
                "provider": "runpod",
                "swarm_name": "alpha-gpu-swarm",
                "model_id": "Qwen/Qwen2.5-Coder-14B-Instruct",
                "gpu_type": "NVIDIA A100 80GB",
                "worker_count": 2,
                "runtime_mode": "vllm",
                "auto_register_model": True,
                "dry_run": False,
            })
            self.assertTrue(live["ok"], live)
            dep = live["deployment"]
            self.assertEqual(dep["status"], "provisioning")
            self.assertEqual(dep["provider_resource_id"], "rp_live_999")
            self.assertIn("rp_live_999-8000.proxy.runpod.net/v1", dep["inference_endpoint"])
            self.assertEqual(dep["registered_model_id"], "runpod/Qwen/Qwen2.5-Coder-14B-Instruct")

            # Endpoint auto-propagated to vault provider config for inference routing
            self.assertEqual(vault.get_provider_config("runpod")["endpoint_url"], dep["inference_endpoint"])

            # Check status transitions to running
            st = mgr.check_status(dep["deployment_id"])
            self.assertTrue(st["ok"])
            self.assertEqual(st["deployment"]["status"], "running")

            # Terminate transitions to terminated
            term = mgr.terminate_deployment(dep["deployment_id"])
            self.assertTrue(term["ok"])
            self.assertEqual(term["deployment"]["status"], "terminated")

            # Verify no raw secret leaked into persisted deployment JSON
            persisted = (root / "gpu_deployments.json").read_text(encoding="utf-8")
            self.assertNotIn("rp_live_test_key_abcdef", persisted)

    def test_desktop_bridge_and_ui_wiring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bridge = SabrinaDesktopBridge(
                role_bindings_path=root / "roles.json",
                vault_path=root / "vault.json",
                swarm_artifacts_dir=root / "missions",
                step_delay_sec=0,
            )
            try:
                gpu_info = bridge.get_gpu_providers()
                self.assertTrue(gpu_info["ok"])
                provider_ids = {p["provider_id"] for p in gpu_info["providers"]}
                self.assertTrue({"runpod", "vastai", "lambda_labs", "google_vertex", "aws_bedrock", "modal", "coreweave", "custom_gpu"}.issubset(provider_ids))

                cfg_res = bridge.set_vault_provider_config("google_vertex", {"project_id": "my-proj", "region": "us-central1"})
                self.assertTrue(cfg_res["ok"])
                self.assertEqual(bridge.vault.get_provider_config("google_vertex")["project_id"], "my-proj")

                dry = bridge.deploy_swarm_to_gpu({
                    "provider": "aws_bedrock",
                    "swarm_name": "aws-preview",
                    "model_id": "anthropic.claude-3-7-sonnet",
                    "dry_run": True,
                })
                self.assertTrue(dry["ok"], dry)
                self.assertTrue(any(d["swarm_name"] == "aws-preview" for d in dry["deployments"]))
            finally:
                bridge.swarm_runner.shutdown()

        # Verify UI HTML includes the new Settings subtab, category filters, and provider options
        html_path = Path(__file__).resolve().parent / "sabrina_desktop_ui.html"
        html = html_path.read_text(encoding="utf-8")
        for marker in (
            'data-subtab="gpu-deploy"',
            'id="subview-gpu-deploy"',
            'id="vault-category-tabs"',
            'id="vault-config-fields"',
            'value="google_vertex"',
            'value="aws_bedrock"',
            'value="runpod"',
            'value="vastai"',
            'value="lambda_labs"',
            'value="modal"',
            'value="coreweave"',
            'value="custom_gpu"',
            'id="btn-gpu-dryrun"',
            'id="btn-gpu-deploy"',
        ):
            self.assertIn(marker, html, f"Missing UI marker: {marker}")


if __name__ == "__main__":
    unittest.main()
