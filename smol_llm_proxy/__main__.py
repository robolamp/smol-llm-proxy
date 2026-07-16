"""Entry point: python -m smol_llm_proxy."""

import sys


def main():
    from smol_llm_proxy.main import app
    from smol_llm_proxy.config import PROXY_HOST, PROXY_PORT
    import uvicorn

    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, loop="uvloop" if sys.platform != "win32" else "asyncio")


if __name__ == "__main__":
    main()
