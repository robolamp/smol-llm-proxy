"""Locust benchmark: measure proxy overhead vs direct llama-server."""

import json
import os
import threading
from locust import HttpUser, task, events

TIMING_FILE = "/tmp/bench_proxy_timings.jsonl"
_timing_lock = threading.Lock()
_header_keys = [
    "x-proxy-auth-time",
    "x-proxy-route-time",
    "x-proxy-alias-time",
    "x-proxy-serialize-time",
    "x-proxy-forward-time",
    "x-proxy-parse-time",
    "x-proxy-total",
]


@events.request.add_listener
def record_timing(request_type, name, response_time, response_length, response, context, **kwargs):
    if "v1/chat/completions" not in name:
        return
    headers = {}
    try:
        resp_headers = getattr(response, "headers", {}) or {}
        for k in _header_keys:
            val = resp_headers.get(k)
            if val:
                headers[k] = float(val.replace("ms", ""))
    except Exception:
        pass

    if headers:
        with _timing_lock:
            try:
                with open(TIMING_FILE, "a") as f:
                    f.write(json.dumps(headers) + "\n")
            except Exception:
                pass


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
