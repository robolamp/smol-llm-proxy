"""Locust benchmark: direct llama-server (baseline)."""

import os
from locust import HttpUser, task

DIRECT_URL = os.environ.get("DIRECT_URL", "http://host:port")
UPSTREAM_KEY = os.environ.get("UPSTREAM_KEY", "")
BENCH_MODEL = os.environ.get("BENCH_MODEL", "model.gguf")

PAYLOAD = {
    "model": BENCH_MODEL,
    "messages": [{"role": "user", "content": "hi"}],
}


class DirectLLaMAServer(HttpUser):
    host = DIRECT_URL
    headers = {"Authorization": f"Bearer {UPSTREAM_KEY}", "Content-Type": "application/json"}

    @task
    def chat_completion(self):
        self.client.post("/v1/chat/completions", json=PAYLOAD, headers=self.headers)
