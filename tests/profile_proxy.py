#!/usr/bin/env python3
"""Profile proxy overhead per-request with cProfile."""

import cProfile
import io
import os
import pstats
import sys
import time as _time

sys.path.insert(0, "/workspace/smol-llm-proxy")
os.environ["ADMIN_KEY"] = "Fdczv9kefrH2BctYxhToOWvEaBREkR7YfOaH3GIwFcE"
os.environ["DB_PATH"] = "/tmp/profile_proxy.db"

from starlette.datastructures import Headers

from smol_llm_proxy.auth import create_api_key
from smol_llm_proxy.database import get_db, init_db
from smol_llm_proxy.proxy import _build_proxy_context
from smol_llm_proxy.rate_limiter import reconcile_rate, reserve_rate

init_db()

# Create key with unlimited limits
result = create_api_key("profile-test")
key_hash = __import__("hashlib").sha256(result["key"].encode()).hexdigest()

with get_db() as conn:
    conn.execute("UPDATE api_keys SET rpm_limit = 1000000, tpm_limit = 1000000 WHERE id = ?", (result["id"],))

# Create a server + model
with get_db() as conn:
    cursor = conn.execute("INSERT INTO servers (name, url) VALUES (?, ?)", ("profile-srv", "http://localhost:9999"))
    server_id = cursor.lastrowid
    conn.execute("INSERT INTO server_models (server_id, model_name) VALUES (?, ?)", (server_id, "test.gguf"))


def make_request(body_json=None):
    """Create a fake Starlette Request."""
    if body_json is None:
        body_json = {"model": "test.gguf", "messages": [{"role": "user", "content": "hello world"}]}
    import orjson

    body_bytes = orjson.dumps(body_json)

    headers = Headers(raw=[(b"authorization", f"Bearer {result['key']}".encode())])
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
    }

    class FakeRequest:
        def __init__(self, scope, body):
            self.scope = scope
            self._body = body
            self.headers = Headers(raw=self.scope["headers"])

        async def body(self):
            return self._body

    return FakeRequest(scope, body_bytes)


def run_single():
    """Run one proxy context build + rate check (no upstream)."""
    request = make_request()
    _build_proxy_context(
        request,
        "/v1/chat/completions",
        body_json={"model": "test.gguf", "messages": [{"role": "user", "content": "hello world"}]},
    )
    ws = int(_time.time())
    reserve_rate(result["id"], 1000000, 1000000, 5)
    reconcile_rate(result["id"], 8, ws, 5)


def run_batch(n=500):
    """Run N iterations and profile."""
    pr = cProfile.Profile()
    pr.enable()

    for _ in range(n):
        run_single()

    pr.disable()
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(30)
    print(s.getvalue())

    # Per-function totals
    ps2 = pstats.Stats(pr, stream=s).sort_stats("tottime")
    ps2.print_stats(20)
    print("\n--- Sorted by tottime ---")
    print(s.getvalue())


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    print(f"Profiling {n} iterations...\n")
    run_batch(n)
