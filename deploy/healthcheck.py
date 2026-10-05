"""Container health check: GET a URL on this container and exit 0 only on HTTP 200.

    python /app/deploy/healthcheck.py http://127.0.0.1:8000/healthz

It sends the first ALLOWED_HOSTS name as the Host (Django refuses an unknown one) and says it came over https
(SECURE_SSL_REDIRECT would answer 301 otherwise); redirects are not followed.
"""

import http.client
import os
import sys
from urllib.parse import urlsplit


def first_host() -> str:
    for name in os.environ.get("ALLOWED_HOSTS", "").split(","):
        name = name.strip().lstrip(".")
        if name and name != "*":
            return name
    return "localhost"


def main(url: str) -> int:
    parts = urlsplit(url)
    try:
        connection = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=5)
        headers = {"Host": first_host(), "X-Forwarded-Proto": "https"}
        connection.request("GET", parts.path or "/", headers=headers)
        status = connection.getresponse().status
    except Exception as exc:  # noqa: BLE001 - any failure is "unhealthy"
        print(f"healthcheck {url}: {exc}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"healthcheck {url}: HTTP {status}", file=sys.stderr)
    return 0 if status == 200 else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000/healthz"))
