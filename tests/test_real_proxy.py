"""Integration tests: start real uvicorn proxy and verify all endpoints."""

import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest


ADMIN_KEY = "test-admin-key"
PROXY_PORT = None
DB_PATH = None
CONFIG_PATH = None
UVICORN_PROC = None
BASE_URL = None


def _find_free_port():
    """Find a free TCP port."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_proxy(port, db_path, config_path):
    """Start uvicorn process and wait for it to be ready."""
    env = os.environ.copy()
    env["ADMIN_KEY"] = ADMIN_KEY
    env["PROXY_PORT"] = str(port)
    env["DB_PATH"] = str(db_path)
    env["CONFIG_PATH"] = str(config_path)

    project_dir = Path(__file__).resolve().parent.parent

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
        env=env,
        cwd=str(project_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Wait for proxy to be ready
    timeout = 15
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            if resp.status_code == 200 and resp.json()["status"] == "ok":
                return proc
        except Exception:
            pass
        time.sleep(0.5)

    proc.kill()
    raise RuntimeError(f"Proxy failed to start on port {port} within {timeout}s")


def _stop_proxy(proc):
    """Stop uvicorn process."""
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@pytest.fixture(scope="session")
def proxy_setup():
    """Start real uvicorn proxy for integration tests."""
    global PROXY_PORT, DB_PATH, CONFIG_PATH, UVICORN_PROC, BASE_URL

    PROXY_PORT = _find_free_port()
    DB_PATH = str(Path(tempfile.mkdtemp()) / "test_proxy.db")
    CONFIG_PATH = str(Path(__file__).parent / "test_config.yaml")
    BASE_URL = f"http://127.0.0.1:{PROXY_PORT}"

    UVICORN_PROC = _start_proxy(PROXY_PORT, DB_PATH, CONFIG_PATH)

    yield {"port": PROXY_PORT, "url": BASE_URL}

    _stop_proxy(UVICORN_PROC)
    # Clean up temp dir
    import shutil

    db_dir = Path(DB_PATH).parent
    if db_dir.exists():
        shutil.rmtree(db_dir)


@pytest.fixture
def client(proxy_setup):
    """Create httpx client for proxy tests."""
    return httpx.Client(base_url=BASE_URL, timeout=10)


class TestAdminServers:
    """Test server CRUD endpoints."""

    def test_create_server(self, client):
        uid = uuid.uuid4().hex[:8]
        resp = client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"test-server-{uid}", "url": "http://127.0.0.1:9999"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "id" in data

    def test_list_servers(self, client):
        resp = client.get("/admin/servers", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        servers = resp.json()
        assert isinstance(servers, list)

    def test_delete_server(self, client):
        uid = uuid.uuid4().hex[:8]
        create_resp = client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"delete-me-{uid}", "url": "http://127.0.0.1:9998"},
        )
        server_id = create_resp.json()["id"]

        delete_resp = client.delete(
            f"/admin/servers/{server_id}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        assert delete_resp.status_code == 200
        assert delete_resp.json()["ok"] is True

    def test_update_server(self, client):
        uid = uuid.uuid4().hex[:8]
        create_resp = client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"update-me-{uid}", "url": "http://127.0.0.1:9997"},
        )
        server_id = create_resp.json()["id"]

        update_resp = client.patch(
            f"/admin/servers/{server_id}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"url": "http://127.0.0.1:9996", "api_key": "new-key"},
        )
        assert update_resp.status_code == 200
        assert update_resp.json()["ok"] is True

    def test_auth_required(self, client):
        resp = client.get("/admin/servers")
        assert resp.status_code == 401


class TestAdminModels:
    """Test server model assignment endpoints."""

    def test_assign_model(self, client):
        uid = uuid.uuid4().hex[:8]
        server_resp = client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"model-server-{uid}", "url": "http://127.0.0.1:9995"},
        )
        server_id = server_resp.json()["id"]

        model_name = f"test-model-{uid}.gguf"
        resp = client.post(
            f"/admin/servers/{server_id}/models",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"model_name": model_name},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_unassign_model(self, client):
        uid = uuid.uuid4().hex[:8]
        server_resp = client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"model-unassign-{uid}", "url": "http://127.0.0.1:9994"},
        )
        server_id = server_resp.json()["id"]

        model_name = f"unassign-model-{uid}.gguf"
        client.post(
            f"/admin/servers/{server_id}/models",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"model_name": model_name},
        )

        resp = client.delete(
            f"/admin/servers/{server_id}/models/{model_name}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestAdminAliases:
    """Test model alias endpoints."""

    def test_create_alias(self, client):
        uid = uuid.uuid4().hex[:8]
        resp = client.post(
            "/admin/aliases",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"alias_name": f"alias-{uid}", "real_model_name": f"real-{uid}.gguf"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_list_aliases(self, client):
        resp = client.get("/admin/aliases", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        aliases = resp.json()
        assert isinstance(aliases, list)

    def test_delete_alias(self, client):
        uid = uuid.uuid4().hex[:8]
        client.post(
            "/admin/aliases",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"alias_name": f"del-alias-{uid}", "real_model_name": f"real-del-{uid}.gguf"},
        )

        resp = client.delete(
            f"/admin/aliases/del-alias-{uid}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestAdminKeys:
    """Test API key management endpoints."""

    def test_create_key(self, client):
        resp = client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "test-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "key" in data
        assert "name" in data

    def test_list_keys(self, client):
        resp = client.get("/admin/keys", headers={"Authorization": f"Bearer {ADMIN_KEY}"})
        assert resp.status_code == 200
        keys = resp.json()
        assert isinstance(keys, list)

    def test_delete_key(self, client):
        create_resp = client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "delete-me"},
        )
        key_id = create_resp.json()["id"]

        resp = client.delete(
            f"/admin/keys/{key_id}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_toggle_key(self, client):
        create_resp = client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "toggle-me"},
        )
        key_id = create_resp.json()["id"]

        resp = client.patch(
            f"/admin/keys/{key_id}/toggle",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"active": False},
        )
        assert resp.status_code == 200

    def test_auth_required(self, client):
        resp = client.post("/admin/keys", json={"name": "test"})
        assert resp.status_code == 401


class TestAdminUsage:
    """Test usage logging endpoint."""

    def test_get_usage(self, client):
        resp = client.get(
            "/admin/usage",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "logs" in data
        assert "summary" in data


class TestProxyEndpoints:
    """Test proxy endpoints with mock backend."""

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "active_servers" in data

    def test_chat_completion_auth_required(self, client):
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 401

    def test_chat_completion_invalid_key(self, client):
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer invalid-key"},
            json={"model": "test", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 403

    def test_chat_completion_no_server(self, client):
        # Create a valid key first
        create_resp = client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "chat-test"},
        )
        user_key = create_resp.json()["key"]

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": "nonexistent-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 404

    def test_completions_auth_required(self, client):
        resp = client.post("/v1/completions", json={"prompt": "test"})
        assert resp.status_code == 401

    def test_embeddings_auth_required(self, client):
        resp = client.post("/v1/embeddings", json={"input": "test"})
        assert resp.status_code == 401

    def test_models_endpoint(self, client):
        # This endpoint doesn't require auth but needs active servers
        resp = client.get("/v1/models")
        # Should return 200 with models list or 503 if no servers connected
        assert resp.status_code in [200, 503]


class TestAdminAuth:
    """Test admin endpoint authentication."""

    def test_wrong_admin_key(self, client):
        resp = client.get(
            "/admin/servers",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 403

    def test_missing_auth_header(self, client):
        resp = client.get("/admin/servers")
        assert resp.status_code == 401

    def test_invalid_auth_format(self, client):
        resp = client.get(
            "/admin/servers",
            headers={"Authorization": "Basic some-token"},
        )
        assert resp.status_code == 401
