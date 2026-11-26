import os
import sys

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from tools.gate_guarded_request import is_url_allowed  # noqa: E402


def idor_checker(base_url, session_cookie_name, session_cookie_value, endpoint_templates, target_ids):
    """
    Generalized IDOR checker engine.

    Args:
        base_url (str): The base URL of the target application.
        session_cookie_name (str): The name of the session cookie.
        session_cookie_value (str): The value of the session cookie.
        endpoint_templates (list): List of URL paths with :id placeholder.
        target_ids (list): List of IDs to test against the templates.
    """
    results = []
    session = requests.Session()
    session.cookies.set(session_cookie_name, session_cookie_value)

    for template in endpoint_templates:
        for target_id in target_ids:
            url = f"{base_url}{template.replace(':id', str(target_id))}"

            # SCOPE GATE (fail-closed): never probe a URL outside the session's authorized scope.
            allowed, reason = is_url_allowed(url)
            if not allowed:
                results.append({"url": url, "blocked": True, "reason": reason})
                continue

            # 1. Authenticated request
            try:
                auth_resp = session.get(url)
            except Exception as e:
                results.append({"url": url, "error": str(e)})
                continue

            # 2. Unauthenticated request
            unauth_session = requests.Session()
            try:
                unauth_resp = unauth_session.get(url)
            except Exception as e:
                results.append({"url": url, "error": str(e)})
                continue

            # Logic: if unauth gets 200 and body is similar to auth, flag as potential IDOR
            is_suspicious = (unauth_resp.status_code == 200 and
                              abs(len(auth_resp.text) - len(unauth_resp.text)) < 100)

            results.append({
                "url": url,
                "auth_status": auth_resp.status_code,
                "unauth_status": unauth_resp.status_code,
                "suspicious": is_suspicious
            })

    return results
