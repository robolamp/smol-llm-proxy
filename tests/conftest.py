"""Fixtures for smol-llm-proxy tests."""

import os
import tempfile
import uuid
import pytest

from fastapi.testclient import TestClient

os.environ["ADMIN_KEY"] = "test-admin-key"
os.environ["PROXY_PORT"] = "8099"


def _load_test_servers():
    """Load llama-server URLs from YAML config."""
    try:
        import yaml

        with open("tests/test_servers.yaml") as f:
            data = yaml.safe_load(f)
        return {s["port"]: s["url"] for s in data.get("servers", [])}
    except (FileNotFoundError, ImportError):
        # Fallback to hardcoded defaults if YAML not found or PyYAML missing
        return {
            8080: os.environ.get("TEST_SERVER_1", "http://host:port"),
            8083: os.environ.get("TEST_SERVER_2", "http://host:port"),
        }


_test_servers = _load_test_servers()


@pytest.fixture(scope="session")
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    if os.path.exists(path):
        os.unlink(path)


@pytest.fixture(scope="session", autouse=True)
def env_setup(db_path):
    os.environ["DB_PATH"] = db_path
    from smol_llm_proxy.database import init_db

    init_db()
    yield


@pytest.fixture(autouse=True)
def _reset_http_client():
    """Reset shared httpx client after each test to avoid event loop issues."""
    yield
    import asyncio
    import smol_llm_proxy.proxy

    if smol_llm_proxy.proxy._httpx_client is None:
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        smol_llm_proxy.proxy._httpx_client = None
        return

    if loop.is_closed():
        smol_llm_proxy.proxy._httpx_client = None
        return

    try:
        if not loop.is_running():
            loop.run_until_complete(smol_llm_proxy.proxy._httpx_client.aclose())
        else:
            loop.run_until_complete(smol_llm_proxy.proxy._httpx_client.aclose())
    except Exception:
        pass
    finally:
        smol_llm_proxy.proxy._httpx_client = None

    import smol_llm_proxy.metrics as _m

    if _m._usage_queue is not None:
        batch = _m._drain_queue()
        if batch:
            _m._flush_batch_sync(batch)
    _m._usage_queue = None
    _m._logger_task = None


@pytest.fixture(scope="session")
def client(db_path):
    from smol_llm_proxy.main import app

    return TestClient(app)


@pytest.fixture(scope="function")
def admin_key(client):
    from smol_llm_proxy.auth import create_api_key

    result = create_api_key("test-user")
    yield result["key"]
    import smol_llm_proxy.metrics as _m

    if _m._usage_queue is not None:
        batch = _m._drain_queue()
        if batch:
            _m._flush_batch_sync(batch)
    from smol_llm_proxy.database import get_db

    with get_db() as conn:
        key_id = conn.execute("SELECT id FROM api_keys WHERE name = ?", ("test-user",)).fetchone()
        if key_id:
            conn.execute("DELETE FROM usage_logs WHERE key_id = ?", (key_id["id"],))
            conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id["id"],))


@pytest.fixture(scope="function")
def server_setup(client):
    """Create a test server with unique name. Returns server info dict."""
    uid = uuid.uuid4().hex[:8]
    resp = client.post(
        "/admin/servers",
        headers={"Authorization": "Bearer test-admin-key"},
        json={"name": f"test-server-{uid}", "url": list(_test_servers.values())[0]},
    )
    assert resp.status_code == 200, f"Failed to create server: {resp.json()}"
    server_id = resp.json()["id"]
    return {"id": server_id, "name": f"test-server-{uid}", "url": list(_test_servers.values())[0]}


@pytest.fixture(scope="function")
def server_with_model(server_setup, client):
    """Create a test server with a model assigned."""
    model_name = f"test-model-{server_setup['id']}.gguf"
    resp = client.post(
        f"/admin/servers/{server_setup['id']}/models",
        headers={"Authorization": "Bearer test-admin-key"},
        json={"model_name": model_name},
    )
    assert resp.status_code == 200 or resp.status_code == 409
    server_setup["model_name"] = model_name
    return server_setup
