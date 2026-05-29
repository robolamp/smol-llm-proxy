"""Configuration for smol-llm-proxy."""

import os
from pathlib import Path

ADMIN_KEY: str = os.environ["ADMIN_KEY"]
PROXY_HOST: str = os.environ.get("PROXY_HOST", "0.0.0.0")
PROXY_PORT: int = int(os.environ.get("PROXY_PORT", "8000"))
HTTPX_TIMEOUT: float = float(os.environ.get("HTTPX_TIMEOUT", "120"))


def get_db_path() -> Path:
    return Path(os.environ.get("DB_PATH", "data/proxy.db"))
