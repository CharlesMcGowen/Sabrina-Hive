"""GPU Provider & Swarm Deployment Integration Framework for Sabrina Console.

Provides a unified adapter protocol, provider registry, and persistent deployment
manager connecting cloud platforms (Google Cloud / Vertex AI, Amazon AWS / Bedrock)
and GPU pod providers (RunPod, Vast.ai, Lambda Cloud, Modal, CoreWeave, Custom GPU)
to Sabrina's Model Fleet and Swarm orchestration engine.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .protocol import guard_safe_path
from .vault import PROVIDER_METADATA, ProviderKeyVault


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _default_http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    req_headers = {
        "User-Agent": "sabrina-gpu-swarm-deployer/1.0",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    data_bytes = None
    if payload is not None:
        data_bytes = json.dumps(payload).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = Request(url, data=data_bytes, headers=req_headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {"text": raw}
            return {"ok": True, "status": resp.status, "data": parsed}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "error": body or str(exc)}
    except (URLError, OSError, ValueError) as exc:
        return {"ok": False, "status": 0, "error": str(exc)}


DEFAULT_RUNTIME_IMAGES: dict[str, str] = {
    "vllm": "vllm/vllm-openai:latest",
    "ollama": "ollama/ollama:latest",
    "sabrina_worker": "ghcr.io/sabrina-console/swarm-worker-gpu:latest",
}


@dataclass(frozen=True)
class GpuOffer:
    """Normalized GPU hardware SKU available on a cloud or GPU pod provider."""

    gpu_type: str
    vram_gb: int
    price_per_hr: float
    region: str = "global"
    cloud_type: str = "on-demand"
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SwarmDeployRequest:
    """Normalized request to deploy a swarm inference or worker pool on a GPU provider."""

    provider: str
    swarm_name: str
    model_id: str
    gpu_type: str = "NVIDIA RTX 4090"
    worker_count: int = 1
    runtime_mode: str = "vllm"  # "vllm" | "ollama" | "sabrina_worker"
    container_image: str = ""
    region: str = ""
    disk_gb: int = 50
    role_assignments: dict[str, str] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)
    auto_register_model: bool = True
    dry_run: bool = False

    def __post_init__(self) -> None:
        self.provider = str(self.provider or "").strip().lower()
        self.swarm_name = str(self.swarm_name or "").strip() or f"sabrina-swarm-{uuid.uuid4().hex[:6]}"
        self.model_id = str(self.model_id or "").strip()
        self.gpu_type = str(self.gpu_type or "").strip() or "NVIDIA RTX 4090"
        self.worker_count = max(1, min(64, int(self.worker_count or 1)))
        self.runtime_mode = str(self.runtime_mode or "vllm").strip().lower()
        if self.runtime_mode not in DEFAULT_RUNTIME_IMAGES:
            self.runtime_mode = "vllm"
        if not str(self.container_image or "").strip():
            self.container_image = DEFAULT_RUNTIME_IMAGES[self.runtime_mode]
        self.disk_gb = max(10, min(2000, int(self.disk_gb or 50)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SwarmDeploymentRecord:
    """Persisted record of a GPU swarm deployment."""

    deployment_id: str
    provider: str
    swarm_name: str
    status: str  # "dry_run_ready" | "provisioning" | "running" | "terminated" | "failed"
    worker_count: int
    gpu_type: str
    model_id: str
    runtime_mode: str
    container_image: str
    region: str
    inference_endpoint: str = ""
    estimated_hourly_usd: float = 0.0
    dry_run: bool = False
    provider_resource_id: str = ""
    provider_payload: dict[str, Any] = field(default_factory=dict)
    registered_model_id: str = ""
    created_at: str = field(default_factory=_utc_now_iso)
    updated_at: str = field(default_factory=_utc_now_iso)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SwarmDeploymentRecord:
        return cls(
            deployment_id=str(data.get("deployment_id") or f"gpu_{uuid.uuid4().hex[:10]}"),
            provider=str(data.get("provider") or ""),
            swarm_name=str(data.get("swarm_name") or ""),
            status=str(data.get("status") or "unknown"),
            worker_count=int(data.get("worker_count") or 1),
            gpu_type=str(data.get("gpu_type") or ""),
            model_id=str(data.get("model_id") or ""),
            runtime_mode=str(data.get("runtime_mode") or "vllm"),
            container_image=str(data.get("container_image") or ""),
            region=str(data.get("region") or ""),
            inference_endpoint=str(data.get("inference_endpoint") or ""),
            estimated_hourly_usd=float(data.get("estimated_hourly_usd") or 0.0),
            dry_run=bool(data.get("dry_run", False)),
            provider_resource_id=str(data.get("provider_resource_id") or ""),
            provider_payload=dict(data.get("provider_payload") or {}) if isinstance(data.get("provider_payload"), dict) else {},
            registered_model_id=str(data.get("registered_model_id") or ""),
            created_at=str(data.get("created_at") or _utc_now_iso()),
            updated_at=str(data.get("updated_at") or _utc_now_iso()),
            error=str(data.get("error") or ""),
        )


def _runtime_entrypoint(request: SwarmDeployRequest) -> list[str]:
    if request.runtime_mode == "vllm":
        model_arg = request.model_id or "Qwen/Qwen2.5-Coder-14B-Instruct"
        return [
            "python3",
            "-m",
            "vllm.entrypoints.openai.api_server",
            "--model",
            model_arg,
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
        ]
    if request.runtime_mode == "ollama":
        return ["ollama", "serve"]
    return [
        "python3",
        "-m",
        "swarm_console.worker_node",
        "--swarm",
        request.swarm_name,
        "--model",
        request.model_id or "default",
    ]


class GpuProviderAdapter(ABC):
    """Abstract base class for integrating a GPU/Cloud platform with Sabrina swarm deployment."""

    provider_id: str = ""
    display_name: str = ""
    category: str = "gpu_pod"  # "cloud_platform" | "gpu_pod"
    capabilities: tuple[str, ...] = ()
    default_offers: tuple[GpuOffer, ...] = ()
    default_port: int = 8000

    def describe(self) -> dict[str, Any]:
        meta = PROVIDER_METADATA.get(self.provider_id, {})
        return {
            "provider_id": self.provider_id,
            "display_name": self.display_name or meta.get("label", self.provider_id),
            "category": self.category,
            "capabilities": list(self.capabilities),
            "offers": [o.to_dict() for o in self.default_offers],
            "config_fields": [dict(f) for f in (meta.get("config_fields") or ())],
        }

    def estimate_hourly_cost(self, request: SwarmDeployRequest) -> float:
        target = request.gpu_type.lower()
        for offer in self.default_offers:
            if offer.gpu_type.lower() == target or target in offer.gpu_type.lower():
                return round(offer.price_per_hr * request.worker_count, 3)
        fallback_rate = self.default_offers[0].price_per_hr if self.default_offers else 0.75
        return round(fallback_rate * request.worker_count, 3)

    @abstractmethod
    def build_deployment_spec(
        self,
        request: SwarmDeployRequest,
        config: dict[str, str],
    ) -> dict[str, Any]:
        """Translate a normalized SwarmDeployRequest into a provider-specific deployment spec."""

    @abstractmethod
    def deploy(
        self,
        api_key: str,
        config: dict[str, str],
        request: SwarmDeployRequest,
        http_fn: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        """Execute a live deployment on the provider and return status/resource_id/endpoint."""

    def check_status(
        self,
        api_key: str,
        config: dict[str, str],
        record: SwarmDeploymentRecord,
        http_fn: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        """Check live status of an existing deployment."""
        return {
            "ok": True,
            "status": record.status,
            "inference_endpoint": record.inference_endpoint,
        }

    def terminate(
        self,
        api_key: str,
        config: dict[str, str],
        record: SwarmDeploymentRecord,
        http_fn: Callable[..., dict[str, Any]],
    ) -> dict[str, Any]:
        """Terminate an existing deployment."""
        return {"ok": True, "status": "terminated"}


class RunPodGpuAdapter(GpuProviderAdapter):
    provider_id = "runpod"
    display_name = "RunPod (GPU Pods & Serverless)"
    category = "gpu_pod"
    capabilities = ("pod_provision", "serverless_inference", "openai_compat_vllm", "swarm_worker_deploy")
    default_offers = (
        GpuOffer("NVIDIA RTX 4090", 24, 0.69, "US/EU", "SECURE", "Fast 14B-32B swarm workers"),
        GpuOffer("NVIDIA L40S", 48, 1.14, "US/EU", "SECURE", "Balanced 70B quantized inference"),
        GpuOffer("NVIDIA A100 80GB", 80, 1.89, "US/EU", "SECURE", "High-throughput 70B+ vLLM"),
        GpuOffer("NVIDIA H100 80GB", 80, 3.29, "US/EU", "SECURE", "Frontier multi-agent swarm host"),
    )

    def build_deployment_spec(self, request: SwarmDeployRequest, config: dict[str, str]) -> dict[str, Any]:
        cloud_type = config.get("cloud_type") or "SECURE"
        env_map = {
            "SABRINA_SWARM_NAME": request.swarm_name,
            "SABRINA_MODEL_ID": request.model_id,
            "SABRINA_RUNTIME_MODE": request.runtime_mode,
            **request.env_vars,
        }
        return {
            "endpoint": "https://rest.runpod.io/v1/pods",
            "method": "POST",
            "body": {
                "name": request.swarm_name,
                "imageName": request.container_image,
                "gpuTypeIds": [request.gpu_type],
                "gpuCount": request.worker_count,
                "cloudType": cloud_type,
                "containerDiskInGb": request.disk_gb,
                "ports": [f"{self.default_port}/http", "22/tcp"],
                "dockerStartCmd": _runtime_entrypoint(request),
                "env": env_map,
            },
        }

    def deploy(self, api_key: str, config: dict[str, str], request: SwarmDeployRequest, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        spec = self.build_deployment_spec(request, config)
        res = http_fn(
            spec["endpoint"],
            method="POST",
            headers={"Authorization": f"Bearer {api_key}"},
            payload=spec["body"],
        )
        if not res.get("ok"):
            return {"ok": False, "status": "failed", "error": str(res.get("error") or "RunPod deploy failed")}
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        pod_id = str(data.get("id") or data.get("podId") or f"rp_{uuid.uuid4().hex[:8]}")
        endpoint = f"https://{pod_id}-{self.default_port}.proxy.runpod.net/v1"
        return {
            "ok": True,
            "status": "provisioning",
            "provider_resource_id": pod_id,
            "inference_endpoint": endpoint,
        }

    def check_status(self, api_key: str, config: dict[str, str], record: SwarmDeploymentRecord, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        if record.dry_run or not record.provider_resource_id:
            return {"ok": True, "status": record.status, "inference_endpoint": record.inference_endpoint}
        res = http_fn(
            f"https://rest.runpod.io/v1/pods/{record.provider_resource_id}",
            method="GET",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if not res.get("ok"):
            return {"ok": False, "status": record.status, "error": str(res.get("error") or "Status check failed")}
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        desired = str(data.get("desiredStatus") or data.get("status") or "running").lower()
        mapped = "running" if desired in {"running", "ready"} else ("terminated" if desired in {"exited", "terminated"} else "provisioning")
        return {"ok": True, "status": mapped, "inference_endpoint": record.inference_endpoint}

    def terminate(self, api_key: str, config: dict[str, str], record: SwarmDeploymentRecord, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        if record.dry_run or not record.provider_resource_id:
            return {"ok": True, "status": "terminated"}
        res = http_fn(
            f"https://rest.runpod.io/v1/pods/{record.provider_resource_id}",
            method="DELETE",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if not res.get("ok"):
            return {"ok": False, "status": record.status, "error": str(res.get("error") or "Terminate failed")}
        return {"ok": True, "status": "terminated"}


class VastAiGpuAdapter(GpuProviderAdapter):
    provider_id = "vastai"
    display_name = "Vast.ai (GPU Marketplace)"
    category = "gpu_pod"
    capabilities = ("pod_provision", "openai_compat_vllm", "swarm_worker_deploy")
    default_offers = (
        GpuOffer("RTX 4090", 24, 0.42, "Marketplace", "spot", "Lowest-cost 24GB GPU pod"),
        GpuOffer("A100 SXM4 80GB", 80, 1.35, "Marketplace", "on-demand", "High-VRAM 80GB swarm node"),
        GpuOffer("H100 SXM5 80GB", 80, 2.55, "Marketplace", "on-demand", "High-speed multi-worker pod"),
    )

    def build_deployment_spec(self, request: SwarmDeployRequest, config: dict[str, str]) -> dict[str, Any]:
        disk_gb = int(config.get("disk_gb") or request.disk_gb or 50)
        return {
            "endpoint": "https://console.vast.ai/api/v0/asks/",
            "method": "PUT",
            "body": {
                "client_id": "sabrina-console",
                "image": request.container_image,
                "disk": disk_gb,
                "label": request.swarm_name,
                "gpu_name": request.gpu_type,
                "num_gpus": request.worker_count,
                "onstart": " ".join(_runtime_entrypoint(request)),
                "env": {
                    "-p 8000:8000": "1",
                    "SABRINA_SWARM_NAME": request.swarm_name,
                    "SABRINA_MODEL_ID": request.model_id,
                    **request.env_vars,
                },
            },
        }

    def deploy(self, api_key: str, config: dict[str, str], request: SwarmDeployRequest, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        spec = self.build_deployment_spec(request, config)
        res = http_fn(
            spec["endpoint"],
            method="POST",
            headers={"Authorization": f"Bearer {api_key}"},
            payload=spec["body"],
        )
        if not res.get("ok"):
            return {"ok": False, "status": "failed", "error": str(res.get("error") or "Vast.ai deploy failed")}
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        inst_id = str(data.get("new_contract") or data.get("id") or f"vast_{uuid.uuid4().hex[:8]}")
        endpoint = config.get("endpoint_url") or f"https://vast-{inst_id}.cloud.vast.ai/v1"
        return {
            "ok": True,
            "status": "provisioning",
            "provider_resource_id": inst_id,
            "inference_endpoint": endpoint,
        }

    def terminate(self, api_key: str, config: dict[str, str], record: SwarmDeploymentRecord, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        if record.dry_run or not record.provider_resource_id:
            return {"ok": True, "status": "terminated"}
        res = http_fn(
            f"https://console.vast.ai/api/v0/instances/{record.provider_resource_id}/",
            method="DELETE",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if not res.get("ok"):
            return {"ok": False, "status": record.status, "error": str(res.get("error") or "Vast.ai terminate failed")}
        return {"ok": True, "status": "terminated"}


class LambdaLabsGpuAdapter(GpuProviderAdapter):
    provider_id = "lambda_labs"
    display_name = "Lambda Cloud (GPU Instances)"
    category = "gpu_pod"
    capabilities = ("pod_provision", "openai_compat_vllm", "swarm_worker_deploy")
    default_offers = (
        GpuOffer("gpu_1x_a10", 24, 0.75, "us-west-1", "on-demand", "1x A10 24GB"),
        GpuOffer("gpu_1x_a100_sxm4", 80, 1.29, "us-west-1", "on-demand", "1x A100 80GB"),
        GpuOffer("gpu_1x_h100_pcie", 80, 2.49, "us-west-1", "on-demand", "1x H100 80GB"),
        GpuOffer("gpu_8x_h100_sxm5", 640, 23.92, "us-west-1", "on-demand", "8x H100 full swarm cluster"),
    )

    def build_deployment_spec(self, request: SwarmDeployRequest, config: dict[str, str]) -> dict[str, Any]:
        region = request.region or config.get("region") or "us-west-1"
        ssh_keys = [config["ssh_key_name"]] if config.get("ssh_key_name") else []
        return {
            "endpoint": "https://cloud.lambdalabs.com/api/v1/instance-operations/launch",
            "method": "POST",
            "body": {
                "region_name": region,
                "instance_type_name": request.gpu_type,
                "ssh_key_names": ssh_keys,
                "quantity": request.worker_count,
                "name": request.swarm_name,
            },
            "bootstrap_command": " ".join(_runtime_entrypoint(request)),
        }

    def deploy(self, api_key: str, config: dict[str, str], request: SwarmDeployRequest, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        spec = self.build_deployment_spec(request, config)
        res = http_fn(
            spec["endpoint"],
            method="POST",
            headers={"Authorization": f"Bearer {api_key}"},
            payload=spec["body"],
        )
        if not res.get("ok"):
            return {"ok": False, "status": "failed", "error": str(res.get("error") or "Lambda Cloud launch failed")}
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        ids = (data.get("data") or {}).get("instance_ids") if isinstance(data.get("data"), dict) else []
        inst_id = str(ids[0]) if ids else f"lambda_{uuid.uuid4().hex[:8]}"
        endpoint = config.get("endpoint_url") or "https://api.lambdalabs.com/v1"
        return {
            "ok": True,
            "status": "provisioning",
            "provider_resource_id": inst_id,
            "inference_endpoint": endpoint,
        }

    def terminate(self, api_key: str, config: dict[str, str], record: SwarmDeploymentRecord, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        if record.dry_run or not record.provider_resource_id:
            return {"ok": True, "status": "terminated"}
        res = http_fn(
            "https://cloud.lambdalabs.com/api/v1/instance-operations/terminate",
            method="POST",
            headers={"Authorization": f"Bearer {api_key}"},
            payload={"instance_ids": [record.provider_resource_id]},
        )
        if not res.get("ok"):
            return {"ok": False, "status": record.status, "error": str(res.get("error") or "Lambda terminate failed")}
        return {"ok": True, "status": "terminated"}


class GoogleVertexGpuAdapter(GpuProviderAdapter):
    provider_id = "google_vertex"
    display_name = "Google Cloud / Vertex AI"
    category = "cloud_platform"
    capabilities = ("serverless_inference", "openai_compat_vllm", "pod_provision", "swarm_worker_deploy")
    default_offers = (
        GpuOffer("NVIDIA L4 (g2-standard-4)", 24, 0.71, "us-central1", "on-demand", "Vertex AI / GCE L4 node"),
        GpuOffer("NVIDIA A100 80GB (a2-ultragpu-1g)", 80, 3.67, "us-central1", "on-demand", "Vertex AI Model Garden / vLLM"),
        GpuOffer("NVIDIA H100 80GB (a3-highgpu-1g)", 80, 6.20, "us-central1", "on-demand", "High-scale swarm serving"),
    )

    def build_deployment_spec(self, request: SwarmDeployRequest, config: dict[str, str]) -> dict[str, Any]:
        project_id = config.get("project_id") or "your-gcp-project"
        region = request.region or config.get("region") or "us-central1"
        endpoint_url = config.get("endpoint_url") or (
            f"https://{region}-aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/{region}/endpoints/openapi"
        )
        return {
            "endpoint": f"https://{region}-aiplatform.googleapis.com/v1/projects/{project_id}/locations/{region}/endpoints",
            "method": "POST",
            "inference_base_url": endpoint_url,
            "body": {
                "displayName": request.swarm_name,
                "dedicatedEndpointEnabled": True,
                "deployedModelSpec": {
                    "model": request.model_id,
                    "containerImage": request.container_image,
                    "machineSpec": {"acceleratorType": request.gpu_type, "acceleratorCount": 1},
                    "minReplicaCount": request.worker_count,
                    "maxReplicaCount": request.worker_count,
                },
            },
        }

    def deploy(self, api_key: str, config: dict[str, str], request: SwarmDeployRequest, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        spec = self.build_deployment_spec(request, config)
        res = http_fn(
            spec["endpoint"],
            method="POST",
            headers={"Authorization": f"Bearer {api_key}"},
            payload={"displayName": request.swarm_name},
        )
        if not res.get("ok"):
            return {"ok": False, "status": "failed", "error": str(res.get("error") or "Vertex AI deploy failed")}
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        op_name = str(data.get("name") or f"vertex_{uuid.uuid4().hex[:8]}")
        return {
            "ok": True,
            "status": "provisioning",
            "provider_resource_id": op_name,
            "inference_endpoint": spec["inference_base_url"],
        }


class AwsBedrockGpuAdapter(GpuProviderAdapter):
    provider_id = "aws_bedrock"
    display_name = "Amazon AWS (Bedrock & EC2 GPU)"
    category = "cloud_platform"
    capabilities = ("serverless_inference", "openai_compat_vllm", "pod_provision", "swarm_worker_deploy")
    default_offers = (
        GpuOffer("AWS Bedrock Serverless", 0, 0.0, "us-east-1", "serverless", "Pay-per-token Bedrock foundation models"),
        GpuOffer("EC2 g5.2xlarge (NVIDIA A10G)", 24, 1.21, "us-east-1", "on-demand", "Dedicated A10G vLLM worker"),
        GpuOffer("EC2 p4d.24xlarge (8x A100)", 320, 32.77, "us-east-1", "on-demand", "Multi-GPU swarm cluster"),
        GpuOffer("EC2 p5.48xlarge (8x H100)", 640, 98.32, "us-east-1", "on-demand", "Frontier swarm cluster"),
    )

    def build_deployment_spec(self, request: SwarmDeployRequest, config: dict[str, str]) -> dict[str, Any]:
        region = request.region or config.get("region") or "us-east-1"
        endpoint_url = config.get("endpoint_url") or f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1"
        return {
            "endpoint": f"{endpoint_url.rstrip('/')}/models",
            "method": "POST",
            "inference_base_url": endpoint_url,
            "body": {
                "swarmName": request.swarm_name,
                "region": region,
                "modelId": request.model_id,
                "instanceType": request.gpu_type,
                "workerCount": request.worker_count,
                "containerImage": request.container_image,
                "command": _runtime_entrypoint(request),
            },
        }

    def deploy(self, api_key: str, config: dict[str, str], request: SwarmDeployRequest, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        spec = self.build_deployment_spec(request, config)
        # Validate connectivity against the configured Bedrock/OpenAI-compat endpoint
        res = http_fn(
            spec["endpoint"],
            method="GET",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if not res.get("ok"):
            return {"ok": False, "status": "failed", "error": str(res.get("error") or "AWS Bedrock/GPU endpoint check failed")}
        return {
            "ok": True,
            "status": "running",
            "provider_resource_id": f"aws_{uuid.uuid4().hex[:8]}",
            "inference_endpoint": spec["inference_base_url"],
        }


class ModalGpuAdapter(GpuProviderAdapter):
    provider_id = "modal"
    display_name = "Modal (Serverless GPU Containers)"
    category = "gpu_pod"
    capabilities = ("serverless_inference", "openai_compat_vllm", "swarm_worker_deploy")
    default_offers = (
        GpuOffer("L4", 24, 0.80, "US", "serverless", "Scale-to-zero L4 container"),
        GpuOffer("A100-80GB", 80, 2.50, "US", "serverless", "Scale-to-zero A100 container"),
        GpuOffer("H100", 80, 3.95, "US", "serverless", "Scale-to-zero H100 container"),
    )

    def build_deployment_spec(self, request: SwarmDeployRequest, config: dict[str, str]) -> dict[str, Any]:
        workspace = config.get("workspace") or "default"
        endpoint_url = config.get("endpoint_url") or f"https://{workspace}--{request.swarm_name}.modal.run/v1"
        return {
            "endpoint": "https://api.modal.com/v1/apps",
            "method": "POST",
            "inference_base_url": endpoint_url,
            "body": {
                "app_name": request.swarm_name,
                "workspace": workspace,
                "gpu": request.gpu_type,
                "concurrency": request.worker_count,
                "image": request.container_image,
                "command": _runtime_entrypoint(request),
            },
        }

    def deploy(self, api_key: str, config: dict[str, str], request: SwarmDeployRequest, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        spec = self.build_deployment_spec(request, config)
        headers = {"Authorization": f"Bearer {api_key}"}
        if config.get("token_id"):
            headers["Modal-Key"] = config["token_id"]
            headers["Modal-Secret"] = api_key
        res = http_fn(spec["endpoint"], method="POST", headers=headers, payload=spec["body"])
        if not res.get("ok"):
            return {"ok": False, "status": "failed", "error": str(res.get("error") or "Modal deploy failed")}
        data = res.get("data") if isinstance(res.get("data"), dict) else {}
        app_id = str(data.get("app_id") or data.get("id") or f"modal_{uuid.uuid4().hex[:8]}")
        return {
            "ok": True,
            "status": "provisioning",
            "provider_resource_id": app_id,
            "inference_endpoint": spec["inference_base_url"],
        }


class CoreWeaveGpuAdapter(GpuProviderAdapter):
    provider_id = "coreweave"
    display_name = "CoreWeave (GPU Cluster)"
    category = "gpu_pod"
    capabilities = ("pod_provision", "openai_compat_vllm", "swarm_worker_deploy")
    default_offers = (
        GpuOffer("NVIDIA A100 80GB", 80, 2.21, "ORD1", "on-demand", "CoreWeave Kubernetes GPU node"),
        GpuOffer("NVIDIA H100 80GB", 80, 4.25, "ORD1", "on-demand", "InfiniBand H100 swarm cluster"),
    )

    def build_deployment_spec(self, request: SwarmDeployRequest, config: dict[str, str]) -> dict[str, Any]:
        region = request.region or config.get("region") or "ORD1"
        endpoint_url = config.get("endpoint_url") or f"https://{request.swarm_name}.{region.lower()}.coreweave.cloud/v1"
        return {
            "endpoint": f"{endpoint_url.rstrip('/')}/models",
            "method": "POST",
            "inference_base_url": endpoint_url,
            "body": {
                "name": request.swarm_name,
                "region": region,
                "gpuType": request.gpu_type,
                "replicas": request.worker_count,
                "image": request.container_image,
                "command": _runtime_entrypoint(request),
            },
        }

    def deploy(self, api_key: str, config: dict[str, str], request: SwarmDeployRequest, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        spec = self.build_deployment_spec(request, config)
        res = http_fn(spec["endpoint"], method="GET", headers={"Authorization": f"Bearer {api_key}"})
        if not res.get("ok"):
            return {"ok": False, "status": "failed", "error": str(res.get("error") or "CoreWeave endpoint check failed")}
        return {
            "ok": True,
            "status": "running",
            "provider_resource_id": f"cw_{uuid.uuid4().hex[:8]}",
            "inference_endpoint": spec["inference_base_url"],
        }


class CustomGpuPodAdapter(GpuProviderAdapter):
    provider_id = "custom_gpu"
    display_name = "Custom GPU Pod / Self-Hosted vLLM"
    category = "gpu_pod"
    capabilities = ("openai_compat_vllm", "swarm_worker_deploy")
    default_offers = (
        GpuOffer("Custom GPU", 24, 0.0, "self-hosted", "self-hosted", "Bring your own GPU server, vLLM, or Ollama pod"),
    )

    def build_deployment_spec(self, request: SwarmDeployRequest, config: dict[str, str]) -> dict[str, Any]:
        endpoint_url = (config.get("endpoint_url") or "http://127.0.0.1:8000/v1").rstrip("/")
        return {
            "endpoint": f"{endpoint_url}/models",
            "method": "GET",
            "inference_base_url": endpoint_url,
            "body": {
                "swarm_name": request.swarm_name,
                "model_id": request.model_id,
                "workers": request.worker_count,
                "ssh_host": config.get("ssh_host") or "",
                "command": _runtime_entrypoint(request),
            },
        }

    def deploy(self, api_key: str, config: dict[str, str], request: SwarmDeployRequest, http_fn: Callable[..., dict[str, Any]]) -> dict[str, Any]:
        spec = self.build_deployment_spec(request, config)
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        res = http_fn(spec["endpoint"], method="GET", headers=headers)
        if not res.get("ok"):
            return {"ok": False, "status": "failed", "error": str(res.get("error") or "Custom GPU pod unreachable")}
        return {
            "ok": True,
            "status": "running",
            "provider_resource_id": f"custom_{uuid.uuid4().hex[:8]}",
            "inference_endpoint": spec["inference_base_url"],
        }


class GpuProviderRegistry:
    """Extensible registry of GPU cloud and pod provider adapters."""

    def __init__(self) -> None:
        self._adapters: dict[str, GpuProviderAdapter] = {}
        for adapter in (
            RunPodGpuAdapter(),
            VastAiGpuAdapter(),
            LambdaLabsGpuAdapter(),
            GoogleVertexGpuAdapter(),
            AwsBedrockGpuAdapter(),
            ModalGpuAdapter(),
            CoreWeaveGpuAdapter(),
            CustomGpuPodAdapter(),
        ):
            self.register(adapter)

    def register(self, adapter: GpuProviderAdapter) -> None:
        key = str(adapter.provider_id or "").strip().lower()
        if not key:
            raise ValueError("Adapter provider_id must be non-empty.")
        self._adapters[key] = adapter

    def get(self, provider_id: str) -> GpuProviderAdapter | None:
        return self._adapters.get(str(provider_id or "").strip().lower())

    def list_adapters(self) -> list[GpuProviderAdapter]:
        return list(self._adapters.values())


class SwarmGpuDeploymentManager:
    """Orchestrates GPU provider swarm deployments, persistence, and Model Fleet registration."""

    def __init__(
        self,
        *,
        vault: ProviderKeyVault,
        deployments_path: str | Path,
        model_registry: Any | None = None,
        provider_registry: GpuProviderRegistry | None = None,
        http_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.vault = vault
        self.deployments_path = guard_safe_path(deployments_path)
        self.model_registry = model_registry
        self.provider_registry = provider_registry or GpuProviderRegistry()
        self._http_fn = http_fn or _default_http_json
        self._lock = threading.RLock()
        self._deployments: dict[str, SwarmDeploymentRecord] = self._load_deployments()

    def _load_deployments(self) -> dict[str, SwarmDeploymentRecord]:
        if not self.deployments_path.exists():
            return {}
        try:
            raw = json.loads(self.deployments_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        items = raw.get("deployments")
        if not isinstance(items, list):
            return {}
        loaded: dict[str, SwarmDeploymentRecord] = {}
        for item in items:
            if isinstance(item, dict):
                rec = SwarmDeploymentRecord.from_dict(item)
                loaded[rec.deployment_id] = rec
        return loaded

    def _save_deployments(self) -> None:
        self.deployments_path.parent.mkdir(parents=True, exist_ok=True)
        ordered = sorted(self._deployments.values(), key=lambda r: r.created_at, reverse=True)
        payload = json.dumps({"version": 1, "deployments": [r.to_dict() for r in ordered]}, indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.deployments_path.parent), prefix="gpu_dep_", suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_path, self.deployments_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def list_providers(self) -> list[dict[str, Any]]:
        """List all registered GPU/cloud providers enriched with live Vault unlock status."""
        vault_status = self.vault.list_providers()
        status_map = {
            str(p.get("provider") or ""): p
            for p in (vault_status.get("providers") or [])
            if isinstance(p, dict)
        }
        items: list[dict[str, Any]] = []
        for adapter in self.provider_registry.list_adapters():
            desc = adapter.describe()
            v_info = status_map.get(adapter.provider_id, {})
            desc["configured"] = bool(v_info.get("configured", False))
            desc["unlocked"] = bool(v_info.get("unlocked", False))
            desc["source"] = str(v_info.get("source") or "none")
            desc["masked_key"] = str(v_info.get("masked_key") or "")
            desc["verified"] = bool(v_info.get("verified", False))
            desc["config"] = dict(v_info.get("config") or {})
            items.append(desc)
        return items

    def list_deployments(self) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(self._deployments.values(), key=lambda r: r.created_at, reverse=True)
            return [r.to_dict() for r in ordered]

    def deploy_swarm(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Deploy (or dry-run preview) a swarm onto a GPU provider and optionally register its model."""
        if not isinstance(payload, dict):
            return {"ok": False, "error": "Deployment payload must be a JSON object."}
        req = SwarmDeployRequest(
            provider=str(payload.get("provider") or ""),
            swarm_name=str(payload.get("swarm_name") or ""),
            model_id=str(payload.get("model_id") or ""),
            gpu_type=str(payload.get("gpu_type") or ""),
            worker_count=int(payload.get("worker_count") or 1),
            runtime_mode=str(payload.get("runtime_mode") or "vllm"),
            container_image=str(payload.get("container_image") or ""),
            region=str(payload.get("region") or ""),
            disk_gb=int(payload.get("disk_gb") or 50),
            role_assignments=dict(payload.get("role_assignments") or {}) if isinstance(payload.get("role_assignments"), dict) else {},
            env_vars=dict(payload.get("env_vars") or {}) if isinstance(payload.get("env_vars"), dict) else {},
            auto_register_model=bool(payload.get("auto_register_model", True)),
            dry_run=bool(payload.get("dry_run", False)),
        )
        adapter = self.provider_registry.get(req.provider)
        if not adapter:
            return {"ok": False, "error": f"Unsupported GPU provider: {req.provider!r}"}

        config = self.vault.get_provider_config(req.provider)
        spec = adapter.build_deployment_spec(req, config)
        est_cost = adapter.estimate_hourly_cost(req)
        deployment_id = f"gpu_{uuid.uuid4().hex[:10]}"

        if req.dry_run:
            record = SwarmDeploymentRecord(
                deployment_id=deployment_id,
                provider=req.provider,
                swarm_name=req.swarm_name,
                status="dry_run_ready",
                worker_count=req.worker_count,
                gpu_type=req.gpu_type,
                model_id=req.model_id,
                runtime_mode=req.runtime_mode,
                container_image=req.container_image,
                region=req.region or config.get("region") or "auto",
                inference_endpoint=str(spec.get("inference_base_url") or config.get("endpoint_url") or ""),
                estimated_hourly_usd=est_cost,
                dry_run=True,
                provider_payload=spec,
            )
            with self._lock:
                self._deployments[deployment_id] = record
                self._save_deployments()
            return {"ok": True, "deployment": record.to_dict(), "spec": spec}

        api_key = self.vault.get_key(req.provider, include_env=True) or ""
        if not api_key and req.provider != "custom_gpu":
            return {
                "ok": False,
                "error": f"Provider '{req.provider}' is locked. Add an API key in API Keys & Providers first.",
            }

        res = adapter.deploy(api_key, config, req, self._http_fn)
        record = SwarmDeploymentRecord(
            deployment_id=deployment_id,
            provider=req.provider,
            swarm_name=req.swarm_name,
            status=str(res.get("status") or ("provisioning" if res.get("ok") else "failed")),
            worker_count=req.worker_count,
            gpu_type=req.gpu_type,
            model_id=req.model_id,
            runtime_mode=req.runtime_mode,
            container_image=req.container_image,
            region=req.region or config.get("region") or "auto",
            inference_endpoint=str(res.get("inference_endpoint") or ""),
            estimated_hourly_usd=est_cost,
            dry_run=False,
            provider_resource_id=str(res.get("provider_resource_id") or ""),
            provider_payload=spec,
            error=str(res.get("error") or ""),
        )
        with self._lock:
            self._deployments[deployment_id] = record
            self._save_deployments()

        if res.get("ok") and req.auto_register_model and record.inference_endpoint:
            self.register_deployment_model(deployment_id)

        with self._lock:
            final_rec = self._deployments[deployment_id].to_dict()
        return {
            "ok": bool(res.get("ok")),
            "deployment": final_rec,
            "spec": spec,
            "error": record.error or None,
        }

    def check_status(self, deployment_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._deployments.get(deployment_id)
        if not record:
            return {"ok": False, "error": f"Unknown deployment_id: {deployment_id!r}"}
        adapter = self.provider_registry.get(record.provider)
        if not adapter:
            return {"ok": False, "error": f"Unknown provider: {record.provider!r}"}
        api_key = self.vault.get_key(record.provider, include_env=True) or ""
        config = self.vault.get_provider_config(record.provider)
        res = adapter.check_status(api_key, config, record, self._http_fn)
        with self._lock:
            if res.get("status"):
                record.status = str(res["status"])
            if res.get("inference_endpoint"):
                record.inference_endpoint = str(res["inference_endpoint"])
            record.error = str(res.get("error") or "")
            record.updated_at = _utc_now_iso()
            self._save_deployments()
            out = record.to_dict()
        return {"ok": bool(res.get("ok", True)), "deployment": out, "error": res.get("error")}

    def terminate_deployment(self, deployment_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._deployments.get(deployment_id)
        if not record:
            return {"ok": False, "error": f"Unknown deployment_id: {deployment_id!r}"}
        adapter = self.provider_registry.get(record.provider)
        if not adapter:
            return {"ok": False, "error": f"Unknown provider: {record.provider!r}"}
        api_key = self.vault.get_key(record.provider, include_env=True) or ""
        config = self.vault.get_provider_config(record.provider)
        res = adapter.terminate(api_key, config, record, self._http_fn)
        with self._lock:
            if res.get("ok"):
                record.status = "terminated"
                record.error = ""
            else:
                record.error = str(res.get("error") or "Terminate failed")
            record.updated_at = _utc_now_iso()
            self._save_deployments()
            out = record.to_dict()
        return {"ok": bool(res.get("ok")), "deployment": out, "error": res.get("error")}

    def register_deployment_model(self, deployment_id: str) -> dict[str, Any]:
        """Register a GPU deployment's inference endpoint into Sabrina's Model Fleet."""
        with self._lock:
            record = self._deployments.get(deployment_id)
        if not record:
            return {"ok": False, "error": f"Unknown deployment_id: {deployment_id!r}"}
        if not self.model_registry or not hasattr(self.model_registry, "add_model"):
            return {"ok": False, "error": "Model registry is not attached."}
        raw_model = record.model_id or record.swarm_name
        prefixed_id = (
            raw_model
            if raw_model.lower().startswith(f"{record.provider}/")
            else f"{record.provider}/{raw_model}"
        )
        try:
            added = self.model_registry.add_model(
                model_id=prefixed_id,
                name=f"{record.swarm_name} ({record.gpu_type})",
                provider=record.provider,
                tier="api",
                role=f"GPU Swarm ({record.worker_count}x {record.gpu_type})",
                endpoint=record.inference_endpoint,
                capabilities=["chat", "reasoning"],
            )
            with self._lock:
                record.registered_model_id = prefixed_id
                record.updated_at = _utc_now_iso()
                self._save_deployments()
            # Also save endpoint_url into provider config if not already set so _invoke_provider_text can route to it
            if record.inference_endpoint:
                cfg = self.vault.get_provider_config(record.provider)
                if not cfg.get("endpoint_url"):
                    self.vault.set_provider_config(record.provider, {"endpoint_url": record.inference_endpoint})
            return {"ok": True, "model": added, "deployment": record.to_dict()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
