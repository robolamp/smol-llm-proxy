"""Entry point: python -m smol_llm_proxy."""

from smol_llm_proxy.main import app  # noqa: F401
import uvicorn
from smol_llm_proxy.config import PROXY_HOST, PROXY_PORT

uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT)
