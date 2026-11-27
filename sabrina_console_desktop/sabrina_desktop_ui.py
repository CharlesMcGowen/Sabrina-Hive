"""Native pywebview desktop shell for Sabrina Operator Console."""
from __future__ import annotations

import base64
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
import urllib.error
import urllib.request
from typing import Any

try:
    import webview  # type: ignore
except ImportError:  # pragma: no cover - headless unit test environments
    webview = None

FROZEN_DESKTOP = bool(getattr(sys, "frozen", False))
SABRINA_WORKSPACE = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SabrinaConsole" / "workspace"
    if FROZEN_DESKTOP else Path(r"c:\Users\charl\OneDrive\Documents\Sabrina")
)
if SABRINA_WORKSPACE.is_dir() and str(SABRINA_WORKSPACE) not in sys.path:
    sys.path.append(str(SABRINA_WORKSPACE))

from swarm_command.blackboard import SessionBlackboard
import re

from sabrina_console_desktop import release_review as release
from sabrina_console_desktop import home_depot_order, interface_privacy
try:
    from sabrina_console_desktop.tools import usage_ledger
except ImportError:  # launched from inside sabrina_console_desktop
    from tools import usage_ledger  # type: ignore[no-redef]
from sabrina_console_desktop.agent_browser_service import BrowserSession
from sabrina_console_desktop.agent_browser_service.fleet import FleetBrowserSession
from sabrina_console_desktop.agent_browser_service.visible import VisibleBrowserSession
from sabrina_console_desktop.agent_desktop_service import DesktopSession, available_windows
from sabrina_console_desktop.interactive_tools import create_interactive_session, list_interactive_backends
from sabrina_console_desktop.context_first_coordinator import is_model_cooled_down
from sabrina_console_desktop.windows_skill_runtime import MODE as WINDOWS_POWERSHELL_MODE
from sabrina_console_desktop.recovery_policy import RecoveryPolicy
from sabrina_console_desktop.recovery_worker_release import RecoveryWorkerReleases
from sabrina_console_desktop.desktop_browser_action import parse_browser_request
from swarm_command.role_binding import (
    RoleBindingConfig,
    RoleBindingResolutionError,
    infer_model_provider,
)
from swarm_command.router import SwarmCommand
from swarm_console.gemini_copilot import (
    DEFAULT_GEMINI_MODEL,
    GeminiCopilotError,
    analyze_swarm_state,
    get_copilot_status,
)
from swarm_console.registry import ModelSkillRegistry, default_skill_roots
from swarm_console.vault import ProviderKeyVault
from swarm_console.gpu_providers import SwarmGpuDeploymentManager
from swarm_console.harness_collective import HarnessPolicy, HarnessRegistry
from swarm_console.harness_updates import NATIVE_HARNESS_ID, HarnessUpdates
from swarm_console.workspace_agent import WorkspaceAgent
from swarm_console.workspace_sandbox import (
    DEFAULT_SANDBOX_ROOT,
    MUTATING_TOOLS,
    TOOL_INPUT_MODELS,
    VALID_POLICY_PROFILES,
    VALID_WORKSPACE_MODES,
    WorkspaceSandbox,
    WorkspaceSandboxError,
    create_sandbox_project as _create_sandbox_project,
)

ROOT_IMPORT = Path(__file__).resolve().parent
if str(ROOT_IMPORT) not in sys.path:
    sys.path.append(str(ROOT_IMPORT))

try:
    from sabrina_console_desktop.live_swarm_runner import LiveSwarmRunner
    from sabrina_console_desktop.followup_coordinator import FollowupCoordinator, IN_PROGRESS, MODE as FOLLOWUP_MODE
    from sabrina_console_desktop.join_cut_registry import JoinCutRegistry
    from sabrina_console_desktop.connector_registry import ConnectorRegistry
    from sabrina_console_desktop.automation_engine import AutomationEngine
    from sabrina_console_desktop.workspace_topology_index import (
        get_machine_and_catalog_snapshot,
        refresh_and_search_machine_catalog,
    )
except ImportError:  # pragma: no cover - direct script execution
    from live_swarm_runner import LiveSwarmRunner  # type: ignore[no-redef]
    from followup_coordinator import FollowupCoordinator, IN_PROGRESS, MODE as FOLLOWUP_MODE  # type: ignore[no-redef]
    from join_cut_registry import JoinCutRegistry  # type: ignore[no-redef]
    from connector_registry import ConnectorRegistry  # type: ignore[no-redef]
    from automation_engine import AutomationEngine  # type: ignore[no-redef]
    from workspace_topology_index import (  # type: ignore[no-redef]
        get_machine_and_catalog_snapshot,
        refresh_and_search_machine_catalog,
    )

try:
    from sabrina_console_desktop.tools.promotion_ledger import PromotionLedger
    from sabrina_console_desktop.tools.signal_promotion_gate import SignalGate, KIND_SIGNAL, KIND_CANDIDATE
except ImportError:  # pragma: no cover - direct script execution
    try:
        from tools.promotion_ledger import PromotionLedger  # type: ignore[no-redef]
        from tools.signal_promotion_gate import SignalGate, KIND_SIGNAL, KIND_CANDIDATE  # type: ignore[no-redef]
    except ImportError:  # pragma: no cover
        PromotionLedger = None  # type: ignore[assignment]
        SignalGate = None  # type: ignore[assignment]
        KIND_SIGNAL = "signal"
        KIND_CANDIDATE = "signal_candidate"

try:
    from sabrina_console_desktop.swarm_schema_audit import (
        SchemaAuditResult,
        audit_swarm_file,
        audit_swarm_payload,
        make_gemini_flash_auditor,
        parse_swarm_input,
        serialize_swarm_markdown,
        validate_swarm_payload,
    )
except ImportError:  # pragma: no cover - direct script execution
    from swarm_schema_audit import (  # type: ignore[no-redef]
        SchemaAuditResult,
        audit_swarm_file,
        audit_swarm_payload,
        make_gemini_flash_auditor,
        parse_swarm_input,
        serialize_swarm_markdown,
        validate_swarm_payload,
    )

try:
    from tools.console_remote import call_console_backend
    from tools.sabrina_console_swarm import (
        decide_pending_item,
        force_release_desktop_lease,
        get_live_swarm_state,
        swap_swarm_pipeline_roles,
    )
except ImportError:  # pragma: no cover - optional legacy tools package
    def call_console_backend(_env_key: str, _path: str, fallback: Any, timeout: float = 1.5) -> Any:
        return fallback()

    def decide_pending_item(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": False, "error": "tools.sabrina_console_swarm unavailable"}

    def force_release_desktop_lease(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"released": False}

    def get_live_swarm_state(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "offline", "members": []}

    def swap_swarm_pipeline_roles(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"new_mode": "default"}

try:
    from sabrina_console_desktop.tools.capability_registry import (
        CapabilityRegistry,
        DEFAULT_CAPABILITIES,
    )
except ImportError:  # pragma: no cover - direct script execution
    try:
        from tools.capability_registry import (  # type: ignore[no-redef]
            CapabilityRegistry,
            DEFAULT_CAPABILITIES,
        )
    except ImportError:  # pragma: no cover
        CapabilityRegistry = None  # type: ignore[assignment,misc]
        DEFAULT_CAPABILITIES = ()  # type: ignore[assignment]

import time

ROOT = Path(__file__).resolve().parent


def _read_desktop_html(path: Path) -> str:
    html = path.read_text(encoding="utf-8")
    asset = path.with_name("santa-workshop.jpg")
    legacy_art = 'url("santa-workshop.jpg")'
    if legacy_art in html and asset.is_file():
        image = base64.b64encode(asset.read_bytes()).decode("ascii")
        html = html.replace(legacy_art, f'url("data:image/jpeg;base64,{image}")')
    return html
API_8087 = os.environ.get("SABRINA_SWARM_CONTROL_API_BASE", "http://127.0.0.1:8087").rstrip("/")
API_8095 = os.environ.get("SABRINA_SWARM_API_BASE", "http://127.0.0.1:8095").rstrip("/")
OLLAMA_BASE = os.environ.get(
    "SABRINA_OLLAMA_BASE_URL", "http://127.0.0.1:11434" if FROZEN_DESKTOP else "http://ego.local:11434",
).rstrip("/")
ASSETS_DIR = ROOT / "swarm_console" / "assets" if FROZEN_DESKTOP else Path(r"c:\Users\charl\OneDrive\Documents\Sabrina\swarm_console\assets")
SWARM_SYNC_FILE = ROOT / "data" / "live_antigravity_swarm.json"


def _http_json(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 3.0,
    headers: dict[str, str] | None = None,
    direct: bool = False,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, method=method, headers=req_headers)
    try:
        if direct:
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None

            response = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=timeout)
        else:
            response = urllib.request.urlopen(req, timeout=timeout)
        with response as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw_body = ""
        try:
            raw_body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            raw_body = ""
        detail = ""
        if raw_body:
            try:
                parsed = json.loads(raw_body)
                if isinstance(parsed, dict):
                    err_obj = parsed.get("error")
                    if isinstance(err_obj, dict) and err_obj.get("message"):
                        msg_val = str(err_obj["message"])
                        code_val = str(err_obj.get("code") or err_obj.get("status") or "").strip()
                        detail = f"{code_val}: {msg_val}" if (code_val and code_val.lower() not in msg_val.lower()) else msg_val
                    elif parsed.get("message"):
                        detail = str(parsed["message"])
            except Exception:
                pass
            if not detail:
                detail = raw_body.strip()[:240]
        if not detail:
            detail = str(getattr(exc, "reason", "") or exc)
        if headers:
            for hv in headers.values():
                secret = str(hv or "").strip()
                if secret.lower().startswith("bearer "):
                    secret = secret[7:].strip()
                if len(secret) >= 6:
                    detail = detail.replace(secret, "[REDACTED]")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc


def _extract_gemini_interactions_text(data: Any) -> str:
    """Extract output text from a Gemini Interactions API response (SDK object or dict)."""
    if isinstance(data, dict):
        direct = str(data.get("output_text") or "").strip()
        if direct:
            return direct
        parts: list[str] = []
        for step in data.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if step.get("type") == "model_output":
                for item in step.get("content") or []:
                    if isinstance(item, dict) and item.get("text"):
                        parts.append(str(item["text"]))
        if parts:
            return "".join(parts).strip()
        for out in data.get("outputs") or []:
            if isinstance(out, dict) and out.get("text"):
                parts.append(str(out["text"]))
        return "".join(parts).strip()

    direct = str(getattr(data, "output_text", "") or "").strip()
    if direct:
        return direct
    parts = []
    for step in getattr(data, "steps", None) or []:
        stype = getattr(step, "type", None)
        if stype == "model_output":
            for item in getattr(step, "content", None) or []:
                txt = getattr(item, "text", None)
                if txt:
                    parts.append(str(txt))
    return "".join(parts).strip()


def _wrap_pcm16_as_wav(pcm_bytes: bytes, sample_rate: int = 24000, channels: int = 1) -> bytes:
    """Wrap raw 16-bit PCM audio bytes into a valid RIFF WAV container if not already WAV."""
    if pcm_bytes.startswith(b"RIFF") and b"WAVE" in pcm_bytes[:16]:
        return pcm_bytes
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def _extract_gemini_interactions_media(data: Any, kind: str) -> tuple[bytes, str]:
    """Extract binary image or audio bytes + mime_type from a Gemini Interactions API response."""
    want_audio = kind == "audio"

    def _decode_blob(raw_data: Any, mime_val: Any) -> tuple[bytes, str] | None:
        if raw_data is None:
            return None
        mime_str = str(mime_val or ("audio/wav" if want_audio else "image/png")).strip()
        if isinstance(raw_data, (bytes, bytearray)):
            b = bytes(raw_data)
            return (b, mime_str) if b else None
        s = str(raw_data).strip()
        if not s:
            return None
        if s.startswith("data:") and "," in s:
            hdr, s = s.split(",", 1)
            if ";" in hdr and ":" in hdr:
                mime_str = hdr.split(":", 1)[1].split(";", 1)[0].strip() or mime_str
        try:
            decoded = base64.b64decode(s)
            return (decoded, mime_str) if decoded else None
        except Exception:
            return None

    attr_name = "output_audio" if want_audio else "output_image"
    if isinstance(data, dict):
        direct_obj = data.get(attr_name)
        if isinstance(direct_obj, dict):
            res = _decode_blob(direct_obj.get("data") or direct_obj.get("bytes"), direct_obj.get("mime_type") or direct_obj.get("mimeType"))
            if res:
                return res
        elif isinstance(direct_obj, list) and direct_obj:
            first = direct_obj[0]
            if isinstance(first, dict):
                res = _decode_blob(first.get("data") or first.get("bytes"), first.get("mime_type") or first.get("mimeType"))
                if res:
                    return res
        for container in (data.get("outputs") or [], [item for step in (data.get("steps") or []) if isinstance(step, dict) for item in (step.get("content") or [])]):
            for item in container:
                if not isinstance(item, dict):
                    continue
                itype = str(item.get("type") or "").lower()
                mime_cand = str(item.get("mime_type") or item.get("mimeType") or "").lower()
                if (want_audio and ("audio" in itype or mime_cand.startswith("audio/"))) or (not want_audio and ("image" in itype or mime_cand.startswith("image/"))):
                    res = _decode_blob(item.get("data") or (item.get("inline_data") or {}).get("data"), mime_cand)
                    if res:
                        return res
                inline = item.get("inlineData") or item.get("inline_data")
                if isinstance(inline, dict):
                    imime = str(inline.get("mimeType") or inline.get("mime_type") or "").lower()
                    if (want_audio and imime.startswith("audio/")) or (not want_audio and imime.startswith("image/")):
                        res = _decode_blob(inline.get("data"), imime)
                        if res:
                            return res
        return b"", ""

    direct_attr = getattr(data, attr_name, None)
    if direct_attr is not None:
        target_item = direct_attr[0] if isinstance(direct_attr, (list, tuple)) and direct_attr else direct_attr
        res = _decode_blob(
            getattr(target_item, "data", None) or getattr(target_item, "image_bytes", None) or getattr(target_item, "audio_bytes", None),
            getattr(target_item, "mime_type", None),
        )
        if res:
            return res
    for step in getattr(data, "outputs", None) or getattr(data, "steps", None) or []:
        items = getattr(step, "content", None) or [step]
        for item in items:
            imime = str(getattr(item, "mime_type", "") or "").lower()
            itype = str(getattr(item, "type", "") or "").lower()
            if (want_audio and ("audio" in itype or imime.startswith("audio/"))) or (not want_audio and ("image" in itype or imime.startswith("image/"))):
                res = _decode_blob(getattr(item, "data", None), imime)
                if res:
                    return res
    return b"", ""


def _call_gemini_interactions_media(
    model_id: str,
    prompt: str,
    api_key: str,
    *,
    kind: str = "image",
    aspect_ratio: str = "16:9",
    voice: str = "Puck",
) -> tuple[bytes, str]:
    """Generate image or TTS audio bytes via Gemini Interactions API (`client.interactions.create`)."""
    clean_key = str(api_key or "").strip().strip("'\"")
    bare_model = str(model_id or "").strip()
    for prefix in ("gemini/", "models/"):
        if bare_model.startswith(prefix):
            bare_model = bare_model[len(prefix) :]
    if bare_model in ("gemini-neo", "gemini-neo-orchestrator"):
        bare_model = "gemini-4-neon"

    cfg: dict[str, Any] = (
        {"speech_config": {"voice": voice}}
        if kind == "audio"
        else {"image_config": {"aspect_ratio": aspect_ratio}}
    )
    response_modalities = ["AUDIO"] if kind == "audio" else ["IMAGE"]

    try:
        from google import genai  # type: ignore

        try:
            from google.genai import types as genai_types  # type: ignore

            opts_cls = getattr(genai_types, "HttpOptions", None)
            retry_cls = getattr(genai_types, "HttpRetryOptions", None)
            http_opts = (
                opts_cls(timeout=45_000, retry_options=retry_cls(attempts=1))
                if (opts_cls is not None and retry_cls is not None)
                else {"timeout": 45_000}
            )
            client = genai.Client(api_key=clean_key, http_options=http_opts)
        except Exception:
            client = genai.Client(api_key=clean_key)

        if hasattr(client, "interactions"):
            interaction = client.interactions.create(
                model=bare_model,
                input=prompt,
                config=cfg,
            )
            raw_bytes, mime = _extract_gemini_interactions_media(interaction, kind)
            if raw_bytes:
                if kind == "audio" and ("pcm" in mime.lower() or "l16" in mime.lower() or not raw_bytes.startswith(b"RIFF")):
                    raw_bytes, mime = _wrap_pcm16_as_wav(raw_bytes), "audio/wav"
                return raw_bytes, mime
    except ImportError:
        pass
    except Exception as sdk_exc:
        err_str = str(sdk_exc)
        if "400" not in err_str and "404" not in err_str and "unexpected keyword" not in err_str.lower():
            raise

    payload: dict[str, Any] = {
        "model": bare_model,
        "input": prompt,
        "config": {**cfg, "response_modalities": response_modalities},
    }
    raw_bytes = b""
    mime = ""
    try:
        data = _http_json(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            method="POST",
            payload=payload,
            headers={"x-goog-api-key": clean_key},
            timeout=35.0,
        )
        raw_bytes, mime = _extract_gemini_interactions_media(data, kind)
    except RuntimeError as exc:
        err_s = str(exc)
        if not any(code in err_s for code in ("HTTP 400", "HTTP 404", "INVALID_ARGUMENT", "NOT_FOUND")):
            raise

    if not raw_bytes:
        # Stdlib generateContent multimodal fallback for Gemini image & TTS models when google-genai SDK is absent
        gen_cfg: dict[str, Any] = (
            {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice or "Puck"}}},
            }
            if kind == "audio"
            else {"responseModalities": ["TEXT", "IMAGE"]}
        )
        gc_data = _http_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{bare_model}:generateContent",
            method="POST",
            payload={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": gen_cfg,
            },
            headers={"x-goog-api-key": clean_key},
            timeout=35.0,
        )
        for cand in gc_data.get("candidates") or []:
            parts = ((cand.get("content") or {}).get("parts") or []) if isinstance(cand, dict) else []
            for p in parts:
                if not isinstance(p, dict):
                    continue
                inl = p.get("inlineData") or p.get("inline_data")
                if isinstance(inl, dict) and inl.get("data"):
                    p_mime = str(inl.get("mimeType") or inl.get("mime_type") or ("audio/wav" if kind == "audio" else "image/png"))
                    if (kind == "audio" and p_mime.lower().startswith("audio/")) or (kind != "audio" and p_mime.lower().startswith("image/")):
                        raw_bytes = base64.b64decode(str(inl["data"]))
                        mime = p_mime
                        break
            if raw_bytes:
                break

    if raw_bytes and kind == "audio" and ("pcm" in mime.lower() or "l16" in mime.lower() or not raw_bytes.startswith(b"RIFF")):
        raw_bytes, mime = _wrap_pcm16_as_wav(raw_bytes), "audio/wav"
    return raw_bytes, mime


def _call_gemini_interactions(
    model_id: str,
    user_prompt: str,
    api_key: str,
    system_prompt: str = "",
    *,
    attachments: list[Path] | None = None,
    tools: list[dict[str, Any]] | None = None,
    usage_out: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
) -> str:
    """Invoke Gemini via Interactions API (SDK -> HTTP) with single generateContent fallback for non-Interactions models.
    When usage_out is given, its "finish_reason" is set to the provider status: "incomplete" means cut off."""
    clean_key = str(api_key or "").strip().strip("'\"")
    bare_model = str(model_id or "").strip()
    for prefix in ("gemini/", "models/"):
        if bare_model.startswith(prefix):
            bare_model = bare_model[len(prefix) :]
    if bare_model in ("gemini-neo", "gemini-neo-orchestrator"):
        bare_model = "gemini-4-neon"

    mime_by_ext = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    inline_parts: list[dict[str, str]] = []
    for att in (attachments or [])[:4]:
        try:
            p_att = Path(att)
            if p_att.is_file() and 0 < p_att.stat().st_size <= 8_000_000:
                m_type = mime_by_ext.get(p_att.suffix.lower())
                if m_type:
                    inline_parts.append({
                        "mime_type": m_type,
                        "data": base64.b64encode(p_att.read_bytes()).decode("ascii"),
                    })
        except Exception:
            continue

    # 1. Try google.genai SDK Interactions API if available (bounded timeout, no multi-minute 429 retry hang)
    try:
        from google import genai  # type: ignore

        http_opts: Any = {"timeout": 25_000}
        try:
            from google.genai import types as genai_types  # type: ignore

            retry_cls = getattr(genai_types, "HttpRetryOptions", None)
            opts_cls = getattr(genai_types, "HttpOptions", None)
            if opts_cls is not None:
                if retry_cls is not None:
                    http_opts = opts_cls(timeout=25_000, retry_options=retry_cls(attempts=1))
                else:
                    http_opts = opts_cls(timeout=25_000)
        except Exception:
            pass
        try:
            client = genai.Client(api_key=clean_key, http_options=http_opts)
        except TypeError:
            client = genai.Client(api_key=clean_key)
        if hasattr(client, "interactions"):
            sdk_input: Any = user_prompt
            if inline_parts:
                sdk_input = [
                    *[{"type": "image", "mime_type": ip["mime_type"], "data": ip["data"]} for ip in inline_parts],
                    {"type": "text", "text": user_prompt},
                ]
            sdk_kwargs: dict[str, Any] = {"model": bare_model, "input": sdk_input}
            if system_prompt:
                sdk_kwargs["system_instruction"] = system_prompt
            if tools:
                sdk_kwargs["tools"] = tools
            if max_output_tokens:
                sdk_kwargs["generation_config"] = {"max_output_tokens": int(max_output_tokens)}
            interaction = client.interactions.create(**sdk_kwargs)
            if usage_out is not None:
                usage_out["finish_reason"] = str(getattr(interaction, "status", "") or "")
            text = _extract_gemini_interactions_text(interaction)
            if text:
                usage = getattr(interaction, "usage", None)
                if usage_out is not None and usage is not None:
                    usage_out.update(usage.model_dump() if hasattr(usage, "model_dump") else (usage if isinstance(usage, dict) else {}))
                return text
    except ImportError:
        pass
    except Exception as sdk_exc:
        err_str = str(sdk_exc)
        if "400" not in err_str and "404" not in err_str:
            raise

    # 2. Stdlib HTTP POST to Gemini Interactions API
    http_input: Any = (
        [
            *[{"type": "image", "mime_type": ip["mime_type"], "data": ip["data"]} for ip in inline_parts],
            {"type": "text", "text": user_prompt},
        ]
        if inline_parts
        else user_prompt
    )
    payload: dict[str, Any] = {"model": bare_model, "input": http_input}
    if system_prompt:
        payload["system_instruction"] = system_prompt
    if tools:
        payload["tools"] = tools
    if max_output_tokens:
        payload["generation_config"] = {"max_output_tokens": int(max_output_tokens)}
    try:
        data = _http_json(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            method="POST",
            payload=payload,
            headers={"x-goog-api-key": clean_key},
            timeout=15.0 if (inline_parts or tools) else 5.0,
        )
        if usage_out is not None:
            usage_out.update(data.get("usage") or {})
            usage_out["finish_reason"] = str(data.get("status") or "")
        return _extract_gemini_interactions_text(data)
    except RuntimeError as exc:
        err_msg = str(exc)
        if not (err_msg.startswith("HTTP 400:") or err_msg.startswith("HTTP 404:")):
            raise
        # Single fallback to generateContent for non-Interactions models (e.g. gemma-* or legacy probed IDs)
        gc_parts: list[dict[str, Any]] = [
            {"inlineData": {"mimeType": ip["mime_type"], "data": ip["data"]}} for ip in inline_parts
        ]
        gc_parts.append({"text": user_prompt})
        gc_payload: dict[str, Any] = {"contents": [{"parts": gc_parts}]}
        if system_prompt:
            gc_payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}
        gc_data = _http_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{bare_model}:generateContent",
            method="POST",
            payload=gc_payload,
            headers={"x-goog-api-key": clean_key},
            timeout=15.0 if inline_parts else 5.0,
        )
        candidates = gc_data.get("candidates") or []
        if usage_out is not None:
            usage_out["finish_reason"] = str((candidates[0] if candidates else {}).get("finishReason") or "")
            usage = gc_data.get("usageMetadata") or {}
            usage_out.update(
                total_input_tokens=usage.get("promptTokenCount"),
                total_output_tokens=usage.get("candidatesTokenCount"),
                total_thought_tokens=usage.get("thoughtsTokenCount"),
                total_tokens=usage.get("totalTokenCount"),
            )
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        return "".join(str(p.get("text") or "") for p in parts).strip()


def _clock_str(ts: float | None = None) -> str:
    t = time.localtime(ts if ts is not None else time.time())
    return time.strftime("%H:%M:%S", t)


def _data_uri(path: Path) -> str:
    if not path.is_file():
        return ""
    raw = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{raw}"


# Output ceiling for provider calls that do not pass a coordinator-set budget. It is a ceiling, not a target.
SWARM_DEFAULT_REPLY_TOKENS = 8192


class SabrinaDesktopBridge:
    """JS-to-Python bridge pulling live state from :8087, :8095, local SQLite, and live Antigravity swarm."""

    def __init__(
        self,
        role_bindings_path: Path | None = None,
        vault_path: Path | None = None,
        vault: Any | None = None,
        custom_models_path: Path | None = None,
        swarm_artifacts_dir: Path | None = None,
        spindown_delay_sec: float | None = None,
        step_delay_sec: float | None = None,
        llm_caller: Any | None = None,
        ui_html_path: Path | None = None,
        coordinator_only: bool = False,
    ) -> None:
        self.active_missions: list[dict[str, Any]] = []
        self.local_feed: list[dict[str, Any]] = []
        self.chat_log: list[dict[str, Any]] = []
        self._chat_log_revision = 0
        self._isolated_test_mode = (
            role_bindings_path is not None
            or vault_path is not None
            or vault is not None
            or custom_models_path is not None
            or swarm_artifacts_dir is not None
        )
        if not self._isolated_test_mode and not coordinator_only:
            # Import the optional SDK while the console initializes, before the
            # first chat needs it. This does not make a model or network request.
            threading.Thread(target=self._prewarm_workspace_sdk,
                             name="sabrina-chat-sdk-prewarm", daemon=True).start()
        effective_spindown = (
            float(spindown_delay_sec)
            if spindown_delay_sec is not None
            else (4.0 if self._isolated_test_mode else 8.0)
        )
        effective_step = (
            float(step_delay_sec)
            if step_delay_sec is not None
            else (0.35 if self._isolated_test_mode else 1.1)
        )
        self._remote_lock = threading.Lock()
        self._kraken_review_lock = threading.Lock()
        self._remote_refreshing = False
        self._remote_last_ts = 0.0
        self._remote_cache: dict[str, Any] = {
            "api_ok": False,
            "health": {"status": "local fallback active"},
            "jobs": [],
            "kraken": {
                "execution_authorized": False,
                "status": "PASS (Phase 4 Verified)",
                "contract": "H1-v2.1-PDLR",
                "verifier_receipt": "ee6d1830f09d7eab16e691865562c9c8b65954b1d80a0d2937b5a6835deea389",
                "artifacts_verified": 24,
            },
            "backend_swarm": {"status": "offline", "members": []},
            "ollama_models": [],
        }
        self._chat_history_seeded = False
        self._window: Any = None
        self._ui_html_path: Path = Path(ui_html_path) if ui_html_path is not None else (ROOT / "sabrina_desktop_ui.html")
        self._ui_loaded_mtime: float = self._read_ui_asset_mtime()
        self._ui_auto_reload = os.environ.get("SABRINA_UI_HOT_RELOAD", "").lower() in {"1", "true", "yes", "on"}
        self.workspace_sandbox: WorkspaceSandbox | None = None
        self.workspace_agent: WorkspaceAgent | None = None
        self._workspace_agent_lock = threading.RLock()
        self._workspace_agent_revision = ""
        self.workspace_mode: str = "approve_writes"
        self.workspace_policy_profile: str = "sandboxed_edit"
        self.allow_local_venv_fallback: bool = False
        self._recent_workspaces: list[str] = []
        self._default_workspace_root: Path = SABRINA_WORKSPACE if SABRINA_WORKSPACE.is_dir() else ROOT.parent
        self._chat_lock = threading.RLock()
        self._chat_job_lock = threading.RLock()
        self._chat_job: dict[str, Any] = {
            "running": False,
            "job_id": "",
            "mode": "",
            "target": "",
            "stage": "",
            "started_at": 0.0,
            "elapsed_sec": 0.0,
            "cancelled": False,
            "ok": True,
            "error": "",
        }
        self._chat_cancel_event: threading.Event | None = None
        self._provider_cooldown_lock = threading.Lock()
        self._provider_cooldowns: dict[str | tuple[str, str], float] = {}
        self._service_cooldowns: dict[tuple[str, str], float] = {}
        self._zero_quota_models: set[str] = set()
        self._unsupported_models: set[str] = set()
        self._latest_recommended_next: dict[str, Any] = {}
        control_dir = ROOT / "data" / "control"
        bindings_file = role_bindings_path or (control_dir / "role_bindings.json")
        self._swarm_display_path = Path(bindings_file).with_name("swarm_display.json")
        vault_file = vault_path or (control_dir / "provider_vault.json")
        custom_file = custom_models_path or (
            role_bindings_path.parent / "custom_models.json"
            if role_bindings_path is not None
            else (control_dir / "custom_models.json")
        )
        self.role_bindings = RoleBindingConfig(config_path=bindings_file)
        if role_bindings_path is None and not Path(bindings_file).is_file():
            try:
                for canonical_role in CANONICAL_ROLES:
                    self.role_bindings.set_binding(
                        canonical_role,
                        "gemini-3.5-flash-lite",
                        ["gemini-4-carbon", "gemini-4-hydrogen", "openai/gpt-5-mini"],
                    )
            except Exception:
                pass
        self._swarm_schema_path: Path = bindings_file.parent / "active_swarm_schema.json"
        self._workspace_state_path: Path = bindings_file.parent / "workspace_state.json"
        self._chat_log_path: Path = bindings_file.parent / "operator_chat_log.json"
        self._restore_operator_chat_log()
        self.vault = vault if vault is not None else ProviderKeyVault(vault_path=vault_file)
        skill_roots = [ROOT / "skills", ROOT / "skills" / "proposed"] + [
            p for p in default_skill_roots() if p != ROOT / "skills"
        ]
        self.registry = ModelSkillRegistry(
            skill_roots=[p for p in skill_roots if p.is_dir()],
            vault=self.vault,
            custom_models_path=custom_file,
        )
        gpu_deployments_file = (
            Path(vault_path).with_name("gpu_deployments.json")
            if vault_path is not None
            else (bindings_file.parent / "gpu_deployments.json")
        )
        self.gpu_deployments = SwarmGpuDeploymentManager(
            vault=self.vault,
            deployments_path=gpu_deployments_file,
            model_registry=self.registry,
        )
        if role_bindings_path is None and SWARM_SYNC_FILE.is_file():
            try:
                seed_data = json.loads(SWARM_SYNC_FILE.read_text(encoding="utf-8"))
                self.registry.set_runtime_fleet(seed_data.get("collective_models", []))
            except Exception:
                pass
        if swarm_artifacts_dir is not None:
            missions_dir: Path | None = Path(swarm_artifacts_dir)
        elif role_bindings_path is not None:
            missions_dir = Path(role_bindings_path).parent / "missions"
        elif vault_path is not None:
            missions_dir = Path(vault_path).parent / "missions"
        elif custom_models_path is not None:
            missions_dir = Path(custom_models_path).parent / "missions"
        else:
            missions_dir = None
        effective_llm_caller = (
            llm_caller
            if llm_caller is not None
            else (None if self._isolated_test_mode else self._call_swarm_llm)
        )
        self._interface_dispatches: list[dict[str, Any]] = []
        harness_state_dir = (
            SABRINA_WORKSPACE.parent / "control"
            if FROZEN_DESKTOP and role_bindings_path is None
            else Path(bindings_file).parent
        )
        self.harness_updates = HarnessUpdates(harness_state_dir / "harness_updates.sqlite3")
        self.followup_coordinator = FollowupCoordinator(harness_state_dir / "mission_followups")
        self._monitor_runs: list[dict[str, str]] = []
        self._monitor_attention: list[dict[str, Any]] = []
        self._monitor_runs_at = 0.0
        self.harness_registry = HarnessRegistry(
            ollama_base_url=OLLAMA_BASE,
            llm_caller=effective_llm_caller,
            updates=self.harness_updates,
        )
        runner_kwargs: dict[str, Any] = {
            "workspace_root": SABRINA_WORKSPACE if SABRINA_WORKSPACE.is_dir() else ROOT.parent,
            "artifacts_dir": missions_dir,
            "role_bindings": self.role_bindings,
            "unlocked_providers_fn": self._unlocked_providers,
            "spindown_delay_sec": effective_spindown,
            "step_delay_sec": effective_step,
            "llm_caller": effective_llm_caller,
            "recovery_policy_path": bindings_file.parent / "recovery_policy.json",
            "recovery_worker_releases": RecoveryWorkerReleases(bindings_file.parent / "recovery_workers"),
        }
        if llm_caller is None and not self._isolated_test_mode:
            runner_kwargs["recovery_llm_caller"] = self._call_swarm_recovery
        if "harness_registry" in inspect.signature(LiveSwarmRunner.__init__).parameters:
            runner_kwargs["harness_registry"] = self.harness_registry
        if "auto_spindown" in inspect.signature(LiveSwarmRunner.__init__).parameters:
            runner_kwargs["auto_spindown"] = self._isolated_test_mode
        jc_db_path = (
            (bindings_file.parent / "join_cut_registry.sqlite3")
            if self._isolated_test_mode
            else (control_dir / "join_cut_registry.sqlite3")
        )
        self.join_cut_registry = JoinCutRegistry(jc_db_path)
        if "join_cut_registry" in inspect.signature(LiveSwarmRunner.__init__).parameters:
            runner_kwargs["join_cut_registry"] = self.join_cut_registry
        ledger_root = (
            (bindings_file.parent / "promotion_ledger")
            if self._isolated_test_mode
            else (ROOT / "artifacts" / "gainers_paper" / "ledger")
        )
        self.signal_gate: Any = None
        self.promotion_ledger: Any = None
        if SignalGate is not None:
            try:
                self.signal_gate = SignalGate(root=ledger_root)
                self.promotion_ledger = self.signal_gate.ledger
            except Exception:
                self.signal_gate = None
                self.promotion_ledger = None
        self.swarm_runner = LiveSwarmRunner(**runner_kwargs)
        if not self._isolated_test_mode and hasattr(self.swarm_runner, "configure_topology"):
            self.swarm_runner.configure_topology(topology_mode="m_eulerian_join_cut", branching_order=3)
        self.swarm_runner.skill_lookup_fn = self.match_swarm_skills
        self.swarm_runner.capability_manifest_fn = self.get_preflight_manifest
        if llm_caller is None and not self._isolated_test_mode:
            self.swarm_runner.model_readiness = lambda role, model: self._call_swarm_llm(
                role, model, "", "", readiness_only=True,
            )["model"]
        self._swarm_a2a_links_path: Path = (
            bindings_file.parent / "swarm_a2a_links.json"
            if self._isolated_test_mode
            else (control_dir / "swarm_a2a_links.json")
        )
        if self._swarm_a2a_links_path.is_file():
            try:
                loaded_links = json.loads(self._swarm_a2a_links_path.read_text(encoding="utf-8"))
                if isinstance(loaded_links, list):
                    self.swarm_runner.set_a2a_links(loaded_links)
            except Exception:
                pass
        self.blackboard = SessionBlackboard(
            session_id="sabrina_desktop",
            checkpoint_path=control_dir / "swarm_blackboard.json",
        )
        self.swarm_command = SwarmCommand(blackboard=self.blackboard)
        self.connector_registry = ConnectorRegistry(
            vault_path=(control_dir / "connectors.dpapi.json") if self._isolated_test_mode else None,
            provider_vault=self.vault,
        )
        self.automation_engine = AutomationEngine(
            db_path=(control_dir / "automations.sqlite3") if self._isolated_test_mode else None,
            connector_registry=self.connector_registry,
            gemini_caller=self._call_private_automation_model,
            swarm_caller=self._run_automation_swarm_step,
            agent_caller=llm_caller or self._call_swarm_llm,
            join_cut_registry=self.join_cut_registry,
            blackboard=self.blackboard,
            workspace_tool_executor=self._execute_automation_workspace_tools,
            branching_order=3,
        )
        if not self._isolated_test_mode and not coordinator_only:
            self.automation_engine.start_background_scheduler(poll_seconds=15.0)
        self._restore_workspace_state()
        if not self._isolated_test_mode and not coordinator_only:
            self._trigger_remote_refresh(force=True)
            threading.Thread(
                target=self._prewarm_registry_worker,
                name="sabrina-registry-prewarm",
                daemon=True,
            ).start()

    @staticmethod
    def _prewarm_workspace_sdk() -> None:
        try:
            from google import genai  # noqa: F401
        except Exception:
            pass

    def _prewarm_registry_worker(self) -> None:
        try:
            self.registry.refresh_models(force=False)
        except Exception:
            pass

    @staticmethod
    def _canonical_model_key(model_id: str) -> str:
        """Normalize model IDs across local (`local:`, `ollama/`) and API (`models/`) forms for deduplication."""
        key = str(model_id or "").strip().lower()
        for prefix in ("local:", "ollama/", "models/"):
            if key.startswith(prefix):
                key = key[len(prefix) :]
        return key.strip()

    def _get_default_chat_model(self) -> str:
        """Resolve Sabrina's default chat model from coordinator role binding and unlocked providers."""
        try:
            resolved = self.role_bindings.resolve_model(
                "coordinator",
                unlocked_providers=self._unlocked_providers(),
            )
            selected = str(resolved.get("selected_model") or "").strip()
            if selected:
                return selected
        except Exception:
            pass
        return DEFAULT_GEMINI_MODEL

    def _get_local_models(
        self,
        runtime_models: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Return deduplicated local Ollama/fleet model routing targets (`direct_node`)."""
        portal_local_by_tag: dict[str, dict[str, Any]] = {}
        if not self._isolated_test_mode:
            try:
                from tools.swarm_chat_portal import LOCAL_FLEET

                for entry in LOCAL_FLEET:
                    tag = str(entry.get("model") or "").strip()
                    if tag:
                        portal_local_by_tag[self._canonical_model_key(tag)] = entry
            except Exception:
                pass

        candidates: list[dict[str, Any]] = []
        seen_keys: set[str] = set()

        def _add_local(raw_id: str, raw_name: str = "", label_hint: str = "") -> None:
            canon = self._canonical_model_key(raw_id or raw_name)
            if not canon or canon in seen_keys:
                return
            seen_keys.add(canon)
            portal_entry = portal_local_by_tag.get(canon)
            if portal_entry:
                target_id = str(portal_entry["id"])
                display = str(portal_entry.get("label") or raw_name or canon)
            else:
                target_id = raw_id if raw_id.startswith(("local:", "ollama/")) else f"ollama/{canon}"
                base_name = raw_name or canon
                display = label_hint or f"{base_name} · Local (Ollama)"
            candidates.append(
                {
                    "id": target_id,
                    "name": raw_name or canon,
                    "label": display,
                    "provider": "ollama",
                    "mode": "direct_node",
                }
            )

        for m in runtime_models or []:
            if not isinstance(m, dict):
                continue
            prov = str(m.get("provider") or "").lower()
            if "ollama" in prov or prov == "local" or m.get("tier") == "local":
                mid = str(m.get("id") or m.get("name") or "").strip()
                mname = str(m.get("name") or mid).strip()
                residency = str(m.get("residency") or "").strip()
                suffix = f" [{residency}]" if residency else ""
                _add_local(mid, mname, f"{mname} · Local (Ollama){suffix}")

        if self._isolated_test_mode or getattr(self.registry, "_models", None):
            try:
                reg_local = self.registry.search_models(
                    tier="local",
                    include_deprecated=False,
                    limit=200,
                    offset=0,
                )
                for item in reg_local.get("items") or []:
                    mid = str(item.get("id") or "").strip()
                    mname = str(item.get("name") or mid).strip()
                    _add_local(mid, mname)
            except Exception:
                pass

        if not self._isolated_test_mode:
            for canon, entry in portal_local_by_tag.items():
                _add_local(str(entry["id"]), str(entry.get("model") or canon), str(entry.get("label") or canon))

        return candidates

    def _get_unlocked_vault_models(self) -> list[dict[str, Any]]:
        """Return chat-capable cloud/API models unlocked via `ProviderKeyVault` or env keys."""
        unlocked_provs = self._unlocked_providers()
        non_local_unlocked = {p for p in unlocked_provs if p not in {"ollama", "local"}}
        if not non_local_unlocked:
            return []

        results: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        non_chat_only = {"embedding", "tts", "stt"}
        if self._isolated_test_mode or getattr(self.registry, "_models", None):
            try:
                reg_unlocked = self.registry.search_models(
                    unlocked_only=True,
                    include_deprecated=False,
                    limit=200,
                    offset=0,
                )
                items = reg_unlocked.get("items") or []
            except Exception:
                items = []
        else:
            items = []

        for item in items:
            if not isinstance(item, dict):
                continue
            prov = str(item.get("provider") or "").strip().lower()
            tier = str(item.get("tier") or "").strip().lower()
            if prov in {"ollama", "local"} or tier in {"local", "collective"}:
                continue
            if prov == "gemma" and "gemini" not in unlocked_provs:
                continue
            if prov.startswith("openrouter:") and "openrouter" not in unlocked_provs:
                continue
            if not self.vault.is_provider_unlocked(prov):
                continue
            caps = {str(c).lower() for c in (item.get("capabilities") or [])}
            if caps and not (caps & {"chat", "reasoning"}) and (caps <= non_chat_only or caps == {"image"}):
                continue
            mid = str(item.get("id") or "").strip()
            if not mid or " " in mid:
                continue
            canon = self._canonical_model_key(mid)
            if not canon or canon in seen_keys:
                continue
            seen_keys.add(canon)
            mname = str(item.get("name") or mid).strip()
            label = f"{mname} ({mid}) · {prov}" if mname != mid else f"{mid} · {prov}"
            results.append(
                {
                    "id": mid,
                    "name": mname,
                    "label": label,
                    "provider": prov,
                    "mode": "direct_node",
                }
            )
        return results

    def _build_chat_roster(
        self,
        runtime_models: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Build deduplicated user chat routing targets (`hive_council` + `direct_node` models)."""
        roster: list[dict[str, Any]] = [
            {
                "id": "hive_council",
                "label": "Hive Council (Multi-Model Synthesis)",
                "mode": "hive_council",
                "provider": "collective",
            }
        ]
        seen_ids: set[str] = {"hive_council"}
        seen_keys: set[str] = set()

        local_entries = self._get_local_models(runtime_models=runtime_models)
        vault_entries = self._get_unlocked_vault_models()
        by_canon: dict[str, dict[str, Any]] = {}
        for entry in [*vault_entries, *local_entries]:
            canon = self._canonical_model_key(entry["id"])
            if canon and canon not in by_canon:
                by_canon[canon] = entry

        default_model = self._get_default_chat_model()
        default_canon = self._canonical_model_key(default_model)
        if default_model and default_canon:
            matched = by_canon.get(default_canon)
            default_id = str(matched["id"]) if matched else default_model
            default_prov = str(matched["provider"]) if matched else infer_model_provider(default_model)
            roster.append(
                {
                    "id": default_id,
                    "name": str(matched.get("name") or default_model) if matched else default_model,
                    "label": f"{default_model} · Default ({default_prov})",
                    "provider": default_prov,
                    "mode": "direct_node",
                }
            )
            seen_ids.add(default_id)
            seen_keys.add(default_canon)

        for entry in (*local_entries, *vault_entries):
            eid = str(entry.get("id") or "").strip()
            canon = self._canonical_model_key(eid)
            if not eid or eid in seen_ids or (canon and canon in seen_keys):
                continue
            seen_ids.add(eid)
            if canon:
                seen_keys.add(canon)
            roster.append(entry)

        return roster

    def _seed_persisted_user_chat(self) -> None:
        """Load prior human/operator conversational turns once (never automated probes or worker board posts)."""
        if self._chat_history_seeded or self._isolated_test_mode:
            self._chat_history_seeded = True
            return
        self._chat_history_seeded = True
        try:
            from tools.swarm_chat_portal import _is_automated_json_probe_turn, get_chat_history

            turns = [t for t in get_chat_history() if isinstance(t, dict) and not _is_automated_json_probe_turn(t)]
            seeded: list[dict[str, Any]] = []
            for turn in turns[-15:]:
                ts_str = str(turn.get("timestamp") or turn.get("time") or "")
                user_msg = str(turn.get("user_message") or "").strip()
                reply_msg = str(turn.get("primary_reply") or "").strip()
                speaker = str(turn.get("target") or turn.get("mode") or "Sabrina")
                if user_msg:
                    seeded.append({"sender": "operator", "time": ts_str, "text": user_msg})
                if reply_msg:
                    seeded.append({"sender": speaker, "time": ts_str, "text": reply_msg})
            if seeded:
                with self._chat_lock:
                    if not self.chat_log:
                        self.chat_log = seeded[-40:]
                        self._chat_log_revision += 1
        except Exception:
            pass

    def _unlocked_providers(self) -> set[str]:
        try:
            status = self.vault.list_providers()
            if isinstance(status, dict):
                unlocked = {str(p).strip().lower() for p in (status.get("unlocked_providers") or []) if str(p).strip()}
                unlocked.add("ollama")
                return unlocked
        except Exception:
            pass
        return {"ollama"}

    def get_avatars(self) -> dict[str, str]:
        return {
            "sabrina": _data_uri(ASSETS_DIR / "coordinator.jpg"),
            "codex": _data_uri(ASSETS_DIR / "architect.jpg"),
            "antigravity": _data_uri(ASSETS_DIR / "worker.jpg"),
            "claude": _data_uri(ASSETS_DIR / "verifier.jpg"),
            "desktop": _data_uri(ASSETS_DIR / "worker.jpg"),
        }

    def _refresh_remote_telemetry_worker(self) -> None:
        api_ok = False
        health: dict[str, Any] = {"status": "local fallback active"}
        jobs: list[dict[str, Any]] = []
        kraken: dict[str, Any] = {
            "execution_authorized": False,
            "api_connected": False,
            "status": "OFFLINE (API :8087 unreachable)",
        }
        backend_swarm: dict[str, Any] = {"status": "offline", "members": []}
        ollama_models: list[dict[str, Any]] = []

        try:
            health = _http_json(f"{API_8087}/health", timeout=2.5)
            api_ok = True
        except Exception as exc:
            health = {"error": str(exc), "status": "local fallback active"}

        if api_ok:
            try:
                # Fetch active/queued/running jobs first so live workloads are never eclipsed by dead historical records
                active_res = _http_json(f"{API_8087}/api/schedule?status=running&limit=25", timeout=2.0).get("jobs", [])
                queued_res = _http_json(f"{API_8087}/api/schedule?status=queued&limit=35", timeout=2.5).get("jobs", [])
                paused_res = _http_json(f"{API_8087}/api/schedule?status=paused&limit=25", timeout=2.0).get("jobs", [])
                recent_res = _http_json(f"{API_8087}/api/schedule?limit=50", timeout=2.5).get("jobs", [])

                seen_ids: set[str] = set()
                combined: list[dict[str, Any]] = []
                for item in (active_res + queued_res + paused_res + recent_res):
                    jid = str(item.get("id") or "")
                    if jid and jid not in seen_ids:
                        seen_ids.add(jid)
                        combined.append(item)
                raw_jobs = combined if combined else recent_res
            except Exception:
                try:
                    raw_jobs = _http_json(f"{API_8087}/api/schedule", timeout=3.5).get("jobs", [])
                except Exception:
                    raw_jobs = []

            for rj in raw_jobs:
                item = dict(rj)
                st = str(item.get("status") or item.get("state") or "draft").lower()
                item["status"] = st
                item["state"] = st
                payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
                title = payload.get("title") or item.get("title") or item.get("capability") or item.get("name") or item.get("id")
                item["title"] = str(title)
                item["name"] = str(title)
                nr = item.get("next_run")
                im = item.get("interval_min")
                sched_parts = []
                if im:
                    if im >= 86400:
                        sched_parts.append(f"Every {round(im / 86400, 1)}d")
                    elif im >= 3600:
                        sched_parts.append(f"Every {round(im / 3600, 1)}h")
                    else:
                        sched_parts.append(f"Every {round(im / 60)}m")
                if nr:
                    sched_parts.append(f"next: {_clock_str(float(nr))}")
                item["schedule"] = " · ".join(sched_parts) if sched_parts else (item.get("schedule") or "manual / one-off")
                jobs.append(item)

        if api_ok:
            try:
                k_res = _http_json(f"{API_8087}/api/kraken/status", timeout=0.8)
                if isinstance(k_res, dict):
                    k_res["execution_authorized"] = False
                    k_res["api_connected"] = True
                    kraken = k_res
            except Exception:
                pass
            try:
                sw = call_console_backend("SABRINA_SWARM_CONTROL_API_BASE", "/api/swarm/state", get_live_swarm_state, timeout=1.0)
                if isinstance(sw, dict):
                    backend_swarm = sw
            except Exception:
                pass
        else:
            try:
                sw = get_live_swarm_state()
                if isinstance(sw, dict):
                    backend_swarm = sw
            except Exception as exc:
                backend_swarm = {"status": "offline", "error": str(exc), "members": []}

        try:
            installed = _http_json(f"{OLLAMA_BASE}/api/tags", timeout=0.8).get("models", [])
            loaded_names = {m.get("name") for m in _http_json(f"{OLLAMA_BASE}/api/ps", timeout=0.8).get("models", [])}
            for m in installed:
                name = m.get("name", "")
                size_bytes = m.get("size") or 0
                ollama_models.append({
                    "name": name,
                    "provider": "Ollama (Local)",
                    "role": "Local Inference Worker",
                    "size_gb": f"{size_bytes / (1024**3):.1f} GB" if size_bytes else "—",
                    "residency": "loaded" if name in loaded_names else "installed",
                })
        except Exception:
            pass

        active_prompt_state: dict[str, Any] = {}
        frontier_research_state: dict[str, Any] = {}
        api_8095_ok = False

        # Step 1: Poll :8095 alongside :8087 with independent timeout & error isolation
        try:
            ap_data = _http_json(f"{API_8095}/api/active-prompt/state", timeout=1.5)
            if isinstance(ap_data, dict) and ap_data.get("ok"):
                active_prompt_state = ap_data
                api_8095_ok = True
        except Exception:
            pass

        try:
            fr_data = _http_json(f"{API_8095}/api/frontier-research/state", timeout=1.5)
            if isinstance(fr_data, dict):
                frontier_research_state = fr_data
                api_8095_ok = True
        except Exception:
            pass

        remote_telemetry_8095: dict[str, Any] = {}
        try:
            tel_data = _http_json(f"{API_8095}/api/swarm/telemetry", timeout=1.5)
            if isinstance(tel_data, dict) and tel_data.get("ok"):
                remote_telemetry_8095 = dict(tel_data.get("telemetry_by_agent") or {})
        except Exception:
            pass

        with self._remote_lock:
            self._remote_cache = {
                "api_ok": api_ok,
                "api_8095_ok": api_8095_ok,
                "health": health,
                "jobs": jobs,
                "kraken": kraken,
                "backend_swarm": backend_swarm,
                "ollama_models": ollama_models,
                "active_prompt_state": active_prompt_state,
                "frontier_research_state": frontier_research_state,
                "remote_telemetry_8095": remote_telemetry_8095,
            }
            self._remote_last_ts = time.monotonic()
            self._remote_refreshing = False

    def _trigger_remote_refresh(self, force: bool = False) -> None:
        if self._isolated_test_mode:
            return
        now = time.monotonic()
        with self._remote_lock:
            ttl = 8.0 if self._remote_cache.get("api_ok") else 20.0
            if self._remote_refreshing:
                return
            if not force and self._remote_last_ts > 0 and (now - self._remote_last_ts) < ttl:
                return
            self._remote_refreshing = True
        threading.Thread(
            target=self._refresh_remote_telemetry_worker,
            name="sabrina-remote-telemetry",
            daemon=True,
        ).start()

    def _read_ui_asset_mtime(self) -> float:
        try:
            return float(self._ui_html_path.stat().st_mtime)
        except OSError:
            return 0.0

    def reload_ui(self) -> dict[str, Any]:
        """Hot-reload sabrina_desktop_ui.html into the live pywebview window without restarting Python."""
        if not self._ui_html_path.is_file():
            return {"ok": False, "error": f"UI asset not found: {self._ui_html_path}"}
        try:
            html_text = _read_desktop_html(self._ui_html_path)
        except Exception as exc:
            return {"ok": False, "error": f"Failed to read UI asset: {exc}"}

        new_mtime = self._read_ui_asset_mtime()
        self._ui_loaded_mtime = new_mtime
        if self._window is not None:
            load_fn = getattr(self._window, "load_html", None)
            if callable(load_fn):
                if self._isolated_test_mode:
                    try:
                        load_fn(html_text)
                    except Exception as exc:
                        return {"ok": False, "error": f"load_html failed: {exc}"}
                else:
                    def _deferred_load() -> None:
                        try:
                            load_fn(html_text)
                        except Exception:
                            pass
                    t = threading.Timer(0.08, _deferred_load)
                    t.daemon = True
                    t.start()
            else:
                return {"ok": False, "error": "Window does not support load_html"}

        return {
            "ok": True,
            "reloaded": self._window is not None,
            "ui_asset_mtime": new_mtime,
            "ui_loaded_mtime": new_mtime,
            "ui_dirty": False,
        }

    def get_swarm_display(self) -> dict[str, Any]:
        try:
            prefs = json.loads(self._swarm_display_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return prefs if isinstance(prefs, dict) else {}

    def save_swarm_display(
        self,
        theme: str,
        motion: str,
        link_style: str | None = None,
        link_speed: str | None = None,
        backdrop: str | None = None,
    ) -> dict[str, Any]:
        settings = {"theme": theme, "motion": motion}
        if link_style is not None:
            settings["link_style"] = link_style
        if link_speed is not None:
            settings["link_speed"] = link_speed
        if backdrop is not None:
            settings["backdrop"] = backdrop
        choices = {
            "theme": ("robots", "christmas", "workshop", "classic"),
            "motion": ("system", "on", "paused"),
            "link_style": ("theme", "snow_bridge", "pulse", "still"),
            "link_speed": ("slow", "normal", "fast"),
            "backdrop": ("standard", "workshop"),
        }
        if any(value not in choices[key] for key, value in settings.items()):
            return {"ok": False, "error": "Invalid graph appearance"}
        path = self._swarm_display_path
        temporary_path = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, prefix="swarm_display-", suffix=".tmp", delete=False,
            ) as stream:
                temporary_path = Path(stream.name)
                json.dump({**self.get_swarm_display(), **settings}, stream)
            temporary_path.replace(path)
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return {"ok": True}

    def _get_execution_kind_map(self) -> dict[str, str]:
        cached = getattr(self, "_capability_execution_kinds", None)
        if isinstance(cached, dict):
            return cached
        mapping: dict[str, str] = {}
        for cap in DEFAULT_CAPABILITIES or ():
            name = str(getattr(cap, "name", "") or "").strip()
            if not name:
                continue
            kind = str(getattr(cap, "execution_kind", "code") or "code").strip().lower()
            if kind not in {"code", "agent", "browser"}:
                kind = "code"
            mapping[name] = kind
        self._capability_execution_kinds = mapping
        return mapping

    def _resolve_job_execution_kind(self, job: dict[str, Any]) -> str:
        existing = str(job.get("execution_kind") or "").strip().lower()
        if existing in {"code", "agent", "browser"}:
            return existing
        cap_name = str(job.get("capability") or "").strip()
        if cap_name:
            return self._get_execution_kind_map().get(cap_name, "code")
        return "code"

    def get_snapshot(self) -> dict[str, Any]:
        self._trigger_remote_refresh(force=False)
        with self._remote_lock:
            remote = dict(self._remote_cache)
            snapshot_loaded = self._remote_last_ts > 0

        api_ok = bool(remote.get("api_ok", False))
        health: dict[str, Any] = dict(remote.get("health") or {})
        raw_jobs: list[dict[str, Any]] = list(remote.get("jobs") or [])
        jobs: list[dict[str, Any]] = []
        for rj in raw_jobs:
            if not isinstance(rj, dict):
                continue
            item = dict(rj)
            item["execution_kind"] = self._resolve_job_execution_kind(item)
            jobs.append(item)
        kraken: dict[str, Any] = dict(
            remote.get("kraken")
            or {"execution_authorized": False, "api_connected": False, "status": "OFFLINE (API :8087 unreachable)"}
        )
        kraken["execution_authorized"] = False
        models: list[dict[str, Any]] = []
        chat_roster: list[dict[str, Any]] = []
        chat_history: list[dict[str, Any]] = []

        # 1. Pull backend swarm state (no static live_antigravity_swarm.json fallbacks)
        raw_backend_swarm = remote.get("backend_swarm")
        swarm: dict[str, Any] = dict(raw_backend_swarm) if isinstance(raw_backend_swarm, dict) else {"status": "offline", "members": []}
        if isinstance(swarm.get("nodes"), dict):
            swarm["nodes"] = {k: dict(v) if isinstance(v, dict) else {} for k, v in swarm["nodes"].items()}

        # 2. Pull live execution state from LiveSwarmRunner (0 child workers when idle; never static workers)
        runner_state = self.swarm_runner.get_state()
        live_workers = list(runner_state.get("workers") or [])
        live_active_missions = list(runner_state.get("active_missions") or [])
        chains = self.followup_coordinator.snapshot()
        active_chains = [c for c in chains if c["state"] in IN_PROGRESS]
        for chain in active_chains:
            live_workers.extend({**worker, "background_chain": chain["chain_id"]}
                                for worker in chain.get("workers") or [])
            if chain["state"] == "RUNNING" and chain.get("current_mission_id"):
                live_active_missions.append({
                    "mission_id": chain["current_mission_id"], "goal": chain.get("current_goal") or chain["goal"],
                    "state": "ACTIVE (RUNNING)", "detail": chain["detail"], "background_chain": chain["chain_id"],
                })
        runner_status = str(
            runner_state.get("status")
            or ("ACTIVE" if (live_workers or live_active_missions) else "IDLE")
        )
        if active_chains:
            runner_status = "ACTIVE"
        self.active_missions = live_active_missions

        # Merge dynamic council node states from LiveSwarmRunner and telemetry blackboard
        runner_nodes = runner_state.get("nodes") or {}
        runner_telemetry = dict(runner_state.get("agent_telemetry") or {})
        remote_telemetry = dict(remote.get("remote_telemetry_8095") or {})
        try:
            from tools.telemetry_blackboard import get_telemetry_blackboard
            db_telemetry = get_telemetry_blackboard().get_latest_telemetry_by_agent()
        except Exception:
            db_telemetry = {}

        merged_telemetry = {**db_telemetry, **remote_telemetry, **runner_telemetry}
        swarm["agent_telemetry"] = merged_telemetry
        swarm["telemetry_by_agent"] = merged_telemetry

        nodes = swarm.setdefault("nodes", {})
        for node_key in ("sabrina", "codex", "antigravity", "claude", "desktop"):
            entry = nodes.setdefault(node_key, {})
            r_node = runner_nodes.get(node_key)
            if isinstance(r_node, dict):
                for k, v in r_node.items():
                    if node_key == "desktop" and k == "status" and entry.get("lease_owner"):
                        continue
                    entry[k] = v
            entry.setdefault("status", "IDLE")
            entry.setdefault("detail", "")
            t_entry = merged_telemetry.get(node_key, {})
            entry["agent_id"] = node_key
            entry["mode"] = "SEEDED"
            if node_key == "sabrina":
                try:
                    b = self.role_bindings.get_binding("coordinator")
                    entry["model"] = b.primary_model
                    entry["role"] = "coordinator"
                except Exception:
                    entry.setdefault("model", "gemini-4-neon")
                    entry.setdefault("role", "coordinator")
            raw_st = str(entry.get("status") or "IDLE")
            entry["state"] = t_entry.get("state") or ("ACTIVE" if ("ACTIVE" in raw_st or "WORKING" in raw_st) else ("VALIDATING" if "VERIFY" in raw_st else "STANDBY"))
            entry["gate_status"] = t_entry.get("gate_status") or ("PASS" if entry["state"] in ("ACTIVE", "VALIDATING") else "STANDBY")
            entry["logprob"] = t_entry.get("logprob") if t_entry.get("logprob") is not None else -0.10
            entry["command"] = t_entry.get("command") or entry.get("command") or ""
            entry["active_command"] = entry["command"]
            entry["task"] = t_entry.get("task") or entry.get("detail") or ""
            entry["parent_id"] = None if node_key == "sabrina" else "sabrina"
            entry["depth"] = 0 if node_key == "sabrina" else 1
            entry["action_trail"] = t_entry.get("action_trail") or []
        if isinstance(nodes.get("desktop"), dict):
            nodes["desktop"]["execution_authorized"] = False

        # 3. Live Swarm Workers & Runner Telemetry (replaces static swarm_extra["workers"])
        for w in live_workers:
            wid = str(w.get("id") or "")
            wt = merged_telemetry.get(wid, {})
            w.setdefault("agent_id", wid)
            w.setdefault("mode", "DYNAMIC")
            w.setdefault("state", wt.get("state") or "ACTIVE")
            w.setdefault("gate_status", wt.get("gate_status") or "PASS")
            w.setdefault("logprob", wt.get("logprob") if wt.get("logprob") is not None else -0.15)
            w.setdefault("command", wt.get("command") or w.get("task") or "")
            w.setdefault("active_command", wt.get("command") or w.get("task") or "")
            w.setdefault("parent_id", wt.get("parent_id") or w.get("parent_node") or "sabrina")
            w.setdefault("depth", wt.get("depth", 1))
            w.setdefault("action_trail", wt.get("action_trail") or [])

        swarm["workers"] = live_workers
        swarm["active_worker_count"] = (int(runner_state.get("active_worker_count", 0))
                                        + sum(len(c.get("workers") or []) for c in active_chains))
        swarm["recent_spindowns"] = list(runner_state.get("recent_spindowns") or [])
        swarm["file_conflicts"] = list(runner_state.get("file_conflicts") or [])
        with self._remote_lock:
            swarm["interface_dispatches"] = list(self._interface_dispatches)
        swarm["active_missions"] = live_active_missions
        swarm["pending_audit_decisions"] = list(runner_state.get("pending_audit_decisions") or [])
        swarm["mission_outputs"] = list(runner_state.get("mission_outputs") or [])
        if time.monotonic() - self._monitor_runs_at >= 5:
            self._monitor_runs_at = time.monotonic()
            try:
                self._monitor_runs = self.swarm_runner.list_completed_runs()[:12]
                self._monitor_attention = self.swarm_runner.list_run_attention()[:12]
            except OSError:
                pass
        swarm["recent_saved_runs"] = self._monitor_runs
        swarm["saved_run_attention"] = self._monitor_attention
        terminal_chains = [c for c in chains if c["state"] not in IN_PROGRESS][:12]
        swarm["followup_chains"] = [{k: v for k, v in c.items() if k != "workers"}
                                    for c in active_chains + terminal_chains]
        swarm["monitor_connection"] = ("connecting" if not snapshot_loaded else
                                       "connected" if api_ok and remote.get("api_8095_ok") else "degraded")
        swarm["active_handoffs"] = list(runner_state.get("active_handoffs") or [])
        swarm["a2a_links"] = list(runner_state.get("a2a_links") or [])
        swarm["deliberations"] = list(runner_state.get("deliberations") or [])
        swarm["harness_events"] = list(runner_state.get("harness_events") or [])
        swarm["active_harness_id"] = str(runner_state.get("active_harness_id") or self.harness_registry.active_harness_id)
        swarm["execution_authorized"] = False
        swarm["status"] = runner_status
        swarm["runner_status"] = runner_status
        swarm["safety_lock"] = str(runner_state.get("safety_lock") or "LOCKED")
        swarm["verifier_verdict"] = str(
            runner_state.get("verifier_verdict")
            or "STANDBY"
        )
        swarm["time_str"] = str(
            runner_state.get("time_str")
            or swarm.get("time_str")
            or time.strftime("%H:%M:%S")
        )

        # 4. Objectives: live runner objectives first (enriched with live progress/workers), then backend/SQLite history
        in_flight_states = {"ACTIVE", "RUNNING", "WORKING", "SPAWNING", "QUEUED", "VERIFYING"}
        workers_by_mission: dict[str, list[dict[str, Any]]] = {}
        for w in live_workers:
            if isinstance(w, dict):
                mid = str(w.get("mission_id") or "")
                if mid:
                    workers_by_mission.setdefault(mid, []).append(w)
        outputs_by_mission: dict[str, dict[str, Any]] = {}
        for mo in swarm.get("mission_outputs") or []:
            if isinstance(mo, dict):
                mid = str(mo.get("mission_id") or "")
                if mid:
                    outputs_by_mission[mid] = mo

        now_ts = time.time()
        def _fmt_age(ts_val: Any) -> str:
            try:
                delta = max(0.0, now_ts - float(ts_val))
            except (TypeError, ValueError):
                return ""
            if delta < 60:
                return f"{int(delta)}s ago"
            if delta < 3600:
                return f"{int(delta // 60)}m ago"
            if delta < 86400:
                return f"{int(delta // 3600)}h ago"
            return f"{int(delta // 86400)}d ago"

        merged_objectives: list[dict[str, Any]] = []
        seen_obj_ids: set[str] = set()
        for obj in runner_state.get("objectives") or []:
            if not isinstance(obj, dict):
                continue
            item = dict(obj)
            oid = str(item.get("id") or "")
            if oid:
                seen_obj_ids.add(oid)
            st_upper = str(item.get("state") or "IDLE").upper()
            m_workers = workers_by_mission.get(oid, [])
            m_out = outputs_by_mission.get(oid) or {}
            total_steps = sum(int(w.get("total_steps") or 0) for w in m_workers)
            completed_steps = sum(int(w.get("completed_steps") or 0) for w in m_workers)
            if total_steps > 0:
                progress_pct = int(round((completed_steps / total_steps) * 100))
            elif m_workers:
                pcts = [int(w.get("progress_pct") or 0) for w in m_workers]
                progress_pct = int(round(sum(pcts) / len(pcts)))
            else:
                progress_pct = 100 if st_upper in {"PASS", "DONE", "COMPLETED"} else 0
            item["source"] = "live_runner"
            item["is_live"] = bool(m_workers) or (st_upper in in_flight_states)
            item["active_workers"] = [str(w.get("name") or w.get("id") or "") for w in m_workers if w.get("name") or w.get("id")]
            item["completed_steps"] = completed_steps
            item["total_steps"] = total_steps
            item["progress_pct"] = progress_pct
            item["live_llm_calls"] = int(m_out.get("live_llm_calls") or item.get("live_llm_calls") or 0)
            item["updated_ago"] = item.get("updated_ago") or _fmt_age(item.get("updated_ts") or item.get("ts"))
            merged_objectives.append(item)
        for obj in swarm.get("objectives") or []:
            if not isinstance(obj, dict):
                continue
            item = dict(obj)
            oid = str(item.get("id") or "")
            if oid and oid in seen_obj_ids:
                continue
            if oid:
                seen_obj_ids.add(oid)
            item["source"] = "backend_sqlite"
            item["is_live"] = False
            item.setdefault("active_workers", [])
            item.setdefault("updated_ago", _fmt_age(item.get("updated_ts") or item.get("updated") or item.get("ts")))
            merged_objectives.append(item)
        swarm["objectives"] = merged_objectives

        # 5. Activity Feed & Research Episodes: merge live runner events with local and SQLite feeds
        merged_feed: list[dict[str, Any]] = []
        merged_feed.extend(item for item in self.local_feed if isinstance(item, dict))
        merged_feed.extend(item for item in (runner_state.get("activity_feed") or []) if isinstance(item, dict))
        merged_feed.extend(item for item in (swarm.get("activity_feed") or []) if isinstance(item, dict))
        merged_feed.sort(key=lambda item: float(item.get("ts") or 0.0), reverse=True)
        swarm["activity_feed"] = merged_feed[:40]

        merged_episodes: list[dict[str, Any]] = []
        seen_ep_ids: set[str] = set()
        for ep in runner_state.get("research_episodes") or []:
            if not isinstance(ep, dict):
                continue
            eid = str(ep.get("id") or "")
            if eid:
                seen_ep_ids.add(eid)
            merged_episodes.append(ep)
        for ep in swarm.get("research_episodes") or []:
            if not isinstance(ep, dict):
                continue
            eid = str(ep.get("id") or "")
            if eid and eid in seen_ep_ids:
                continue
            if eid:
                seen_ep_ids.add(eid)
            merged_episodes.append(ep)
        swarm["research_episodes"] = merged_episodes[:20]

        # 6. Models: live local Ollama telemetry only (no static live_antigravity_swarm.json models)
        for om in remote.get("ollama_models") or []:
            models.append(om)

        self.registry.set_runtime_fleet(models)

        # 7. Chat Roster (real routing targets) & isolated Swarm Board vs Operator Chat History
        chat_roster = self._build_chat_roster(runtime_models=models)

        swarm_board_messages: list[dict[str, Any]] = []
        swarm["board_messages"] = swarm_board_messages

        self._seed_persisted_user_chat()
        with self._chat_lock:
            chat_history = list(self.chat_log[-40:])
            chat_revision = self._chat_log_revision

        schema_errors, schema_warnings, schema_counts = validate_swarm_payload(swarm)
        schema_audit = {
            "valid": len(schema_errors) == 0,
            "errors": schema_errors,
            "warnings": schema_warnings,
            "counts": schema_counts,
        }

        unlocked = self._unlocked_providers()
        active_prompt_state = dict(remote.get("active_prompt_state") or {})
        frontier_research_state = dict(remote.get("frontier_research_state") or {})
        api_8095_ok = bool(remote.get("api_8095_ok", False))
        lab: dict[str, Any] = {
            "api_8087_ok": api_ok,
            "api_8095_ok": api_8095_ok,
            "runner_status": runner_status,
            "active_worker_count": swarm["active_worker_count"],
            "active_mission_count": len(live_active_missions),
            "unlocked_providers": sorted(unlocked),
            "ollama_model_count": len(remote.get("ollama_models") or []),
            "active_prompt_state": active_prompt_state if api_8095_ok else {"status": "offline"},
            "frontier_research_state": frontier_research_state if api_8095_ok else {"status": "offline"},
        }
        current_ui_mtime = self._read_ui_asset_mtime()
        ui_dirty = current_ui_mtime > 0.0 and abs(current_ui_mtime - self._ui_loaded_mtime) > 1e-6

        for prog in (self.automation_engine.get_active_progress() or {}).values():
            if not isinstance(prog, dict):
                continue
            p_rec = str(prog.get("recommended_next_goal") or "").strip()
            p_ts = float(prog.get("updated_at") or 0.0)
            if p_rec and prog.get("status") == "completed":
                if not self._latest_recommended_next or p_ts > float(self._latest_recommended_next.get("ts") or 0.0):
                    self._latest_recommended_next = {
                        "goal": p_rec,
                        "source": f"workflow:{prog.get('run_id') or prog.get('loop_id') or 'join_cut'}",
                        "run_id": str(prog.get("run_id") or ""),
                        "goal_history": list(prog.get("goal_history") or []),
                        "ts": p_ts,
                    }

        best_rec: dict[str, Any] = dict(self._latest_recommended_next) if self._latest_recommended_next else {}
        for mo in swarm.get("mission_outputs") or []:
            if not isinstance(mo, dict):
                continue
            m_rec = str(mo.get("recommended_next_goal") or "").strip()
            m_state = str(mo.get("state") or "").upper()
            if m_rec and m_state in {"PASS", "DONE", "COMPLETED", "DRY_RUN"}:
                m_ts = float(mo.get("completed_at") or 0.0)
                if not best_rec or m_ts >= float(best_rec.get("ts") or 0.0):
                    best_rec = {
                        "goal": m_rec,
                        "source": f"mission:{mo.get('mission_id', '')}",
                        "mission_id": str(mo.get("mission_id") or ""),
                        "ts": m_ts or time.time(),
                    }
                break
        if best_rec.get("goal"):
            swarm["recommended_next_goal"] = best_rec["goal"]

        return {
            "api_ok": api_ok,
            "api_8095_ok": api_8095_ok,
            "ui_asset_mtime": current_ui_mtime,
            "ui_loaded_mtime": self._ui_loaded_mtime,
            "ui_dirty": ui_dirty,
            "ui_auto_reload": self._ui_auto_reload,
            "recommended_next_step": best_rec,
            "active_prompt_state": active_prompt_state,
            "frontier_research_state": frontier_research_state,
            "swarm": swarm,
            "agent_telemetry": merged_telemetry,
            "telemetry_by_agent": merged_telemetry,
            "swarm_deliberations": swarm.get("deliberations", []),
            "swarm_active_handoffs": swarm.get("active_handoffs", []),
            "swarm_a2a_links": swarm.get("a2a_links", []),
            "schema_audit": schema_audit,
            "health": health,
            "jobs": jobs,
            "kraken": kraken,
            "lab": lab,
            "models": models,
            "chat_roster": chat_roster,
            "chat_history": chat_history,
            "chat_revision": chat_revision,
            "chat_job": self.get_chat_job_status(),
            "workspace": self._get_workspace_summary(),
            "swarm_board_messages": swarm_board_messages,
            "collective_models": models,
            "vault": self.get_vault_status(),
            "role_bindings": self.role_bindings.list_bindings(unlocked_providers=unlocked),
            "harness_collective": self.harness_registry.list_harnesses(),
            "deployment_health": self.get_deployment_health(),
            "evo_bench": self._get_evo_bench_snapshot_light(),
            "automations": {
                "loops": self.automation_engine.list_loops(),
                "pending_approvals": self.automation_engine.list_pending_approvals(),
                "connectors": self.connector_registry.list_connectors(),
                "active_progress": self.automation_engine.get_active_progress(),
            },
            "backend_warnings": [
                f"API :8087 unreachable (remote service on ego:8087 not running — local fallback active): {health.get('error')}"
            ] if (not api_ok and isinstance(health, dict) and health.get("error")) else [],
        }

    def get_deployment_health(self) -> dict[str, Any]:
        """Return live status of wired backend modules for the Deployment Health badge."""
        modules: list[dict[str, Any]] = []

        # 1. Join-Cut Registry
        jc_active = self.join_cut_registry is not None and bool(getattr(self.swarm_runner, "join_cut_enabled", False))
        jc_nodes = 0
        if jc_active:
            try:
                jc_nodes = len(self.join_cut_registry.all_nodes())
            except Exception:
                jc_nodes = -1
        modules.append({
            "id": "join_cut_registry",
            "label": "Join-Cut Swarm",
            "active": jc_active,
            "detail": f"m={self.join_cut_registry.params.m}, nodes={jc_nodes}" if jc_active else "disabled",
        })

        # 2. Promotion Ledger
        pl_active = self.promotion_ledger is not None
        pl_finalized = 0
        if pl_active:
            try:
                pl_finalized = len(self.promotion_ledger.finalized())
            except Exception:
                pl_finalized = -1
        modules.append({
            "id": "promotion_ledger",
            "label": "Promotion Ledger",
            "active": pl_active,
            "detail": f"finalized={pl_finalized}" if pl_active else "unwired",
        })

        # 3. Signal Promotion Gate
        sg_active = self.signal_gate is not None
        sg_signals = 0
        sg_candidates = 0
        if sg_active and self.promotion_ledger is not None:
            try:
                fin = self.promotion_ledger.finalized()
                sg_signals = sum(1 for r in fin if r.get("kind") == KIND_SIGNAL)
                sg_candidates = sum(1 for r in fin if r.get("kind") == KIND_CANDIDATE)
            except Exception:
                pass
        modules.append({
            "id": "signal_promotion_gate",
            "label": "Signal Gate",
            "active": sg_active,
            "detail": f"signals={sg_signals}, candidates={sg_candidates}" if sg_active else "unwired",
        })

        # 4. GPU Providers
        gpu_active = hasattr(self, "get_gpu_providers")
        modules.append({
            "id": "gpu_providers",
            "label": "GPU Providers",
            "active": gpu_active,
            "detail": "ready" if gpu_active else "unwired",
        })

        # 5. m-Eulerian RSI Schema
        schema_candidates = [
            ROOT / "data" / "control" / "m_eulerian_rsi_schema.json",
        ]
        if hasattr(self, "_swarm_schema_path"):
            schema_candidates.insert(0, self._swarm_schema_path.parent / "m_eulerian_rsi_schema.json")
        schema_active = any(p.is_file() for p in schema_candidates)
        modules.append({
            "id": "m_eulerian_rsi_schema",
            "label": "m-Eulerian Schema",
            "active": schema_active,
            "detail": "present" if schema_active else "missing",
        })

        active_count = sum(1 for m in modules if m["active"])
        total_count = len(modules)
        return {
            "active_count": active_count,
            "total_count": total_count,
            "all_ok": active_count == total_count,
            "summary": f"Modules: {active_count}/{total_count} Active",
            "modules": modules,
        }

    def get_promotion_ledger_status(self) -> dict[str, Any]:
        """Expose PromotionLedger & SignalGate status to the UI/bridge."""
        if self.promotion_ledger is None or self.signal_gate is None:
            return {"ok": False, "active": False, "finalized": [], "signals": 0, "candidates": 0}
        try:
            fin = self.promotion_ledger.finalized()
            return {
                "ok": True,
                "active": True,
                "dataset": self.signal_gate.dataset,
                "finalized_count": len(fin),
                "signals": sum(1 for r in fin if r.get("kind") == KIND_SIGNAL),
                "candidates": sum(1 for r in fin if r.get("kind") == KIND_CANDIDATE),
                "recent": fin[-10:],
            }
        except Exception as exc:
            return {"ok": False, "active": True, "error": str(exc)}

    def _try_local_desktop_action(self, goal: str) -> str | None:
        """Execute explicit safe local launch intents (e.g. open chrome / open URL)."""
        import re
        import subprocess
        import webbrowser

        low = (goal or "").strip().lower()
        url_match = re.search(r"https?://[^\s]+", goal or "")
        if url_match and any(verb in low for verb in ("open ", "launch ", "go to ", "browse ")):
            url = url_match.group(0).rstrip(".,);")
            webbrowser.open(url)
            return f"Opened {url} in default browser"
        if re.search(r"\b(open|launch|start)\s+(google\s+)?chrome\b", low):
            chrome_paths = [
                Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            ]
            exe = next((str(p) for p in chrome_paths if p.is_file()), None)
            if exe:
                subprocess.Popen([exe], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            else:
                subprocess.Popen(["cmd", "/c", "start", "", "chrome"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return "Launched Google Chrome locally"
        if re.search(r"\b(open|launch|start)\s+(the\s+)?browser\b", low):
            webbrowser.open("https://www.google.com")
            return "Opened default browser"
        return None

    def launch_release_review(self, replay_id: str = "") -> dict[str, Any]:
        try:
            planner = (release.original_planner(self.swarm_runner.artifacts_dir, replay_id) if replay_id
                       else self.role_bindings.get_binding("planner_architect").primary_model)
            review = release.ReleaseReview(
                planner, self._invoke_provider_text, lambda: self.vault.get_key("gemini", include_env=True)
            )
            return self.swarm_runner.launch_mission(release.GOAL, mode=release.MODE, review=review)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    def launch_live_contract(self, raw: str) -> dict[str, Any]:
        return self.swarm_runner.launch_mission("", live_contract_json=raw)

    def revise_live_contract(self, mission_id: str, expected_revision: int, raw: str) -> dict[str, Any]:
        return self.swarm_runner.revise_mission_contract(mission_id, expected_revision, raw)

    def get_preflight_manifest(self, allow_workspace_writes: bool = False) -> dict[str, Any]:
        """Layer 1: Assemble the Deterministic Pre-Flight Manifest from live Vault, cooldown table, and Ollama state."""
        with self._remote_lock:
            ollama_models = list(self._remote_cache.get("ollama_models") or [])
        with self._provider_cooldown_lock:
            cooldowns = dict(self._provider_cooldowns)
            zero_quota = set(self._zero_quota_models)
            unsupported = set(self._unsupported_models)
        manifest = self.swarm_runner.build_preflight_manifest(
            vault=self.vault,
            registry=self.registry,
            unlocked_providers=self._unlocked_providers(),
            cooldowns=cooldowns,
            zero_quota_models=zero_quota,
            unsupported_models=unsupported,
            ollama_models=ollama_models,
            ollama_ready=bool(ollama_models),
            candidate_models=self._get_qualified_portal_candidates(),
            allow_workspace_writes=bool(allow_workspace_writes),
        )
        return manifest.to_dict()

    def _get_qualified_portal_candidates(self, limit_per_tier: int | None = 256) -> list[str]:
        """Return sanctioned, PortalPolicy-qualified external AI portals (`portal:<id>`) if enabled."""
        if self._isolated_test_mode:
            return []
        try:
            from harnesses.external.collective_pool import KindCollectivePool

            pool = KindCollectivePool()
            flash = pool.get_tier_candidates("flash")
            deep = pool.get_tier_candidates("deep_reasoning")
            if limit_per_tier is not None and limit_per_tier > 0:
                flash = flash[:limit_per_tier]
                deep = deep[:limit_per_tier]
            out: list[str] = []
            for pid in [*flash, *deep]:
                tag = f"portal:{pid}"
                if tag not in out:
                    out.append(tag)
            return out
        except Exception:
            return []

    def match_swarm_skills(self, query: str, limit: int = 2) -> list[dict[str, str]]:
        """Match relevant SKILL.md playbooks from ModelSkillRegistry for dynamic Swarm prompt injection."""
        try:
            self.registry.refresh_skills(force=False)
            skills = list(getattr(self.registry, "_skills", None) or [])
        except Exception:
            return []
        if not skills:
            return []
        raw_tokens = [
            tok for tok in re.findall(r"[a-z0-9_-]{4,}", (query or "").lower())
            if tok not in {"make", "with", "that", "this", "from", "into", "your", "have", "need", "want", "please"}
        ]
        if not raw_tokens:
            return []
        scored: list[tuple[int, dict[str, Any]]] = []
        for sk in skills:
            sid = str(sk.get("id") or "").lower()
            if sid == "windows-powershell":
                continue
            blob = f"{sid} {sk.get('name', '')} {sk.get('description', '')}".lower()
            hits = sum(2 if tok in sid else (1 if tok in blob else 0) for tok in set(raw_tokens))
            if hits >= 2:
                scored.append((hits, sk))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("id") or "")))
        matched_out: list[dict[str, str]] = []
        for _, sk in scored[: max(1, int(limit))]:
            detail = self.registry.get_skill_detail(str(sk["id"])) or sk
            body = str(detail.get("content") or "").strip()
            matched_out.append({
                "id": str(sk.get("id") or ""),
                "name": str(sk.get("name") or ""),
                "description": str(sk.get("description") or ""),
                "excerpt": body[:1600],
            })
        return matched_out

    def generate_swarm_media(
        self,
        kind: str,
        prompt: str,
        output_path: Path | str,
        *,
        model: str = "",
        aspect_ratio: str = "16:9",
        voice: str = "Puck",
        workspace_root: Path | None = None,
    ) -> dict[str, Any]:
        """Generate binary image or TTS audio inside `workspace_root` using a Coordinator/worker-chosen manifest model."""
        from swarm_console.protocol import guard_safe_path

        clean_kind = "audio" if str(kind or "").strip().lower() in {"audio", "tts"} else "image"
        cap_key = "tts" if clean_kind == "audio" else "image"
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt:
            return {"ok": False, "error": f"Empty prompt passed to generate_{clean_kind}."}

        eff_root = (Path(workspace_root) if workspace_root is not None else self.swarm_runner.workspace_root).resolve()
        raw_rel = str(output_path or "").strip()
        if not raw_rel:
            return {"ok": False, "error": "Missing relative output path."}
        p_in = Path(raw_rel)
        cand = p_in.resolve() if p_in.is_absolute() else (eff_root / raw_rel.lstrip("/\\")).resolve()
        if not cand.is_relative_to(eff_root):
            return {"ok": False, "error": f"Output path escapes workspace_root: {raw_rel}"}
        try:
            safe_target = guard_safe_path(cand)
        except Exception as exc:
            return {"ok": False, "error": f"Blocked by path safety policy: {exc}"}

        manifest_dict = self.get_preflight_manifest(allow_workspace_writes=True)
        media_pool = list((manifest_dict.get("healthy_media_models") or {}).get(cap_key) or [])
        if not media_pool:
            hint = "Write a clean vector `.svg` file via `write_file` instead." if clean_kind == "image" else "Provide text script instead."
            return {
                "ok": False,
                "error": f"No unlocked healthy `{cap_key}` model in Pre-Flight Manifest. {hint}",
            }

        chosen_model = ""
        req_model = str(model or "").strip()
        if req_model:
            req_bare = req_model.lower().split("/", 1)[-1]
            for m_entry in media_pool:
                if req_model.lower() == m_entry.lower() or req_bare == m_entry.lower().split("/", 1)[-1]:
                    chosen_model = m_entry
                    break
            if not chosen_model:
                return {
                    "ok": False,
                    "error": f"Requested media model '{req_model}' is not in healthy_media_models[{cap_key!r}]: {media_pool}",
                }
        else:
            chosen_model = media_pool[0]

        ordered_models = [chosen_model] + [m for m in media_pool if m != chosen_model]
        errs: list[str] = []
        skipped_gemini_free_img = False
        for cand_m in ordered_models[:6]:
            if skipped_gemini_free_img and cand_m.lower().startswith("gemini/"):
                continue
            prov = infer_model_provider(cand_m)
            if prov == "unknown" and "/" in cand_m:
                prov = cand_m.split("/", 1)[0].lower()
            api_key = self.vault.get_key(prov, include_env=True) if hasattr(self.vault, "get_key") else None
            if not api_key and prov not in {"pollinations", "lmstudio", "ollama"}:
                errs.append(f"{cand_m}: no API key unlocked for {prov}")
                continue
            bare_m = cand_m[len(prov) + 1 :] if cand_m.lower().startswith(f"{prov}/") else cand_m
            try:
                raw_bytes = b""
                mime = "image/png" if clean_kind == "image" else "audio/wav"
                if prov == "gemini":
                    raw_bytes, mime = _call_gemini_interactions_media(
                        cand_m,
                        clean_prompt,
                        api_key or "",
                        kind=clean_kind,
                        aspect_ratio=aspect_ratio or "16:9",
                        voice=voice or "Puck",
                    )
                elif clean_kind == "image" and prov == "huggingface":
                    hf_req = urllib.request.Request(
                        f"https://router.huggingface.co/hf-inference/models/{bare_m}",
                        data=json.dumps({"inputs": clean_prompt}).encode("utf-8"),
                        method="POST",
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                            "Accept": "image/png, image/jpeg",
                        },
                    )
                    with urllib.request.urlopen(hf_req, timeout=35.0) as hf_resp:
                        raw_bytes = hf_resp.read(12_000_000)
                        mime = str(hf_resp.headers.get("Content-Type") or "image/png").split(";", 1)[0].strip()
                elif clean_kind == "image" and prov in {"openai", "together", "fireworks"}:
                    img_urls = {
                        "openai": "https://api.openai.com/v1/images/generations",
                        "together": "https://api.together.xyz/v1/images/generations",
                        "fireworks": "https://api.fireworks.ai/inference/v1/images/generations",
                    }
                    img_data = _http_json(
                        img_urls[prov],
                        method="POST",
                        payload={
                            "model": bare_m,
                            "prompt": clean_prompt,
                            "n": 1,
                            "response_format": "b64_json",
                        },
                        headers={"Authorization": f"Bearer {api_key}"},
                        timeout=35.0,
                    )
                    items_img = img_data.get("data") or []
                    if items_img and isinstance(items_img[0], dict) and items_img[0].get("b64_json"):
                        raw_bytes = base64.b64decode(str(items_img[0]["b64_json"]))
                        mime = "image/png"
                elif clean_kind == "image" and prov == "pollinations":
                    import urllib.parse as _up

                    w_px, h_px = (1024, 576) if "16:9" in (aspect_ratio or "") else (768, 768)
                    pol_url = f"https://image.pollinations.ai/prompt/{_up.quote(clean_prompt)}?width={w_px}&height={h_px}&nologo=true"
                    pol_headers = {"User-Agent": "SabrinaSwarm/1.0"}
                    if api_key and api_key.lower() not in {"open", "public", "true", "1", "enabled"}:
                        pol_headers["Authorization"] = f"Bearer {api_key}"
                    pol_req = urllib.request.Request(pol_url, headers=pol_headers)
                    with urllib.request.urlopen(pol_req, timeout=35.0) as pol_resp:
                        raw_bytes = pol_resp.read(12_000_000)
                        mime = str(pol_resp.headers.get("Content-Type") or "image/jpeg").split(";", 1)[0].strip()
                else:
                    errs.append(f"{cand_m}: unsupported media provider '{prov}' for {clean_kind}")
                    continue

                if not raw_bytes:
                    errs.append(f"{cand_m}: empty media payload returned")
                    continue
                safe_target.parent.mkdir(parents=True, exist_ok=True)
                tmp_target = safe_target.with_suffix(safe_target.suffix + ".tmp")
                tmp_target.write_bytes(raw_bytes)
                os.replace(tmp_target, safe_target)
                rel_posix = safe_target.relative_to(eff_root).as_posix()
                return {
                    "ok": True,
                    "path": rel_posix,
                    "bytes": len(raw_bytes),
                    "mime_type": mime,
                    "model": cand_m,
                    "provider": prov,
                }
            except Exception as exc:
                err_s = str(exc)
                errs.append(f"{cand_m}: {err_s}")
                err_l = err_s.lower()
                if "limit: 0" in err_l or "requests per day limit exceeded" in err_l:
                    with self._provider_cooldown_lock:
                        self._zero_quota_models.add(cand_m.lower())
                        if prov == "gemini" and "free_tier" in err_l and clean_kind == "image":
                            for sibling_m in media_pool:
                                if sibling_m.lower().startswith("gemini/"):
                                    self._zero_quota_models.add(sibling_m.lower())
                            skipped_gemini_free_img = True
                            errs.append(
                                "Note: Google AI Studio Free Tier has limit: 0 on Gemini raster image models; "
                                "unlock HuggingFace (`HF_TOKEN` -> FLUX.1-schnell), Together, or Pollinations in API Keys, "
                                "or write standalone `.svg` vector illustrations via `write_file:images/<name>.svg`."
                            )
                            continue
                elif prov != "gemini" and any(q in err_l for q in ("no credits remaining", "insufficient_quota", "exceeded your current quota")):
                    with self._provider_cooldown_lock:
                        self._provider_cooldowns[prov] = float("inf")
        return {"ok": False, "error": "; ".join(errs) or f"Failed to generate {clean_kind}."}

    def ground_swarm_web(self, kind: str, query_or_url: str) -> dict[str, Any]:
        """Execute read-only external web search or URL fetch for Research Scout."""
        import ipaddress
        import socket
        import urllib.parse

        clean_kind = str(kind or "web_search").strip().lower()
        target = str(query_or_url or "").strip()
        if not target:
            return {"ok": False, "error": "Empty query or URL."}

        if clean_kind == "web_search":
            try:
                if hasattr(self, "connector_registry") and self.connector_registry.is_configured("searxng"):
                    c_res = self.connector_registry.execute_action("searxng", "web.search", {"query": target})
                    if c_res.get("ok"):
                        return {"ok": True, "source": "searxng", "text": json.dumps(c_res.get("data") or c_res)[:3500]}
            except Exception:
                pass
            if "gemini" in self._unlocked_providers():
                api_key = self.vault.get_key("gemini", include_env=True) if hasattr(self.vault, "get_key") else None
                if api_key and self._provider_cooldowns.get("gemini") != float("inf"):
                    try:
                        txt = _call_gemini_interactions(
                            "gemini-3.5-flash-lite",
                            f"Search the web and provide a concise, factual summary with source URLs for:\n{target}",
                            api_key,
                            tools=[{"type": "google_search"}],
                        )
                        if txt:
                            return {"ok": True, "source": "gemini_google_search", "text": txt[:3500]}
                    except Exception as exc:
                        return {"ok": False, "error": f"web_search failed: {exc}"}
            return {"ok": False, "error": "No web search provider unlocked (configure SearXNG or unlock Gemini)."}

        # clean_kind == "fetch_url"
        def check_public_url(url: str) -> None:
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None:
                raise ValueError("fetch_url requires a public http:// or https:// URL without credentials.")
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
            if not addresses or any(not ipaddress.ip_address(info[4][0]).is_global for info in addresses):
                raise ValueError("Blocked non-public IP address.")

        class PublicRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                check_public_url(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        try:
            check_public_url(target)
            req = urllib.request.Request(target, headers={"User-Agent": "SabrinaSwarmScout/1.0"})
            with urllib.request.build_opener(PublicRedirect()).open(req, timeout=8.0) as resp:
                source_url = resp.geturl()
                if not resp.headers.get_content_type().startswith("text/"):
                    return {"ok": False, "error": "fetch_url source did not return a text document."}
                raw_html = resp.read(262144).decode("utf-8", errors="replace")
            cleaned = re.sub(r"<(script|style|head|nav)\b[^>]*>.*?</\1\s*>", " ", raw_html, flags=re.S | re.I)
            cleaned = re.sub(r"<[^>]+>", " ", cleaned)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            if not cleaned:
                return {"ok": False, "error": "fetch_url source returned no readable text."}
            return {"ok": True, "source": "http_fetch", "source_url": source_url, "verified": True, "text": cleaned[:3500]}
        except Exception as exc:
            return {"ok": False, "error": f"fetch_url failed: {exc}"}

    def launch_context_first_mission(
        self,
        goal: str,
        *,
        blueprint: Any | None = None,
        coordinator_model: str | None = None,
        allow_workspace_writes: bool = False,
        strict_blueprint: bool = False,
        browser_backend: str = "",
        browser_url: str = "",
        browser_manual_start: bool = False,
        desktop_window_id: str = "",
        desktop_headed: bool = False,
    ) -> dict[str, Any]:
        """Launch a mission using the 3-Layer Context-First Coordinator architecture."""
        browser_session = None
        if browser_backend or browser_url:
            try:
                browser_session = create_interactive_session(
                    browser_backend, browser_url, desktop_window_id=desktop_window_id,
                    headed=bool(desktop_headed), manual_start=bool(browser_manual_start),
                    session_types={"desktop": DesktopSession, "visible": VisibleBrowserSession,
                                   "container": BrowserSession, "fleet": FleetBrowserSession},
                )
                browser_session.check_ready()
            except (ValueError, RuntimeError) as exc:
                return {"ok": False, "error": str(exc), "execution_authorized": False}
        with self._remote_lock:
            ollama_models = list(self._remote_cache.get("ollama_models") or [])
        with self._provider_cooldown_lock:
            cooldowns = dict(self._provider_cooldowns)
            zero_quota = set(self._zero_quota_models)
            unsupported = set(self._unsupported_models)
        return self.swarm_runner.launch_context_first_mission(
            goal,
            blueprint=blueprint,
            coordinator_model=coordinator_model,
            allow_workspace_writes=bool(allow_workspace_writes),
            strict_blueprint=bool(strict_blueprint),
            vault=self.vault,
            cooldowns=cooldowns,
            zero_quota_models=zero_quota,
            unsupported_models=unsupported,
            ollama_models=ollama_models,
            ollama_ready=bool(ollama_models),
            browser_session=browser_session,
        )

    def list_desktop_windows(self) -> dict[str, Any]:
        try:
            return {"ok": True, "windows": available_windows()}
        except RuntimeError as exc:
            return {"ok": False, "error": str(exc), "windows": []}

    def list_interactive_backends(self) -> dict[str, Any]:
        return {"ok": True, "backends": list_interactive_backends()}

    def open_visible_browser_settings(self, backend: str) -> dict[str, Any]:
        settings = {
            "visible-chrome": ("chrome.exe", "Google/Chrome/Application/chrome.exe", "chrome://inspect/#remote-debugging"),
            "visible-edge": ("msedge.exe", "Microsoft/Edge/Application/msedge.exe", "edge://inspect/#remote-debugging"),
        }
        if backend not in settings:
            return {"ok": False, "error": "Choose Chrome or Microsoft Edge."}
        name, relative, address = settings[backend]
        candidates = [
            shutil.which(name),
            *[str(Path(root) / relative) for root in (
                os.environ.get("PROGRAMFILES", ""),
                os.environ.get("PROGRAMFILES(X86)", ""),
                os.environ.get("LOCALAPPDATA", ""),
            ) if root],
        ]
        executable = next((path for path in candidates if path and Path(path).is_file()), None)
        if not executable:
            return {"ok": False, "error": f"{name} was not found on this computer."}
        try:
            subprocess.Popen([executable, address])
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "message": "In the browser, enable 'Allow remote debugging for this browser instance', then launch your mission.",
        }

    def continue_browser_mission(self, mission_id: str) -> dict[str, Any]:
        return self.swarm_runner.continue_browser_mission(mission_id)

    def list_plugin_tabs(self) -> dict[str, Any]:
        """List plugin-declared console tabs. Discovery reads metadata only; no plugin code runs."""
        try:
            from tools.plugin_console import list_tabs
            return list_tabs()
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "tabs": []}

    def run_plugin_tab(self, name: str) -> dict[str, Any]:
        """Run the declared TOOL_TAB operation (falling back to read-only 'dashboard' if Ego SSH is offline)."""
        if not isinstance(name, str):
            return {"ok": False, "error": "invalid_plugin"}
        try:
            from tools.plugin_console import execute_command, list_tabs
            tab = next((t for t in list_tabs()["tabs"] if t["plugin"] == name), None)
            if tab is None:
                return {"ok": False, "error": "tab_not_found"}
            command = "/plugin run " + name + " " + json.dumps({"operation": tab["operation"]})
            res = execute_command(command)
            if not res.get("ok") and name == "kraken_monitor":
                fallback_res = execute_command('/plugin run kraken_monitor {"operation": "dashboard"}')
                if fallback_res.get("ok"):
                    return fallback_res
            return res
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def run_plugin_tab_operation(
        self,
        name: str,
        operation: str,
        args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a declarative read-only operation (e.g., filter or ws_ticks) for a registered TOOL_TAB plugin."""
        if not isinstance(name, str) or not isinstance(operation, str):
            return {"ok": False, "error": "invalid_plugin_or_operation", "execution_authorized": False}
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", operation):
            return {"ok": False, "error": "invalid_operation", "execution_authorized": False}
        if args is not None and not isinstance(args, dict):
            return {"ok": False, "error": "invalid_args", "execution_authorized": False}
        try:
            from tools.plugin_console import discover, execute_command
            plugins, _ = discover()
            meta = plugins.get(name)
            if not meta or meta.get("access") != "read" or not meta.get("tab"):
                return {"ok": False, "error": "read_only_tab_plugin_required", "execution_authorized": False}
            payload = dict(args or {})
            payload["operation"] = operation
            encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False)
            if len(encoded) > 4096:
                return {"ok": False, "error": "args_too_large", "execution_authorized": False}
            out = execute_command(f"/plugin run {name} {encoded}")
            out["execution_authorized"] = False
            return out
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__, "execution_authorized": False}

    def run_plugin_view(self, name: str, operation: str, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run one operation a plugin declared in TOOL_VIEW and return its validated schema v1 view."""
        if not isinstance(name, str) or not isinstance(operation, str):
            return {"ok": False, "error": "invalid_plugin_or_operation"}
        try:
            from tools.plugin_console import run_view
            return run_view(name, operation, inputs)
        except Exception as exc:
            return {"ok": False, "error": type(exc).__name__}

    def run_product_qa(self, target: str = "kraken", run_tests: bool = True) -> dict[str, Any]:
        """Run deterministic Product QA & headless GUI smoke checks against the active or Sabrina workspace."""
        try:
            from sabrina_console_desktop.product_qa import run_product_qa as _exec_qa
        except ImportError:
            from product_qa import run_product_qa as _exec_qa  # type: ignore[no-redef]
        ws_root = self.swarm_runner.workspace_root
        if str(target or "").strip().lower().startswith("kraken") and not (ws_root / "sabrina_console_desktop" / "launch_kraken_monitor.py").is_file():
            if SABRINA_WORKSPACE.is_dir():
                ws_root = SABRINA_WORKSPACE
        res = _exec_qa(ws_root, target=str(target or "kraken").strip() or ".", run_tests=bool(run_tests))
        self.local_feed.insert(
            0,
            {
                "ts": time.time(),
                "time": time.strftime("%H:%M:%S"),
                "ago": "just now",
                "badge": "QA_PASS" if res.get("passed") else "QA_FAIL",
                "actor": "Product QA Verifier",
                "summary": f"Product QA ({res.get('target')}): {res.get('status')} · score={res.get('score')} · defects={len(res.get('defects') or [])}",
            },
        )
        return res

    def get_kraken_replacement_status(self) -> dict[str, Any]:
        from sabrina_console_desktop.kraken_model_review import replacement_status
        return replacement_status()

    def run_kraken_model_review(self) -> dict[str, Any]:
        try:
            from sabrina_console_desktop.kraken_model_review import fetch_paper_review_data, review
            from sabrina_console_desktop.kraken_review_cli import consult
        except ImportError:
            return {"ok": False, "error": "The Kraken review tools are unavailable in this Sabrina installation."}

        if not self._kraken_review_lock.acquire(blocking=False):
            return {"ok": False, "error": "A Kraken paper review is already in progress."}
        try:
            data = fetch_paper_review_data()

            def invoke(provider, model, system, prompt):
                result = self._invoke_provider_text(provider, model, system, prompt,
                                                    self.vault.get_key(provider), max_reply_tokens=768)
                if not self._isolated_test_mode:
                    usage_ledger.record("kraken_paper_review", result)
                return result

            return review(data, invoke, subscription=consult)
        except (ValueError, KeyError, OSError, TypeError, RuntimeError, subprocess.SubprocessError):
            return {"ok": False, "error": "The Kraken forward paper runner did not return a usable snapshot."}
        finally:
            self._kraken_review_lock.release()

    def launch_kraken_monitor_gui(self) -> dict[str, Any]:
        """Launch the standalone Sabrina Kraken Live & $1,000 Paper Desktop Monitor window."""
        launcher = ROOT / "launch_kraken_monitor.py"
        if not launcher.is_file():
            return {"ok": False, "error": f"Kraken launcher not found: {launcher}", "execution_authorized": False}
        py_exe = Path(sys.executable)
        pyw = py_exe.with_name("pythonw.exe")
        exe_to_use = str(pyw if pyw.is_file() else py_exe)
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        base_tcl = Path(sys.base_prefix) / "tcl" / "tcl8.6"
        if (base_tcl / "init.tcl").is_file():
            env.pop("TCL_LIBRARY", None)
            env.pop("TK_LIBRARY", None)
        proc = subprocess.Popen(
            [exe_to_use, "-B", str(launcher)],
            cwd=str(ROOT),
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        msg = f"Launched Sabrina Kraken Desktop Monitor (PID {proc.pid})."
        self.local_feed.insert(
            0,
            {
                "ts": time.time(),
                "time": time.strftime("%H:%M:%S"),
                "ago": "just now",
                "badge": "KRAKEN_GUI",
                "actor": "Sabrina Console",
                "summary": msg,
            },
        )
        return {"ok": True, "pid": proc.pid, "message": msg, "execution_authorized": False}

    def launch_kraken_qa_mission(self, goal: str = "") -> dict[str, Any]:
        """Point workspace to Sabrina repo (if needed) and launch a live Swarm Product QA mission on Kraken GUI."""
        if SABRINA_WORKSPACE.is_dir() and not (self.swarm_runner.workspace_root / "sabrina_console_desktop" / "launch_kraken_monitor.py").is_file():
            self.swarm_runner.set_workspace_root(SABRINA_WORKSPACE)
        eff_goal = str(goal or "").strip() or (
            "Perform full Product Quality Assurance (QA) on the Kraken GUI product "
            "(launch_kraken_monitor.py, tools/kraken_desktop_widget.py, sabrina_operator_console.py KrakenView, "
            "and Kraken unit tests): verify entrypoint imports, headless Tcl/Tk GUI smoke construction, "
            "UI parity, and unit test execution, and produce a sealed QA verification report."
        )
        return self.launch_mission(eff_goal, mode="product_qa", physical_input=False)

    def launch_powershell_mission(self, goal: str) -> dict[str, Any]:
        return self.swarm_runner.launch_mission(goal, mode=WINDOWS_POWERSHELL_MODE)

    def launch_mission(
        self,
        goal: str,
        mode: str = "codex_thought_antigravity_implement",
        physical_input: bool = False,
        coordinator_count: int | None = None,
        workers_per_coordinator: int | None = None,
        quorum_split_threshold: int | None = None,
        enable_work_stealing: bool | None = None,
        allow_workspace_writes: bool = False,
        parent_mission_id: str = "",
        plugin_capabilities: list[str] | None = None,
        auto_followups: int = 0,
    ) -> dict[str, Any]:
        clean_goal = (goal or "").strip()
        if not clean_goal:
            return {
                "ok": False,
                "error": "Enter a non-empty mission goal.",
                "execution_authorized": False,
            }

        now_ts = time.time()
        chosen_mode = mode or "codex_thought_antigravity_implement"
        if type(auto_followups) is not int or not 0 <= auto_followups <= 3:
            return {"ok": False, "error": "Choose zero to three automatic follow-ups.", "execution_authorized": False}
        if auto_followups and (chosen_mode != FOLLOWUP_MODE or physical_input or plugin_capabilities
                               or parent_mission_id or (not allow_workspace_writes and release.matches_goal(clean_goal))):
            return {"ok": False, "error": "Background follow-ups require a standard mission without interactive tools or plugin grants.",
                    "execution_authorized": False}
        if chosen_mode == WINDOWS_POWERSHELL_MODE:
            if plugin_capabilities:
                return {"ok": False, "error": "Plugin capabilities are unavailable in PowerShell inspection missions.", "execution_authorized": False}
            if physical_input or allow_workspace_writes:
                return {"ok": False, "error": "Windows PowerShell inspection is read-only.", "execution_authorized": False}
            return self.launch_powershell_mission(clean_goal)
        if not parent_mission_id and not allow_workspace_writes and (chosen_mode == release.MODE or release.matches_goal(clean_goal)):
            if plugin_capabilities:
                return {"ok": False, "error": "Plugin capabilities are unavailable in release review missions.", "execution_authorized": False}
            return self.launch_release_review()
        try:
            browser_request = parse_browser_request(clean_goal, os.environ.get("SABRINA_LINKEDIN_ACCOUNT_URL", ""))
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}
        if browser_request:
            if plugin_capabilities or auto_followups:
                if auto_followups:
                    return {"ok": False, "error": "Background follow-ups do not open a browser.", "execution_authorized": False}
                return {"ok": False, "error": "Plugin capabilities are unavailable in browser missions.", "execution_authorized": False}
            return self.swarm_runner.launch_mission(clean_goal, browser_request=browser_request)
        if auto_followups:
            return self.followup_coordinator.submit(
                clean_goal, self.swarm_runner.workspace_root, auto_followups, allow_workspace_writes,
            )

        local_action_msg: str | None = None
        try:
            local_action_msg = self._try_local_desktop_action(clean_goal)
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Local action failed: {exc}",
                "execution_authorized": False,
            }

        if local_action_msg:
            self.local_feed.insert(
                0,
                {
                    "ts": now_ts,
                    "time": time.strftime("%H:%M:%S"),
                    "ago": "just now",
                    "badge": "ACTION",
                    "actor": "Sabrina Coordinator",
                    "summary": local_action_msg,
                },
            )
            if len(self.local_feed) > 40:
                self.local_feed = self.local_feed[:40]

        try:
            eff_c = max(1, min(8, int(coordinator_count))) if coordinator_count is not None else None
        except (TypeError, ValueError):
            eff_c = None
        try:
            eff_w = max(1, min(32, int(workers_per_coordinator))) if workers_per_coordinator is not None else None
        except (TypeError, ValueError):
            eff_w = None

        try:
            runner_res = self.swarm_runner.launch_mission(
                clean_goal,
                mode=chosen_mode,
                physical_input=bool(physical_input),
                coordinator_count=eff_c,
                workers_per_coordinator=eff_w,
                quorum_split_threshold=quorum_split_threshold,
                enable_work_stealing=enable_work_stealing,
                allow_workspace_writes=bool(allow_workspace_writes),
                candidate_models=self._get_qualified_portal_candidates(),
                **({"plugin_capabilities": plugin_capabilities} if plugin_capabilities else {}),
                **({"parent_mission_id": parent_mission_id} if parent_mission_id else {}),
            )
        except Exception as exc:
            if local_action_msg:
                return {
                    "ok": True,
                    "message": f"{local_action_msg} (runner note: {exc})",
                    "local_action": local_action_msg,
                    "execution_authorized": False,
                }
            return {
                "ok": False,
                "error": f"Live swarm launch failed: {exc}",
                "execution_authorized": False,
            }

        if not isinstance(runner_res, dict) or not runner_res.get("ok"):
            err = (
                str(runner_res.get("error") or "Live swarm runner failed to launch mission")
                if isinstance(runner_res, dict)
                else "Live swarm runner failed to launch mission"
            )
            if local_action_msg:
                return {
                    "ok": True,
                    "message": f"{local_action_msg} (runner note: {err})",
                    "local_action": local_action_msg,
                    "execution_authorized": False,
                }
            return {
                "ok": False,
                "error": err,
                "execution_authorized": False,
            }

        mission_id = str(
            runner_res.get("mission_id")
            or runner_res.get("objective_id")
            or f"m-{int(now_ts * 1000) % 1000000}"
        )
        oid = str(runner_res.get("objective_id") or mission_id)
        workers_spawned = list(runner_res.get("workers_spawned") or [])
        state = self.swarm_runner.get_state()
        self.active_missions = list(state.get("active_missions") or [])
        base_msg = str(
            runner_res.get("message")
            or f"Mission [{mission_id}] launched ({len(workers_spawned)} workers)"
        )
        msg = f"{local_action_msg} · {base_msg}" if local_action_msg else base_msg
        result: dict[str, Any] = {
            "ok": True,
            "mission_id": mission_id,
            "objective_id": oid,
            "goal": clean_goal,
            "mode": str(runner_res.get("mode") or chosen_mode),
            "state": str(runner_res.get("state") or "ACTIVE"),
            "ratio": str(runner_res.get("ratio") or "1:4"),
            "coordinator_count": int(runner_res.get("coordinator_count") or 1),
            "workers_per_coordinator": int(runner_res.get("workers_per_coordinator") or 4),
            "workers_spawned": workers_spawned,
            "active_worker_count": int(state.get("active_worker_count", len(workers_spawned))),
            "local_action": local_action_msg,
            "legacy_forwarded": False,
            "execution_authorized": False,
            "message": msg,
        }
        return result

    def open_home_depot_order_draft(self, mission_id: str) -> dict[str, Any]:
        """Open the verified local draft for a mission when the operator clicks Review."""
        import webbrowser

        invalid = {"ok": False, "error": "A completed Home Depot order draft is required.", "execution_authorized": False}
        if not isinstance(mission_id, str) or not re.fullmatch(r"m-[0-9a-f]{8}", mission_id):
            return invalid
        output = self.swarm_runner.get_mission_output(mission_id).get("output") or {}
        if (output.get("state") != "PASS" or output.get("mode") != home_depot_order.MODE
                or not output.get("order_draft")):
            return invalid

        root = self.swarm_runner.artifacts_dir.resolve()
        target = root / f"{mission_id}-home-depot-order.html"
        try:
            if target.resolve() != target or not target.is_file() or str(target) != output.get("artifact_path"):
                return {"ok": False, "error": "The saved order draft is unavailable.", "execution_authorized": False}
            if not webbrowser.open(target.as_uri(), new=2):
                return {"ok": False, "error": "The browser could not open the order draft.", "execution_authorized": False}
        except (OSError, RuntimeError) as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}
        return {"ok": True, "message": "Order draft opened.", "execution_authorized": False}

    def stop_mission(self, mission_id: str = "") -> dict[str, Any]:
        try:
            background = self.followup_coordinator.stop(mission_id or None)
            if not mission_id:
                ev_cancel = getattr(self, "_evo_bench_cancel_event", None)
                if ev_cancel is not None:
                    ev_cancel.set()
                cur_eb = getattr(self, "_evo_bench_state", None)
                if isinstance(cur_eb, dict) and cur_eb.get("running"):
                    cur_eb["running"] = False
                    cur_eb["step"] = "stopped"
                    cur_eb["detail"] = "Stopped by operator."
            res = self.swarm_runner.stop_mission(mission_id or None)
            state = self.swarm_runner.get_state()
            self.active_missions = list(state.get("active_missions") or [])
            if isinstance(res, dict):
                if background:
                    res = {**res, "ok": True, "background_stop_requested": background,
                           "message": f"Stop requested for {len(background)} background chain(s)."}
                return {**res, "execution_authorized": False}
            return {
                "ok": True,
                "mission_id": mission_id or None,
                "stopped_missions": [],
                "spun_down_workers": [],
                "active_worker_count": int(state.get("active_worker_count", 0)),
                "execution_authorized": False,
                "message": "Stopped active missions.",
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    def spawn_worker(
        self,
        name: str,
        role: str,
        model: str = "",
        task: str = "",
    ) -> dict[str, Any]:
        try:
            res = self.swarm_runner.spawn_worker(
                name=name,
                role=role,
                model=model,
                task=task,
            )
            if isinstance(res, dict):
                return {**res, "execution_authorized": False}
            return {"ok": False, "error": "Invalid runner response", "execution_authorized": False}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    def spindown_worker(self, worker_id: str) -> dict[str, Any]:
        try:
            res = self.swarm_runner.spindown_worker(worker_id)
            if isinstance(res, dict):
                return {**res, "execution_authorized": False}
            return {
                "ok": False,
                "error": "Invalid runner response",
                "worker_id": worker_id,
                "active_worker_count": int(self.swarm_runner.get_state().get("active_worker_count", 0)),
                "execution_authorized": False,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "worker_id": worker_id,
                "execution_authorized": False,
            }

    def list_completed_runs(self) -> dict[str, Any]:
        return {"ok": True, "runs": self.swarm_runner.list_completed_runs(), "execution_authorized": False}

    def get_next_step_draft(self, mission_id: str) -> dict[str, Any]:
        return self.swarm_runner.get_next_step_draft(mission_id)

    def launch_mission_followup(self, parent_mission_id: str, goal: str, allow_workspace_writes: bool = False) -> dict[str, Any]:
        if not parent_mission_id:
            return {"ok": False, "error": "Select a parent mission.", "execution_authorized": False}
        result = self.launch_mission(goal, allow_workspace_writes=allow_workspace_writes, parent_mission_id=parent_mission_id)
        if result.get("ok") and result.get("mission_id"):
            result["parent_mission_id"] = parent_mission_id
        return result

    def get_mission_output(self, mission_id: str = "") -> dict[str, Any]:
        try:
            res = self.swarm_runner.get_mission_output(mission_id=mission_id)
            if isinstance(res, dict):
                return {**res, "execution_authorized": False}
            return {"ok": False, "error": "Invalid runner response", "execution_authorized": False}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    def clear_completed(self) -> dict[str, Any]:
        try:
            res = self.swarm_runner.clear_completed()
            if isinstance(res, dict):
                return {**res, "execution_authorized": False}
            return {"ok": True, "execution_authorized": False}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    def clear_completed_missions(self) -> dict[str, Any]:
        return self.clear_completed()

    def decide_mission_audit(self, mission_id: str, choice: str, custom_note: str = "") -> dict[str, Any]:
        try:
            return self.swarm_runner.decide_mission_audit(mission_id, choice, custom_note=custom_note)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def swap_roles(self, current_mode: str, objective: str) -> dict[str, Any]:
        try:
            res = swap_swarm_pipeline_roles(current_mode, objective)
            return {"ok": True, "new_mode": res.get("new_mode"), "message": f"Swapped to {res.get('new_mode')}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def release_lease(self) -> dict[str, Any]:
        try:
            res = force_release_desktop_lease()
            return {"ok": True, "message": "Cursor lease released." if res.get("released") else "No active lease."}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def decide_pending(self, kind: str, item_id: str, approve: bool) -> dict[str, Any]:
        try:
            decide_pending_item(kind, item_id, bool(approve))
            return {"ok": True, "message": f"{'Approved' if approve else 'Rejected'} {item_id}"}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def job_action(self, job_id: str, action: str) -> dict[str, Any]:
        target_action = str(action or "run-now").strip().lower()
        with self._remote_lock:
            api_ok = bool(self._remote_cache.get("api_ok", False))
            has_probed = float(self._remote_cache.get("last_refresh", 0.0) or 0.0) > 0.0
        if has_probed and not api_ok:
            msg = f"Backend :8087 is offline — cannot run '{target_action}' on job {job_id}."
            return {"ok": False, "error": "Backend :8087 offline", "message": msg, "job_id": job_id, "action": target_action}
        try:
            _http_json(f"{API_8087}/api/schedule/{job_id}/{target_action}", method="POST", payload={}, timeout=2.5)
            target_state = "queued" if target_action in ("run-now", "run", "resume") else ("paused" if target_action == "pause" else "cancelled")
            with self._remote_lock:
                cached_jobs = self._remote_cache.get("jobs", [])
                for cj in cached_jobs:
                    if cj.get("id") == job_id:
                        cj["status"] = target_state
                        cj["state"] = target_state
                        break
            self._trigger_remote_refresh(force=True)
            msg = f"Job {job_id}: {target_action} dispatched (now {target_state})."
            return {"ok": True, "message": msg, "job_id": job_id, "action": target_action, "state": target_state}
        except Exception as exc:
            err_s = str(exc)
            low = err_s.lower()
            if "timed out" in low or "refused" in low or "10054" in low or "10061" in low:
                msg = f"Backend :8087 offline or unreachable ({err_s}) — cannot run '{target_action}' on job {job_id}."
                return {"ok": False, "error": "Backend :8087 offline", "message": msg, "job_id": job_id, "action": target_action}
            return {"ok": False, "error": err_s, "message": f"Job {job_id} {target_action} failed: {err_s}"}

    def _save_operator_chat_log(self) -> None:
        try:
            with self._chat_lock:
                payload = {"messages": list(self.chat_log[-100:])}
            self._chat_log_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._chat_log_path.with_suffix(".json.tmp")
            tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, self._chat_log_path)
        except Exception:
            pass

    def _restore_operator_chat_log(self) -> None:
        if not self._chat_log_path.is_file():
            return
        try:
            data = json.loads(self._chat_log_path.read_text(encoding="utf-8"))
            raw_msgs = data.get("messages") if isinstance(data, dict) else (data if isinstance(data, list) else [])
            if not isinstance(raw_msgs, list):
                return
            restored: list[dict[str, Any]] = []
            for item in raw_msgs[-100:]:
                if not isinstance(item, dict):
                    continue
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                restored.append({
                    "sender": str(item.get("sender") or "operator"),
                    "time": str(item.get("time") or ""),
                    "text": text,
                })
            if restored:
                with self._chat_lock:
                    self.chat_log = restored
                    self._chat_log_revision += 1
        except Exception:
            pass

    def _append_chat(self, sender: str, text: str) -> None:
        with self._chat_lock:
            self.chat_log.append({
                "sender": sender,
                "time": time.strftime("%I:%M:%S %p"),
                "text": text,
            })
            if len(self.chat_log) > 100:
                self.chat_log = self.chat_log[-100:]
            self._chat_log_revision += 1
        self._save_operator_chat_log()

    def get_chat_view(self) -> dict[str, Any]:
        """Read chat state without waiting for the full dashboard snapshot."""
        job = self.get_chat_job_status()
        with self._chat_lock:
            return {"chat_job": job, "chat_history": list(self.chat_log[-40:]),
                    "chat_revision": self._chat_log_revision}

    def get_chat_job_status(self) -> dict[str, Any]:
        with self._chat_job_lock:
            job = dict(self._chat_job)
        if job.get("running") and float(job.get("started_at") or 0.0) > 0.0:
            job["elapsed_sec"] = round(max(0.0, time.monotonic() - float(job["started_at"])), 1)
        job.pop("started_at", None)
        return job

    def cancel_chat_job(self) -> dict[str, Any]:
        with self._chat_job_lock:
            was_running = bool(self._chat_job.get("running"))
            if self._chat_cancel_event is not None:
                self._chat_cancel_event.set()
            self._chat_job["running"] = False
            self._chat_job["cancelled"] = True
            self._chat_job["ok"] = False
            self._chat_job["stage"] = "Cancelled by operator."
            self._chat_job["error"] = "Cancelled by operator."
        if was_running:
            self._append_chat("Sabrina Coordinator", "[CANCELLED] Operator cancelled the in-flight chat request.")
        return {"ok": True, "cancelled": True, "was_running": was_running, "chat_job": self.get_chat_job_status()}

    def send_chat_async(self, text: str, mode: str, target: str) -> dict[str, Any]:
        """Non-blocking Operator Chat dispatch so long hive_council / model turns never freeze the pywebview UI."""
        clean = (text or "").strip()
        if not clean:
            return {"ok": False, "error": "Empty message."}
        with self._chat_job_lock:
            if self._chat_job.get("running"):
                return {
                    "ok": False,
                    "busy": True,
                    "error": "A chat turn is already running. Click Cancel to stop it first.",
                    "chat_job": self.get_chat_job_status(),
                }
            job_id = f"chat-{time.time_ns() & 0xFFFFFFFF:08x}"
            cancel_ev = threading.Event()
            self._chat_cancel_event = cancel_ev
            self._chat_job = {
                "running": True,
                "job_id": job_id,
                "mode": str(mode or "hive_council"),
                "target": str(target or ""),
                "stage": f"Dispatching via {mode or 'hive_council'}…",
                "started_at": time.monotonic(),
                "elapsed_sec": 0.0,
                "cancelled": False,
                "ok": True,
                "error": "",
            }

        self._append_chat("operator", clean)

        def _progress(msg: str) -> None:
            with self._chat_job_lock:
                if self._chat_job.get("job_id") == job_id and self._chat_job.get("running"):
                    self._chat_job["stage"] = str(msg or "")[:180]

        def _worker() -> None:
            try:
                res = self._execute_chat_turn(
                    clean,
                    mode,
                    target,
                    progress=_progress,
                    cancel_event=cancel_ev,
                )
                with self._chat_job_lock:
                    if self._chat_job.get("job_id") != job_id:
                        return
                    elapsed = round(max(0.0, time.monotonic() - float(self._chat_job.get("started_at") or time.monotonic())), 1)
                    self._chat_job["elapsed_sec"] = elapsed
                    if cancel_ev.is_set() or (isinstance(res, dict) and res.get("cancelled")):
                        self._chat_job["running"] = False
                        self._chat_job["cancelled"] = True
                        self._chat_job["ok"] = False
                        self._chat_job["stage"] = "Cancelled by operator."
                        return
                    ok = bool(isinstance(res, dict) and res.get("ok", True))
                    err = str((res or {}).get("error") or (res or {}).get("message") or "") if (isinstance(res, dict) and not ok) else ""
                    self._chat_job["running"] = False
                    self._chat_job["ok"] = ok
                    self._chat_job["error"] = err
                    self._chat_job["stage"] = f"Delivered ({elapsed}s)." if ok else (err or "Chat failed.")
                    if ok and res.get("mode") == home_depot_order.MODE:
                        self._chat_job["mission_id"] = res["mission_id"]
            except Exception as exc:
                with self._chat_job_lock:
                    if self._chat_job.get("job_id") == job_id:
                        self._chat_job["running"] = False
                        self._chat_job["ok"] = False
                        self._chat_job["error"] = str(exc)
                        self._chat_job["stage"] = f"Error: {exc}"
                if not cancel_ev.is_set():
                    self._append_chat("Chat Error", str(exc))

        t = threading.Thread(target=_worker, name=f"sabrina-chat-{job_id}", daemon=True)
        t.start()
        return {
            "ok": True,
            "accepted": True,
            "async": True,
            "job_id": job_id,
            "chat_job": self.get_chat_job_status(),
        }

    def run_signal_research(self, workspace_root: str | None = None) -> dict[str, Any]:
        """Run one generation of the gainers signal research loop with a live swarm model.

        The model proposes signals; the promotion ledger registers them before scoring and only
        its own scoring can promote one. Research only: this path cannot place orders.
        """
        from tools.crypto_strategy_rsi_loop import CryptoStrategyRsiEngine, format_cycle_report

        try:
            model_id = self.swarm_runner._resolve_model_for_role("planner_architect")
        except Exception:
            model_id = "gemini-2.5-flash"
        try:
            engine = CryptoStrategyRsiEngine(
                workspace_root=Path(workspace_root) if workspace_root else None,
                llm_caller=self._call_swarm_llm, model_id=model_id,
            )
            history = []
            if engine.history_file.is_file():
                history = [json.loads(x) for x in engine.history_file.read_text(encoding="utf-8").splitlines() if x.strip()]
            if history and history[-1].get("stopping_rule_triggered"):
                return {"ok": True, "execution_authorized": False, "stopped": True,
                        "reply": "The stopping rule was reached in generation "
                                 f"{history[-1].get('generation')}: {history[-1].get('verdict_summary')} "
                                 "No new generation was run. Change the inputs (new data or features) before starting again."}
            result = engine.run_cycle(generation=1 + max((int(h.get("generation") or 0) for h in history), default=0))
        except Exception as exc:
            return {"ok": False, "execution_authorized": False, "error": f"Signal research failed: {exc}"}
        return {"ok": True, "execution_authorized": False, "generation": result.generation,
                "promoted": len(result.promoted), "disproven": len(result.disproven),
                "reply": format_cycle_report(result, engine.last_author)}

    def send_chat(self, text: str, mode: str, target: str) -> dict[str, Any]:
        clean = (text or "").strip()
        if not clean:
            return {"ok": False, "error": "Empty message."}
        self._append_chat("operator", clean)
        return self._execute_chat_turn(clean, mode, target)

    def _execute_chat_turn(
        self,
        clean: str,
        mode: str,
        target: str,
        *,
        progress: Any = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        lower = clean.lower()
        if lower == "/plugin" or lower.startswith("/plugin "):
            from tools.plugin_console import execute_command
            res = execute_command(clean)
            self._append_chat("Plugins", res.get("reply") or res.get("error", "Plugin command failed"))
            return res

        if lower == "/signals":
            res = self.run_signal_research()
            self._append_chat("Signal Research", res.get("reply") or res.get("error", "Signal research failed"))
            return res

        if lower.startswith(("/research", "/codex", "/experiment")):
            from tools.research_console import execute_command
            res = execute_command(clean, progress=progress, cancel_event=cancel_event)
            self._append_chat("Research & Offline Experiments", res.get("reply") or res.get("error", "Research failed"))
            return res

        # Interactive directive routing (Codex / Claude / Antigravity / Cursor style)
        if lower.startswith(("/mission ", "/swarm ", "/dispatch ", "/run ")):
            goal = clean.split(" ", 1)[1].strip()
            c_val = None
            w_val = None
            m_ratio = re.search(r"(?:--ratio|-r|ratio=)\s*(\d+)[:/](\d+)", goal, re.IGNORECASE)
            if m_ratio:
                c_val = int(m_ratio.group(1))
                w_val = int(m_ratio.group(2))
                goal = re.sub(r"(?:--ratio|-r|ratio=)\s*\d+[:/]\d+", "", goal).strip()
            res = self.launch_mission(
                goal,
                mode="codex_thought_antigravity_implement",
                physical_input=False,
                coordinator_count=c_val,
                workers_per_coordinator=w_val,
            )
            self._append_chat("Sabrina Coordinator", f"[DISPATCHED] {res.get('message', goal)}")
            return res

        if mode != "workspace_agent" and home_depot_order.matches_goal(clean):
            if cancel_event and cancel_event.is_set():
                return {"ok": False, "cancelled": True}
            result = self.launch_mission(clean)
            reply = (
                f"I started the Home Depot draft [{result['mission_id']}]. "
                "Open Swarm Monitor and select Review order draft in Live Mission Output when it’s ready."
                if result.get("ok") else str(result.get("error") or "The Home Depot draft could not start.")
            )
            self._append_chat("Sabrina Coordinator", reply)
            return result

        if lower.startswith("/command ") or mode == "swarm_command":
            prompt = clean.split(" ", 1)[1].strip() if lower.startswith("/command ") else clean
            turn_res = self.swarm_command.run_turn(prompt)
            self._append_chat(
                f"Swarm Command ({turn_res.mode.value})",
                f"[owner={turn_res.active_owner}] {turn_res.response}",
            )
            return {
                "ok": True,
                "mode": turn_res.mode.value,
                "active_owner": turn_res.active_owner,
                "response": turn_res.response,
                "artifacts_written": turn_res.artifacts_written,
                "verdict": turn_res.verdict,
                "circuit_breaker_triggered": turn_res.circuit_breaker_triggered,
                "blackboard": self.blackboard.snapshot(),
            }

        if lower == "/swap":
            res = self.swap_roles("codex_thought_antigravity_implement", "Chat directive role swap")
            self._append_chat("Sabrina Coordinator", f"[SWAP] {res.get('message', res)}")
            return res

        if lower.startswith("/skills"):
            query = clean[len("/skills"):].strip()
            res = self.search_skills(query=query, limit=15, offset=0)
            items = res.get("items") or []
            lines = [f"Found {res.get('total', len(items))} skills:"] + [
                f"- {s['name']} ({s.get('source', 'local')}): {s.get('description', '')[:100]}"
                for s in items
            ]
            self._append_chat("Sabrina Skills", "\n".join(lines))
            return {"ok": True, "message": f"Listed {len(items)} skills."}

        if lower == "/status":
            snap = self.get_snapshot()
            swarm = snap.get("swarm") or {}
            summary = (
                f"API :8087: {'connected' if snap.get('api_ok') else 'offline (local fallback)'}\n"
                f"Workers: {len(swarm.get('workers') or [])} · Objectives: {len(swarm.get('objectives') or [])}\n"
                f"Safety: {swarm.get('safety_lock', 'LOCKED')} · Verdict: {swarm.get('verifier_verdict', 'n/a')}"
            )
            self._append_chat("Sabrina Coordinator", summary)
            return {"ok": True, "message": "Status posted."}

        if lower.startswith("/gemini "):
            q = clean.split(" ", 1)[1].strip()
            res = self.gemini_analyze(question=q)
            if res.get("ok"):
                analysis = res.get("analysis") or {}
                reply = f"[{analysis.get('health', 'ok')}] {analysis.get('summary', '')}"
                self._append_chat(f"Gemini ({res.get('model')})", reply)
                return res
            self._append_chat("Gemini Copilot", f"[ERROR] {res.get('message') or res.get('error')}")
            return res

        if mode in {"sabrina", "workspace_agent"}:
            if progress:
                progress("Running Sabrina's selected workspace harness…")
            if mode == "sabrina" and self.workspace_sandbox is None:
                return self._chat_without_workspace(clean, cancel_event=cancel_event)
            return self.send_workspace_chat(clean, effort="low" if mode == "sabrina" else "medium",
                                            cancel_event=cancel_event)

        chosen_target = (target or "").strip()
        if mode == "hive_council" or chosen_target in ("", "hive_council"):
            with self._remote_lock:
                has_cached_ollama = bool(self._remote_cache.get("ollama_models"))
            if not has_cached_ollama:
                res = self._fallback_chat_to_unlocked_provider(clean, "hive_council", "hive_council", progress=progress)
                if res and res.get("ok"):
                    return res
            return self._dispatch_swarm_portal_chat(
                clean,
                mode="hive_council",
                target="hive_council",
                progress=progress,
                cancel_event=cancel_event,
            )

        # direct_node: check swarm_chat_portal local/portal targets first, then Ollama, then unlocked vault providers.
        if self._is_portal_target(chosen_target):
            return self._dispatch_swarm_portal_chat(
                clean,
                mode="direct_node",
                target=chosen_target,
                progress=progress,
                cancel_event=cancel_event,
            )

        if chosen_target.startswith(("ollama/", "local:")):
            if progress:
                progress(f"Querying {chosen_target}…")
            return self._dispatch_ollama_chat(chosen_target, clean)

        provider = infer_model_provider(chosen_target)
        if provider == "ollama":
            if progress:
                progress(f"Querying {chosen_target}…")
            return self._dispatch_ollama_chat(chosen_target, clean)

        if provider in {
            "gemini", "google_vertex", "aws_bedrock", "openai", "anthropic", "openrouter",
            "xai", "deepseek", "groq", "mistral", "together",
            "runpod", "vastai", "lambda_labs", "modal", "coreweave", "custom_gpu",
        }:
            api_key = self.vault.get_key(provider) if hasattr(self.vault, "get_key") else None
            if not api_key:
                err_msg = f"Provider '{provider}' is locked for model '{chosen_target}'. Add an API key in API Keys."
                self._append_chat("Chat Error", err_msg)
                return {"ok": False, "error": err_msg}
            if progress:
                progress(f"Querying {provider} ({chosen_target})…")
            return self._dispatch_vault_provider_chat(provider, chosen_target, clean, api_key)

        return self._dispatch_swarm_portal_chat(
            clean,
            mode=mode or "direct_node",
            target=chosen_target,
            progress=progress,
            cancel_event=cancel_event,
        )

    def _is_portal_target(self, target: str) -> bool:
        if target.startswith("portal:"):
            return True
        try:
            from tools import swarm_chat_portal as scp

            for attr in ("LOCAL_FLEET", "EXTERNAL_WSP_AND_ARENA_PORTALS", "external_nodes", "EXTERNAL_NODES"):
                entries = getattr(scp, attr, None)
                if isinstance(entries, dict) and target in entries:
                    return True
                if isinstance(entries, list) and any(
                    isinstance(e, dict) and e.get("id") == target for e in entries
                ):
                    return True
        except Exception:
            pass
        return False

    def _dispatch_swarm_portal_chat(
        self,
        clean: str,
        mode: str,
        target: str,
        *,
        progress: Any = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        try:
            from tools.swarm_chat_portal import send_swarm_chat

            kwargs: dict[str, Any] = {"message": clean, "mode": mode, "target": target}
            sig_params = inspect.signature(send_swarm_chat).parameters
            if progress is not None and "progress" in sig_params:
                kwargs["progress"] = progress
            if cancel_event is not None and "cancel_event" in sig_params:
                kwargs["cancel_event"] = cancel_event
            res = send_swarm_chat(**kwargs)
            if cancel_event is not None and cancel_event.is_set():
                return {"ok": False, "cancelled": True, "error": "Chat cancelled by operator."}
            if isinstance(res, dict):
                if res.get("cancelled"):
                    return res
                res.setdefault("ok", not bool(res.get("error")))
                reply_text = str(
                    res.get("reply")
                    or res.get("primary_reply")
                    or res.get("summary")
                    or res.get("message")
                    or res.get("error")
                    or ""
                ).strip()
                has_valid_responses = any(
                    isinstance(r, dict) and r.get("ok") and str(r.get("reply") or "").strip()
                    for r in (res.get("responses") or [])
                )
                if reply_text and has_valid_responses:
                    self._append_chat(target or mode or "swarm", reply_text)
                    return res

                # Local Ollama / guild produced no usable answer -> automatic cloud failover!
                fallback_res = self._fallback_chat_to_unlocked_provider(clean, target, mode, progress=progress)
                if fallback_res and fallback_res.get("ok"):
                    return fallback_res
                if reply_text:
                    self._append_chat(target or mode or "swarm", reply_text)
                else:
                    self._append_chat("Swarm Coordinator", "All local Ollama nodes are offline. Configure an API key in API Keys or start Ollama.")
                return res
            self._append_chat(target or "swarm", str(res))
            return {"ok": True, "result": res}
        except Exception as exc:
            import traceback

            if cancel_event is None or not cancel_event.is_set():
                fallback_res = self._fallback_chat_to_unlocked_provider(clean, target, mode, progress=progress)
                if fallback_res and fallback_res.get("ok"):
                    return fallback_res
                self._append_chat("Swarm Chat Error", str(exc))
            return {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}

    def _fallback_chat_to_unlocked_provider(
        self,
        clean: str,
        target: str,
        mode: str,
        *,
        progress: Any = None,
    ) -> dict[str, Any] | None:
        unlocked = self._unlocked_providers()
        if "gemini" in unlocked:
            api_key = self.vault.get_key("gemini")
            if api_key:
                if progress:
                    progress("Failing over to Google Gemini (gemini-3.5-flash-lite)…")
                sys_prompt = (
                    "You are the Sabrina Multi-Agent Swarm Coordinator. "
                    "Analyze the user prompt with technical depth, architectural rigor, and clarity. "
                    "Provide a crisp, actionable answer."
                )
                res = self._invoke_provider_text(
                    "gemini",
                    "gemini-3.5-flash-lite",
                    sys_prompt,
                    clean,
                    api_key=api_key,
                )
                if res.get("ok") and str(res.get("text") or "").strip():
                    text = str(res["text"]).strip()
                    speaker = f"Sabrina Coordinator (gemini-3.5-flash-lite)"
                    self._append_chat(speaker, text)
                    return {
                        "ok": True,
                        "reply": text,
                        "primary_reply": text,
                        "speaker": speaker,
                        "provider": "gemini",
                        "model": "gemini-3.5-flash-lite",
                        "used_fallback": True,
                    }
        return None

    def _dispatch_ollama_chat(self, model_id: str, prompt: str) -> dict[str, Any]:
        model_tag = model_id
        while True:
            stripped = False
            for prefix in ("ollama/", "local:"):
                if model_tag.startswith(prefix):
                    model_tag = model_tag[len(prefix) :]
                    stripped = True
            if not stripped:
                break
        try:
            data = _http_json(
                f"{OLLAMA_BASE}/api/chat",
                method="POST",
                payload={
                    "model": model_tag,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
                timeout=30.0,
            )
            reply = (data.get("message") or {}).get("content") or data.get("response") or ""
            self._append_chat(model_id, str(reply))
            return {"ok": True, "reply": str(reply), "model": model_id, "provider": "ollama"}
        except Exception as exc:
            self._append_chat(f"{model_id} (Error)", str(exc))
            return {"ok": False, "error": str(exc), "model": model_id, "provider": "ollama"}

    def _dispatch_vault_provider_chat(
        self,
        provider: str,
        model_id: str,
        prompt: str,
        api_key: str,
    ) -> dict[str, Any]:
        bare_model = model_id
        if provider != "gemini" and bare_model.startswith(f"{provider}/"):
            bare_model = bare_model[len(provider) + 1 :]
        if provider == "gemini" and bare_model.startswith("models/"):
            bare_model = bare_model[len("models/") :]

        try:
            if provider == "gemini":
                reply = _call_gemini_interactions(bare_model, prompt, api_key)
            elif provider == "anthropic":
                data = _http_json(
                    "https://api.anthropic.com/v1/messages",
                    method="POST",
                    payload={
                        "model": bare_model,
                        "max_tokens": 1024,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                    timeout=30.0,
                )
                blocks = data.get("content") or []
                reply = "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))
            else:
                endpoints = {
                    "openai": "https://api.openai.com/v1/chat/completions",
                    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
                    "xai": "https://api.x.ai/v1/chat/completions",
                    "deepseek": "https://api.deepseek.com/chat/completions",
                    "groq": "https://api.groq.com/openai/v1/chat/completions",
                    "mistral": "https://api.mistral.ai/v1/chat/completions",
                    "together": "https://api.together.xyz/v1/chat/completions",
                }
                url = endpoints[provider]
                data = _http_json(
                    url,
                    method="POST",
                    payload={
                        "model": bare_model,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=30.0,
                )
                choices = data.get("choices") or []
                reply = ((choices[0].get("message") or {}).get("content") or "") if choices else ""

            self._append_chat(model_id, str(reply))
            return {"ok": True, "reply": str(reply), "model": model_id, "provider": provider}
        except Exception as exc:
            self._append_chat(f"{model_id} (Error)", str(exc))
            return {"ok": False, "error": str(exc), "model": model_id, "provider": provider}

    def _invoke_provider_text(
        self,
        provider: str,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        api_key: str | None = None,
        *,
        local_timeout: float = 90.0,
        local_reply_tokens: int | None = None,
        attachments: list[Path] | None = None,
        max_reply_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Direct non-chat LLM inference call for Swarm workers (never writes to operator chat_log)."""
        t0 = time.monotonic()
        prov = str(provider or "").strip().lower()
        bare_model = str(model_id or "").strip()
        if prov == "ollama":
            while True:
                stripped = False
                for prefix in ("ollama/", "local:"):
                    if bare_model.startswith(prefix):
                        bare_model = bare_model[len(prefix) :]
                        stripped = True
                if not stripped:
                    break
            try:
                ollama_opts: dict[str, Any] = {"num_ctx": 16384}
                if local_reply_tokens:
                    ollama_opts["num_predict"] = local_reply_tokens
                user_msg: dict[str, Any] = {"role": "user", "content": user_prompt}
                if attachments and any(vt in bare_model.lower() for vt in ("gemma3", "llava", "vision", "vl", "minicpm-v", "pixtral", "moondream")):
                    b64_imgs: list[str] = []
                    for att in attachments[:4]:
                        try:
                            p_att = Path(att)
                            if p_att.is_file() and p_att.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} and 0 < p_att.stat().st_size <= 8_000_000:
                                b64_imgs.append(base64.b64encode(p_att.read_bytes()).decode("ascii"))
                        except Exception:
                            continue
                    if b64_imgs:
                        user_msg["images"] = b64_imgs
                ollama_payload = {
                    "model": bare_model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        user_msg,
                    ],
                    "options": ollama_opts,
                    "stream": False,
                }
                try:
                    local_endpoint = interface_privacy.private_ollama_url(OLLAMA_BASE) or "http://127.0.0.1:11434"
                    data = _http_json(
                        f"{local_endpoint}/api/chat",
                        method="POST",
                        payload=ollama_payload,
                        timeout=local_timeout,
                        direct=True,
                    )
                except Exception as primary_ollama_exc:
                    if local_endpoint != "http://127.0.0.1:11434":
                        data = _http_json(
                            "http://127.0.0.1:11434/api/chat",
                            method="POST",
                            payload=ollama_payload,
                            timeout=local_timeout,
                            direct=True,
                        )
                    else:
                        raise primary_ollama_exc
                reply = str((data.get("message") or {}).get("content") or data.get("response") or "").strip()
                if not reply:
                    return {"ok": False, "error": "empty Ollama response", "provider": "ollama", "model": model_id}
                o_tok = int(data.get("prompt_eval_count") or 0) + int(data.get("eval_count") or 0)
                if o_tok <= 0:
                    o_tok = max(1, (len(system_prompt) + len(user_prompt) + len(reply)) // 4)
                return {
                    "ok": True,
                    "text": reply,
                    "model": model_id,
                    "provider": "ollama",
                    "tokens_used": o_tok,
                    "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc), "provider": "ollama", "model": model_id}

        if prov == "portal" or bare_model.startswith("portal:"):
            portal_id = bare_model.split(":", 1)[1].strip() if ":" in bare_model else bare_model
            try:
                from harnesses.external.collective_pool import KindCollectivePool

                pool = KindCollectivePool()
                combined_prompt = f"{system_prompt}\n\n{user_prompt}".strip() if system_prompt else user_prompt
                p_res = pool.invoke_member(portal_id, combined_prompt, supervised=False)
                if p_res.get("ok") and str(p_res.get("reply") or "").strip():
                    reply_p = str(p_res["reply"]).strip()
                    return {
                        "ok": True,
                        "text": reply_p,
                        "model": f"portal:{portal_id}",
                        "provider": "portal",
                        "tokens_used": max(1, (len(combined_prompt) + len(reply_p)) // 4),
                        "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
                    }
                return {
                    "ok": False,
                    "error": str(p_res.get("error") or p_res.get("failure") or "portal unavailable"),
                    "provider": "portal",
                    "model": model_id,
                }
            except Exception as exc:
                return {"ok": False, "error": str(exc), "provider": "portal", "model": model_id}

        if not api_key and prov not in {"pollinations", "lmstudio", "custom_gpu"}:
            return {"ok": False, "error": f"No API key unlocked for {prov}", "provider": prov, "model": model_id}

        if prov != "gemini" and bare_model.startswith(f"{prov}/"):
            bare_model = bare_model[len(prov) + 1 :]
        if prov == "gemini":
            for prefix in ("gemini/", "models/"):
                if bare_model.startswith(prefix):
                    bare_model = bare_model[len(prefix) :]

        try:
            provider_usage: dict[str, Any] = {}
            finish_reason = ""  # provider stop reason; "" = not exposed by this path
            if prov == "gemini":
                reply = _call_gemini_interactions(
                    bare_model,
                    user_prompt,
                    api_key,
                    system_prompt=system_prompt,
                    attachments=attachments,
                    usage_out=provider_usage,
                    max_output_tokens=int(max_reply_tokens or SWARM_DEFAULT_REPLY_TOKENS),
                )
                raw_finish = str(provider_usage.get("finish_reason") or "")
                finish_reason = "max_tokens" if raw_finish in {"incomplete", "MAX_TOKENS"} else raw_finish
            elif prov == "anthropic":
                data = _http_json(
                    "https://api.anthropic.com/v1/messages",
                    method="POST",
                    payload={
                        "model": bare_model,
                        # A max_tokens value is a ceiling, not a target: the model stops when it is done.
                        "max_tokens": int(max_reply_tokens or SWARM_DEFAULT_REPLY_TOKENS),
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": user_prompt}],
                    },
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                    timeout=30.0,
                )
                blocks = data.get("content") or []
                reply = "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict)).strip()
                finish_reason = str(data.get("stop_reason") or "")
            else:
                lm_base = os.environ.get("SABRINA_LOCAL_OPENAI_URL", "http://127.0.0.1:1234/v1").rstrip("/")
                endpoints = {
                    "openai": "https://api.openai.com/v1/chat/completions",
                    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
                    "huggingface": "https://router.huggingface.co/v1/chat/completions",
                    "cerebras": "https://api.cerebras.ai/v1/chat/completions",
                    "sambanova": "https://api.sambanova.ai/v1/chat/completions",
                    "fireworks": "https://api.fireworks.ai/inference/v1/chat/completions",
                    "nvidia": "https://integrate.api.nvidia.com/v1/chat/completions",
                    "github": "https://models.inference.ai.azure.com/chat/completions",
                    "cohere": "https://api.cohere.com/compatibility/v1/chat/completions",
                    "perplexity": "https://api.perplexity.ai/chat/completions",
                    "pollinations": "https://text.pollinations.ai/openai",
                    "lmstudio": f"{lm_base}/chat/completions",
                    "xai": "https://api.x.ai/v1/chat/completions",
                    "deepseek": "https://api.deepseek.com/chat/completions",
                    "groq": "https://api.groq.com/openai/v1/chat/completions",
                    "mistral": "https://api.mistral.ai/v1/chat/completions",
                    "together": "https://api.together.xyz/v1/chat/completions",
                }
                url = endpoints.get(prov)
                if not url and prov in {"google_vertex", "aws_bedrock", "runpod", "vastai", "lambda_labs", "modal", "coreweave", "custom_gpu"}:
                    cfg = self.vault.get_provider_config(prov) if hasattr(self.vault, "get_provider_config") else {}
                    custom_base = str(cfg.get("endpoint_url") or "").strip().rstrip("/")
                    if not custom_base:
                        if prov == "google_vertex":
                            region = cfg.get("region") or "us-central1"
                            project_id = cfg.get("project_id") or "default"
                            custom_base = f"https://{region}-aiplatform.googleapis.com/v1beta1/projects/{project_id}/locations/{region}/endpoints/openapi"
                        elif prov == "aws_bedrock":
                            region = cfg.get("region") or "us-east-1"
                            custom_base = f"https://bedrock-runtime.{region}.amazonaws.com/openai/v1"
                        elif prov == "runpod" and cfg.get("endpoint_id"):
                            custom_base = f"https://api.runpod.ai/v2/{cfg['endpoint_id']}/openai/v1"
                        elif prov == "lambda_labs":
                            custom_base = "https://api.lambdalabs.com/v1"
                        elif prov == "custom_gpu":
                            custom_base = "http://127.0.0.1:8000/v1"
                    if custom_base:
                        url = custom_base if custom_base.endswith("/chat/completions") else f"{custom_base}/chat/completions"
                if not url:
                    return {"ok": False, "error": f"Unsupported provider {prov} (configure an OpenAI-compatible Endpoint URL in Models & API Keys).", "provider": prov, "model": model_id}
                req_headers: dict[str, str] = {}
                if api_key and not (prov in {"pollinations", "lmstudio", "custom_gpu"} and api_key.lower() in {"local", "open", "public", "true", "1", "enabled"}):
                    req_headers["Authorization"] = f"Bearer {api_key}"
                if prov == "modal" and hasattr(self.vault, "get_provider_config"):
                    m_cfg = self.vault.get_provider_config("modal")
                    if m_cfg.get("token_id"):
                        req_headers["Modal-Key"] = m_cfg["token_id"]
                        if api_key:
                            req_headers["Modal-Secret"] = api_key
                data = _http_json(
                    url,
                    method="POST",
                    payload={
                        "model": bare_model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        **({"max_completion_tokens": max_reply_tokens}
                           if prov == "openai" and max_reply_tokens is not None else {}),
                    },
                    headers=req_headers,
                    timeout=30.0 if prov in {
                        "huggingface", "cerebras", "sambanova", "fireworks", "nvidia", "github",
                        "cohere", "perplexity", "pollinations", "lmstudio",
                        "google_vertex", "aws_bedrock", "runpod", "vastai", "lambda_labs", "modal", "coreweave", "custom_gpu",
                    } else 5.0,
                )
                choices = data.get("choices") or []
                reply = str(((choices[0].get("message") or {}).get("content") or "") if choices else "").strip()
                finish_reason = str((choices[0].get("finish_reason") if choices else "") or "")

            if not reply:
                disp_m = model_id if str(model_id).lower().startswith(f"{prov}/") else f"{prov}/{model_id}"
                return {"ok": False, "error": f"empty response from {disp_m}", "provider": prov, "model": model_id}
            usage_obj = provider_usage or (data.get("usage") if "data" in locals() and isinstance(data, dict) else None) or {}
            c_tok = int(
                usage_obj.get("total_tokens")
                or (int(usage_obj.get("input_tokens") or 0) + int(usage_obj.get("output_tokens") or 0))
                or 0
            )
            if c_tok <= 0:
                c_tok = max(1, (len(system_prompt) + len(user_prompt) + len(reply)) // 4)
            return {
                "ok": True,
                "text": reply,
                "model": model_id,
                "provider": prov,
                "tokens_used": c_tok,
                # True when the provider stopped at the output-token limit, so the reply is incomplete.
                "finish_reason": finish_reason,
                "truncated": finish_reason in {"max_tokens", "length"},
                "token_usage": {
                    "input": usage_obj.get("total_input_tokens", usage_obj.get("prompt_tokens", usage_obj.get("input_tokens"))),
                    "output": usage_obj.get("total_output_tokens", usage_obj.get("completion_tokens", usage_obj.get("output_tokens"))),
                    "thinking": usage_obj.get("total_thought_tokens"),
                },
                "latency_ms": round((time.monotonic() - t0) * 1000.0, 1),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "provider": prov, "model": model_id}

    def _call_swarm_recovery(self, policy: RecoveryPolicy, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        return self._invoke_provider_text(
            "ollama", policy.model, system_prompt, user_prompt,
            local_timeout=policy.timeout_seconds, local_reply_tokens=policy.max_reply_tokens,
        )

    def _ready_stage_model(self, role: str, model_id: str) -> str:
        return self._call_swarm_llm(role, model_id, "", "", readiness_only=True)["model"]

    def _call_private_swarm_llm(
        self, role: str, model_id: str, system_prompt: str, user_prompt: str,
        *, attachments: list[Path] | None = None,
    ) -> dict[str, Any]:
        return self._call_swarm_llm(
            role, model_id, system_prompt, user_prompt, attachments=attachments, local_only=True,
        )

    def review_interface_brief(self, operation: str, local_context: dict[str, Any], model_id: str = "") -> dict[str, Any]:
        model_id = model_id or "gemini-3.5-flash-lite"
        if model_id not in {"gemini-3.5-flash-lite", "groq/openai/gpt-oss-120b", "ollama/gemma3:4b"}:
            return {"ok": False, "error": "Select an approved interface model.", "execution_authorized": False}
        try:
            fields, safe_prompt = interface_privacy.build_brief(operation, local_context)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}
        return self._call_swarm_llm(
            "coordinator", model_id, interface_privacy.SYSTEM, safe_prompt, interface_fields=fields,
        )

    def _call_swarm_llm(
        self,
        role: str,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        *,
        readiness_only: bool = False,
        attachments: list[Path] | None = None,
        local_only: bool = False,
        interface_fields: dict[str, Any] | None = None,
        max_reply_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Run one swarm LLM call and append its token usage to the usage ledger."""
        result = self._call_swarm_llm_inner(
            role,
            model_id,
            system_prompt,
            user_prompt,
            readiness_only=readiness_only,
            attachments=attachments,
            local_only=local_only,
            interface_fields=interface_fields,
            max_reply_tokens=max_reply_tokens,
        )
        if not readiness_only:
            usage_ledger.record(role, result)
        return result

    def _call_swarm_llm_inner(
        self,
        role: str,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        *,
        readiness_only: bool = False,
        attachments: list[Path] | None = None,
        local_only: bool = False,
        interface_fields: dict[str, Any] | None = None,
        max_reply_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Resolve an unlocked model for `role` and execute a real LLM inference call with fallback."""
        unlocked = set(self._unlocked_providers())
        if local_only:
            unlocked &= {"ollama"}
        if interface_fields is not None:
            unlocked &= {"ollama", infer_model_provider(model_id)}
        with self._remote_lock:
            cached_ollama_models = list(self._remote_cache.get("ollama_models") or [])
        candidates: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        skipped: list[str] = []
        skip_reasons: dict[str, str] = {}

        def note_skip(model: str, reason: str) -> None:
            if model not in skip_reasons:
                skip_reasons[model] = reason
                skipped.append(f"{model}: {reason}")

        def blocked_reason(prov: str, model: str) -> str:
            key = f"{prov}/{_bare_key(prov, model)}"
            with self._provider_cooldown_lock:
                if self._provider_cooldowns.get(prov) == float("inf"):
                    return "account credits exhausted"
                if float(self._provider_cooldowns.get(prov) or 0.0) > time.monotonic():
                    return "provider rate-limit cooldown active"
                if key in self._zero_quota_models:
                    return "model quota is zero"
                if self._service_cooldowns.get((prov, _bare_key(prov, model)), 0) > time.monotonic():
                    return "temporary cooldown after HTTP 503"
                if is_model_cooled_down(key, cooldowns=self._provider_cooldowns,
                                        unsupported_models=self._unsupported_models):
                    return "model or provider on cooldown or unsupported"
            return ""

        non_chat_tokens = (
            "embed", "tts", "imagen", "nano-banana", "flash-lite-image", "flash-image", "pro-image",
            "gpt-image", "chatgpt-image", "gpt-audio", "gpt-live", "computer-use", "robotics",
            "whisper", "veo", "aqa", "moderation", "omni-moderation", "realtime",
            "transcribe", "lyria", "genie", "decision",
        )

        def _push(cand_model: str) -> None:
            cm = str(cand_model or "").strip()
            if not cm:
                return
            if any(tok in cm.lower() for tok in non_chat_tokens):
                return
            while cm.lower().startswith("ollama/ollama/"):
                cm = cm[len("ollama/") :]
            prov = infer_model_provider(cm)
            if prov == "unknown" and "/" in cm:
                prov = cm.split("/", 1)[0].lower()
            if prov not in unlocked:
                return
            if prov == "ollama" and not cached_ollama_models:
                return
            pair = (prov, cm)
            if pair not in seen:
                seen.add(pair)
                candidates.append(pair)

        def _bare_key(prov: str, cm: str) -> str:
            b = cm.strip().lower()
            for prefix in (f"{prov}/", "models/", "gemini/", "local:", "ollama/"):
                if b.startswith(prefix):
                    b = b[len(prefix) :]
            return b

        # Collect live-probed chat models per unlocked provider so verified GA models come first
        probed_by_prov: dict[str, list[str]] = {}
        probed_bare_by_prov: dict[str, set[str]] = {}
        try:
            pms = getattr(self.vault, "_probed_models", None) or (self.vault.get_probed_models() if hasattr(self.vault, "get_probed_models") else [])
            for pm in pms:
                pid = str(pm.get("id") or "").strip()
                pprov = str(pm.get("provider") or "").strip().lower()
                if pprov in unlocked and pid and not any(tok in pid.lower() for tok in non_chat_tokens):
                    probed_by_prov.setdefault(pprov, []).append(pid)
                    probed_bare_by_prov.setdefault(pprov, set()).add(_bare_key(pprov, pid))
        except Exception:
            pass

        deferred_bindings: list[str] = []
        if model_id:
            cm = str(model_id).strip()
            p = infer_model_provider(cm)
            if p == "unknown" and "/" in cm:
                p = cm.split("/", 1)[0].lower()
            if p in unlocked:
                if p in probed_bare_by_prov and _bare_key(p, cm) not in probed_bare_by_prov[p]:
                    deferred_bindings.append(cm)
                    note_skip(cm, "absent from the probed catalog, deferred")
                else:
                    _push(cm)

        binding_models: list[str] = []
        try:
            binding = self.role_bindings.get_binding(role)
            binding_models.extend(binding.candidate_chain())
        except Exception:
            pass

        for m in binding_models:
            cm = str(m or "").strip()
            if not cm:
                continue
            p = infer_model_provider(cm)
            if p == "unknown" and "/" in cm:
                p = cm.split("/", 1)[0].lower()
            if p not in unlocked:
                note_skip(cm, "provider unavailable")
                continue
            # If we already have live-probed models for this provider and this binding isn't among them,
            # try verified probed/GA models first so we don't burn retries on unprobed codenames.
            if p in probed_bare_by_prov and _bare_key(p, cm) not in probed_bare_by_prov[p]:
                deferred_bindings.append(cm)
                if cm != model_id:
                    note_skip(cm, "absent from the probed catalog, deferred")
            else:
                _push(cm)

        # Prioritize live-probed models (favoring fast GA Gemini models first when present)
        for pprov, pids in probed_by_prov.items():
            ranked = sorted(
                pids,
                key=lambda x: (
                    0 if "gemini-3.5-flash-lite" in x.lower() else (1 if any(g in x.lower() for g in ("gemini-4", "gemini-3.5", "gpt-4.1", "gpt-5", "claude-sonnet")) else 2),
                    1 if any(tag in x.lower() for tag in ("exp", "preview", "thinking")) else 0,
                ),
            )
            for pid in ranked[:8]:
                _push(pid)

        # Include safe GA defaults for any unlocked cloud provider
        provider_defaults: dict[str, list[str]] = {
            "gemini": ["gemini-4-neon", "gemini-neo", "gemini-3.5-flash-lite", "gemini-4-hydrogen", "gemini-4-argon", "gemini-4-carbon"],
            "openai": ["openai/gpt-4.1-mini"],
            "anthropic": ["anthropic/claude-sonnet-4"],
            "openrouter": ["openrouter/google/gemini-3.5-flash-lite", "openrouter/qwen/qwen3-32b:free"],
            "huggingface": ["huggingface/Qwen/Qwen2.5-72B-Instruct", "huggingface/deepseek-ai/DeepSeek-R1-Distill-Qwen-32B"],
            "cerebras": ["cerebras/llama-3.3-70b", "cerebras/qwen-3-32b"],
            "sambanova": ["sambanova/Meta-Llama-3.3-70B-Instruct", "sambanova/DeepSeek-R1"],
            "fireworks": ["fireworks/accounts/fireworks/models/llama-v3p3-70b-instruct"],
            "nvidia": ["nvidia/meta/llama-3.3-70b-instruct"],
            "github": ["github/gpt-4.1-mini", "github/DeepSeek-R1"],
            "cohere": ["cohere/command-a-03-2025"],
            "perplexity": ["perplexity/sonar"],
            "pollinations": ["pollinations/openai-fast"],
            "lmstudio": ["lmstudio/local-model"],
            "xai": ["xai/grok-3-mini"],
            "deepseek": ["deepseek/deepseek-chat"],
            "groq": ["groq/llama-3.3-70b-versatile"],
            "mistral": ["mistral/mistral-small-latest"],
            "together": ["together/meta-llama/Llama-3.3-70B-Instruct-Turbo"],
        }
        for prov, def_models in provider_defaults.items():
            if prov in unlocked:
                for dm in def_models:
                    _push(dm)

        # Keep deferred unprobed binding models as lower-priority fallbacks
        for dm in deferred_bindings:
            _push(dm)

        # Include cached live Ollama models (if Ollama is actually reachable)
        for om in cached_ollama_models:
            oname = str(om.get("name") or "").strip()
            while oname.lower().startswith("ollama/"):
                oname = oname[len("ollama/") :]
            if oname:
                _push(f"ollama/{oname}")

        if interface_fields is not None:
            local_candidates = [(provider, model) for provider, model in candidates if provider == "ollama"]
            local_candidates.sort(key=lambda candidate: candidate[1] != "ollama/gemma3:4b")
            selected_provider = infer_model_provider(model_id)
            if selected_provider == "ollama":
                candidates = local_candidates
            elif selected_provider in unlocked:
                candidates = [(selected_provider, model_id), *local_candidates]
            else:
                candidates = local_candidates

        if readiness_only:
            return {"model": next((m for p, m in candidates if not blocked_reason(p, m)), model_id)}

        runtime_errors: list[str] = []
        errors: list[str] = list(skipped)
        configured = binding_models[0] if binding_models else model_id
        auth_failed_providers: set[str] = set()
        unreachable_providers: set[str] = set()
        rate_limited_models: set[tuple[str, str]] = set()
        broken_models: set[tuple[str, str]] = set()
        requested = configured if configured in skip_reasons and configured != model_id else model_id
        for provider, candidate in candidates:
            if candidate == configured and candidate != model_id:
                reason = blocked_reason(provider, candidate)
                if reason:
                    requested = configured
                    errors.insert(0, f"{candidate}: {reason}")
                    break
        gemini_probed = False
        idx = 0
        attempts = 0
        while idx < len(candidates) and attempts < 6:
            prov, cand_model = candidates[idx]
            idx += 1
            mkey = (prov, _bare_key(prov, cand_model))
            reason = blocked_reason(prov, cand_model)
            if reason:
                message = f"{cand_model}: {reason}"
                if message not in errors:
                    errors.append(message)
                continue
            if (
                prov in auth_failed_providers
                or prov in unreachable_providers
                or mkey in rate_limited_models
                or (prov, "*") in rate_limited_models
                or mkey in broken_models
            ):
                continue
            api_key = None
            if prov not in {"ollama", "portal", "pollinations", "lmstudio"}:
                try:
                    api_key = self.vault.get_key(prov, include_env=True)
                except Exception:
                    api_key = None
                if not api_key:
                    continue
            elif prov in {"pollinations", "lmstudio"}:
                try:
                    api_key = self.vault.get_key(prov, include_env=True)
                except Exception:
                    api_key = None
            attempts += 1
            extra_inv_kw: dict[str, Any] = {"attachments": attachments} if attachments else {}
            if max_reply_tokens:
                extra_inv_kw["max_reply_tokens"] = int(max_reply_tokens)
            if interface_fields is not None and prov != "ollama":
                with self._remote_lock:
                    self._interface_dispatches.insert(0, {
                        "provider": prov, "model": cand_model, "fields": dict(interface_fields),
                        "characters": len(system_prompt) + len(user_prompt),
                    })
                    del self._interface_dispatches[30:]
            res = self._invoke_provider_text(
                prov,
                cand_model,
                system_prompt,
                user_prompt,
                api_key=api_key,
                **extra_inv_kw,
            )
            if res.get("ok"):
                combined_errs = runtime_errors + [e for e in errors if e not in runtime_errors]
                requested_provider = infer_model_provider(requested) if requested else ""
                is_fallback = (
                    (requested_provider != prov or _bare_key(prov, cand_model) != _bare_key(prov, requested))
                    if requested else bool(runtime_errors)
                )
                if is_fallback:
                    res["used_fallback"] = True
                    res["primary_error"] = combined_errs[0] if combined_errs else f"Requested model '{model_id}' was skipped (cooldown active or unavailable)"
                    res["fallback_errors"] = list(combined_errs) if combined_errs else [res["primary_error"]]
                    res["requested_model"] = requested
                    res["attempts"] = attempts
                    # Auto-recover role binding when configured primary model is permanently blocked
                    # (zero credits, zero tier quota, unsupported/404, or locked provider) so subsequent
                    # stages/missions stop repeating LLM_WARN forever.
                    if configured and _bare_key(prov, cand_model) != _bare_key(infer_model_provider(configured), configured):
                        cfg_prov = infer_model_provider(configured)
                        if cfg_prov == "unknown" and "/" in configured:
                            cfg_prov = configured.split("/", 1)[0].lower()
                        cfg_reason = (blocked_reason(cfg_prov, configured) + " " + " ".join(combined_errs)).lower()
                        is_permanent_cfg_block = (
                            cfg_prov not in unlocked
                            or any(
                                ptok in cfg_reason
                                for ptok in (
                                    "account credits exhausted",
                                    "no credits remaining",
                                    "insufficient_quota",
                                    "exceeded your current quota",
                                    "model quota is zero",
                                    "limit: 0",
                                    "0 input tokens per minute",
                                    "not found",
                                    "unsupported",
                                    "provider unavailable",
                                )
                            )
                        )
                        had_transient_cloud_rl = (
                            prov == "ollama"
                            and any(
                                ("rate-limit" in e.lower() or "rate limit" in e.lower() or "resource_exhausted" in e.lower())
                                and "limit: 0" not in e.lower()
                                and "no credits" not in e.lower()
                                and "exceeded your current quota" not in e.lower()
                                for e in combined_errs
                            )
                        )
                        if is_permanent_cfg_block and not had_transient_cloud_rl and not local_only and interface_fields is None and not self._isolated_test_mode:
                            try:
                                from swarm_command.role_binding import normalize_role_name

                                canon_r = normalize_role_name(role)
                                cur_b = self.role_bindings.get_binding(canon_r)
                                if cur_b.primary_model == configured and cur_b.primary_model != cand_model:
                                    reordered_fb = [m for m in cur_b.fallback_models if m != cand_model] + [configured]
                                    self.role_bindings.set_binding(canon_r, cand_model, fallback_models=reordered_fb)
                                    res["role_binding_healed"] = {"role": canon_r, "from": configured, "to": cand_model}
                                    if hasattr(self, "swarm_runner") and self.swarm_runner is not None:
                                        with self.swarm_runner._lock:
                                            self.swarm_runner._record_feed_locked(
                                                badge="SELF_HEALED",
                                                actor=f"Role Router ({canon_r})",
                                                summary=f"Promoted healthy model '{cand_model}' for {canon_r} (demoted blocked '{configured}') — future turns cleared of LLM_WARN.",
                                            )
                            except Exception:
                                pass
                return res
            err_text = str(res.get("error") or "")
            disp_cand = cand_model if str(cand_model).lower().startswith(f"{prov}/") else f"{prov}/{cand_model}"
            err_entry = f"{disp_cand}: {err_text}"
            runtime_errors.append(err_entry)
            errors.append(err_entry)
            err_low = err_text.lower()
            is_zero_quota = bool(re.search(r"\blimit:\s*0\b(?![.\d])", err_low)) or any(
                tok in err_low
                for tok in (
                    "0 input tokens per minute",
                    "0 tokens per minute",
                    "limit: 0 tokens",
                    "requests per day limit exceeded",
                )
            )
            is_account_quota_err = any(
                tok in err_low
                for tok in (
                    "no credits remaining",
                    "insufficient_quota",
                    "add credits",
                    "credit balance is too low",
                    "billing_hard_limit",
                    "billing_not_active",
                )
            ) or (
                prov != "gemini"
                and any(
                    tok in err_low
                    for tok in (
                        "exceeded your current quota",
                        "check your plan and billing",
                    )
                )
            )
            is_rate_limit_err = any(
                tok in err_low
                for tok in (
                    "429",
                    "too many requests",
                    "rate limit",
                    "rate_limit",
                    "resource_exhausted",
                    "quota exceeded",
                    "quota_exceeded",
                )
            )
            if is_zero_quota:
                with self._provider_cooldown_lock:
                    self._zero_quota_models.add(f"{prov}/{mkey[1]}")
                    if "free tier" in err_low and prov == "gemini" and mkey[1].startswith("gemini-4-"):
                        for g_code in ("gemini-4-carbon", "gemini-4-argon", "gemini-4-hydrogen", "gemini-4-neon"):
                            self._zero_quota_models.add(f"gemini/{g_code}")
                continue
            if is_account_quota_err:
                with self._provider_cooldown_lock:
                    self._provider_cooldowns[prov] = float("inf")
                continue
            if re.search(r"\b503\b", err_low):
                with self._provider_cooldown_lock:
                    until = time.monotonic() + 60.0
                    self._provider_cooldowns[mkey] = until
                    self._service_cooldowns[mkey] = until
                continue
            if is_rate_limit_err:
                rate_limited_models.add(mkey)
                model_scope = bool(re.search(
                    rf"\b(?:for|on)\s+(?:the\s+)?model\s*[:=]?\s*['\"`]?"
                    rf"(?:models/|{re.escape(prov)}/)?{re.escape(mkey[1])}(?![a-z0-9_.-])",
                    err_low,
                ))
                if not model_scope:
                    rate_limited_models.add((prov, "*"))
                retry_after = 45.0
                retry_match = re.search(r"\b(?:retry|try again)\s+(?:in|after)\s+((?:\d+(?:\.\d+)?\s*[hms]\s*)+)", err_low)
                if retry_match:
                    units = {"h": 3600.0, "m": 60.0, "s": 1.0}
                    seconds = sum(float(value) * units[unit] for value, unit in re.findall(
                        r"(\d+(?:\.\d+)?)\s*([hms])", retry_match.group(1)
                    ))
                    retry_after = max(1.0, min(86400.0, seconds))
                with self._provider_cooldown_lock:
                    until_rl = time.monotonic() + retry_after
                    self._provider_cooldowns[mkey] = until_rl
                    if not model_scope:
                        self._provider_cooldowns[prov] = until_rl
                continue
            if prov == "ollama" and ("http 500" in err_low or "error loading model" in err_low or "llama-server process has terminated" in err_low):
                # Crashing local Ollama model -> skip for the rest of the session
                broken_models.add(mkey)
                with self._provider_cooldown_lock:
                    self._provider_cooldowns[mkey] = time.monotonic() + 3600.0
                continue
            is_not_found = any(
                tok in err_low
                for tok in (
                    "http 404",
                    "not found",
                    "is not found",
                    "model not found",
                    "is not supported",
                    "unsupported model",
                )
            )
            if is_not_found:
                broken_models.add(mkey)
                with self._provider_cooldown_lock:
                    self._unsupported_models.add(f"{prov}/{mkey[1]}")
                    self._provider_cooldowns[mkey] = time.monotonic() + 86400.0
                continue
            is_auth_err = any(
                tok in err_low
                for tok in (
                    "api key not valid",
                    "api_key_invalid",
                    "invalid_api_key",
                    "invalid api key",
                    "unauthorized",
                    "http 401",
                    "http 403",
                    "permission_denied",
                )
            )
            if is_auth_err:
                auth_failed_providers.add(prov)
                continue
            is_net_err = any(
                tok in err_low
                for tok in (
                    "getaddrinfo failed",
                    "name or service not known",
                    "network is unreachable",
                    "connection refused",
                    "timed out",
                    "timeout",
                    "connection reset",
                    "failed to establish a new connection",
                )
            )
            if is_net_err:
                unreachable_providers.add(prov)
                continue
            if (
                not gemini_probed
                and prov == "gemini"
                and "gemini" in unlocked
                and ("HTTP 400" in err_text or "HTTP 404" in err_text or "400" in err_text or "404" in err_text)
                and hasattr(self.vault, "get_probed_models")
                and hasattr(self.vault, "probe_provider")
            ):
                existing_gemini = [
                    m for m in self.vault.get_probed_models()
                    if str(m.get("provider") or "").lower() == "gemini"
                ]
                if not existing_gemini:
                    gemini_probed = True
                    try:
                        self.vault.probe_provider("gemini")
                        for pm in self.vault.get_probed_models()[:40]:
                            pid = str(pm.get("id") or "").strip()
                            pprov = str(pm.get("provider") or "").strip().lower()
                            if (
                                pprov == "gemini"
                                and pid
                                and not any(tok in pid.lower() for tok in ("embed", "tts", "imagen", "whisper", "veo"))
                            ):
                                _push(pid)
                    except Exception:
                        pass

        if not attempts:
            no_key_prefix = (
                "No unlocked provider API key or local Ollama model available. "
                if (unlocked <= {"ollama"} and not cached_ollama_models)
                else ""
            )
            return {
                "ok": False,
                "error": no_key_prefix + "Blocked: no eligible model route." + (" " + "; ".join(errors[:4]) if errors else ""),
                "provider": "none",
                "model": model_id,
                "primary_error": errors[0] if errors else "",
                "all_errors": errors,
                "fallback_errors": [],
                "requested_model": model_id,
                "used_fallback": False,
            }
        return {
            "ok": False,
            "error": "; ".join(errors[:2]),
            "provider": "none",
            "model": model_id,
            "primary_error": errors[0] if errors else "",
            "all_errors": list(errors),
            "fallback_errors": list(errors),
            "requested_model": model_id,
            "used_fallback": len(errors) > 1,
        }

    def clear_chat(self) -> dict[str, Any]:
        with self._chat_lock:
            self.chat_log.clear()
            self._chat_log_revision += 1
        self._save_operator_chat_log()
        try:
            from tools.swarm_chat_portal import clear_chat_history
            clear_chat_history()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def gemini_status(self) -> dict[str, Any]:
        return {"ok": True, **get_copilot_status()}

    def open_gemini_key_page(self) -> dict[str, Any]:
        try:
            import webbrowser
            webbrowser.open("https://aistudio.google.com/apikey")
            return {"ok": True, "url": "https://aistudio.google.com/apikey"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_gemini_api_key(self, api_key: str) -> dict[str, Any]:
        cleaned = (api_key or "").strip()
        if not cleaned:
            return {"ok": False, "error": "API key cannot be empty."}
        os.environ["GEMINI_API_KEY"] = cleaned
        try:
            import subprocess
            subprocess.run(
                ["setx", "GEMINI_API_KEY", cleaned],
                check=False,
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass
        return {"ok": True, **get_copilot_status()}

    def gemini_analyze(
        self,
        question: str = "",
        model: str = DEFAULT_GEMINI_MODEL,
        previous_interaction_id: str | None = None,
        thinking_level: str = "medium",
    ) -> dict[str, Any]:
        try:
            snap = self.get_snapshot()
            swarm = snap.get("swarm") or {}
            state_view = {
                "plan": {
                    "title": "Sabrina Operator Console",
                    "goal": (swarm.get("objectives") or [{}])[0].get("goal", "Collective Swarm"),
                    "phase": swarm.get("verifier_verdict", "active"),
                    "threads": swarm.get("long_horizon_projects", []),
                },
                "safety": {"execution_authorized": bool((snap.get("kraken") or {}).get("execution_authorized", False))},
                "roster": {"workers": {w.get("name") or w.get("id") or f"w-{i}": w for i, w in enumerate(swarm.get("workers") or [])}},
                "lock_matrix": {"conflicts": []},
                "board": swarm.get("activity_feed", [])[:20],
            }
            result = analyze_swarm_state(
                state_view,
                question=question or None,
                model=model or DEFAULT_GEMINI_MODEL,
                thinking_level=thinking_level or "medium",
                previous_interaction_id=previous_interaction_id or None,
            )
            return {"ok": True, **result}
        except GeminiCopilotError as exc:
            return {"ok": False, "error": exc.code, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": "gemini_bridge_error", "message": str(exc)}

    def get_vault_status(self) -> dict[str, Any]:
        try:
            status = self.vault.list_providers()
            if isinstance(status, dict):
                return {"ok": True, **status}
            return {"ok": True, "providers": [], "unlocked_providers": sorted(self._unlocked_providers())}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "providers": [], "unlocked_providers": []}

    def set_vault_key(
        self,
        provider: str,
        api_key: str,
        probe: bool = True,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            res = self.vault.set_key(provider, api_key, config=config)
            probe_res: dict[str, Any] = {}
            if probe:
                raw_probe = self.vault.probe_provider(provider)
                probe_res = raw_probe if isinstance(raw_probe, dict) else {"result": raw_probe}
                if probe_res.get("ok"):
                    self._clear_quota_blocks(provider)
            self.registry.refresh_models(force=True)
            vault_status = self.get_vault_status()
            unlocked_count = int(probe_res.get("unlocked_count", 0)) if probe_res else 0
            return {
                "ok": True,
                **(res if isinstance(res, dict) else {"result": res}),
                **vault_status,
                "probe": probe_res,
                "unlocked_count": unlocked_count,
                "vault": vault_status,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "provider": provider}

    def set_vault_provider_config(self, provider: str, config: dict[str, Any]) -> dict[str, Any]:
        try:
            res = self.vault.set_provider_config(provider, config)
            vault_status = self.get_vault_status()
            return {"ok": True, "provider_status": res, **vault_status, "vault": vault_status}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "provider": provider}

    def get_gpu_providers(self) -> dict[str, Any]:
        try:
            return {
                "ok": True,
                "providers": self.gpu_deployments.list_providers(),
                "deployments": self.gpu_deployments.list_deployments(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "providers": [], "deployments": []}

    def list_gpu_deployments(self) -> dict[str, Any]:
        try:
            return {"ok": True, "deployments": self.gpu_deployments.list_deployments()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "deployments": []}

    def deploy_swarm_to_gpu(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            res = self.gpu_deployments.deploy_swarm(payload)
            if res.get("ok"):
                self.registry.refresh_models(force=True)
            return {**res, "deployments": self.gpu_deployments.list_deployments()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def check_gpu_deployment_status(self, deployment_id: str) -> dict[str, Any]:
        try:
            res = self.gpu_deployments.check_status(deployment_id)
            return {**res, "deployments": self.gpu_deployments.list_deployments()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def terminate_gpu_deployment(self, deployment_id: str) -> dict[str, Any]:
        try:
            res = self.gpu_deployments.terminate_deployment(deployment_id)
            return {**res, "deployments": self.gpu_deployments.list_deployments()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def register_gpu_deployment_model(self, deployment_id: str) -> dict[str, Any]:
        try:
            res = self.gpu_deployments.register_deployment_model(deployment_id)
            if res.get("ok"):
                self.registry.refresh_models(force=True)
            return {**res, "deployments": self.gpu_deployments.list_deployments()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_vault_key(self, provider: str) -> dict[str, Any]:
        try:
            res = self.vault.delete_key(provider)
            self.registry.refresh_models(force=True)
            vault_status = self.get_vault_status()
            return {
                "ok": True,
                **(res if isinstance(res, dict) else {"result": res}),
                **vault_status,
                "vault": vault_status,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "provider": provider}

    def _clear_quota_blocks(self, provider: str) -> None:
        provider = provider.strip().lower()
        with self._provider_cooldown_lock:
            if self._provider_cooldowns.get(provider) == float("inf"):
                self._provider_cooldowns.pop(provider)
            self._zero_quota_models = {
                model for model in self._zero_quota_models if not model.startswith(f"{provider}/")
            }

    def probe_vault_provider(self, provider: str, api_key: str | None = None) -> dict[str, Any]:
        try:
            res = self.vault.probe_provider(provider, api_key=api_key or None)
            if isinstance(res, dict) and res.get("ok"):
                if not api_key:
                    self._clear_quota_blocks(provider)
                self.registry.refresh_models(force=True)
            return {
                "ok": bool(res.get("ok", True)) if isinstance(res, dict) else True,
                **(res if isinstance(res, dict) else {"result": res}),
                "vault": self.get_vault_status(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "provider": provider}

    def probe_all_vault_providers(self) -> dict[str, Any]:
        try:
            res = self.vault.probe_all_unlocked()
            for provider, status in res.get("results", {}).items():
                if status.get("ok"):
                    self._clear_quota_blocks(provider)
            self.registry.refresh_models(force=True)
            return {
                "ok": True,
                **(res if isinstance(res, dict) else {"result": res}),
                "vault": self.get_vault_status(),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _call_private_automation_model(
        self,
        prompt: str,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        sys_inst = ""
        if response_schema is not None:
            sys_inst = (
                "Respond ONLY with valid JSON (no markdown fences) matching this JSON schema:\n"
                + json.dumps(response_schema)
            )
        result = self._call_swarm_llm("coordinator", "", sys_inst, prompt)
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error") or "LLM route unavailable."))
        raw = str(result.get("text") or "")
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines).strip()
        return cleaned

    # --- Automations & Connectors Bridge ---

    def list_automations(self) -> dict[str, Any]:
        return {
            "ok": True,
            "loops": self.automation_engine.list_loops(),
            "pending_approvals": self.automation_engine.list_pending_approvals(),
        }

    def save_automation(self, spec: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.automation_engine.save_loop(spec)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _record_automation_recommendation(self, res: dict[str, Any], loop_name: str = "") -> None:
        if not isinstance(res, dict):
            return
        run_obj = res.get("run") if isinstance(res.get("run"), dict) else res
        rec_goal = str(run_obj.get("recommended_next_goal") or "").strip()
        if rec_goal:
            rid = str(run_obj.get("run_id") or run_obj.get("id") or "")
            self._latest_recommended_next = {
                "goal": rec_goal,
                "source": f"workflow:{rid or loop_name or 'a2a'}",
                "run_id": rid,
                "goal_history": list(run_obj.get("goal_history") or []),
                "ts": time.time(),
            }

    def recommend_next_mission_step(self, current_goal: str = "") -> dict[str, Any]:
        """Generate a concrete Finale Recommended Next Step prompt from the latest swarm/workflow context."""
        from sabrina_console_desktop.automation_engine import extract_recommended_next_goal

        state = self.swarm_runner.get_state()
        outputs = list(state.get("mission_outputs") or [])
        latest = outputs[0] if outputs else {}
        seed_goal = str(current_goal or latest.get("goal") or "").strip()
        report_excerpt = str(latest.get("report_markdown") or latest.get("summary") or "")[:3600]
        sys_p = (
            "You are Sabrina's Finale Next-Step Strategist. Review the current mission goal and latest swarm output, "
            "and propose a single concrete, high-leverage follow-up mission prompt ready to paste directly into "
            "'Enter a mission goal for the swarm...'. Reply ending with: RECOMMENDED_NEXT_GOAL: <single-line prompt>"
        )
        usr_p = f"Current Goal: {seed_goal}\n\nLatest Swarm Output:\n{report_excerpt}"
        llm_res = self._call_swarm_llm("coordinator", "", sys_p, usr_p)
        rec_goal = extract_recommended_next_goal(
            str(llm_res.get("text") or "") if isinstance(llm_res, dict) else "",
            seed_goal,
        )
        self._latest_recommended_next = {
            "goal": rec_goal,
            "source": "strategist:manual",
            "ts": time.time(),
        }
        return {
            "ok": True,
            "recommended_next_goal": rec_goal,
            "recommended_next_step": dict(self._latest_recommended_next),
            "llm_ok": bool(isinstance(llm_res, dict) and llm_res.get("ok")),
        }

    def _run_automation_swarm_step(self, goal: str, role: str) -> dict[str, Any]:
        """Launch a swarm mission for an automation step and await its settled deliverable."""
        mode = role if role in ("codex_thought_antigravity_implement", "windows_powershell_inspect") else "codex_thought_antigravity_implement"
        launch_res = self.launch_mission(goal=goal, mode=mode)
        if not isinstance(launch_res, dict) or not launch_res.get("ok"):
            return dict(launch_res) if isinstance(launch_res, dict) else {"ok": False, "error": "Swarm launch failed"}
        mid = str(launch_res.get("mission_id") or "").strip()
        if mid and hasattr(self.swarm_runner, "wait_for_mission"):
            settled = self.swarm_runner.wait_for_mission(mid, timeout=300.0)
            if isinstance(settled, dict):
                st = str(settled.get("state") or "")
                passed = st in ("PASS", "COMPLETED", "DONE")
                report_txt = str(settled.get("report_markdown") or settled.get("summary") or "").strip()
                prefix = "PASS: " if passed else ("NEEDS_WORK: " if st in ("FAILED", "NEEDS_MORE_WORK", "ERROR") else "")
                return {
                    "ok": st not in ("FAILED", "ERROR", "CANCELLED"),
                    "mission_id": mid,
                    "state": st,
                    "passed": passed,
                    "text": f"{prefix}{report_txt}" if (prefix and not report_txt.upper().startswith(("PASS:", "NEEDS_WORK:"))) else report_txt,
                }
        return launch_res

    def _execute_automation_workspace_tools(self, text: str, allow_writes: bool = False) -> tuple[bool, str, list[str]]:
        """Execute safe read/search tool blocks (and gated workspace writes) for Join-Cut workflow agents."""
        has_tools = False
        tool_out = ""
        written: list[str] = []
        if hasattr(self.swarm_runner, "_execute_ide_tool_blocks"):
            try:
                has_tools, tool_out = self.swarm_runner._execute_ide_tool_blocks(text, allow_tests=False)
            except Exception:
                has_tools, tool_out = False, ""
        if allow_writes and hasattr(self.swarm_runner, "_apply_workspace_writes"):
            try:
                written = list(self.swarm_runner._apply_workspace_writes(text) or [])
            except Exception:
                written = []
        return has_tools, tool_out, written

    def save_and_initiate_automation(self, spec: dict[str, Any]) -> dict[str, Any]:
        try:
            res = self.automation_engine.save_and_initiate_loop(spec)
            self._record_automation_recommendation(res, str((spec or {}).get("name") or ""))
            return res
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def initiate_automation_async(self, spec: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.automation_engine.initiate_loop_async(spec)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def stop_automation(self, target_id: str = "") -> dict[str, Any]:
        try:
            return self.automation_engine.cancel_run(target_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def validate_automation_topology(self, spec: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"ok": True, "topology": self.automation_engine.validate_workflow_topology(spec or {})}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _swarm_schema_agents(schema: dict[str, Any]) -> list[dict[str, Any]]:
        raw_agents = schema.get("agents") or schema.get("workers") or schema.get("nodes") or []
        if isinstance(raw_agents, dict):
            return [
                {"id": str(key), **(value if isinstance(value, dict) else {})}
                for key, value in raw_agents.items()
            ]
        return [item for item in raw_agents if isinstance(item, dict)] if isinstance(raw_agents, list) else []

    @staticmethod
    def _swarm_runtime_role_aliases() -> dict[str, str]:
        return {
            "sabrina": "coordinator",
            "codex": "planner_architect",
            "antigravity": "execution_specialist",
            "claude": "critic_verifier",
        }

    def _schema_a2a_links(self, schema: dict[str, Any], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate runtime worker aliases to the matching IDs in the schema graph."""
        agents = self._swarm_schema_agents(schema)
        aliases = self._swarm_runtime_role_aliases()

        def endpoint(value: Any) -> str:
            key = str(value or "").strip()
            if not key:
                return key
            direct = next((a for a in agents if key in {
                str(a.get("id") or ""), str(a.get("role") or ""), str(a.get("name") or "")
            }), None)
            role = aliases.get(key, key)
            match = direct or next((a for a in agents if str(a.get("role") or "") == role), None)
            return str(match.get("id") or match.get("role") or key) if match else key

        normalized: list[dict[str, Any]] = []
        for link in links:
            if not isinstance(link, dict):
                continue
            item = dict(link)
            item["from"] = endpoint(item.get("from"))
            item["to"] = endpoint(item.get("to"))
            normalized.append(item)
        return normalized

    def _runtime_a2a_links(self, schema: dict[str, Any], links: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Translate schema agent IDs/roles back to runner worker aliases where known."""
        agents = self._swarm_schema_agents(schema)
        aliases = self._swarm_runtime_role_aliases()
        role_to_alias = {role: alias for alias, role in aliases.items()}

        def endpoint(value: Any) -> str:
            key = str(value or "").strip()
            if not key:
                return key
            direct = next((a for a in agents if key in {
                str(a.get("id") or ""), str(a.get("role") or ""), str(a.get("name") or "")
            }), None)
            role = aliases.get(key, key)
            match = direct or next((a for a in agents if str(a.get("role") or "") == role), None)
            if not match:
                return key
            agent_role = str(match.get("role") or "")
            return role_to_alias.get(agent_role) or str(match.get("id") or agent_role or key)

        normalized: list[dict[str, Any]] = []
        for link in links:
            if not isinstance(link, dict):
                continue
            item = dict(link)
            item["from"] = endpoint(item.get("from"))
            item["to"] = endpoint(item.get("to"))
            normalized.append(item)
        return normalized

    def _mirror_swarm_a2a_links_into_schema(self, links: list[dict[str, Any]]) -> None:
        """Keep the schema file's graph edges aligned with the live runner configuration."""
        try:
            if not self._swarm_schema_path.is_file():
                return
            schema = json.loads(self._swarm_schema_path.read_text(encoding="utf-8"))
            if not isinstance(schema, dict):
                return
            schema["a2a_links"] = self._schema_a2a_links(schema, links)
            self._swarm_schema_path.write_text(json.dumps(schema, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _persist_swarm_a2a_links(self) -> list[dict[str, Any]]:
        links = self.swarm_runner.get_a2a_links()
        try:
            self._swarm_a2a_links_path.parent.mkdir(parents=True, exist_ok=True)
            self._swarm_a2a_links_path.write_text(json.dumps(links, indent=2), encoding="utf-8")
        except Exception:
            pass
        self._mirror_swarm_a2a_links_into_schema(links)
        return links

    def get_swarm_a2a_links(self) -> dict[str, Any]:
        return {"ok": True, "a2a_links": self.swarm_runner.get_a2a_links()}

    def upsert_swarm_a2a_link(self, spec: dict[str, Any]) -> dict[str, Any]:
        try:
            saved = self.swarm_runner.upsert_a2a_link(spec)
            all_links = self._persist_swarm_a2a_links()
            return {"ok": True, "link": saved, "a2a_links": all_links}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_swarm_a2a_link(self, link_id: str) -> dict[str, Any]:
        try:
            deleted = self.swarm_runner.delete_a2a_link(link_id)
            all_links = self._persist_swarm_a2a_links()
            return {"ok": True, "deleted": deleted, "a2a_links": all_links}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def clear_swarm_a2a_links(self) -> dict[str, Any]:
        try:
            self.swarm_runner.set_a2a_links([])
            self._persist_swarm_a2a_links()
            return {"ok": True, "a2a_links": []}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_automation(self, loop_id: str) -> dict[str, Any]:
        try:
            return self.automation_engine.delete_loop(loop_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def toggle_automation(self, loop_id: str, enabled: bool) -> dict[str, Any]:
        try:
            return self.automation_engine.toggle_loop(loop_id, bool(enabled))
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def run_automation_now(self, loop_id: str) -> dict[str, Any]:
        try:
            res = self.automation_engine.run_loop_now(loop_id)
            self._record_automation_recommendation(res, loop_id)
            return res
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_automation_runs(self, loop_id: str | None = None, limit: int = 25) -> dict[str, Any]:
        try:
            return {"ok": True, "runs": self.automation_engine.get_runs(loop_id=loop_id, limit=limit)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "runs": []}

    def decide_automation_approval(
        self,
        run_id: str,
        approve: bool,
        edited_params: dict[str, Any] | None = None,
        feedback: str = "",
    ) -> dict[str, Any]:
        try:
            res = self.automation_engine.decide_approval(
                run_id,
                bool(approve),
                edited_params=edited_params,
                feedback=feedback,
            )
            self._record_automation_recommendation(res, run_id)
            return res
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def ai_design_automation_step(
        self,
        goal: str,
        answers: dict[str, Any] | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.automation_engine.design_workflow_with_gemini(
            goal=goal,
            answers=answers,
            history=history,
        )

    def list_connectors(self) -> dict[str, Any]:
        return {"ok": True, "connectors": self.connector_registry.list_connectors()}

    def list_plugin_connections(self) -> dict[str, Any]:
        try:
            return {"ok": True, "connections": self.connector_registry.list_plugin_connections()}
        except Exception:
            return {"ok": False, "error": "Unable to read plugin connections from the vault."}

    def save_plugin_connection(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        try:
            self.connector_registry.save_plugin_config(name, values)
            return {"ok": True, "connections": self.connector_registry.list_plugin_connections()}
        except Exception:
            return {"ok": False, "error": "Unable to save plugin connection. Check the fields and vault access."}

    def save_connector_config(self, connector_id: str, values: dict[str, Any]) -> dict[str, Any]:
        try:
            res = self.connector_registry.save_config(connector_id, values)
            return {**res, "connectors": self.connector_registry.list_connectors()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def delete_connector_config(self, connector_id: str) -> dict[str, Any]:
        try:
            res = self.connector_registry.delete_config(connector_id)
            return {**res, "connectors": self.connector_registry.list_connectors()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def test_connector(self, connector_id: str) -> dict[str, Any]:
        try:
            return self.connector_registry.test_connector(connector_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def plaid_create_link_token(self) -> dict[str, Any]:
        try:
            return self.connector_registry.plaid_create_link_token()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def plaid_exchange_public_token(self, public_token: str) -> dict[str, Any]:
        try:
            return self.connector_registry.plaid_exchange_public_token(public_token)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def plaid_sandbox_quick_connect(self, institution_id: str = "ins_109508") -> dict[str, Any]:
        try:
            return self.connector_registry.plaid_sandbox_quick_connect(institution_id=institution_id)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_swarm_command_state(self) -> dict[str, Any]:
        """Return the live SessionBlackboard snapshot for Swarm Command."""
        return {"ok": True, "blackboard": self.blackboard.snapshot()}

    def get_role_bindings(self) -> dict[str, Any]:
        unlocked = self._unlocked_providers()
        return {
            "ok": True,
            "bindings": self.role_bindings.list_bindings(unlocked_providers=unlocked),
            "unlocked_providers": sorted(unlocked),
        }

    def set_role_binding(
        self,
        role: str,
        primary_model: str,
        fallback_models: list[str] | str | None = None,
    ) -> dict[str, Any]:
        try:
            parsed_fallbacks: list[str] | None
            if fallback_models is None:
                parsed_fallbacks = None
            elif isinstance(fallback_models, str):
                parsed_fallbacks = [part.strip() for part in fallback_models.split(",") if part.strip()]
            else:
                parsed_fallbacks = [str(part).strip() for part in fallback_models if str(part).strip()]
            binding = self.role_bindings.set_binding(role, primary_model, parsed_fallbacks)
            unlocked = self._unlocked_providers()
            return {
                "ok": True,
                "binding": binding.to_dict(),
                "bindings": self.role_bindings.list_bindings(unlocked_providers=unlocked),
                "unlocked_providers": sorted(unlocked),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def resolve_role_model(
        self,
        role: str,
        unhealthy_models: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            unlocked = self._unlocked_providers()
            unhealthy = {str(m).strip() for m in (unhealthy_models or []) if str(m).strip()} or None
            resolved = self.role_bindings.resolve_model(
                role,
                unlocked_providers=unlocked,
                unhealthy_models=unhealthy,
            )
            return {"ok": True, **resolved}
        except (RoleBindingResolutionError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}

    def search_models(
        self,
        query: str = "",
        provider: str = "",
        capability: str = "",
        include_deprecated: bool = False,
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
        unlocked_only: bool = False,
        tier: str = "",
    ) -> dict[str, Any]:
        try:
            res = self.registry.search_models(
                query=query,
                provider=provider,
                capability=capability,
                tier=tier,
                include_deprecated=bool(include_deprecated),
                unlocked_only=bool(unlocked_only),
                limit=int(limit),
                offset=int(offset),
                refresh=bool(refresh),
            )
            return {"ok": True, **res}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "items": [], "total": 0}

    def add_custom_model(
        self,
        model_id: str,
        name: str = "",
        provider: str = "",
        tier: str = "external",
        role: str = "",
        endpoint: str = "",
    ) -> dict[str, Any]:
        try:
            record = self.registry.add_model(
                model_id=model_id,
                name=name,
                provider=provider,
                tier=tier,
                role=role,
                endpoint=endpoint,
            )
            return {"ok": True, "model": record}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def remove_custom_model(self, model_id: str) -> dict[str, Any]:
        try:
            removed = self.registry.remove_model(model_id)
            return {"ok": True, "removed": removed, "id": model_id}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def search_skills(
        self,
        query: str = "",
        source: str = "",
        limit: int = 50,
        offset: int = 0,
        refresh: bool = False,
    ) -> dict[str, Any]:
        try:
            res = self.registry.search_skills(
                query=query,
                source=source,
                limit=int(limit),
                offset=int(offset),
                refresh=bool(refresh),
            )
            return {"ok": True, **res}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "items": [], "total": 0}

    def get_skill_detail(self, skill_id: str) -> dict[str, Any]:
        try:
            detail = self.registry.get_skill_detail(skill_id)
            if detail is None:
                return {"ok": False, "error": f"Skill '{skill_id}' not found"}
            return {"ok": True, "skill": detail}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def audit_swarm_schema(
        self,
        use_flash: bool = False,
        target: str = "live",
        flash_auditor: Any | None = None,
        raw_schema: str | None = None,
    ) -> dict[str, Any]:
        """Run deterministic schema pre-check (including INV-01..INV-05 on collective JSON/MD) and optional Flash diagnosis."""
        try:
            auditor = flash_auditor
            if auditor is None and use_flash:
                gemini_key = None
                try:
                    if hasattr(self.vault, "get_key"):
                        gemini_key = self.vault.get_key("gemini", include_env=True)
                except Exception:
                    gemini_key = None
                auditor = make_gemini_flash_auditor(api_key=gemini_key)

            unlocked = self._unlocked_providers()
            target_norm = str(target or "").strip().lower()
            if raw_schema is not None and str(raw_schema).strip():
                parsed, fmt, err = parse_swarm_input(str(raw_schema))
                if err or parsed is None:
                    return {
                        "ok": True,
                        "target": target_norm or "schema",
                        "format": fmt,
                        "valid": False,
                        "errors": [err or "Schema parse error"],
                        "warnings": [],
                        "counts": {},
                        "invariants": [],
                        "flash_diagnosis": None,
                    }
                res: SchemaAuditResult = audit_swarm_payload(
                    parsed,
                    flash_auditor=auditor if use_flash else None,
                    force_flash=bool(use_flash),
                    unlocked_providers=unlocked,
                )
                return {"ok": True, "target": target_norm or "schema", "format": fmt, **res.to_dict()}
            if target_norm == "file":
                res = audit_swarm_file(
                    SWARM_SYNC_FILE,
                    flash_auditor=auditor if use_flash else None,
                    force_flash=bool(use_flash),
                    unlocked_providers=unlocked,
                )
            elif target_norm == "schema" and self._swarm_schema_path.is_file():
                res = audit_swarm_file(
                    self._swarm_schema_path,
                    flash_auditor=auditor if use_flash else None,
                    force_flash=bool(use_flash),
                    unlocked_providers=unlocked,
                )
            else:
                snap = self.get_snapshot()
                res = audit_swarm_payload(
                    snap.get("swarm") or {},
                    flash_auditor=auditor if use_flash else None,
                    force_flash=bool(use_flash),
                    unlocked_providers=unlocked,
                )
            return {"ok": True, "target": target, **res.to_dict()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "valid": False, "errors": [str(exc)], "warnings": [], "invariants": []}

    def run_health_check(self) -> dict[str, Any]:
        """Perform a structured health check across backend APIs, Vault keys, schema, and workspace."""
        try:
            snap = self.get_snapshot()
            unlocked = self._unlocked_providers()
            schema_audit = snap.get("schema_audit") or {}
            health = snap.get("health") or {}
            api_ok = bool(snap.get("api_ok"))
            api_8095_ok = bool(snap.get("api_8095_ok"))
            workspace = snap.get("workspace") or {}
            workspace_root = workspace.get("root") or workspace.get("swarm_workspace_root")
            swarm = snap.get("swarm") or {}

            checks = {
                "backend_8087": {
                    "ok": api_ok,
                    "status": "ONLINE" if api_ok else "OFFLINE (local fallback active)",
                    "error": health.get("error") if isinstance(health, dict) else None,
                },
                "backend_8095": {
                    "ok": api_8095_ok,
                    "status": "ONLINE" if api_8095_ok else "OFFLINE",
                },
                "vault": {
                    "ok": bool(unlocked),
                    "unlocked_providers": sorted(list(unlocked)),
                    "unlocked_count": len(unlocked),
                },
                "schema": {
                    "ok": bool(schema_audit.get("valid", False)),
                    "errors": list(schema_audit.get("errors") or []),
                    "warnings": list(schema_audit.get("warnings") or []),
                    "counts": dict(schema_audit.get("counts") or {}),
                    "invariants": list(schema_audit.get("invariants") or []),
                },
                "workspace": {
                    "ok": bool(workspace_root and Path(workspace_root).is_dir()),
                    "root": str(workspace_root or ""),
                },
                "swarm": {
                    "active_workers": int(swarm.get("active_worker_count") or 0),
                    "active_missions": len(swarm.get("active_missions") or []),
                },
            }

            is_healthy = checks["schema"]["ok"] and checks["workspace"]["ok"]
            status = "UNHEALTHY"
            if is_healthy:
                status = "HEALTHY" if api_ok and api_8095_ok else "DEGRADED"

            return {
                "ok": True,
                "status": status,
                "checks": checks,
                "timestamp": time.time(),
            }
        except Exception as exc:
            return {"ok": False, "status": "ERROR", "error": str(exc)}

    def get_swarm_schema(self) -> dict[str, Any]:
        """Return the active schema overlaid with the current runtime A2A graph."""
        try:
            if self._swarm_schema_path.is_file():
                raw = self._swarm_schema_path.read_text(encoding="utf-8")
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("Persisted swarm schema must be a JSON object.")
                runtime_links = self.swarm_runner.get_a2a_links()
                runtime_links_file_exists = False
                try:
                    runtime_links_file_exists = isinstance(
                        json.loads(self._swarm_a2a_links_path.read_text(encoding="utf-8")), list
                    )
                except Exception:
                    pass
                if runtime_links or runtime_links_file_exists:
                    parsed["a2a_links"] = self._schema_a2a_links(parsed, runtime_links)
                return {
                    "ok": True,
                    "exists": True,
                    "path": str(self._swarm_schema_path),
                    "schema": parsed,
                    "raw_json": json.dumps(parsed, indent=2),
                    "raw_markdown": serialize_swarm_markdown(parsed),
                }
            return {"ok": True, "exists": False, "path": str(self._swarm_schema_path), "schema": None}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "exists": False, "path": str(self._swarm_schema_path)}

    def get_transition_traces(self, mission_id: str) -> dict[str, Any]:
        """Fetch structured transition traces and critic rubric evaluations for a mission."""
        try:
            from sabrina_console_desktop.tools.telemetry_blackboard import get_telemetry_blackboard
            tb = get_telemetry_blackboard()
            traces = tb.get_mission_transition_traces(mission_id)
            return {"ok": True, "mission_id": str(mission_id), "traces": traces, "count": len(traces)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "traces": [], "count": 0}

    def load_preset_swarm_schema(self, preset_name: str = "crypto_market_research") -> dict[str, Any]:
        """Load a predefined swarm schema preset (e.g. crypto_market_research or security_audit)."""
        preset_clean = str(preset_name or "m_eulerian_rsi").strip().lower()
        if preset_clean in ("crypto", "crypto_market", "crypto_market_research"):
            candidates = [
                self._swarm_schema_path.parent / "crypto_market_research_schema.json",
                ROOT / "data" / "control" / "crypto_market_research_schema.json",
            ]
        elif preset_clean in ("m_eulerian", "m_eulerian_rsi", "eulerian", "rsi"):
            candidates = [
                self._swarm_schema_path.parent / "m_eulerian_rsi_schema.json",
                ROOT / "data" / "control" / "m_eulerian_rsi_schema.json",
            ]
        else:
            candidates = [
                self._swarm_schema_path.parent / f"{preset_clean}_schema.json",
                ROOT / "data" / "control" / f"{preset_clean}_schema.json",
            ]

        schema_file = next((p for p in candidates if p.is_file()), candidates[0])
        if not schema_file.is_file():
            return {"ok": False, "error": f"Schema preset file not found: {schema_file.name}", "execution_authorized": False}
        try:
            raw_content = schema_file.read_text(encoding="utf-8")
            return self.save_swarm_schema(raw_content, sync_role_bindings=True)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    def save_swarm_schema(self, raw_json: str, sync_role_bindings: bool = True) -> dict[str, Any]:
        """Validate, persist, and apply a Swarm Schema (.JSON or .MD) payload to the active swarm & role bindings."""
        parsed, fmt, parse_err = parse_swarm_input(str(raw_json or ""))
        if parse_err or parsed is None:
            return {"ok": False, "error": parse_err or "Invalid schema payload", "format": fmt, "execution_authorized": False}
        if not isinstance(parsed, dict):
            return {"ok": False, "error": "Top-level schema must be a JSON object.", "format": fmt, "execution_authorized": False}

        # A schema without an A2A section should preserve the deployed graph. If it
        # includes a list (including []), that list is an explicit graph update.
        a2a_links_explicit = isinstance(parsed.get("a2a_links"), list)
        previous_runtime_links = self.swarm_runner.get_a2a_links()
        if not a2a_links_explicit and previous_runtime_links:
            parsed["a2a_links"] = self._schema_a2a_links(parsed, previous_runtime_links)

        # Enforce hard safety lock invariant
        parsed["execution_authorized"] = False
        unlocked = self._unlocked_providers()
        audit_res = audit_swarm_payload(parsed, unlocked_providers=unlocked)
        if not audit_res.valid:
            return {
                "ok": False,
                "error": "; ".join(audit_res.errors),
                "errors": audit_res.errors,
                "warnings": audit_res.warnings,
                "counts": audit_res.counts,
                "invariants": audit_res.invariants,
                "format": fmt,
                "execution_authorized": False,
            }

        runtime_links_to_restore: list[dict[str, Any]] | None = None
        if a2a_links_explicit:
            runtime_links_to_restore = previous_runtime_links
            runtime_links = self._runtime_a2a_links(parsed, parsed.get("a2a_links") or [])
            applied_runtime_links = self.swarm_runner.set_a2a_links(runtime_links)
            parsed["a2a_links"] = self._schema_a2a_links(parsed, applied_runtime_links)

        try:
            self._swarm_schema_path.parent.mkdir(parents=True, exist_ok=True)
            formatted = json.dumps(parsed, indent=2)
            self._swarm_schema_path.write_text(formatted, encoding="utf-8")
        except Exception as exc:
            if runtime_links_to_restore is not None:
                self.swarm_runner.set_a2a_links(runtime_links_to_restore)
            return {"ok": False, "error": f"Failed to write schema file: {exc}", "execution_authorized": False}

        if a2a_links_explicit:
            self._persist_swarm_a2a_links()

        updated_roles: list[str] = []
        if sync_role_bindings:
            raw_agents = parsed.get("agents") or parsed.get("workers") or parsed.get("nodes") or []
            agents_list = (
                raw_agents
                if isinstance(raw_agents, list)
                else [
                    {"id": k, **(v if isinstance(v, dict) else {})}
                    for k, v in (raw_agents.items() if isinstance(raw_agents, dict) else [])
                ]
            )
            for item in agents_list:
                if not isinstance(item, dict) or item.get("enabled") is False:
                    continue
                role = str(item.get("role") or "").strip()
                model = str(item.get("model") or item.get("primary_model") or "").strip()
                if role and model:
                    fallbacks = item.get("fallback_models")
                    try:
                        existing = self.role_bindings.get_binding(role)
                        fb_list = (
                            fallbacks
                            if isinstance(fallbacks, list)
                            else list(getattr(existing, "fallback_models", None) or [])
                        )
                        self.role_bindings.set_binding(role, model, fb_list)
                        updated_roles.append(role)
                    except Exception:
                        pass

        topo = parsed.get("topology") if isinstance(parsed.get("topology"), dict) else {}
        if hasattr(self.swarm_runner, "configure_topology"):
            try:
                self.swarm_runner.configure_topology(
                    coordinator_count=topo.get("coordinator_count"),
                    workers_per_coordinator=topo.get("workers_per_coordinator"),
                    topology_mode=topo.get("topology_mode"),
                    branching_order=topo.get("branching_order"),
                    quorum_split_threshold=topo.get("quorum_split_threshold"),
                    quorum_prune_threshold=topo.get("quorum_prune_threshold"),
                    enable_work_stealing=topo.get("enable_work_stealing"),
                    max_total_agents=topo.get("max_total_agents"),
                    max_concurrency=topo.get("max_concurrency"),
                )
            except Exception:
                pass

        self.local_feed.insert(
            0,
            {
                "actor": "Swarm Schema",
                "badge": "SCHEMA",
                "time": _clock_str(),
                "ago": "just now",
                "summary": f"Applied collective schema ({audit_res.counts.get('agents', 0)} agents, {len(updated_roles)} role bindings synced).",
            },
        )
        return {
            "ok": True,
            "path": str(self._swarm_schema_path),
            "format": fmt,
            "normalized_json": formatted,
            "counts": audit_res.counts,
            "warnings": audit_res.warnings,
            "invariants": audit_res.invariants,
            "updated_roles": updated_roles,
            "execution_authorized": False,
            "message": f"Saved & applied schema to {self._swarm_schema_path.name} ({len(updated_roles)} role bindings synced).",
        }

    def set_swarm_ratio(
        self,
        coordinator_count: int = 1,
        workers_per_coordinator: int = 4,
        topology_mode: str = "m_eulerian_join_cut",
        quorum_split_threshold: int = 6,
        enable_work_stealing: bool = True,
        branching_order: int = 3,
    ) -> dict[str, Any]:
        """Configure and persist coordinator-to-worker ratio & topology settings."""
        try:
            cfg = self.swarm_runner.configure_topology(
                coordinator_count=coordinator_count,
                workers_per_coordinator=workers_per_coordinator,
                topology_mode=topology_mode,
                branching_order=branching_order,
                quorum_split_threshold=quorum_split_threshold,
                enable_work_stealing=enable_work_stealing,
            )
            return {"ok": True, **cfg, "execution_authorized": False}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    def get_launch_log_tail(self, lines: int = 40) -> dict[str, Any]:
        log_path = ROOT / "console-launch.log"
        if not log_path.is_file():
            log_path = ROOT.parent / "console-launch.log"
        if not log_path.is_file():
            return {"ok": True, "lines": [], "path": str(log_path)}
        try:
            with log_path.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 32768), os.SEEK_SET)
                chunk = fh.read().decode("utf-8", errors="replace")
            tail = chunk.splitlines()[-max(1, min(200, int(lines))):]
            return {"ok": True, "lines": tail, "path": str(log_path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "lines": []}


    def _is_safe_workspace_root(self, folder: Path) -> tuple[bool, str]:
        try:
            resolved = folder.expanduser().resolve()
        except Exception as exc:
            return False, f"Invalid directory path: {exc}"
        if resolved == resolved.parent:
            return False, f"Refusing filesystem root directory: {resolved}"
        lower_str = str(resolved).lower().rstrip("\\/")
        blocked_prefixes = (
            r"c:\windows",
            r"c:\program files",
            r"c:\program files (x86)",
            r"c:\programdata",
            r"c:\$recycle.bin",
            r"c:\system volume information",
        )
        for prefix in blocked_prefixes:
            if lower_str == prefix or lower_str.startswith(prefix + "\\"):
                return False, f"Refusing protected system directory: {resolved}"
        blocked_names = {".ssh", ".aws", ".gnupg", ".kube", ".azure", ".git"}
        if any(part.lower() in blocked_names for part in resolved.parts):
            return False, f"Refusing sensitive/credential directory: {resolved}"
        return True, ""

    def _save_workspace_state(self) -> None:
        try:
            self._workspace_state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "active_root": str(self.workspace_sandbox.root) if self.workspace_sandbox is not None else "",
                "mode": self.workspace_mode,
                "policy_profile": self.workspace_policy_profile,
                "allow_local_venv_fallback": bool(self.allow_local_venv_fallback),
                "recent_workspaces": list(self._recent_workspaces[:8]),
            }
            self._workspace_state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _restore_workspace_state(self) -> None:
        if not self._workspace_state_path.is_file():
            return
        try:
            data = json.loads(self._workspace_state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            recent_raw = data.get("recent_workspaces") or []
            if isinstance(recent_raw, list):
                cleaned: list[str] = []
                for item in recent_raw:
                    s = str(item or "").strip()
                    if s and s not in cleaned:
                        cleaned.append(s)
                self._recent_workspaces = cleaned[:8]
            saved_mode = str(data.get("mode") or self.workspace_mode)
            if saved_mode in VALID_WORKSPACE_MODES:
                self.workspace_mode = saved_mode
            saved_policy = str(data.get("policy_profile") or self.workspace_policy_profile)
            if saved_policy in VALID_POLICY_PROFILES:
                self.workspace_policy_profile = saved_policy
            self.allow_local_venv_fallback = bool(data.get("allow_local_venv_fallback", False))
            active_root = str(data.get("active_root") or "").strip()
            if active_root:
                p = Path(active_root).expanduser()
                if p.is_dir():
                    safe, _ = self._is_safe_workspace_root(p)
                    if safe:
                        self._bind_workspace(p, mode=self.workspace_mode, persist=False)
        except Exception:
            pass

    def _workspace_gemini_key(self) -> str | None:
        return self.vault.get_key("gemini", include_env=False)

    def _bind_workspace(self, root_dir: Path, mode: str, *, persist: bool = True) -> dict[str, Any]:
        resolved = Path(root_dir).expanduser().resolve()
        safe_mode = mode if mode in VALID_WORKSPACE_MODES else "approve_writes"
        with self._workspace_agent_lock:
            sandbox = WorkspaceSandbox(
                resolved,
                allow_local_venv_fallback=self.allow_local_venv_fallback,
                policy_profile=self.workspace_policy_profile,
            )
            profile = self.harness_registry.native_profile()
            agent = profile.create_agent(sandbox, mode=safe_mode, api_key_provider=self._workspace_gemini_key)
            self.workspace_mode = safe_mode
            self.workspace_sandbox = sandbox
            self.workspace_agent = agent
            self._workspace_agent_revision = profile.revision
        if hasattr(self.swarm_runner, "set_workspace_root"):
            self.swarm_runner.set_workspace_root(resolved)
        else:
            self.swarm_runner.workspace_root = resolved
        root_str = str(resolved)
        self._recent_workspaces = [root_str] + [p for p in self._recent_workspaces if p.lower() != root_str.lower()]
        self._recent_workspaces = self._recent_workspaces[:8]
        if persist:
            self._save_workspace_state()
        return self.get_workspace_status()

    def _get_machine_index_cached(self, swarm_root: str, *, force: bool = False) -> dict[str, Any]:
        now_m = time.monotonic()
        cached = getattr(self, "_cached_machine_index", None)
        cached_at = float(getattr(self, "_cached_machine_index_at", 0.0) or 0.0)
        cached_root = str(getattr(self, "_cached_machine_index_root", "") or "")
        if not force and isinstance(cached, dict) and cached_root == swarm_root and (now_m - cached_at) < 15.0:
            return cached
        cat_db = getattr(self, "_catalog_db_path", None)
        snap = get_machine_and_catalog_snapshot(
            swarm_root,
            recent_workspaces=list(self._recent_workspaces[:8]),
            catalog_db_path=cat_db,
        )
        self._cached_machine_index = snap
        self._cached_machine_index_at = now_m
        self._cached_machine_index_root = swarm_root
        return snap

    def _get_workspace_summary(self) -> dict[str, Any]:
        active = self.workspace_sandbox is not None
        root_str = str(self.workspace_sandbox.root) if self.workspace_sandbox is not None else ""
        swarm_root = str(getattr(self.swarm_runner, "workspace_root", self._default_workspace_root))
        activity = self.swarm_runner.workspace_tracker.scan_delta(force=False)
        machine_idx = self._get_machine_index_cached(swarm_root, force=False)
        eff_root = root_str or swarm_root
        return {
            "active": active,
            "root": root_str,
            "effective_root": eff_root,
            "name": Path(eff_root).name if eff_root else "",
            "mode": self.workspace_mode,
            "policy_profile": self.workspace_sandbox.policy_profile if self.workspace_sandbox is not None else self.workspace_policy_profile,
            "swarm_workspace_root": swarm_root,
            "recent_workspaces": list(self._recent_workspaces[:8]),
            "activity": activity,
            "machine_index": machine_idx,
            "execution_authorized": False,
        }

    def inspect_topology_file(self, rel_path: str) -> dict[str, Any]:
        """Inspect a workspace file (source, unified diff, AST symbols/imports, image/SVG preview) safely."""
        return self.swarm_runner.workspace_tracker.inspect_file(rel_path)

    def rescan_topology_workspace(self, reset_baseline: bool = False) -> dict[str, Any]:
        """Rescan the active workspace or reset the CREATED/MODIFIED baseline."""
        if bool(reset_baseline):
            self.swarm_runner.workspace_tracker.capture_baseline(clear_history=True)
        activity = self.swarm_runner.workspace_tracker.scan_delta(force=True)
        swarm_root = str(getattr(self.swarm_runner, "workspace_root", self._default_workspace_root))
        return {
            "ok": True,
            "execution_authorized": False,
            "reset_baseline": bool(reset_baseline),
            "activity": activity,
            "workspace": self._get_workspace_summary(),
            "machine_index": self._get_machine_index_cached(swarm_root, force=True),
        }

    def index_workspace_and_machines(
        self,
        query: str = "",
        include_rag_fts: bool = False,
        extra_root: str = "",
    ) -> dict[str, Any]:
        """Index active/enrolled local workspace roots into `lab_code_catalog` (and optional `code_indexer` FTS5) or search indexed symbols."""
        swarm_root = str(getattr(self.swarm_runner, "workspace_root", self._default_workspace_root))
        roots: list[str] = [swarm_root]
        if extra_root and str(extra_root).strip():
            roots.insert(0, str(extra_root).strip())
        for rw in self._recent_workspaces[:4]:
            if rw and rw not in roots:
                roots.append(rw)
        cat_db = getattr(self, "_catalog_db_path", None)
        res = refresh_and_search_machine_catalog(
            roots,
            query=query,
            include_rag_fts=bool(include_rag_fts),
            catalog_db_path=cat_db,
        )
        self._cached_machine_index = res.get("snapshot")
        self._cached_machine_index_at = time.monotonic()
        self._cached_machine_index_root = swarm_root
        return res

    def open_topology_file_in_browser(self, rel_path: str) -> dict[str, Any]:
        """Open a jailed in-workspace HTML/SVG/image deliverable in the system browser upon explicit operator click."""
        import webbrowser
        from swarm_console.protocol import guard_safe_path

        clean_rel = str(rel_path or "").strip().replace("\\", "/").lstrip("/")
        if not clean_rel:
            return {"ok": False, "error": "Empty file path.", "execution_authorized": False}
        root = Path(getattr(self.swarm_runner, "workspace_root", self._default_workspace_root)).resolve()
        try:
            cand = (root / clean_rel).resolve()
            if not cand.is_relative_to(root) or not cand.is_file():
                return {"ok": False, "error": f"File not found inside workspace: {clean_rel}", "execution_authorized": False}
            guard_safe_path(cand)
            if cand.suffix.lower() not in {".html", ".htm", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt", ".md"}:
                return {"ok": False, "error": f"Browser preview only supports HTML/SVG/image/doc files (got {cand.suffix}).", "execution_authorized": False}
            if not webbrowser.open(cand.as_uri()):
                return {"ok": False, "error": "System browser did not accept the preview.", "execution_authorized": False}
            return {"ok": True, "path": cand.relative_to(root).as_posix(), "uri": cand.as_uri(), "execution_authorized": False}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    def get_workspace_status(self) -> dict[str, Any]:
        swarm_root = str(getattr(self.swarm_runner, "workspace_root", self._default_workspace_root))
        if self.workspace_sandbox is None:
            return {
                "ok": True,
                "active": False,
                "root": "",
                "name": "",
                "default_sandbox_root": str(DEFAULT_SANDBOX_ROOT),
                "swarm_workspace_root": swarm_root,
                "recent_workspaces": list(self._recent_workspaces[:8]),
                "mode": self.workspace_mode,
                "policy_profile": self.workspace_policy_profile,
                "active_harness_id": self.harness_registry.active_harness_id,
                "allow_local_venv_fallback": self.allow_local_venv_fallback,
                "entries": [],
                "pending_approvals": [],
                "staged_diffs": [],
                "checkpoints": [],
                "processes": [],
                "execution_authorized": False,
            }
        tree = self.workspace_sandbox.execute_tool("list_dir", {"path": ".", "max_depth": 3})
        pending = list(self.workspace_agent.pending_actions.values()) if self.workspace_agent else []
        root_str = str(self.workspace_sandbox.root)
        return {
            "ok": True,
            "active": True,
            "root": root_str,
            "name": Path(root_str).name,
            "default_sandbox_root": str(DEFAULT_SANDBOX_ROOT),
            "swarm_workspace_root": swarm_root,
            "recent_workspaces": list(self._recent_workspaces[:8]),
            "mode": self.workspace_mode,
            "policy_profile": self.workspace_sandbox.policy_profile,
            "active_harness_id": self.harness_registry.active_harness_id,
            "allow_local_venv_fallback": self.allow_local_venv_fallback,
            "entries": tree.get("entries", []),
            "pending_approvals": pending,
            "staged_diffs": self.workspace_sandbox.list_staged_diffs(),
            "checkpoints": self.workspace_sandbox.list_checkpoints()[:15],
            "processes": self.workspace_sandbox.list_processes(),
            "execution_authorized": False,
        }

    def set_workspace_directory(
        self,
        path_str: str,
        mode: str = "",
        create_if_missing: bool = False,
    ) -> dict[str, Any]:
        raw = str(path_str or "").strip().strip('"').strip("'").strip()
        if not raw:
            return {"ok": False, "error": "Enter a workspace directory path.", "execution_authorized": False}
        try:
            expanded = os.path.expandvars(raw)
            folder = Path(expanded).expanduser()
            if not folder.is_absolute():
                folder = (DEFAULT_SANDBOX_ROOT / folder).resolve()
            else:
                folder = folder.resolve()
        except Exception as exc:
            return {"ok": False, "error": f"Invalid workspace path: {exc}", "execution_authorized": False}

        safe, reason = self._is_safe_workspace_root(folder)
        if not safe:
            return {"ok": False, "error": reason, "execution_authorized": False}

        if not folder.exists():
            if create_if_missing:
                try:
                    folder.mkdir(parents=True, exist_ok=True)
                except Exception as exc:
                    return {"ok": False, "error": f"Could not create directory: {exc}", "execution_authorized": False}
            else:
                return {
                    "ok": False,
                    "error": f"Directory does not exist: {folder}",
                    "execution_authorized": False,
                }
        if not folder.is_dir():
            return {"ok": False, "error": f"Path is not a directory: {folder}", "execution_authorized": False}

        chosen_mode = mode if mode in VALID_WORKSPACE_MODES else (self.workspace_mode or "approve_writes")
        try:
            status = self._bind_workspace(folder, mode=chosen_mode)
            self._append_chat("Sabrina Workspace", f"Workspace set to {folder} (mode: {chosen_mode}; Swarm missions & Workspace Agent synced).")
            return status
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    def clear_workspace(self) -> dict[str, Any]:
        with self._workspace_agent_lock:
            self.workspace_sandbox = None
            self.workspace_agent = None
            self._workspace_agent_revision = ""
        if hasattr(self.swarm_runner, "set_workspace_root"):
            self.swarm_runner.set_workspace_root(self._default_workspace_root)
        else:
            self.swarm_runner.workspace_root = self._default_workspace_root
        self._save_workspace_state()
        return self.get_workspace_status()

    def create_sandbox_project(self, name: str) -> dict[str, Any]:
        try:
            target = _create_sandbox_project(name, base_root=DEFAULT_SANDBOX_ROOT)
            status = self._bind_workspace(target, mode="sandbox_auto")
            self._append_chat("Sabrina Workspace", f"Created sandbox project at {target} (mode: sandbox_auto).")
            return status
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def pick_workspace_folder(self) -> dict[str, Any]:
        if self._window is None:
            return {"ok": False, "error": "Desktop window not ready."}
        try:
            selected = self._window.create_file_dialog(webview.FOLDER_DIALOG)
            if not selected:
                return {"ok": False, "error": "Folder selection cancelled."}
            folder = Path(selected[0] if isinstance(selected, (list, tuple)) else str(selected))
            safe, reason = self._is_safe_workspace_root(folder)
            if not safe:
                return {"ok": False, "error": reason}
            status = self._bind_workspace(folder, mode="approve_writes")
            self._append_chat("Sabrina Workspace", f"Attached workspace {folder} (mode: approve_writes).")
            return status
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_workspace_options(
        self,
        mode: str,
        allow_local_venv_fallback: bool,
        policy_profile: str = "",
    ) -> dict[str, Any]:
        if mode not in VALID_WORKSPACE_MODES:
            return {"ok": False, "error": f"Invalid mode: {mode}"}
        if policy_profile:
            if policy_profile not in VALID_POLICY_PROFILES:
                return {"ok": False, "error": f"Invalid policy_profile: {policy_profile}"}
            self.workspace_policy_profile = policy_profile
        self.workspace_mode = mode
        self.allow_local_venv_fallback = bool(allow_local_venv_fallback)
        if self.workspace_sandbox is not None:
            self.workspace_sandbox.allow_local_venv_fallback = self.allow_local_venv_fallback
            if policy_profile:
                self.workspace_sandbox.set_policy_profile(policy_profile)
        if self.workspace_agent is not None:
            self.workspace_agent.mode = mode
        self._save_workspace_state()
        return self.get_workspace_status()

    def workspace_create_item(self, kind: str, rel_path: str, content: str = "") -> dict[str, Any]:
        if self.workspace_sandbox is None:
            return {"ok": False, "error": "No active project workspace. Create a sandbox or open a folder first."}
        try:
            if kind == "dir":
                res = self.workspace_sandbox.execute_tool("create_dir", {"path": rel_path})
            else:
                before = self.swarm_runner.workspace_tracker.snapshot_before_write(rel_path)
                res = self.workspace_sandbox.execute_tool("write_file", {"path": rel_path, "content": content, "overwrite": False})
                mut = self.swarm_runner.workspace_tracker.record_after_write(
                    rel_path,
                    before,
                    worker_id="operator",
                    worker_name="Operator Workspace",
                    role="operator",
                    source="workspace_ui",
                )
                self.swarm_runner._record_worker_file_mutation(mut)
            return {"ok": True, "result": res, "workspace": self.get_workspace_status()}
        except WorkspaceSandboxError as exc:
            return {"ok": False, "error": str(exc)}

    def _console_info(self, section: str, query: str = "", offset: int = 0) -> dict[str, Any]:
        """Return bounded, read-only facts for the native agent's self-inspection tool."""
        if section == "skill_detail":
            if (not isinstance(query, str) or not query.strip() or len(query) > 200
                    or isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 32768):
                return {"ok": False, "error": "An exact skill ID and a valid offset are required."}
            skill_id = query.strip()
            detail = self.registry.get_skill_detail(skill_id)
            if detail is None:
                self.registry.refresh_skills(force=True)
                detail = self.registry.get_skill_detail(skill_id)
            if detail is None:
                return {"ok": False, "error": "Skill not found in the current registry."}
            content = str(detail.get("content") or "")
            if not content:
                return {"ok": False, "error": "The skill's instructions could not be read."}
            if offset > len(content):
                return {"ok": False, "error": "The skill offset is past the available instructions."}
            end = min(len(content), offset + 1800)
            while True:
                result = {"ok": True, "skill": {
                    "id": str(detail.get("id") or "")[:200],
                    "name": str(detail.get("name") or "")[:200],
                    "instructions": content[offset:end],
                    "offset": offset,
                    "next_offset": end if end < len(content) else None,
                    "available_characters": len(content),
                    "registry_may_have_truncated": len(content) >= 32768,
                }}
                if len(json.dumps(result, ensure_ascii=False)) <= 3000 or end == offset:
                    return result
                end = offset + (end - offset) * 3 // 4
        if section == "skills":
            result = self.search_skills(query=query, limit=8)
            return {"ok": bool(result.get("ok")), "total": result.get("total", 0),
                    "skills": [{key: str(item.get(key) or "")[:limit]
                                for key, limit in (("id", 100), ("name", 80), ("description", 80), ("source", 30))}
                               for item in result.get("items", []) if isinstance(item, dict)],
                    **({"error": result.get("error")} if result.get("error") else {})}
        if section == "plugins":
            from tools.plugin_console import discover
            plugins, errors = discover()
            matched = [p for name, p in sorted(plugins.items())
                       if not query or query.lower() in (name + " " + str(p.get("description") or "")).lower()]
            return {"ok": True, "total": len(matched),
                    "plugins": [{"name": p["name"], "description": str(p.get("description") or "")[:160],
                                 "access": p.get("access")} for p in matched[:10]],
                    "discovery_error_count": len(errors),
                    "execution": "Listing a plugin does not grant permission to run it. The /plugin command is available to the operator; agent requests require an explicitly scoped plugin request and the workspace policy."}

        sandbox = self.workspace_sandbox
        tool_states = []
        for name in sorted(TOOL_INPUT_MODELS):
            if sandbox is None:
                state = "unavailable: no workspace attached"
            else:
                permitted, reason = sandbox.check_policy(name)
                if not permitted:
                    state = reason
                elif name in MUTATING_TOOLS and self.workspace_mode == "read_only":
                    state = "blocked: workspace is read-only"
                elif name in MUTATING_TOOLS and self.workspace_mode == "approve_writes":
                    state = "requires operator approval"
                else:
                    state = "available within the workspace"
            tool_states.append({"name": name, "state": state})
        with self._remote_lock:
            checked = self._remote_last_ts > 0
            api_ok = bool(self._remote_cache.get("api_ok"))
            model_count = len(self._remote_cache.get("ollama_models") or [])
            age = round(max(0.0, time.monotonic() - self._remote_last_ts), 1) if checked else None
        return {
            "ok": True,
            "identity": "Sabrina Console's workspace agent",
            "active_harness": self.harness_registry.active_harness_id,
            "workspace": {"attached": sandbox is not None, "root": str(sandbox.root) if sandbox else None,
                          "mode": self.workspace_mode,
                          "policy": sandbox.policy_profile if sandbox else self.workspace_policy_profile},
            "tools": tool_states,
            "console_info_tool": "Read-only; can inspect overview, status, tools, plugins and skills.",
            "plugin_access": "Plugin discovery is not plugin execution. Agent plugin requests require explicit scope and policy permission; the operator can use /plugin commands.",
            "operator_commands": ["/status", "/skills", "/plugin", "/mission", "/research"],
            "cached_connections": {"checked": checked, "age_seconds": age,
                                   "swarm_api": ("reachable" if api_ok else "unreachable") if checked else "not checked",
                                   "ollama_models_reported": model_count if checked else None},
        }

    def _chat_without_workspace(self, message: str, *, cancel_event: threading.Event | None = None) -> dict[str, Any]:
        if cancel_event is not None and cancel_event.is_set():
            return {"ok": False, "cancelled": True}
        key = self._workspace_gemini_key() or os.environ.get("GEMINI_API_KEY", "").strip()
        if not key:
            error = "Sabrina chat needs an unlocked Gemini key. Select a direct model or council, or add a Gemini key."
            self._append_chat("Sabrina", error)
            return {"ok": False, "error": error}
        facts = self._console_info("overview")
        system = ("You are Sabrina, the assistant in Sabrina Console. Answer concisely and honestly. "
                  "No workspace is attached and this chat has no callable tools. Do not claim to have acted. "
                  "To use workspace tools, the operator can open a folder in the Workspace panel. "
                  "Current console information: " + json.dumps(facts, ensure_ascii=False))
        result = self._invoke_provider_text("gemini", DEFAULT_GEMINI_MODEL, system, message, api_key=key)
        if cancel_event is not None and cancel_event.is_set():
            return {"ok": False, "cancelled": True}
        if not result.get("ok") or not str(result.get("text") or "").strip():
            error = str(result.get("error") or "Sabrina returned an empty response.")
            self._append_chat("Sabrina Error", error)
            return {"ok": False, "error": error}
        reply = str(result["text"]).strip()
        self._append_chat("Sabrina", reply)
        return {"ok": True, "reply": reply, "workspace_attached": False}

    def _requested_workspace_skills(self, message: str) -> tuple[str, list[dict[str, Any]]]:
        """Deliver expressly named registry skills without expanding tool authority."""
        patterns = (
            r"\b(?:use|apply|follow|using)\s+(?:the\s+)?[`'\"]?([a-z0-9][a-z0-9_-]{0,127})[`'\"]?\s+skill\b",
            r"\b(?:use|apply|follow|using)\s+(?:the\s+)?skill\s+[`'\"]?([a-z0-9][a-z0-9_-]{0,127})\b",
            r"\b(?:use|apply|follow|using)\s+(?:the\s+)?[`'\"]?([a-z0-9][a-z0-9_-]*[-_][a-z0-9_-]+)\b",
            r"\b(?:and|with)\s+(?:the\s+)?[`'\"]?([a-z0-9][a-z0-9_-]{0,127})[`'\"]?\s+skill\b",
            r"(?<!\w)\$([a-z0-9][a-z0-9_-]{0,127})\b",
        )
        matches = sorted((m for pattern in patterns for m in re.finditer(pattern, message, re.I)), key=lambda m: m.start())
        parts: list[str] = []
        loaded: list[dict[str, Any]] = []
        seen: set[str] = set()
        forced_refresh = False
        for match in matches:
            if re.search(r"\b(?:do\s+not|don't|never|not)\s*$", message[max(0, match.start() - 20):match.start()], re.I):
                continue
            if re.match(r"(?:and|with)\b", match.group(0), re.I):
                clause = re.split(r"[.!?;\n]", message[max(0, match.start() - 160):match.start()])[-1]
                if re.search(r"\b(?:do\s+not|don't|never)\s+(?:use|apply|follow)\b", clause, re.I):
                    continue
            skill_id = match.group(1).lower()
            if skill_id in seen or len(loaded) >= 4:
                continue
            seen.add(skill_id)
            try:
                detail = self.registry.get_skill_detail(skill_id)
                if not detail and not forced_refresh:
                    self.registry.refresh_skills(force=True)
                    forced_refresh = True
                    detail = self.registry.get_skill_detail(skill_id)
            except Exception:
                continue
            content = str((detail or {}).get("content") or "")
            if not content:
                continue
            excerpt = content[:8000]
            truncated = len(content) > len(excerpt)
            parts.append(f"<registry-skill id=\"{skill_id}\">\n{excerpt}\n</registry-skill>" +
                         (f"\nSkill truncated; use get_console_info skill_detail for {skill_id} and its offsets." if truncated else ""))
            loaded.append({"id": skill_id, "loaded_chars": len(excerpt), "truncated": truncated})
        return "\n\n".join(parts), loaded

    def send_workspace_chat(
        self,
        message: str,
        model: str = DEFAULT_GEMINI_MODEL,
        effort: str = "medium",
        cancel_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        try:
            with self._workspace_agent_lock:
                sandbox = self.workspace_sandbox
                agent = self.workspace_agent
                if agent is None or sandbox is None:
                    msg = "No active workspace selected. Click '+ New Sandbox' or 'Open Folder' in Swarm Chat first."
                    self._append_chat("Sabrina Workspace", f"[INFO] {msg}")
                    return {"ok": False, "error": msg}

                harness_id = self.harness_registry.active_harness_id
                if harness_id != NATIVE_HARNESS_ID:
                    res = self.run_harness_turn(message, harness_id=harness_id)
                    if cancel_event is not None and cancel_event.is_set():
                        return {"ok": False, "cancelled": True}
                    label = res.get("run_meta", {}).get("harness_name", harness_id)
                    if res.get("ok"):
                        self._append_chat(label, res["reply"])
                    else:
                        detail = res.get("error") or next(
                            (event["summary"] for event in res.get("events", []) if event["kind"] == "error"),
                            "Harness turn failed.",
                        )
                        self._append_chat(label, detail)
                        res = {**res, "error": detail}
                    return {**res, "harness_id": harness_id, "workspace": self.get_workspace_status()}

                profile = self.harness_registry.native_profile()
                if profile.revision != self._workspace_agent_revision and not agent.pending_actions:
                    agent = profile.create_agent(
                        sandbox, mode=self.workspace_mode, api_key_provider=self._workspace_gemini_key
                    )
                    self.workspace_agent = agent
                    self._workspace_agent_revision = profile.revision
                revision = self._workspace_agent_revision
                agent.console_info_provider = lambda section, query, offset=0: {
                    **self._console_info(section, query, offset), "model": model,
                }
                skill_guidance, registry_skills = self._requested_workspace_skills(message)
                res = agent.send_message(message, model=model, thinking_level=effort, cancel_event=cancel_event,
                                         skill_guidance=skill_guidance)
                res["registry_skills"] = registry_skills
                if res.get("cancelled") or (cancel_event is not None and cancel_event.is_set()):
                    return {**res, "ok": False, "cancelled": True}
            for hev in res.get("harness_events") or []:
                self.swarm_runner.record_harness_event(
                    {**hev, "harness_id": NATIVE_HARNESS_ID}
                )
            events = res.get("tool_events") or []
            if events:
                lines = [f"• {e['tool']} ({e['status']}): {e.get('arguments', {}).get('path', '.')}" for e in events]
                self._append_chat("Workspace Tools", "\n".join(lines))
            self._append_chat(f"Workspace Agent ({res.get('model')})", res.get("reply", ""))
            if any(e.get("status") == "executed" and e.get("tool") in MUTATING_TOOLS for e in events):
                self.swarm_runner.workspace_tracker.scan_delta(force=True)
            result = {**res, "harness_id": NATIVE_HARNESS_ID, "harness_revision": revision}
            if cancel_event is None:
                result["workspace"] = self.get_workspace_status()
            return result
        except GeminiCopilotError as exc:
            self._append_chat("Workspace Agent Error", str(exc))
            return {"ok": False, "error": str(exc), "code": exc.code}
        except Exception as exc:
            self._append_chat("Workspace Agent Error", str(exc))
            return {"ok": False, "error": str(exc)}

    def decide_workspace_action(
        self,
        action_id: str,
        approve: bool,
        accepted_hunk_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            with self._workspace_agent_lock:
                if self.workspace_agent is None:
                    return {"ok": False, "error": "No active workspace agent."}
                res = self.workspace_agent.decide_action(
                    action_id,
                    approve=bool(approve),
                    accepted_hunk_ids=accepted_hunk_ids,
                )
            for hev in res.get("harness_events") or []:
                self.swarm_runner.record_harness_event(
                    {**hev, "harness_id": NATIVE_HARNESS_ID}
                )
            if res.get("ok"):
                self._append_chat("Workspace Agent", res.get("reply", "Action resolved."))
            self.swarm_runner.workspace_tracker.scan_delta(force=True)
            return {**res, "workspace": self.get_workspace_status()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def create_workspace_checkpoint(self, label: str = "manual") -> dict[str, Any]:
        if self.workspace_sandbox is None:
            return {"ok": False, "error": "No active project workspace."}
        try:
            cp = self.workspace_sandbox.create_checkpoint(label=label)
            self.swarm_runner.record_harness_event(
                {
                    "kind": "checkpoint",
                    "harness_id": self.harness_registry.active_harness_id,
                    "actor": "Workspace Checkpoint",
                    "summary": f"Created checkpoint {cp['id']} ({cp['label']} · {cp['file_count']} files)",
                    "payload": cp,
                }
            )
            return {"ok": True, "checkpoint": cp, "workspace": self.get_workspace_status()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def rollback_workspace_checkpoint(self, checkpoint_id: str) -> dict[str, Any]:
        if self.workspace_sandbox is None:
            return {"ok": False, "error": "No active project workspace."}
        try:
            rb = self.workspace_sandbox.rollback_checkpoint(checkpoint_id)
            self.swarm_runner.record_harness_event(
                {
                    "kind": "checkpoint",
                    "harness_id": self.harness_registry.active_harness_id,
                    "actor": "Workspace Rollback",
                    "summary": f"Rolled back to {checkpoint_id} ({len(rb.get('restored_files', []))} restored, {len(rb.get('removed_files', []))} removed)",
                    "payload": rb,
                }
            )
            return {"ok": True, "rollback": rb, "workspace": self.get_workspace_status()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def run_workspace_diagnostics(
        self,
        rel_path: str = ".",
        checks: list[str] | None = None,
    ) -> dict[str, Any]:
        if self.workspace_sandbox is None:
            return {"ok": False, "error": "No active project workspace."}
        try:
            diag = self.workspace_sandbox.execute_tool(
                "run_diagnostics",
                {"path": rel_path or ".", "checks": checks or ["syntax", "security"]},
            )
            self.swarm_runner.record_harness_event(
                {
                    "kind": "diagnostic",
                    "harness_id": self.harness_registry.active_harness_id,
                    "actor": "Workspace Diagnostics",
                    "summary": f"Diagnostics ({', '.join(diag.get('checks_run', []))}): {'PASS' if diag.get('passed') else 'FAIL'} ({diag.get('error_count', 0)} errors, {diag.get('warning_count', 0)} warnings)",
                    "payload": diag,
                }
            )
            return {"ok": True, **diag, "workspace": self.get_workspace_status()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def get_harness_collective(self) -> dict[str, Any]:
        return self.harness_registry.list_harnesses()

    def get_harness_updates(self) -> dict[str, Any]:
        return self._harness_update_response()

    def propose_harness_update(
        self, base_revision: str, instructions: str, max_turns: int, reason: str, json_handoff: bool | None = None
    ) -> dict[str, Any]:
        return self._harness_update_response(
            self.harness_updates.propose, base_revision, instructions, max_turns, reason, json_handoff
        )

    def probe_harness_update(self, revision: str) -> dict[str, Any]:
        return self._harness_update_response(self.harness_updates.probe, revision)

    def activate_harness_update(self, revision: str) -> dict[str, Any]:
        return self._harness_update_response(self.harness_updates.activate, revision)

    def rollback_harness_update(self, expected_revision: str) -> dict[str, Any]:
        return self._harness_update_response(self.harness_updates.rollback, expected_revision)

    def _harness_update_response(self, operation=None, *args: Any) -> dict[str, Any]:
        try:
            result = operation(*args) if operation is not None else {}
            return {"ok": True, **result, "updates": self.harness_updates.status()}
        except (ValueError, OSError, sqlite3.Error) as exc:
            return {"ok": False, "error": str(exc)}

    def set_active_harness(self, harness_id: str) -> dict[str, Any]:
        try:
            res = self.harness_registry.set_active_harness(harness_id)
            self.swarm_runner.record_harness_event(
                {
                    "kind": "telegraphic_note",
                    "harness_id": res["active_harness_id"],
                    "actor": "Harness Collective",
                    "summary": f"Switched active harness to {res['harness']['name']} ({res['active_harness_id']})",
                }
            )
            return {**res, "collective": self.harness_registry.list_harnesses()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def set_role_harness(self, role: str, harness_id: str) -> dict[str, Any]:
        try:
            res = self.harness_registry.set_role_harness(role, harness_id)
            return {**res, "collective": self.harness_registry.list_harnesses()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def run_harness_turn(
        self,
        task: str,
        harness_id: str = "",
        policy_profile: str = "",
    ) -> dict[str, Any]:
        ws_root = (
            self.workspace_sandbox.root
            if self.workspace_sandbox is not None
            else (SABRINA_WORKSPACE if SABRINA_WORKSPACE.is_dir() else ROOT.parent)
        )
        eff_profile = policy_profile or self.workspace_policy_profile
        policy = HarnessPolicy(
            profile=eff_profile if eff_profile in VALID_POLICY_PROFILES else "sandboxed_edit",
            workspace_mode=self.workspace_mode,
            allow_local_venv_fallback=self.allow_local_venv_fallback,
        )
        try:
            res = self.harness_registry.run_harness_turn(
                task=task,
                workspace_root=ws_root,
                harness_id=harness_id or None,
                policy=policy,
            )
            for ev in res.get("events") or []:
                self.swarm_runner.record_harness_event(ev)
            self.swarm_runner.workspace_tracker.scan_delta(force=True)
            return {**res, "collective": self.harness_registry.list_harnesses()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    # ------------------------------------------------------------------
    # Dependencies & Local Artifact Repository Endpoints
    # ------------------------------------------------------------------

    def get_dependencies_status(self) -> dict[str, Any]:
        """Return status of uv, local wheelhouse, and installed packages for the dependencies tab."""
        try:
            ws_root = (
                self.workspace_sandbox.root
                if self.workspace_sandbox is not None
                else (SABRINA_WORKSPACE if SABRINA_WORKSPACE.is_dir() else ROOT.parent)
            )
            from swarm_console.workspace_sandbox import WorkspaceSandbox
            sb = WorkspaceSandbox(
                ws_root,
                allow_local_venv_fallback=True,
                policy_profile=self.workspace_policy_profile if self.workspace_policy_profile in VALID_POLICY_PROFILES else "sandboxed_edit",
            )
            cache_info = sb.execute_tool("manage_dependencies", {"action": "cache_info"})
            list_info = sb.execute_tool("manage_dependencies", {"action": "list"})

            packages = []
            if list_info.get("ok"):
                raw_out = list_info.get("output", "")
                for line in raw_out.splitlines():
                    parts = line.strip().split()
                    if len(parts) >= 2 and not parts[0].startswith("-") and parts[0].lower() != "package":
                        packages.append({"name": parts[0], "version": parts[1]})

            active_profile = self.harness_updates.active_profile()

            return {
                "ok": True,
                "uv_available": cache_info.get("uv_available", False),
                "backend": cache_info.get("backend", "pip"),
                "wheelhouse_dir": cache_info.get("wheelhouse_dir", ""),
                "cached_wheels_count": cache_info.get("cached_wheels_count", 0),
                "cached_wheels": cache_info.get("cached_wheels", []),
                "cache_dir": cache_info.get("cache_dir", ""),
                "installed_packages": packages,
                "installed_count": len(packages),
                "active_revision": active_profile.revision,
                "instructions_summary": active_profile.reason,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def install_dependency_package(self, packages: list[str], wheelhouse_only: bool = False) -> dict[str, Any]:
        """Install one or more packages using the sandbox dependency manager."""
        try:
            ws_root = (
                self.workspace_sandbox.root
                if self.workspace_sandbox is not None
                else (SABRINA_WORKSPACE if SABRINA_WORKSPACE.is_dir() else ROOT.parent)
            )
            from swarm_console.workspace_sandbox import WorkspaceSandbox
            sb = WorkspaceSandbox(
                ws_root,
                allow_local_venv_fallback=True,
                policy_profile="sandboxed_edit",
            )
            res = sb.execute_tool(
                "manage_dependencies",
                {"action": "install", "packages": packages, "wheelhouse_only": bool(wheelhouse_only)},
            )
            return res
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def cache_dependency_wheels(self, packages: list[str]) -> dict[str, Any]:
        """Download wheels to local wheelhouse for offline access."""
        try:
            ws_root = (
                self.workspace_sandbox.root
                if self.workspace_sandbox is not None
                else (SABRINA_WORKSPACE if SABRINA_WORKSPACE.is_dir() else ROOT.parent)
            )
            from swarm_console.workspace_sandbox import WorkspaceSandbox
            sb = WorkspaceSandbox(
                ws_root,
                allow_local_venv_fallback=True,
                policy_profile="sandboxed_edit",
            )
            res = sb.execute_tool(
                "manage_dependencies",
                {"action": "cache_wheel", "packages": packages},
            )
            return res
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def compare_harnesses(
        self,
        task: str,
        harness_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        ws_root = (
            self.workspace_sandbox.root
            if self.workspace_sandbox is not None
            else (SABRINA_WORKSPACE if SABRINA_WORKSPACE.is_dir() else ROOT.parent)
        )
        try:
            res = self.harness_registry.compare_harnesses(
                task=task,
                workspace_root=ws_root,
                harness_ids=harness_ids,
            )
            self.swarm_runner.record_harness_event(
                {
                    "kind": "telegraphic_note",
                    "harness_id": self.harness_registry.active_harness_id,
                    "actor": "Harness Benchmark",
                    "summary": f"Compared {len(res.get('comparisons') or [])} harnesses on '{task[:80]}'",
                    "payload": res,
                }
            )
            return {**res, "collective": self.harness_registry.list_harnesses()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}

    # ------------------------------------------------------------------
    # Evo-Bench Self-Analytical Evolution Endpoints
    # ------------------------------------------------------------------

    def get_evo_bench_state(self) -> dict[str, Any]:
        try:
            from sabrina_console_desktop.swarm_evo_bench import EvoBenchDatabase
        except ImportError:
            from swarm_evo_bench import EvoBenchDatabase  # type: ignore[no-redef]
        state = dict(getattr(self, "_evo_bench_state", None) or {
            "running": False,
            "run_id": "",
            "step": "idle",
            "detail": "Ready to run self-analytical round (Option A: execution_authorized=False).",
            "progress_pct": 0,
            "latest_summary": None,
        })
        try:
            db_kw = {"db_path": self._evo_bench_db_path} if getattr(self, "_evo_bench_db_path", None) else {}
            db = EvoBenchDatabase(**db_kw)
            runs = db.get_recent_runs(limit=8)
            state["recent_runs"] = runs
            if state.get("latest_summary") is None and runs:
                state["latest_summary"] = runs[0]
        except Exception as exc:
            state["recent_runs"] = []
            state["db_error"] = str(exc)
        state["ok"] = True
        state["execution_authorized"] = False
        return state

    def _get_evo_bench_snapshot_light(self) -> dict[str, Any]:
        now = time.monotonic()
        cur = dict(getattr(self, "_evo_bench_state", None) or {
            "running": False,
            "run_id": "",
            "step": "idle",
            "detail": "Ready (click ⚡ Test Limits & Adapt or Launch Mission).",
            "progress_pct": 0,
            "latest_summary": None,
            "generations": [],
            "rejected_adaptations": [],
        })
        cached_at = float(getattr(self, "_evo_bench_db_cached_at", 0.0) or 0.0)
        if not cur.get("running") and (now - cached_at) < 4.0 and getattr(self, "_evo_bench_cached_runs", None) is not None:
            runs = getattr(self, "_evo_bench_cached_runs", [])
            cur["recent_runs"] = runs
            if cur.get("latest_summary") is None and runs:
                cur["latest_summary"] = runs[0]
            cur["ok"] = True
            cur["execution_authorized"] = False
            return cur
        st = self.get_evo_bench_state()
        self._evo_bench_db_cached_at = now
        self._evo_bench_cached_runs = st.get("recent_runs", [])
        return st

    def stop_evo_bench_round(self) -> dict[str, Any]:
        ev_cancel = getattr(self, "_evo_bench_cancel_event", None)
        if ev_cancel is not None:
            ev_cancel.set()
        self.swarm_runner.stop_mission(None)
        st = dict(getattr(self, "_evo_bench_state", None) or {})
        st.update({
            "running": False,
            "step": "stopped",
            "detail": "Evo-Bench stopped by operator.",
        })
        self._evo_bench_state = st
        return {"ok": True, "message": "Stopped Evo-Bench round and active benchmark workers.", "execution_authorized": False}

    def start_evo_bench_round(
        self,
        generations: int = 2,
        transfer_model: str = "ollama/gemma3:12b",
        token_budget: int = 24000,
        max_calls_per_task: int = 12,
    ) -> dict[str, Any]:
        cur = getattr(self, "_evo_bench_state", None) or {}
        if cur.get("running"):
            return {"ok": False, "error": "An Evo-Bench round is already running.", "execution_authorized": False}
        try:
            from sabrina_console_desktop.swarm_evo_bench import SwarmEvoBenchmarkRunner
        except ImportError:
            from swarm_evo_bench import SwarmEvoBenchmarkRunner  # type: ignore[no-redef]

        eff_gens = max(1, min(8, int(generations or 2)))
        eff_transfer = str(transfer_model or "ollama/gemma3:12b").strip()
        eff_budget = max(2000, int(token_budget or 24000))
        eff_calls = max(4, min(24, int(max_calls_per_task or 12)))
        cancel_ev = threading.Event()
        self._evo_bench_cancel_event = cancel_ev

        self._evo_bench_state = {
            "running": True,
            "run_id": "starting",
            "step": "starting",
            "detail": f"Starting {eff_gens}-generation live limit benchmark on Swarm Monitor (transfer={eff_transfer})…",
            "progress_pct": 2,
            "generations": [],
            "rejected_adaptations": [],
            "active_scaffold": None,
            "latest_summary": cur.get("latest_summary"),
        }

        def _on_progress(p: dict[str, Any]) -> None:
            st = dict(getattr(self, "_evo_bench_state", None) or {})
            st.update({
                "run_id": p.get("run_id", st.get("run_id", "")),
                "step": p.get("step", "running"),
                "detail": p.get("detail", ""),
                "progress_pct": int(p.get("progress_pct") or 0),
                "generations": p.get("generations", st.get("generations", [])),
                "rejected_adaptations": p.get("rejected_adaptations", st.get("rejected_adaptations", [])),
                "active_scaffold": p.get("active_scaffold", st.get("active_scaffold")),
            })
            self._evo_bench_state = st

        def _worker() -> None:
            try:
                db_kw = {"db_path": self._evo_bench_db_path} if getattr(self, "_evo_bench_db_path", None) else {}
                bench = SwarmEvoBenchmarkRunner(
                    llm_caller=self._call_swarm_llm,
                    max_llm_calls_per_task=eff_calls,
                    max_tokens_per_round=eff_budget,
                    progress_cb=_on_progress,
                    cancel_event=cancel_ev,
                    **db_kw,
                )
                summary = bench.run_evolution_round(
                    generations=eff_gens,
                    transfer_model=eff_transfer,
                )
                self._evo_bench_db_cached_at = 0.0
                self._evo_bench_state = {
                    "running": False,
                    "run_id": summary["run_id"],
                    "step": "done",
                    "detail": f"Completed {summary['run_id']}: Δ Held-Out={summary['net_capability_gain']*100:+.1f}%, Promoted={summary['promoted_hash']}",
                    "progress_pct": 100,
                    "generations": summary.get("generations", []),
                    "rejected_adaptations": summary.get("rejected_adaptations", []),
                    "active_scaffold": summary.get("promoted_scaffold"),
                    "latest_summary": summary,
                }
            except Exception as exc:
                is_stop = cancel_ev.is_set() or "cancelled" in str(exc).lower()
                self._evo_bench_state = {
                    "running": False,
                    "run_id": "",
                    "step": "stopped" if is_stop else "error",
                    "detail": "Stopped by operator." if is_stop else f"Evo-Bench error: {exc}",
                    "progress_pct": 0,
                    "generations": (getattr(self, "_evo_bench_state", None) or {}).get("generations", []),
                    "rejected_adaptations": (getattr(self, "_evo_bench_state", None) or {}).get("rejected_adaptations", []),
                    "latest_summary": cur.get("latest_summary"),
                }

        threading.Thread(target=_worker, name="sabrina-evo-bench", daemon=True).start()
        return {"ok": True, "message": f"Started {eff_gens}-generation live limit benchmark on Swarm Monitor.", "execution_authorized": False}

    def apply_promoted_scaffold(self, run_id: str = "") -> dict[str, Any]:
        try:
            from sabrina_console_desktop.swarm_evo_bench import EvoBenchDatabase, SwarmScaffold
            from sabrina_console_desktop.recovery_policy import DEFAULT_PATH as REC_DEFAULT_PATH
        except ImportError:
            from swarm_evo_bench import EvoBenchDatabase, SwarmScaffold  # type: ignore[no-redef]
            from recovery_policy import DEFAULT_PATH as REC_DEFAULT_PATH  # type: ignore[no-redef]
        try:
            db_kw = {"db_path": self._evo_bench_db_path} if getattr(self, "_evo_bench_db_path", None) else {}
            pol_dest = getattr(self, "_evo_bench_policy_path", None) or REC_DEFAULT_PATH
            db = EvoBenchDatabase(**db_kw)
            runs = db.get_recent_runs(limit=10)
            target = next((r for r in runs if not run_id or r.get("run_id") == run_id), None)
            if not target:
                return {"ok": False, "error": "No completed Evo-Bench run found.", "execution_authorized": False}
            raw_scaf = json.loads(target["promoted_scaffold_json"]) if isinstance(target.get("promoted_scaffold_json"), str) else (target.get("promoted_scaffold") or {})
            scaffold = SwarmScaffold.from_dict(raw_scaf)
            scaffold.apply_to_runner(self.swarm_runner, policy_path=pol_dest)
            return {
                "ok": True,
                "run_id": target["run_id"],
                "promoted_hash": scaffold.scaffold_hash,
                "scaffold": scaffold.to_dict(),
                "message": f"Applied promoted scaffold Σ ({scaffold.scaffold_hash}) to live swarm & recovery_policy.json.",
                "execution_authorized": False,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "execution_authorized": False}


def run_desktop_ui() -> None:
    import datetime
    crash_log = ROOT / "console-launch.log"
    log_fp = None
    try:
        if FROZEN_DESKTOP:
            SABRINA_WORKSPACE.mkdir(parents=True, exist_ok=True)
        log_fp = open(crash_log, "a", encoding="utf-8", buffering=1)
        sys.stdout = log_fp
        sys.stderr = log_fp
        print(f"\n=======================================================")
        print(f"  Sabrina Console Session Started: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Python executable: {sys.executable}")
        print(f"  Workspace: {SABRINA_WORKSPACE}")
        print(f"=======================================================\n", flush=True)

        html_path = ROOT / "sabrina_desktop_ui.html"
        if not html_path.is_file():
            html_path = SABRINA_WORKSPACE / "sabrina_desktop_ui.html"
        if not html_path.is_file():
            raise FileNotFoundError(f"Console UI template not found at {html_path}")

        bridge = SabrinaDesktopBridge()
        if webview is not None:
            replay_id = sys.argv[2] if len(sys.argv) == 3 and sys.argv[1] == "--replay-release" else ""
            powershell_check = sys.argv[1:] == ["--powershell-check"]
            title = "Sabrina Operator Console"
            if replay_id:
                title += " — Release Review"
            elif powershell_check:
                title += " — PowerShell"
            win = webview.create_window(
                title,
                html=_read_desktop_html(html_path),
                js_api=bridge,
                width=1280,
                height=820,
                min_size=(960, 640),
                background_color="#0b0e14",
                text_select=True,
            )
            bridge._window = win
            if replay_id:
                result = bridge.launch_release_review(replay_id)
                if not result.get("ok"):
                    raise RuntimeError(f"Release review could not start: {result.get('error')}")
                print(f"[RELEASE] Replaying {replay_id} as {result['mission_id']}", flush=True)
            if powershell_check:
                fixture = tempfile.TemporaryDirectory(prefix="SabrinaPowerShell-")
                fixture_root = Path(fixture.name)
                target = "PowerShell [check].txt"
                (fixture_root / target).write_bytes(b"Sabrina PowerShell mission.\n")
                result = bridge.swarm_runner.launch_mission(
                    "Use all three Windows PowerShell tools to check the installed engine version and edition, "
                    f"Get-Item support for LiteralPath, and the byte length of the workspace file {target!r}. "
                    "Report the three live results with execution_authorized=False.",
                    mode=WINDOWS_POWERSHELL_MODE,
                    windows_workspace=fixture_root,
                )
                if not result.get("ok"):
                    raise RuntimeError(f"PowerShell check could not start: {result.get('error')}")
                print(f"[POWERSHELL] Started {result['mission_id']} for {target}", flush=True)
            print(f"[INIT] Window created successfully. Starting pywebview loop...", flush=True)
            webview.start(debug=not FROZEN_DESKTOP)
            print(f"[EXIT] Sabrina Console window closed cleanly at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
        else:
            raise RuntimeError(
                "pywebview module is not available in the active Python environment. "
                "Run: pip install pywebview"
            )
    except Exception as exc:
        import traceback
        try:
            if log_fp:
                log_fp.write(f"\n[CRASH {datetime.datetime.now().isoformat()}] {exc}\n")
                traceback.print_exc(file=log_fp)
                log_fp.flush()
        except Exception:
            pass
        try:
            from tkinter import messagebox
            import tkinter as tk
            root_tk = tk.Tk()
            root_tk.withdraw()
            messagebox.showerror(
                "Sabrina Operator Console Error",
                f"Failed to start Sabrina Operator Console:\n\n{exc}\n\nSee log: {crash_log}",
            )
            root_tk.destroy()
        except Exception:
            pass
        raise
    finally:
        if log_fp and not log_fp.closed:
            try:
                log_fp.close()
            except Exception:
                pass


if __name__ == "__main__":
    if sys.argv[1:2] == ["--followup-coordinator"] and len(sys.argv) == 3:
        from followup_coordinator import run_worker
        run_worker(sys.argv[2])
    else:
        run_desktop_ui()

