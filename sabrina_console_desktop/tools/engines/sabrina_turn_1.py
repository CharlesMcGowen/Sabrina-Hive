import json

def process_command_line(json_input):
    params = json.loads(json_input)

    if 'url' not in params or 'method' not in params or 'endpoint' not in params:
        return {'error': 'Invalid input. JSON must contain "url", "method", and "endpoint".'}

    target_url = params['url']
    method = params['method'].upper()
    endpoint = params.get('endpoint', '')

    try:
        response = requests.request(method, f"{target_url}{endpoint}")

        if str(response.status_code).startswith('200'):
            return {
                'confirmed': True,
                'successful_payload': "Payload successfully sent",
                'evidence': response.content.decode()
            }
        else:
            return {'error': f"Request to {method} {target_url}{endpoint} failed with status: {response.status_code}"}
    except Exception as e:
        return {'error': str(e)}

if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python script.py <json_input>")
        sys.exit(1)

    json_input = sys.argv[1]
    
    result = process_command_line(json_input)
    print(json.dumps(result))