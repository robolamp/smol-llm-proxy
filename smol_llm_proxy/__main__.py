"""Entry point: python -m smol_llm_proxy."""

import sys


def main():
    import uvicorn

    from smol_llm_proxy.config import PROXY_HOST, PROXY_PORT
    from smol_llm_proxy.main import app

    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, loop="uvloop" if sys.platform != "win32" else "asyncio")


if __name__ == "__main__":
    main()
