"""Locust benchmark: through proxy."""

import os
from locust import HttpUser, task

PROXY_URL = os.environ.get("PROXY_URL", "http://localhost:8000")
USER_KEY = os.environ.get("USER_KEY", "")
BENCH_MODEL = os.environ.get("BENCH_MODEL", "model.gguf")

PAYLOAD = {
    "model": BENCH_MODEL,
    "messages": [{"role": "user", "content": "hi"}],
}


class ThroughProxy(HttpUser):
    host = PROXY_URL
    headers = {"Authorization": f"Bearer {USER_KEY}", "Content-Type": "application/json"}

    @task
    def chat_completion(self):
        self.client.post("/v1/chat/completions", json=PAYLOAD, headers=self.headers)
