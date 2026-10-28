import base64
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest


MODULE = Path(__file__).resolve().parents[1] / "proxy.py"
SPEC = importlib.util.spec_from_file_location("proxy", MODULE)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)
HOST = "opencode.example.com"
PASSWORD = "test-password-only"
AUTH = "Basic " + base64.b64encode(("opencode:" + PASSWORD).encode()).decode()
NGINX = shutil.which("nginx")
APP_HTML = b'<!doctype html><html><head></head><body><script type="module" src="/app.js"></script></body></html>'


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.spec = {"hostname": HOST, "port": 49374, "proxy_port": 49375}

    def test_loopback_and_authentication(self):
        config = proxy.configuration(self.spec, PASSWORD, "/run/test")
        self.assertIn("listen 127.0.0.1:49375;", config)
        self.assertIn("proxy_pass http://127.0.0.1:49374;", config)
        self.assertIn('Authorization "' + AUTH + '";', config)
        self.assertNotIn(PASSWORD, config)

    def test_invalid_hostnames(self):
        for hostname in ("", "example.com; return 200", "example.com\n", "https://example.com", "a\"b"):
            with self.subTest(hostname=hostname), self.assertRaises(ValueError):
                proxy.configuration(dict(self.spec, hostname=hostname), PASSWORD, "/run/test")

    def test_invalid_ports_or_empty_password(self):
        for changes in ({"port": 80}, {"proxy_port": 65536}, {"proxy_port": 49374}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                proxy.configuration(dict(self.spec, **changes), PASSWORD, "/run/test")
        with self.assertRaises(ValueError):
            proxy.configuration(self.spec, "", "/run/test")


class Backend(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.seen.append(dict(self.headers.items()))
        if self.headers.get("Authorization") != AUTH:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"data: ready\n\n")
            self.wfile.flush()
            self.server.release_stream.wait(5)
            self.close_connection = True
            return
        if self.headers.get("Upgrade") == "websocket":
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.end_headers()
            self.wfile.write(b"upgraded")
            self.wfile.flush()
            self.close_connection = True
            return
        payload = APP_HTML if self.path == "/" else b'{"projects": []}'
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path == "/" else "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@unittest.skipUnless(NGINX, "nginx is required for HTTP integration tests")
class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="opencode-proxy-test-")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.backend = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        cls.backend.daemon_threads = True
        cls.backend.seen = []
        cls.backend.release_stream = threading.Event()
        cls.addClassCleanup(cls.backend.server_close)
        threading.Thread(target=cls.backend.serve_forever, daemon=True).start()
        cls.addClassCleanup(cls.backend.shutdown)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            cls.port = sock.getsockname()[1]
        runtime = Path(cls.directory.name)
        proxy.prepare_assets(runtime)
        config = runtime / "nginx.conf"
        config.write_text(proxy.configuration(
            {"hostname": HOST, "port": cls.backend.server_port, "proxy_port": cls.port}, PASSWORD, runtime
        ))
        config.chmod(0o600)
        cls.error_log = (runtime / "stderr").open("w+")
        cls.addClassCleanup(cls.error_log.close)
        cls.process = subprocess.Popen(
            [NGINX, "-p", str(runtime) + "/", "-c", str(config), "-g", "daemon off;"],
            stdout=subprocess.DEVNULL, stderr=cls.error_log
        )
        cls.addClassCleanup(cls.stop_proxy)
        for _ in range(100):
            if cls.process.poll() is not None:
                cls.error_log.seek(0)
                raise RuntimeError(cls.error_log.read())
            try:
                with socket.create_connection(("127.0.0.1", cls.port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            raise RuntimeError("nginx did not start")

    @classmethod
    def stop_proxy(cls):
        cls.backend.release_stream.set()
        cls.process.terminate()
        try:
            cls.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait(timeout=5)

    def request(self, path="/api/info", headers=None, port=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", port or self.port, timeout=2)
        self.addCleanup(connection.close)
        connection.request(method, path, headers={"Host": HOST, **(headers or {})})
        return connection.getresponse()

    def test_browser_needs_no_native_credential(self):
        response = self.request()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b'{"projects": []}')

    def test_recovery_assets_are_local_no_store_and_contain_no_native_credential(self):
        for path, filename, content_type in (("/sw.js", "service-worker.js", "application/javascript"),
                                              ("/auth/access-refresh", "access-refresh.html", "text/html")):
            with self.subTest(path=path):
                calls = len(self.backend.seen)
                response = self.request(path)
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Cache-Control"), "no-store")
                self.assertTrue(response.getheader("Content-Type").startswith(content_type))
                body = response.read()
                self.assertEqual(body, (MODULE.parent / filename).read_bytes())
                self.assertNotIn(PASSWORD.encode(), body)
                self.assertNotIn(AUTH.encode(), body)
                self.assertEqual(len(self.backend.seen), calls)
                if path == "/sw.js":
                    self.assertEqual(response.getheader("Service-Worker-Allowed"), "/")

    def test_recovery_assets_allow_only_get_and_head(self):
        for path in ("/sw.js", "/auth/access-refresh"):
            response = self.request(path, method="HEAD")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"")
            for method in ("POST", "PUT", "DELETE"):
                with self.subTest(path=path, method=method):
                    calls = len(self.backend.seen)
                    response = self.request(path, method=method)
                    self.assertEqual(response.status, 405)
                    response.read()
                    self.assertEqual(len(self.backend.seen), calls)

    def test_application_html_is_not_rewritten(self):
        response = self.request("/")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), APP_HTML)

    def test_recovery_assets_keep_origin_and_host_checks(self):
        for path in ("/sw.js", "/auth/access-refresh"):
            for headers, status in (({"Origin": "https://attacker.example"}, 403),
                                    ({"Host": "wrong.example"}, 421)):
                with self.subTest(path=path, headers=headers):
                    calls = len(self.backend.seen)
                    response = self.request(path, headers=headers)
                    self.assertEqual(response.status, status)
                    response.read()
                    self.assertEqual(len(self.backend.seen), calls)

    def test_client_authorization_is_replaced(self):
        response = self.request(headers={"Authorization": "Bearer incorrect"})
        self.assertEqual(response.status, 200)
        response.read()
        self.assertEqual(self.backend.seen[-1]["Authorization"], AUTH)

    def test_native_server_still_requires_authentication(self):
        response = self.request(port=self.backend.server_port)
        self.assertEqual(response.status, 401)
        response.read()

    def test_foreign_origin_and_host_never_reach_backend(self):
        for headers, status in (({"Origin": "https://attacker.example"}, 403), ({"Host": "wrong.example"}, 421)):
            with self.subTest(headers=headers):
                calls = len(self.backend.seen)
                response = self.request(headers=headers)
                self.assertEqual(response.status, status)
                response.read()
                self.assertEqual(len(self.backend.seen), calls)

    def test_same_origin_allowed_and_access_assertion_stripped(self):
        response = self.request(headers={"Origin": "https://" + HOST, "Cf-Access-Jwt-Assertion": "dummy"})
        self.assertEqual(response.status, 200)
        response.read()
        self.assertNotIn("Cf-Access-Jwt-Assertion", self.backend.seen[-1])

    def test_event_stream_arrives_before_backend_closes(self):
        self.backend.release_stream.clear()
        try:
            response = self.request("/events")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.readline(), b"data: ready\n")
            self.assertFalse(self.backend.release_stream.is_set())
        finally:
            self.backend.release_stream.set()

    def test_websocket_upgrade_passes_through(self):
        response = self.request(headers={"Upgrade": "websocket", "Connection": "Upgrade"})
        self.assertEqual(response.status, 101)
        self.assertEqual(response.getheader("Upgrade"), "websocket")


if __name__ == "__main__":
    unittest.main()
