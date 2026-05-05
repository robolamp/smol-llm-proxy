"""FastAPI application with admin and proxy endpoints."""

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

from smol_llm_proxy.config import ADMIN_KEY, PROXY_HOST, PROXY_PORT
from smol_llm_proxy.database import init_db, get_db
from smol_llm_proxy.auth import (
    create_api_key, delete_api_key, toggle_api_key, list_api_keys,
)
from smol_llm_proxy.proxy import proxy_non_streaming, proxy_streaming, proxy_public
from smol_llm_proxy.cache import clear_key_cache, clear_alias_cache, clear_route_cache, clear_all

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    init_db()
    from smol_llm_proxy.config_loader import sync_config, CONFIG_PATH
    import os
    print(f"DB_PATH={os.environ.get('DB_PATH')} CONFIG_PATH={CONFIG_PATH}", flush=True)
    sync_config()
    yield

app = FastAPI(title="smol-llm-proxy", lifespan=lifespan)


# ── Admin helpers ────────────────────────────────────────────────────────

def _check_admin(authorization: str | None):
    """Validate admin Bearer token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing admin Authorization")
    token = authorization[7:].strip()
    if token != ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Invalid admin key")


# ── Admin: servers ───────────────────────────────────────────────────────

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
    return JSONResponse({"ok": True, "id": cursor.lastrowid})


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
    fields = []
    params = []
    for key in ("url", "api_key", "active"):
        if key in data:
            fields.append(f"{key} = ?")
            params.append(data[key] if key != "active" else int(data[key]))
    params.append(server_id)
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    with get_db() as conn:
        conn.execute(f"UPDATE servers SET {', '.join(fields)} WHERE id = ?", params)
    return {"ok": True}


# ── Admin: server models (routing) ───────────────────────────────────────

@app.post("/admin/servers/{server_id}/models")
async def admin_assign_model(server_id: int, request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    data = await request.json()
    model_name = data.get("model_name", "")
    if not model_name:
        raise HTTPException(status_code=400, detail="model_name is required")
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO server_models (server_id, model_name) VALUES (?, ?)",
                (server_id, model_name),
            )
    except Exception:
        raise HTTPException(status_code=409, detail="Model already assigned to a server")
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


# ── Admin: model aliases ─────────────────────────────────────────────────

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
                "INSERT INTO model_aliases (alias_name, real_model_name) VALUES (?, ?)",
                (alias_name, real_model_name),
            )
    except Exception:
        raise HTTPException(status_code=409, detail="Alias already exists")
    clear_alias_cache()
    return {"ok": True}


@app.delete("/admin/aliases/{alias_name}")
async def admin_delete_alias(alias_name: str, authorization: str | None = Header(None)):
    _check_admin(authorization)
    with get_db() as conn:
        conn.execute("DELETE FROM model_aliases WHERE alias_name = ?", (alias_name,))
    clear_alias_cache()
    return {"ok": True}


@app.get("/admin/aliases")
async def admin_list_aliases(authorization: str | None = Header(None)):
    _check_admin(authorization)
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM model_aliases ORDER BY id").fetchall()
    return [dict(r) for r in rows]


# ── Admin: API keys ──────────────────────────────────────────────────────

@app.post("/admin/keys")
async def admin_create_key(request: Request, authorization: str | None = Header(None)):
    _check_admin(authorization)
    data = await request.json()
    name = data.get("name", "unnamed")
    key = create_api_key(name)
    return JSONResponse({"ok": True, "key": key, "name": name})


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
    return result


@app.get("/admin/keys")
async def admin_list_keys(authorization: str | None = Header(None)):
    _check_admin(authorization)
    return list_api_keys()


# ── Admin: usage / metrics ───────────────────────────────────────────────

@app.get("/admin/usage")
async def admin_get_usage(
    key_id: str | None = None,
    server_id: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    authorization: str | None = Header(None),
):
    _check_admin(authorization)
    from smol_llm_proxy.metrics import get_usage_logs, get_usage_summary

    filters = {}
    if key_id is not None:
        filters["key_id"] = int(key_id)
    if server_id is not None:
        filters["server_id"] = int(server_id)
    if start_date is not None:
        filters["start_date"] = start_date
    if end_date is not None:
        filters["end_date"] = end_date

    logs = get_usage_logs(**filters)
    summary = get_usage_summary(**filters)
    return {"logs": logs, "summary": summary}


# ── Proxy: chat completions ──────────────────────────────────────────────

@app.post("/v1/chat/completions")
async def proxy_chat_completions(request: Request):
    body = await request.body()
    try:
        import json
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    is_streaming = data.get("stream", False)
    if is_streaming:
        return await proxy_streaming(request, "v1/chat/completions")
    return await proxy_non_streaming(request, "v1/chat/completions")


# ── Proxy: completions ───────────────────────────────────────────────────

@app.post("/v1/completions")
async def proxy_completions(request: Request):
    body = await request.body()
    try:
        import json
        data = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    is_streaming = data.get("stream", False)
    if is_streaming:
        return await proxy_streaming(request, "v1/completions")
    return await proxy_non_streaming(request, "v1/completions")


# ── Proxy: embeddings ────────────────────────────────────────────────────

@app.post("/v1/embeddings")
async def proxy_embeddings(request: Request):
    return await proxy_non_streaming(request, "v1/embeddings")


# ── Proxy: models (public) ───────────────────────────────────────────────

@app.get("/v1/models")
async def proxy_models():
    return await proxy_public(b"")


# ── Health ───────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    from smol_llm_proxy.database import get_db
    try:
        with get_db() as conn:
            servers = conn.execute("SELECT COUNT(*) as cnt FROM servers WHERE active = 1").fetchone()["cnt"]
        return {"status": "ok", "active_servers": servers}
    except Exception:
        return JSONResponse({"status": "error"}, status_code=503)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT)
