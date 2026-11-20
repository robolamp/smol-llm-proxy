"""Configuration for smol-llm-proxy."""

import os
from pathlib import Path


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv()

ADMIN_KEY: str = os.environ.get("ADMIN_KEY", "")
if not ADMIN_KEY:
    raise RuntimeError(
        "ADMIN_KEY environment variable is required. "
        "Set it to a secret value before starting the proxy, e.g. "
        "ADMIN_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(24))')"
    )
PROXY_HOST: str = os.environ.get("PROXY_HOST", "0.0.0.0")
PROXY_PORT: int = int(os.environ.get("PROXY_PORT", "8000"))
HTTPX_TIMEOUT: float = float(os.environ.get("HTTPX_TIMEOUT", "120"))
HTTPX_CONNECT_TIMEOUT: float = float(os.environ.get("HTTPX_CONNECT_TIMEOUT", "5"))
MODELS_TIMEOUT: float = float(os.environ.get("MODELS_TIMEOUT", "5"))
PROXY_MAX_CONNECTIONS: int = int(os.environ.get("PROXY_MAX_CONNECTIONS", "50"))
PROXY_MAX_KEEPALIVE: int = int(os.environ.get("PROXY_MAX_KEEPALIVE", "20"))
PROXY_KEEPALIVE_EXPIRY: float = float(os.environ.get("PROXY_KEEPALIVE_EXPIRY", "30.0"))
SSE_KEEPALIVE_INTERVAL: float = float(os.environ.get("SSE_KEEPALIVE_INTERVAL", "15"))


def get_db_path() -> Path:
    return Path(os.environ.get("DB_PATH", "data/proxy.db"))
