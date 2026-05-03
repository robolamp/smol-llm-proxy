"""Locust benchmark: measure proxy overhead vs direct llama-server."""

import os
from locust import HttpUser, task


DIRECT_URL = os.environ.get("DIRECT_URL", "http://host:port")
UPSTREAM_KEY = os.environ.get("UPSTREAM_KEY", "")
USER_KEY = os.environ.get("USER_KEY", "")

PAYLOAD = {
    "model": os.environ.get("BENCH_MODEL", "model.gguf"),
    "messages": [{"role": "user", "content": "hi"}],
}


class DirectLLaMAServer(HttpUser):
    """Benchmark against llama-server directly (baseline)."""

    host = DIRECT_URL
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}", "Content-Type": "application/json"}

    @task
    def chat_completion(self):
        self.client.post("/v1/chat/completions", json=PAYLOAD, headers=self.headers)


class ThroughProxy(HttpUser):
    """Benchmark through proxy (measures overhead)."""

    host = os.environ.get("PROXY_URL", "http://localhost:8000")
    headers = {"Authorization": f"Bearer {USER_KEY}", "Content-Type": "application/json"}

    @task
    def chat_completion(self):
        self.client.post("/v1/chat/completions", json=PAYLOAD, headers=self.headers)


# Disable unused class at runtime via env var
if os.environ.get("BENCH_DIRECT") == "0":
    DirectLLaMAServer = None
if os.environ.get("BENCH_PROXY") == "0":
    ThroughProxy = None
