#!/usr/bin/env python3
"""Authenticate the same-origin browser to OpenCode after the tunnel validates Cloudflare Access.

The upstream password remains in a private runtime file, never HTML, browser storage, URLs or argv. nginx carries HTTP
streams and WebSockets. The proxy listens on loopback for a same-host Cloudflare Tunnel connector. The native server also stays
on loopback and still requires its own credential.
"""
import base64
import json
import os
from pathlib import Path


def prepare_assets(runtime):
    for name in ("service-worker.js", "access-refresh.html"):
        path = Path(runtime) / name
        path.write_bytes((Path(__file__).resolve().parent / name).read_bytes())
        path.chmod(0o600)


def configuration(spec, password, runtime):
    hostname = spec["hostname"]
    if not hostname or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in hostname):
        raise ValueError("invalid OpenCode hostname")
    port, upstream = int(spec["proxy_port"]), int(spec["port"])
    if not (1024 <= port <= 65535 and 1024 <= upstream <= 65535 and port != upstream and password):
        raise ValueError("invalid OpenCode proxy configuration")
    auth = base64.b64encode(("opencode:" + password).encode()).decode()
    return f'''pid {runtime}/nginx.pid;
error_log stderr warn;
events {{ worker_connections 128; }}
http {{
    access_log off;
    map $http_upgrade $connection_upgrade {{ default upgrade; '' close; }}
    map $http_origin $origin_allowed {{ default 0; '' 1; "https://{hostname}" 1; }}
    client_body_temp_path {runtime}/body;
    proxy_temp_path {runtime}/proxy;
    server {{
        listen 127.0.0.1:{port};
        server_name {hostname};
        allow 127.0.0.1;
        deny all;
        if ($host != "{hostname}") {{ return 421; }}
        if ($origin_allowed = 0) {{ return 403; }}
        client_max_body_size 32m;
        location = /sw.js {{
            if ($request_method !~ "^(GET|HEAD)$") {{ return 405; }}
            default_type application/javascript;
            add_header Cache-Control "no-store" always;
            add_header Service-Worker-Allowed "/" always;
            alias {runtime}/service-worker.js;
        }}
        location = /auth/access-refresh {{
            if ($request_method !~ "^(GET|HEAD)$") {{ return 405; }}
            default_type text/html;
            add_header Cache-Control "no-store" always;
            alias {runtime}/access-refresh.html;
        }}
        location / {{
            proxy_pass http://127.0.0.1:{upstream};
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header Authorization "Basic {auth}";
            proxy_set_header Cf-Access-Jwt-Assertion "";
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;
            proxy_buffering off;
            proxy_request_buffering off;
            proxy_read_timeout 3600s;
        }}
    }}
}}
'''


def main():
    os.umask(0o077)
    directory = Path.home() / ".config/opencode"
    spec = json.loads((directory / "cloudflare.json").read_text())
    password = json.loads((directory / "service.json").read_text())["password"]
    runtime = Path(os.environ["RUNTIME_DIRECTORY"])
    prepare_assets(runtime)
    path = runtime / "nginx.conf"
    path.write_text(configuration(spec, password, runtime))
    path.chmod(0o600)
    os.execv("/usr/sbin/nginx", ["nginx", "-p", str(runtime) + "/", "-c", str(path), "-g", "daemon off;"])


if __name__ == "__main__":
    main()
