"""Production WSGI entrypoint; trust exactly one private reverse-proxy peer."""
import ipaddress
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def server_options():
    proxy = os.environ.get("OSEE_TRUSTED_PROXY_IP", "")
    if not proxy:
        raise RuntimeError("OSEE_TRUSTED_PROXY_IP must name the reverse proxy's exact IP.")
    ipaddress.ip_address(proxy)
    return {"listen": "0.0.0.0:8000", "threads": 8,
            "trusted_proxy": proxy, "trusted_proxy_count": 1,
            "trusted_proxy_headers": {"x-forwarded-for", "x-forwarded-proto"},
            "clear_untrusted_proxy_headers": True,
            "max_request_body_size": 12 * 1024 * 1024,
            "channel_timeout": 60, "ident": "OSEE"}


if __name__ == "__main__":
    from django.core.wsgi import get_wsgi_application
    from waitress import serve
    serve(get_wsgi_application(), **server_options())
