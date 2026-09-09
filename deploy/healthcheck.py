"""Container-local check; no public endpoint or secret is needed."""
import os
import sys
import urllib.request

try:
    host = os.environ["DJANGO_ALLOWED_HOSTS"].split(",")[0].strip()
    request = urllib.request.Request("http://127.0.0.1:8000/ready/", headers={"Host": host})
    with urllib.request.urlopen(request, timeout=8) as response:
        valid = response.status == 200 and b'"ready"' in response.read(1024)
except Exception:
    valid = False
sys.exit(0 if valid else 1)
