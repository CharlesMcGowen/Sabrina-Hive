"""
tools/sql_injection_tool.py
===========================
Sabrina sub-tool: SQL Injection RL Campaign Launcher.

Wraps the rl_sql_guesser_system (RL loop + two-brain Ollama) and exposes it
as a callable, Sabrina-compatible tool. Sabrina can invoke this when she
detects a SQL injection attack surface during a pentest engagement.

Usage (standalone):
    from tools.sql_injection_tool import SQLInjectionTool
    tool = SQLInjectionTool(target_base_url="http://192.168.2.69:8020")
    results = tool.run_campaign()
    print(results.summary())

Usage (via Sabrina):
    Sabrina will call run_campaign() as part of her pentest planning step.
    The tool returns a structured SQLCampaignResult so Sabrina can reason
    over which payloads succeeded and queue fine-tuning jobs if desired.
"""

import os
import sys
import json
import subprocess
import threading
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from pathlib import Path

# --- Path resolution ---
# Live-verified on the lab host: SQLplayground is a DIRECT SIBLING of Sabrina
# under /home/ego/bot/, not under a separate "bot2" directory -- that path
# never existed. Fixed after checking the real filesystem layout rather than
# trusting the original assumption.
_TOOL_DIR   = Path(__file__).resolve().parent          # .../bot/Sabrina/tools
_SABRINA_DIR = _TOOL_DIR.parent                        # .../bot/Sabrina
_RL_SYSTEM_DIR = _SABRINA_DIR.parent / "SQLplayground" / "rl_sql_guesser_system"

# Default challenge file bundled with the RL system
_DEFAULT_CHALLENGES = str(_RL_SYSTEM_DIR / "sql_challenges.jsonl")

# Default DPO output directory
_DEFAULT_OUTPUT_DIR = str(_RL_SYSTEM_DIR / "rl_data")


@dataclass
class SQLCampaignResult:
    """Structured result returned to Sabrina after an RL campaign."""
    target_url: str
    tasks_attempted: int = 0
    tasks_solved: int = 0
    payloads_tried: int = 0
    successful_payloads: List[Dict] = field(default_factory=list)
    dpo_output_path: str = ""
    raw_log: str = ""
    error: Optional[str] = None

    def summary(self) -> str:
        if self.error:
            return f"[SQL Campaign Error] {self.error}"
        lines = [
            f"[SQL Injection Campaign — {self.target_url}]",
            f"  Tasks attempted : {self.tasks_attempted}",
            f"  Tasks solved    : {self.tasks_solved}",
            f"  Payloads tried  : {self.payloads_tried}",
            f"  DPO corpus at   : {self.dpo_output_path}",
        ]
        if self.successful_payloads:
            lines.append("  Winning payloads:")
            for p in self.successful_payloads[:5]:  # Show top 5
                lines.append(f"    • [{p.get('task_id', '?')}] {p.get('payload', '')[:80]}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "target_url": self.target_url,
            "tasks_attempted": self.tasks_attempted,
            "tasks_solved": self.tasks_solved,
            "payloads_tried": self.payloads_tried,
            "successful_payloads": self.successful_payloads,
            "dpo_output_path": self.dpo_output_path,
            "error": self.error,
        }


class SQLInjectionTool:
    """
    Sabrina-compatible wrapper around the rl_sql_guesser_system.

    Args:
        target_base_url:  The base URL of the vulnerable SQL server to attack.
                          Defaults to http://localhost:8020 (local lab).
        reasoner_model:   Ollama model name for the Reasoner brain.
        coder_model:      Ollama model name for the Coder brain.
        max_iters:        Max RL iterations per challenge task.
        workers:          Number of parallel challenge workers.
        challenges_file:  Path to the .jsonl file listing SQL challenges.
        output_dir:       Directory where DPO output and logs are saved.
    """

    def __init__(
        self,
        target_base_url: str = "http://localhost:8020",
        reasoner_model: str = "llama3.1:8b",
        coder_model: str = "llama3.1:8b",
        max_iters: int = 5,
        workers: int = 1,
        challenges_file: Optional[str] = None,
        output_dir: Optional[str] = None,
    ):
        self.target_base_url = target_base_url.rstrip("/")
        self.reasoner_model  = reasoner_model
        self.coder_model     = coder_model
        self.max_iters       = max_iters
        self.workers         = workers
        self.challenges_file = challenges_file or _DEFAULT_CHALLENGES
        self.output_dir      = output_dir or _DEFAULT_OUTPUT_DIR

    # ------------------------------------------------------------------
    # Health check
    # ------------------------------------------------------------------

    def _check_target_alive(self) -> bool:
        """Ping the target server's /api/schema endpoint to verify it's up."""
        try:
            url = f"{self.target_base_url}/api/schema"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Corpus parsing helpers
    # ------------------------------------------------------------------

    def _parse_corpus(self, output_file: str) -> List[Dict]:
        """Parse the successful payload corpus file written by the RL loop."""
        payloads = []
        if not os.path.exists(output_file):
            return payloads
        with open(output_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    instruction = data.get("instruction", "")
                    response    = data.get("response", "")
                    # Extract task id from instruction ("Task: <id>")
                    task_id = "unknown"
                    for part in instruction.split("\n"):
                        if part.startswith("Task:"):
                            task_id = part.replace("Task:", "").strip()
                            break
                    # Extract the raw payload from the code block
                    import re
                    match = re.search(r"```[^\n]*\n(.*?)```", response, re.DOTALL)
                    payload = match.group(1).strip() if match else response[:200]
                    payloads.append({"task_id": task_id, "payload": payload})
                except Exception:
                    pass
        return payloads

    def _count_attempts_in_log(self, log: str) -> int:
        """Count total payloads tried by counting 'Generated Input:' lines."""
        import re
        return len(re.findall(r"Generated Input:", log))

    # ------------------------------------------------------------------
    # Main campaign runner
    # ------------------------------------------------------------------

    def run_campaign(
        self,
        timeout_seconds: int = 600,
        resume: bool = False,
    ) -> SQLCampaignResult:
        """
        Launch the RL SQL injection campaign.

        Args:
            timeout_seconds: Max wall-clock time (in seconds) to allow the
                             campaign to run before force-killing it.
            resume:          If True, skip challenges already solved in a
                             previous run.

        Returns:
            SQLCampaignResult with all findings structured for Sabrina.
        """
        result = SQLCampaignResult(target_url=self.target_base_url)

        # 1. Confirm the target is alive
        print(f"[SQLInjectionTool] Checking target: {self.target_base_url} ...")
        if not self._check_target_alive():
            result.error = (
                f"Target {self.target_base_url} is not responding. "
                "Make sure the SQL Playground server is running."
            )
            print(f"[SQLInjectionTool] ERROR: {result.error}")
            return result

        print(f"[SQLInjectionTool] Target is ALIVE. Starting RL campaign...")

        # 2. Check RL system directory exists
        if not _RL_SYSTEM_DIR.exists():
            result.error = f"RL system directory not found: {_RL_SYSTEM_DIR}"
            return result

        # 3. Count challenges
        challenges_path = Path(self.challenges_file)
        if not challenges_path.exists():
            result.error = f"Challenges file not found: {self.challenges_file}"
            return result

        with open(challenges_path, "r", encoding="utf-8") as f:
            challenges = [l for l in f if l.strip()]
        result.tasks_attempted = len(challenges)

        # 4. Build the subprocess command
        # Live-confirmed bug: a fixed "sql_corpus.jsonl" filename in the shared
        # rl_data directory meant _parse_corpus() below could read YEARS of
        # leftover results from unrelated prior runs -- a run where every
        # single model call failed still reported "60 tasks solved" this way.
        # A fresh run (resume=False, the default) now gets a unique filename
        # so it can never see another run's data. resume=True intentionally
        # keeps the shared filename, since resuming means "see what's already
        # solved and skip it."
        if resume:
            output_file = "sql_corpus.jsonl"
        else:
            output_file = f"sql_corpus_{int(time.time())}.jsonl"
        print(f"[SQLInjectionTool] Output file for this run: {output_file}")
        output_dir    = self.output_dir
        os.makedirs(output_dir, exist_ok=True)

        cmd = [
            sys.executable,
            str(_RL_SYSTEM_DIR / "rl_sql_guesser.py"),
            "--reasoner-model", self.reasoner_model,
            "--coder-model",    self.coder_model,
            "--input-file",     str(challenges_path),
            "--output-file",    output_file,
            "--iters",          str(self.max_iters),
            "--workers",        str(self.workers),
            "--data-dir",       output_dir,
        ]
        if resume:
            cmd.append("--resume")

        print(f"[SQLInjectionTool] Launching: {' '.join(cmd)}")

        # 5. Run the RL loop as a subprocess and stream its output
        log_lines: List[str] = []
        lock = threading.Lock()

        try:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(_RL_SYSTEM_DIR) + os.pathsep + env.get("PYTHONPATH", "")
            
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=str(_RL_SYSTEM_DIR),
                env=env,
            )

            def _stream():
                for line in proc.stdout:
                    with lock:
                        log_lines.append(line.rstrip())
                    print(f"  [RL] {line.rstrip()}")

            reader = threading.Thread(target=_stream, daemon=True)
            reader.start()

            proc.wait(timeout=timeout_seconds)
            reader.join(timeout=10)

        except subprocess.TimeoutExpired:
            proc.kill()
            print(f"[SQLInjectionTool] Campaign timed out after {timeout_seconds}s. Killing process.")
        except Exception as e:
            result.error = f"Failed to launch RL guesser subprocess: {e}"
            return result

        # 6. Parse the results
        raw_log = "\n".join(log_lines)
        result.raw_log = raw_log

        output_corpus_path = os.path.join(output_dir, output_file)
        result.successful_payloads = self._parse_corpus(output_corpus_path)
        result.tasks_solved        = len(result.successful_payloads)
        result.payloads_tried      = self._count_attempts_in_log(raw_log)
        result.dpo_output_path     = output_dir

        print(f"\n[SQLInjectionTool] Campaign complete!")
        print(result.summary())
        return result


# ---------------------------------------------------------------------------
# Module-level convenience function for Sabrina to call directly
# ---------------------------------------------------------------------------

def run_sql_injection_campaign(
    target_url: str = "http://localhost:8020",
    reasoner_model: str = "llama3.1:8b",
    coder_model: str = "llama3.1:8b",
    max_iters: int = 5,
    workers: int = 1,
    resume: bool = False,
    timeout: int = 600,
) -> dict:
    """
    Convenience function for Sabrina to invoke directly.
    Returns a plain dict so it can be serialized to Sabrina's blackboard.
    """
    tool = SQLInjectionTool(
        target_base_url=target_url,
        reasoner_model=reasoner_model,
        coder_model=coder_model,
        max_iters=max_iters,
        workers=workers,
    )
    result = tool.run_campaign(timeout_seconds=timeout, resume=resume)
    return result.to_dict()


if __name__ == "__main__":
    # Quick smoke test
    import argparse
    parser = argparse.ArgumentParser(description="Sabrina SQL Injection Tool")
    parser.add_argument("--target",   default="http://localhost:8020")
    parser.add_argument("--reasoner", default="llama3.1:8b")
    parser.add_argument("--coder",    default="llama3.1:8b")
    parser.add_argument("--iters",    type=int, default=3)
    parser.add_argument("--workers",  type=int, default=1)
    parser.add_argument("--resume",   action="store_true")
    parser.add_argument("--timeout",  type=int, default=600)
    args = parser.parse_args()

    tool = SQLInjectionTool(
        target_base_url=args.target,
        reasoner_model=args.reasoner,
        coder_model=args.coder,
        max_iters=args.iters,
        workers=args.workers,
    )
    result = tool.run_campaign(timeout_seconds=args.timeout, resume=args.resume)
    print("\n=== FINAL RESULT ===")
    print(json.dumps(result.to_dict(), indent=2))
