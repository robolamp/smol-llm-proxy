"""Configuration for smol-llm-proxy."""

import os
from pathlib import Path

ADMIN_KEY: str = os.environ.get("ADMIN_KEY", "")
PROXY_HOST: str = os.environ.get("PROXY_HOST", "0.0.0.0")
PROXY_PORT: int = int(os.environ.get("PROXY_PORT", "8000"))
HTTPX_TIMEOUT: float = float(os.environ.get("HTTPX_TIMEOUT", "120"))
PROXY_MAX_CONNECTIONS: int = int(os.environ.get("PROXY_MAX_CONNECTIONS", "50"))
PROXY_MAX_KEEPALIVE: int = int(os.environ.get("PROXY_MAX_KEEPALIVE", "20"))
PROXY_KEEPALIVE_EXPIRY: float = float(os.environ.get("PROXY_KEEPALIVE_EXPIRY", "30.0"))


def get_db_path() -> Path:
    return Path(os.environ.get("DB_PATH", "data/proxy.db"))
