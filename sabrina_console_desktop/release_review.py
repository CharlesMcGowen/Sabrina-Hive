"""Inputs and model selection for the console's fixed, report-only release review."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from zipfile import ZipFile


MODE = "github_release_review"
GOAL = ("Develop a GitHub automation skill and clean codebase packaging to publish the Sabrina Console "
        "repository to GitHub, including sanitized configuration templates, repository structure, SKILL.md "
        "definition, automated release script, and safety guardrails under execution_authorized=False.")
LOCAL_MODEL = "ollama/gemma3:12b"
KIT = Path(__file__).resolve().parent.parent / "release" / "console-kit"
INPUTS = ("skills/github-console-release/SKILL.md", "scripts/build_github_release.py", ".env.example", "REPOSITORY.md")


def matches_goal(text: str) -> bool:
    return " ".join(text.split()).rstrip(".").casefold() == GOAL.rstrip(".").casefold()


def original_planner(report_dir: Path, mission_id: str) -> str:
    if not re.fullmatch(r"m-[0-9a-f]{8}", mission_id):
        raise ValueError("Invalid release mission ID")
    report = (report_dir / f"{mission_id}.md").read_text(encoding="utf-8")
    goal = re.search(r"^- \*\*Goal:\*\* (.+)$", report, re.M)
    if not goal or not matches_goal(goal.group(1)):
        raise ValueError("The selected report is for a different objective")
    planner = re.search(r"^### Planner Architect .*?(?=^### |\Z)", report, re.M | re.S)
    attempted = re.search(r"LLM failed: gemini/(gemini-[\w.-]+)", planner.group() if planner else "")
    if not attempted:
        raise ValueError("The report does not record a failed Gemini planner attempt")
    return attempted.group(1)


class ReleaseReview:
    def __init__(self, planner_model, invoke_text, get_gemini_key):
        self.planner_model = planner_model
        self._invoke_text = invoke_text
        self._get_gemini_key = get_gemini_key
        archive_path = KIT / "dist" / "sabrina-console-release-kit.zip"
        if archive_path.resolve() != archive_path or archive_path.stat().st_size > 1_000_000:
            raise ValueError("Unexpected release archive")
        self.digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        self.files = {}
        with ZipFile(archive_path) as archive:
            if sorted(archive.namelist()) != sorted(INPUTS):
                raise ValueError("The archive must contain exactly the four release-kit files")
            for name in INPUTS:
                source = KIT / name
                if source.resolve() != source or source.stat().st_size > 32_768:
                    raise ValueError(f"Unexpected release input: {name}")
                contents = source.read_bytes()
                if archive.getinfo(name).file_size != len(contents) or archive.read(name) != contents:
                    raise ValueError(f"Rebuild the release archive after editing {name}")
                self.files[name] = contents.decode("utf-8")
        settings = [line.strip() for line in self.files[".env.example"].splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        if sorted(settings) != sorted(("EXECUTION_AUTHORIZED=false", "GEMINI_API_KEY=", "OLLAMA_MODEL=gemma3:12b")):
            raise ValueError("The release template must use empty credentials and keep execution disabled")

    def workspace_info(self):
        return {
            "workspace_root": "release/console-kit",
            "top_entries": list(self.files),
            "scanned_files": len(self.files),
            "extension_summary": {".md": 2, ".py": 1, ".example": 1},
            "keywords": ["github", "release", "packaging", "skill"],
            "matched_files": [{"path": name, "matched_terms": ["release"], "size_bytes": len(text.encode())}
                              for name, text in self.files.items()],
            "file_excerpts": [{"path": name, "excerpt": text} for name, text in self.files.items()],
            "execution_authorized": False,
        }

    def excerpts(self):
        return "\n\n".join(f"--- {name} ---\n{text}" for name, text in self.files.items())

    def call(self, role, model, system_prompt, user_prompt):
        if role != "planner_architect":
            evidence = (f"Verified release kit at release/console-kit. Archive: dist/sabrina-console-release-kit.zip. "
                        f"SHA-256: {self.digest}. Its four entries matched the release files byte-for-byte. "
                        "Scope: release tooling and sanitized configuration, prepared locally for operator review.\n"
                        + self.files["REPOSITORY.md"] + "\n" + self.files[".env.example"])
            return self._invoke_text("ollama", LOCAL_MODEL, system_prompt + " Use at most 150 words.",
                                     f"{user_prompt}\n\nBuild evidence:\n{evidence}",
                                     local_timeout=120.0, local_reply_tokens=320)
        # The cloud planner receives only this fixed release checklist, never workspace contents.
        system_prompt = "Plan a local software release-kit verification in three concise steps."
        user_prompt = ("Review the sanitized config, repository layout, SKILL.md and local ZIP builder. "
                       "A fixed four-file archive was verified byte-for-byte. Execution is disabled. "
                       "The deliverable is the local kit and review report. Publication is a separate decision.")
        key = None
        try:
            key = self._get_gemini_key()
            cloud = (self._invoke_text("gemini", self.planner_model, system_prompt, user_prompt, api_key=key)
                     if key else {"ok": False, "error": "Gemini key unavailable"})
        except Exception as exc:
            cloud = {"ok": False, "error": str(exc)}
        if cloud.get("ok") and str(cloud.get("text") or "").strip():
            return cloud
        error = str(cloud.get("error") or "Gemini returned an empty plan")
        if key:
            error = error.replace(key, "[redacted]")
        primary_error = f"gemini/{self.planner_model}: {error}"
        try:
            local = self._invoke_text("ollama", LOCAL_MODEL, system_prompt + " Use at most 90 words.", user_prompt,
                                      local_timeout=120.0, local_reply_tokens=220)
        except Exception as exc:
            local = {"ok": False, "error": str(exc)}
        if not local.get("ok") or not str(local.get("text") or "").strip():
            errors = [primary_error, f"{LOCAL_MODEL}: {local.get('error') or 'empty plan'}"]
            return {"ok": False, "error": " | ".join(errors), "all_errors": errors,
                    "primary_error": primary_error, "requested_model": self.planner_model}
        return {**local, "used_fallback": True, "requested_model": self.planner_model,
                "primary_error": primary_error, "fallback_errors": [primary_error], "attempts": 2}
