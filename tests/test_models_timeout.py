"""Tests for /v1/models timeout and shared client split timeouts."""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _HealthyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "healthy-model", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


class TestModelsTimeout:
    """Test MODELS_TIMEOUT fan-out behaviour."""

    def test_models_skips_hanging_server(self, client, admin_key, monkeypatch):
        # Healthy upstream via ThreadingHTTPServer
        healthy = ThreadingHTTPServer(("127.0.0.1", 0), _HealthyHandler)
        healthy_thread = threading.Thread(target=healthy.serve_forever, daemon=True)
        healthy_thread.start()
        healthy_port = healthy.server_address[1]
        healthy_url = f"http://127.0.0.1:{healthy_port}"

        # Hanging upstream via raw socket — accepts and keeps connections alive
        hang_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        hang_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        hang_sock.bind(("127.0.0.1", 0))
        hang_sock.listen(1)
        hang_port = hang_sock.getsockname()[1]
        hang_url = f"http://127.0.0.1:{hang_port}"

        accepted_conns = []

        def _hang_server():
            while True:
                try:
                    conn, _ = hang_sock.accept(timeout=5)
                    accepted_conns.append(conn)
                except Exception:
                    break

        hang_thread = threading.Thread(target=_hang_server, daemon=True)
        hang_thread.start()

        from smol_llm_proxy.database import get_db

        try:
            with get_db() as conn:
                conn.execute(
                    "INSERT INTO servers (name, url, api_key, active) VALUES (?, ?, ?, 1)",
                    (f"healthy-{id(client)}", healthy_url, ""),
                )
                conn.execute(
                    "INSERT INTO servers (name, url, api_key, active) VALUES (?, ?, ?, 1)",
                    (f"hanging-{id(client)}", hang_url, ""),
                )

            monkeypatch.setattr("smol_llm_proxy.proxy.MODELS_TIMEOUT", 0.5)

            start = time.monotonic()
            resp = client.get("/v1/models", headers={"Authorization": f"Bearer {admin_key}"})
            elapsed = time.monotonic() - start

            assert resp.status_code == 200
            assert elapsed < 3.0
            data = resp.json()
            model_ids = [m["id"] for m in data.get("data", [])]
            assert "healthy-model" in model_ids
        finally:
            with get_db() as conn:
                conn.execute("DELETE FROM servers WHERE name LIKE 'healthy-%' OR name LIKE 'hanging-%'")
            for c in accepted_conns:
                try:
                    c.close()
                except Exception:
                    pass
            hang_sock.close()
            healthy.shutdown()
            healthy.server_close()


class TestSplitTimeouts:
    """Test that shared client uses split timeout config."""

    def test_shared_client_split_timeouts(self, monkeypatch):
        import smol_llm_proxy.proxy as proxy

        monkeypatch.setattr(proxy, "HTTPX_TIMEOUT", 77.0)
        monkeypatch.setattr(proxy, "HTTPX_CONNECT_TIMEOUT", 3.0)
        monkeypatch.setattr(proxy, "_httpx_client", None)

        c = proxy.get_httpx_client()
        assert c.timeout.connect == 3.0
        assert c.timeout.read == 77.0
        assert c.timeout.write == 77.0
        assert c.timeout.pool == 77.0
