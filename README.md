# smol-llm-proxy

Lightweight API key proxy for llama.cpp servers with per-user token usage tracking.

Routes requests to multiple llama-servers based on model name, tracks token consumption per user, and manages access keys without restarting the backend.

## Features

- Per-user API keys (create / delete / toggle active)
- Multi-server routing by model name
- Model aliases (`qwen-smol` → `Qwen3.5-2B-UD-Q4_K_XL.gguf`)
- Token usage logging: prompt/completion tokens, timings, TPS
- Streaming and non-streaming proxy support
- SQLite backend (zero external DB dependencies)

## Dependencies

```
fastapi  — ASGI web framework
uvicorn  — ASGI server
httpx    — HTTP client for forwarding
pydantic — request/response validation
sqlite3  — stdlib, built-in database
```

## Quick Start

```bash
# Install
pip install fastapi uvicorn httpx pydantic

# Run (requires ADMIN_KEY env var)
ADMIN_KEY=secret python -m smol_llm_proxy
```

The proxy listens on `0.0.0.0:8000` by default. Override with `PROXY_PORT` and `PROXY_HOST` env vars.

## Setup

### 1. Register a llama-server

```bash
curl -X POST http://localhost:8000/admin/servers \
  -H "Authorization: Bearer secret" \
  -d '{"name": "local", "url": "http://127.0.0.1:8080"}'
```

### 2. Assign a model to the server

```bash
curl -X POST http://localhost:8000/admin/servers/1/models \
  -H "Authorization: Bearer secret" \
  -d '{"model_name": "Qwen3.5-2B-UD-Q4_K_XL.gguf"}'
```

### 3. Create a user key

```bash
curl -X POST http://localhost:8000/admin/keys \
  -H "Authorization: Bearer secret" \
  -d '{"name": "alice"}'
# → {"ok": true, "key": "sk-abc...", "name": "alice"}
```

### 4. Use it

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-abc..." \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen3.5-2B-UD-Q4_K_XL.gguf", "messages": [{"role": "user", "content": "hi"}]}'
```

## Admin API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/admin/servers` | `POST` | Register a llama-server |
| `/admin/servers/{id}` | `DELETE` / `PATCH` | Remove or update server |
| `/admin/servers/{id}/models` | `POST` / `DELETE` | Assign/unassign model name |
| `/admin/keys` | `POST` | Create user key |
| `/admin/keys/{key}` | `DELETE` | Revoke key |
| `/admin/keys/{key}/toggle` | `PATCH` | Activate/deactivate key |
| `/admin/usage` | `GET` | View token usage logs |

All admin endpoints require `Authorization: Bearer <ADMIN_KEY>` header.

## Usage Logs

Each request logs: user, server, model name, prompt/completion tokens, timings (ms), and total tokens. No conversation content is stored.

```bash
curl "http://localhost:8000/admin/usage?key_id=1" \
  -H "Authorization: Bearer secret"
```

## Architecture

```
[users] ──HTTPS──> [proxy :8000] ──HTTP──> [llama-server :8080]
                      │
                      ├── validate API key (SQLite)
                      ├── route by model name → server
                      ├── forward request (+ replace auth header)
                      └── log tokens from response
```


