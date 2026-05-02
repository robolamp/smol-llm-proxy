"""Entry point: python -m smol_llm_proxy or pip install + smol-llm-proxy."""

import uvicorn
from config import PROXY_HOST, PROXY_PORT
from main import app


def main():
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT)


if __name__ == "__main__":
    main()
