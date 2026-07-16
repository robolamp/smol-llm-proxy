"""End-to-end alias test: real HTTP client through proxy to mock backend.

Verifies alias content-length handling (BUG 1 fix).
Tests both cases: alias shorter and longer than real model name.
"""

import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.real_proxy

ADMIN_KEY = "test-admin-key"


def _find_free_port():
    with __import__("socket").socket(__import__("socket").AF_INET, __import__("socket").SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_mock_backend(port):
    """Start a minimal mock llama-server that echoes back a valid response."""
    code = f"""
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()

@app.post("/v1/chat/completions")
async def chat(request: httpx.Request):
    body = await request.json()
    model = body.get("model", "unknown")
    return JSONResponse({{
        "id": "chatcmpl-1",
        "model": model,
        "choices": [{{"index": 0, "message": {{"role": "assistant", "content": "ok"}}, "finish_reason": "stop"}}],
        "usage": {{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        "timings": {{"prompt_ms": 12.0, "predicted_ms": 45.0}}
    }})

@app.post("/v1/completions")
async def completions(request: httpx.Request):
    body = await request.json()
    model = body.get("model", "unknown")
    return JSONResponse({{
        "id": "cmpl-1",
        "model": model,
        "text": "ok",
        "usage": {{"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}
    }})

@app.post("/v1/embeddings")
async def embeddings(request: httpx.Request):
    body = await request.json()
    return JSONResponse({{
        "model": body.get("model", "unknown"),
        "data": [{{"embedding": [0.1, 0.2]}}],
        "usage": {{"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2}}
    }})

@app.get("/v1/models")
async def models():
    return JSONResponse({{"object": "list", "data": [{{"id": "model.gguf"}}]}})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    timeout = 10
    start = time.time()
    while time.time() - start < timeout:
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            proc_ready = True
            break
        except Exception:
            pass
        time.sleep(0.3)
    else:
        proc_ready = False
    return proc, proc_ready


def _start_proxy(proxy_port, db_path, backend_port):
    env = os.environ.copy()
    env["ADMIN_KEY"] = ADMIN_KEY
    env["PROXY_PORT"] = str(proxy_port)
    env["DB_PATH"] = str(db_path)
    env.pop("CONFIG_PATH", None)

    project_dir = Path(__file__).resolve().parent.parent

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "smol_llm_proxy.main:app", "--host", "127.0.0.1", "--port", str(proxy_port)],
        env=env,
        cwd=str(project_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    timeout = 15
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"http://127.0.0.1:{proxy_port}/health", timeout=1)
            if resp.status_code == 200 and resp.json()["status"] == "ok":
                return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.kill()
    raise RuntimeError(f"Proxy failed to start on port {proxy_port}")


@pytest.fixture(scope="module")
def backend():
    port = _find_free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.applications import Starlette
from starlette.routing import Route
import json
async def chat(request: Request):
    body = await request.json()
    model = body.get("model", "unknown")
    return JSONResponse({{"id": "chatcmpl-1", "model": model, "choices": [{{"index": 0, "message": {{"role": "assistant", "content": "ok"}}, "finish_reason": "stop"}}], "usage": {{"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}, "timings": {{"prompt_ms": 12.0, "predicted_ms": 45.0}}}})
async def completions(request: Request):
    body = await request.json()
    return JSONResponse({{"id": "cmpl-1", "model": body.get("model","unknown"), "text": "ok", "usage": {{"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}}})
async def embeddings(request: Request):
    body = await request.json()
    return JSONResponse({{"model": body.get("model","unknown"), "data": [{{"embedding": [0.1]}}], "usage": {{"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2}}}})
async def models(request: Request):
    return JSONResponse({{"object": "list", "data": [{{"id": "model.gguf"}}]}})
async def health(request: Request):
    return JSONResponse({{"status": "ok"}})
app = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]), Route("/v1/completions", completions, methods=["POST"]), Route("/v1/embeddings", embeddings, methods=["POST"]), Route("/v1/models", models, methods=["GET"]), Route("/health", health, methods=["GET"])])
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port={port}, log_level="warning")
""",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    timeout = 10
    start = time.time()
    ready = False
    while time.time() - start < timeout:
        try:
            httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
            ready = True
            break
        except Exception:
            pass
        time.sleep(0.3)
    assert ready, "Mock backend failed to start"
    yield {"port": port, "url": f"http://127.0.0.1:{port}"}
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture(scope="module")
def proxy_setup():
    proxy_port = _find_free_port()
    db_path = str(Path(tempfile.mkdtemp()) / "test_e2e.db")
    proc = _start_proxy(proxy_port, db_path, 8080)
    yield {"port": proxy_port, "url": f"http://127.0.0.1:{proxy_port}", "db_path": db_path}
    proc.terminate()
    proc.wait(timeout=5)
    import shutil

    db_dir = Path(db_path).parent
    if db_dir.exists():
        shutil.rmtree(db_dir)


@pytest.fixture
def e2e_client(proxy_setup):
    return httpx.Client(base_url=proxy_setup["url"], timeout=10)


class TestE2EAliasShorterThanReal:
    """Alias is SHORTER than real model name: e.g. 'fast' -> 'qwen3-30b-a3b-instruct.gguf'.

    Before fix: 500 (h11: Too much data for declared Content-Length).
    After fix: 200 (hop-by-hop headers stripped, httpx recomputes content-length).
    """

    def test_alias_shorter(self, e2e_client, backend):
        uid = uuid.uuid4().hex[:8]
        real_model = f"qwen3-30b-a3b-instruct-{uid}.gguf"
        alias = "fast"

        # Create server and assign real model
        server_resp = e2e_client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"e2e-server-{uid}", "url": backend["url"]},
        )
        assert server_resp.status_code == 200
        server_id = server_resp.json()["id"]

        model_resp = e2e_client.post(
            f"/admin/servers/{server_id}/models",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"model_name": real_model},
        )
        assert model_resp.status_code in [200, 409]

        # Create alias: fast -> real_model
        alias_resp = e2e_client.post(
            "/admin/aliases",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"alias_name": alias, "real_model_name": real_model},
        )
        assert alias_resp.status_code == 200

        # Create user key
        key_resp = e2e_client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "e2e-user"},
        )
        user_key = key_resp.json()["key"]

        # Send request using the SHORTER alias
        resp = e2e_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}", "Content-Type": "application/json"},
            json={"model": alias, "messages": [{"role": "user", "content": "Hello, this is a longer prompt text"}]},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["model"] == real_model


class TestE2EAliasLongerThanReal:
    """Alias is LONGER than real model name: e.g. 'my-great-alias' -> 'gpt'.

    Before fix: hang until timeout (unread tail on pooled connection).
    After fix: 200.
    """

    def test_alias_longer(self, e2e_client, backend):
        uid = uuid.uuid4().hex[:8]
        real_model = f"tiny-{uid}"
        alias = f"my-great-alias-name-{uid}"

        server_resp = e2e_client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"e2e-server-long-{uid}", "url": backend["url"]},
        )
        assert server_resp.status_code == 200
        server_id = server_resp.json()["id"]

        model_resp = e2e_client.post(
            f"/admin/servers/{server_id}/models",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"model_name": real_model},
        )
        assert model_resp.status_code in [200, 409]

        alias_resp = e2e_client.post(
            "/admin/aliases",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"alias_name": alias, "real_model_name": real_model},
        )
        assert alias_resp.status_code == 200

        key_resp = e2e_client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "e2e-user-long"},
        )
        user_key = key_resp.json()["key"]

        resp = e2e_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}", "Content-Type": "application/json"},
            json={"model": alias, "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["model"] == real_model


class TestE2EAliasSwitching:
    """Test that an alias can be switched from one model to another."""

    def test_alias_switches_between_models(self, e2e_client, backend):
        uid = uuid.uuid4().hex[:8]
        real_model_a = f"model-a-{uid}.gguf"
        real_model_b = f"model-b-{uid}.gguf"
        alias = f"switching-alias-{uid}"

        # Create two servers with different models
        server_a_resp = e2e_client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"server-a-{uid}", "url": backend["url"]},
        )
        assert server_a_resp.status_code == 200
        server_a_id = server_a_resp.json()["id"]

        server_b_resp = e2e_client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"server-b-{uid}", "url": backend["url"]},
        )
        assert server_b_resp.status_code == 200
        server_b_id = server_b_resp.json()["id"]

        # Assign model A to server A
        model_a_resp = e2e_client.post(
            f"/admin/servers/{server_a_id}/models",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"model_name": real_model_a},
        )
        assert model_a_resp.status_code in [200, 409]

        # Create alias pointing to model A
        alias_resp = e2e_client.post(
            "/admin/aliases",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"alias_name": alias, "real_model_name": real_model_a},
        )
        assert alias_resp.status_code == 200

        key_resp = e2e_client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"switching-user-{uid}"},
        )
        user_key = key_resp.json()["key"]

        # Request via alias should resolve to model A
        resp = e2e_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}", "Content-Type": "application/json"},
            json={"model": alias, "messages": [{"role": "user", "content": "hello"}]},
        )
        assert resp.status_code == 200
        assert resp.json()["model"] == real_model_a

        # Switch alias to model B (update existing alias)
        alias_resp2 = e2e_client.patch(
            f"/admin/aliases/{alias}",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"real_model_name": real_model_b},
        )
        assert alias_resp2.status_code == 200

        # Assign model B to server B
        model_b_resp = e2e_client.post(
            f"/admin/servers/{server_b_id}/models",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"model_name": real_model_b},
        )
        assert model_b_resp.status_code in [200, 409]

        # Request via alias should now resolve to model B
        resp2 = e2e_client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}", "Content-Type": "application/json"},
            json={"model": alias, "messages": [{"role": "user", "content": "hello"}]},
        )
        assert resp2.status_code == 200
        assert resp2.json()["model"] == real_model_b


class TestE2EEstimateInputTokens:
    """Test that _estimate_input_tokens works for completions and embeddings routes."""

    def test_completions_estimate(self, e2e_client, backend):
        uid = uuid.uuid4().hex[:8]
        server_resp = e2e_client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"e2e-server-comp-{uid}", "url": backend["url"]},
        )
        assert server_resp.status_code == 200
        server_id = server_resp.json()["id"]

        model_resp = e2e_client.post(
            f"/admin/servers/{server_id}/models",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"model_name": "completion-model.gguf"},
        )
        assert model_resp.status_code in [200, 409]

        key_resp = e2e_client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "e2e-user-comp"},
        )
        user_key = key_resp.json()["key"]

        prompt_text = "a" * 400  # ~100 tokens estimated
        resp = e2e_client.post(
            "/v1/completions",
            headers={"Authorization": f"Bearer {user_key}", "Content-Type": "application/json"},
            json={"model": "completion-model.gguf", "prompt": prompt_text},
        )
        assert resp.status_code == 200

    def test_embeddings_estimate(self, e2e_client, backend):
        uid = uuid.uuid4().hex[:8]
        server_resp = e2e_client.post(
            "/admin/servers",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": f"e2e-server-emb-{uid}", "url": backend["url"]},
        )
        assert server_resp.status_code == 200
        server_id = server_resp.json()["id"]

        model_resp = e2e_client.post(
            f"/admin/servers/{server_id}/models",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"model_name": "embedding-model.gguf"},
        )
        assert model_resp.status_code in [200, 409]

        key_resp = e2e_client.post(
            "/admin/keys",
            headers={"Authorization": f"Bearer {ADMIN_KEY}"},
            json={"name": "e2e-user-emb"},
        )
        user_key = key_resp.json()["key"]

        input_text = "x" * 400  # ~100 tokens estimated
        resp = e2e_client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {user_key}", "Content-Type": "application/json"},
            json={"model": "embedding-model.gguf", "input": input_text},
        )
        assert resp.status_code == 200
