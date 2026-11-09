"""Local HTTP integration tests for command redirect and credential isolation."""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
from http_transport import open_http


class HttpTransportTests(unittest.TestCase):
    def setUp(self):
        self.received = []
        received = self.received

        class Target(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_GET

            def log_message(self, *args):
                pass

        target = self.start_server(Target)
        target_url = f"http://127.0.0.1:{target.server_port}/target"

        class Source(BaseHTTPRequestHandler):
            def do_GET(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if self.path == "/ok":
                    self.send_response(200)
                    self.send_header("Content-Length", "17")
                    self.end_headers()
                    self.wfile.write(b'{"success": true}')
                    return
                self.send_response(int(self.path.rsplit("/", 1)[-1]))
                self.send_header("Location", target_url)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = do_GET

            def log_message(self, *args):
                pass

        source = self.start_server(Source)
        self.base_url = f"http://127.0.0.1:{source.server_port}"

    def start_server(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)

        self.addCleanup(cleanup)
        return server

    def test_authenticated_get_and_post_never_follow_redirects(self):
        for method in ("GET", "POST"):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(method=method, status=status):
                    request = urllib.request.Request(
                        f"{self.base_url}/redirect/{status}",
                        data=b"{}" if method == "POST" else None,
                        headers={"Authorization": "Bearer private-test-token"}, method=method,
                    )
                    with self.assertRaises(urllib.error.HTTPError) as raised:
                        open_http(request, timeout=2)
                    self.assertEqual(raised.exception.code, status)
                    raised.exception.close()
        self.assertEqual(self.received, [])

    def test_success_preserves_body_and_does_not_install_global_opener(self):
        with patch.object(urllib.request, "install_opener") as install:
            with open_http(urllib.request.Request(self.base_url + "/ok"), timeout=2) as response:
                self.assertEqual(json.load(response), {"success": True})
        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
