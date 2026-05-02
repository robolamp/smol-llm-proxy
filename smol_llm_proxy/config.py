"""Configuration for smol-llm-proxy."""

import os
from pathlib import Path

ADMIN_KEY: str = os.environ["ADMIN_KEY"]
PROXY_HOST: str = os.environ.get("PROXY_HOST", "0.0.0.0")
PROXY_PORT: int = int(os.environ.get("PROXY_PORT", "8000"))
DB_PATH: Path = Path(os.environ.get("DB_PATH", "data/llm_proxy.db"))
HTTPX_TIMEOUT: float = 120.0
