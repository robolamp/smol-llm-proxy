"""Proxy logic: route by model name and forward to llama-server."""

import httpx
import orjson

from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

from .config import HTTPX_TIMEOUT
from .database import get_db, validate_key, resolve_routing
from .auth import _hash_key
from .cache import (
    get_cached_alias, set_cached_alias,
)
from .metrics import enqueue_usage, flush_usage_logs
from .database import get_db





def _extract_user_key(authorization: str | None) -> str | None:
    """Extract Bearer token from Authorization header."""
    if not authorization:
        return None
    if authorization.startswith("Bearer "):
        return authorization[7:].strip()
    return None


async def _resolve_model(model_name: str) -> tuple[str, str]:
    """Resolve alias to real model name. Returns (display_name, real_name)."""
    cached = get_cached_alias(model_name)
    if cached:
        return model_name, cached

    with get_db() as conn:
        row = conn.execute(
            "SELECT real_model_name FROM model_aliases WHERE alias_name = ?",
            (model_name,),
        ).fetchone()
    if row:
        set_cached_alias(model_name, row["real_model_name"])
        return model_name, row["real_model_name"]
    return model_name, model_name


def _format_server_url(server_url: str, path: str) -> str:
    """Ensure server_url has no trailing slash and join with path."""
    base = server_url.rstrip("/")
    return f"{base}/{path.lstrip('/')}"


async def _forward_request(target_url: str, headers: dict, body: bytes, method: str):
    """Forward a request to llama-server."""
    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
        resp = await client.request(
            method=method,
            url=target_url,
            headers=headers,
            content=body,
        )
        return resp.status_code, resp.content


def _parse_usage_from_body(body_bytes: bytes) -> tuple[int, int, float, float]:
    """Extract tokens and timings from response body JSON."""
    try:
        data = orjson.loads(body_bytes)
    except (orjson.JSONDecodeError, TypeError):
        return 0, 0, 0.0, 0.0

    usage = data.get("usage", {})
    prompt_tokens = 0
    completion_tokens = 0
    if isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

    timings = data.get("timings", {})
    prompt_ms = timings.get("prompt_ms", 0.0) or 0.0
    predicted_ms = timings.get("predicted_ms", 0.0) or 0.0

    return prompt_tokens, completion_tokens, prompt_ms, predicted_ms


async def proxy_non_streaming(request: Request, path: str):
    """Handle a non-streaming proxy request."""
    user_key = _extract_user_key(request.headers.get("authorization"))
    if not user_key:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    body_bytes = await request.body()
    try:
        body_json = orjson.loads(body_bytes)
    except orjson.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model_name = body_json.get("model", "")
    key_hash = _hash_key(user_key)
    key_info = validate_key(key_hash)
    if not key_info:
        raise HTTPException(status_code=403, detail="Invalid or inactive API key")

    routing = resolve_routing(key_info["id"], model_name)

    display_name, real_model_name = await _resolve_model(model_name)

    if not routing:
        raise HTTPException(
            status_code=404,
            detail=f"No server configured for model '{display_name}'",
        )

    server = {
        "id": routing["server_id"],
        "name": "",
        "url": routing["url"],
        "api_key": routing["api_key"],
    }

    target_url = _format_server_url(server["url"], path)

    upstream_headers = dict(request.headers)
    upstream_headers.pop("host", None)
    if server["api_key"]:
        upstream_headers["authorization"] = f"Bearer {server['api_key']}"

    try:
        status_code, resp_body = await _forward_request(
            target_url, upstream_headers, body_bytes, request.method
        )
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"Cannot connect to server at {target_url}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Server request timed out")

    prompt_tokens, completion_tokens, prompt_ms, predicted_ms = _parse_usage_from_body(resp_body)

    enqueue_usage(key_info["id"], routing["server_id"], display_name, real_model_name,
                  prompt_tokens, completion_tokens, prompt_ms, predicted_ms)
    flush_usage_logs()

    return JSONResponse(content=orjson.loads(resp_body), status_code=status_code)


def _parse_sse_usage(chunk: str) -> tuple[int, int, float, float]:
    """Extract token counts and timings from a single SSE data chunk."""
    try:
        stripped = chunk.strip()
        if stripped.startswith("data: "):
            stripped = stripped[6:]
        if stripped == "[DONE]":
            return 0, 0, 0.0, 0.0
        data = orjson.loads(stripped)
    except (orjson.JSONDecodeError, TypeError):
        return 0, 0, 0.0, 0.0

    # llama.cpp puts timings at top level in streaming mode
    timings = data.get("timings", {})
    if timings and isinstance(timings, dict):
        prompt_tokens = timings.get("prompt_n", 0) or 0
        completion_tokens = timings.get("predicted_n", 0) or 0
        prompt_ms = timings.get("prompt_ms", 0.0) or 0.0
        predicted_ms = timings.get("predicted_ms", 0.0) or 0.0
        if prompt_tokens > 0 or completion_tokens > 0:
            return prompt_tokens, completion_tokens, prompt_ms, predicted_ms

    # fallback: standard OpenAI format with top-level usage
    usage = data.get("usage", {})
    if isinstance(usage, dict):
        return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), 0.0, 0.0
    return 0, 0, 0.0, 0.0


async def _stream_chunks(target_url: str, headers: dict, body: bytes):
    """Stream chunks from llama-server and yield SSE-formatted strings."""
    async with httpx.AsyncClient(timeout=HTTPX_TIMEOUT) as client:
        async with client.stream("POST", target_url, headers=headers, content=body) as resp:
            async for chunk in resp.aiter_bytes():
                yield chunk.decode("utf-8")


async def proxy_streaming(request: Request, path: str):
    """Handle a streaming proxy request."""
    user_key = _extract_user_key(request.headers.get("authorization"))
    if not user_key:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    body_bytes = await request.body()
    try:
        body_json = orjson.loads(body_bytes)
    except orjson.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    model_name = body_json.get("model", "")
    key_hash = _hash_key(user_key)
    key_info = validate_key(key_hash)
    if not key_info:
        raise HTTPException(status_code=403, detail="Invalid or inactive API key")

    routing = resolve_routing(key_info["id"], model_name)

    display_name, real_model_name = await _resolve_model(model_name)

    if not routing:
        raise HTTPException(
            status_code=404,
            detail=f"No server configured for model '{display_name}'",
        )

    server = {
        "id": routing["server_id"],
        "name": "",
        "url": routing["url"],
        "api_key": routing["api_key"],
    }

    target_url = _format_server_url(server["url"], path)

    upstream_headers = dict(request.headers)
    upstream_headers.pop("host", None)
    if server["api_key"]:
        upstream_headers["authorization"] = f"Bearer {server['api_key']}"

    total_prompt = 0
    total_completion = 0
    last_prompt_ms = 0.0
    last_predicted_ms = 0.0

    async def generate():
        nonlocal total_prompt, total_completion, last_prompt_ms, last_predicted_ms
        try:
            async for chunk in _stream_chunks(target_url, upstream_headers, body_bytes):
                p, c, pm, pr = _parse_sse_usage(chunk)
                total_prompt += p
                total_completion += c
                if pm > 0 or pr > 0:
                    last_prompt_ms = pm
                    last_predicted_ms = pr
                yield chunk
        finally:
            enqueue_usage(key_info["id"], routing["server_id"], display_name, real_model_name,
                          total_prompt, total_completion, last_prompt_ms, last_predicted_ms)

    resp = StreamingResponse(generate(), media_type="text/event-stream")
    return resp


async def proxy_public(body: bytes = b"", path: str = "/v1/models"):
    """Forward requests that don't need auth (e.g. /v1/models)."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT url, id FROM servers WHERE active = 1"
        ).fetchall()

    if not rows:
        raise HTTPException(status_code=503, detail="No active llama-server configured")

    all_models = []
    for row in rows:
        target_url = _format_server_url(row["url"], path)
        try:
            status_code, resp_body = await _forward_request(
                target_url, {}, body, "GET"
            )
            data = orjson.loads(resp_body)
            if "data" in data:
                for m in data["data"]:
                    all_models.append(m)
        except httpx.ConnectError:
            continue

    return JSONResponse(content={"object": "list", "data": all_models}, status_code=200)
