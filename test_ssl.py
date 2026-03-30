import requests
try:
    r = requests.get("https://oauth2.googleapis.com/token", timeout=10)
    print("Status:", r.status_code)
    print("Text:", r.text)
except Exception as e:
    print("Error:", e)
