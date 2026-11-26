TOOL_NAME = "custom_engine_plugin"

TOOL_PROMPT = """custom_engine_plugin: Runs a specialized custom_engine check.
Format: {"tool": "custom_engine_plugin", "target_url": "<url>", "endpoint": "<path>", "method": "<method>", "headers": {}, "body": {}}
"""

# NOTE: this is a low-value auto-generated stub whose detection logic is largely a placeholder. It is
# kept only because it registers as a live in-loop tool (TOOL_NAME/TOOL_PROMPT/execute). It previously
# sent requests.get/post to an arbitrary caller-supplied target_url with NO scope check at all -- a real
# engagement-scope BYPASS, since a model could point it at any host. It is now fail-closed scope-gated
# through the same trusted-channel guard every other engine uses. Deletion candidate.

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def execute(action_dict, agent_instance):
    import re

    from tools.gate_guarded_request import gate_guarded_request

    # Extract parameters from action_dict
    target_url = action_dict.get('target_url')
    endpoint = action_dict.get('endpoint', '/')
    method = action_dict.get('method', 'GET')
    headers = action_dict.get('headers', {})
    body = action_dict.get('body', None)

    if not target_url:
        return "Error: 'target_url' is required."

    # Construct full URL
    if not target_url.endswith('/'):
        target_url += '/'
    url = target_url + endpoint.lstrip('/')

    if method.upper() not in ('GET', 'POST'):
        return f"Unsupported HTTP method: {method}"

    try:
        # Route through the shared scope gate (fail-closed): an out-of-scope / unauthorized URL is
        # refused here and returns None -- the request is never sent.
        if method.upper() == 'GET':
            response = gate_guarded_request('GET', url, headers=headers, timeout=10)
        else:
            response = gate_guarded_request('POST', url, headers=headers, json=body, timeout=10)

        if response is None:
            return (f"BLOCKED (scope gate): {url} is not within the authorized engagement scope "
                    f"(or no scope is configured). No request was sent.")

        # Define patterns that might indicate custom engine behavior
        custom_engine_patterns = [
            r'custom_engine',
            r'engine_custom',
            r'custom.*engine',
            r'engine.*custom',
            r'eval\s*\(',
            r'exec\s*\(',
            r'javascript:',
            r'vulnerable_to_custom_engine'
        ]

        # Compile patterns for efficiency
        compiled_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in custom_engine_patterns]

        # Check response content
        normalized_text = response.text.lower()
        matches = []

        for pattern in compiled_patterns:
            if pattern.search(normalized_text):
                matches.append(pattern.pattern)

        # Determine result based on findings
        if matches and response.status_code == 200:
            return f"Custom engine vulnerability detected. Matching patterns found: {', '.join(matches)}"
        elif response.status_code == 200:
            return "No custom engine vulnerability detected"
        else:
            return f"Unexpected status code: {response.status_code}"

    except Exception as e:
        return f"An error occurred: {str(e)}"
