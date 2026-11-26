import requests

def is_vulnerable(url):
    """Check if the endpoint is vulnerable to XSS without causing permanent damage."""
    target = f'{url.rstrip("/")}/api/v1/products/search'
    print(f'Vulnerability check: Testing {target} for CVE-2023-29798...')
    try:
        response = requests.get(target, timeout=5)
        if response.status_code == 200 and 'XSS' in response.text.lower():
            return True
        else:
            print('Not vulnerable. Response:', response.status_code)
    except requests.RequestException:
        pass
    return False

def run_exploit(url):
    """Attempt a targeted reproduction request targeting the vulnerability endpoint."""
    target = f'{url.rstrip("/")}/api/v1/products/search'
    try:
        response = requests.get(target, timeout=5)
        if response.status_code == 200 and 'XSS' in response.text.lower():
            print('Vulnerability confirmed. Response:', response.status_code)
            print(response.text[:300].strip())
    except requests.RequestException:
        pass
if __name__ == '__main__':
    target = 'http://192.168.2.69:3000'
    if is_vulnerable(target):
        run_exploit(target)