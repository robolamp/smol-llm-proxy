import os
import orjson
import secrets
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Header, HTTPException, Query
from fastapi.responses import Response
from smol_llm_proxy.config import ADMIN_KEY, PROXY_HOST, PROXY_PORT
from smol_llm_proxy.database import init_db, get_db
from smol_llm_proxy.proxy import proxy_non_streaming, proxy_streaming, proxy_public
from smol_llm_proxy.cache import clear_route_cache, clear_key_cache, set_bench_cold
from smol_llm_proxy.auth import create_api_key, delete_api_key, toggle_api_key, list_api_keys

set_bench_cold(os.environ.get("BENCH_COLD_CACHE") == "1")


def _parse_usage_filters(request):
    return {
        k: (int(v) if k in ("key_id", "server_id") else v)
        for k in ("key_id", "server_id", "start_date", "end_date")
        if (v := request.query_params.get(k)) is not None
    }


async def _read_json_body(request: Request):
    body = await request.body()
    try:
        return body, orjson.loads(body)
    except orjson.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")


@asynccontextmanager
async def lifespan(app):
    init_db()
    from smol_llm_proxy.config_loader import sync_config
    from smol_llm_proxy.rate_limiter import start_rate_flush
    from smol_llm_proxy.metrics import start_retention_cleanup

    sync_config()
    start_rate_flush()
    start_retention_cleanup()
    yield
    from smol_llm_proxy.proxy import shutdown_httpx_client
    from smol_llm_proxy.metrics import _shutdown_async_logger, stop_retention_cleanup
    from smol_llm_proxy.rate_limiter import stop_rate_flush

    await shutdown_httpx_client()
    await _shutdown_async_logger()
    stop_rate_flush()
    stop_retention_cleanup()


app = FastAPI(title="smol-llm-proxy", lifespan=lifespan)


def _check_admin(authorization: str | None):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing admin Authorization")
    key = authorization[7:].strip()
    if not ADMIN_KEY or not secrets.compare_digest(key, ADMIN_KEY):
        raise HTTPException(status_code=403, detail="Invalid admin key")


@app.post("/admin/servers")
async def admin_create_server(request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    data = await request.json()
    try:
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO servers (name, url, api_key) VALUES (?, ?, ?)",
                (data["name"], data["url"], data.get("api_key", "")),
            )
    except Exception:
        raise HTTPException(status_code=409, detail="Server with this name already exists")
    clear_route_cache()
    return {"ok": True, "id": cursor.lastrowid}


@app.get("/admin/servers")
async def admin_list_servers(authorization: str | None = Header(None)):
    _check_admin(authorization)
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM servers ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.delete("/admin/servers/{server_id}")
async def admin_delete_server(server_id: int, authorization: str | None = Header(None)):
    _check_admin(authorization)
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM servers WHERE id = ?", (server_id,))
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail="Server not found")
    clear_route_cache()
    return {"ok": True}


@app.patch("/admin/servers/{server_id}")
async def admin_update_server(server_id: int, request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    data = await request.json()
    fields, params = [], []
    for key in ("url", "api_key", "active"):
        if key in data:
            fields.append(f"{key} = ?")
            params.append(int(data[key]) if key == "active" else data[key])
    params.append(server_id)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    with get_db() as conn:
        conn.execute(f"UPDATE servers SET {', '.join(fields)} WHERE id = ?", params)
    clear_route_cache()
    return {"ok": True}


@app.post("/admin/servers/{server_id}/models")
async def admin_assign_model(
    server_id: int,
    request: Request,
    reassign: str | None = Query(None),
    authorization: str | None = Header(None),
):
    _check_admin(authorization)
    data = await request.json()
    model_name = data.get("model_name", "")
    if not model_name:
        raise HTTPException(status_code=400, detail="model_name is required")
    with get_db() as conn:
        existing = conn.execute(
            "SELECT sm.server_id, s.name FROM server_models sm JOIN servers s ON s.id = sm.server_id WHERE sm.model_name = ?",
            (model_name,),
        ).fetchone()
        if existing:
            existing_server_id = existing["server_id"]
            existing_server_name = existing["name"]
            if existing_server_id != server_id and (reassign or reassign == "true"):
                conn.execute("DELETE FROM server_models WHERE model_name = ?", (model_name,))
                conn.execute(
                    "INSERT INTO server_models (server_id, model_name) VALUES (?, ?)",
                    (server_id, model_name),
                )
            else:
                raise HTTPException(
                    status_code=409,
                    detail=f"Model '{model_name}' already assigned to server '{existing_server_name}' (id={existing_server_id}). Use ?reassign=true to move.",
                )
        else:
            try:
                conn.execute(
                    "INSERT INTO server_models (server_id, model_name) VALUES (?, ?)",
                    (server_id, model_name),
                )
            except Exception:
                raise HTTPException(status_code=409, detail="Model already assigned to this server")
    clear_route_cache()
    return {"ok": True}


@app.delete("/admin/servers/{server_id}/models/{model_name}")
async def admin_unassign_model(server_id: int, model_name: str, authorization: str | None = Header(None)):
    _check_admin(authorization)
    with get_db() as conn:
        conn.execute(
            "DELETE FROM server_models WHERE server_id = ? AND model_name = ?",
            (server_id, model_name),
        )
    clear_route_cache()
    return {"ok": True}


@app.post("/admin/aliases")
async def admin_create_alias(request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    data = await request.json()
    alias_name = data.get("alias_name", "")
    real_model_name = data.get("real_model_name", "")
    if not alias_name or not real_model_name:
        raise HTTPException(status_code=400, detail="alias_name and real_model_name are required")
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO model_aliases (alias_name, real_model_name) VALUES (?, ?)", (alias_name, real_model_name)
            )
    except Exception:
        raise HTTPException(status_code=409, detail="Alias already exists")
    clear_route_cache()
    return {"ok": True}


@app.patch("/admin/aliases/{alias_name}")
async def admin_update_alias(alias_name: str, request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    data = await request.json()
    real_model_name = data.get("real_model_name", "")
    if not real_model_name:
        raise HTTPException(status_code=400, detail="real_model_name is required")
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM model_aliases WHERE alias_name = ?", (alias_name,)).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Alias not found")
        conn.execute("UPDATE model_aliases SET real_model_name = ? WHERE alias_name = ?", (real_model_name, alias_name))
    clear_route_cache()
    return {"ok": True}


@app.delete("/admin/aliases/{alias_name}")
async def admin_delete_alias(alias_name: str, authorization: str | None = Header(None)):
    _check_admin(authorization)
    with get_db() as conn:
        conn.execute("DELETE FROM model_aliases WHERE alias_name = ?", (alias_name,))
    clear_route_cache()
    return {"ok": True}


@app.get("/admin/aliases")
async def admin_list_aliases(authorization: str | None = Header(None)):
    _check_admin(authorization)
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM model_aliases ORDER BY id").fetchall()
    return [dict(r) for r in rows]


@app.post("/admin/keys")
async def admin_create_key(request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    data = await request.json()
    name = data.get("name", "unnamed")
    result = create_api_key(name)
    return {"ok": True, "id": result["id"], "key": result["key"], "name": result["name"]}


@app.delete("/admin/keys/{key_id}")
async def admin_delete_key(key_id: int, authorization: str | None = Header(None)):
    _check_admin(authorization)
    deleted = delete_api_key(key_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Key not found")
    return {"ok": True}


@app.patch("/admin/keys/{key_id}/toggle")
async def admin_toggle_key(key_id: int, request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    data = await request.json()
    active = data.get("active", True)
    result = toggle_api_key(key_id, active)
    if not result:
        raise HTTPException(status_code=404, detail="Key not found")
    return dict(result)


@app.put("/admin/keys/{key_id}/limits")
async def admin_set_rate_limits(key_id: int, request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    data = await request.json()
    rpm = data.get("rpm_limit", 100)
    tpm = data.get("tpm_limit", 50000)
    if not isinstance(rpm, int) or isinstance(rpm, bool) or rpm < 0:
        raise HTTPException(status_code=400, detail="rpm_limit must be a non-negative integer")
    if not isinstance(tpm, int) or isinstance(tpm, bool) or tpm < 0:
        raise HTTPException(status_code=400, detail="tpm_limit must be a non-negative integer")
    with get_db() as conn:
        conn.execute("UPDATE api_keys SET rpm_limit = ?, tpm_limit = ? WHERE id = ?", (rpm, tpm, key_id))
        row = conn.execute(
            "SELECT id, name, active, rpm_limit, tpm_limit FROM api_keys WHERE id = ?", (key_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Key not found")
    clear_key_cache()
    return dict(row)


@app.get("/admin/keys")
async def admin_list_keys(authorization: str | None = Header(None)):
    _check_admin(authorization)
    return list_api_keys()


@app.get("/admin/usage")
async def admin_get_usage(request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    from smol_llm_proxy.metrics import get_usage_logs, get_usage_summary

    filters = _parse_usage_filters(request)
    return {"logs": get_usage_logs(**filters, limit=100, offset=0), "summary": get_usage_summary(**filters)}


@app.get("/admin/usage/summary")
async def admin_get_usage_summary(request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    from smol_llm_proxy.metrics import get_usage_summary

    return get_usage_summary(**_parse_usage_filters(request))


@app.get("/admin/usage/summary/real")
async def admin_get_usage_summary_real(request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    from smol_llm_proxy.metrics import get_usage_summary_by_real

    return get_usage_summary_by_real(**_parse_usage_filters(request))


@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    body_bytes, data = await _read_json_body(request)
    fn = proxy_streaming if data.get("stream") else proxy_non_streaming
    return await fn(request, "v1/chat/completions", body_bytes=body_bytes, body_json=data)


@app.post("/v1/completions")
async def proxy_completions(request: Request):
    body_bytes, data = await _read_json_body(request)
    fn = proxy_streaming if data.get("stream") else proxy_non_streaming
    return await fn(request, "v1/completions", body_bytes=body_bytes, body_json=data)


@app.post("/v1/embeddings")
async def proxy_embeddings(request: Request):
    body_bytes, body_json = await _read_json_body(request)
    return await proxy_non_streaming(request, "v1/embeddings", body_bytes=body_bytes, body_json=body_json)


@app.get("/v1/models")
async def proxy_models():
    return await proxy_public(b"")


@app.get("/health")
async def health():
    try:
        import asyncio

        cnt = await asyncio.to_thread(_health_sync)
        return {"status": "ok", "active_servers": cnt}
    except Exception:
        print("health check failed", flush=True)
        return Response(content='{"status":"error"}', media_type="application/json", status_code=503)


def _health_sync():
    with get_db() as conn:
        return conn.execute("SELECT COUNT(*) as cnt FROM servers WHERE active = 1").fetchone()["cnt"]


if __name__ == "__main__":
    import sys
    import uvicorn

    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, loop="uvloop" if sys.platform != "win32" else "asyncio")
