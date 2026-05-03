"""Entry point: python -m smol_llm_proxy."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
from smol_llm_proxy.config import PROXY_HOST, PROXY_PORT


def main():
    uvicorn.run("main:app", host=PROXY_HOST, port=PROXY_PORT)


if __name__ == "__main__":
    main()
