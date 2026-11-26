import json
import re
import sys
import requests

def search_sabrina_knowledge_base(query):
    # This is a placeholder for the actual function implementation that would query a technical knowledge base.
    pass


class GeneratedToolEngine:
    def __init__(self, config_json):
        self.config = config_json
        self.payloads = [
            {"key": "body"},
            {"key": "query_params"},
            {"key": "headers"}
        ]

    def test_vulnerability(self, payload, params=None):
        # Test the vulnerability by sending a request with the specified payload.
        try:
            response = requests.request(
                method=self.config['method'],
                url=self.config['target_url'] + self.config['endpoint'],
                headers=params.get('headers', {}),
                data=payload
            )
            
            status_code = response.status_code
            content = response.text
            
            # Define regex patterns for different types of errors or responses indicating a vulnerability.
            blocked_prefix_pattern = re.compile(r"Blocked|Invalid")
            unexpected_status_pattern = re.compile(r"[5][0-9]{2}")
            unexpected_response_format = re.compile(r"Unexpected format: (.+)")
            
            if blocked_prefix_pattern.search(content) or \
               unexpected_status_pattern.match(str(status_code)) or \
               unexpected_response_format.search(content):
                return True
        except Exception as e:
            print(f"Error during request: {e}")
        
        return False

    def main(self):
        config_json = self.config
        
        if not isinstance(config_json, dict) or 'target_url' not in config_json or \
           'endpoint' not in config_json or 'method' not in config_json:
            print("Invalid configuration JSON.")
            sys.exit(1)

        evidence = []
        
        for payload_type in self.payloads:
            key = payload_type['key']
            
            if 'body' == key and isinstance(config_json.get('data'), dict):
                # Assuming 'config_json["data"]' is a dictionary to use as body content.
                payload_content = json.dumps(config_json['data'])
            elif 'query_params' in config_json:
                # Convert query parameters to string for testing.
                query_params_str = '&'.join(f"{k}={v}" for k, v in config_json.get('query_params', {}).items())
                if not query_params_str and key == 'query_params':
                    continue  # Skip empty query params
            else:
                payload_content = ''  # No payload specified
            
            test_payloads = [
                (payload_content, {"headers": config_json['headers']}),
                ("XSS_test", {"headers": config_json['headers']})
            ]
            
            for payload, headers in test_payloads:
                if self.test_vulnerability(payload, headers):
                    evidence.append({
                        "payload_key": key,
                        "payload_value": payload_content,
                        "response_status_code": response.status_code
                    })
        
        output = {
            "confirmed": len(evidence) > 0,
            "evidence": evidence
        }
        print(json.dumps(output))
        return

if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python generated_tool_engine.py <config_json>")
    
    try:
        config_json = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(f"Invalid JSON format: {e}")
        sys.exit(1)
    
    engine = GeneratedToolEngine(config_json)
    engine.main()