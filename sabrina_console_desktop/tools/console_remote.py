"""Connect restored desktop views to an explicitly configured server authority."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable


def call_console_backend(
    setting: str,
    path: str,
    local_call: Callable[[], Any],
    payload: dict[str, Any] | None = None,
    *,
    timeout: float | None = None,
) -> Any:
    base = os.environ.get(setting, "").strip().rstrip("/")
    if not base:
        return local_call()
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base + path,
        data=body,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    if timeout is None:
        timeout = 180 if payload is not None else 15
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def run_console_chat(
    message: str,
    mode: str,
    target: str,
    operation_id: str,
    progress: Callable[[str], None],
    local_call: Callable[[], Any],
    *,
    context: dict[str, Any] | None = None,
) -> Any:
    if not os.environ.get("SABRINA_SWARM_API_BASE", "").strip():
        return local_call()
    if context:
        from tools.swarm_chat_portal import format_structured_as_markdown
        supplied_context = format_structured_as_markdown(context, title="Supplied Context")
        message = f"{message.strip()}\n\n{supplied_context}".strip()

    deadline = time.monotonic() + 1800
    receipt_path = "/api/swarm-chat/jobs/" + operation_id
    progress(f"Submitting job {operation_id}")
    receipt = None
    try:
        receipt = call_console_backend(
            "SABRINA_SWARM_API_BASE", "/api/swarm-chat/jobs", local_call,
            {"operation_id": operation_id, "message": message, "mode": mode, "target": target},
            timeout=15,
        )
    except urllib.error.HTTPError as exc:
        if 400 <= exc.code < 500:
            raise RuntimeError(f"Job {operation_id} was rejected (HTTP {exc.code}). No resubmission was made.") from exc
        progress(f"Job {operation_id}: submission outcome uncertain; checking its receipt")
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        progress(f"Job {operation_id}: submission outcome uncertain; checking its receipt")

    while time.monotonic() < deadline:
        if receipt is not None:
            if receipt.get("id") != operation_id:
                raise RuntimeError(f"Unexpected receipt for job {operation_id}; review the server receipt.")
            state = receipt.get("state")
            progress(f"Job {operation_id} · {state}: {receipt.get('progress', '')}")
            if state == "complete":
                if not isinstance(receipt.get("result"), dict):
                    raise RuntimeError(f"Job {operation_id} completed without a usable result; review its receipt.")
                return receipt["result"]
            if state in {"failed", "interrupted"}:
                raise RuntimeError(f"Job {operation_id} {state}: {receipt.get('progress', '')}. No resubmission was made.")
            if state not in {"running", "queued"}:
                raise RuntimeError(f"Job {operation_id} has unknown state {state!r}; review its receipt.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            receipt = call_console_backend(
                "SABRINA_SWARM_API_BASE", receipt_path, local_call,
                timeout=min(15, remaining),
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise RuntimeError(f"Job {operation_id} has no server receipt. Its submission outcome needs review. No resubmission was made.") from exc
            if 400 <= exc.code < 500:
                raise RuntimeError(f"Cannot read job {operation_id} (HTTP {exc.code}); review its receipt.") from exc
            receipt = None
            progress(f"Job {operation_id}: receipt temporarily unavailable; continuing to check")
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            receipt = None
            progress(f"Job {operation_id}: receipt temporarily unavailable; continuing to check")
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"Stopped waiting after 1800 seconds for job {operation_id}. It may still be running. Review its receipt before sending again.")
