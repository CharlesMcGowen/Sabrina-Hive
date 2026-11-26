"""Deterministic Product Quality Assurance (QA) & Headless GUI Smoke Engine for Sabrina Swarm.

Executes bounded, side-effect-free Quality Assurance checks inside a workspace so
Swarm missions (`product_qa` mode / `tool:run_qa`) and A2A Automation Workflows
(`step_type="qa"` / `qa_verifier`) can verify real product runnability beyond
static AST syntax checks:

1. `syntax_and_security`: AST parse on Python modules, launcher path existence,
   and `tkinter.messagebox` double-crash detection inside `TclError`-prone launch guards.
2. `entrypoint_imports`: Isolated subprocess import probe (`python -B`) across
   target entrypoints & modules to catch `ModuleNotFoundError` / `ImportError`
   and import-time side-effect crashes.
3. `gui_smoke`: Isolated headless Tkinter smoke probe (`root = tk.Tk(); root.withdraw()`)
   that replays launcher `TCL_LIBRARY`/`TK_LIBRARY` environment setup and constructs
   GUI widgets (`KrakenDesktopWidget`, `KrakenView`, etc.) with network polling stubbed,
   catching Tcl/Tk version conflicts (`TclError`) and widget layout crashes without
   flashing windows or blocking on `mainloop()`.
4. `ui_html_parity`: Verifies `sabrina_desktop_ui.html` asset sync, static `el("id")`
   DOM coverage, and `#view-kraken` / bridge API method parity when applicable.
5. `unit_tests`: Runs matching `unittest` suites in a bounded subprocess and records
   exact pass/fail counts and failure tracebacks.

Safety Invariant: `execution_authorized` is strictly `False` on every returned payload.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_ROOT = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(DESKTOP_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from swarm_console.protocol import PathIsolationError, guard_safe_path

DEFAULT_QA_CHECKS: tuple[str, ...] = (
    "syntax_and_security",
    "entrypoint_imports",
    "gui_smoke",
    "ui_html_parity",
    "unit_tests",
)

_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        "site-packages",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "tcl",
    }
)


def _python_exe() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        cand = exe.with_name("python.exe")
        if cand.is_file():
            return str(cand)
    return str(exe)


def _extract_static_tcl_env_overrides(py_source: str, file_dir: Path) -> dict[str, str]:
    """Detect unconditional `os.environ['TCL_LIBRARY'] = ...` overrides in launcher scripts."""
    overrides: dict[str, str] = {}
    try:
        tree = ast.parse(py_source)
    except SyntaxError:
        return overrides

    # Only flag top-level (unconditional) or `if __name__ == '__main__':` unconditional assignments
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            for tgt in stmt.targets:
                if (
                    isinstance(tgt, ast.Subscript)
                    and isinstance(tgt.value, ast.Attribute)
                    and isinstance(tgt.value.value, ast.Name)
                    and tgt.value.value.id == "os"
                    and tgt.value.attr == "environ"
                ):
                    key = None
                    if isinstance(tgt.slice, ast.Constant) and isinstance(tgt.slice.value, str):
                        key = tgt.slice.value
                    if key in ("TCL_LIBRARY", "TK_LIBRARY"):
                        sub = "tcl8.6" if key == "TCL_LIBRARY" else "tk8.6"
                        cand = file_dir / "tcl" / sub
                        overrides[key] = str(cand)
    return overrides


def _has_unguarded_tk_messagebox_in_except(py_source: str) -> bool:
    """Return True if an `except` handler calls `messagebox.showerror` without catching `TclError`."""
    try:
        tree = ast.parse(py_source)
    except SyntaxError:
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            body_src = ast.unparse(node) if hasattr(ast, "unparse") else ""
            if "messagebox.showerror" in body_src:
                has_inner_try = any(isinstance(ch, ast.Try) for ch in node.body)
                if not has_inner_try:
                    return True
    return False


def _discover_target_files(workspace_root: Path, target: str) -> dict[str, list[Path]]:
    """Collect Python files, GUI scripts, tests, and HTML files matching `target`."""
    root = guard_safe_path(workspace_root).resolve()
    clean_target = str(target or ".").strip()
    low_target = clean_target.lower()

    py_files: list[Path] = []
    test_files: list[Path] = []
    gui_files: list[Path] = []
    html_files: list[Path] = []

    # Check if `target` is an explicit relative file or directory
    explicit_path: Path | None = None
    if clean_target not in ("", ".", "all", "*"):
        cand = (root / clean_target.lstrip("/\\")).resolve()
        if cand.is_relative_to(root) and cand.exists():
            explicit_path = cand

    keyword_tokens = [
        tok
        for tok in re.findall(r"[a-z0-9_]+", low_target)
        if tok not in {"py", "gui", "qa", "test", "tests", "run", "the", "and", "for", "check", "verify", "product"}
    ]

    # Curated priority entrypoints when auditing Kraken or Console GUI in Sabrina repo
    is_kraken_focus = any(k in low_target for k in ("kraken", "monitor", "widget"))
    priority_rels: list[str] = []
    if is_kraken_focus:
        priority_rels.extend(
            [
                "sabrina_console_desktop/launch_kraken_monitor.py",
                "sabrina_console_desktop/launch_console.py",
                "sabrina_console_desktop/sabrina_operator_console.py",
                "sabrina_console_desktop/tools/kraken_desktop_widget.py",
                "sabrina_console_desktop/tools/sabrina_console_kraken.py",
                "sabrina_console_desktop/tools/sabrina_console_workflow.py",
                "sabrina_console_desktop/tools/kraken_turnover_score.py",
                "sabrina_console_desktop/test_kraken_turnover_score.py",
                "kraken_tooling/tests/test_kraken_tooling.py",
            ]
        )

    seen: set[Path] = set()
    for rel_str in priority_rels:
        p = (root / rel_str).resolve()
        if p.is_file() and p.is_relative_to(root) and p not in seen:
            seen.add(p)
            if p.name.startswith("test_") or p.name.endswith("_test.py"):
                test_files.append(p)
            else:
                py_files.append(p)

    scan_base = explicit_path if (explicit_path and explicit_path.is_dir()) else root
    if explicit_path and explicit_path.is_file():
        if explicit_path.suffix.lower() == ".py" and explicit_path not in seen:
            seen.add(explicit_path)
            if explicit_path.name.startswith("test_"):
                test_files.append(explicit_path)
            else:
                py_files.append(explicit_path)
        elif explicit_path.suffix.lower() in (".html", ".htm"):
            html_files.append(explicit_path)
    else:
        scanned = 0
        for dirpath, dirnames, filenames in os.walk(scan_base):
            dirnames[:] = [d for d in sorted(dirnames) if d not in _SKIP_DIRS and not d.startswith(".")]
            for fname in sorted(filenames):
                if scanned >= 350:
                    break
                fpath = (Path(dirpath) / fname).resolve()
                if fpath in seen:
                    continue
                if fname == "product_qa.py" or "qa_workflow" in fname.lower():
                    continue
                try:
                    guard_safe_path(fpath)
                    rel = fpath.relative_to(root).as_posix().lower()
                except (ValueError, PathIsolationError):
                    continue
                scanned += 1
                ext = fpath.suffix.lower()
                if ext in (".html", ".htm"):
                    if not keyword_tokens or is_kraken_focus or any(t in rel for t in keyword_tokens):
                        html_files.append(fpath)
                    continue
                if ext != ".py":
                    continue
                if keyword_tokens and not any(t in rel for t in keyword_tokens):
                    continue
                seen.add(fpath)
                if fname.startswith("test_") or fname.endswith("_test.py"):
                    test_files.append(fpath)
                else:
                    py_files.append(fpath)
            if scanned >= 350:
                break

    for pf in py_files[:30]:
        try:
            txt = pf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(tok in txt for tok in ("import tkinter", "from tkinter", "TCL_LIBRARY", "webview.create_window")):
            gui_files.append(pf)

    return {
        "py_files": py_files[:24],
        "gui_files": gui_files[:12],
        "test_files": test_files[:10],
        "html_files": html_files[:6],
    }


def _module_import_spec(workspace_root: Path, file_path: Path) -> tuple[str, list[str]]:
    """Derive an importable dotted module name and sys.path roots for `file_path`."""
    rel = file_path.relative_to(workspace_root)
    sys_paths = [str(workspace_root)]
    desktop_dir = workspace_root / "sabrina_console_desktop"
    if desktop_dir.is_dir():
        sys_paths.insert(0, str(desktop_dir))
        if file_path.is_relative_to(desktop_dir):
            sub_rel = file_path.relative_to(desktop_dir).with_suffix("")
            return ".".join(sub_rel.parts), sys_paths
    return ".".join(rel.with_suffix("").parts), sys_paths


def _run_subprocess_json(code: str, payload: dict[str, Any], cwd: Path, timeout: float = 15.0) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        proc = subprocess.run(
            [_python_exe(), "-B", "-c", code],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd),
            env=env,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout = (proc.stdout or "").strip()
        if stdout:
            last_line = stdout.splitlines()[-1].strip()
            try:
                return json.loads(last_line)
            except ValueError:
                pass
        return {
            "ok": False,
            "error": (proc.stderr or proc.stdout or f"Process exited with {proc.returncode}").strip()[-500:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Subprocess QA probe timed out after {timeout:.0f}s"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


_IMPORT_PROBE_SUBPROCESS = r"""
import importlib, json, sys, traceback

cfg = json.loads(sys.stdin.read())
for p in reversed(cfg.get("sys_paths") or []):
    if p not in sys.path:
        sys.path.insert(0, p)

results = []
for item in cfg.get("modules") or []:
    mod_name = item["module"]
    rel_path = item["path"]
    try:
        importlib.import_module(mod_name)
        results.append({"path": rel_path, "module": mod_name, "ok": True, "error": ""})
    except Exception as exc:
        tb = traceback.format_exc().splitlines()
        short_tb = " | ".join(ln.strip() for ln in tb[-4:] if ln.strip())
        results.append({
            "path": rel_path,
            "module": mod_name,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "detail": short_tb[:360],
        })

print(json.dumps({"ok": True, "results": results}))
"""


_GUI_SMOKE_SUBPROCESS = r"""
import importlib, json, os, sys, traceback

cfg = json.loads(sys.stdin.read())
for p in reversed(cfg.get("sys_paths") or []):
    if p not in sys.path:
        sys.path.insert(0, p)

# Apply launcher environment overrides (if any) or call module's configure_tk_environment()
file_path = cfg.get("file_path", "")
mod_name = cfg.get("module", "")
tcl_overrides = cfg.get("tcl_overrides") or {}

try:
    # If the launcher module defines `configure_tk_environment()`, let it configure Tcl safely;
    # otherwise replay any raw hardcoded top-level TCL_LIBRARY/TK_LIBRARY overrides we detected.
    mod_obj = importlib.import_module(mod_name) if mod_name else None
    if mod_obj is not None and hasattr(mod_obj, "configure_tk_environment"):
        mod_obj.configure_tk_environment()
    else:
        for k, v in tcl_overrides.items():
            os.environ[k] = v

    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.withdraw()
    widget_checked = "tk.Tk()"

    if mod_obj is not None:
        if hasattr(mod_obj, "KrakenDesktopWidget"):
            cls = mod_obj.KrakenDesktopWidget
            orig_start = getattr(cls, "_start_polling", None)
            # Prevent infinite background SSH threads during smoke test while verifying full UI construction
            if hasattr(mod_obj, "fetch_remote_snapshot"):
                mod_obj.fetch_remote_snapshot = lambda: {
                    "fetched_at": "QA_SMOKE",
                    "live_service_up": False,
                    "paper_service_up": True,
                    "live": {"enabled": False, "cash_usd": 0.0, "today_pnl_usd": 0.0, "today_wins": 0, "today_losses": 0},
                    "paper": {"cycles": 1, "saved_at": "QA_SMOKE", "portfolios": {}, "open_items": [], "recent_trades": []},
                }
            w = cls(root)
            widget_checked = "KrakenDesktopWidget"
            assert hasattr(w, "paper_widgets") and len(w.paper_widgets) >= 5, "Expected 5 paper portfolio cards"
            assert hasattr(w, "txt_feed"), "Missing activity feed text widget"
        elif hasattr(mod_obj, "KrakenView"):
            nb = ttk.Notebook(root)
            nb.pack()
            kv = mod_obj.KrakenView(nb, lambda *a, **k: {"jobs": []})
            nb.add(kv, text="Kraken")
            widget_checked = "KrakenView"

    root.update_idletasks()
    tcl_ver = str(root.tk.call("info", "patchlevel"))
    root.destroy()
    print(json.dumps({"ok": True, "widget": widget_checked, "tcl_patchlevel": tcl_ver}))
except Exception as exc:
    tb = traceback.format_exc().splitlines()
    short_tb = " | ".join(ln.strip() for ln in tb[-5:] if ln.strip())
    print(json.dumps({
        "ok": False,
        "error": f"{type(exc).__name__}: {exc}",
        "detail": short_tb[:420],
    }))
"""


def run_product_qa(
    workspace_root: Path | str,
    target: str = ".",
    *,
    checks: list[str] | tuple[str, ...] | None = None,
    run_tests: bool = True,
) -> dict[str, Any]:
    """Execute deterministic Product QA checks and return a structured report."""
    t0 = time.monotonic()
    root = guard_safe_path(Path(workspace_root)).resolve()
    active_checks = tuple(checks or DEFAULT_QA_CHECKS)
    discovered = _discover_target_files(root, target)

    check_results: list[dict[str, Any]] = []
    defects: list[dict[str, Any]] = []
    warnings: list[str] = []
    remediation_hints: list[str] = []

    # 1. Syntax, Launcher Integrity & Error-Handler Safety
    if "syntax_and_security" in active_checks:
        syn_errors: list[str] = []
        for pf in discovered["py_files"] + discovered["test_files"]:
            rel = pf.relative_to(root).as_posix()
            try:
                src = pf.read_text(encoding="utf-8", errors="replace")
                ast.parse(src, filename=rel)
            except SyntaxError as exc:
                msg = f"{rel}:{exc.lineno}: SyntaxError: {exc.msg}"
                syn_errors.append(msg)
                defects.append({"check": "syntax_and_security", "file": rel, "severity": "high", "summary": msg})
                continue

            if ("TCL_LIBRARY" in src or "tkinter" in src) and _has_unguarded_tk_messagebox_in_except(src):
                warn_msg = (
                    f"{rel}: `except` block calls `tkinter.messagebox.showerror` without a `TclError`/Win32 fallback; "
                    "if Tcl/Tk fails to initialize, the error dialog itself crashes silently."
                )
                warnings.append(warn_msg)
                remediation_hints.append(
                    f"In `{rel}`, wrap `messagebox.showerror` in `try/except Exception` with a `ctypes.windll.user32.MessageBoxW` fallback."
                )

        check_results.append(
            {
                "name": "syntax_and_security",
                "passed": len(syn_errors) == 0,
                "checked_count": len(discovered["py_files"]) + len(discovered["test_files"]),
                "errors": syn_errors,
            }
        )

    # 2. Subprocess Entrypoint & Module Import Probe
    if "entrypoint_imports" in active_checks:
        mod_specs: list[dict[str, str]] = []
        all_sys_paths: list[str] = [str(root)]
        for pf in discovered["py_files"][:16]:
            # Skip Linux-only systemd installer scripts on Windows
            rel = pf.relative_to(root).as_posix()
            if os.name == "nt" and pf.name in {"install_kraken_paper_launcher.py", "kraken_paper_sandbox_launcher.py"}:
                continue
            mod_name, sp_list = _module_import_spec(root, pf)
            for sp in sp_list:
                if sp not in all_sys_paths:
                    all_sys_paths.append(sp)
            mod_specs.append({"path": rel, "module": mod_name})

        imp_errors: list[str] = []
        if mod_specs:
            probe_res = _run_subprocess_json(
                _IMPORT_PROBE_SUBPROCESS,
                {"sys_paths": all_sys_paths, "modules": mod_specs},
                cwd=root,
                timeout=20.0,
            )
            if not probe_res.get("ok"):
                err = str(probe_res.get("error") or "Import probe failed")
                imp_errors.append(err)
                defects.append({"check": "entrypoint_imports", "file": target, "severity": "high", "summary": err})
            else:
                for item in probe_res.get("results") or []:
                    if not item.get("ok"):
                        msg = f"{item['path']} ({item['module']}): {item['error']}"
                        imp_errors.append(msg)
                        defects.append(
                            {
                                "check": "entrypoint_imports",
                                "file": item["path"],
                                "severity": "high",
                                "summary": msg,
                                "detail": item.get("detail", ""),
                            }
                        )
                        if "No module named 'mcp'" in item.get("error", ""):
                            remediation_hints.append(
                                f"Guard optional `mcp.server.fastmcp` imports in `sabrina_console_desktop/tools/sabrina_lab_assistant_mcp.py` "
                                f"so `{item['path']}` can import helper functions when the optional `mcp` package is not installed."
                            )
        check_results.append(
            {
                "name": "entrypoint_imports",
                "passed": len(imp_errors) == 0,
                "checked_count": len(mod_specs),
                "errors": imp_errors,
            }
        )

    # 3. Headless GUI Smoke Verification (Tkinter / Tcl Environment + Widget Construction)
    if "gui_smoke" in active_checks:
        gui_errors: list[str] = []
        gui_verified: list[str] = []
        for gf in discovered["gui_files"][:8]:
            rel = gf.relative_to(root).as_posix()
            try:
                src = gf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "tkinter" not in src and "TCL_LIBRARY" not in src:
                continue
            mod_name, sp_list = _module_import_spec(root, gf)
            tcl_overrides = _extract_static_tcl_env_overrides(src, gf.parent)
            smoke_res = _run_subprocess_json(
                _GUI_SMOKE_SUBPROCESS,
                {
                    "sys_paths": sp_list,
                    "file_path": rel,
                    "module": mod_name,
                    "tcl_overrides": tcl_overrides,
                },
                cwd=gf.parent,
                timeout=15.0,
            )
            if smoke_res.get("ok"):
                gui_verified.append(f"{rel} ({smoke_res.get('widget', 'tk')} · Tcl {smoke_res.get('tcl_patchlevel', '')})")
            else:
                msg = f"{rel}: {smoke_res.get('error', 'GUI smoke failed')}"
                gui_errors.append(msg)
                defects.append(
                    {
                        "check": "gui_smoke",
                        "file": rel,
                        "severity": "critical",
                        "summary": msg,
                        "detail": smoke_res.get("detail", ""),
                    }
                )
                if "TclError" in msg or "init.tcl" in msg:
                    remediation_hints.append(
                        f"Fix `TCL_LIBRARY`/`TK_LIBRARY` resolution in `{rel}`: prefer Python's native `Path(sys.base_prefix) / 'tcl' / 'tcl8.6'` "
                        "when present instead of unconditionally overriding with a mismatched bundled `tcl/tcl8.6` directory."
                    )
        check_results.append(
            {
                "name": "gui_smoke",
                "passed": len(gui_errors) == 0,
                "checked_count": len(gui_verified) + len(gui_errors),
                "verified": gui_verified,
                "errors": gui_errors,
            }
        )

    # 4. Desktop HTML & Bridge Parity Check
    if "ui_html_parity" in active_checks:
        html_errors: list[str] = []
        desktop_html = root / "sabrina_console_desktop" / "sabrina_desktop_ui.html"
        root_html = root / "sabrina_desktop_ui.html"
        checked_html = 0
        if desktop_html.is_file():
            checked_html += 1
            html_txt = desktop_html.read_text(encoding="utf-8", errors="replace")
            if root_html.is_file():
                checked_html += 1
                if html_txt != root_html.read_text(encoding="utf-8", errors="replace"):
                    msg = "sabrina_desktop_ui.html and sabrina_console_desktop/sabrina_desktop_ui.html are out of sync."
                    html_errors.append(msg)
                    defects.append({"check": "ui_html_parity", "file": "sabrina_desktop_ui.html", "severity": "medium", "summary": msg})
            defined_ids = set(re.findall(r'\bid="([^"]+)"', html_txt))
            referenced_ids = set(re.findall(r'\bel\(\s*"([A-Za-z0-9_-]+)"\s*\)', html_txt))
            missing_ids = sorted(referenced_ids - defined_ids)
            if missing_ids:
                msg = f"Missing DOM IDs referenced by el(): {', '.join(missing_ids[:8])}"
                html_errors.append(msg)
                defects.append({"check": "ui_html_parity", "file": "sabrina_console_desktop/sabrina_desktop_ui.html", "severity": "high", "summary": msg})
        else:
            for hf in discovered["html_files"]:
                checked_html += 1
                htxt = hf.read_text(encoding="utf-8", errors="replace")
                for asset_m in re.finditer(r'(?:src|href)="([^"#?:]+)"', htxt):
                    ref = asset_m.group(1).strip()
                    if ref.startswith(("http://", "https://", "data:", "javascript:", "mailto:")):
                        continue
                    cand_asset = (hf.parent / ref).resolve()
                    if not cand_asset.is_file():
                        rel_h = hf.relative_to(root).as_posix()
                        msg = f"{rel_h}: missing local asset `{ref}`"
                        html_errors.append(msg)
                        defects.append({"check": "ui_html_parity", "file": rel_h, "severity": "medium", "summary": msg})
        check_results.append(
            {
                "name": "ui_html_parity",
                "passed": len(html_errors) == 0,
                "checked_count": checked_html,
                "errors": html_errors,
            }
        )

    # 5. Bounded Unit Test Suite Execution
    if "unit_tests" in active_checks and run_tests and os.environ.get("SABRINA_PRODUCT_QA_ACTIVE") != "1":
        test_modules: list[str] = []
        for tf in discovered["test_files"][:6]:
            mod_name, _ = _module_import_spec(root, tf)
            # Prefer repo-root dotted module path for unittest runner
            rel_mod = ".".join(tf.relative_to(root).with_suffix("").parts)
            test_modules.append(rel_mod)

        test_errors: list[str] = []
        tests_ran = 0
        if test_modules:
            env = dict(os.environ)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["SABRINA_PRODUCT_QA_ACTIVE"] = "1"
            py_paths = [str(root)]
            if (root / "sabrina_console_desktop").is_dir():
                py_paths.insert(0, str(root / "sabrina_console_desktop"))
            if env.get("PYTHONPATH"):
                py_paths.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = os.pathsep.join(py_paths)
            try:
                proc = subprocess.run(
                    [_python_exe(), "-B", "-m", "unittest", *test_modules, "-v"],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    timeout=35.0,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                combined = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                m_ran = re.search(r"Ran (\d+) tests?", combined)
                if m_ran:
                    tests_ran = int(m_ran.group(1))
                if proc.returncode != 0:
                    tail = "\n".join(combined.splitlines()[-14:])
                    msg = f"Unit tests failed (exit={proc.returncode}): {tail[:360]}"
                    test_errors.append(msg)
                    defects.append({"check": "unit_tests", "file": ", ".join(test_modules), "severity": "high", "summary": msg})
            except Exception as exc:
                msg = f"Unit test execution error: {exc}"
                test_errors.append(msg)
                defects.append({"check": "unit_tests", "file": ", ".join(test_modules), "severity": "high", "summary": msg})

        check_results.append(
            {
                "name": "unit_tests",
                "passed": len(test_errors) == 0,
                "checked_count": len(test_modules),
                "tests_ran": tests_ran,
                "modules": test_modules,
                "errors": test_errors,
            }
        )

    passed = all(c["passed"] for c in check_results)
    total_checks = max(1, len(check_results))
    passed_checks = sum(1 for c in check_results if c["passed"])
    score = round(passed_checks / total_checks, 3)
    elapsed_ms = round((time.monotonic() - t0) * 1000.0, 1)
    dedup_hints = list(dict.fromkeys(remediation_hints))

    md_lines = [
        f"### Product Quality Assurance (QA) Receipt · `{'PASS' if passed else 'FAIL'}`",
        f"- **Target:** `{target}` (`{root}`)",
        f"- **Checks Passed:** `{passed_checks}/{len(check_results)}` (score `{score:.2f}`, `{elapsed_ms:.0f}ms`)",
        f"- **Defects Found:** `{len(defects)}` | **Warnings:** `{len(warnings)}`",
        "",
    ]
    for c in check_results:
        badge = "PASS" if c["passed"] else "FAIL"
        extra = f" ({c['tests_ran']} assertions)" if "tests_ran" in c else f" ({c['checked_count']} item(s))"
        md_lines.append(f"- **`{c['name']}`**: `{badge}`{extra}")
        for v in (c.get("verified") or [])[:5]:
            md_lines.append(f"  - Verified GUI: `{v}`")
        for err in (c.get("errors") or [])[:5]:
            md_lines.append(f"  - Defect: `{err[:260]}`")
    if warnings:
        md_lines.append("")
        md_lines.append("#### QA Warnings")
        for w in warnings[:5]:
            md_lines.append(f"- {w}")
    if dedup_hints:
        md_lines.append("")
        md_lines.append("#### Actionable Remediation Hints")
        for h in dedup_hints[:5]:
            md_lines.append(f"- {h}")

    return {
        "ok": True,
        "passed": passed,
        "status": "PASS" if passed else "FAIL",
        "target": target,
        "workspace_root": str(root),
        "score": score,
        "elapsed_ms": elapsed_ms,
        "checks": check_results,
        "defects": defects,
        "warnings": warnings,
        "remediation_hints": dedup_hints,
        "markdown": "\n".join(md_lines),
        "execution_authorized": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sabrina Product QA & GUI Smoke Runner")
    parser.add_argument("--workspace", type=Path, default=REPO_ROOT)
    parser.add_argument("--target", type=str, default="kraken")
    parser.add_argument("--no-tests", action="store_true")
    args = parser.parse_args()
    res = run_product_qa(args.workspace, args.target, run_tests=not args.no_tests)
    print(json.dumps(res, indent=2))
    sys.exit(0 if res["passed"] else 1)


if __name__ == "__main__":
    main()
