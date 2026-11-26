import requests
TARGET = 'http://192.168.2.69:3000'

def check_vulnerability():
    print(f'Checking {TARGET} for auth bypass vulnerability...')
    try:
        response = requests.get(TARGET, timeout=5)
        if response.status_code == 200:
            print('Vulnerable to Blind Auth Bypass!')
            print(f'Response: {response.text}')
            return True
        else:
            print('Not vulnerable. Response status:', response.status_code)
            return False
    except requests.exceptions.RequestException as e:
        print(f'Error checking vulnerability: {e}')
        return False

if __name__ == '__main__':
    check_vulnerability()