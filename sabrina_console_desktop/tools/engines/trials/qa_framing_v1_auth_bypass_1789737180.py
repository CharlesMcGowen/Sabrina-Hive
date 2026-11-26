import requests

# Target URL and parameters for the auth_bypass test
target_url = 'http://192.168.2.69:3000/api/login'
login_data = {'username': 'admin', 'password': 'wrong_password'}
expected_success_signal = '{"status":"error","message":"Incorrect'}  # Expected part of the response for a successful auth bypass

def send_request():
    with requests.Session() as session:
        response = session.post(target_url, data=login_data)
        if expected_success_signal in response.text:
            print('RESULT: SUCCESS - Server responded with expected error message.')
            print(f"Evidence: {response.text}")
        else:
            print('RESULT: NO_SIGNAL - No obvious auth bypass found.')

if __name__ == '__main__':
    send_request()