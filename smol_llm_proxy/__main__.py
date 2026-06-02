"""Entry point: python -m smol_llm_proxy."""

import sys
import uvicorn
from smol_llm_proxy.config import PROXY_HOST, PROXY_PORT


def main():
    loop = "uvloop" if sys.platform != "win32" else "asyncio"
    uvicorn.run("smol_llm_proxy.main:app", host=PROXY_HOST, port=PROXY_PORT, loop=loop)


if __name__ == "__main__":
    main()
