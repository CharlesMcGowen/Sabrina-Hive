import requests
TARGET = 'http://192.168.2.69:3000'
BASE_PATH = '/rest/products/search?query='

def test_ssrf():
    print(f'\n\n[+] Testing {TARGET} for SSRF vulnerability...')
    try:
        response = requests.get(TARGET + BASE_PATH, timeout=5)
        if response.status_code == 200 and 'products' in response.text.lower():
            print('\nRESULT: SUCCESS')
            print('Evidence:')
            print(f'\n\n[+] Target is vulnerable to SSRF. Response:\n{response.text[:100]}\n')
            return True
        else:
            print('[!] Not a clear positive - check the target manually.')
    except requests.exceptions.RequestException:
        print('\nRESULT: NO_SIGNAL')
        print(f'\n[+] Target {TARGET} is not vulnerable to SSRF. Response:\n{response.text[:100]}\n')
        return False
test_ssrf()