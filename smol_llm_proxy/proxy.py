"""Proxy logic: route by model name and forward to llama-server."""

import asyncio
import codecs

import httpx
import orjson
from fastapi import HTTPException
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .auth import _find_key_info_sync, _hash_key
from .cache import get_cached_key
from .config import (
    HTTPX_TIMEOUT,
    PROXY_KEEPALIVE_EXPIRY,
    PROXY_MAX_CONNECTIONS,
    PROXY_MAX_KEEPALIVE,
    SSE_KEEPALIVE_INTERVAL,
)
from .database import get_db, resolve_routing
from .metrics import enqueue_usage
from .rate_limiter import reconcile_rate, reserve_rate

_httpx_client: httpx.AsyncClient | None = None


async def _validate_api_key(request):
    user_key = _extract_user_key(request.headers.get("authorization"))
    if not user_key:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    key_hash = _hash_key(user_key)
    cached_key = get_cached_key(key_hash)
    if cached_key:
        key_info = cached_key
    else:
        key_info = await asyncio.to_thread(_find_key_info_sync, user_key)
    if not key_info or not key_info.get("active"):
        raise HTTPException(status_code=403, detail="Invalid or inactive API key")
    return key_info


def get_httpx_client() -> httpx.AsyncClient:
    """Get or create the shared async HTTP client with connection pooling."""
    global _httpx_client
    if _httpx_client is None or _httpx_client.is_closed:
        _httpx_client = httpx.AsyncClient(
            timeout=HTTPX_TIMEOUT,
            limits=httpx.Limits(
                max_connections=PROXY_MAX_CONNECTIONS,
                max_keepalive_connections=PROXY_MAX_KEEPALIVE,
                keepalive_expiry=PROXY_KEEPALIVE_EXPIRY,
            ),
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


def _format_server_url(server_url, path):
    """Join a server URL and API path."""
    return f"{server_url.rstrip('/')}/{path.lstrip('/')}"


async def _forward_request(target_url, headers, body, method):
    client = get_httpx_client()
    try:
        resp = await client.request(method=method, url=target_url, headers=headers, content=body)
        return resp.status_code, resp.content
    except httpx.HTTPError as e:
        raise HTTPException(status_code=504 if isinstance(e, httpx.TimeoutException) else 502, detail=str(e))


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
    """Rough input-token estimate from the request body, ~4 characters per token."""
    raw = next((body_json[k] for k in ("messages", "prompt", "input") if k in body_json), "")
    if isinstance(raw, str):
        raw = [raw]
    elif isinstance(raw, list) and raw and isinstance(raw[0], dict):
        raw = [m.get("content", "") for m in raw]
    return max(1, sum(len(x) for x in raw if isinstance(x, str)) // 4)


async def _check_rate_limit(key_info, body_json):
    """Check rate limit for a request. Returns 429 response or None."""
    if not body_json:
        return None, None
    est_tokens = _estimate_input_tokens(body_json)
    allowed, retry_after, ws = await asyncio.to_thread(
        reserve_rate, key_info["id"], key_info["rpm_limit"], key_info["tpm_limit"], est_tokens
    )
    if not allowed:
        return _rate_limit_response(retry_after), None
    return None, ws


async def _build_proxy_context(request, path, *, body_bytes=None, body_json=None):
    key_info = await _validate_api_key(request)
    if body_bytes is None:
        body_bytes = await request.body()
    if body_json is None:
        try:
            body_json = orjson.loads(body_bytes)
        except orjson.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON body")
    model_name = body_json.get("model", "")
    routing = await asyncio.to_thread(resolve_routing, key_info["id"], model_name)
    real_model_name = routing["real_model"] if routing else model_name
    display_name = model_name
    if display_name != real_model_name:
        body_json["model"] = real_model_name
        body_bytes = orjson.dumps(body_json)
    if not routing:
        raise HTTPException(status_code=404, detail=f"No server configured for model '{display_name}'")
    return key_info, routing, display_name, real_model_name, body_json, body_bytes


_HOP_BY_HOP = frozenset(
    ["host", "authorization", "content-length", "transfer-encoding", "connection", "keep-alive", "te", "upgrade"]
)


def _build_upstream(server, request, path):
    """Build the target URL and upstream headers for forwarding."""
    target_url = _format_server_url(server["url"], path)
    upstream_headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
    if server.get("api_key"):
        upstream_headers["authorization"] = f"Bearer {server['api_key']}"
    return target_url, upstream_headers


async def _check_and_setup(request, path, *, body_bytes=None, body_json=None):
    """Validate the key, resolve routing, reserve rate quota and build the upstream request."""
    ki, rt, dn, rm, bj, bb = await _build_proxy_context(request, path, body_bytes=body_bytes, body_json=body_json)
    rr, aw = await _check_rate_limit(ki, bj)
    if rr:
        return rr
    return ki, rt, dn, rm, bj, bb, *_build_upstream(rt, request, path), aw


def _rate_limit_response(retry_after):
    """Build a 429 JSON response with Retry-After header."""
    return JSONResponse(
        content={"error": {"message": "Rate limit exceeded", "type": "rate_limit", "retry_after": retry_after}},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


async def proxy_non_streaming(request, path, *, body_bytes=None, body_json=None):
    result = await _check_and_setup(request, path, body_bytes=body_bytes, body_json=body_json)
    if isinstance(result, JSONResponse):
        return result
    ki, rt, dn, rm, bj, bb, tu, uh, aw = result
    sc, rb = await _forward_request(tu, uh, bb, request.method)
    pt, ct, pm, pr = _parse_usage_from_body(rb)
    et = _estimate_input_tokens(bj)
    reconcile_rate(ki["id"], pt + ct, aw, et)
    enqueue_usage(ki["id"], rt["server_id"], dn, rm, pt, ct, pm, pr, ki["name"], rt.get("server_name", ""))
    return Response(content=rb, status_code=sc, media_type="application/json")


async def proxy_streaming(request, path, *, body_bytes=None, body_json=None):
    """Proxy a streaming request, emitting SSE keepalives while the upstream is still thinking."""
    result = await _check_and_setup(request, path, body_bytes=body_bytes, body_json=body_json)
    if isinstance(result, JSONResponse):
        return result
    ki, rt, dn, rm, bj, bb, tu, uh, aw = result
    et = _estimate_input_tokens(bj)
    client = get_httpx_client()
    upstream = client.build_request("POST", tu, headers=uh, content=bb)
    send_task = asyncio.create_task(client.send(upstream, stream=True))
    try:
        if SSE_KEEPALIVE_INTERVAL > 0:
            await asyncio.wait((send_task,), timeout=SSE_KEEPALIVE_INTERVAL)
            if not send_task.done():
                # Headers have not arrived yet: commit to SSE and keep the connection warm from here on.
                return StreamingResponse(
                    _keepalive_stream(send_task, ki, rt, dn, rm, et, aw), media_type="text/event-stream"
                )
        resp = await send_task
    except httpx.HTTPError as e:
        raise HTTPException(status_code=504 if isinstance(e, httpx.TimeoutException) else 502, detail=str(e))
    except BaseException:
        await _discard_send(send_task)
        raise
    if resp.status_code != 200:
        body = await resp.aread()
        await resp.aclose()
        return Response(body, resp.status_code, media_type="application/json")
    return StreamingResponse(_stream_upstream(resp, ki, rt, dn, rm, et, aw), media_type="text/event-stream")


async def _keepalive_stream(send_task, ki, rt, dn, rm, et, aw):
    """Yield SSE comments until the upstream send resolves, then proxy the stream itself."""
    try:
        while not send_task.done():
            await asyncio.wait((send_task,), timeout=SSE_KEEPALIVE_INTERVAL)
            if not send_task.done():
                yield b": keepalive\n\n"
        try:
            resp = send_task.result()
        except httpx.HTTPError as e:
            yield _upstream_error_event(str(e), 504 if isinstance(e, httpx.TimeoutException) else 502)
            return
        if resp.status_code != 200:
            body = await resp.aread()
            await resp.aclose()
            yield _upstream_error_event(body.decode("utf-8", "replace"), resp.status_code)
            return
        async for chunk in _stream_upstream(resp, ki, rt, dn, rm, et, aw):
            yield chunk
    finally:
        # Covers client disconnects before and during streaming: nothing may be left open.
        await _discard_send(send_task)


async def _discard_send(send_task):
    """Cancel an abandoned upstream send and close whichever response actually arrived."""
    send_task.cancel()
    try:
        resp = await send_task
    except BaseException:
        return
    await resp.aclose()


def _upstream_error_event(message, status):
    """Build an OpenAI-style SSE error event for a failure that lands after the 200 was sent."""
    try:
        message = orjson.loads(message)["error"]["message"]
    except Exception:
        pass
    payload = {"error": {"message": str(message)[:500], "type": "upstream_error", "code": status}}
    return b"data: " + orjson.dumps(payload) + b"\n\n"


async def _stream_upstream(resp, ki, rt, dn, rm, et, aw):
    """Forward upstream SSE bytes while accumulating token usage, then bill for the request."""
    tp = tc = 0
    lpm = lpr = 0.0
    dec = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf, seen = b"", 0

    def _account(lines):
        nonlocal tp, tc, lpm, lpr, seen
        for ln in lines:
            try:
                p, c, dc, pm, pr = _parse_sse_chunk(ln)
            except TypeError:
                p, c, dc, pm, pr = 0, 0, 0, 0.0, 0.0
            if p > 0 or c > 0:
                tp, tc = p, c
            seen += dc
            if pm > 0 or pr > 0:
                lpm, lpr = pm, pr

    try:
        async for chunk in resp.aiter_bytes():
            if not chunk:
                continue
            buf += chunk
            idx = buf.rfind(b"\n")
            if idx >= 0:
                _account(dec.decode(buf[: idx + 1], final=False).splitlines())
                buf = buf[idx + 1 :]
            yield chunk
        if buf:
            _account(dec.decode(buf, final=True).splitlines())
    finally:
        await resp.aclose()
        if not (tp or tc):
            tp, tc = et, seen
        reconcile_rate(ki["id"], tp + tc, aw, et)
        enqueue_usage(ki["id"], rt["server_id"], dn, rm, tp, tc, lpm, lpr, ki["name"], rt.get("server_name", ""))


def _parse_sse_chunk(text):
    """Parse SSE chunk: extract timings, usage, and delta count."""
    delta_count = 0
    last_pm = last_pr = 0.0
    last_usage_p = last_usage_c = None
    fallback_p = fallback_c = 0
    try:
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")
        for line in text.strip().split("\n"):
            line = line.strip()
            line = line.removeprefix("data: ")
            if line == "[DONE]":
                continue
            try:
                data = orjson.loads(line)
            except (orjson.JSONDecodeError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            timings = data.get("timings")
            if isinstance(timings, dict) and timings:
                pn, cn = timings.get("prompt_n", 0) or 0, timings.get("predicted_n", 0) or 0
                if pn > 0 or cn > 0:
                    fallback_p, fallback_c = pn, cn
                last_pm, last_pr = timings.get("prompt_ms", 0.0) or 0.0, timings.get("predicted_ms", 0.0) or 0.0
            usage = data.get("usage")
            if isinstance(usage, dict) and usage:
                up, uc = usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)
                if up > 0 or uc > 0:
                    last_usage_p, last_usage_c = up, uc
            for ch in data.get("choices", []):
                if isinstance(ch, dict) and isinstance(ch.get("delta"), dict):
                    content = ch["delta"].get("content")
                    if isinstance(content, str) and content:
                        delta_count += len(content)
    except (UnicodeDecodeError, TypeError):
        pass
    final = (last_usage_p, last_usage_c) if last_usage_p is not None else (fallback_p, fallback_c)
    return (*final, delta_count, last_pm, last_pr)


async def proxy_models(request):
    await _validate_api_key(request)

    with get_db() as conn:
        rows = conn.execute("SELECT url, id FROM servers WHERE active = 1").fetchall()
    if not rows:
        raise HTTPException(status_code=503, detail="No active llama-server configured")

    async def _fetch(row):
        try:
            _, rb = await _forward_request(_format_server_url(row["url"], "/v1/models"), {}, b"", "GET")
            data = orjson.loads(rb)
            if "data" in data:
                return data["data"]
        except Exception:
            pass
        return []

    results = await asyncio.gather(*(_fetch(row) for row in rows), return_exceptions=True)
    return JSONResponse(
        content={"object": "list", "data": [m for r in results if isinstance(r, list) for m in r]}, status_code=200
    )
