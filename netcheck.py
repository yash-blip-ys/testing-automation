import urllib.request

url = "http://172.17.100.36:8184/scm"
try:
    response = urllib.request.urlopen(url, timeout=10)
    print(f"SUCCESS - Site is reachable. Status: {response.status}")
except Exception as e:
    print(f"FAILED - Cannot reach site: {e}")