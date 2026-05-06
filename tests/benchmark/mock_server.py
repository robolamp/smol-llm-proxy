#!/usr/bin/env python3
"""Mock llama.cpp server with fixed latency for benchmarking."""

import asyncio
import json
import os

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

app = FastAPI()

DELAY_MS = int(os.environ.get("MOCK_DELAY_MS", "100"))
PORT = int(os.environ.get("MOCK_PORT", "8765"))


def _make_response_body(stream: bool = False) -> dict | str:
    if stream:
        return 'data: {"id":"mock-1","object":"chat.completion.chunk","created":1700000000,"model":"mock","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}\ndata: {"id":"mock-1","object":"chat.completion.chunk","created":1700000000,"model":"mock","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n'
    return {
        "id": "mock-1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "mock",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.body()
    data = json.loads(body)
    stream = data.get("stream", False)

    await asyncio.sleep(DELAY_MS / 1000.0)

    if stream:
        return StreamingResponse(iter([_make_response_body(stream=True).encode()]), media_type="text/event-stream")
    return _make_response_body(stream=False)


@app.post("/v1/completions")
async def completions(request: Request):
    await asyncio.sleep(DELAY_MS / 1000.0)
    return {"id": "mock-1", "object": "text_completion", "choices": [{"text": "ok", "finish_reason": "stop"}]}


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    await asyncio.sleep(DELAY_MS / 1000.0)
    return {"data": [{"embedding": [0.1] * 4}], "model": "mock", "object": "list"}


@app.get("/v1/models")
async def models():
    await asyncio.sleep(DELAY_MS / 1000.0)
    return {
        "object": "list",
        "data": [{"id": "mock", "object": "model", "created": 1700000000, "owned_by": "mock"}],
    }


if __name__ == "__main__":
    import uvicorn

    print(f"Mock server: delay={DELAY_MS}ms port={PORT}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
