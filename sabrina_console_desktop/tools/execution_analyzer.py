import re

HTML_ERROR_SIGNALS = [
    "<!doctype html",
    "<html",
    "<body",
    "<style",
    "background: #ece9e9",
    "-webkit-gradient",
    "cannot get ",
    "cannot post ",
    "404 not found",
    "unexpected path:",
    "error: unexpected",
    "<title>error</title>",
    "<title>404",
    "<app-root>",
]

def analyze_execution(stdout: str, stderr: str, returncode: int) -> str:
    """Analyze the raw execution output and produce a high-signal summary."""
    stdout = stdout.strip()
    stderr = stderr.strip()
    combined_lower = f"{stdout.lower()}\n{stderr.lower()}"

    is_html_error = any(sig in combined_lower for sig in HTML_ERROR_SIGNALS)
    has_json_error_on_html = "jsondecodeerror" in combined_lower and any(
        s in combined_lower for s in ["<", "doctype", "style", "html", "gradient", "line 1 column 1"]
    )
    is_404 = "404" in combined_lower and ("not found" in combined_lower or "cannot" in combined_lower or "404 |" in combined_lower)

    if is_html_error or has_json_error_on_html or is_404:
        return (
            "WRONG ENDPOINT / ROUTE NOT FOUND: The target server returned an HTML/SPA error page or 404 "
            "(e.g., Angular shell or Express route not found) rather than API JSON. This endpoint path "
            "does NOT exist on the server. DO NOT retry variations of this same path. Pivot immediately "
            "to a genuinely different candidate endpoint."
        )

    if returncode != 0:
        if "TimeoutExpired" in stderr or "Timeout" in stdout:
            return "The script timed out. Ensure there are no blocking loops or network hangs."
            
        # Look for a python traceback in stderr
        if "Traceback (most recent call last):" in stderr:
            lines = stderr.splitlines()
            err_line = lines[-1] if lines else "Unknown Error"
            
            # Try to find the line number
            line_num_match = re.search(r"line (\d+)", stderr)
            line_info = f" around line {line_num_match.group(1)}" if line_num_match else ""
            
            return f"The script crashed with a Python exception{line_info}: `{err_line}`. Please fix this bug."
            
        return f"The script exited with code {returncode}. Error snippet: `{stderr[:200]}`"

    if "[SABRINA-FAIL]" in stdout:
        fail_line = [l for l in stdout.splitlines() if "[SABRINA-FAIL]" in l]
        if fail_line:
            fail_msg = fail_line[-1].replace("[SABRINA-FAIL] Hypothesis NOT confirmed:", "").strip()
            if any(sig in fail_msg.lower() for sig in HTML_ERROR_SIGNALS) or "404" in fail_msg:
                return (
                    "WRONG ENDPOINT / ROUTE NOT FOUND: The target server responded with 404 / HTML error "
                    f"(`{fail_msg[:120]}`). The endpoint path does NOT exist. DO NOT retry variations "
                    "of this path. Pivot immediately to a different candidate endpoint."
                )
            return f"The script executed correctly but the hypothesis failed. The server responded with: `{fail_msg}`. The expected payload was not reflected or the status code did not match."

    if not stdout and not stderr:
        return "The script produced no output. Ensure it prints the expected [SABRINA-PASS] or [SABRINA-FAIL] blocks."

    # Fallback
    return f"The script completed but did not confirm the hypothesis. Output snippet: `{stdout[:300]}`"

