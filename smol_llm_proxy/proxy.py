"""Proxy logic: route by model name and forward to llama-server."""

import httpx
import orjson
import time
from fastapi import HTTPException
from fastapi.responses import StreamingResponse, Response, JSONResponse
from .config import HTTPX_TIMEOUT
from .database import get_db, resolve_routing
from .auth import _find_key_info
from .cache import get_cached_alias, set_cached_alias
from .rate_limiter import check_rate, commit_rate
from .metrics import enqueue_usage

_httpx_client: httpx.AsyncClient | None = None


def _init_rate_table():
    """Create the rate_limits table for per-key sliding window tracking."""
    with get_db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rate_limits (id INTEGER PRIMARY KEY AUTOINCREMENT, key_id INTEGER NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE, window_start REAL NOT NULL, request_count INTEGER NOT NULL DEFAULT 0, token_sum INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_key_window ON rate_limits(key_id, window_start)")


def get_httpx_client() -> httpx.AsyncClient:
    """Get or create the shared async HTTP client with connection pooling."""
    global _httpx_client
    if _httpx_client is None or _httpx_client.is_closed:
        _httpx_client = httpx.AsyncClient(
            timeout=HTTPX_TIMEOUT,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0),
        )
    return _httpx_client


async def shutdown_httpx_client():
    """Close the shared HTTP client."""
    global _httpx_client
    if _httpx_client is not None and not _httpx_client.is_closed:
        await _httpx_client.aclose()
        _httpx_client = None


def _extract_user_key(authorization):
    """Extract the API key from a Bearer authorization header."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:].strip()


async def _resolve_model(model_name):
    """Resolve an alias to its real model name, using cache when possible."""
    cached = get_cached_alias(model_name)
    if cached:
        return model_name, cached
    with get_db() as conn:
        row = conn.execute("SELECT real_model_name FROM model_aliases WHERE alias_name = ?", (model_name,)).fetchone()
    if row:
        set_cached_alias(model_name, row["real_model_name"])
        return model_name, row["real_model_name"]
    return model_name, model_name


def _format_server_url(server_url, path):
    """Join a server URL and API path."""
    return f"{server_url.rstrip('/')}/{path.lstrip('/')}"


async def _forward_request(target_url, headers, body, method):
    """Forward an HTTP request to the upstream llama-server."""
    client = get_httpx_client()
    try:
        resp = await client.request(method=method, url=target_url, headers=headers, content=body)
        return resp.status_code, resp.content
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"Cannot connect to server at {target_url}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Server request timed out")


def _parse_usage_from_body(body_bytes):
    """Extract token counts and timing from a response body."""
    try:
        data = orjson.loads(body_bytes)
    except (orjson.JSONDecodeError, TypeError):
        return 0, 0, 0.0, 0.0
    usage = data.get("usage", {}) if isinstance(data, dict) else {}
    pt = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
    ct = usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0
    t = data.get("timings", {}) if isinstance(data, dict) else {}
    return pt, ct, t.get("prompt_ms", 0.0) or 0.0, t.get("predicted_ms", 0.0) or 0.0


def _estimate_input_tokens(body_json):
    """Estimate input token count from message characters (chars / 4)."""
    total_chars = sum(
        len(m.get("content", "")) for m in body_json.get("messages", []) if isinstance(m.get("content"), str)
    )
    return max(1, total_chars // 4)


async def _build_proxy_context(request, path, *, body_bytes=None, body_json=None):
    t0 = time.perf_counter()
    user_key = _extract_user_key(request.headers.get("authorization"))
    if not user_key:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    if body_bytes is None:
        body_bytes = await request.body()
    body_read_ms = (time.perf_counter() - t0) * 1000
    if body_json is None:
        try:
            body_json = orjson.loads(body_bytes)
        except orjson.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
    json_parse_ms = (time.perf_counter() - t0) * 1000
    model_name = body_json.get("model", "")
    key_info = _find_key_info(user_key)
    auth_ms = (time.perf_counter() - t0) * 1000
    if not key_info or not key_info.get("active"):
        raise HTTPException(status_code=403, detail="Invalid or inactive API key")
    routing = resolve_routing(key_info["id"], model_name)
    route_ms = (time.perf_counter() - t0) * 1000
    display_name, real_model_name = await _resolve_model(model_name)
    alias_ms = (time.perf_counter() - t0) * 1000
    serialize_ms = 0.0
    if display_name != real_model_name:
        body_json["model"] = real_model_name
        body_bytes = orjson.dumps(body_json)
        serialize_ms = (time.perf_counter() - t0) * 1000
    if not routing:
        raise HTTPException(status_code=404, detail=f"No server configured for model '{display_name}'")
    timing = {
        "body_read_ms": body_read_ms,
        "json_parse_ms": json_parse_ms,
        "auth_ms": auth_ms,
        "route_ms": route_ms,
        "alias_ms": alias_ms,
        "serialize_ms": serialize_ms,
    }
    return key_info, routing, display_name, real_model_name, body_json, body_bytes, timing


def _build_upstream(server, request, path):
    """Build the target URL and upstream headers for forwarding."""
    target_url = _format_server_url(server["url"], path)
    upstream_headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "authorization")}
    if server.get("api_key"):
        upstream_headers["authorization"] = f"Bearer {server['api_key']}"
    return target_url, upstream_headers


def _timing_headers(timing, forward_ms, parse_ms, pre_forward_ms, proxy_overhead):
    mapping = [
        ("X-Proxy-Body-Read", "body_read_ms"),
        ("X-Proxy-Json-Parse", "json_parse_ms"),
        ("X-Proxy-Auth-Time", "auth_ms"),
        ("X-Proxy-Route-Time", "route_ms"),
        ("X-Proxy-Alias-Time", "alias_ms"),
        ("X-Proxy-Serialize-Time", "serialize_ms"),
    ]
    headers = {k: f"{timing[v]:.2f}ms" for k, v in mapping}
    headers.update(
        {
            "X-Proxy-Forward-Time": f"{forward_ms:.2f}ms",
            "X-Proxy-Parse-Time": f"{parse_ms:.2f}ms",
            "X-Proxy-Total-Overhead": f"{proxy_overhead:.2f}ms",
        }
    )
    return headers


async def _proxy_setup(request, path, *, body_bytes=None, body_json=None):
    """Build the full proxy context: auth, routing, alias resolution, and upstream URL."""
    key_info, routing, display_name, real_model_name, _, body_bytes, timing = await _build_proxy_context(
        request, path, body_bytes=body_bytes, body_json=body_json
    )
    server = {"id": routing["server_id"], "url": routing["url"], "api_key": routing.get("api_key", "")}
    target_url, upstream_headers = _build_upstream(server, request, path)
    return key_info, routing, display_name, real_model_name, body_bytes, timing, target_url, upstream_headers


def _rate_limit_response(retry_after):
    """Build a 429 JSON response with Retry-After header."""
    return JSONResponse(
        content={"error": {"message": "Rate limit exceeded", "type": "rate_limit", "retry_after": retry_after}},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


async def proxy_non_streaming(request, path, *, body_bytes=None, body_json=None):
    """Handle a non-streaming chat completion request through the proxy."""
    t0 = time.perf_counter()
    (
        key_info,
        routing,
        display_name,
        real_model_name,
        body_bytes,
        timing,
        target_url,
        upstream_headers,
    ) = await _proxy_setup(request, path, body_bytes=body_bytes, body_json=body_json)
    pre_forward_ms = (time.perf_counter() - t0) * 1000
    if body_json:
        est_tokens = _estimate_input_tokens(body_json)
        allowed, retry_after = check_rate(key_info["id"], key_info["rpm_limit"], key_info["tpm_limit"], est_tokens)
        if not allowed:
            return _rate_limit_response(retry_after)
    t0 = time.perf_counter()
    status_code, resp_body = await _forward_request(target_url, upstream_headers, body_bytes, request.method)
    forward_ms = (time.perf_counter() - t0) * 1000
    prompt_tokens, completion_tokens, prompt_ms, predicted_ms = _parse_usage_from_body(resp_body)
    parse_ms = (time.perf_counter() - t0) * 1000
    commit_rate(key_info["id"], prompt_tokens + completion_tokens)
    enqueue_usage(
        key_info["id"],
        routing["server_id"],
        display_name,
        real_model_name,
        prompt_tokens,
        completion_tokens,
        prompt_ms,
        predicted_ms,
    )
    proxy_overhead = (
        timing["body_read_ms"]
        + timing["json_parse_ms"]
        + timing["auth_ms"]
        + timing["route_ms"]
        + timing["alias_ms"]
        + timing["serialize_ms"]
        + parse_ms
    )
    return Response(
        content=resp_body,
        status_code=status_code,
        media_type="application/json",
        headers=_timing_headers(timing, forward_ms, parse_ms, pre_forward_ms, proxy_overhead),
    )


async def proxy_streaming(request, path, *, body_bytes=None, body_json=None):
    """Handle a streaming chat completion request through the proxy."""
    (
        key_info,
        routing,
        display_name,
        real_model_name,
        body_bytes,
        timing,
        target_url,
        upstream_headers,
    ) = await _proxy_setup(request, path, body_bytes=body_bytes, body_json=body_json)
    if body_json:
        est_tokens = _estimate_input_tokens(body_json)
        allowed, retry_after = check_rate(key_info["id"], key_info["rpm_limit"], key_info["tpm_limit"], est_tokens)
        if not allowed:
            return _rate_limit_response(retry_after)
    total_prompt = 0
    total_completion = 0
    last_prompt_ms = 0.0
    last_predicted_ms = 0.0
    forward_ms = 0.0
    parse_ms = 0.0

    async def generate():
        nonlocal total_prompt, total_completion, last_prompt_ms, last_predicted_ms, forward_ms, parse_ms
        t0 = time.perf_counter()
        try:
            async for chunk in _stream_chunks(target_url, upstream_headers, body_bytes):
                p, c, pm, pr = _parse_sse_usage(chunk)
                total_prompt += p
                total_completion += c
                if pm > 0 or pr > 0:
                    last_prompt_ms = pm
                    last_predicted_ms = pr
                parse_ms += (time.perf_counter() - t0) * 1000
                yield chunk
        finally:
            forward_ms = (time.perf_counter() - t0) * 1000
            commit_rate(key_info["id"], total_prompt + total_completion)
            enqueue_usage(
                key_info["id"],
                routing["server_id"],
                display_name,
                real_model_name,
                total_prompt,
                total_completion,
                last_prompt_ms,
                last_predicted_ms,
            )

    proxy_overhead = (
        timing["body_read_ms"]
        + timing["json_parse_ms"]
        + timing["auth_ms"]
        + timing["route_ms"]
        + timing["alias_ms"]
        + timing["serialize_ms"]
    )
    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers=_timing_headers(timing, forward_ms, parse_ms, 0.0, proxy_overhead),
    )


def _parse_sse_usage(chunk):
    """Extract token counts and timing from an SSE chunk."""
    try:
        stripped = chunk.strip()
        if stripped.startswith("data: "):
            stripped = stripped[6:]
        if stripped == "[DONE]":
            return 0, 0, 0.0, 0.0
        data = orjson.loads(stripped)
    except (orjson.JSONDecodeError, TypeError):
        return 0, 0, 0.0, 0.0
    timings = data.get("timings", {}) if isinstance(data, dict) else {}
    if timings and isinstance(timings, dict):
        pn, cn = timings.get("prompt_n", 0) or 0, timings.get("predicted_n", 0) or 0
        if pn > 0 or cn > 0:
            return pn, cn, timings.get("prompt_ms", 0.0) or 0.0, timings.get("predicted_ms", 0.0) or 0.0
    usage = data.get("usage", {}) if isinstance(data, dict) else {}
    if isinstance(usage, dict):
        return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), 0.0, 0.0
    return 0, 0, 0.0, 0.0


async def _stream_chunks(target_url, headers, body):
    """Stream response chunks from the upstream server."""
    client = get_httpx_client()
    async with client.stream("POST", target_url, headers=headers, content=body) as resp:
        async for chunk in resp.aiter_bytes():
            yield chunk.decode("utf-8")


async def proxy_public(body=b"", path="/v1/models"):
    """Public endpoint: fetch models from all active backends without auth."""
    with get_db() as conn:
        rows = conn.execute("SELECT url, id FROM servers WHERE active = 1").fetchall()
    if not rows:
        raise HTTPException(status_code=503, detail="No active llama-server configured")
    all_models = []
    for row in rows:
        target_url = _format_server_url(row["url"], path)
        try:
            status_code, resp_body = await _forward_request(target_url, {}, body, "GET")
            data = orjson.loads(resp_body)
            if "data" in data:
                all_models.extend(data["data"])
        except Exception:
            continue
    return JSONResponse(content={"object": "list", "data": all_models}, status_code=200)
