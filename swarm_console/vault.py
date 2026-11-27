"""Encrypted API Key Vault (Windows DPAPI) and live provider model probing."""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .protocol import guard_safe_path
from .registry import normalize_model_record, normalize_openrouter_catalog

SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "gemini",
    "google_vertex",
    "aws_bedrock",
    "openai",
    "anthropic",
    "openrouter",
    "huggingface",
    "cerebras",
    "sambanova",
    "fireworks",
    "nvidia",
    "github",
    "cohere",
    "perplexity",
    "xai",
    "deepseek",
    "groq",
    "mistral",
    "together",
    "runpod",
    "vastai",
    "lambda_labs",
    "modal",
    "coreweave",
    "custom_gpu",
    "pollinations",
    "lmstudio",
    "ollama",
)

PROVIDER_METADATA: dict[str, dict[str, Any]] = {
    "gemini": {
        "label": "Google Gemini",
        "category": "model_api",
        "env_vars": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/models",
        "auth_type": "x-goog-api-key",
    },
    "google_vertex": {
        "label": "Google Cloud / Vertex AI",
        "category": "cloud_platform",
        "env_vars": ("GOOGLE_CLOUD_API_KEY", "VERTEX_AI_API_KEY", "GCP_ACCESS_TOKEN"),
        "endpoint": "https://{region}-aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/{region}/endpoints/openapi/models",
        "auth_type": "bearer",
        "config_fields": (
            {"key": "project_id", "label": "GCP Project ID", "placeholder": "my-gcp-project", "default": ""},
            {"key": "region", "label": "Region", "placeholder": "us-central1", "default": "us-central1"},
            {"key": "endpoint_url", "label": "Custom OpenAI/Inference URL (optional)", "placeholder": "https://us-central1-aiplatform.googleapis.com/v1beta1/...", "default": ""},
        ),
    },
    "aws_bedrock": {
        "label": "Amazon AWS (Bedrock & EC2 GPU)",
        "category": "cloud_platform",
        "env_vars": ("AWS_BEARER_TOKEN_BEDROCK", "AWS_SECRET_ACCESS_KEY", "AWS_BEDROCK_API_KEY"),
        "endpoint": "https://bedrock-runtime.{region}.amazonaws.com/openai/v1/models",
        "auth_type": "bearer",
        "config_fields": (
            {"key": "region", "label": "AWS Region", "placeholder": "us-east-1", "default": "us-east-1"},
            {"key": "access_key_id", "label": "AWS Access Key ID (optional for EC2/IAM)", "placeholder": "AKIA...", "default": ""},
            {"key": "endpoint_url", "label": "Custom Bedrock / vLLM Endpoint (optional)", "placeholder": "https://bedrock-runtime.us-east-1.amazonaws.com/openai/v1", "default": ""},
        ),
    },
    "openai": {
        "label": "OpenAI",
        "category": "model_api",
        "env_vars": ("OPENAI_API_KEY",),
        "endpoint": "https://api.openai.com/v1/models",
        "auth_type": "bearer",
    },
    "anthropic": {
        "label": "Anthropic",
        "category": "model_api",
        "env_vars": ("ANTHROPIC_API_KEY",),
        "endpoint": "https://api.anthropic.com/v1/models",
        "auth_type": "anthropic",
    },
    "openrouter": {
        "label": "OpenRouter",
        "category": "model_api",
        "env_vars": ("OPENROUTER_API_KEY",),
        "endpoint": "https://openrouter.ai/api/v1/models",
        "auth_type": "bearer",
    },
    "huggingface": {
        "label": "HuggingFace Router & Inference",
        "category": "model_api",
        "env_vars": ("HF_TOKEN", "HUGGINGFACE_API_KEY", "HUGGINGFACEHUB_API_TOKEN"),
        "endpoint": "https://router.huggingface.co/v1/models",
        "auth_type": "bearer",
    },
    "cerebras": {
        "label": "Cerebras (Wafer-Scale)",
        "category": "model_api",
        "env_vars": ("CEREBRAS_API_KEY",),
        "endpoint": "https://api.cerebras.ai/v1/models",
        "auth_type": "bearer",
    },
    "sambanova": {
        "label": "SambaNova Cloud",
        "category": "model_api",
        "env_vars": ("SAMBANOVA_API_KEY",),
        "endpoint": "https://api.sambanova.ai/v1/models",
        "auth_type": "bearer",
    },
    "fireworks": {
        "label": "Fireworks AI",
        "category": "model_api",
        "env_vars": ("FIREWORKS_API_KEY",),
        "endpoint": "https://api.fireworks.ai/inference/v1/models",
        "auth_type": "bearer",
    },
    "nvidia": {
        "label": "NVIDIA NIM",
        "category": "model_api",
        "env_vars": ("NVIDIA_API_KEY", "NGC_API_KEY"),
        "endpoint": "https://integrate.api.nvidia.com/v1/models",
        "auth_type": "bearer",
    },
    "github": {
        "label": "GitHub Models (Azure AI)",
        "category": "model_api",
        "env_vars": ("GITHUB_MODELS_TOKEN",),
        "endpoint": "https://models.inference.ai.azure.com/models",
        "auth_type": "bearer",
    },
    "cohere": {
        "label": "Cohere",
        "category": "model_api",
        "env_vars": ("COHERE_API_KEY", "CO_API_KEY"),
        "endpoint": "https://api.cohere.com/v1/models",
        "auth_type": "bearer",
    },
    "perplexity": {
        "label": "Perplexity Sonar",
        "category": "model_api",
        "env_vars": ("PERPLEXITY_API_KEY", "PPLX_API_KEY"),
        "endpoint": "https://api.perplexity.ai/models",
        "auth_type": "bearer",
    },
    "xai": {
        "label": "xAI (Grok)",
        "category": "model_api",
        "env_vars": ("XAI_API_KEY",),
        "endpoint": "https://api.x.ai/v1/models",
        "auth_type": "bearer",
    },
    "deepseek": {
        "label": "DeepSeek",
        "category": "model_api",
        "env_vars": ("DEEPSEEK_API_KEY",),
        "endpoint": "https://api.deepseek.com/models",
        "auth_type": "bearer",
    },
    "groq": {
        "label": "Groq",
        "category": "model_api",
        "env_vars": ("GROQ_API_KEY",),
        "endpoint": "https://api.groq.com/openai/v1/models",
        "auth_type": "bearer",
    },
    "mistral": {
        "label": "Mistral AI",
        "category": "model_api",
        "env_vars": ("MISTRAL_API_KEY",),
        "endpoint": "https://api.mistral.ai/v1/models",
        "auth_type": "bearer",
    },
    "together": {
        "label": "Together AI",
        "category": "model_api",
        "env_vars": ("TOGETHER_API_KEY",),
        "endpoint": "https://api.together.xyz/v1/models",
        "auth_type": "bearer",
    },
    "runpod": {
        "label": "RunPod (GPU Pods & Serverless)",
        "category": "gpu_pod",
        "env_vars": ("RUNPOD_API_KEY",),
        "endpoint": "https://rest.runpod.io/v1/pods",
        "auth_type": "bearer",
        "config_fields": (
            {"key": "endpoint_id", "label": "Serverless Endpoint ID (optional)", "placeholder": "e.g. vllm-abc123", "default": ""},
            {"key": "endpoint_url", "label": "OpenAI-Compat Inference URL (optional)", "placeholder": "https://api.runpod.ai/v2/<endpoint_id>/openai/v1", "default": ""},
            {"key": "gpu_type", "label": "Default GPU Type", "placeholder": "NVIDIA RTX 4090", "default": "NVIDIA RTX 4090"},
            {"key": "cloud_type", "label": "Cloud Type", "placeholder": "SECURE or COMMUNITY", "default": "SECURE"},
        ),
    },
    "vastai": {
        "label": "Vast.ai (GPU Marketplace)",
        "category": "gpu_pod",
        "env_vars": ("VAST_API_KEY", "VASTAI_API_KEY"),
        "endpoint": "https://console.vast.ai/api/v0/instances/",
        "auth_type": "bearer",
        "config_fields": (
            {"key": "endpoint_url", "label": "OpenAI-Compat Inference URL (optional)", "placeholder": "https://<pod-host>:<port>/v1", "default": ""},
            {"key": "gpu_type", "label": "Default GPU Type", "placeholder": "RTX 4090", "default": "RTX 4090"},
            {"key": "disk_gb", "label": "Default Disk (GB)", "placeholder": "50", "default": "50"},
        ),
    },
    "lambda_labs": {
        "label": "Lambda Cloud (GPU Instances)",
        "category": "gpu_pod",
        "env_vars": ("LAMBDA_API_KEY", "LAMBDA_CLOUD_API_KEY"),
        "endpoint": "https://cloud.lambdalabs.com/api/v1/instance-types",
        "auth_type": "bearer",
        "config_fields": (
            {"key": "region", "label": "Default Region", "placeholder": "us-west-1", "default": "us-west-1"},
            {"key": "ssh_key_name", "label": "SSH Key Name", "placeholder": "sabrina-swarm-key", "default": ""},
            {"key": "endpoint_url", "label": "Lambda Inference / vLLM URL (optional)", "placeholder": "https://api.lambdalabs.com/v1", "default": "https://api.lambdalabs.com/v1"},
        ),
    },
    "modal": {
        "label": "Modal (Serverless GPU Containers)",
        "category": "gpu_pod",
        "env_vars": ("MODAL_TOKEN_SECRET", "MODAL_API_KEY"),
        "endpoint": "https://api.modal.com/v1/apps",
        "auth_type": "bearer",
        "config_fields": (
            {"key": "token_id", "label": "Modal Token ID", "placeholder": "ak-...", "default": ""},
            {"key": "workspace", "label": "Workspace", "placeholder": "default", "default": ""},
            {"key": "endpoint_url", "label": "Deployed vLLM / Swarm URL", "placeholder": "https://<workspace>--sabrina-vllm.modal.run/v1", "default": ""},
        ),
    },
    "coreweave": {
        "label": "CoreWeave (GPU Cluster)",
        "category": "gpu_pod",
        "env_vars": ("COREWEAVE_API_KEY",),
        "endpoint": "https://api.coreweave.com/v1/models",
        "auth_type": "bearer",
        "config_fields": (
            {"key": "region", "label": "Region / Zone", "placeholder": "ORD1", "default": "ORD1"},
            {"key": "endpoint_url", "label": "Inference / Cluster URL", "placeholder": "https://<cluster>.coreweave.cloud/v1", "default": ""},
            {"key": "gpu_type", "label": "Default GPU Type", "placeholder": "NVIDIA H100 80GB", "default": "NVIDIA H100 80GB"},
        ),
    },
    "custom_gpu": {
        "label": "Custom GPU Pod / Self-Hosted vLLM",
        "category": "gpu_pod",
        "env_vars": ("SABRINA_CUSTOM_GPU_API_KEY",),
        "endpoint": "http://127.0.0.1:8000/v1/models",
        "auth_type": "bearer",
        "config_fields": (
            {"key": "endpoint_url", "label": "OpenAI-Compat Base URL", "placeholder": "https://gpu-pod.example.com/v1", "default": "http://127.0.0.1:8000/v1"},
            {"key": "gpu_type", "label": "GPU Hardware Label", "placeholder": "NVIDIA A100 80GB", "default": "Custom GPU"},
            {"key": "ssh_host", "label": "SSH / Control Host (optional)", "placeholder": "root@gpu-pod.example.com", "default": ""},
        ),
    },
    "pollinations": {
        "label": "Pollinations AI (Open Text & Image)",
        "category": "model_api",
        "env_vars": ("POLLINATIONS_API_KEY", "SABRINA_ENABLE_POLLINATIONS"),
        "endpoint": "https://text.pollinations.ai/models",
        "auth_type": "none",
    },
    "lmstudio": {
        "label": "LM Studio / Local vLLM (:1234)",
        "category": "local",
        "env_vars": ("SABRINA_LOCAL_OPENAI_URL", "LMSTUDIO_API_KEY"),
        "endpoint": "http://127.0.0.1:1234/v1/models",
        "auth_type": "none",
    },
    "ollama": {
        "label": "Ollama (Local)",
        "category": "local",
        "env_vars": (),
        "endpoint": "http://127.0.0.1:11434/api/tags",
        "auth_type": "none",
    },
}

_PROVIDER_ALIASES: dict[str, str] = {
    "google": "gemini",
    "vertex": "google_vertex",
    "vertexai": "google_vertex",
    "gcp": "google_vertex",
    "aws": "aws_bedrock",
    "amazon": "aws_bedrock",
    "bedrock": "aws_bedrock",
    "vast": "vastai",
    "vast.ai": "vastai",
    "lambda": "lambda_labs",
    "lambdalabs": "lambda_labs",
    "x-ai": "xai",
    "hf": "huggingface",
    "hf-router": "huggingface",
    "pplx": "perplexity",
    "nim": "nvidia",
    "vllm": "lmstudio",
    "llamacpp": "lmstudio",
}

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _get_dpapi_functions() -> tuple[Any, Any, Any]:
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI is only available on Windows.")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptProtectData.restype = ctypes.wintypes.BOOL

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DATA_BLOB),
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.wintypes.DWORD,
        ctypes.POINTER(_DATA_BLOB),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.wintypes.BOOL

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32.CryptProtectData, crypt32.CryptUnprotectData, kernel32.LocalFree


def dpapi_encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string using Windows DPAPI and return base64 ciphertext."""
    raw_bytes = plaintext.encode("utf-8")
    if not raw_bytes:
        raise ValueError("Cannot encrypt an empty secret.")
    protect_fn, _, local_free = _get_dpapi_functions()
    buf = (ctypes.c_ubyte * len(raw_bytes)).from_buffer_copy(raw_bytes)
    in_blob = _DATA_BLOB(len(raw_bytes), buf)
    out_blob = _DATA_BLOB()
    ok = protect_fn(
        ctypes.byref(in_blob),
        "SabrinaProviderKeyVault",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        cipher_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            local_free(out_blob.pbData)
    return base64.b64encode(cipher_bytes).decode("ascii")


def dpapi_decrypt(b64_ciphertext: str) -> str:
    """Decrypt base64 Windows DPAPI ciphertext back into a UTF-8 string."""
    cipher_bytes = base64.b64decode(b64_ciphertext.encode("ascii"))
    if not cipher_bytes:
        raise ValueError("Ciphertext is empty.")
    _, unprotect_fn, local_free = _get_dpapi_functions()
    buf = (ctypes.c_ubyte * len(cipher_bytes)).from_buffer_copy(cipher_bytes)
    in_blob = _DATA_BLOB(len(cipher_bytes), buf)
    out_blob = _DATA_BLOB()
    ok = unprotect_fn(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        plain_bytes = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        if out_blob.pbData:
            local_free(out_blob.pbData)
    return plain_bytes.decode("utf-8")


def mask_key(raw: str) -> str:
    """Return a masked representation of an API key, never exposing the full secret."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= 8:
        return "••••••••"
    return f"{cleaned[:4]}...{cleaned[-4:]}"


def _scrub_secret(text: str, secret: str | None) -> str:
    if not text:
        return ""
    if secret and secret.strip():
        return text.replace(secret.strip(), "***")
    return text


def _default_fetch_json(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 6.0,
) -> Any:
    req_headers = {
        "User-Agent": "sabrina-swarm-console/1.0",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    req = Request(url, headers=req_headers)
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_probed_payload(provider: str, payload: Any) -> list[dict[str, Any]]:
    """Normalize a live provider model catalog payload into canonical model records."""
    models: list[dict[str, Any]] = []

    if provider == "openrouter":
        for item in normalize_openrouter_catalog(payload):
            item["source"] = "live_probe"
            item["unlocked"] = True
            models.append(item)
        return models

    if provider == "gemini":
        raw_list = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(raw_list, list):
            return []
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            raw_name = str(entry.get("name") or "").strip()
            model_id = raw_name.removeprefix("models/")
            if not model_id:
                continue
            display = str(entry.get("displayName") or model_id).strip()
            norm = normalize_model_record(
                {
                    "id": model_id,
                    "name": display,
                    "provider": "gemini",
                    "context_window": entry.get("inputTokenLimit"),
                    "max_output_tokens": entry.get("outputTokenLimit"),
                    "source": "live_probe",
                }
            )
            if norm:
                norm["unlocked"] = True
                models.append(norm)
        return models

    if provider == "ollama":
        raw_list = payload.get("models") if isinstance(payload, dict) else None
        if not isinstance(raw_list, list):
            return []
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or entry.get("model") or "").strip()
            if not name:
                continue
            norm = normalize_model_record(
                {
                    "id": f"ollama/{name}",
                    "name": name,
                    "provider": "ollama",
                    "source": "live_probe",
                }
            )
            if norm:
                norm["unlocked"] = True
                models.append(norm)
        return models

    # GPU Pod / Cloud compute providers that return pods/instances/offers rather than LLM model lists
    if provider == "runpod":
        items = payload if isinstance(payload, list) else (payload.get("data") if isinstance(payload, dict) else [])
        if isinstance(items, list):
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                pod_id = str(entry.get("id") or entry.get("name") or "").strip()
                if not pod_id:
                    continue
                display = str(entry.get("name") or pod_id).strip()
                norm = normalize_model_record(
                    {
                        "id": f"runpod/{pod_id}",
                        "name": f"RunPod: {display}",
                        "provider": "runpod",
                        "source": "live_probe",
                    }
                )
                if norm:
                    norm["unlocked"] = True
                    models.append(norm)
        return models

    if provider == "lambda_labs" and isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        for sku_id, entry in payload["data"].items():
            if not isinstance(entry, dict):
                continue
            it = entry.get("instance_type") if isinstance(entry.get("instance_type"), dict) else {}
            display = str(it.get("description") or sku_id).strip()
            norm = normalize_model_record(
                {
                    "id": f"lambda_labs/{sku_id}",
                    "name": f"Lambda GPU: {display}",
                    "provider": "lambda_labs",
                    "source": "live_probe",
                }
            )
            if norm:
                norm["unlocked"] = True
                models.append(norm)
        return models

    if provider == "vastai" and isinstance(payload, dict) and isinstance(payload.get("instances"), list):
        for entry in payload["instances"]:
            if not isinstance(entry, dict):
                continue
            inst_id = str(entry.get("id") or "").strip()
            if not inst_id:
                continue
            gpu = str(entry.get("gpu_name") or "GPU Pod").strip()
            norm = normalize_model_record(
                {
                    "id": f"vastai/{inst_id}",
                    "name": f"Vast.ai: {gpu} (#{inst_id})",
                    "provider": "vastai",
                    "source": "live_probe",
                }
            )
            if norm:
                norm["unlocked"] = True
                models.append(norm)
        return models

    # Cohere: {"models": [...]}
    if provider == "cohere" and isinstance(payload, dict) and isinstance(payload.get("models"), list):
        raw_list = payload["models"]
    elif isinstance(payload, list):
        raw_list = payload
    elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
        raw_list = payload["data"]
    else:
        return []

    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        raw_id = str(entry.get("id") or entry.get("name") or "").strip()
        if not raw_id:
            continue
        model_id = f"{provider}/{raw_id}" if not raw_id.lower().startswith(f"{provider}/") else raw_id
        display = str(entry.get("display_name") or entry.get("name") or raw_id).strip()
        norm = normalize_model_record(
            {
                "id": model_id,
                "name": display,
                "provider": provider,
                "context_window": entry.get("context_length") or entry.get("max_tokens"),
                "source": "live_probe",
            }
        )
        if norm:
            norm["unlocked"] = True
            models.append(norm)
    return models


class ProviderKeyVault:
    """Encrypted API key vault backed by Windows DPAPI and atomic JSON persistence."""

    def __init__(
        self,
        vault_path: str | Path | None = None,
        fetch_json: Callable[..., Any] | None = None,
    ) -> None:
        raw_path = vault_path if vault_path is not None else (Path.home() / ".sabrina" / "vault.json")
        self.vault_path: Path = guard_safe_path(raw_path)
        self._fetch_json = fetch_json if fetch_json is not None else _default_fetch_json
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._load_state()

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "encryption": "dpapi",
            "keys": {},
            "configs": {},
            "probes": {},
            "ignored_env": [],
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.vault_path.exists():
            return self._empty_state()
        try:
            raw = json.loads(self.vault_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._empty_state()
        if not isinstance(raw, dict):
            return self._empty_state()
        keys = raw.get("keys")
        configs = raw.get("configs")
        probes = raw.get("probes")
        ignored_env = raw.get("ignored_env")
        return {
            "version": 1,
            "encryption": "dpapi",
            "keys": dict(keys) if isinstance(keys, dict) else {},
            "configs": dict(configs) if isinstance(configs, dict) else {},
            "probes": dict(probes) if isinstance(probes, dict) else {},
            "ignored_env": [str(p).strip().lower() for p in ignored_env if str(p).strip()]
            if isinstance(ignored_env, list)
            else [],
        }

    def _save_state(self) -> None:
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self._state, indent=2)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.vault_path.parent),
            prefix="vault_",
            suffix=".tmp",
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_path, self.vault_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    @staticmethod
    def _validate_provider(provider: str) -> str:
        norm = (provider or "").strip().lower()
        norm = _PROVIDER_ALIASES.get(norm, norm)
        if norm not in PROVIDER_METADATA:
            raise ValueError(
                f"Unsupported provider {provider!r}. Expected one of: {', '.join(SUPPORTED_PROVIDERS)}."
            )
        return norm

    @staticmethod
    def _is_placeholder_key(val: str) -> bool:
        upper = val.strip().upper()
        if not upper:
            return True
        if upper.startswith(("YOUR_", "REPLACE_", "PLACEHOLDER", "DUMMY_", "EXAMPLE_")):
            return True
        return upper in {"CHANGEME", "NONE", "NULL", "UNDEFINED", "TODO"}

    def _get_env_key(self, provider: str) -> str | None:
        if provider in (self._state.get("ignored_env") or []):
            return None
        meta = PROVIDER_METADATA.get(provider)
        if not meta:
            return None
        for env_name in meta["env_vars"]:
            val = os.environ.get(env_name, "").strip()
            if val and not self._is_placeholder_key(val):
                return val
        return None

    def get_key(self, provider: str, include_env: bool = True) -> str | None:
        """Retrieve a decrypted key from vault, falling back to environment variables."""
        prov = self._validate_provider(provider)
        with self._lock:
            entry = self._state["keys"].get(prov)
            if isinstance(entry, dict) and entry.get("ciphertext"):
                try:
                    return dpapi_decrypt(str(entry["ciphertext"]))
                except Exception:
                    return None
        if include_env:
            return self._get_env_key(prov)
        return None

    def get_provider_config(self, provider: str) -> dict[str, str]:
        """Return non-secret configuration fields for `provider` merged with defaults."""
        prov = self._validate_provider(provider)
        meta = PROVIDER_METADATA[prov]
        defaults: dict[str, str] = {}
        for field in meta.get("config_fields") or ():
            defaults[str(field["key"])] = str(field.get("default") or "")
        with self._lock:
            stored = self._state.get("configs", {}).get(prov)
            if isinstance(stored, dict):
                for k, v in stored.items():
                    if v is not None:
                        defaults[str(k)] = str(v).strip()
        return defaults

    def set_provider_config(self, provider: str, config: dict[str, Any]) -> dict[str, Any]:
        """Persist non-secret configuration fields (region, project_id, endpoint_url, etc.) for `provider`."""
        prov = self._validate_provider(provider)
        if not isinstance(config, dict):
            raise ValueError("Provider config must be a dictionary.")
        meta = PROVIDER_METADATA[prov]
        allowed_keys = {str(f["key"]) for f in (meta.get("config_fields") or ())}
        with self._lock:
            configs = self._state.setdefault("configs", {})
            existing = dict(configs.get(prov) or {}) if isinstance(configs.get(prov), dict) else {}
            for k, v in config.items():
                key = str(k).strip()
                if not key:
                    continue
                if allowed_keys and key not in allowed_keys:
                    continue
                val = str(v or "").strip()
                if val:
                    existing[key] = val
                else:
                    existing.pop(key, None)
            configs[prov] = existing
            self._save_state()
            return self._provider_status(prov)

    def _provider_status(self, provider: str) -> dict[str, Any]:
        meta = PROVIDER_METADATA[provider]
        entry = self._state["keys"].get(provider)
        probe = self._state["probes"].get(provider)
        if not isinstance(probe, dict):
            probe = {}

        configured = False
        unlocked = False
        source = "none"
        masked = ""
        updated_at: str | None = None
        last_error: str | None = probe.get("last_error")

        if isinstance(entry, dict) and entry.get("ciphertext"):
            updated_at = entry.get("updated_at")
            try:
                raw_key = dpapi_decrypt(str(entry["ciphertext"]))
                configured = True
                unlocked = True
                source = "vault"
                masked = mask_key(raw_key)
            except Exception as exc:
                configured = False
                unlocked = False
                source = "none"
                last_error = f"DPAPI decrypt failed: {exc}"
        else:
            env_key = self._get_env_key(provider)
            if env_key:
                configured = True
                unlocked = True
                source = "env"
                masked = mask_key(env_key)
            elif provider == "ollama":
                configured = True
                unlocked = True
                source = "local"
                masked = "local"

        probed_models = probe.get("models")
        unlocked_count = int(probe.get("unlocked_count", len(probed_models) if isinstance(probed_models, list) else 0))
        config_fields = [dict(f) for f in (meta.get("config_fields") or ())]
        return {
            "provider": provider,
            "label": meta["label"],
            "category": str(meta.get("category") or "model_api"),
            "configured": configured,
            "unlocked": unlocked,
            "source": source,
            "masked_key": masked,
            "updated_at": updated_at,
            "verified": bool(probe.get("verified", False)),
            "unlocked_count": unlocked_count,
            "last_error": last_error,
            "config": self.get_provider_config(provider),
            "config_fields": config_fields,
        }

    def list_providers(self) -> dict[str, Any]:
        """Return masked status for all supported providers."""
        with self._lock:
            providers = [self._provider_status(prov) for prov in SUPPORTED_PROVIDERS]
            unlocked_providers = [p["provider"] for p in providers if p["unlocked"]]
            return {
                "providers": providers,
                "unlocked_providers": unlocked_providers,
                "encryption": "dpapi",
            }

    def set_key(
        self,
        provider: str,
        api_key: str,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Encrypt and persist an API key (and optional non-secret config) for `provider`, returning masked status."""
        prov = self._validate_provider(provider)
        cleaned = (api_key or "").strip()
        if not cleaned:
            raise ValueError(f"API key for {prov} cannot be empty.")

        ciphertext = dpapi_encrypt(cleaned)
        with self._lock:
            self._state["keys"][prov] = {
                "scheme": "dpapi",
                "ciphertext": ciphertext,
                "updated_at": _utc_now_iso(),
            }
            if isinstance(config, dict) and config:
                meta = PROVIDER_METADATA[prov]
                allowed_keys = {str(f["key"]) for f in (meta.get("config_fields") or ())}
                configs = self._state.setdefault("configs", {})
                existing = dict(configs.get(prov) or {}) if isinstance(configs.get(prov), dict) else {}
                for k, v in config.items():
                    key = str(k).strip()
                    if not key or (allowed_keys and key not in allowed_keys):
                        continue
                    val = str(v or "").strip()
                    if val:
                        existing[key] = val
                    else:
                        existing.pop(key, None)
                configs[prov] = existing
            ignored = [p for p in (self._state.get("ignored_env") or []) if p != prov]
            self._state["ignored_env"] = ignored
            # Reset stale error on new key entry
            if prov in self._state["probes"] and isinstance(self._state["probes"][prov], dict):
                self._state["probes"][prov]["last_error"] = None
                self._state["probes"][prov]["verified"] = False
            self._save_state()
            return self._provider_status(prov)

    def delete_key(self, provider: str) -> dict[str, Any]:
        """Delete stored key, cached probed models, and disconnect any env fallback for `provider`."""
        prov = self._validate_provider(provider)
        with self._lock:
            self._state["keys"].pop(prov, None)
            self._state["probes"].pop(prov, None)
            # If an environment variable exists for this provider, disconnect it so Remove truly locks it.
            meta = PROVIDER_METADATA.get(prov)
            has_raw_env = False
            if meta:
                for env_name in meta["env_vars"]:
                    val = os.environ.get(env_name, "").strip()
                    if val and not self._is_placeholder_key(val):
                        has_raw_env = True
                        break
            if has_raw_env:
                ignored = list(self._state.get("ignored_env") or [])
                if prov not in ignored:
                    ignored.append(prov)
                self._state["ignored_env"] = ignored
            self._save_state()
            return self.list_providers()

    def is_provider_unlocked(self, provider: str) -> bool:
        """Check whether a provider (including openrouter:* or gemma) is unlocked."""
        norm = (provider or "").strip().lower()
        if not norm:
            return False

        if norm.startswith("openrouter:"):
            sub = norm.split(":", 1)[1].strip()
            return self.is_provider_unlocked("openrouter") or (
                bool(sub) and self.is_provider_unlocked(sub)
            )

        if norm == "gemma":
            return self.is_provider_unlocked("gemini") or self.is_provider_unlocked("ollama")

        norm = _PROVIDER_ALIASES.get(norm, norm)
        if norm == "ollama":
            return True

        if norm not in PROVIDER_METADATA:
            return False

        return self.get_key(norm, include_env=True) is not None

    def _build_probe_request(self, provider: str, raw_key: str | None) -> tuple[str, dict[str, str]]:
        meta = PROVIDER_METADATA[provider]
        cfg = self.get_provider_config(provider)
        url = meta["endpoint"]
        if provider == "ollama":
            url = os.environ.get("SWARM_OLLAMA_TAGS_URL", url)
        else:
            custom_url = cfg.get("endpoint_url", "").strip()
            if custom_url:
                base = custom_url.rstrip("/")
                url = base if base.endswith("/models") else f"{base}/models"
            elif "{region}" in url or "{project_id}" in url:
                region = cfg.get("region") or ("us-central1" if provider == "google_vertex" else "us-east-1")
                project_id = cfg.get("project_id") or "default"
                url = url.format(region=region, project_id=project_id)

        auth_type = meta["auth_type"]
        headers: dict[str, str] = {"Accept": "application/json"}
        if auth_type == "x-goog-api-key" and raw_key:
            headers["x-goog-api-key"] = raw_key
        elif auth_type == "bearer" and raw_key:
            headers["Authorization"] = f"Bearer {raw_key}"
        elif auth_type == "anthropic" and raw_key:
            headers["x-api-key"] = raw_key
            headers["anthropic-version"] = "2023-06-01"
        if provider == "modal" and cfg.get("token_id"):
            headers["Modal-Key"] = cfg["token_id"]
            if raw_key:
                headers["Modal-Secret"] = raw_key
        return url, headers

    def probe_provider(
        self,
        provider: str,
        api_key: str | None = None,
        timeout: float = 6.0,
    ) -> dict[str, Any]:
        """Probe a provider's live model catalog endpoint and cache normalized models."""
        prov = self._validate_provider(provider)
        raw_key = api_key.strip() if api_key is not None else self.get_key(prov, include_env=True)

        if prov != "ollama" and not raw_key:
            err_msg = f"No API key configured for {prov}."
            with self._lock:
                self._state["probes"][prov] = {
                    "verified": False,
                    "unlocked_count": 0,
                    "models": [],
                    "last_error": err_msg,
                    "probed_at": _utc_now_iso(),
                }
                self._save_state()
            return {
                "ok": False,
                "provider": prov,
                "valid": False,
                "unlocked_count": 0,
                "models": [],
                "error": err_msg,
            }

        url, headers = self._build_probe_request(prov, raw_key)
        try:
            payload = self._fetch_json(url, headers=headers, timeout=timeout)
            models = _normalize_probed_payload(prov, payload)
            with self._lock:
                self._state["probes"][prov] = {
                    "verified": True,
                    "unlocked_count": len(models),
                    "models": models,
                    "last_error": None,
                    "probed_at": _utc_now_iso(),
                }
                self._save_state()
            return {
                "ok": True,
                "provider": prov,
                "valid": True,
                "unlocked_count": len(models),
                "models": models,
                "error": None,
            }
        except HTTPError as exc:
            err_msg = _scrub_secret(f"HTTP {exc.code}: {exc.reason}", raw_key)
        except (URLError, OSError, ValueError, TypeError) as exc:
            err_msg = _scrub_secret(str(exc), raw_key)
        except Exception as exc:
            err_msg = _scrub_secret(str(exc), raw_key)

        with self._lock:
            self._state["probes"][prov] = {
                "verified": False,
                "unlocked_count": 0,
                "models": [],
                "last_error": err_msg,
                "probed_at": _utc_now_iso(),
            }
            self._save_state()
        return {
            "ok": False,
            "provider": prov,
            "valid": False,
            "unlocked_count": 0,
            "models": [],
            "error": err_msg,
        }

    def probe_all_unlocked(self, timeout: float = 6.0) -> dict[str, Any]:
        """Probe all currently unlocked providers and return aggregated results."""
        status = self.list_providers()
        results: dict[str, dict[str, Any]] = {}
        for prov in status["unlocked_providers"]:
            results[prov] = self.probe_provider(prov, timeout=timeout)
        return {
            "ok": True,
            "results": results,
            **self.list_providers(),
        }

    def get_probed_models(self) -> list[dict[str, Any]]:
        """Return all cached live-probed model records across providers."""
        with self._lock:
            if getattr(self, "_probed_models", None):
                return [dict(item) for item in self._probed_models if isinstance(item, dict)]
            collected: list[dict[str, Any]] = []
            for prov in SUPPORTED_PROVIDERS:
                probe = self._state["probes"].get(prov)
                if not isinstance(probe, dict):
                    continue
                models = probe.get("models")
                if isinstance(models, list):
                    for item in models:
                        if isinstance(item, dict):
                            collected.append(dict(item))
            return collected
