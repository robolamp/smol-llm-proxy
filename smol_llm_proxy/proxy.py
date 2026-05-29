"""Proxy logic: route by model name and forward to llama-server."""
import httpx
import orjson
import time
from fastapi import Request, HTTPException
from fastapi.responses import StreamingResponse, Response, JSONResponse
from .config import HTTPX_TIMEOUT
from .database import get_db, resolve_routing
from .auth import _find_key_info
from .cache import get_cached_alias, set_cached_alias
from .metrics import enqueue_usage

_httpx_client: httpx.AsyncClient | None = None

def get_httpx_client() -> httpx.AsyncClient:
    global _httpx_client
    if _httpx_client is None or _httpx_client.is_closed:
        _httpx_client = httpx.AsyncClient(
            timeout=HTTPX_TIMEOUT,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20, keepalive_expiry=30.0),
        )
    return _httpx_client

async def shutdown_httpx_client():
    global _httpx_client
    if _httpx_client is not None and not _httpx_client.is_closed:
        await _httpx_client.aclose()
        _httpx_client = None

def _extract_user_key(authorization: str | None) -> str | None:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    return authorization[7:].strip()

async def _resolve_model(model_name: str) -> tuple[str, str]:
    cached = get_cached_alias(model_name)
    if cached:
        return model_name, cached
    with get_db() as conn:
        row = conn.execute("SELECT real_model_name FROM model_aliases WHERE alias_name = ?", (model_name,)).fetchone()
    if row:
        set_cached_alias(model_name, row["real_model_name"])
        return model_name, row["real_model_name"]
    return model_name, model_name

def _format_server_url(server_url: str, path: str) -> str:
    return f"{server_url.rstrip('/')}/{path.lstrip('/')}"

async def _forward_request(target_url: str, headers: dict, body: bytes, method: str):
    client = get_httpx_client()
    try:
        resp = await client.request(method=method, url=target_url, headers=headers, content=body)
        return resp.status_code, resp.content
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail=f"Cannot connect to server at {target_url}")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Server request timed out")

def _parse_usage_from_body(body_bytes: bytes) -> tuple[int, int, float, float]:
    try:
        data = orjson.loads(body_bytes)
    except (orjson.JSONDecodeError, TypeError):
        return 0, 0, 0.0, 0.0
    usage = data.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0) if isinstance(usage, dict) else 0
    completion_tokens = usage.get("completion_tokens", 0) if isinstance(usage, dict) else 0
    timings = data.get("timings", {})
    return (
        prompt_tokens,
        completion_tokens,
        timings.get("prompt_ms", 0.0) or 0.0,
        timings.get("predicted_ms", 0.0) or 0.0,
    )

async def _build_proxy_context(request: Request, path: str, *, body_bytes=None, body_json=None):
    """Extract auth info, resolve routing."""
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


def _build_upstream(server: dict, request: Request, path: str) -> tuple[str, dict]:
    target_url = _format_server_url(server["url"], path)
    upstream_headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "authorization")}
    if server.get("api_key"):
        upstream_headers["authorization"] = f"Bearer {server['api_key']}"
    return target_url, upstream_headers

def _timing_headers(timing: dict, forward_ms: float, parse_ms: float, pre_forward_ms: float, proxy_overhead: float) -> dict:
    return {
        "X-Proxy-Body-Read": f"{timing['body_read_ms']:.2f}ms",
        "X-Proxy-Json-Parse": f"{timing['json_parse_ms']:.2f}ms",
        "X-Proxy-Auth-Time": f"{timing['auth_ms']:.2f}ms",
        "X-Proxy-Route-Time": f"{timing['route_ms']:.2f}ms",
        "X-Proxy-Alias-Time": f"{timing['alias_ms']:.2f}ms",
        "X-Proxy-Serialize-Time": f"{timing['serialize_ms']:.2f}ms",
        "X-Proxy-Forward-Time": f"{forward_ms:.2f}ms",
        "X-Proxy-Parse-Time": f"{parse_ms:.2f}ms",
        "X-Proxy-Total-Overhead": f"{proxy_overhead:.2f}ms",
    }

async def _proxy_setup(request: Request, path: str, *, body_bytes=None, body_json=None):
    key_info, routing, display_name, real_model_name, _, body_bytes, timing = await _build_proxy_context(request, path, body_bytes=body_bytes, body_json=body_json)
    server = {"id": routing["server_id"], "url": routing["url"], "api_key": routing.get("api_key", "")}
    target_url, upstream_headers = _build_upstream(server, request, path)
    return key_info, routing, display_name, real_model_name, body_bytes, timing, target_url, upstream_headers

async def proxy_non_streaming(request: Request, path: str, *, body_bytes=None, body_json=None):
    t0 = time.perf_counter()

    key_info, routing, display_name, real_model_name, body_bytes, timing, target_url, upstream_headers = await _proxy_setup(request, path, body_bytes=body_bytes, body_json=body_json)

    pre_forward_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    status_code, resp_body = await _forward_request(target_url, upstream_headers, body_bytes, request.method)
    forward_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    prompt_tokens, completion_tokens, prompt_ms, predicted_ms = _parse_usage_from_body(resp_body)
    parse_ms = (time.perf_counter() - t0) * 1000

    enqueue_usage(
        key_info["id"], routing["server_id"], display_name, real_model_name,
        prompt_tokens, completion_tokens, prompt_ms, predicted_ms,
    )

    proxy_overhead = timing["body_read_ms"] + timing["json_parse_ms"] + timing["auth_ms"] + timing["route_ms"] + timing["alias_ms"] + timing["serialize_ms"] + parse_ms
    return Response(
        content=resp_body, status_code=status_code, media_type="application/json",
        headers=_timing_headers(timing, forward_ms, parse_ms, pre_forward_ms, proxy_overhead),
    )

async def proxy_streaming(request: Request, path: str, *, body_bytes=None, body_json=None):
    key_info, routing, display_name, real_model_name, body_bytes, timing, target_url, upstream_headers = await _proxy_setup(request, path, body_bytes=body_bytes, body_json=body_json)

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
            enqueue_usage(
                key_info["id"], routing["server_id"], display_name, real_model_name,
                total_prompt, total_completion, last_prompt_ms, last_predicted_ms,
            )

    proxy_overhead = timing["body_read_ms"] + timing["json_parse_ms"] + timing["auth_ms"] + timing["route_ms"] + timing["alias_ms"] + timing["serialize_ms"]
    return StreamingResponse(
        generate(), media_type="text/event-stream",
        headers=_timing_headers(timing, forward_ms, parse_ms, 0.0, proxy_overhead),
    )

def _parse_sse_usage(chunk: str) -> tuple[int, int, float, float]:
    try:
        stripped = chunk.strip()
        if stripped.startswith("data: "):
            stripped = stripped[6:]
        if stripped == "[DONE]":
            return 0, 0, 0.0, 0.0
        data = orjson.loads(stripped)
    except (orjson.JSONDecodeError, TypeError):
        return 0, 0, 0.0, 0.0

    timings = data.get("timings", {})
    if timings and isinstance(timings, dict):
        pn, cn = timings.get("prompt_n", 0) or 0, timings.get("predicted_n", 0) or 0
        if pn > 0 or cn > 0:
            return pn, cn, timings.get("prompt_ms", 0.0) or 0.0, timings.get("predicted_ms", 0.0) or 0.0

    usage = data.get("usage", {})
    if isinstance(usage, dict):
        return usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0), 0.0, 0.0
    return 0, 0, 0.0, 0.0

async def _stream_chunks(target_url: str, headers: dict, body: bytes):
    client = get_httpx_client()
    async with client.stream("POST", target_url, headers=headers, content=body) as resp:
        async for chunk in resp.aiter_bytes():
            yield chunk.decode("utf-8")

async def proxy_public(body: bytes = b"", path: str = "/v1/models"):
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
