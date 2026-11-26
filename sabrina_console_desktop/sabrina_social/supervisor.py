"""Bounded recovery supervisor for the social pulse.

This layer may repair connectivity and retry a pulse, but it never creates a
new browser profile or attempts to authenticate an account.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.request
import urllib.error
import sys
from urllib.parse import urlsplit
from pathlib import Path
from typing import Callable

from .recovery import RecoveryJournal


def foreman_next_step(objective: str, health: dict, capabilities: list[dict] | None = None) -> dict:
    """Choose one verified, allowlisted next step for a long-horizon objective.

    This is deliberately deterministic: the model may propose an objective, but
    infrastructure state decides whether recovery, waiting, or execution is safe.
    """
    capabilities = capabilities or []
    bridge = health.get("bridge", {})
    scheduler = health.get("scheduler", {})
    supervisor = health.get("supervisor", {})
    if not supervisor.get("heartbeat_fresh", False):
        return {"state": "blocked", "reason": "supervisor_heartbeat_stale", "next": "inspect_supervisor"}
    if bridge.get("status") != "bridge_ready":
        ready = any(c.get("capability") == "browser.recovery" and c.get("availability") == "ready" for c in capabilities)
        return {"state": "recovering" if ready else "blocked", "reason": "browser_bridge_unavailable",
                "next": "browser.recovery" if ready else "request_operator"}
    if not scheduler.get("active", False):
        return {"state": "blocked", "reason": "scheduler_inactive", "next": "restart_scheduler"}
    return {"state": "ready", "objective": objective, "next": "dispatch_registered_capability"}


def diagnose_browser_state(cdp: dict, tabs: list[dict] | None = None) -> dict:
    """Classify a browser failure and select the next safe capability.

    Browser chrome such as Restore pages is not page DOM, so this uses the CDP
    response plus tab URLs/titles. The result is a routing decision, not an
    instruction to bypass authentication.
    """
    if not cdp.get("ready"):
        return {"state": "tunnel_or_browser_down", "capability": "browser.recovery",
                "action": "reconnect", "safe_to_retry": False}
    tabs = tabs or []
    text = " ".join(f"{t.get('title','')} {t.get('url','')}" for t in tabs).lower()
    if "restore pages" in text or "session restore" in text or "chrome://session-restore" in text:
        return {"state": "session_restore_ui", "capability": "browser.recovery",
                "action": "restore_existing_session", "safe_to_retry": False,
                "requires": "operator_confirm_existing_profile"}
    linkedin = [urlsplit(str(t.get("url", ""))) for t in tabs]
    linkedin = [u for u in linkedin if u.hostname == "linkedin.com" or (u.hostname or "").endswith(".linkedin.com")]
    if any("checkpoint" in u.path.lower() for u in linkedin):
        return {"state": "linkedin_checkpoint", "capability": "linkedin.browser",
                "action": "halt_and_request_human", "safe_to_retry": False}
    if any(u.path.startswith(("/login", "/uas/login", "/authwall")) for u in linkedin):
        return {"state": "linkedin_login", "capability": "browser.recovery",
                "action": "request_authentication", "safe_to_retry": False}
    if linkedin:
        return {"state": "linkedin_ready", "capability": "linkedin.browser",
                "action": "resume_pulse", "safe_to_retry": True}
    return {"state": "browser_ready_unknown_page", "capability": "browser.recovery",
            "action": "inspect_tabs", "safe_to_retry": False}


def cdp_ready(url: str, opener: Callable = urllib.request.urlopen) -> dict:
    try:
        with opener(url.rstrip("/") + "/json/version", timeout=4) as response:
            value = json.loads(response.read().decode("utf-8"))
        if not value.get("webSocketDebuggerUrl"):
            return {"ready": False, "reason": "CDP response has no debugger URL"}
        return {"ready": True, "browser": value.get("Browser", "unknown")}
    except Exception as exc:
        return {"ready": False, "reason": f"CDP unavailable: {exc}"}


def request_bridge_recovery(url: str, token: str, opener=urllib.request.urlopen) -> dict:
    request = urllib.request.Request(url.rstrip("/") + "/recover", method="POST",
        headers={"Authorization": "Bearer " + token})
    try:
        with opener(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"status": "recovery_unauthorized" if exc.code in (401, 403) else "recovery_failed"}
    except Exception as exc:
        return {"status": "recovery_unreachable", "error_type": type(exc).__name__}


def bridge_health(url: str) -> dict:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=3) as response:
            value = json.load(response)
        if not isinstance(value, dict) or value.get("service") != "sabrina-bridge":
            return {"status": "unexpected_recovery_endpoint", "reachable": False}
        return {**value, "reachable": True}
    except Exception:
        return {"status": "recovery_transport_down", "reachable": False}


def verify_linkedin(cdp_url: str) -> dict:
    """A bounded read-only worker performs the existing DOM authentication check."""
    # Fast path: an existing non-login LinkedIn page is sufficient to prove
    # the shared browser resource is present; avoid blocking on a coordinator
    # lease before the pulse can start.
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/list", timeout=5) as response:
            tabs = json.loads(response.read().decode("utf-8"))
        linkedin = [t for t in tabs if "linkedin.com" in str(t.get("url", "")).lower()]
        blocked = any(any(marker in str(t.get("url", "")).lower()
                          for marker in ("/login", "/authwall", "checkpoint")) for t in linkedin)
        if linkedin and not blocked:
            return {"status": "ready", "authenticated": True,
                    "auth_detail": "existing_linkedin_tab_verified_via_cdp"}
    except Exception:
        pass
    try:
        result = subprocess.run([sys.executable, "-m", "sabrina_social.supervisor",
                                 "--check-auth", "--cdp-url", cdp_url],
                                capture_output=True, text=True, timeout=60, check=False)
        if result.returncode == 0:
            value = json.loads(result.stdout.strip().splitlines()[-1])
            if isinstance(value, dict) and value.get("status") != "authentication_check_unavailable":
                return value
    except Exception:
        pass
    # Fallback for a healthy shared CDP session whose coordinator lease or
    # browser manager is slow: inspect only the existing tab URLs. This never
    # logs in or changes state, but prevents a healthy LinkedIn tab from being
    # misclassified as an unavailable authentication check.
    try:
        with urllib.request.urlopen(cdp_url.rstrip("/") + "/json/list", timeout=5) as response:
            tabs = json.loads(response.read().decode("utf-8"))
        linkedin = [t for t in tabs if "linkedin.com" in str(t.get("url", "")).lower()]
        blocked = any(any(marker in str(t.get("url", "")).lower()
                          for marker in ("/login", "/authwall", "checkpoint")) for t in linkedin)
        if linkedin and not blocked:
            return {"status": "ready", "authenticated": True,
                    "auth_detail": "existing_linkedin_tab_verified_via_cdp"}
    except Exception:
        pass
    return {"status": "authentication_check_unavailable", "authenticated": False}


def navigate_authenticated_blank_page(manager, status: dict) -> dict:
    """Use the existing blank CDP tab for read-only LinkedIn preflight instead of leaving it blank."""
    result = dict(status)
    if not result.get("linkedin_authenticated") or result.get("current_url") != "about:blank":
        return result
    page = manager.browser_manager.page or manager._ensure_browser_page()
    navigation = manager.stealth.safe_navigate(page, "https://www.linkedin.com/feed/")
    if navigation.get("status") != "success":
        result.update({"status": "linkedin_navigation_failed", "linkedin_authenticated": False,
                       "auth_detail": navigation.get("error", "LinkedIn feed navigation failed")})
        return result
    authenticated, detail = manager.stealth.is_logged_in(page)
    current_url = page.url
    parsed = urlsplit(current_url)
    redirected_to_login = (parsed.hostname in {"linkedin.com", "www.linkedin.com"} and
                           any(marker in parsed.path.lower() for marker in ("/login", "/authwall", "/checkpoint")))
    if redirected_to_login:
        authenticated = False
        detail = "LinkedIn redirected the browser to an authentication or checkpoint page."
    result.update({"current_url": current_url, "linkedin_authenticated": bool(authenticated),
                   "auth_detail": detail})
    if not authenticated:
        result["status"] = "needs_user_authentication" if redirected_to_login else "authentication_check_unavailable"
    return result


def check_linkedin_auth(runtime) -> dict:
    """Check the existing session and return a simple, explicit preflight result."""
    value = navigate_authenticated_blank_page(runtime.manager, runtime.connection_status())
    authenticated = bool(value.get("linkedin_authenticated"))
    if authenticated:
        status = "ready"
    elif value.get("status") in {"linkedin_navigation_failed", "needs_user_authentication",
                                  "authentication_check_unavailable"}:
        status = value["status"]
    else:
        status = "needs_user_authentication" if value.get("browser_connected") else "authentication_check_unavailable"
    return {"status": status, "authenticated": authenticated}


def prepare_browser(cdp_url, recovery_url, recovery_token, journal, wait_seconds=120,
                    probe=None, health=bridge_health, recover=request_bridge_recovery,
                    auth=verify_linkedin, sleep=time.sleep, monotonic=time.monotonic):
    """Adapt only among registered infrastructure paths before a pulse starts."""
    from tools.capability_registry import CapabilityRegistry
    registry = CapabilityRegistry(journal.path.with_name("capabilities.sqlite3"))
    probe = probe or cdp_ready
    deadline = monotonic() + max(0, wait_seconds)
    last = {}
    recovery_attempts = 0
    while True:
        cdp = probe(cdp_url)
        bridge = health(recovery_url) if recovery_url else {"reachable": False, "status": "recovery_not_configured"}
        last = {"cdp": cdp, "recovery": bridge}
        if cdp.get("ready") and bridge.get("reachable"):
            # Infrastructure is healthy; clear stale recovery exhaustion before
            # evaluating LinkedIn authentication so the dashboard reflects the
            # real blocker instead of an old bridge incident.
            journal.resolve("social-preflight")
            auth_result = auth(cdp_url)
            if auth_result.get("authenticated"):
                return {"status": "ready", **last}
            return {"status": auth_result.get("status", "authentication_check_unavailable"), **last}
        diagnosis = "recovery_transport_down" if not bridge.get("reachable") else "browser_down"
        # When the recovery endpoint is absent, the independent Windows watchdog
        # discovers and repairs its SSH dependency. Never repeatedly POST a dead tunnel.
        if bridge.get("reachable") and not cdp.get("ready") and recovery_url and recovery_token and recovery_attempts < 1:
            if registry.discover(capability="browser.recovery", action="reconnect"):
                receipt = journal.begin("social-preflight", "reconnect", diagnosis)
                if receipt["status"] == "approved":
                    recovery_attempts += 1
                    write_status(os.environ.get("SABRINA_SOCIAL_STATUS_FILE"),
                                 "recovering_browser", attempt=recovery_attempts,
                                 recovery_url=recovery_url, diagnosis=diagnosis)
                    result = recover(recovery_url, recovery_token)
                    journal.finish("social-preflight", receipt, result)
                    if result.get("status") in {"recovery_unauthorized", "unauthorized"}:
                        return {"status": "recovery_unauthorized", **last}
                elif receipt["status"] == "recovery_exhausted":
                    return {**last, **receipt}
        else:
            if journal.infrastructure and not bridge.get('reachable'):
                return {'status': 'recovering_infrastructure', 'subsystem': diagnosis, **last}
            # Waiting for the independent path also has a durable budget.
            receipt = journal.begin("social-preflight", "await_windows_watchdog", diagnosis)
            if receipt["status"] == "approved":
                journal.finish("social-preflight", receipt, {"status": diagnosis})
            elif receipt["status"] == "recovery_exhausted":
                return {**last, **receipt}
        if monotonic() >= deadline:
            return {"status": "recovering_infrastructure", "subsystem": diagnosis, **last}
        sleep(min(5, max(0, deadline - monotonic())))


def write_status(path: str | None, status: str, **details) -> None:
    from .availability_support import atomic_json, notify_systemd
    notify_systemd()
    if not path:
        return
    atomic_json(path, {"status": status, "updated": time.time(), **details})


def run_progress_worker(command, *, journal=None, ident=None, status_file=None):
    """Keep the supervisor responsive while the bounded pulse runs in its own worker."""
    from tools.worker_identity import executor_identity, process_start
    from .availability_support import notify_systemd
    deadline = time.monotonic() + 1200
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          env={**os.environ, 'SABRINA_SOCIAL_OCCURRENCE': 'pulse:' + str(ident)}) as child:
        identity = {**executor_identity('pulse:' + str(ident)), 'pid': child.pid,
                    'process_start': process_start(child.pid)}
        if journal and ident:
            journal.bind_pulse_executor(ident, identity)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, 1200)
                try:
                    out, err = child.communicate(timeout=min(20, remaining))
                    return subprocess.CompletedProcess(command, child.returncode, out, err)
                except subprocess.TimeoutExpired:
                    write_status(status_file, 'pulse_running', launch_id=ident,
                                 worker=identity, deadline_seconds=max(0, int(remaining)))
                    notify_systemd()
        except BaseException:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait(timeout=5)
            raise


def run_supervised_pulse(cdp_url: str, command: list[str], wait_seconds: int = 45,
                         poll_seconds: int = 5, run: Callable = subprocess.run,
                         sleep: Callable = time.sleep, recovery_url: str | None = None,
                         recovery_token: str | None = None, recover: Callable = request_bridge_recovery,
                         status_file: str | None = None, journal=None, prepare=None) -> dict:
    """Wait briefly for an existing browser, then run exactly one pulse.

    A missing browser returns ``infrastructure_unavailable`` and does not invoke
    the pulse. A pulse is never retried after it has started, preventing a
    duplicate like/comment after an uncertain browser outcome.
    """
    if prepare is not None:
        result = prepare()
        if result.get("status") != "ready":
            write_status(status_file, result["status"], result=result)
            return result
    deadline = time.monotonic() + max(0, int(wait_seconds))
    recovery = None
    write_status(status_file, "waiting_for_browser", cdp_url=cdp_url)
    if not cdp_ready(cdp_url)["ready"] and recovery_url and recovery_token:
        write_status(status_file, "recovering_browser", recovery_url=recovery_url)
        recovery = recover(recovery_url, recovery_token)
    checks = 0
    last = {"ready": False, "reason": "not checked"}
    while True:
        checks += 1
        last = cdp_ready(cdp_url)
        if last["ready"]:
            break
        if time.monotonic() >= deadline:
            return {"status": "infrastructure_unavailable", "checks": checks, "recovery": recovery,
                    "cdp": last, "action": "inspect_bridge"}
        sleep(max(0.1, min(poll_seconds, deadline - time.monotonic())))
    ident = journal.claim_pulse() if journal else None
    if journal and ident is None:
        result = {"status": "uncertain_previous_pulse", "action": "reconcile_operation_journal"}
        write_status(status_file, result["status"], **{k: v for k, v in result.items() if k != "status"})
        return result
    write_status(status_file, "pulse_running", launch_id=ident)
    try:
        if run is subprocess.run:
            completed = run_progress_worker(command, journal=journal, ident=ident, status_file=status_file)
        else:
            completed = run(command, capture_output=True, text=True, timeout=1200, check=False)
    except BaseException:
        if journal:
            journal.finish_pulse(ident, False)
        raise
    if journal:
        journal.finish_pulse(ident, completed.returncode == 0)
    write_status(status_file, "completed" if completed.returncode == 0 else "failed",
                 exit_code=completed.returncode, pulse_output=completed.stdout[-1000:])
    return {"status": "pulse_started", "checks": checks,
            "cdp": last, "exit_code": completed.returncode,
            "stdout": completed.stdout[-4000:], "stderr": completed.stderr[-2000:]}


def run_continuous(cdp_url: str, command: list[str], interval_seconds: int = 180,
                   status_file: str | None = None, max_cycles: int = 0,
                   run_pulse: Callable = run_supervised_pulse,
                   sleep: Callable = time.sleep) -> int:
    """Keep the supervisor alive; each cycle remains independently bounded."""
    # Preserve an existing scheduled wait when systemd restarts this process.
    if status_file and Path(status_file).exists():
        previous = json.loads(Path(status_file).read_text(encoding="utf-8"))
        if previous.get("status") == "waiting":
            due = previous.get("updated", 0) + previous.get("next_cycle_seconds", 0)
            remaining = max(0, min(int(interval_seconds), due - time.time()))
            while remaining > 0:
                write_status(status_file, "waiting", next_cycle_seconds=remaining,
                             last_result=previous.get("last_result", {}))
                delay = min(30, remaining)
                sleep(delay)
                remaining -= delay
    cycle = 0
    while max_cycles <= 0 or cycle < max_cycles:
        cycle += 1
        result = {"status": "supervisor_error"}
        write_status(status_file, "starting_cycle", cycle=cycle)
        try:
            result = run_pulse(cdp_url, command, status_file=status_file)
            write_status(status_file, result.get("status", "cycle_finished"), cycle=cycle,
                         result={k: result.get(k) for k in ("exit_code", "recovery", "cdp")})
        except Exception as exc:
            write_status(status_file, "supervisor_error", cycle=cycle, error=str(exc))
        if max_cycles > 0 and cycle >= max_cycles:
            break
        remaining = 30 if result.get("status") == "recovering_infrastructure" else max(30, int(interval_seconds))
        while remaining > 0:
            write_status(status_file, "waiting", cycle=cycle, next_cycle_seconds=remaining,
                         last_result={k: result.get(k) for k in ("status", "subsystem", "exit_code", "action")})
            delay = min(30, remaining)
            sleep(delay)
            remaining -= delay
    return cycle


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one recoverable Sabrina social pulse")
    parser.add_argument("--cdp-url", default=os.environ.get("SABRINA_SOCIAL_CDP_URL", "http://127.0.0.1:19224"))
    parser.add_argument("--wait-seconds", type=int, default=45)
    parser.add_argument("--continuous", action="store_true")
    parser.add_argument("--interval-seconds", type=int, default=int(os.environ.get("SABRINA_SOCIAL_PULSE_INTERVAL", "180")))
    parser.add_argument("--check-auth", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if args.check_auth:
        from .runtime import SocialRuntime
        from .recovery import recovery_lease
        runtime = SocialRuntime(cdp_url=args.cdp_url)
        try:
            with recovery_lease():
                print(json.dumps(check_linkedin_auth(runtime)))
        finally:
            runtime.disconnect_browser()
        return
    journal = RecoveryJournal(Path(os.environ.get("SABRINA_SOCIAL_DATA_DIR", "data/social")) / "recovery.sqlite3", infrastructure=True)
    from .availability_support import notify_systemd
    notify_systemd('READY=1\nWATCHDOG=1')
    command = ["/usr/bin/python3", "-m", "sabrina_social.pulse", "--live", "--max-pulses", "1", "--max-posts", "4"]
    token = os.environ.get("SABRINA_BRIDGE_TOKEN") or (open(os.environ["SABRINA_BRIDGE_TOKEN_FILE"], encoding="utf-8").read().strip() if os.environ.get("SABRINA_BRIDGE_TOKEN_FILE") else None)
    def pulse_runner(cdp, cmd, status_file=None):
        def prepare():
            return prepare_browser(cdp, os.environ.get("SABRINA_BRIDGE_RECOVERY_URL"), token,
                                   journal, wait_seconds=args.wait_seconds)
        if args.preflight_only:
            return prepare()
        return run_supervised_pulse(cdp, cmd, args.wait_seconds,
            status_file=status_file, journal=journal, prepare=prepare)
    if args.continuous and not args.preflight_only:
        run_continuous(args.cdp_url, command, args.interval_seconds,
            os.environ.get("SABRINA_SOCIAL_STATUS_FILE"), run_pulse=pulse_runner)
        return
    print(json.dumps(pulse_runner(args.cdp_url, command,
        status_file=os.environ.get("SABRINA_SOCIAL_STATUS_FILE")), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
