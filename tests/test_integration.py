"""Integration tests: full flow with real llama-server backends."""

import os

import pytest
import httpx
import threading
import time
import yaml


def _load_test_servers():
    """Load llama-server URLs from YAML config."""
    try:
        import yaml

        with open("tests/test_servers.yaml") as f:
            data = yaml.safe_load(f)
        return {s["port"]: s["url"] for s in data.get("servers", [])}
    except (FileNotFoundError, ImportError):
        return {8080: "http://127.0.0.1:8080", 8083: "http://127.0.0.1:8083"}


_test_servers = _load_test_servers()


@pytest.fixture(scope="session")
def available_servers():
    """Discover real llama-servers and their models."""
    servers = {}
    for port, url in _test_servers.items():
        try:
            with httpx.Client(timeout=5) as c:
                resp = c.get(f"{url}/v1/models")
                if resp.status_code == 200:
                    models = [m["id"] for m in resp.json().get("data", [])]
                    servers[port] = {"url": url, "models": models}
        except Exception:
            pass
    return servers


@pytest.fixture(scope="function")
def integration_key(client):
    """Create a dedicated key for integration tests."""
    import uuid
    from smol_llm_proxy.auth import create_api_key
    from smol_llm_proxy.database import get_db

    name = f"integration-tester-{uuid.uuid4().hex[:8]}"
    result = create_api_key(name)
    yield result["key"]
    import time

    time.sleep(2)

    with get_db() as conn:
        key_id = conn.execute("SELECT id FROM api_keys WHERE name = ?", (name,)).fetchone()
        if key_id:
            conn.execute("DELETE FROM usage_logs WHERE key_id = ?", (key_id["id"],))
            conn.execute("DELETE FROM rate_limits WHERE key_id = ?", (key_id["id"],))
            conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id["id"],))


@pytest.fixture(scope="session", autouse=True)
def setup_integration_servers(client, available_servers):
    """Create server entries and assign models for integration tests."""
    if not available_servers:
        return

    for port, info in available_servers.items():
        uid = f"port-{port}"
        resp = client.post(
            "/admin/servers",
            headers={"Authorization": "Bearer test-admin-key"},
            json={"name": uid, "url": info["url"]},
        )
        if resp.status_code == 200 or resp.status_code == 409:
            sid = resp.json().get("id") if resp.status_code == 200 else None
            if not sid:
                servers_resp = client.get(
                    "/admin/servers",
                    headers={"Authorization": "Bearer test-admin-key"},
                )
                for s in servers_resp.json():
                    if s["name"] == uid:
                        sid = s["id"]
                        break

            if sid:
                for model in info["models"]:
                    client.post(
                        f"/admin/servers/{sid}/models",
                        headers={"Authorization": "Bearer test-admin-key"},
                        json={"model_name": model},
                    )


@pytest.fixture(scope="session")
def proxy_http_url(available_servers):
    """Start the proxy server in a background thread for streaming tests.

    TestClient works for non-streaming (synthetic), but streaming needs a real HTTP endpoint.
    This fixture starts uvicorn in a thread and yields the base URL.
    """
    import tempfile as _tempfile
    import uvicorn
    from smol_llm_proxy.config import PROXY_HOST, PROXY_PORT
    from smol_llm_proxy.main import app

    # Generate a test config.yaml with the available servers so sync_config
    # doesn't delete the servers created via the admin API.
    test_config_path = _tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False).name
    servers_cfg = []
    for port, info in available_servers.items():
        servers_cfg.append(
            {
                "name": f"port-{port}",
                "url": info["url"],
                "models": info["models"],
            }
        )
    with open(test_config_path, "w") as f:
        yaml.dump({"servers": servers_cfg, "aliases": {}}, f)

    old_config_path = os.environ.get("CONFIG_PATH")
    os.environ["CONFIG_PATH"] = test_config_path

    config = uvicorn.Config(app=app, host=PROXY_HOST, port=PROXY_PORT, log_level="error")
    server = uvicorn.Server(config)

    def run():
        server.run()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # wait for server to start
    import httpx as _httpx

    for _ in range(20):
        try:
            r = _httpx.get(f"http://127.0.0.1:{PROXY_PORT}/health", timeout=1)
            if r.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError(f"Proxy failed to start on port {PROXY_PORT}")

    yield f"http://127.0.0.1:{PROXY_PORT}"

    server.should_exit = True
    t.join(timeout=5)

    from smol_llm_proxy.rate_limiter import stop_rate_flush

    stop_rate_flush()

    os.environ.pop("CONFIG_PATH", None)
    if old_config_path is not None:
        os.environ["CONFIG_PATH"] = old_config_path

    try:
        os.unlink(test_config_path)
    except FileNotFoundError:
        pass

    import smol_llm_proxy.proxy

    smol_llm_proxy.proxy._httpx_client = None


class TestNonStreaming:
    def test_chat_completion_full_flow(self, client, integration_key, available_servers):
        """Full cycle: proxy → llama-server → response with tokens."""
        if 8080 not in available_servers:
            pytest.skip("No llama-server on port 8080")

        model = available_servers[8080]["models"][0]

        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {integration_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "choices" in data
        assert len(data["choices"]) > 0
        assert "message" in data["choices"][0]
        assert "usage" in data
        assert data["usage"]["total_tokens"] > 0

    def test_embedding_full_flow(self, client, integration_key, available_servers):
        """Full cycle: proxy → llama-server embedding endpoint."""
        if 8083 not in available_servers:
            pytest.skip("No llama-server on port 8083")

        model = available_servers[8083]["models"][0]

        resp = client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {integration_key}"},
            json={
                "model": model,
                "input": "hello world",
            },
        )

        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data
        assert len(data["data"]) > 0
        assert "embedding" in data["data"][0]
        assert len(data["data"][0]["embedding"]) > 0


class TestStreaming:
    def test_streaming_full_flow(self, integration_key, available_servers, proxy_http_url):
        """Full cycle: proxy → llama-server streaming → SSE chunks.

        Uses httpx.Client to hit the real HTTP endpoint because TestClient doesn't support streaming.
        """
        if 8080 not in available_servers:
            pytest.skip("No llama-server on port 8080")

        model = available_servers[8080]["models"][0]

        with httpx.Client(base_url=proxy_http_url, timeout=30) as c:
            resp = c.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {integration_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "say hello"}],
                    "stream": True,
                },
            )

        chunks = list(resp.iter_lines())
        # Should have multiple SSE data lines + [DONE]
        assert len(chunks) >= 2
        done_chunks = [c for c in chunks if "[DONE]" in c]
        assert len(done_chunks) == 1

        import smol_llm_proxy.metrics as _m

        if _m._usage_queue is not None:
            batch = _m._drain_queue()
            if batch:
                _m._flush_batch_sync(batch)


class TestMultiServerRouting:
    def test_requests_go_to_correct_server(self, client, integration_key, available_servers):
        """Chat goes to server with chat model, embeddings to embedding server."""
        if 8080 not in available_servers or 8083 not in available_servers:
            pytest.skip("Need both llama-servers")

        chat_model = available_servers[8080]["models"][0]
        emb_model = available_servers[8083]["models"][0]

        # Chat completion should go through server on 8080
        resp_chat = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {integration_key}"},
            json={
                "model": chat_model,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp_chat.status_code == 200

        # Embedding should go through server on 8083
        resp_emb = client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {integration_key}"},
            json={
                "model": emb_model,
                "input": "hello",
            },
        )
        assert resp_emb.status_code == 200


class TestUsageLogging:
    def test_nonstreaming_logged(self, client, integration_key, available_servers):
        """Non-streaming request logs tokens + timings."""
        if 8080 not in available_servers:
            pytest.skip("No llama-server on port 8080")

        model = available_servers[8080]["models"][0]

        client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {integration_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

        import smol_llm_proxy.metrics as _m

        if _m._usage_queue is not None:
            batch = _m._drain_queue()
            if batch:
                _m._flush_batch_sync(batch)
        from smol_llm_proxy.metrics import get_usage_logs

        logs = get_usage_logs()
        assert len(logs) >= 1
        last = logs[0]
        assert last["prompt_tokens"] > 0
        assert last["completion_tokens"] > 0
        assert last["total_tokens"] > 0
        assert last["prompt_ms"] > 0
        assert last["predicted_ms"] > 0

    def test_streaming_logged(self, integration_key, available_servers, proxy_http_url):
        """Streaming request also logs tokens + timings."""
        if 8080 not in available_servers:
            pytest.skip("No llama-server on port 8080")

        model = available_servers[8080]["models"][0]

        with httpx.Client(base_url=proxy_http_url, timeout=30) as c:
            r = c.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {integration_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "say hello"}],
                    "stream": True,
                },
            )
            print(f"proxy response: {r.status_code}")

        import smol_llm_proxy.metrics as _m

        if _m._usage_queue is not None:
            batch = _m._drain_queue()
            if batch:
                _m._flush_batch_sync(batch)
        from smol_llm_proxy.metrics import get_usage_logs

        logs = get_usage_logs()
        assert len(logs) >= 1
        last = logs[0]
        assert last["completion_tokens"] > 0
        assert last["prompt_ms"] > 0 or last["predicted_ms"] > 0

    def test_both_types_in_logs(self, integration_key, available_servers, proxy_http_url):
        """After both stream and non-stream, we have two separate log entries."""
        if 8080 not in available_servers:
            pytest.skip("No llama-server on port 8080")

        model = available_servers[8080]["models"][0]

        with httpx.Client(base_url=proxy_http_url, timeout=30) as c:
            c.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {integration_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
            c.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {integration_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": "bye"}],
                    "stream": True,
                },
            )

        import smol_llm_proxy.metrics as _m

        if _m._usage_queue is not None:
            batch = _m._drain_queue()
            if batch:
                _m._flush_batch_sync(batch)

        from smol_llm_proxy.database import get_db

        with get_db() as conn:
            key_id = conn.execute("SELECT id FROM api_keys ORDER BY created_at DESC LIMIT 1").fetchone()["id"]
            logs = conn.execute(
                "SELECT * FROM usage_logs WHERE key_id = ? ORDER BY created_at DESC",
                (key_id,),
            ).fetchall()
        assert len(logs) == 2
