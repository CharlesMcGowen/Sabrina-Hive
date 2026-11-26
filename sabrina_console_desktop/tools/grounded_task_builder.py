"""
grounded_task_builder.py
========================
Builds structured probe briefs from observed endpoint metadata.

The brief tells the agentic probe LLM *what* to test and *what shape* the
target has — not *how* to exploit it. The LLM reasons from its own knowledge
(and eventually RAG context) to write the actual exploit code.

No hardcoded payloads, no target-specific assumptions.
"""


def build_grounded_probe_prompt(endpoint: dict, vuln_type: str) -> str:
    """Return a structured probe brief for an (endpoint, vuln_type) pair.

    Args:
        endpoint: Dict with keys: path, method, params, param_type
        vuln_type: One of sqli, xss, idor, auth_bypass, ssrf, xxe,
                   deserialization, session, privilege_escalation,
                   path_traversal, open_redirect, injection, etc.

    Returns:
        A natural-language brief that the agentic probe LLM uses to reason
        about what code to write. Does NOT prescribe specific payloads or URLs
        beyond what is observed from the endpoint shape.
    """
    path       = endpoint.get("path", "/")
    method     = endpoint.get("method", "GET").upper()
    params     = endpoint.get("params", [])
    param_type = endpoint.get("param_type", "unknown")

    # Describe the parameter surface factually
    param_desc_parts = []
    for p in params:
        if param_type == "query":
            param_desc_parts.append(f"query parameter '{p}'")
        elif param_type == "body_json":
            param_desc_parts.append(f"JSON body field '{p}'")
        elif param_type == "path_variable":
            param_desc_parts.append(f"path variable '{p}'")
        else:
            param_desc_parts.append(f"parameter '{p}'")

    param_surface = (
        f"Parameters: {', '.join(param_desc_parts)}."
        if param_desc_parts
        else "No parameters observed during recon (may accept a body or path segments)."
    )

    # Vulnerability-specific context: what property of the endpoint is relevant,
    # and what class of evidence would confirm a finding.
    # This is *not* a payload list — it's the reasoning frame for the LLM.
    VULN_CONTEXT = {
        "sqli": (
            "Test for SQL injection: inject SQL metacharacters into all string-typed "
            "inputs and observe whether the response differs from the baseline in ways "
            "that indicate SQL evaluation (error messages, changed row counts, "
            "authentication bypass, data leakage). "
            "Evidence of a finding: SQL error text in response, unexpected data "
            "returned, or authentication token issued without valid credentials."
        ),
        "xss": (
            "Test for Cross-Site Scripting: inject HTML/JS payloads into all string "
            "inputs and check whether the payload is reflected in the response body "
            "without encoding. "
            "Evidence of a finding: injected payload appears verbatim and unescaped "
            "in the response."
        ),
        "idor": (
            "Test for Insecure Direct Object Reference: enumerate object identifiers "
            "(numeric IDs, UUIDs) in path variables or query parameters and check "
            "whether resources belonging to other users/sessions are returned. "
            "Evidence of a finding: response contains data belonging to a different "
            "user or session than the one authenticated."
        ),
        "auth_bypass": (
            "Test for authentication bypass: attempt to access the endpoint without "
            "credentials, with malformed credentials, or with manipulated tokens. "
            "Also try logic flaws that might grant access without valid auth. "
            "Evidence of a finding: access granted or sensitive data returned without "
            "a valid authenticated session."
        ),
        "ssrf": (
            "Test for Server-Side Request Forgery: supply URL values in any parameter "
            "that the server might fetch (image URLs, webhook URLs, redirect targets, "
            "import endpoints). Point at internal addresses or out-of-band callback "
            "listeners. "
            "Evidence of a finding: internal host data in response, DNS/HTTP callback "
            "received, or error messages revealing internal network topology."
        ),
        "xxe": (
            "Test for XML External Entity injection: if the endpoint accepts XML "
            "or can be coerced into XML parsing, inject an XXE entity referencing "
            "a local file or internal URL. "
            "Evidence of a finding: file contents or internal response data appears "
            "in the server response."
        ),
        "deserialization": (
            "Test for unsafe deserialization: if the endpoint accepts serialized "
            "objects (JSON with __proto__, pickle, Java serialized bytes, etc.), "
            "send crafted payloads that would trigger execution or error stack traces "
            "revealing the deserialization library. "
            "Evidence of a finding: stack trace from a deserialization library, "
            "prototype pollution reflected in a later response, or RCE indicators."
        ),
        "session": (
            "Test for session management weaknesses: examine any tokens issued by "
            "this endpoint for weak signing algorithms (alg:none, HS256 with guessable "
            "secret), missing expiry, or predictable values. Attempt to forge or "
            "tamper a token and re-submit. "
            "Evidence of a finding: forged/tampered token accepted by the server."
        ),
        "privilege_escalation": (
            "Test for privilege escalation: authenticate as a low-privilege user and "
            "attempt operations that should require higher privilege — modifying "
            "another user's data, accessing admin-only endpoints, or changing role "
            "attributes in writable fields. "
            "Evidence of a finding: operation succeeds that should be forbidden for "
            "the authenticated user's role."
        ),
        "path_traversal": (
            "Test for path traversal: inject '../' sequences (URL-encoded and raw) "
            "into path variables, query parameters, or filenames to attempt to read "
            "files outside the intended directory. "
            "Evidence of a finding: contents of a file outside the web root appear "
            "in the response (e.g. /etc/passwd, application source files)."
        ),
        "open_redirect": (
            "Test for open redirect: inject external URLs into any redirect-related "
            "parameters (to, url, redirect, continue, next, return, etc.). Follow "
            "the redirect chain and check the final destination. "
            "Evidence of a finding: server issues a 3xx redirect to an attacker-controlled "
            "external domain."
        ),
        "injection": (
            "Test for injection flaws: inject NoSQL operators, template expressions, "
            "OS command separators, or LDAP metacharacters into all string inputs "
            "and observe whether the response indicates server-side evaluation. "
            "Evidence of a finding: evaluated expression result in response, OS "
            "command output, or NoSQL operator bypass granting unintended access."
        ),
    }

    vuln_ctx = VULN_CONTEXT.get(
        vuln_type,
        (
            f"Test for {vuln_type}: probe all available inputs with relevant payloads "
            "and observe whether the response indicates a vulnerability. "
            "Evidence of a finding: unexpected behavior that deviates from the baseline "
            "in a way consistent with the vulnerability class."
        ),
    )

    brief = (
        f"Target: {method} {path}\n"
        f"{param_surface}\n"
        f"Vulnerability class: {vuln_type}\n\n"
        f"{vuln_ctx}\n\n"
        "Write a self-contained Python 3 script using only urllib.request that:\n"
        "1. Sends one or more targeted probe requests to the target URL.\n"
        "2. Evaluates the response for evidence of the vulnerability.\n"
        "3. Prints SABRINA_FINDING:<json> (with keys: vuln_type, url, severity, "
        "payload, evidence, description) if a vulnerability is confirmed.\n"
        "4. Prints SABRINA_RESULT: no_finding if no vulnerability is found.\n"
        "Do not use any third-party libraries. Do not use subprocess or os.system.\n"
        "The script must terminate within 30 seconds."
    )

    return brief


if __name__ == "__main__":
    # Example: show what the LLM receives for a login endpoint + sqli
    ep = {
        "path": "/rest/user/login",
        "method": "POST",
        "params": ["email", "password"],
        "param_type": "body_json",
    }
    print(build_grounded_probe_prompt(ep, "sqli"))
    print("\n" + "=" * 60 + "\n")
    print(build_grounded_probe_prompt(ep, "auth_bypass"))